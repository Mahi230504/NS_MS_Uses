"""Tests for the external-trigger entry point (Android voice -> webhook).

Two phases: handle_external_trigger proposes (no launch), handle_external_confirm
acts on the spoken yes/no.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from bot.apps import get_app, get_task
from bot.handlers import Handlers
from bot.router import Route
from bot.session import SessionState


def _make(*, paired: bool = True, router=None):
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    orch = MagicMock()
    orch.run_task = AsyncMock(return_value=None)
    users = MagicMock()
    users.is_allowed.return_value = paired
    h = Handlers(
        application=app,
        orchestrator=orch,
        hitl=MagicMock(),
        users=users,
        pairing=MagicMock(),
        admin_id=1,
        repo=None,
        router=router,
    )
    return h, app, orch


def _route(param):
    app = get_app("blinkit")
    return Route(app=app, task=get_task(app, "order"), param=param)


class _FixedRouter:
    def __init__(self, route):
        self._route = route

    async def route(self, text):
        return self._route


class TestPropose:
    async def test_unpaired_user_rejected(self) -> None:
        h, app, orch = _make(paired=False)
        msg = await h.handle_external_trigger(42, "order milk")
        assert "paired" in msg
        assert 42 not in h._pending
        orch.run_task.assert_not_called()

    async def test_blank_text_rejected(self) -> None:
        h, app, orch = _make()
        msg = await h.handle_external_trigger(42, "   ")
        assert msg
        assert 42 not in h._pending

    async def test_already_running_rejected(self) -> None:
        h, app, orch = _make()
        h._running[42] = MagicMock(done=MagicMock(return_value=False))
        msg = await h.handle_external_trigger(42, "order milk")
        assert "already running" in msg
        assert 42 not in h._pending

    async def test_freeform_proposes_without_launching(self) -> None:
        h, app, orch = _make(router=None)
        msg = await h.handle_external_trigger(42, "do a barrel roll")
        orch.run_task.assert_not_called()  # nothing runs until confirmed
        assert 42 in h._pending
        assert h._pending[42].description == "do a barrel roll"
        assert h._pending[42].launch_package is None
        # The literal "Confirm?" is the contract token the Android client
        # matches on to prompt for a spoken yes/no (data.message.includes).
        assert "barrel roll" in msg and "Confirm?" in msg

    async def test_route_proposes_with_launch_package(self) -> None:
        h, app, orch = _make(router=_FixedRouter(_route("milk")))
        msg = await h.handle_external_trigger(42, "order milk on blinkit")
        orch.run_task.assert_not_called()
        pending = h._pending[42]
        assert pending.launch_package == "com.grofers.customerapp"
        assert "milk" in pending.description
        assert "Blinkit" in msg

    async def test_route_needs_param_defers_to_telegram(self) -> None:
        h, app, orch = _make(router=_FixedRouter(_route(None)))
        msg = await h.handle_external_trigger(42, "order on blinkit")
        orch.run_task.assert_not_called()
        assert 42 not in h._pending  # no proposal; handed to Telegram instead
        app.bot.send_message.assert_awaited()
        assert h._sessions.get(42).state == SessionState.AWAITING_PARAM
        assert "Telegram" in msg


class TestConfirm:
    async def test_yes_launches_pending(self) -> None:
        h, app, orch = _make(router=_FixedRouter(_route("milk")))
        await h.handle_external_trigger(42, "order milk on blinkit")
        msg = await h.handle_external_confirm(42, True)
        await asyncio.sleep(0)
        orch.run_task.assert_called_once()
        assert (
            orch.run_task.call_args.kwargs["launch_package"]
            == "com.grofers.customerapp"
        )
        assert "milk" in orch.run_task.call_args.args[0].description
        assert 42 not in h._pending  # consumed
        assert h._sessions.get(42).state == SessionState.RUNNING
        assert "Telegram" in msg

    async def test_yes_launches_freeform_without_package(self) -> None:
        h, app, orch = _make(router=None)
        await h.handle_external_trigger(42, "play jazz")
        await h.handle_external_confirm(42, True)
        await asyncio.sleep(0)
        orch.run_task.assert_called_once()
        assert orch.run_task.call_args.kwargs["launch_package"] is None

    async def test_no_cancels_without_launching(self) -> None:
        h, app, orch = _make(router=None)
        await h.handle_external_trigger(42, "play jazz")
        msg = await h.handle_external_confirm(42, False)
        await asyncio.sleep(0)
        orch.run_task.assert_not_called()
        assert 42 not in h._pending
        assert "cancel" in msg.lower()

    async def test_confirm_without_pending(self) -> None:
        h, app, orch = _make()
        msg = await h.handle_external_confirm(42, True)
        orch.run_task.assert_not_called()
        assert "nothing to confirm" in msg.lower()

    async def test_expired_pending_not_launched(self) -> None:
        h, app, orch = _make(router=None)
        await h.handle_external_trigger(42, "play jazz")
        # Backdate the proposal past its TTL.
        h._pending[42].created_at -= 1000
        msg = await h.handle_external_confirm(42, True)
        await asyncio.sleep(0)
        orch.run_task.assert_not_called()
        assert "nothing to confirm" in msg.lower()


class TestRun:
    """Single-shot path for clients without a working confirm prompt."""

    async def test_run_proposes_and_launches_in_one_call(self) -> None:
        h, app, orch = _make(router=_FixedRouter(_route("milk")))
        msg = await h.handle_external_run(42, "order milk on blinkit")
        await asyncio.sleep(0)
        orch.run_task.assert_called_once()
        assert (
            orch.run_task.call_args.kwargs["launch_package"]
            == "com.grofers.customerapp"
        )
        assert "milk" in orch.run_task.call_args.args[0].description
        assert 42 not in h._pending  # consumed by the auto-confirm
        assert h._sessions.get(42).state == SessionState.RUNNING
        assert "Telegram" in msg

    async def test_run_freeform_launches_without_package(self) -> None:
        h, app, orch = _make(router=None)
        await h.handle_external_run(42, "play jazz")
        await asyncio.sleep(0)
        orch.run_task.assert_called_once()
        assert orch.run_task.call_args.kwargs["launch_package"] is None

    async def test_run_param_needed_defers_and_does_not_launch(self) -> None:
        # When the routed task needs a param, there's no pending intent to
        # auto-confirm — must defer to Telegram, not launch blind.
        h, app, orch = _make(router=_FixedRouter(_route(None)))
        msg = await h.handle_external_run(42, "order on blinkit")
        await asyncio.sleep(0)
        orch.run_task.assert_not_called()
        assert 42 not in h._pending
        assert "Telegram" in msg

    async def test_run_unpaired_does_not_launch(self) -> None:
        h, app, orch = _make(paired=False)
        msg = await h.handle_external_run(42, "order milk")
        orch.run_task.assert_not_called()
        assert "paired" in msg
