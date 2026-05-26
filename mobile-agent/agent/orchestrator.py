"""Master agent: receives task, runs loop, reports back."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import Awaitable, Callable, Optional

_log = logging.getLogger("mobile_agent.orchestrator")

from agent.action_executor import execute as execute_action
from agent.persistence import TaskRepository
from agent.phash import compute as compute_phash
from agent.providers.base import VisionProvider
from agent.skills import SkillRegistry
from agent.state_machine import Task, TaskState
from agent.ui_tree import (
    UiElement,
    find_smallest_element_at,
    is_coord_in_elements,
    parse as ui_tree_parse,
    to_prompt_section as ui_tree_to_prompt,
)
from bot.users import UserStore
from device.adb_controller import KEYCODE_BACK, AdbController, AdbError
from security.action_validator import validate
from security.audit_logger import AuditLogger
from security.hitl_gate import HitlGate, ReadOnlyViolation


MAX_LOOP_ITERATIONS = 30
# Minimum gap between mid-loop step status messages sent to Telegram. Lifecycle
# messages (starting / done / failed / timeout) bypass this throttle.
STEP_STATUS_MIN_INTERVAL_SECONDS = 2.0
# Regexes for the "model gives up" need_approval reasons. The orchestrator
# treats matching reasons as an illegal escape (rule 10 in the system prompt)
# and rejects them unless the task history shows the model actually scrolled.
# Each pattern is matched case-insensitively against the reason string.
# Note: there is NO textual "after scrolling" bypass — earlier versions of
# this guard accepted that suffix as proof of scrolling, but the model
# learned to append it without actually scrolling. Only an executed swipe in
# task.history counts.
_GIVEUP_PATTERNS = (
    # "no <anything> products found" / "no products found" / "no Maggi found"
    re.compile(r"\bno\b[^.]{0,40}\b(products?|results?|items?|matches?)\b", re.IGNORECASE),
    re.compile(r"\bno\s+\w+\s+found\b", re.IGNORECASE),  # "no Maggi found"
    re.compile(r"\bcouldn'?t\s+find\b", re.IGNORECASE),
    re.compile(r"\bcould\s+not\s+find\b", re.IGNORECASE),
    re.compile(r"\bnothing\s+(found|matches?|matching)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+available\b", re.IGNORECASE),
)
# Tokens that indicate the most recent tap was supposed to add a product to
# the cart. If the model claims that, then immediately emits a giveup-style
# need_approval, the two statements are contradictory — block the escape.
_ADD_TAP_NOTE_RE = re.compile(
    r"\b(add\s+to\s+cart|tap\s+add|ADD\b|\+\s|qty|quantity|increment|buy\s+now)",
    re.IGNORECASE,
)
# Intent regexes for the intent-vs-element check. They scan the tap's `note`
# field to infer what the model THINKS it's tapping, then we cross-check
# against the actual element under the coord.
#
# Search intent: the note mentions a search bar / box / input / field — but
# NOT an address/location word, because some apps overlap (e.g. "search for
# an address" is a legitimate location-picker action).
_SEARCH_INTENT_RE = re.compile(
    r"\b(search\s+(bar|box|input|field|icon)|search\s+for|search\s+the)\b",
    re.IGNORECASE,
)
_ADDRESS_INTENT_RE = re.compile(
    r"\b(address|location|deliver(?:y|ing)?\s|pin\s?code|change\s+pin)\b",
    re.IGNORECASE,
)
# ADD/cart intent: the model claims the tap is adding to cart, incrementing
# quantity, or starting checkout. The target MUST be an [ACTION] element.
_ADD_INTENT_RE = re.compile(
    r"\b(tap\s+add|press\s+add|add\s+to\s+cart|add\s+button|"
    r"\+\s?button|plus\s+button|increment|qty|quantity\s+(\+|plus)|"
    r"checkout|place\s+order|proceed\s+to|buy\s+now|pay\s+now)\b",
    re.IGNORECASE,
)
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
# If the model emits `need_approval` within this many steps of a loop_hint
# being injected, treat the approval as a giveup and hard-terminate. The
# user wants stuck-loops to fail fast, not pause for human rescue. Genuine
# cart-review approvals happen on clean flows where no loop was detected.
LOOP_TO_GIVEUP_WINDOW = 3
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
        self._last_loop_step: int = -1
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
        self._last_loop_step = -1
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
            _log.warning("task %d %s", task.user_id, task.failure_reason)
            await self._status(task, task.failure_reason)
        except OrchestratorError as e:
            task.state = TaskState.FAILED
            task.failure_reason = str(e)
            _log.warning("task %d failed: %s", task.user_id, e)
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
            # exc_info gives us the full traceback in stdout — previous
            # behaviour was a one-line "unexpected error" in Telegram only.
            _log.exception("task %d unexpected error", task.user_id)
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
            _log.info("step %d: begin", task.step_count)

            _log.info("step %d: screencap", task.step_count)
            screenshot = await self._adb.screencap()
            screenshot_b64 = base64.standard_b64encode(screenshot).decode("ascii")
            current_phash = _safe_phash(screenshot)

            # The UI tree is fetched once per iteration. We use it for two
            # purposes: (1) inject into the model prompt for accurate
            # coordinates, (2) validate the model's tap/swipe coords against
            # known element bounds to reject hallucinated coords. Both
            # branches below need access to `ui_elements` for the validation.
            ui_tree: str | None = None
            ui_elements: list[UiElement] = []

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
                _log.info("step %d: skill+ui_tree", task.step_count)
                skill_hint = await self._lookup_skill()
                ui_tree, ui_elements = await self._lookup_ui_tree()
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
                _log.info("step %d: vision call", task.step_count)
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
                _log.info(
                    "step %d: vision returned %s",
                    task.step_count, action.get("action"),
                )
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

            # Coord grounding: tap/swipe coords must map to some element in
            # the UI tree (with a 20px forgiveness margin). If they don't,
            # the model is hallucinating "tap on X" at coords that don't
            # actually point to X. Reject the action, hint the model, retry
            # next iteration.
            mismatch = self._coords_mismatch(action, ui_elements)
            if mismatch is not None:
                hint = (
                    f"REJECTED: {mismatch}. Pick coords from the UI elements "
                    "listing — never invent or estimate coordinates."
                )
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action, "COORD_REJECTED"
                )
                await self._step_status(
                    task, f"step {task.step_count}: coords rejected, retrying"
                )
                # Don't let dedup short-circuit the next iteration — we need a
                # fresh model call so it sees the rejection and adjusts.
                self._last_phash = None
                continue

            # Intent-vs-element check: the coord lands SOMEWHERE in the tree
            # (passed coord grounding) but on the WRONG kind of element for
            # what the note says. Catches the search-bar-vs-location-header
            # misclick and ADD-on-non-action hallucinations.
            intent_problem = self._intent_mismatch(action, ui_elements)
            if intent_problem is not None:
                hint = (
                    f"REJECTED: {intent_problem}. Pick a different element "
                    "whose type matches your stated intent."
                )
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "INTENT_MISMATCH_REJECTED",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: intent/element mismatch, retrying",
                )
                self._last_phash = None
                continue

            # Giveup rejection: model emits need_approval with a "no products
            # found"-style reason before having actually scrolled, OR while
            # contradicting a recent ADD tap. Don't pause for the user —
            # force a recovery attempt. This mirrors the coord-rejection
            # shape: append a hint to history, reset dedup, continue.
            # Legitimate need_approval (cart review, payment, no payment
            # method, etc.) doesn't trip this.
            giveup_reason = self._giveup_rejection(task, action)
            if giveup_reason is not None:
                hint = (
                    f"REJECTED need_approval: {giveup_reason}. The "
                    "orchestrator no longer trusts textual claims like "
                    "'after scrolling' — only an actual executed swipe in "
                    "history counts as proof of scrolling. Required next "
                    "action: emit a real swipe (e.g. swipe from (540,1600) "
                    "to (540,800)) to scroll the results. If you just "
                    "claimed to tap ADD on a product, navigate to the cart "
                    "icon instead of emitting need_approval — the ADD "
                    "either landed (go to cart) or missed (retry the tap)."
                )
                task.history.append({"action": action, "result": hint})
                self._audit.log_action(
                    task.user_id, task.description, action,
                    "REJECTED: giveup before scrolling",
                )
                await self._step_status(
                    task,
                    f"step {task.step_count}: need_approval rejected — "
                    "scroll first",
                )
                self._last_phash = None
                continue

            _log.info("step %d: hitl gate", task.step_count)
            await self._gate_with_hitl(task, action, screenshot, screenshot_b64, current_phash)

            # Security rule 4: audit BEFORE execute, never after.
            self._audit.log_action(
                task.user_id, task.description, action, "EXECUTING"
            )
            _log.info("step %d: execute %s", task.step_count, action.get("action"))

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

    async def _lookup_ui_tree(self) -> tuple[str | None, list[UiElement]]:
        """Fetch the on-screen accessibility tree.

        Returns a 2-tuple: (rendered prompt block, parsed element list).
        The rendered block goes into the model prompt; the parsed list is
        used by the coord-validation check to reject hallucinated taps.
        Both default to (None, []) on any failure so callers can degrade.
        """
        try:
            xml = await self._adb.dump_ui_xml()
        except Exception:
            return None, []
        if not xml:
            return None, []
        elements = ui_tree_parse(xml)
        rendered = ui_tree_to_prompt(xml) or None
        return rendered, elements

    @staticmethod
    def _giveup_rejection(task: Task, action: dict) -> str | None:
        """Return a rejection reason if `action` is a premature "no products
        found"-style bail-out, else None.

        Two independent rejection paths:

        1. Giveup-before-scroll: the reason matches a `_GIVEUP_PATTERNS` regex
           AND task.history shows no executed swipe. The only valid bypass is
           an actual swipe in history — earlier versions accepted a textual
           "after scrolling" suffix as proof, but the model learned to fake
           that suffix, so it's been removed.

        2. Giveup-contradicting-ADD: the most recent executed action is a tap
           whose note matches `_ADD_TAP_NOTE_RE` (e.g. "tap ADD on Maggi …"),
           and yet the model immediately emits a "no products found" giveup.
           Those two statements can't both be true — either the tap actually
           added a product (then the next step is cart-review, not giveup) or
           the tap missed (then the model should retry, not bail). Block.

        Genuine sensitive HITL (cart review, payment, no payment method, OTP)
        doesn't match any of these patterns, so it passes through untouched.
        """
        if action.get("action") != "need_approval":
            return None
        reason = str(action.get("reason", ""))
        if not reason:
            return None
        match = None
        for p in _GIVEUP_PATTERNS:
            m = p.search(reason)
            if m is not None:
                match = m
                break
        if match is None:
            return None

        # Contradiction check: the model's last executed action was an apparent
        # ADD tap. The giveup can't be honest — reject regardless of whether
        # scrolling happened. Search through history for the LAST executed
        # tap; skip entries that were rejected at the orchestrator level.
        last_tap = _last_executed_tap(task.history)
        if last_tap is not None:
            note = str(last_tap.get("note", ""))
            if _ADD_TAP_NOTE_RE.search(note):
                return (
                    f"reason matches giveup pattern '{match.group(0)}' but "
                    f"the previous tap was '{note}' — these are contradictory. "
                    "Either the ADD landed (then the cart is the next stop) or "
                    "it missed (then retry, don't bail)"
                )

        if _history_has_swipe(task.history):
            return None
        return (
            f"reason matches giveup pattern '{match.group(0)}' but no "
            "scroll has been attempted"
        )

    @staticmethod
    def _intent_mismatch(action: dict, elements: list[UiElement]) -> str | None:
        """Return a rejection reason when the tap's note disagrees with the
        element actually under the coord, else None.

        Two specific traps:

        1. Search intent on a [LOCATION] element. The model's note mentions
           "search bar / box / icon" but the coord lands on the delivery-
           address header. This is the run-2 misclick — y=200 on Blinkit's
           home screen.

        2. ADD/cart intent on a non-[ACTION] element. The note claims
           "tap ADD on …" / "+ button" / "checkout" but the target isn't
           an [ACTION] element. Often the model is hallucinating: it
           describes an action it wanted to take on a screen that doesn't
           actually have the ADD button visible.

        Passes through when no UI tree is available or the action isn't a
        tap. Returns None on a clean tap so the loop continues normally.
        """
        if action.get("action") != "tap":
            return None
        if not elements:
            return None
        try:
            x = int(action.get("x"))
            y = int(action.get("y"))
        except (TypeError, ValueError):
            return None
        note = str(action.get("note", ""))
        target = find_smallest_element_at(x, y, elements)
        if target is None:
            return None  # caught by _coords_mismatch

        # (1) Search intent landing on the LOCATION header.
        if (
            _SEARCH_INTENT_RE.search(note)
            and not _ADDRESS_INTENT_RE.search(note)
            and target.looks_like_location_header
        ):
            return (
                f"note '{note}' implies tapping the search bar, but the "
                f"element at ({x},{y}) is the [LOCATION] delivery/address "
                "header. The real search bar is a separate element below — "
                "look for class=EditText or an id containing 'search', "
                "usually with cy in 300–400 range"
            )

        # (2) ADD/cart intent landing on a non-[ACTION] element.
        if _ADD_INTENT_RE.search(note) and not target.is_action:
            target_kind = (
                "[CATEGORY?] tile"
                if (target.clickable and target.looks_like_category_text)
                else "non-action element"
            )
            return (
                f"note '{note}' implies tapping an ADD/+/checkout button, "
                f"but the element at ({x},{y}) is a {target_kind} (not "
                "marked [ACTION]). Pick the exact coords of an [ACTION]-"
                "prefixed element from the UI list. If no [ACTION] ADD "
                "button is visible for the target product, swipe to scroll "
                "and re-evaluate — don't tap on a category, image, or "
                "label hoping it acts as ADD"
            )

        return None

    @staticmethod
    def _coords_mismatch(action: dict, elements: list[UiElement]) -> str | None:
        """Return a human-readable reason if action's coords don't map to any
        element in the UI tree, or None when the action's coords are fine.

        Non-coord actions (wait / done / need_approval / type) pass through.
        Empty element list also passes through — without a tree to compare
        against we can't reject anything.
        """
        if not elements:
            return None
        action_type = action.get("action")
        if action_type == "tap":
            try:
                x = int(action.get("x"))
                y = int(action.get("y"))
            except (TypeError, ValueError):
                return None  # validator already caught malformed; don't double-fail
            if not is_coord_in_elements(x, y, elements):
                return f"tap at ({x}, {y}) doesn't fall on any UI element"
        elif action_type == "swipe":
            try:
                x1 = int(action.get("x1"))
                y1 = int(action.get("y1"))
            except (TypeError, ValueError):
                return None
            if not is_coord_in_elements(x1, y1, elements):
                return (
                    f"swipe start ({x1}, {y1}) doesn't fall on any UI element"
                )
        return None

    def _loop_hint(self, task: Task) -> str:
        """Detect repeated-action and cycle patterns; return a hint or ''.

        Three independent detectors:
          1. A-A           : last two state-changing actions identical
          2. A-B-A-B       : last four form a two-step alternation
          3. Same-action seen 3+ times anywhere in history (catches 3-step
             cycles like A-B-C-A-B-C and any other repetition shape)
        Any detector firing increments self._loops_detected; the caller
        hard-aborts when that crosses MAX_LOOPS_BEFORE_ABORT.
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
        # The most-recent action has been emitted at least 3 times somewhere
        # in this task's history — catches longer cycles + general repetition.
        latest = recent_state_changing[-1]
        repeat_count = sum(
            1 for a in recent_state_changing if _actions_equivalent(a, latest)
        )
        is_triplet = repeat_count >= 3

        if not (is_aa or is_abab or is_triplet):
            return ""

        self._loops_detected += 1
        self._last_loop_step = task.step_count
        if is_aa:
            shape = "identical"
        elif is_abab:
            shape = "alternating A↔B cycle"
        else:
            shape = f"repeated {repeat_count} times"
        remaining = max(0, MAX_LOOPS_BEFORE_ABORT - self._loops_detected)
        return (
            f"NOTE: Your last action has the pattern '{shape}' and isn't "
            "advancing the screen. STOP this loop. Required next step: pick a "
            "DIFFERENT element from the UI list (prefer one marked [ACTION] "
            "over a card body), or swipe to scroll, or press back to dismiss "
            "an overlay. DO NOT emit need_approval — that's reserved for "
            "payment/OTP/delete/permission and the cart-review handoff only. "
            f"If you loop {remaining} more time(s) the task will be hard-"
            "terminated."
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

        # If the model emits need_approval shortly after a loop hint was
        # injected, it's using approval as a give-up escape hatch rather
        # than a genuine sensitive-action gate. Hard-terminate instead of
        # pausing for human rescue.  Genuine cart-review approvals happen
        # on clean flows where no loop was recently detected.
        if (
            self._last_loop_step >= 0
            and (task.step_count - self._last_loop_step) <= LOOP_TO_GIVEUP_WINDOW
            and action.get("action") == "need_approval"
        ):
            self._audit.log_action(
                task.user_id, task.description, action,
                "BLOCKED: need_approval used as loop escape",
            )
            raise OrchestratorError(
                "model emitted need_approval to escape a detected loop; "
                "aborting instead of pausing for approval"
            )

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
                _log.warning(
                    "adb action %s failed (attempt %d): %s",
                    action.get("action"), attempt + 1, e,
                )
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


def _history_has_swipe(history: list[dict]) -> bool:
    """True if the task has executed at least one swipe action.

    Used by the giveup-rejection check to decide whether the model has
    earned the right to emit 'no products found'. A rejected-coord or
    rejected-giveup entry isn't a swipe; we look only at the action's
    `action` field.
    """
    for entry in history:
        action = entry.get("action") if isinstance(entry, dict) else None
        if isinstance(action, dict) and action.get("action") == "swipe":
            # Skip swipes that never actually executed (e.g. rejected by
            # coord grounding). A rejected entry has a result starting
            # with "REJECTED".
            result = str(entry.get("result", ""))
            if not result.startswith("REJECTED"):
                return True
    return False


def _last_executed_tap(history: list[dict]) -> dict | None:
    """Return the most recent successfully-executed tap action, or None.

    Used by the giveup-rejection's contradiction check. We skip entries
    whose result starts with "REJECTED" or "ERROR:" — those represent
    actions that didn't actually run on the device.
    """
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue
        action = entry.get("action")
        if not isinstance(action, dict) or action.get("action") != "tap":
            continue
        result = str(entry.get("result", ""))
        if result.startswith("REJECTED") or result.startswith("ERROR:"):
            continue
        return action
    return None


def _safe_phash(image_bytes: bytes) -> str | None:
    try:
        return compute_phash(image_bytes)
    except ValueError:
        return None
