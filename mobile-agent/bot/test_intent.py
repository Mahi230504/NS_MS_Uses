"""Unit tests for the intent classifier (single vs compare vs unknown)."""
from __future__ import annotations

import pytest

from bot.intent import (
    CompareIntent,
    IntentClassifier,
    RankingKey,
    RunSavedIntent,
    SaveIntent,
    ScheduleIntent,
    SingleIntent,
    UnknownIntent,
)
from bot.router import Router


class _FakeProvider:
    """Returns canned replies in order (last repeats); records calls."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies) or [""]
        self.calls: list[str] = []

    async def complete_text(
        self, system_prompt: str, user_prompt: str, max_tokens: int = 200
    ) -> str:
        self.calls.append(user_prompt)
        idx = min(len(self.calls) - 1, len(self._replies) - 1)
        return self._replies[idx]


def _classifier(*replies: str, max_candidates: int = 3) -> IntentClassifier:
    provider = _FakeProvider(*replies)
    return IntentClassifier(provider, Router(provider), max_candidates=max_candidates)


class TestCompare:
    async def test_two_named_apps(self) -> None:
        c = _classifier(
            '{"intent":"compare","category":"food","app_ids":["swiggy","zomato"],'
            '"item":"margherita pizza","ranking_key":"cheapest"}'
        )
        r = await c.classify("cheapest pizza on swiggy or zomato")
        assert isinstance(r, CompareIntent)
        assert {a.id for a in r.candidates} == {"swiggy", "zomato"}
        assert r.item == "margherita pizza"
        assert r.ranking_key is RankingKey.CHEAPEST
        assert r.category == "food"

    async def test_ranking_fastest(self) -> None:
        c = _classifier(
            '{"intent":"compare","category":"mobility","app_ids":["uber","ola"],'
            '"item":"airport","ranking_key":"fastest"}'
        )
        r = await c.classify("fastest cab to the airport on uber or ola")
        assert isinstance(r, CompareIntent)
        assert r.ranking_key is RankingKey.FASTEST

    async def test_category_only_expands(self) -> None:
        c = _classifier(
            '{"intent":"compare","category":"groceries","app_ids":[],'
            '"item":"milk","ranking_key":"cheapest"}'
        )
        r = await c.classify("which app is cheapest for milk")
        assert isinstance(r, CompareIntent)
        assert 2 <= len(r.candidates) <= 3
        assert all(a.category == "groceries" for a in r.candidates)

    async def test_single_named_app_expands_within_category(self) -> None:
        c = _classifier(
            '{"intent":"compare","category":"food","app_ids":["swiggy"],'
            '"item":"biryani","ranking_key":"best"}'
        )
        r = await c.classify("best biryani on swiggy")
        assert isinstance(r, CompareIntent)
        assert "swiggy" in {a.id for a in r.candidates}
        assert len(r.candidates) >= 2

    async def test_caps_at_max_candidates(self) -> None:
        c = _classifier(
            '{"intent":"compare","category":"groceries","app_ids":[],'
            '"item":"milk","ranking_key":"cheapest"}',
            max_candidates=2,
        )
        r = await c.classify("cheapest milk anywhere")
        assert isinstance(r, CompareIntent)
        assert len(r.candidates) == 2

    async def test_no_item_is_not_compare(self) -> None:
        # A compare with no item can't be probed → degrade. Router fallback
        # also fails to find a route here → Unknown.
        c = _classifier(
            '{"intent":"compare","category":"food","app_ids":["swiggy","zomato"],'
            '"item":null,"ranking_key":"cheapest"}'
        )
        r = await c.classify("compare food")
        assert not isinstance(r, CompareIntent)


class TestSingle:
    async def test_direct_single(self) -> None:
        c = _classifier(
            '{"intent":"single","category":"groceries","app_ids":["blinkit"],'
            '"task_id":"order","param":"milk"}'
        )
        r = await c.classify("order milk on blinkit")
        assert isinstance(r, SingleIntent)
        assert r.route.app.id == "blinkit"
        assert r.route.task.id == "order"
        assert r.route.param == "milk"

    async def test_router_shaped_reply_falls_back_to_router(self) -> None:
        # No "intent" key → classifier defers to the router, which parses the
        # same reply (its own contract) into a route.
        c = _classifier('{"app_id":"spotify","task_id":"play","param":"jazz"}')
        r = await c.classify("play jazz on spotify")
        assert isinstance(r, SingleIntent)
        assert r.route.app.id == "spotify"
        assert r.route.param == "jazz"

    async def test_unresolvable_single_degrades(self) -> None:
        c = _classifier(
            '{"intent":"single","app_ids":["bogusapp"],"task_id":"order","param":"x"}'
        )
        r = await c.classify("do a thing")
        assert isinstance(r, UnknownIntent)


class TestUnknownAndEdges:
    async def test_unknown(self) -> None:
        c = _classifier('{"intent":"unknown","app_ids":[]}')
        r = await c.classify("what's the weather like")
        assert isinstance(r, UnknownIntent)

    async def test_empty_text_skips_llm(self) -> None:
        provider = _FakeProvider('{"intent":"unknown"}')
        c = IntentClassifier(provider, Router(provider))
        assert isinstance(await c.classify(""), UnknownIntent)
        assert isinstance(await c.classify("   "), UnknownIntent)
        assert provider.calls == []

    async def test_provider_exception_degrades_to_unknown(self) -> None:
        class _Boom:
            async def complete_text(self, *a, **k):  # type: ignore[no-untyped-def]
                raise RuntimeError("down")

        c = IntentClassifier(_Boom(), Router(_Boom()))
        assert isinstance(await c.classify("order milk"), UnknownIntent)

    async def test_garbage_reply_degrades(self) -> None:
        c = _classifier("the model rambled without any json")
        assert isinstance(await c.classify("hello"), UnknownIntent)


class TestSaveAndRunSaved:
    async def test_save_intent(self) -> None:
        c = _classifier('{"intent":"save","name":"sunday order"}')
        r = await c.classify("save this as my sunday order")
        assert isinstance(r, SaveIntent) and r.name == "sunday order"

    async def test_run_saved_intent(self) -> None:
        c = _classifier('{"intent":"run_saved","name":"sunday order"}')
        r = await c.classify("run my sunday order")
        assert isinstance(r, RunSavedIntent) and r.name == "sunday order"

    async def test_save_without_name_degrades(self) -> None:
        c = _classifier('{"intent":"save","name":null}')
        assert not isinstance(await c.classify("save this"), SaveIntent)


class TestScheduleIntent:
    async def test_schedule_daily(self) -> None:
        c = _classifier(
            '{"intent":"schedule","app_ids":["blinkit"],"task_id":"order",'
            '"param":"milk","schedule_freq":"daily","schedule_time":"09:00",'
            '"pay_automatically":false}'
        )
        r = await c.classify("order milk on blinkit every day at 9am")
        assert isinstance(r, ScheduleIntent)
        assert r.freq == "daily" and r.time_str == "09:00"
        assert r.route.app.id == "blinkit" and r.route.param == "milk"
        assert r.pay_automatically is False

    async def test_schedule_weekly_with_pay(self) -> None:
        c = _classifier(
            '{"intent":"schedule","app_ids":["zepto"],"task_id":"order",'
            '"param":"groceries","schedule_freq":"weekly","schedule_time":"09:00",'
            '"schedule_weekday":"sunday","pay_automatically":true}'
        )
        r = await c.classify("every sunday 9am reorder groceries on zepto and pay")
        assert isinstance(r, ScheduleIntent)
        assert r.weekday_name == "sunday" and r.pay_automatically is True

    async def test_schedule_unresolvable_task_degrades(self) -> None:
        c = _classifier(
            '{"intent":"schedule","app_ids":[],"schedule_freq":"daily",'
            '"schedule_time":"09:00"}'
        )
        assert not isinstance(await c.classify("schedule something"), ScheduleIntent)


class TestRankingKey:
    def test_parse_known(self) -> None:
        assert RankingKey.parse("FASTEST") is RankingKey.FASTEST
        assert RankingKey.parse("best") is RankingKey.BEST

    def test_parse_unknown_defaults_cheapest(self) -> None:
        assert RankingKey.parse("weird") is RankingKey.CHEAPEST
        assert RankingKey.parse(None) is RankingKey.CHEAPEST
