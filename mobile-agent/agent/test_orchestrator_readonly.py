"""Orchestrator tests for the `report` terminal and read-only probe mode."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.orchestrator import (
    Orchestrator,
    _repeated_rejection_count,
    _taps_since_last_type,
)
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
        # `report` is a valid terminal in any mode; missing/!dict data -> {}.
        # Run NOT read-only so the probe-completeness guard (which nudges a
        # price-less report) doesn't intercept this minimal terminal test.
        vision = _ScriptedVision([({"action": "report"}, _usage())])
        task = Task(user_id=1, description="probe")
        await _orch(_FakeAdb(), vision, tmp_path).run_task(task)
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


class TestProbeCompletenessUnit:
    def _task(self, history):
        t = Task(user_id=1, description="probe")
        t.history = history
        return t

    def test_done_is_nudged(self) -> None:
        assert Orchestrator._readonly_probe_incomplete(
            self._task([]), {"action": "done"}
        )

    def test_priceless_report_without_drill_is_nudged(self) -> None:
        hist = [
            {"action": {"action": "type", "text": "pizza"}, "result": "typed 5 char(s)"},
        ]
        hint = Orchestrator._readonly_probe_incomplete(
            self._task(hist), {"action": "report", "data": {"price": None}}
        )
        assert hint is not None

    def test_priceless_report_after_drill_is_accepted(self) -> None:
        hist = [
            {"action": {"action": "type", "text": "pizza"}, "result": "typed"},
            {"action": {"action": "tap", "x": 1, "y": 2, "note": "open result"}, "result": "tapped (1,2)"},
        ]
        assert Orchestrator._readonly_probe_incomplete(
            self._task(hist), {"action": "report", "data": {"price": None}}
        ) is None

    def test_report_with_price_is_accepted(self) -> None:
        assert Orchestrator._readonly_probe_incomplete(
            self._task([]), {"action": "report", "data": {"price": 199}}
        ) is None

    def test_unavailable_report_is_accepted(self) -> None:
        assert Orchestrator._readonly_probe_incomplete(
            self._task([]),
            {"action": "report", "data": {"price": None, "available": False}},
        ) is None


class TestOverlayDismiss:
    async def test_dismisses_popup_then_proceeds(self, tmp_path: Path) -> None:
        # A popup is shadowing the screen (sparse tree). The orchestrator should
        # press BACK to dismiss it and then proceed — without wasting an LLM
        # call on the popup-shadowed screen.
        adb = _FakeAdb(popup_focused=True)
        vision = _ScriptedVision([({"action": "done", "summary": "ok"}, _usage())])
        task = Task(user_id=1, description="t")
        await _orch(adb, vision, tmp_path).run_task(task)
        assert 4 in adb.key_events  # pressed BACK (KEYCODE_BACK) to dismiss
        assert task.state is TaskState.DONE
        assert len(vision.calls) == 1  # popup step skipped the vision call

    async def test_no_back_when_no_popup(self, tmp_path: Path) -> None:
        adb = _FakeAdb(popup_focused=False)
        vision = _ScriptedVision([({"action": "done", "summary": "ok"}, _usage())])
        await _orch(adb, vision, tmp_path).run_task(Task(user_id=1, description="t"))
        assert 4 not in adb.key_events


class TestRepeatedRejectionCount:
    def _rej(self, x=5, y=5):
        return {"action": {"action": "tap", "x": x, "y": y}, "result": "REJECTED: nope"}

    def test_counts_identical(self) -> None:
        assert _repeated_rejection_count([self._rej()] * 4) == 4

    def test_different_action_breaks(self) -> None:
        # >5px apart so they aren't treated as the same gesture.
        assert _repeated_rejection_count([self._rej(5, 5), self._rej(500, 600)]) == 1

    def test_executed_breaks(self) -> None:
        h = [
            self._rej(5, 5),
            {"action": {"action": "tap", "x": 5, "y": 5}, "result": "tapped (5,5)"},
            self._rej(5, 5),
        ]
        assert _repeated_rejection_count(h) == 1

    def test_empty(self) -> None:
        assert _repeated_rejection_count([]) == 0


class TestTapsSinceLastType:
    def test_counts_taps_after_type(self) -> None:
        hist = [
            {"action": {"action": "type"}, "result": "typed"},
            {"action": {"action": "tap"}, "result": "tapped"},
            {"action": {"action": "tap"}, "result": "tapped"},
        ]
        assert _taps_since_last_type(hist) == 2

    def test_zero_when_no_tap_after_type(self) -> None:
        hist = [{"action": {"action": "type"}, "result": "typed"}]
        assert _taps_since_last_type(hist) == 0

    def test_zero_when_no_type(self) -> None:
        assert _taps_since_last_type([{"action": {"action": "tap"}, "result": "tapped"}]) == 0

    def test_rejected_tap_not_counted(self) -> None:
        hist = [
            {"action": {"action": "type"}, "result": "typed"},
            {"action": {"action": "tap"}, "result": "REJECTED: nope"},
        ]
        assert _taps_since_last_type(hist) == 0


class TestProbeCompletenessLoop:
    async def test_done_then_drill_then_report(self, tmp_path: Path) -> None:
        # Probe tries to bail with `done`; gets nudged; opens a result; reports
        # a real price.
        adb = _FakeAdb()
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 10, "y": 20, "note": "tap search bar"}, _usage()),
                ({"action": "type", "text": "pizza", "note": "type pizza"}, _usage()),
                ({"action": "done"}, _usage()),  # nudged
                ({"action": "tap", "x": 30, "y": 40, "note": "open the first result"}, _usage()),
                ({"action": "report", "data": {"price": 199, "item_name": "Margherita"}}, _usage()),
            ]
        )
        task = Task(user_id=1, description="probe pizza")
        await _orch(adb, vision, tmp_path).run_task(task, read_only=True)
        assert task.state is TaskState.DONE
        assert task.report == {"price": 199, "item_name": "Margherita"}
        assert (30, 40) in adb.taps  # it opened a result

    async def test_repeated_rejection_aborts_fast(self, tmp_path: Path) -> None:
        # Model keeps proposing the same cart tap (rejected in read-only). The
        # breaker aborts after MAX_CONSECUTIVE_REJECTS instead of burning the
        # whole budget; nothing is ever executed.
        adb = _FakeAdb()
        add_tap = (
            {"action": "tap", "x": 10, "y": 20, "note": "tap ADD to cart for milk"},
            _usage(),
        )
        vision = _ScriptedVision([add_tap] * 10)
        task = Task(user_id=1, description="probe")
        await _orch(adb, vision, tmp_path).run_task(task, read_only=True)
        assert task.state is TaskState.FAILED
        assert "rejected action" in (task.failure_reason or "")
        assert adb.taps == []  # never executed
        # Aborted well before the 10 scripted steps (saved LLM calls).
        assert len(vision.calls) <= 5

    async def test_priceless_bail_is_bounded_not_infinite(self, tmp_path: Path) -> None:
        # Model keeps reporting null without drilling — nudged MAX_PROBE_NUDGES
        # times, then accepted so the probe can't loop forever.
        adb = _FakeAdb()
        vision = _ScriptedVision(
            [
                ({"action": "type", "text": "pizza", "note": "type pizza"}, _usage()),
                ({"action": "report", "data": {"price": None}}, _usage()),
                ({"action": "report", "data": {"price": None}}, _usage()),
                ({"action": "report", "data": {"price": None}}, _usage()),
            ]
        )
        task = Task(user_id=1, description="probe pizza")
        await _orch(adb, vision, tmp_path).run_task(task, read_only=True)
        assert task.state is TaskState.DONE
        assert task.report == {"price": None}
