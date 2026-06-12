"""Action executor: commit_enter gates the post-type ENTER (WhatsApp fix)."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.action_executor import (
    NO_ENTER_COMMIT_PACKAGES,
    ActionExecutionError,
    execute,
)
from agent.orchestrator import Orchestrator
from agent.state_machine import Task, TaskState
from security.audit_logger import AuditLogger
from security.hitl_gate import HitlGate

from agent.test_orchestrator import _FakeAdb, _ScriptedVision, _usage


class _RecordingAdb:
    """Stand-in for AdbController. Records calls; never spawns subprocesses."""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.texts: list[str] = []
        self.key_events: list[int] = []

    async def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    async def type_text(self, text: str) -> None:
        self.texts.append(text)

    async def key_event(self, keycode: int) -> None:
        self.key_events.append(keycode)


class TestTypeCommitEnter:
    async def test_type_sends_enter_by_default(self) -> None:
        adb = _RecordingAdb()
        result = await execute({"action": "type", "text": "hello"}, adb)
        assert adb.texts == ["hello"]
        assert 66 in adb.key_events  # KEYCODE_ENTER commits the search
        assert result == "typed 5 char(s)"

    async def test_type_with_commit_enter_true_sends_enter(self) -> None:
        adb = _RecordingAdb()
        await execute({"action": "type", "text": "hello"}, adb, commit_enter=True)
        assert adb.texts == ["hello"]
        assert 66 in adb.key_events

    async def test_type_with_commit_enter_false_suppresses_enter(self) -> None:
        adb = _RecordingAdb()
        result = await execute(
            {"action": "type", "text": "hello"}, adb, commit_enter=False
        )
        # Text lands, but no ENTER — WhatsApp's "Enter is send" would fire
        # the message before the HITL approval.
        assert adb.texts == ["hello"]
        assert 66 not in adb.key_events
        assert result == "typed 5 char(s)"

    async def test_commit_enter_is_keyword_only(self) -> None:
        adb = _RecordingAdb()
        with pytest.raises(TypeError):
            await execute({"action": "type", "text": "hi"}, adb, False)  # type: ignore[misc]

    async def test_whatsapp_in_no_enter_commit_packages(self) -> None:
        assert "com.whatsapp" in NO_ENTER_COMMIT_PACKAGES

    async def test_unknown_action_still_raises(self) -> None:
        adb = _RecordingAdb()
        with pytest.raises(ActionExecutionError):
            await execute({"action": "frobnicate"}, adb)


class TestOrchestratorSuppressesEnterForWhatsApp:
    """Orchestrator wiring: the tracked foreground package gates commit_enter."""

    @pytest.fixture
    def audit(self, tmp_path: Path) -> AuditLogger:
        return AuditLogger(tmp_path)

    async def test_no_enter_after_type_on_whatsapp(self, audit: AuditLogger) -> None:
        adb = _FakeAdb(foreground_package="com.whatsapp")
        vision = _ScriptedVision(
            [
                ({"action": "type", "text": "good morning",
                  "note": "type the message"}, _usage()),
                ({"action": "done", "summary": "typed"}, _usage()),
            ]
        )
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="whatsapp Mom good morning")
        await orch.run_task(task)

        # The text landed but ENTER was suppressed — WhatsApp's "Enter is
        # send" would have fired the message before approval.
        assert adb.texts == ["good morning"]
        assert 66 not in adb.key_events
        assert task.state is TaskState.DONE

    async def test_enter_still_sent_on_other_packages(
        self, audit: AuditLogger
    ) -> None:
        adb = _FakeAdb(foreground_package="com.grofers.customerapp")
        vision = _ScriptedVision(
            [
                ({"action": "type", "text": "maggi",
                  "note": "type maggi into search"}, _usage()),
                ({"action": "done", "summary": "searched"}, _usage()),
            ]
        )
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="search for maggi")
        await orch.run_task(task)

        assert adb.texts == ["maggi"]
        assert 66 in adb.key_events  # commit behaviour unchanged elsewhere
        assert task.state is TaskState.DONE
