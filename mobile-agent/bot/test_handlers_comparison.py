"""Tests for cross-app comparison wiring in the handlers (Telegram + voice)."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from agent.comparison import ComparisonResult, Quote
from bot.apps import get_app
from bot.handlers import (
    Handlers,
    _PendingComparison,
    _primary_order_task,
    _render_comparison,
)
from bot.intent import CompareIntent, RankingKey


class _FakeClassifier:
    def __init__(self, intent) -> None:
        self._intent = intent

    async def classify(self, text):
        return self._intent


class _FakeEngine:
    def __init__(self, result) -> None:
        self._result = result
        self.calls: list[dict] = []

    async def run(self, *, targets, item, category, ranking_key, user_id,
                  on_progress=None, should_abort=None):
        self.calls.append({"item": item, "ranking_key": ranking_key,
                           "targets": [t.app_id for t in targets]})
        if on_progress is not None:
            await on_progress("checking…")
        return self._result


def _compare_intent():
    return CompareIntent(
        category="food",
        candidates=(get_app("swiggy"), get_app("zomato")),
        item="pizza",
        ranking_key=RankingKey.CHEAPEST,
    )


def _result():
    z = Quote("zomato", "Zomato", ok=True, price=229.0, eta="30 mins", available=True)
    s = Quote("swiggy", "Swiggy", ok=True, price=249.0, eta="25 min", available=True)
    return ComparisonResult(
        item="pizza", category="food", ranking_key="cheapest",
        quotes=[z, s], ranked=[z, s], winner=z,
    )


def _make(*, intent=None, result=None, repo=None):
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    orch = MagicMock()
    orch.run_task = AsyncMock(return_value=None)
    users = MagicMock()
    users.is_allowed.return_value = True
    h = Handlers(
        application=app,
        orchestrator=orch,
        hitl=MagicMock(),
        users=users,
        pairing=MagicMock(),
        admin_id=1,
        repo=repo,
        classifier=_FakeClassifier(intent) if intent is not None else None,
        comparison=_FakeEngine(result) if result is not None else None,
    )
    return h, app, orch


class TestMessageDispatch:
    async def test_compare_intent_runs_and_presents(self) -> None:
        h, app, orch = _make(intent=_compare_intent(), result=_result())
        update = MagicMock()
        update.effective_user.id = 42
        update.message.text = "cheapest pizza on swiggy or zomato"
        update.message.reply_text = AsyncMock()

        await h.message(update, MagicMock())
        assert 42 in h._running
        await h._running[42]  # let the comparison coroutine finish

        # A ranked table was posted and the order buttons are live.
        assert app.bot.send_message.await_count >= 1
        assert 42 in h._pending_comparison
        assert h._pending_comparison[42].result.winner.app_id == "zomato"
        # The winner order is staged for a voice "yes".
        assert h._pending[42].launch_package == get_app("zomato").package
        # No order placed yet (read-only probes only).
        orch.run_task.assert_not_called()


class TestComparisonPick:
    def _query(self, data: str):
        q = MagicMock()
        q.from_user.id = 42
        q.data = data
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        q.message = MagicMock()
        return q

    async def test_pick_orders_chosen_app(self) -> None:
        h, app, orch = _make()
        h._pending_comparison[42] = _PendingComparison(
            result=_result(), comparison_id=None, created_at=time.monotonic()
        )
        q = self._query("cmp:swiggy")
        await h.menu_callback(MagicMock(callback_query=q), MagicMock())
        await asyncio.sleep(0)

        orch.run_task.assert_called_once()
        assert (
            orch.run_task.call_args.kwargs["launch_package"]
            == get_app("swiggy").package
        )
        assert "pizza" in orch.run_task.call_args.args[0].description
        assert 42 not in h._pending_comparison  # consumed

    async def test_pick_none_places_no_order(self) -> None:
        h, app, orch = _make()
        h._pending_comparison[42] = _PendingComparison(
            result=_result(), comparison_id=None, created_at=time.monotonic()
        )
        q = self._query("cmp:none")
        await h.menu_callback(MagicMock(callback_query=q), MagicMock())
        await asyncio.sleep(0)
        orch.run_task.assert_not_called()

    async def test_expired_comparison_rejected(self) -> None:
        h, app, orch = _make()
        h._pending_comparison[42] = _PendingComparison(
            result=_result(), comparison_id=None, created_at=time.monotonic() - 100000
        )
        q = self._query("cmp:swiggy")
        await h.menu_callback(MagicMock(callback_query=q), MagicMock())
        await asyncio.sleep(0)
        orch.run_task.assert_not_called()
        assert "expired" in q.edit_message_text.call_args.args[0].lower()


class TestVoiceComparison:
    async def test_voice_stages_then_confirm_runs(self) -> None:
        h, app, orch = _make(intent=_compare_intent(), result=_result())
        msg = await h.handle_external_trigger(42, "cheapest pizza on swiggy or zomato")
        assert "Confirm?" in msg
        assert h._pending[42].compare is not None

        await h.handle_external_confirm(42, True)
        assert 42 in h._running
        await h._running[42]
        assert 42 in h._pending_comparison
        orch.run_task.assert_not_called()  # comparison probes only, no order


class TestRendering:
    def test_render_table_and_buttons(self) -> None:
        text, markup = _render_comparison(_result())
        assert "Zomato" in text and "🏆" in text
        assert markup is not None
        flat = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "cmp:zomato" in flat
        assert "cmp:swiggy" in flat
        assert "cmp:none" in flat

    def test_render_no_winner_has_no_buttons(self) -> None:
        res = ComparisonResult(
            item="x", category="food", ranking_key="cheapest",
            quotes=[Quote("a", "A", ok=False, failure_reason="boom")],
            ranked=[], winner=None,
        )
        text, markup = _render_comparison(res)
        assert markup is None
        assert "couldn't" in text.lower()

    def test_primary_order_task_by_category(self) -> None:
        assert _primary_order_task(get_app("blinkit")).id == "order"
        assert _primary_order_task(get_app("amazon")).id == "search"
        assert _primary_order_task(get_app("uber")).id == "book"
