"""Orchestrator emits step/state events to the dashboard bus (un-throttled)."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.orchestrator import Orchestrator
from agent.state_machine import Task, TaskState
from security.audit_logger import AuditLogger
from security.hitl_gate import HitlGate

from agent.test_orchestrator import _FakeAdb, _ScriptedVision, _usage


class _RecBus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)

    def latest_snapshot(self):
        return self.events[-1] if self.events else None


class TestOrchestratorEmits:
    async def test_emits_state_and_steps(self, tmp_path: Path) -> None:
        bus = _RecBus()
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 10, "y": 20, "note": "tap"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        orch = Orchestrator(
            _FakeAdb(), HitlGate(), AuditLogger(tmp_path), vision,
            session_timeout_seconds=10, event_bus=bus,
        )
        await orch.run_task(Task(user_id=1, description="t"))

        types = [e["type"] for e in bus.events]
        assert "state" in types and "step" in types
        # First state event is RUNNING, last is the terminal DONE.
        states = [e for e in bus.events if e["type"] == "state"]
        assert states[0]["state"] == "running"
        assert states[-1]["state"] == "done"
        # A step event carries the tap coords + a screenshot URL.
        steps = [e for e in bus.events if e["type"] == "step"]
        tap = next(s for s in steps if s["action_type"] == "tap")
        assert tap["coords"]["tap"] == {"x": 10, "y": 20}
        assert tap["screenshot_url"] is None or "/screenshot" in tap["screenshot_url"]

    async def test_no_bus_is_fine(self, tmp_path: Path) -> None:
        # event_bus=None (default) must not break a run.
        vision = _ScriptedVision([({"action": "done", "summary": "ok"}, _usage())])
        orch = Orchestrator(
            _FakeAdb(), HitlGate(), AuditLogger(tmp_path), vision,
            session_timeout_seconds=10,
        )
        task = Task(user_id=1, description="t")
        await orch.run_task(task)
        assert task.state is TaskState.DONE
