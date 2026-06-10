"""Tests for schedule wiring in the handlers (feature #5)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.persistence import ScheduleRepository, TaskRepository
from agent.state_machine import Task, TaskState
from bot.apps import get_app, get_task
from bot.handlers import Handlers
from bot.intent import ScheduleIntent
from bot.router import Route

IST = "Asia/Kolkata"


class _FakeClassifier:
    def __init__(self, intent):
        self._intent = intent

    async def classify(self, text):
        return self._intent


def _schedule_intent(*, freq="daily", time_str="09:00", pay=False, weekday=None):
    app = get_app("blinkit")
    route = Route(app=app, task=get_task(app, "order"), param="milk")
    return ScheduleIntent(
        route=route, freq=freq, time_str=time_str, weekday_name=weekday,
        pay_automatically=pay, name="Morning milk",
    )


async def _setup(tmp_path, *, intent=None):
    base = TaskRepository(tmp_path / "t.db")
    await base.initialize()
    sched = ScheduleRepository(tmp_path / "t.db")
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    orch = MagicMock()
    orch.run_task = AsyncMock(
        return_value=Task(user_id=42, description="x", state=TaskState.DONE)
    )
    users = MagicMock()
    users.is_allowed.return_value = True
    h = Handlers(
        application=app, orchestrator=orch, hitl=MagicMock(), users=users,
        pairing=MagicMock(), admin_id=1, repo=base,
        classifier=_FakeClassifier(intent) if intent is not None else None,
        schedules=sched, timezone_name=IST,
    )
    return h, app, orch, sched


def _update(user_id, text):
    u = MagicMock()
    u.effective_user.id = user_id
    u.message.text = text
    u.message.reply_text = AsyncMock()
    return u


class TestCreate:
    async def test_message_creates_schedule(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(tmp_path, intent=_schedule_intent())
        upd = _update(42, "order milk on blinkit every day at 9am")
        await h.message(upd, MagicMock())
        rows = await sched.list_for(42)
        assert len(rows) == 1
        assert rows[0].freq == "daily" and rows[0].at_minute == 540
        assert rows[0].app_id == "blinkit" and rows[0].param == "milk"
        assert "Scheduled" in upd.message.reply_text.call_args.args[0]

    async def test_pay_automatically_flag_persisted(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(tmp_path, intent=_schedule_intent(pay=True))
        await h.message(_update(42, "order milk every day at 9am and pay automatically"), MagicMock())
        rows = await sched.list_for(42)
        assert rows[0].pay_automatically == 1

    async def test_voice_creates_schedule(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(tmp_path, intent=_schedule_intent())
        msg = await h.handle_external_trigger(42, "order milk on blinkit every day at 9am")
        assert "Scheduled" in msg
        assert len(await sched.list_for(42)) == 1


class TestLaunchScheduled:
    async def test_launch_runs_and_sets_autopay(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(tmp_path)
        sid = await sched.insert(
            user_id=42, name="Milk", freq="daily", at_minute=540, tz=IST,
            raw_description="Add milk", next_run_at="2026-01-01T00:00:00+00:00",
            app_id="blinkit", task_id="order", param="milk",
            launch_package="com.grofers.customerapp", pay_automatically=True,
        )
        row = await sched.get(sid)
        ok = await h.launch_scheduled(row)
        assert ok is True
        orch.run_task.assert_called_once()
        # auto-pay schedule -> the launched Task carries auto_approve_payment.
        launched_task = orch.run_task.call_args.args[0]
        assert launched_task.auto_approve_payment is True
        # bounded approval window passed through.
        assert orch.run_task.call_args.kwargs["approval_timeout"] is not None

    async def test_collision_skips(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(tmp_path)
        h._running[42] = MagicMock(done=MagicMock(return_value=False))
        sid = await sched.insert(
            user_id=42, name="Milk", freq="daily", at_minute=540, tz=IST,
            raw_description="Add milk", next_run_at="2026-01-01T00:00:00+00:00",
        )
        ok = await h.launch_scheduled(await sched.get(sid))
        assert ok is False
        orch.run_task.assert_not_called()


class TestUnschedule:
    async def test_unschedule_deletes(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(tmp_path)
        sid = await sched.insert(
            user_id=42, name="Milk", freq="daily", at_minute=540, tz=IST,
            raw_description="Add milk", next_run_at="2026-01-01T00:00:00+00:00",
        )
        ctx = MagicMock()
        ctx.args = [str(sid)]
        upd = _update(42, f"/unschedule {sid}")
        await h.unschedule(upd, ctx)
        assert await sched.get(sid) is None
        assert "Removed" in upd.message.reply_text.call_args.args[0]
