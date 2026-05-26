"""Unit tests for the free-form intent router."""
from __future__ import annotations

import pytest

from bot.router import Route, Router


class _FakeProvider:
    """Returns a canned response, records the calls."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def complete_text(
        self, system_prompt: str, user_prompt: str, max_tokens: int = 200
    ) -> str:
        self.calls.append(
            {"system": system_prompt, "user": user_prompt, "max_tokens": max_tokens}
        )
        return self.reply


class TestRouter:
    async def test_parses_clean_json(self) -> None:
        provider = _FakeProvider(
            '{"app_id": "blinkit", "task_id": "order", "param": "milk"}'
        )
        router = Router(provider)
        route = await router.route("order milk on blinkit")
        assert route is not None
        assert route.app.id == "blinkit"
        assert route.task.id == "order"
        assert route.param == "milk"

    async def test_parses_json_in_code_fence(self) -> None:
        provider = _FakeProvider(
            'Here you go:\n```json\n'
            '{"app_id":"blinkit","task_id":"order","param":"milk"}\n```'
        )
        router = Router(provider)
        route = await router.route("milk on blinkit")
        assert route is not None
        assert route.param == "milk"

    async def test_returns_none_on_null_match(self) -> None:
        provider = _FakeProvider(
            '{"app_id": null, "task_id": null, "param": null}'
        )
        router = Router(provider)
        assert await router.route("hello there") is None

    async def test_returns_none_on_unknown_app(self) -> None:
        provider = _FakeProvider(
            '{"app_id": "nonexistent", "task_id": "order", "param": "x"}'
        )
        router = Router(provider)
        assert await router.route("...") is None

    async def test_returns_none_on_unknown_task(self) -> None:
        provider = _FakeProvider(
            '{"app_id": "blinkit", "task_id": "nonsuch", "param": "x"}'
        )
        router = Router(provider)
        assert await router.route("...") is None

    async def test_returns_none_on_garbage(self) -> None:
        provider = _FakeProvider("the model just rambled here")
        router = Router(provider)
        assert await router.route("...") is None

    async def test_empty_text_skips_call(self) -> None:
        provider = _FakeProvider('{"app_id":"blinkit","task_id":"order","param":"milk"}')
        router = Router(provider)
        assert await router.route("") is None
        assert await router.route("   ") is None
        assert provider.calls == []  # no LLM call wasted on empty input

    async def test_provider_exception_returns_none(self) -> None:
        class _Failing:
            async def complete_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                raise RuntimeError("openrouter down")

        router = Router(_Failing())
        assert await router.route("order milk") is None

    async def test_listing_includes_every_app(self) -> None:
        # The system prompt embeds the (app, task) listing — make sure it
        # actually covers every app in the registry, else the router can't
        # route to apps not in its listing.
        from bot.apps import APPS

        provider = _FakeProvider('{}')
        router = Router(provider)
        await router.route("ping")
        sent_user_prompt = provider.calls[0]["user"]
        for app in APPS:
            assert f"app_id={app.id}" in sent_user_prompt

    async def test_param_extraction_with_whitespace(self) -> None:
        provider = _FakeProvider(
            '{"app_id": "blinkit", "task_id": "order", "param": "   milk and eggs  "}'
        )
        router = Router(provider)
        route = await router.route("...")
        assert route is not None and route.param == "milk and eggs"

    async def test_empty_param_becomes_none(self) -> None:
        provider = _FakeProvider(
            '{"app_id": "blinkit", "task_id": "order", "param": ""}'
        )
        router = Router(provider)
        route = await router.route("...")
        assert route is not None and route.param is None
