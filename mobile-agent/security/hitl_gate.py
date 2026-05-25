"""Detects irreversible actions, pauses loop, awaits Telegram approval."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Literal, Protocol


SENSITIVE_KEYWORDS: tuple[str, ...] = (
    # Payment
    "payment", "pay now", "checkout", "card number", "cvv", "upi", "billing",
    # OTP / auth
    "otp", "verification code", "verify your", "2fa", "two-factor",
    # Destructive
    "delete", "remove account", "uninstall", "clear data", "factory reset",
    # Permissions
    "permission", "allow access", "grant access", "grant permission",
)

# Action types that mutate device state. `read_only` users can't do any of these.
_STATE_CHANGING_ACTIONS: frozenset[str] = frozenset({"tap", "type", "swipe"})


# Policy string literals — kept in sync with bot.users.UserPolicy. We don't
# import UserPolicy here to keep security/ free of bot/ imports.
Policy = Literal["always_approve", "confirm_sensitive", "read_only"]

# Question used by the vision-augmented classifier. Asked verbatim per call.
_SENSITIVITY_QUESTION = (
    "Does this screen show a payment confirmation, OTP/verification code entry, "
    "an account deletion/uninstall prompt, a system permission dialog, or any "
    "other irreversible action that needs human approval?"
)
# Bound the cache so a long-running session can't grow it without limit.
_CLASSIFY_CACHE_MAX = 256


class _Classifier(Protocol):
    async def classify_yes_no(self, screenshot_bytes: bytes, question: str) -> bool: ...


class ReadOnlyViolation(RuntimeError):
    """Raised when a read_only user's loop tries to mutate device state.

    Distinct from approval-denied so the orchestrator can log it accurately.
    """


class HitlGate:
    """Human-in-the-loop gate.

    Two responsibilities:
      1. `requires_approval` — pure detection based on the action, screenshot,
         and the user's policy.
      2. Blocking wait — orchestrator awaits user decision keyed by user_id.
    """

    def __init__(self) -> None:
        self._pending: dict[int, asyncio.Event] = {}
        self._results: dict[int, bool] = {}
        # Phash → classifier verdict. LRU-ish: oldest entries evicted at cap.
        self._classify_cache: OrderedDict[str, bool] = OrderedDict()

    async def requires_approval(
        self,
        action_json: dict,
        screenshot_base64: str,
        user_policy: Policy = "confirm_sensitive",
    ) -> bool:
        action_type = action_json.get("action", "")

        if user_policy == "read_only":
            if action_type in _STATE_CHANGING_ACTIONS:
                # Caller must treat this as a hard refusal, not a prompt.
                raise ReadOnlyViolation(
                    f"read_only user cannot perform '{action_type}'"
                )

        if user_policy == "always_approve":
            return False

        if action_type == "need_approval":
            return True

        # The keyword scan only makes sense for actions that actually do
        # something. `wait` and `done` carry the model's narration in their
        # `reason`/`summary`, which routinely mentions the broader plan
        # (e.g. "waiting before checkout") — that's not a payment screen,
        # it's the model thinking out loud. Gating waits creates false
        # positives that derail every run.
        if action_type not in _STATE_CHANGING_ACTIONS:
            return False

        # `note` is the model's human-readable description of what it's doing
        # ("tapping Place Order button"). Scanning it lets the gate catch
        # dangerous intentions stated in plain English even when the action
        # type alone wouldn't.
        haystack = " ".join(
            str(action_json.get(k, "")) for k in ("reason", "summary", "text", "note")
        ).lower()
        return any(kw in haystack for kw in SENSITIVE_KEYWORDS)

    async def wait_for_approval(
        self, user_id: int, timeout: float | None = None
    ) -> bool:
        """Block until grant/deny is called for user_id, or until timeout.

        Returns True if granted, False if denied or timed out.
        """
        event = asyncio.Event()
        self._pending[user_id] = event
        self._results.pop(user_id, None)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return self._results.get(user_id, False)
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending.pop(user_id, None)
            self._results.pop(user_id, None)

    def grant(self, user_id: int) -> None:
        self._results[user_id] = True
        ev = self._pending.get(user_id)
        if ev is not None:
            ev.set()

    def deny(self, user_id: int) -> None:
        self._results[user_id] = False
        ev = self._pending.get(user_id)
        if ev is not None:
            ev.set()

    def has_pending(self, user_id: int) -> bool:
        return user_id in self._pending

    async def classify_sensitivity(
        self,
        screenshot_bytes: bytes,
        screenshot_phash: str,
        provider: _Classifier,
    ) -> bool:
        """Ask the vision provider whether the current screen needs HITL.

        Cached by phash so revisiting a static screen mid-task doesn't burn
        a request. Failures default to False so a flaky classify call doesn't
        unblock a sensitive screen — the keyword check is still authoritative
        and the orchestrator OR-gates the two results.
        """
        cached = self._classify_cache.get(screenshot_phash)
        if cached is not None:
            # Refresh LRU position.
            self._classify_cache.move_to_end(screenshot_phash)
            return cached
        try:
            verdict = await provider.classify_yes_no(
                screenshot_bytes, _SENSITIVITY_QUESTION
            )
        except Exception:
            return False
        self._classify_cache[screenshot_phash] = verdict
        self._classify_cache.move_to_end(screenshot_phash)
        if len(self._classify_cache) > _CLASSIFY_CACHE_MAX:
            self._classify_cache.popitem(last=False)
        return verdict
