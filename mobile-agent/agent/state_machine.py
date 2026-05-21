"""Task states: IDLE, RUNNING, AWAITING_APPROVAL, DONE, FAILED."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class TaskState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass
class Task:
    user_id: int
    description: str
    state: TaskState = TaskState.IDLE
    step_count: int = 0
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    history: list[dict] = field(default_factory=list)
    pending_action: dict | None = None
    failure_reason: str | None = None
    final_summary: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    latest_rpd_remaining: int | None = None
