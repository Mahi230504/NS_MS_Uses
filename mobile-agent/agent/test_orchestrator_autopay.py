"""Orchestrator tests for the unattended-scheduled-run payment policy (#5)."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.orchestrator import Orchestrator
from agent.state_machine import Task, TaskState
from security.audit_logger import AuditLogger
from security.hitl_gate import HitlGate

from agent.test_orchestrator import _FakeAdb, _ScriptedVision, _usage


def _orch(vision, tmp_path: Path) -> Orchestrator:
    return Orchestrator(
        _FakeAdb(), HitlGate(), AuditLogger(tmp_path), vision,
        session_timeout_seconds=10,
    )


class TestAutoPay:
    async def test_auto_grants_cart_review(self, tmp_path: Path) -> None:
        vision = _ScriptedVision(
            [
                ({"action": "need_approval",
                  "reason": "Cart review: 1x milk, total ₹50. Approving authorizes payment."},
                 _usage()),
                ({"action": "done", "summary": "ordered"}, _usage()),
            ]
        )
        task = Task(user_id=1, description="order milk")
        task.auto_approve_payment = True
        # approval_timeout guards against a hang if the auto-grant regressed.
        await _orch(vision, tmp_path).run_task(task, approval_timeout=2.0)
        assert task.state is TaskState.DONE
        assert task.payment_pre_approved is True  # latched by the auto-grant

    async def test_does_not_auto_grant_otp(self, tmp_path: Path) -> None:
        vision = _ScriptedVision(
            [({"action": "need_approval",
               "reason": "Enter the OTP / verification code sent to your phone"},
              _usage())]
        )
        task = Task(user_id=1, description="order milk")
        task.auto_approve_payment = True
        # OTP is excluded from auto-pay -> waits -> times out -> fails safely.
        await _orch(vision, tmp_path).run_task(task, approval_timeout=0.05)
        assert task.state is TaskState.FAILED


class TestStopAtCart:
    async def test_default_scheduled_run_times_out(self, tmp_path: Path) -> None:
        # No auto_approve_payment: a scheduled cart-review with nobody to
        # approve hits the bounded wait and fails — nothing paid.
        vision = _ScriptedVision(
            [({"action": "need_approval",
               "reason": "Cart review: 1x milk. Approve to pay."}, _usage())]
        )
        task = Task(user_id=1, description="order milk")
        await _orch(vision, tmp_path).run_task(task, approval_timeout=0.05)
        assert task.state is TaskState.FAILED

    async def test_no_timeout_is_interactive_default(self, tmp_path: Path) -> None:
        # With no approval_timeout and an immediate grant, the run completes.
        vision = _ScriptedVision(
            [
                ({"action": "need_approval", "reason": "Cart review: milk"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        hitl = HitlGate()
        orch = Orchestrator(
            _FakeAdb(), hitl, AuditLogger(tmp_path), vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="order milk")

        # Grant approval as soon as the gate is pending.
        import asyncio

        async def _grant_when_pending():
            for _ in range(200):
                if hitl.has_pending(1):
                    hitl.grant(1)
                    return
                await asyncio.sleep(0.01)

        granter = asyncio.create_task(_grant_when_pending())
        await orch.run_task(task)  # approval_timeout=None (interactive)
        await granter
        assert task.state is TaskState.DONE
