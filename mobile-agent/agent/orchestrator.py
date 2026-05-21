"""Master agent: receives task, runs loop, reports back."""
from __future__ import annotations

import asyncio
import base64
import time
from typing import Awaitable, Callable, Optional

from agent.action_executor import execute as execute_action
from agent.persistence import TaskRepository
from agent.phash import compute as compute_phash
from agent.providers.base import VisionProvider
from agent.skills import SkillRegistry
from agent.state_machine import Task, TaskState
from bot.users import UserStore
from device.adb_controller import KEYCODE_BACK, AdbController, AdbError
from security.action_validator import validate
from security.audit_logger import AuditLogger
from security.hitl_gate import HitlGate, ReadOnlyViolation


MAX_LOOP_ITERATIONS = 30
# Minimum gap between mid-loop step status messages sent to Telegram. Lifecycle
# messages (starting / done / failed / timeout) bypass this throttle.
STEP_STATUS_MIN_INTERVAL_SECONDS = 2.0
# When the provider reports fewer than this many requests left for the day,
# warn the user so they aren't surprised by a QuotaExceeded mid-task.
LOW_RPD_WARNING_THRESHOLD = 30
# Cap consecutive synthetic waits emitted by dedup. After this many in a row,
# fall through to a real provider call so a frozen UI eventually gets noticed.
MAX_CONSECUTIVE_SYNTHETIC_WAITS = 3
# Outcome-verification: if the screen doesn't change after this many state-
# changing actions in a row, try a back-button recovery, then give up.
UNCHANGED_STREAK_RECOVERY = 2
UNCHANGED_STREAK_FAIL = 3
# Retry policy for ADB action execution. Index = retry attempt (0..n).
RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)
# Action types whose effect we verify with a follow-up screencap.
_STATE_CHANGING = frozenset({"tap", "type", "swipe"})


ApprovalCallback = Callable[[Task, dict], Awaitable[None]]
StatusCallback = Callable[[Task, str], Awaitable[None]]


class OrchestratorError(RuntimeError):
    """Raised internally to short-circuit the loop with a failure reason."""


class Orchestrator:
    """Runs the per-task agent loop defined in CLAUDE.md."""

    def __init__(
        self,
        adb: AdbController,
        hitl: HitlGate,
        audit: AuditLogger,
        vision: VisionProvider,
        session_timeout_seconds: int,
        users: UserStore | None = None,
        skills: SkillRegistry | None = None,
        repo: TaskRepository | None = None,
    ) -> None:
        self._adb = adb
        self._hitl = hitl
        self._audit = audit
        self._vision = vision
        self._timeout = session_timeout_seconds
        self._users = users
        self._skills = skills
        self._repo = repo
        self.on_approval_request: Optional[ApprovalCallback] = None
        self.on_status_update: Optional[StatusCallback] = None
        self._tasks: dict[int, Task] = {}
        self._last_step_status_at: float = 0.0
        self._low_rpd_warned: bool = False
        # Per-task scratch state, reset at run_task() entry.
        self._last_phash: str | None = None
        self._consecutive_synthetic_waits: int = 0
        self._unchanged_streak: int = 0
        self._current_task_db_id: int | None = None

    def get_task(self, user_id: int) -> Task | None:
        return self._tasks.get(user_id)

    async def run_task(self, task: Task) -> Task:
        self._tasks[task.user_id] = task
        self._last_step_status_at = 0.0
        self._low_rpd_warned = False
        self._last_phash = None
        self._consecutive_synthetic_waits = 0
        self._unchanged_streak = 0
        self._current_task_db_id = None
        task.state = TaskState.RUNNING
        await self._persist_insert(task)
        await self._status(task, f"starting: {task.description}")
        try:
            await asyncio.wait_for(self._loop(task), timeout=self._timeout)
        except asyncio.TimeoutError:
            task.state = TaskState.TIMED_OUT
            task.failure_reason = f"task exceeded {self._timeout}s session timeout"
            await self._status(task, task.failure_reason)
        except OrchestratorError as e:
            task.state = TaskState.FAILED
            task.failure_reason = str(e)
            await self._status(task, f"failed: {e}")
        except asyncio.CancelledError:
            task.state = TaskState.FAILED
            task.failure_reason = "cancelled"
            await self._status(task, "cancelled")
            await self._persist_update(task)
            raise
        except Exception as e:
            task.state = TaskState.FAILED
            task.failure_reason = f"unexpected error: {e}"
            await self._status(task, task.failure_reason)
        await self._persist_update(task)
        return task

    async def _loop(self, task: Task) -> None:
        screen_size = await self._safe_screen_size()

        for _ in range(MAX_LOOP_ITERATIONS):
            task.step_count += 1

            screenshot = await self._adb.screencap()
            screenshot_b64 = base64.standard_b64encode(screenshot).decode("ascii")
            current_phash = _safe_phash(screenshot)

            # Dedup: if the screen is visually identical to what the model
            # last saw and we already took at least one action, skip the
            # provider call and synthesize a wait. The model has nothing new
            # to react to.
            synthetic = self._maybe_synthesize_wait(task, current_phash)
            if synthetic is not None:
                action = synthetic
                self._consecutive_synthetic_waits += 1
            else:
                self._consecutive_synthetic_waits = 0
                skill_hint = await self._lookup_skill()
                response = await self._vision.get_next_action(
                    screenshot_bytes=screenshot,
                    task_description=task.description,
                    step_history=task.history,
                    screen_size=screen_size,
                    skill_hint=skill_hint,
                )
                action = response.action
                self._record_usage(task, response.usage)
                await self._maybe_warn_low_rpd(task)
                # `_last_phash` records the screen the MODEL last saw. Update
                # here (after a real call) and nowhere else — the dedup check
                # depends on this invariant.
                self._last_phash = current_phash

            ok, reason = validate(action)
            if not ok:
                self._audit.log_action(
                    task.user_id, task.description, action, f"BLOCKED: {reason}"
                )
                raise OrchestratorError(f"action validation failed: {reason}")

            await self._gate_with_hitl(task, action, screenshot, screenshot_b64, current_phash)

            # Security rule 4: audit BEFORE execute, never after.
            self._audit.log_action(
                task.user_id, task.description, action, "EXECUTING"
            )

            action_type = action.get("action")
            if action_type == "done":
                task.state = TaskState.DONE
                task.final_summary = action.get("summary", "")
                task.history.append({"action": action, "result": "done"})
                await self._persist_step(task, action, "done")
                await self._status(task, f"done: {task.final_summary}")
                return

            result = await self._execute_with_retry(task, action)
            task.history.append({"action": action, "result": result})
            self._audit.log_action(
                task.user_id, task.description, action, f"RESULT: {result}"
            )
            await self._persist_step(task, action, result)
            await self._step_status(task, f"step {task.step_count}: {result}")

            # Outcome verification is meaningful only for state-changing
            # actions. A wait (real or synthetic) doesn't reset the streak —
            # a stuck UI stays stuck whether or not we pause between probes.
            if action_type in _STATE_CHANGING:
                await self._verify_outcome(task, action, current_phash)

        raise OrchestratorError(f"exceeded {MAX_LOOP_ITERATIONS} loop iterations")

    # ------------------------------------------------------------------
    # Dedup + skill lookup

    def _maybe_synthesize_wait(self, task: Task, phash: str | None) -> dict | None:
        if phash is None or self._last_phash is None:
            return None
        if task.step_count <= 1:
            return None
        if phash != self._last_phash:
            return None
        if self._consecutive_synthetic_waits >= MAX_CONSECUTIVE_SYNTHETIC_WAITS:
            return None
        # Don't synthesize back-to-back waits if the model just chose to wait —
        # they're indistinguishable to the next iteration and we want the
        # model to see at least one real call between waits.
        last = task.history[-1]["action"] if task.history else {}
        if last.get("action") == "wait":
            return None
        return {"action": "wait", "reason": "screen unchanged"}

    async def _lookup_skill(self) -> str | None:
        if self._skills is None:
            return None
        try:
            pkg = await self._adb.get_foreground_package()
        except Exception:
            return None
        return self._skills.get(pkg)

    # ------------------------------------------------------------------
    # HITL gate (keyword + vision)

    async def _gate_with_hitl(
        self,
        task: Task,
        action: dict,
        screenshot: bytes,
        screenshot_b64: str,
        screenshot_phash: str | None,
    ) -> None:
        policy = (
            self._users.policy_for(task.user_id).value
            if self._users is not None
            else "confirm_sensitive"
        )
        try:
            needs_approval = await self._hitl.requires_approval(
                action, screenshot_b64, policy
            )
        except ReadOnlyViolation as e:
            self._audit.log_action(
                task.user_id, task.description, action, f"BLOCKED: {e}"
            )
            raise OrchestratorError(f"read-only policy: {e}")

        # Vision-augmented check OR's into the keyword check. We only run it
        # for state-changing actions on the standard policy — terminal actions
        # and read_only / always_approve users are already handled above.
        if (
            not needs_approval
            and policy == "confirm_sensitive"
            and action.get("action") in _STATE_CHANGING
            and screenshot_phash is not None
            and hasattr(self._vision, "classify_yes_no")
        ):
            vision_says = await self._hitl.classify_sensitivity(
                screenshot, screenshot_phash, self._vision  # type: ignore[arg-type]
            )
            if vision_says:
                needs_approval = True
                self._audit.log_action(
                    task.user_id, task.description, action, "VISION_HITL_TRIGGERED"
                )

        if not needs_approval:
            return

        task.state = TaskState.AWAITING_APPROVAL
        task.pending_action = action
        self._audit.log_action(
            task.user_id, task.description, action, "AWAITING_APPROVAL"
        )
        await self._request_approval(task, action)
        approved = await self._hitl.wait_for_approval(task.user_id)
        if not approved:
            self._audit.log_action(
                task.user_id, task.description, action, "DENIED"
            )
            raise OrchestratorError("user denied approval")
        task.state = TaskState.RUNNING
        task.pending_action = None

    # ------------------------------------------------------------------
    # Execution: retry + outcome verification

    async def _execute_with_retry(self, task: Task, action: dict) -> str:
        last_err: Exception | None = None
        for attempt, backoff in enumerate((0.0,) + RETRY_BACKOFF_SECONDS):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                return await execute_action(action, self._adb)
            except AdbError as e:
                last_err = e
                # Append a failure marker so the next provider call sees that
                # the previous attempt didn't land.
                task.history.append(
                    {"action": action, "result": f"ERROR: {e}", "previous_attempt_failed": True}
                )
                self._audit.log_action(
                    task.user_id, task.description, action, f"RETRY[{attempt + 1}]: {e}"
                )
        raise OrchestratorError(f"adb action failed after retries: {last_err}")

    async def _verify_outcome(
        self, task: Task, action: dict, pre_phash: str | None
    ) -> None:
        try:
            post = await self._adb.screencap()
        except AdbError:
            # Flaky adb shouldn't kill the task — let the next iter retake.
            return
        post_phash = _safe_phash(post)
        if pre_phash is None or post_phash is None or post_phash != pre_phash:
            self._unchanged_streak = 0
            return

        self._unchanged_streak += 1
        if self._unchanged_streak >= UNCHANGED_STREAK_FAIL:
            raise OrchestratorError(
                f"screen unchanged after {self._unchanged_streak} actions; aborting"
            )
        if self._unchanged_streak >= UNCHANGED_STREAK_RECOVERY:
            try:
                await self._adb.key_event(KEYCODE_BACK)
            except AdbError as e:
                self._audit.log_action(
                    task.user_id, task.description, action,
                    f"RECOVERY_FAILED: back-button: {e}",
                )
                return
            self._audit.log_action(
                task.user_id, task.description, action, "RECOVERY: back-button"
            )

    # ------------------------------------------------------------------
    # Utilities

    async def _safe_screen_size(self) -> tuple[int, int] | None:
        try:
            return await self._adb.get_screen_size()
        except Exception:
            return None

    @staticmethod
    def _record_usage(task: Task, usage) -> None:
        task.total_input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        task.total_output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        rpd = getattr(usage, "rpd_remaining", None)
        if rpd is not None:
            task.latest_rpd_remaining = int(rpd)

    async def _maybe_warn_low_rpd(self, task: Task) -> None:
        if self._low_rpd_warned:
            return
        rpd = task.latest_rpd_remaining
        if rpd is None or rpd > LOW_RPD_WARNING_THRESHOLD:
            return
        self._low_rpd_warned = True
        await self._status(task, f"⚠️ low daily quota: {rpd} requests remaining")

    async def _request_approval(self, task: Task, action: dict) -> None:
        if self.on_approval_request is not None:
            await self.on_approval_request(task, action)

    async def _status(self, task: Task, message: str) -> None:
        """Always send (lifecycle, errors, warnings)."""
        if self.on_status_update is not None:
            await self.on_status_update(task, message)

    async def _step_status(self, task: Task, message: str) -> None:
        """Per-step progress — throttled so we don't spam Telegram."""
        if self.on_status_update is None:
            return
        now = time.monotonic()
        if now - self._last_step_status_at < STEP_STATUS_MIN_INTERVAL_SECONDS:
            return
        self._last_step_status_at = now
        await self.on_status_update(task, message)


    async def _persist_insert(self, task: Task) -> None:
        if self._repo is None:
            return
        try:
            self._current_task_db_id = await self._repo.insert_task(task)
        except Exception:
            # Persistence is best-effort — never let a DB hiccup kill a task.
            self._current_task_db_id = None

    async def _persist_update(self, task: Task) -> None:
        if self._repo is None or self._current_task_db_id is None:
            return
        try:
            await self._repo.update_state(self._current_task_db_id, task)
        except Exception:
            pass

    async def _persist_step(self, task: Task, action: dict, result: str) -> None:
        if self._repo is None or self._current_task_db_id is None:
            return
        try:
            await self._repo.append_step(
                self._current_task_db_id, task.step_count, action, result
            )
        except Exception:
            pass


def _safe_phash(image_bytes: bytes) -> str | None:
    try:
        return compute_phash(image_bytes)
    except ValueError:
        return None
