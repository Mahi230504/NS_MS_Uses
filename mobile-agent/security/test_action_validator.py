"""Unit tests for the action validator."""
from __future__ import annotations

from security.action_validator import ALLOWED_ACTIONS, validate


class TestValidate:
    def test_tap_is_allowed(self) -> None:
        ok, reason = validate({"action": "tap", "x": 100, "y": 200})
        assert ok, reason

    def test_done_is_allowed(self) -> None:
        ok, _ = validate({"action": "done", "summary": "all good"})
        assert ok

    def test_need_approval_is_allowed(self) -> None:
        ok, _ = validate({"action": "need_approval", "reason": "OTP entry"})
        assert ok

    def test_unknown_action_is_rejected(self) -> None:
        ok, reason = validate({"action": "screenshot"})
        assert not ok
        assert "not in the allowed set" in reason

    def test_missing_action_field(self) -> None:
        ok, reason = validate({"x": 1, "y": 2})
        assert not ok
        assert "missing 'action'" in reason

    def test_non_dict_payload(self) -> None:
        ok, reason = validate("tap")  # type: ignore[arg-type]
        assert not ok
        assert "must be a dict" in reason

    # Regression: legitimate text input "call mom" must not be blocked.
    def test_type_call_mom_is_not_blocked(self) -> None:
        ok, reason = validate({"action": "type", "text": "call mom"})
        assert ok, reason

    def test_done_summary_with_call_is_not_blocked(self) -> None:
        ok, _ = validate(
            {"action": "done", "summary": "added a call to action button"}
        )
        assert ok

    def test_need_approval_reason_with_uninstall_is_not_blocked(self) -> None:
        # need_approval is the EXIT path for sensitive operations — its reason
        # field will frequently mention dangerous words. Must remain allowed.
        ok, _ = validate(
            {"action": "need_approval", "reason": "user is about to uninstall app"}
        )
        assert ok

    # Defense in depth: structural fields are still scanned.
    def test_structural_field_with_blocked_term(self) -> None:
        ok, reason = validate({"action": "tap", "x": 1, "y": 2, "intent": "call"})
        assert not ok
        assert "blocked term" in reason

    def test_allowed_action_set_is_locked_down(self) -> None:
        # The allowed set is small and intentional; this test fails if anyone
        # widens it without thinking.
        assert ALLOWED_ACTIONS == frozenset(
            {"tap", "type", "swipe", "done", "need_approval", "wait"}
        )
