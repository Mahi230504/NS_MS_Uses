"""Unit tests for Task / TaskState."""
from __future__ import annotations

from datetime import datetime, timezone

from agent.state_machine import Task, TaskState


class TestTask:
    def test_defaults(self) -> None:
        t = Task(user_id=1, description="find shoes")
        assert t.state is TaskState.IDLE
        assert t.step_count == 0
        assert t.history == []
        assert t.pending_action is None
        assert t.failure_reason is None
        assert t.final_summary is None
        assert isinstance(t.start_time, datetime)
        assert t.start_time.tzinfo == timezone.utc

    def test_state_enum_values_stable(self) -> None:
        # Other modules and the UI rely on these string values.
        assert TaskState.IDLE.value == "idle"
        assert TaskState.RUNNING.value == "running"
        assert TaskState.AWAITING_APPROVAL.value == "awaiting_approval"
        assert TaskState.DONE.value == "done"
        assert TaskState.FAILED.value == "failed"
        assert TaskState.TIMED_OUT.value == "timed_out"

    def test_history_is_per_instance(self) -> None:
        a = Task(user_id=1, description="a")
        b = Task(user_id=2, description="b")
        a.history.append({"x": 1})
        assert b.history == []
