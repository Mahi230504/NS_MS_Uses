"""Tests for saved-quick-task wiring in the handlers (feature #4)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.persistence import SavedTaskRepository, TaskRepository
from agent.state_machine import Task, TaskState
from bot.handlers import Handlers, _LastRun, _slugify
from bot.intent import RunSavedIntent, SaveIntent
from bot.session import SessionState


class _FakeClassifier:
    def __init__(self, intent):
        self._intent = intent

    async def classify(self, text):
        return self._intent


async def _setup(tmp_path, *, intent=None):
    base = TaskRepository(tmp_path / "t.db")
    await base.initialize()
    saved = SavedTaskRepository(tmp_path / "t.db")
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    orch = MagicMock()
    orch.run_task = AsyncMock(
        return_value=Task(user_id=42, description="x", state=TaskState.DONE)
    )
    users = MagicMock()
    users.is_allowed.return_value = True
    h = Handlers(
        application=app,
        orchestrator=orch,
        hitl=MagicMock(),
        users=users,
        pairing=MagicMock(),
        admin_id=1,
        repo=base,
        classifier=_FakeClassifier(intent) if intent is not None else None,
        saved=saved,
    )
    return h, app, orch, saved


def _update(user_id: int, text: str):
    u = MagicMock()
    u.effective_user.id = user_id
    u.message.text = text
    u.message.reply_text = AsyncMock()
    return u


def test_slugify() -> None:
    assert _slugify("Sunday Order!") == "sunday-order"
    assert _slugify("  morning   coffee ") == "morning-coffee"
    assert _slugify("!!!") == ""


class TestSaveOffer:
    async def test_offer_fires_on_success(self, tmp_path) -> None:
        h, app, orch, saved = await _setup(tmp_path)
        h._last_run[42] = _LastRun(description="do x")

        async def _done():
            return Task(user_id=42, description="do x", state=TaskState.DONE)

        fut = asyncio.ensure_future(_done())
        await fut
        h._on_run_done(fut, 42)
        await asyncio.sleep(0.02)  # let the scheduled offer run
        sent = " ".join(str(c) for c in app.bot.send_message.call_args_list)
        assert "quick task" in sent

    async def test_no_offer_on_failure(self, tmp_path) -> None:
        h, app, orch, saved = await _setup(tmp_path)
        h._last_run[42] = _LastRun(description="do x")

        async def _failed():
            return Task(user_id=42, description="do x", state=TaskState.FAILED)

        fut = asyncio.ensure_future(_failed())
        await fut
        h._on_run_done(fut, 42)
        await asyncio.sleep(0.02)
        app.bot.send_message.assert_not_called()


class TestSaveFlow:
    async def test_awaiting_save_name_saves_last_run(self, tmp_path) -> None:
        h, app, orch, saved = await _setup(tmp_path)
        h._last_run[42] = _LastRun(
            description="Add milk to the cart", app_id="blinkit",
            task_id="order", param="milk",
            launch_package="com.grofers.customerapp",
        )
        h._sessions.get(42).state = SessionState.AWAITING_SAVE_NAME
        await h.message(_update(42, "Sunday order"), MagicMock())
        row = await saved.get(42, "sunday-order")
        assert row is not None and row.app_id == "blinkit" and row.param == "milk"
        assert h._sessions.get(42).state == SessionState.IDLE

    async def test_save_core_without_last_run(self, tmp_path) -> None:
        h, app, orch, saved = await _setup(tmp_path)
        msg = await h._save_last_run_core(42, "anything")
        assert "recent task" in msg.lower()


class TestRunSaved:
    async def test_run_resolves_and_launches(self, tmp_path) -> None:
        h, app, orch, saved = await _setup(tmp_path)
        await saved.upsert(
            user_id=42, slug="sunday-order", label="Sunday order",
            raw_description="Add milk", app_id="blinkit", task_id="order",
            param="milk", launch_package="com.grofers.customerapp",
        )
        msg = _update(42, "/run sunday-order").message
        row = await h._resolve_saved(42, "sunday order")  # fuzzy by label
        assert row is not None
        await h._run_saved(msg, 42, row)
        orch.run_task.assert_called_once()
        assert (
            orch.run_task.call_args.kwargs["launch_package"]
            == "com.grofers.customerapp"
        )
        # mark_run bumped the counter.
        assert (await saved.get(42, "sunday-order")).run_count == 1

    async def test_resolve_fuzzy_and_substring(self, tmp_path) -> None:
        h, app, orch, saved = await _setup(tmp_path)
        await saved.upsert(user_id=42, slug="morning-coffee", label="Morning coffee",
                           raw_description="x")
        assert (await h._resolve_saved(42, "coffee")).slug == "morning-coffee"   # substring
        assert (await h._resolve_saved(42, "mornin coffe")).slug == "morning-coffee"  # fuzzy
        assert await h._resolve_saved(42, "pizza") is None

    async def test_run_missing_via_message(self, tmp_path) -> None:
        h, app, orch, saved = await _setup(tmp_path, intent=RunSavedIntent(name="ghost"))
        upd = _update(42, "run my ghost")
        await h.message(upd, MagicMock())
        orch.run_task.assert_not_called()
        assert "no saved task" in upd.message.reply_text.call_args.args[0].lower()


class TestVoice:
    async def test_voice_save_intent_saves_immediately(self, tmp_path) -> None:
        h, app, orch, saved = await _setup(tmp_path, intent=SaveIntent(name="Sunday order"))
        h._last_run[42] = _LastRun(description="Add milk", app_id="blinkit",
                                   task_id="order", param="milk")
        msg = await h.handle_external_trigger(42, "save this as sunday order")
        assert "saved" in msg.lower()
        assert await saved.get(42, "sunday-order") is not None

    async def test_voice_run_saved_stages_confirm(self, tmp_path) -> None:
        h, app, orch, saved = await _setup(tmp_path, intent=RunSavedIntent(name="Sunday order"))
        await saved.upsert(user_id=42, slug="sunday-order", label="Sunday order",
                           raw_description="Add milk", app_id="blinkit",
                           task_id="order", param="milk",
                           launch_package="com.grofers.customerapp")
        msg = await h.handle_external_trigger(42, "run my sunday order")
        assert "Confirm?" in msg
        assert h._pending[42].launch_package == "com.grofers.customerapp"
        # Confirm launches it.
        await h.handle_external_confirm(42, True)
        await asyncio.sleep(0)
        orch.run_task.assert_called_once()
