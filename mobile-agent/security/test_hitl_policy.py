"""Per-user policy tests for HitlGate.

The original HitlGate tests cover the default behavior (confirm_sensitive).
This file covers the new always_approve / read_only paths.
"""
from __future__ import annotations

import pytest

from security.hitl_gate import HitlGate, ReadOnlyViolation


class TestAlwaysApprove:
    async def test_skips_sensitive_keyword(self) -> None:
        gate = HitlGate()
        # Even a payment-flavored action passes when policy is always_approve.
        assert not await gate.requires_approval(
            {"action": "tap", "reason": "payment confirm"}, "", "always_approve"
        )

    async def test_skips_need_approval_action(self) -> None:
        gate = HitlGate()
        assert not await gate.requires_approval(
            {"action": "need_approval", "reason": "OTP"}, "", "always_approve"
        )


class TestReadOnly:
    @pytest.mark.parametrize("action", ["tap", "type", "swipe"])
    async def test_blocks_state_changing(self, action: str) -> None:
        gate = HitlGate()
        payload = {"action": action, "x": 0, "y": 0} if action != "type" else {
            "action": "type",
            "text": "hi",
        }
        with pytest.raises(ReadOnlyViolation):
            await gate.requires_approval(payload, "", "read_only")

    async def test_allows_wait(self) -> None:
        gate = HitlGate()
        # wait is non-mutating; should not raise. With no sensitive keywords
        # in reason, also should not require approval.
        assert not await gate.requires_approval(
            {"action": "wait", "reason": "loading"}, "", "read_only"
        )

    async def test_allows_done(self) -> None:
        gate = HitlGate()
        assert not await gate.requires_approval(
            {"action": "done", "summary": "all good"}, "", "read_only"
        )

    async def test_need_approval_still_prompts(self) -> None:
        gate = HitlGate()
        assert await gate.requires_approval(
            {"action": "need_approval", "reason": "x"}, "", "read_only"
        )
