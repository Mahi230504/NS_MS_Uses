"""Orchestrator tests for the `report` terminal and read-only probe mode."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.orchestrator import Orchestrator
from agent.state_machine import Task, TaskState
from agent.ui_tree import UiElement
from security.audit_logger import AuditLogger
from security.hitl_gate import HitlGate

# Reuse the established fakes from the main orchestrator test module.
from agent.test_orchestrator import _FakeAdb, _ScriptedVision, _usage


def _orch(adb: _FakeAdb, vision: _ScriptedVision, tmp_path: Path) -> Orchestrator:
    return Orchestrator(
        adb, HitlGate(), AuditLogger(tmp_path), vision, session_timeout_seconds=10
    )


class TestReportTerminal:
    async def test_report_sets_state_done_and_report(self, tmp_path: Path) -> None:
        vision = _ScriptedVision(
            [({"action": "report", "data": {"price": 99, "item_name": "Milk"}}, _usage())]
        )
        task = Task(user_id=1, description="probe milk")
        await _orch(_FakeAdb(), vision, tmp_path).run_task(task, read_only=True)
        assert task.state is TaskState.DONE
        assert task.report == {"price": 99, "item_name": "Milk"}

    async def test_report_without_data_coerces_to_empty(self, tmp_path: Path) -> None:
        vision = _ScriptedVision([({"action": "report"}, _usage())])
        task = Task(user_id=1, description="probe")
        await _orch(_FakeAdb(), vision, tmp_path).run_task(task, read_only=True)
        assert task.state is TaskState.DONE
        assert task.report == {}


class TestReadOnlyLoop:
    async def test_add_note_tap_is_rejected_not_executed(self, tmp_path: Path) -> None:
        adb = _FakeAdb()
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 10, "y": 20, "note": "tap ADD to cart for milk"}, _usage()),
                ({"action": "report", "data": {"price": 99}}, _usage()),
            ]
        )
        task = Task(user_id=1, description="probe milk")
        await _orch(adb, vision, tmp_path).run_task(task, read_only=True)
        # The ADD tap was rejected before execution; only the report ran.
        assert adb.taps == []
        assert task.state is TaskState.DONE
        assert task.report == {"price": 99}

    async def test_benign_tap_is_allowed(self, tmp_path: Path) -> None:
        adb = _FakeAdb()
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 10, "y": 20, "note": "tap the search bar"}, _usage()),
                ({"action": "report", "data": {"price": 49}}, _usage()),
            ]
        )
        task = Task(user_id=1, description="probe milk")
        await _orch(adb, vision, tmp_path).run_task(task, read_only=True)
        assert adb.taps == [(10, 20)]  # search tap executed
        assert task.state is TaskState.DONE

    async def test_not_read_only_allows_add_tap(self, tmp_path: Path) -> None:
        # The same ADD tap is fine in a normal (ordering) run.
        adb = _FakeAdb()
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 10, "y": 20, "note": "tap ADD to cart for milk"}, _usage()),
                ({"action": "done", "summary": "added"}, _usage()),
            ]
        )
        task = Task(user_id=1, description="order milk")
        await _orch(adb, vision, tmp_path).run_task(task)  # read_only defaults False
        assert adb.taps == [(10, 20)]


class TestReadOnlyGuardUnit:
    def _action_elem(self) -> UiElement:
        return UiElement(
            text="ADD", desc="", resource_id="add_btn", class_name="Button",
            cx=50, cy=50, bounds=(0, 0, 100, 100), clickable=True, is_action=True,
        )

    def test_element_based_rejection(self) -> None:
        action = {"action": "tap", "x": 50, "y": 50, "note": "tap the button"}
        assert Orchestrator._readonly_violation_rejection(action, [self._action_elem()])

    def test_note_based_rejection_without_tree(self) -> None:
        action = {"action": "tap", "x": 1, "y": 1, "note": "confirm booking"}
        assert Orchestrator._readonly_violation_rejection(action, [])

    def test_benign_navigation_allowed(self) -> None:
        action = {"action": "tap", "x": 1, "y": 1, "note": "tap the search bar"}
        assert Orchestrator._readonly_violation_rejection(action, []) is None

    def test_non_tap_ignored(self) -> None:
        assert Orchestrator._readonly_violation_rejection({"action": "swipe"}, []) is None
