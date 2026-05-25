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
from agent.ui_tree import to_prompt_section as ui_tree_to_prompt
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
# After this many loop-detected events in a single task we give up — the model
# isn't going to escape on its own. Hard-fail with a clear reason.
MAX_LOOPS_BEFORE_ABORT = 4
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
        enable_vision_hitl: bool = False,
    ) -> None:
        self._adb = adb
        self._hitl = hitl
        self._audit = audit
        self._vision = vision
        self._timeout = session_timeout_seconds
        self._users = users
        self._skills = skills
        self._repo = repo
        self._enable_vision_hitl = enable_vision_hitl
        self.on_approval_request: Optional[ApprovalCallback] = None
        self.on_status_update: Optional[StatusCallback] = None
        self._tasks: dict[int, Task] = {}
        self._last_step_status_at: float = 0.0
        self._low_rpd_warned: bool = False
        # Per-task scratch state, reset at run_task() entry.
        self._last_phash: str | None = None
        self._consecutive_synthetic_waits: int = 0
        self._unchanged_streak: int = 0
        self._loops_detected: int = 0
        self._current_task_db_id: int | None = None

    def get_task(self, user_id: int) -> Task | None:
        return self._tasks.get(user_id)

    async def run_task(
        self,
        task: Task,
        *,
        launch_package: str | None = None,
    ) -> Task:
        self._tasks[task.user_id] = task
        self._last_step_status_at = 0.0
        self._low_rpd_warned = False
        self._last_phash = None
        self._consecutive_synthetic_waits = 0
        self._unchanged_streak = 0
        self._loops_detected = 0
        self._current_task_db_id = None
        task.state = TaskState.RUNNING
        await self._persist_insert(task)
        await self._status(task, f"starting: {task.description}")
        # Switch the device's IME to ADBKeyboard for the duration of the task,
        # so the agent's type actions land reliably. Restore the user's normal
        # IME on exit (finally:). Returns None if ADBKeyboard isn't enabled,
        # in which case typing falls back to `adb shell input text`.
        original_ime: str | None = None
        try:
            original_ime = await self._adb.use_adbkeyboard_for_task()
        except Exception:
            original_ime = None
        try:
            if launch_package is not None:
                try:
                    launched = await self._adb.launch_package(launch_package)
                    await self._status(task, f"launched {launched}")
                except AdbError as e:
                    # Don't fail the task — the agent may still be able to
                    # find the app from the home screen. But warn the user.
                    await self._status(
                        task, f"⚠️ couldn't launch {launch_package}: {e}"
                    )
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
        finally:
            # Restore the user's original IME if we swapped for the task.
            # Best-effort — a stuck IME never blocks task completion.
            try:
                await self._adb.restore_ime(original_ime)
            except Exception:
                pass
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
                ui_tree = await self._lookup_ui_tree()
                loop_hint = self._loop_hint(task)
                if loop_hint:
                    self._audit.log_action(
                        task.user_id, task.description, {"action": "loop_hint"},
                        f"INJECTED: {loop_hint}",
                    )
                    if self._loops_detected >= MAX_LOOPS_BEFORE_ABORT:
                        raise OrchestratorError(
                            f"stuck: detected {self._loops_detected} action loops; "
                            "the model isn't escaping. Aborting."
                        )
                response = await self._vision.get_next_action(
                    screenshot_bytes=screenshot,
                    task_description=(
                        f"{task.description}\n\n{loop_hint}" if loop_hint else task.description
                    ),
                    step_history=task.history,
                    screen_size=screen_size,
                    skill_hint=skill_hint,
                    ui_tree=ui_tree,
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
            note = str(action.get("note", "")).strip()
            status_msg = (
                f"step {task.step_count}: {result} — {note}"
                if note
                else f"step {task.step_count}: {result}"
            )
            await self._step_status(task, status_msg)

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

    async def _lookup_ui_tree(self) -> str | None:
        """Fetch the on-screen accessibility tree as a model-friendly text
        block. Returns None on any failure — the model can still operate on
        the screenshot alone, just less precisely.
        """
        try:
            xml = await self._adb.dump_ui_xml()
        except Exception:
            return None
        if not xml:
            return None
        rendered = ui_tree_to_prompt(xml)
        return rendered or None

    def _loop_hint(self, task: Task) -> str:
        """Detect repeated-action and cycle patterns; return a hint or ''.

        Recognised shapes (looking only at state-changing actions in history):
          - A-A          : last two actions identical
          - A-B-A-B      : last four actions form a two-step cycle
        Either pattern increments self._loops_detected; the caller hard-aborts
        when that crosses MAX_LOOPS_BEFORE_ABORT.
        """
        recent_state_changing = [
            h.get("action", {})
            for h in task.history
            if isinstance(h.get("action"), dict)
            and h["action"].get("action") in _STATE_CHANGING
        ]
        if len(recent_state_changing) < 2:
            return ""

        last_two = recent_state_changing[-2:]
        last_four = recent_state_changing[-4:]

        is_aa = _actions_equivalent(last_two[0], last_two[1])
        is_abab = (
            len(last_four) == 4
            and _actions_equivalent(last_four[0], last_four[2])
            and _actions_equivalent(last_four[1], last_four[3])
            and not _actions_equivalent(last_four[0], last_four[1])
        )

        if not (is_aa or is_abab):
            return ""

        self._loops_detected += 1
        shape = "identical" if is_aa else "alternating A↔B cycle"
        return (
            f"NOTE: Your last actions form an {shape} pattern that isn't "
            "advancing the screen. STOP this loop — pick a DIFFERENT element "
            "from the UI list, scroll to surface new options, or emit "
            "`need_approval` if you're genuinely stuck."
        )

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
            self._enable_vision_hitl
            and not needs_approval
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
        # If the approval message can't be delivered (Telegram outage), the
        # subsequent wait_for_approval will time out cleanly — no need to
        # crash the task with the network error.
        if self.on_approval_request is None:
            return
        try:
            await self.on_approval_request(task, action)
        except Exception:
            pass

    async def _status(self, task: Task, message: str) -> None:
        """Lifecycle / error messages — best-effort, never fatal.

        A Telegram outage or transient timeout must not abort an in-flight
        task. We swallow status-callback exceptions; the agent loop keeps
        running and the user can /status the task later.
        """
        if self.on_status_update is None:
            return
        try:
            await self.on_status_update(task, message)
        except Exception:
            pass

    async def _step_status(self, task: Task, message: str) -> None:
        """Per-step progress — throttled + best-effort (same rationale)."""
        if self.on_status_update is None:
            return
        now = time.monotonic()
        if now - self._last_step_status_at < STEP_STATUS_MIN_INTERVAL_SECONDS:
            return
        self._last_step_status_at = now
        try:
            await self.on_status_update(task, message)
        except Exception:
            pass


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


def _actions_equivalent(a: dict, b: dict) -> bool:
    """True if two actions are 'the same effective gesture'.

    For tap/swipe we compare coords (with a tiny tolerance — pixel-perfect
    repeats are rare so any few-pixel jitter still counts as a loop). For
    type we compare the text. Anything else compares by action type only.
    """
    if a.get("action") != b.get("action"):
        return False
    t = a.get("action")
    if t == "tap":
        return abs(int(a.get("x", 0)) - int(b.get("x", 0))) <= 5 \
            and abs(int(a.get("y", 0)) - int(b.get("y", 0))) <= 5
    if t == "type":
        return str(a.get("text", "")) == str(b.get("text", ""))
    if t == "swipe":
        return all(
            abs(int(a.get(k, 0)) - int(b.get(k, 0))) <= 5
            for k in ("x1", "y1", "x2", "y2")
        )
    return True


def _safe_phash(image_bytes: bytes) -> str | None:
    try:
        return compute_phash(image_bytes)
    except ValueError:
        return None
