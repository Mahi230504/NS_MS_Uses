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
    # True once the user has approved a "Cart review …" need_approval whose
    # message stated that the approval also covers payment. Subsequent
    # payment-shaped HITL gates auto-grant on this flag so the user only
    # confirms once per checkout. Reset implicitly per task (new Task
    # instance = fresh flag).
    payment_pre_approved: bool = False
    # Set by the scheduler for an unattended run whose schedule opted into
    # "pay automatically". When True, the orchestrator auto-grants the
    # cart-review + payment HITL gates (and only those — never OTP / permission
    # / delete) so a recurring order can complete with nobody watching. Default
    # False, so interactive runs are unaffected. See feature #5.
    auto_approve_payment: bool = False
    # Structured payload from a `report` terminal action (the read-only probe
    # path used by cross-app comparison). None for ordinary tasks that finish
    # with `done`. Carries e.g. {price, currency, eta, available, item_name,
    # notes} — see config.prompts.PROBE_ADDENDUM for the schema the model fills.
    report: dict | None = None
