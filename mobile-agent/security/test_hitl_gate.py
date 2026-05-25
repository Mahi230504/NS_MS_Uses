"""Unit tests for HITL gate detection + grant/deny flow."""
from __future__ import annotations

import asyncio

import pytest

from security.hitl_gate import HitlGate


class TestRequiresApproval:
    async def test_need_approval_action_always_triggers(self) -> None:
        gate = HitlGate()
        assert await gate.requires_approval({"action": "need_approval", "reason": "x"}, "")

    async def test_plain_tap_does_not_trigger(self) -> None:
        gate = HitlGate()
        assert not await gate.requires_approval({"action": "tap", "x": 1, "y": 2}, "")

    @pytest.mark.parametrize(
        "field,value",
        [
            ("reason", "approaching payment confirmation"),
            ("summary", "completed checkout"),
            ("text", "entering OTP code"),
            ("reason", "system permission dialog visible"),
            ("reason", "about to uninstall app"),
            ("reason", "factory reset prompt"),
        ],
    )
    async def test_sensitive_keywords_trigger(self, field: str, value: str) -> None:
        gate = HitlGate()
        assert await gate.requires_approval({"action": "tap", field: value}, "")

    async def test_sensitive_keyword_in_note_triggers(self) -> None:
        # `note` is the model's plain-English narration. If it mentions
        # "checkout" / "payment" / etc., that's a dangerous intention and
        # the gate should fire even if no `reason` field is present.
        gate = HitlGate()
        assert await gate.requires_approval(
            {"action": "tap", "x": 1, "y": 2, "note": "tap Place Order to pay now"},
            "",
        )

    async def test_benign_text_does_not_trigger(self) -> None:
        gate = HitlGate()
        assert not await gate.requires_approval(
            {"action": "type", "text": "hello world"}, ""
        )


class TestGrantDenyFlow:
    async def test_grant_unblocks_wait(self) -> None:
        gate = HitlGate()
        waiter = asyncio.create_task(gate.wait_for_approval(42, timeout=1.0))
        await asyncio.sleep(0.01)  # let the waiter register its event
        gate.grant(42)
        assert await waiter is True

    async def test_deny_unblocks_wait(self) -> None:
        gate = HitlGate()
        waiter = asyncio.create_task(gate.wait_for_approval(42, timeout=1.0))
        await asyncio.sleep(0.01)
        gate.deny(42)
        assert await waiter is False

    async def test_timeout_returns_false(self) -> None:
        gate = HitlGate()
        assert await gate.wait_for_approval(42, timeout=0.05) is False

    async def test_has_pending_lifecycle(self) -> None:
        gate = HitlGate()
        assert not gate.has_pending(42)
        waiter = asyncio.create_task(gate.wait_for_approval(42, timeout=1.0))
        await asyncio.sleep(0.01)
        assert gate.has_pending(42)
        gate.grant(42)
        await waiter
        assert not gate.has_pending(42)

    async def test_grant_for_other_user_does_not_unblock(self) -> None:
        gate = HitlGate()
        waiter = asyncio.create_task(gate.wait_for_approval(42, timeout=0.1))
        await asyncio.sleep(0.01)
        gate.grant(99)  # different user
        # Should still time out and return False.
        assert await waiter is False
