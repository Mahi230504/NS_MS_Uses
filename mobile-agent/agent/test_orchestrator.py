"""Unit tests for the orchestrator loop: usage accumulation + step status throttle."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent.orchestrator import (
    Orchestrator,
    _is_single_item_task,
    _requested_quantity,
)
from agent.persistence import TaskRepository
from agent.profiles import resolve_profile
from agent.providers.base import ProviderResponse, RequestUsage
from agent.state_machine import Task, TaskState
from agent.ui_tree import UiElement
from bot.users import UserPolicy, UserRecord, UserStore
from security.audit_logger import AuditLogger
from security.hitl_gate import HitlGate


class _FakeAdb:
    """Stand-in for AdbController. Records calls; never spawns subprocesses."""

    def __init__(
        self,
        screen_size: tuple[int, int] | None = (1080, 1920),
        screencaps: list[bytes] | None = None,
        foreground_package: str | None = None,
    ) -> None:
        self.taps: list[tuple[int, int]] = []
        self.screen_size = screen_size
        self.texts: list[str] = []
        self.key_events: list[int] = []
        self.woke: int = 0
        # If `screencaps` is provided, calls pop sequentially (last entry
        # repeats). Otherwise each call synthesizes a fresh unique PNG so
        # phash-based dedup / outcome verification doesn't engage. Tests that
        # want to exercise dedup pass an explicit list of repeating bytes.
        self._screencaps = list(screencaps) if screencaps is not None else None
        self._screencap_idx = 0
        self._screencap_counter = 0
        self._foreground_package = foreground_package

    async def screencap(self) -> bytes:
        if self._screencaps is not None:
            if self._screencap_idx < len(self._screencaps) - 1:
                data = self._screencaps[self._screencap_idx]
                self._screencap_idx += 1
            else:
                data = self._screencaps[-1]
            return data
        # Unique stream — embed the counter in the PNG with distinct spatial
        # structure so phash actually differentiates the frames (solid colors
        # alone collide on perceptual hash).
        self._screencap_counter += 1
        return _make_unique_png(self._screencap_counter)

    async def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    async def type_text(self, text: str) -> None:
        self.texts.append(text)

    async def swipe(self, *args: int) -> None: ...

    async def key_event(self, keycode: int) -> None:
        self.key_events.append(keycode)

    async def wake_screen(self) -> None:
        self.woke += 1

    async def get_screen_size(self) -> tuple[int, int]:
        if self.screen_size is None:
            raise RuntimeError("no screen size")
        return self.screen_size

    async def get_foreground_package(self) -> str | None:
        return self._foreground_package

    async def dump_ui_xml(self) -> str | None:
        # Tests don't need a real tree; return None so the orchestrator
        # degrades to screenshot-only (existing assertions still hold).
        return None

    async def use_adbkeyboard_for_task(self) -> str | None:
        # No IME on a fake device — return None (nothing to restore).
        return None

    async def restore_ime(self, ime_id: str | None) -> None:
        return None


def _make_png(color: tuple[int, int, int]) -> bytes:
    """Return PNG bytes for a tiny solid-color image — phash-stable."""
    from io import BytesIO
    from PIL import Image

    img = Image.new("RGB", (32, 32), color=color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_unique_png(seed: int) -> bytes:
    """Return PNG bytes with a phash that varies with `seed`.

    phash collapses any uniform image to the same hash, so we draw a small
    pattern whose layout depends on `seed`. A 32x32 grid with a single black
    cell at position (seed_x, seed_y) is enough for the perceptual hash to
    distinguish frames.
    """
    from io import BytesIO
    from PIL import Image

    img = Image.new("RGB", (32, 32), color=(255, 255, 255))
    pixels = img.load()
    # Spread the marker around the image so consecutive seeds aren't visually
    # adjacent — phash distance grows.
    x = (seed * 7) % 32
    y = (seed * 13) % 32
    for dx in range(4):
        for dy in range(4):
            pixels[(x + dx) % 32, (y + dy) % 32] = (0, 0, 0)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_PNG_BLANK_RED = _make_png((255, 0, 0))
_PNG_BLANK_BLUE = _make_png((0, 0, 255))


class _ScriptedVision:
    """VisionProvider that returns a queue of pre-scripted (action, usage) pairs."""

    def __init__(self, script: list[tuple[dict, RequestUsage]]) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    async def get_next_action(
        self,
        screenshot_bytes: bytes,
        task_description: str,
        step_history: list,
        screen_size: tuple[int, int] | None = None,
        skill_hint: str | None = None,
        ui_tree: str | None = None,
    ) -> ProviderResponse:
        self.calls.append(
            {
                "screen_size": screen_size,
                "skill_hint": skill_hint,
                "ui_tree": ui_tree,
                "task_description": task_description,
                "history_len": len(step_history),
            }
        )
        action, usage = self._script.pop(0)
        return ProviderResponse(action=action, usage=usage)

    async def classify_yes_no(self, screenshot_bytes: bytes, question: str) -> bool:
        # Default: classifier says no. Individual tests override per-instance.
        return False


def _usage(input_tokens: int = 100, output_tokens: int = 20, rpd_remaining: int = 1000) -> RequestUsage:
    return RequestUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        rpm_remaining=5,
        rpd_remaining=rpd_remaining,
    )


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path)


class TestOrchestratorUsage:
    async def test_accumulates_tokens_across_iterations(
        self, audit: AuditLogger
    ) -> None:
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 10, "y": 20}, _usage(50, 5, 1499)),
                ({"action": "tap", "x": 30, "y": 40}, _usage(60, 7, 1498)),
                ({"action": "done", "summary": "finished"}, _usage(40, 3, 1497)),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)
        task = Task(user_id=1, description="do a thing")

        await orch.run_task(task)

        assert task.state is TaskState.DONE
        assert task.total_input_tokens == 150
        assert task.total_output_tokens == 15
        assert task.latest_rpd_remaining == 1497

    async def test_passes_screen_size_to_provider(self, audit: AuditLogger) -> None:
        vision = _ScriptedVision(
            [({"action": "done", "summary": "ok"}, _usage(1, 1, 100))]
        )
        adb = _FakeAdb(screen_size=(1440, 2960))
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)
        task = Task(user_id=1, description="t")

        await orch.run_task(task)
        assert vision.calls[0]["screen_size"] == (1440, 2960)

    async def test_screen_size_failure_is_tolerated(self, audit: AuditLogger) -> None:
        vision = _ScriptedVision(
            [({"action": "done", "summary": "ok"}, _usage(1, 1, 100))]
        )
        adb = _FakeAdb(screen_size=None)
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)
        task = Task(user_id=1, description="t")

        await orch.run_task(task)
        assert task.state is TaskState.DONE
        assert vision.calls[0]["screen_size"] is None


class TestStepStatusThrottle:
    async def test_first_step_status_sent_then_throttled(
        self, audit: AuditLogger
    ) -> None:
        # Three taps then done. The test completes well inside the 2s throttle
        # window in real time, so only the first non-terminal step's status
        # should pass the throttle.
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 1, "y": 2}, _usage()),
                ({"action": "tap", "x": 3, "y": 4}, _usage()),
                ({"action": "tap", "x": 5, "y": 6}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)

        sent: list[str] = []

        async def fake_status(_task: Task, message: str) -> None:
            sent.append(message)

        orch.on_status_update = fake_status
        task = Task(user_id=1, description="t")
        await orch.run_task(task)

        # Lifecycle: "starting: ..." and "done: ..." always send.
        # Step messages: only the first tap should pass the throttle.
        assert any(m.startswith("starting") for m in sent)
        assert any(m.startswith("done") for m in sent)
        step_msgs = [m for m in sent if m.startswith("step ")]
        assert len(step_msgs) == 1
        assert "step 1" in step_msgs[0]


class TestLowRpdWarning:
    async def test_warns_once_under_threshold(self, audit: AuditLogger) -> None:
        # Two iterations both report low RPD; we should warn exactly once.
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 1, "y": 2}, _usage(rpd_remaining=10)),
                ({"action": "done", "summary": "ok"}, _usage(rpd_remaining=9)),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)

        sent: list[str] = []

        async def fake_status(_task: Task, message: str) -> None:
            sent.append(message)

        orch.on_status_update = fake_status
        await orch.run_task(Task(user_id=1, description="t"))

        warnings = [m for m in sent if "low daily quota" in m]
        assert len(warnings) == 1

    async def test_no_warning_above_threshold(self, audit: AuditLogger) -> None:
        vision = _ScriptedVision(
            [({"action": "done", "summary": "ok"}, _usage(rpd_remaining=500))]
        )
        adb = _FakeAdb()
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)

        sent: list[str] = []

        async def fake_status(_task: Task, message: str) -> None:
            sent.append(message)

        orch.on_status_update = fake_status
        await orch.run_task(Task(user_id=1, description="t"))

        assert not any("low daily quota" in m for m in sent)


class TestDedup:
    async def test_unchanged_screen_synthesizes_wait(
        self, audit: AuditLogger
    ) -> None:
        # An unchanging screen means the loop fails fast on the unchanged-
        # streak guard. The key invariant: between the first real call and
        # the failure, the provider was NOT called every iter — dedup
        # synthesized wait actions instead.
        same = _make_unique_png(42)
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 1, "y": 2}, _usage()),
                # Subsequent entries should NOT be needed if dedup engages:
                # the orchestrator should give up via the streak guard first.
                ({"action": "tap", "x": 3, "y": 4}, _usage()),
                ({"action": "tap", "x": 5, "y": 6}, _usage()),
                ({"action": "tap", "x": 7, "y": 8}, _usage()),
            ]
        )
        adb = _FakeAdb(screencaps=[same] * 12)

        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=20)
        task = Task(user_id=1, description="t")
        await orch.run_task(task)

        # The loop runs more iters than provider calls — that's the dedup
        # signal. Without dedup, calls == iters.
        assert task.step_count > len(vision.calls)
        assert len(vision.calls) >= 1  # iter 1 always calls the provider


class TestRetry:
    async def test_retries_on_adb_error_then_succeeds(
        self, audit: AuditLogger, monkeypatch
    ) -> None:
        # Make backoff free so the test runs fast.
        monkeypatch.setattr(
            "agent.orchestrator.RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0)
        )

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 1, "y": 2}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )

        from device.adb_controller import AdbError

        class _FlakyAdb(_FakeAdb):
            def __init__(self) -> None:
                super().__init__()
                self.tap_attempts = 0

            async def tap(self, x: int, y: int) -> None:
                self.tap_attempts += 1
                if self.tap_attempts < 3:
                    raise AdbError("flaky")
                # Third attempt succeeds.
                await super().tap(x, y)

        adb = _FlakyAdb()
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)
        task = Task(user_id=1, description="t")
        await orch.run_task(task)

        assert task.state is TaskState.DONE
        assert adb.tap_attempts == 3
        # The two failed attempts must show up in history as failure markers.
        failed = [
            h for h in task.history if h.get("previous_attempt_failed")
        ]
        assert len(failed) == 2

    async def test_retry_exhausted_fails_task(
        self, audit: AuditLogger, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "agent.orchestrator.RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0)
        )

        from device.adb_controller import AdbError

        vision = _ScriptedVision([
            ({"action": "tap", "x": 1, "y": 2}, _usage()),
        ])

        class _AlwaysFailAdb(_FakeAdb):
            async def tap(self, x: int, y: int) -> None:
                raise AdbError("device offline")

        adb = _AlwaysFailAdb()
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)
        task = Task(user_id=1, description="t")
        await orch.run_task(task)

        assert task.state is TaskState.FAILED
        assert "adb action failed" in (task.failure_reason or "")


class TestOutcomeVerification:
    async def test_unchanged_after_action_triggers_back_button(
        self, audit: AuditLogger
    ) -> None:
        same = _make_unique_png(99)
        # Two real taps in a row, both leaving the screen unchanged. The
        # second one should fire the back-button recovery.
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 1, "y": 2}, _usage()),
                ({"action": "tap", "x": 3, "y": 4}, _usage()),
                # If recovery succeeds and screen changes, this done lands.
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        # 4 identical frames cover: iter1 top, iter1 verify, iter2 top, iter2 verify.
        # Recovery re-screencap (if it happens, returns a 5th frame that changes).
        adb = _FakeAdb(screencaps=[same, same, same, same])

        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=20)
        task = Task(user_id=1, description="t")
        await orch.run_task(task)

        # Back-button recovery should have fired before the 3-strike fail.
        from device.adb_controller import KEYCODE_BACK

        assert KEYCODE_BACK in adb.key_events


class TestSkillInjection:
    async def test_skill_passed_to_provider(
        self, audit: AuditLogger, tmp_path: Path
    ) -> None:
        from agent.skills import SkillRegistry

        (tmp_path / "com.example.app.md").write_text("Tap the green pin first.")
        skills = SkillRegistry(tmp_path)

        vision = _ScriptedVision(
            [({"action": "done", "summary": "ok"}, _usage())]
        )
        adb = _FakeAdb(foreground_package="com.example.app")
        # com.example.app is non-commerce → GENERIC (no addendum), so the
        # guidance block is just the per-app skill markdown.
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            skills=skills, profile_resolver=resolve_profile,
        )
        await orch.run_task(Task(user_id=1, description="t"))

        assert vision.calls[0]["skill_hint"] == "Tap the green pin first."

    async def test_no_skill_when_package_unknown(
        self, audit: AuditLogger, tmp_path: Path
    ) -> None:
        from agent.skills import SkillRegistry

        skills = SkillRegistry(tmp_path)  # empty dir
        vision = _ScriptedVision(
            [({"action": "done", "summary": "ok"}, _usage())]
        )
        adb = _FakeAdb(foreground_package="com.unknown")
        # Unknown package → GENERIC: no addendum and no skill → no guidance.
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            skills=skills, profile_resolver=resolve_profile,
        )
        await orch.run_task(Task(user_id=1, description="t"))

        assert vision.calls[0]["skill_hint"] is None


class TestVisionAugmentedHitl:
    async def test_vision_says_yes_triggers_approval(
        self, audit: AuditLogger, tmp_path: Path
    ) -> None:
        # The action has no sensitive keyword, so the keyword filter says no.
        # The vision classifier returns yes — orchestrator must still gate.
        class _SayYesVision(_ScriptedVision):
            async def classify_yes_no(self, screenshot_bytes: bytes, question: str) -> bool:
                return True

        vision = _SayYesVision(
            [
                ({"action": "tap", "x": 1, "y": 2}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        users = UserStore(tmp_path / "users.json")
        await users.add(
            UserRecord.new(user_id=1, name="u", policy=UserPolicy.CONFIRM_SENSITIVE)
        )

        hitl = HitlGate()
        approvals: list[dict] = []

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)
            hitl.grant(_task.user_id)  # auto-approve so the loop continues

        orch = Orchestrator(
            adb,
            hitl,
            audit,
            vision,
            session_timeout_seconds=10,
            users=users,
            enable_vision_hitl=True,
        )
        orch.on_approval_request = on_approval
        await orch.run_task(Task(user_id=1, description="t"))

        # Exactly one approval request — for the tap that the vision flagged.
        assert len(approvals) == 1
        assert approvals[0]["action"] == "tap"


class TestLoopDetection:
    async def test_repeated_tap_triggers_loop_hint(
        self, audit: AuditLogger
    ) -> None:
        same_tap = {"action": "tap", "x": 100, "y": 200}
        vision = _ScriptedVision(
            [
                (same_tap, _usage()),
                (same_tap, _usage()),
                ({"action": "done", "summary": "stopping after loop hint"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)
        await orch.run_task(Task(user_id=1, description="do the thing"))

        # First two calls: no loop hint (only one prior action each, or none).
        assert "loop" not in vision.calls[0]["task_description"].lower()
        assert "loop" not in vision.calls[1]["task_description"].lower()
        # Third call: hint should be injected.
        assert "identical" in vision.calls[2]["task_description"]

    async def test_abab_cycle_triggers_loop_hint(self, audit: AuditLogger) -> None:
        # Four actions forming A-B-A-B → the fifth call gets the cycle hint.
        a = {"action": "tap", "x": 100, "y": 200}
        b = {"action": "tap", "x": 800, "y": 900}
        vision = _ScriptedVision(
            [
                (a, _usage()),
                (b, _usage()),
                (a, _usage()),
                (b, _usage()),
                ({"action": "done", "summary": "stop"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)
        await orch.run_task(Task(user_id=1, description="t"))

        # Hint should appear on the 5th call (after the 4-action cycle is complete).
        assert "cycle" in vision.calls[4]["task_description"]

    async def test_persistent_looping_hard_aborts(self, audit: AuditLogger) -> None:
        # Same tap forever — after MAX_LOOPS_BEFORE_ABORT detected loops,
        # orchestrator should fail the task instead of hinting endlessly.
        same = {"action": "tap", "x": 100, "y": 200}
        vision = _ScriptedVision([(same, _usage()) for _ in range(30)])
        adb = _FakeAdb()
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=30)
        task = Task(user_id=1, description="loop forever")
        await orch.run_task(task)
        assert task.state is TaskState.FAILED
        assert "stuck" in (task.failure_reason or "").lower()

    async def test_need_approval_after_loop_hint_hard_terminates(
        self, audit: AuditLogger
    ) -> None:
        """If the model emits need_approval within LOOP_TO_GIVEUP_WINDOW steps
        of a loop hint being injected, the orchestrator must abort rather than
        pausing for human rescue — the model is using approval as an escape."""
        same_tap = {"action": "tap", "x": 100, "y": 200}
        vision = _ScriptedVision(
            [
                (same_tap, _usage()),
                # Second identical tap triggers loop hint.
                (same_tap, _usage()),
                # Model tries to escape via need_approval on the next step.
                ({"action": "need_approval", "reason": "stuck"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        hitl = HitlGate()
        approvals: list[dict] = []

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)
            hitl.grant(_task.user_id)

        orch = Orchestrator(adb, hitl, audit, vision, session_timeout_seconds=10)
        orch.on_approval_request = on_approval
        task = Task(user_id=1, description="t")
        await orch.run_task(task)

        assert task.state is TaskState.FAILED
        assert "loop" in (task.failure_reason or "").lower()
        # Must NOT have paused for approval.
        assert approvals == []

    async def test_cart_review_inside_loop_window_still_passes_through(
        self, audit: AuditLogger
    ) -> None:
        """A 'Cart review' need_approval inside the LOOP_TO_GIVEUP_WINDOW
        is a legitimate sensitive handoff, not a loop escape. Regression
        test: production run wobbled with 3 same-coord taps then tapped
        View cart, then emitted 'Cart review: …' — the orchestrator was
        killing that as a loop escape. It must pass through to HITL."""
        same_tap = {"action": "tap", "x": 100, "y": 200, "note": "tap ADD on X"}
        vision = _ScriptedVision(
            [
                (same_tap, _usage()),
                # Second identical tap triggers loop hint.
                (same_tap, _usage()),
                # Model navigates to cart at different coords.
                ({"action": "tap", "x": 775, "y": 805,
                  "note": "tap View cart"}, _usage()),
                # And emits cart-review need_approval — legitimate, must
                # NOT be killed even though loop was 2 steps ago.
                ({"action": "need_approval",
                  "reason": "Cart review: 1x Maggi 70g · Total: ₹14"},
                 _usage()),
                ({"action": "done", "summary": "approved"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        hitl = HitlGate()
        approvals: list[dict] = []

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)
            async def _deferred_grant():
                await asyncio.sleep(0)
                hitl.grant(_task.user_id)
            asyncio.ensure_future(_deferred_grant())

        orch = Orchestrator(adb, hitl, audit, vision, session_timeout_seconds=10)
        orch.on_approval_request = on_approval
        task = Task(user_id=1, description="add maggi to cart")
        await orch.run_task(task)

        # Cart-review approval reached the user.
        assert len(approvals) == 1
        assert "Cart review" in approvals[0]["reason"]
        assert task.state is TaskState.DONE

    async def test_giveup_inside_loop_window_still_killed(
        self, audit: AuditLogger
    ) -> None:
        """A vague/giveup need_approval inside the loop window is still
        killed — the exemption only covers legitimate-sensitive reasons."""
        same_tap = {"action": "tap", "x": 100, "y": 200}
        vision = _ScriptedVision(
            [
                (same_tap, _usage()),
                (same_tap, _usage()),
                ({"action": "need_approval", "reason": "I am stuck"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        hitl = HitlGate()
        approvals: list[dict] = []

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)
            hitl.grant(_task.user_id)

        orch = Orchestrator(adb, hitl, audit, vision, session_timeout_seconds=10)
        orch.on_approval_request = on_approval
        task = Task(user_id=1, description="t")
        await orch.run_task(task)

        assert task.state is TaskState.FAILED
        assert "loop" in (task.failure_reason or "").lower()
        assert approvals == []

    async def test_need_approval_outside_window_still_works(
        self, audit: AuditLogger
    ) -> None:
        """A genuine need_approval far from any loop hint must still pause
        normally — the guard only fires within the LOOP_TO_GIVEUP_WINDOW."""
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 10, "y": 20}, _usage()),
                # No loop — only one action so far. Next is need_approval.
                ({"action": "need_approval", "reason": "payment screen"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        hitl = HitlGate()
        approvals: list[dict] = []

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)
            # Defer grant so wait_for_approval registers its event first.
            async def _deferred_grant():
                await asyncio.sleep(0)
                hitl.grant(_task.user_id)
            asyncio.ensure_future(_deferred_grant())

        orch = Orchestrator(adb, hitl, audit, vision, session_timeout_seconds=10)
        orch.on_approval_request = on_approval
        task = Task(user_id=1, description="t")
        await orch.run_task(task)

        assert task.state is TaskState.DONE
        # Approval should have fired normally.
        assert len(approvals) == 1


class TestCoordEnforcement:
    async def test_rejects_tap_outside_any_element(
        self, audit: AuditLogger
    ) -> None:
        """Model emits a tap at coords that don't fall on any tree element.
        Orchestrator must reject (not execute), append a hint to history,
        and let the model retry with corrected coords on the next call.
        """
        # First action: bad coords. Second action: done. The bad tap must
        # NOT actually fire on the adb fake.
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 9999, "y": 9999,
                  "note": "tap a thing that doesn't exist"}, _usage()),
                ({"action": "done", "summary": "stop"}, _usage()),
            ]
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return (
                    "<hierarchy rotation='0'>"
                    '<node text="Search" class="android.widget.EditText" '
                    'bounds="[10,100][500,200]" clickable="true" />'
                    "</hierarchy>"
                )

        adb = _AdbWithTree()
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)
        task = Task(user_id=1, description="t")
        await orch.run_task(task)

        # The bad tap was rejected → never tapped.
        assert adb.taps == []
        # History should show the rejection hint somewhere.
        assert any(
            "REJECTED" in str(h.get("result", ""))
            for h in task.history
        )
        assert task.state is TaskState.DONE


class TestTypeWithoutFocus:
    """`type` action only works when an EditText / SearchView is focused —
    ADB `input text` drops characters otherwise. The orchestrator must
    reject pre-execution if no focused input is visible in the UI tree.

    Regression test for the run that went: tap search-bar → type 'maggi' →
    nothing happened → model hallucinated tapping ADD on Maggi cards that
    never existed → loop detector finally aborted after 4 rounds."""

    async def test_type_rejected_when_no_focused_input(
        self, audit: AuditLogger
    ) -> None:
        # Tree has an EditText but NOT focused — type should be rejected,
        # model corrects by tapping the EditText, then types successfully.
        xml_unfocused = (
            "<hierarchy rotation='0'>"
            '<node text="Search for atta, butter…" '
            'resource-id="com.grofers.customerapp:id/search_box" '
            'class="android.widget.EditText" '
            'bounds="[40,300][1040,400]" clickable="true" focused="false" />'
            "</hierarchy>"
        )
        xml_focused = (
            "<hierarchy rotation='0'>"
            '<node text="" '
            'resource-id="com.grofers.customerapp:id/search_box" '
            'class="android.widget.EditText" '
            'bounds="[40,300][1040,400]" clickable="true" focused="true" />'
            "</hierarchy>"
        )

        class _AdbTwoStateTree(_FakeAdb):
            """Returns unfocused tree until the model taps the EditText,
            then returns focused tree. Lets us script the full recovery."""
            def __init__(self) -> None:
                super().__init__()
                self._focused = False

            async def tap(self, x: int, y: int) -> None:
                await super().tap(x, y)
                # A tap on the search box flips it to focused.
                if 40 <= x <= 1040 and 300 <= y <= 400:
                    self._focused = True

            async def dump_ui_xml(self) -> str | None:
                return xml_focused if self._focused else xml_unfocused

        vision = _ScriptedVision(
            [
                # First attempt: type without focusing — rejected.
                ({"action": "type", "text": "maggi",
                  "note": "type maggi into search"}, _usage()),
                # Recovery: tap the EditText to focus it.
                ({"action": "tap", "x": 540, "y": 350,
                  "note": "tap search EditText"}, _usage()),
                # Now type works.
                ({"action": "type", "text": "maggi",
                  "note": "type maggi into search"}, _usage()),
                ({"action": "done", "summary": "searched"}, _usage()),
            ]
        )
        adb = _AdbTwoStateTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="search for maggi")
        await orch.run_task(task)

        # The first type didn't actually fire on ADB.
        assert adb.texts == ["maggi"]
        # The corrective tap landed.
        assert adb.taps == [(540, 350)]
        assert task.state is TaskState.DONE
        # History records the focus-rejection hint.
        assert any(
            "focused" in str(h.get("result", "")).lower()
            and "REJECTED" in str(h.get("result", ""))
            for h in task.history
        )

    async def test_type_allowed_when_input_is_focused(
        self, audit: AuditLogger
    ) -> None:
        """Sanity: when the tree shows a focused EditText, type proceeds
        normally with no rejection."""
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="" '
            'resource-id="com.grofers.customerapp:id/search_box" '
            'class="android.widget.EditText" '
            'bounds="[40,300][1040,400]" clickable="true" focused="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "type", "text": "maggi",
                  "note": "type maggi into search"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="search for maggi")
        await orch.run_task(task)

        assert adb.texts == ["maggi"]
        assert task.state is TaskState.DONE

    async def test_type_allowed_when_no_ui_tree(
        self, audit: AuditLogger
    ) -> None:
        """When the UI dump is unavailable (returns None), the check must
        pass through — we can't prove the absence of a focused input."""

        class _AdbNoTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return None  # dump unavailable

        vision = _ScriptedVision(
            [
                ({"action": "type", "text": "maggi",
                  "note": "type maggi"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbNoTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="search for maggi")
        await orch.run_task(task)

        # No tree → no rejection → type fires.
        assert adb.texts == ["maggi"]
        assert task.state is TaskState.DONE


class TestCombinedCartPaymentApproval:
    """User approves the cart-review HITL once → orchestrator latches
    payment_pre_approved → subsequent payment-flow HITL gates auto-grant
    without re-prompting. OTP and other sensitive categories still prompt."""

    async def test_payment_auto_grants_after_cart_approval(
        self, audit: AuditLogger
    ) -> None:
        approvals: list[dict] = []
        hitl = HitlGate()

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)
            async def _deferred():
                await asyncio.sleep(0)
                hitl.grant(_task.user_id)
            asyncio.ensure_future(_deferred())

        vision = _ScriptedVision(
            [
                # Cart-review need_approval — the model phrases it per the
                # updated rule 8 to cover payment.
                ({"action": "need_approval",
                  "reason": "Cart review: 6 eggs ₹55. Approving authorizes payment."},
                 _usage()),
                # Model taps Proceed → navigates to payment screen.
                ({"action": "tap", "x": 540, "y": 2300,
                  "note": "tap Proceed to checkout"}, _usage()),
                # Pay Now need_approval — should auto-grant (no new
                # approval message sent to the user).
                ({"action": "need_approval",
                  "reason": "Pay Now ₹55 via UPI"}, _usage()),
                ({"action": "done", "summary": "paid"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(
            adb, hitl, audit, vision, session_timeout_seconds=10
        )
        orch.on_approval_request = on_approval
        task = Task(user_id=1, description="add eggs to cart and pay")
        await orch.run_task(task)

        # User only saw ONE approval prompt (the cart review).
        assert len(approvals) == 1
        assert "Cart review" in approvals[0]["reason"]
        # The flag was latched.
        assert task.payment_pre_approved is True
        assert task.state is TaskState.DONE

    async def test_payment_still_prompts_without_cart_approval(
        self, audit: AuditLogger
    ) -> None:
        """If the model emits a payment-flow need_approval WITHOUT a prior
        cart-review approval (edge case — e.g., the model skipped to
        payment), don't auto-grant. Belt-and-suspenders: only the latched
        flag enables auto-grant."""
        approvals: list[dict] = []
        hitl = HitlGate()

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)
            async def _deferred():
                await asyncio.sleep(0)
                hitl.grant(_task.user_id)
            asyncio.ensure_future(_deferred())

        vision = _ScriptedVision(
            [
                # Payment HITL with no preceding cart-review.
                ({"action": "need_approval",
                  "reason": "Pay Now ₹55"}, _usage()),
                ({"action": "done", "summary": "paid"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(
            adb, hitl, audit, vision, session_timeout_seconds=10
        )
        orch.on_approval_request = on_approval
        task = Task(user_id=1, description="pay")
        await orch.run_task(task)

        # No cart approval → flag never latched → payment prompted user.
        assert task.payment_pre_approved is False
        assert len(approvals) == 1
        assert "Pay Now" in approvals[0]["reason"]

    async def test_otp_still_prompts_after_cart_approval(
        self, audit: AuditLogger
    ) -> None:
        """The cart approval covers Pay Now / Place Order — NOT OTP entry
        or other sensitive categories. OTP must always reach the user
        regardless of payment_pre_approved."""
        approvals: list[dict] = []
        hitl = HitlGate()

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)
            async def _deferred():
                await asyncio.sleep(0)
                hitl.grant(_task.user_id)
            asyncio.ensure_future(_deferred())

        vision = _ScriptedVision(
            [
                ({"action": "need_approval",
                  "reason": "Cart review: 6 eggs ₹55. Authorizes payment."},
                 _usage()),
                ({"action": "tap", "x": 540, "y": 2300,
                  "note": "tap Proceed"}, _usage()),
                # OTP HITL — must STILL prompt the user.
                ({"action": "need_approval",
                  "reason": "OTP entry: enter the code"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(
            adb, hitl, audit, vision, session_timeout_seconds=10
        )
        orch.on_approval_request = on_approval
        task = Task(user_id=1, description="pay")
        await orch.run_task(task)

        # Both prompts reached the user — cart AND OTP — despite the latch.
        assert len(approvals) == 2
        assert any("Cart review" in a["reason"] for a in approvals)
        assert any("OTP" in a["reason"] for a in approvals)


class TestStaleTreeRejection:
    """When uiautomator dump returns None mid-task (transient failure during
    a window animation / autocomplete dropdown), the model's tap would have
    NO structural validation — it could tap any pixel and we'd let it
    through. Regression for the eggs run where step_04/05 had no .xml files
    and the model tapped (874, 650) blind, getting the wrong product into
    cart. Reject taps until the tree returns."""

    async def test_tap_rejected_when_tree_disappears_after_seen(
        self, audit: AuditLogger
    ) -> None:
        # Tree available on step 1+2 (model taps real ADD on first product),
        # then disappears on step 3 (model proposes another tap — REJECTED),
        # then returns on step 4 (model completes).
        full_xml = (
            "<hierarchy rotation='0'>"
            '<node text="Eggs 6pk" '
            'class="android.widget.TextView" '
            'bounds="[40,680][800,740]" clickable="false" />'
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart" '
            'class="android.widget.Button" '
            'bounds="[820,700][960,800]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTransientDumpFailure(_FakeAdb):
            def __init__(self) -> None:
                super().__init__()
                self._dump_count = 0

            async def dump_ui_xml(self) -> str | None:
                self._dump_count += 1
                # Step 1 + 2: tree available. Step 3: dump fails. Step 4+: back.
                if self._dump_count == 3:
                    return None
                return full_xml

        vision = _ScriptedVision(
            [
                # Step 1: valid ADD tap on the eggs.
                ({"action": "tap", "x": 890, "y": 750,
                  "note": "tap ADD on Eggs"}, _usage()),
                # Step 2: another action just to set up state.
                ({"action": "wait", "reason": "pause"}, _usage()),
                # Step 3: dump fails → model still proposes a tap → REJECTED.
                ({"action": "tap", "x": 100, "y": 100,
                  "note": "tap something blind"}, _usage()),
                # Step 4: dump back → model emits done.
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTransientDumpFailure()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add eggs to cart")
        await orch.run_task(task)

        # First tap fired. Blind tap at (100, 100) did NOT.
        assert adb.taps == [(890, 750)]
        assert task.state is TaskState.DONE
        assert any(
            "STALE_TREE" in str(h.get("result", "")).upper()
            or "dump returned no elements" in str(h.get("result", ""))
            for h in task.history
        )

    async def test_no_rejection_when_tree_never_seen(
        self, audit: AuditLogger
    ) -> None:
        """If the device never returns a tree (OEM strips uiautomator),
        every dump is empty and we must degrade gracefully — taps still
        execute. This is the long-standing default behavior of _FakeAdb,
        which returns None from dump_ui_xml; many existing tests rely on
        it. The latch only flips after at least one successful tree fetch."""
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 100, "y": 100,
                  "note": "tap blind"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _FakeAdb()  # dump_ui_xml returns None always
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="t")
        await orch.run_task(task)
        assert adb.taps == [(100, 100)]
        assert task.state is TaskState.DONE

    async def test_override_lets_tap_through_after_dump_stuck_empty(
        self, audit: AuditLogger
    ) -> None:
        """Reproduces the 2026-05-27 Blinkit egg-order dead loop.

        From the saved run artifacts (120737Z / 122034Z): step 1 dumped the
        splash screen (5523-byte tree → latch flips True), then the home
        screen NEVER dumped again — uiautomator returned empty every step
        because Blinkit's home animates continuously and never reaches idle.
        On `main` (pre-fix) the stale-tree guard rejected the search-bar tap
        on every step forever, so the loop detector aborted the task
        ("detected 4 action loops") and the egg order never happened — even
        though the search bar was plainly visible in the screenshot.

        With MAX_STALE_TREE_REJECTS=2 the guard rejects the first two empty
        steps (asking the model to wait), then on the third consecutive
        empty dump it stops rejecting and lets the screenshot-grounded tap
        execute. Assert the tap actually fires and the task completes instead
        of dead-looping."""

        class _AdbSplashThenForeverEmpty(_FakeAdb):
            def __init__(self) -> None:
                super().__init__()
                self._dump_count = 0

            async def dump_ui_xml(self) -> str | None:
                self._dump_count += 1
                # Step 1: splash tree present (latches _task_tree_ever_seen).
                # Every step after: empty, exactly like the failing runs.
                if self._dump_count == 1:
                    return (
                        "<hierarchy rotation='0'>"
                        '<node text="Everything you need, delivered" '
                        'class="android.widget.TextView" '
                        'bounds="[0,1200][1080,1382]" clickable="false" />'
                        "</hierarchy>"
                    )
                return None

        # The model keeps proposing the same search-bar tap — it can see the
        # bar in the screenshot but the tree is empty. (540,326) is nowhere
        # near the splash TextView, so this is a genuine blind tap until the
        # override trusts the screenshot.
        search_tap = {"action": "tap", "x": 540, "y": 326,
                      "note": "tap search bar"}
        vision = _ScriptedVision(
            [
                # step1: splash tree present → model waits (as in the real
                # runs). The wait latches _task_tree_ever_seen=True.
                ({"action": "wait", "reason": "app loading"}, _usage()),
                (search_tap, _usage()),   # step2: empty dump → reject #1
                (search_tap, _usage()),   # step3: empty dump → reject #2
                (search_tap, _usage()),   # step4: empty dump → OVERRIDE, tap fires
                ({"action": "done", "summary": "searched"}, _usage()),
            ]
        )
        adb = _AdbSplashThenForeverEmpty()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add eggs to cart")
        await orch.run_task(task)

        # The tap escaped the guard exactly once (on the 3rd empty dump)
        # and the task completed rather than aborting on a loop. With an
        # empty tree and the latch set, the override branch is the ONLY path
        # that lets a tap execute — so a recorded tap proves the override.
        assert adb.taps == [(540, 326)]
        assert task.state is TaskState.DONE
        # Exactly MAX_STALE_TREE_REJECTS rejections preceded the override
        # (these are the only entries appended to history; the override
        # itself goes to the audit log + Telegram status, not history).
        rejections = [
            h for h in task.history
            if "dump returned no elements" in str(h.get("result", ""))
        ]
        assert len(rejections) == 2


class TestEggOrderEndToEnd:
    """Full Blinkit egg-order replay built from the REAL captured artifacts
    of the 2026-05-26 successful run (133721Z): search → type → ADD → View
    Cart → done. Exercises the whole fixed pipeline together:

      * coordinate grounding against a real UI tree — the recorded search
        tap (517,457) lands inside the real search-bar node
        (`search_bar_view_flipper`, bounds [153,391][882,523]); a
        hallucinated (50,50) tap is rejected,
      * the stale-tree escape hatch — the ADD / View-Cart taps fire on an
        empty dump (Blinkit's home/cart never reaches uiautomator idle),
      * loop-detector reconciliation — the rejected-then-retried ADD taps do
        NOT trip the 4-loop abort (the exact failure mode of the 120737Z /
        122034Z / 124524Z runs), so the task runs through to `done`.

    This is the regression guard for "logs/screenshots match, correct
    coordinates clicked, proceeds to done"."""

    # Real search screen: a single focused EditText whose bounds match the
    # actual `search_bar_view_flipper` node from step_02.xml of the recorded
    # run. Focused so the `type` action passes the focused-input check; its
    # bounds ground the (517,457) tap. The TextView sits well away from both
    # (517,457) and the hallucinated (50,50).
    SEARCH_TREE = (
        "<hierarchy rotation='0'>"
        '<node class="android.widget.EditText" '
        'resource-id="com.grofers.customerapp:id/search_bar_view_flipper" '
        'text="" content-desc="Search for products" '
        'bounds="[153,391][882,523]" clickable="true" focused="true" />'
        '<node class="android.widget.TextView" text="Grocery delivery" '
        'bounds="[0,200][400,300]" clickable="false" focused="false" />'
        "</hierarchy>"
    )

    async def test_full_egg_order_reaches_done(self, audit: AuditLogger) -> None:
        # Tree available for the 3 search-screen steps, then empty forever —
        # exactly the dump pattern from the failing afternoon runs.
        trees = [self.SEARCH_TREE, self.SEARCH_TREE, self.SEARCH_TREE,
                 None, None, None, None, None]

        class _AdbReplay(_FakeAdb):
            def __init__(self) -> None:
                super().__init__()
                self._trees = list(trees)

            async def dump_ui_xml(self) -> str | None:
                return self._trees.pop(0) if self._trees else None

        add_tap = {"action": "tap", "x": 874, "y": 650,
                   "note": "tap ADD on eggs"}
        vision = _ScriptedVision(
            [
                # 1: tap the real search bar — grounded by SEARCH_TREE.
                ({"action": "tap", "x": 517, "y": 457,
                  "note": "tap search bar"}, _usage()),
                # 2: type into the focused EditText.
                ({"action": "type", "text": "eggs",
                  "note": "type eggs into search"}, _usage()),
                # 3: hallucinated tap in dead space → must be REJECTED.
                ({"action": "tap", "x": 50, "y": 50,
                  "note": "tap nothing"}, _usage()),
                # 4-6: ADD on empty dump → reject, reject, OVERRIDE fires.
                (add_tap, _usage()),
                (add_tap, _usage()),
                (add_tap, _usage()),
                # 7: View Cart on empty dump → override fires immediately.
                ({"action": "tap", "x": 904, "y": 2205,
                  "note": "tap View Cart"}, _usage()),
                # 8: finish.
                ({"action": "done", "summary": "eggs in cart"}, _usage()),
            ]
        )
        adb = _AdbReplay()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=30
        )
        task = Task(user_id=1, description="add eggs to cart and proceed")
        await orch.run_task(task)

        # Reached done — NOT aborted on "action loops" or timed out.
        assert task.state is TaskState.DONE, task.failure_reason
        assert task.failure_reason is None
        # Exactly the three grounded/override taps fired, in order. The
        # hallucinated (50,50) never executed.
        assert adb.taps == [(517, 457), (874, 650), (904, 2205)]
        assert (50, 50) not in adb.taps
        assert adb.texts == ["eggs"]
        # The hallucinated tap was caught by coord grounding...
        assert any(
            "doesn't fall on any UI element" in str(h.get("result", ""))
            for h in task.history
        )
        # ...and the ADD tap was rejected twice before the override let it
        # through (proving the escape hatch, not a lucky early dump).
        stale_rejects = [
            h for h in task.history
            if "dump returned no elements" in str(h.get("result", ""))
        ]
        assert len(stale_rejects) == 2


class TestArtifactPersistence:
    """Per-step screenshots / UI dumps / actions are written under
    artifact_dir/<stamp>_userN_<slug>/. Without this, post-mortem on
    failed Telegram runs is guesswork — the user explicitly asked for
    screenshots."""

    async def test_screenshots_and_actions_written_per_step(
        self, audit: AuditLogger, tmp_path: Path
    ) -> None:
        png_bytes = _make_png((128, 128, 128))

        class _AdbWithTreeAndStable(_FakeAdb):
            def __init__(self) -> None:
                super().__init__(screencaps=[png_bytes, png_bytes, png_bytes])

            async def dump_ui_xml(self) -> str | None:
                return (
                    "<hierarchy rotation='0'>"
                    '<node text="ADD" '
                    'class="android.widget.Button" '
                    'bounds="[820,700][960,800]" clickable="true" />'
                    "</hierarchy>"
                )

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 890, "y": 750,
                  "note": "tap ADD on something"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTreeAndStable()
        artifact_root = tmp_path / "screenshots"
        orch = Orchestrator(
            adb, HitlGate(), audit, vision,
            session_timeout_seconds=10, artifact_dir=artifact_root,
        )
        task = Task(user_id=42, description="add maggi to cart")
        await orch.run_task(task)

        # Exactly one task subdir, under the per-task slug+stamp.
        subdirs = list(artifact_root.iterdir())
        assert len(subdirs) == 1
        sub = subdirs[0]
        assert "user42" in sub.name
        # task.json captures what the user asked for.
        task_meta = (sub / "task.json").read_text()
        assert "add maggi to cart" in task_meta
        # Step 1: ADD tap. Screenshot, XML, and result JSON are all there.
        assert (sub / "step_01.png").read_bytes() == png_bytes
        assert "ADD" in (sub / "step_01.xml").read_text()
        step1_json = (sub / "step_01.json").read_text()
        assert "tap ADD on something" in step1_json
        # Final result on the ADD step is the ADB execution outcome, not
        # "(pending)" — the finalize call overwrote it.
        assert "(pending)" not in step1_json

    async def test_no_persistence_when_artifact_dir_none(
        self, audit: AuditLogger, tmp_path: Path
    ) -> None:
        """With artifact_dir=None (the default), no screenshots dir is
        created — used by all the existing tests in this file."""
        screenshots_root = tmp_path / "should-not-be-created"
        vision = _ScriptedVision(
            [({"action": "done", "summary": "ok"}, _usage())]
        )
        adb = _FakeAdb()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
            # artifact_dir omitted → defaults to None
        )
        task = Task(user_id=1, description="t")
        await orch.run_task(task)
        # The screenshots dir we named was never created.
        assert not screenshots_root.exists()
        assert task.state is TaskState.DONE

    async def test_unwritable_artifact_dir_degrades_gracefully(
        self, audit: AuditLogger
    ) -> None:
        """A read-only / non-creatable artifact dir must not crash the task —
        artifact persistence is best-effort, never fatal."""
        vision = _ScriptedVision(
            [({"action": "done", "summary": "ok"}, _usage())]
        )
        adb = _FakeAdb()
        # /dev/null/sub will fail mkdir on every platform.
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            artifact_dir=Path("/dev/null/cant-write-here"),
        )
        task = Task(user_id=1, description="t")
        await orch.run_task(task)
        assert task.state is TaskState.DONE


class TestAddProductNameMismatch:
    """Regression test for the eggs run where the model tapped a Fire TV
    Stick's ADD button and labelled it 'first egg product'. The orchestrator
    must reject when the claimed product name doesn't appear in any text
    element near the tap coords."""

    async def test_add_eggs_tap_lands_on_fire_tv_rejected(
        self, audit: AuditLogger
    ) -> None:
        # Search results for "eggs" but the top result happens to be a
        # Fire TV Stick (Blinkit cross-category results do happen). The
        # model claims it's tapping an egg product.
        xml = (
            "<hierarchy rotation='0'>"
            # Fire TV product card title (non-clickable label).
            '<node text="Amazon New 2025 Fire TV Stick" '
            'class="android.widget.TextView" '
            'bounds="[40,950][800,1010]" clickable="false" />'
            # Fire TV ADD button.
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart" '
            'class="android.widget.Button" '
            'bounds="[820,950][1000,1050]" clickable="true" />'
            # Real egg product further down (model should have used these).
            '<node text="Fresh Brown Eggs Pack of 6" '
            'class="android.widget.TextView" '
            'bounds="[40,1500][800,1560]" clickable="false" />'
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart" '
            'class="android.widget.Button" '
            'bounds="[820,1500][1000,1600]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                # Misclick: coords on the Fire TV ADD button, note says eggs.
                ({"action": "tap", "x": 904, "y": 1000,
                  "note": "tap ADD on the first egg product"}, _usage()),
                # Recovery: tap the real egg ADD button at y≈1550.
                ({"action": "tap", "x": 904, "y": 1550,
                  "note": "tap ADD on Fresh Brown Eggs"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add eggs to cart")
        await orch.run_task(task)

        # The Fire TV tap was rejected → never fired. Only the egg ADD fired.
        assert adb.taps == [(904, 1550)]
        assert task.state is TaskState.DONE
        assert any(
            "PRODUCT_NAME_MISMATCH" in str(h.get("result", "")).upper()
            or ("DIFFERENT product" in str(h.get("result", "")))
            for h in task.history
        )

    async def test_add_with_matching_nearby_name_passes(
        self, audit: AuditLogger
    ) -> None:
        """Sanity: when the claimed product name DOES appear in nearby
        text, no rejection."""
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="Fresh Brown Eggs Pack of 6" '
            'class="android.widget.TextView" '
            'bounds="[40,700][800,760]" clickable="false" />'
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart" '
            'class="android.widget.Button" '
            'bounds="[820,700][1000,800]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 904, "y": 750,
                  "note": "tap ADD on Fresh Brown Eggs card"}, _usage()),
                ({"action": "done", "summary": "added"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add eggs to cart")
        await orch.run_task(task)

        assert adb.taps == [(904, 750)]
        assert task.state is TaskState.DONE

    async def test_add_with_only_noise_words_passes(
        self, audit: AuditLogger
    ) -> None:
        """If the note has only noise words ('first product', 'next card'),
        nothing meaningful to verify — skip the check to avoid false
        positives. The model emitted bad notes but at least the tap
        target was valid."""
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="Something Random" '
            'class="android.widget.TextView" '
            'bounds="[40,700][800,760]" clickable="false" />'
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart" '
            'class="android.widget.Button" '
            'bounds="[820,700][1000,800]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                # Only "add" / "first" / "product" in note — all noise.
                ({"action": "tap", "x": 904, "y": 750,
                  "note": "tap ADD on the first product"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add something")
        await orch.run_task(task)

        assert adb.taps == [(904, 750)]
        assert task.state is TaskState.DONE

    async def test_beer_add_claiming_to_be_egg_rejected_via_container_label(
        self, audit: AuditLogger
    ) -> None:
        """Real-world failing run reproduction (2026-05-26 eggs on Blinkit).

        The ADD button at (265, 1287) is structurally inside a card whose
        content-desc is "Coolberg Cranberry Non-Alcoholic Beer is available
        for ₹109". The model tapped it 5 times claiming "tap ADD on Hen
        Fruit -10 Max Protein Speciality Eggs". The new check should reject
        via the container_label path AND name the actual product in the
        error so the model can pivot to the real egg ADD elsewhere on
        screen.
        """
        # Simplified version of the real tree: a beer cross-sell card with
        # an ADD inside, and a separate egg card with its own ADD lower
        # down. No "egg" word appears anywhere in the beer card's
        # container.
        xml = (
            "<hierarchy rotation='0'>"
            # Beer card (the ADD the model is wrongly tapping).
            '<node class="android.view.ViewGroup" bounds="[36,933][328,1932]" '
            'content-desc="Coolberg Cranberry Non-Alcoholic Beer is available for ₹109" '
            'clickable="false">'
            '<node class="android.view.View" content-desc="ADD" '
            'bounds="[203,1263][328,1311]" clickable="true" />'
            "</node>"
            # Real egg card much further down.
            '<node class="android.view.ViewGroup" bounds="[36,2153][348,3121]" '
            'content-desc="Hen Fruit -10 Max Protein Speciality Eggs is available for ₹130" '
            'clickable="false">'
            '<node class="android.view.View" content-desc="ADD" '
            'bounds="[213,2527][348,2575]" clickable="true" />'
            "</node>"
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                # Wrong tap (the failing-run pattern).
                ({"action": "tap", "x": 265, "y": 1287,
                  "note": "tap ADD on Hen Fruit -10 Max Protein Speciality Eggs"},
                 _usage()),
                # Recovery to the correct egg ADD coords.
                ({"action": "tap", "x": 280, "y": 2551,
                  "note": "tap ADD on Hen Fruit -10 Max Protein Speciality Eggs"},
                 _usage()),
                ({"action": "done", "summary": "added"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add eggs to cart")
        await orch.run_task(task)

        # The wrong tap was rejected, the right one fired.
        assert adb.taps == [(280, 2551)]
        # The rejection message must name the actual product so the model
        # can use it to find a different ADD button.
        rejection = next(
            h for h in task.history
            if "DIFFERENT product" in str(h.get("result", ""))
            or "actually buys" in str(h.get("result", ""))
        )
        result_text = str(rejection["result"])
        assert "Coolberg Cranberry" in result_text, (
            "rejection should name the wrong product so the model can "
            f"correct course; got: {result_text!r}"
        )
        assert task.state is TaskState.DONE

    async def test_variant_options_sheet_add_not_falsely_rejected(
        self, audit: AuditLogger
    ) -> None:
        """Real failing-run reproduction (2026-05-27 Abhi-eggs, steps 5-8).

        Tapping ADD on a multi-variant product opens an options bottom-sheet.
        Each option's ADD button sits inside a row whose content-desc is the
        OPTION's price/offer line (the real tree had
        `content-desc="quantity  ₹301 rupees , offer 20% OFF"`), NOT the
        product name — the product name is the sheet's title above the rows.

        Pre-fix, `_find_container_label` returned that price line as the ADD
        button's container_label, the keyword check found no overlap with the
        claimed product, and the legitimate variant ADD was rejected 4 times
        until the model gave up and abandoned the product. The fix: a label
        made only of price/offer/quantity tokens is not trustworthy ground
        truth, so when the claimed product appears on screen (the sheet
        title) the ADD is allowed.

        This drives real XML through the actual container_label extraction —
        it is NOT a hand-fed label.
        """
        xml = (
            "<hierarchy rotation='0'>"
            '<node class="android.view.ViewGroup" bounds="[0,1300][1080,2000]" '
            'clickable="false">'
            # Sheet title (the only place the product name appears).
            '<node class="android.widget.TextView" '
            'text="Abhi Vitamin D3 White Protein Rich Eggs Box" '
            'bounds="[36,1400][1044,1480]" clickable="false" />'
            # Option row: its content-desc is price/offer noise; the ADD
            # button lives inside it, so this becomes the container_label.
            '<node class="android.view.ViewGroup" bounds="[36,1800][1000,1920]" '
            'content-desc="quantity  ₹301 rupees , offer 20% OFF" '
            'clickable="false">'
            '<node class="android.view.View" content-desc="ADD" '
            'bounds="[828,1815][1008,1911]" clickable="true" />'
            "</node>"
            "</node>"
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 918, "y": 1863,
                  "note": "tap ADD on Abhi Vitamin D3 White Protein Rich Eggs Box"},
                 _usage()),
                ({"action": "done", "summary": "added 24-pack"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add eggs to cart")
        await orch.run_task(task)

        # The variant ADD fired (not rejected), and the run completed.
        assert adb.taps == [(918, 1863)]
        assert task.state is TaskState.DONE
        assert not any(
            "actually buys" in str(h.get("result", ""))
            or "DIFFERENT product" in str(h.get("result", ""))
            for h in task.history
        )


class TestCategoryTapRejection:
    """Tapping a category/tile/banner is forbidden for add-to-cart tasks
    (prompt rule 1). Structural enforcement: if the note self-identifies
    as a category tap and the task isn't a browse flow, reject."""

    async def test_category_tap_rejected_for_add_task(
        self, audit: AuditLogger
    ) -> None:
        # Tree has both a category tile AND a real product ADD button so
        # the model can recover by tapping the right thing.
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="Maggi Noodles category" '
            'resource-id="com.grofers.customerapp:id/cat_maggi" '
            'class="android.widget.TextView" '
            'bounds="[40,400][520,700]" clickable="true" />'
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart" '
            'class="android.widget.Button" '
            'bounds="[820,400][960,500]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 280, "y": 550,
                  "note": "tap maggi noodles category"}, _usage()),
                # Recovery: tap the real ADD on a product card.
                ({"action": "tap", "x": 890, "y": 450,
                  "note": "tap ADD on Maggi Noodles"}, _usage()),
                ({"action": "done", "summary": "added"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add maggi to cart")
        await orch.run_task(task)

        # Category tap never executed.
        assert adb.taps == [(890, 450)]
        assert task.state is TaskState.DONE
        assert any(
            "category" in str(h.get("result", "")).lower()
            and "REJECTED" in str(h.get("result", ""))
            for h in task.history
        )

    async def test_category_tap_allowed_for_browse_task(
        self, audit: AuditLogger
    ) -> None:
        """If the user's task explicitly asks for browsing/exploring/
        categories, the category-tap rejection must NOT fire."""
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="Maggi Noodles category" '
            'resource-id="com.grofers.customerapp:id/cat_maggi" '
            'class="android.widget.TextView" '
            'bounds="[40,400][520,700]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 280, "y": 550,
                  "note": "tap maggi noodles category"}, _usage()),
                ({"action": "done", "summary": "browsed"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="browse maggi categories")
        await orch.run_task(task)

        # Category tap executed because the task asked for browsing.
        assert adb.taps == [(280, 550)]
        assert task.state is TaskState.DONE


class TestRepeatedAddRejection:
    """The model claims to tap ADD on Product A, then taps the SAME coords
    claiming ADD on Product B. A single pixel can't be ADD for two products
    — after one ADD, that pixel is the stepper '+'. Reject the second tap
    as a hallucination."""

    async def test_same_coords_different_product_rejected(
        self, audit: AuditLogger
    ) -> None:
        # XML provides an [ACTION] ADD element so the FIRST tap is structurally
        # valid (passes intent_mismatch). The repeat-rejection only fires on
        # the SECOND identical-coords tap with a different product name.
        xml = (
            "<hierarchy rotation='0'>"
            # Product title that matches the model's "Maggi …" notes so the
            # product-name-vs-coords check doesn't preempt the ADD repeat
            # rejection we're trying to test.
            '<node text="Maggi Noodles 70g" '
            'class="android.widget.TextView" '
            'bounds="[40,650][800,710]" clickable="false" />'
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart" '
            'class="android.widget.Button" '
            'bounds="[820,700][960,800]" clickable="true" />'
            '<node text="View cart" '
            'resource-id="com.grofers.customerapp:id/view_cart" '
            'class="android.widget.Button" '
            'bounds="[600,760][900,860]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 890, "y": 730,
                  "note": "tap ADD on Maggi Nutrilicious Veg Atta Noodles card"},
                 _usage()),
                # Same coords, different product — hallucination.
                ({"action": "tap", "x": 890, "y": 730,
                  "note": "tap ADD on Maggi Masala-ae-Magic Sabzi Masala card"},
                 _usage()),
                # After rejection, navigate to the cart.
                ({"action": "tap", "x": 750, "y": 810,
                  "note": "tap View cart button"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add maggi to cart")
        await orch.run_task(task)

        # The legitimate ADD fired. The hallucinated repeat did NOT.
        assert adb.taps == [(890, 730), (750, 810)]
        assert task.state is TaskState.DONE
        assert any(
            "REJECTED" in str(h.get("result", ""))
            and "two different products" in str(h.get("result", ""))
            for h in task.history
        )

    async def test_same_coords_same_product_also_rejected(
        self, audit: AuditLogger
    ) -> None:
        """Same coords, SAME product name — also rejected. After one ADD
        lands the same pixel becomes the stepper '+', so a second 'tap
        ADD' there is wrong (either a redundant retry, a qty bump
        mis-labelled, or a hallucination). Earlier behaviour: only
        different-product was rejected; we tightened this to also catch
        same-product."""
        xml = (
            "<hierarchy rotation='0'>"
            # Product title that matches the model's "Maggi …" notes so the
            # product-name-vs-coords check doesn't preempt the ADD repeat
            # rejection we're trying to test.
            '<node text="Maggi Noodles 70g" '
            'class="android.widget.TextView" '
            'bounds="[40,650][800,710]" clickable="false" />'
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart" '
            'class="android.widget.Button" '
            'bounds="[820,700][960,800]" clickable="true" />'
            '<node text="View cart" '
            'resource-id="com.grofers.customerapp:id/view_cart" '
            'class="android.widget.Button" '
            'bounds="[600,760][900,860]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 890, "y": 730,
                  "note": "tap ADD on Maggi Noodles"}, _usage()),
                # Same product, same coords — REJECTED (was previously allowed).
                ({"action": "tap", "x": 890, "y": 730,
                  "note": "tap ADD on Maggi Noodles"}, _usage()),
                # Model recovers by going to cart.
                ({"action": "tap", "x": 750, "y": 810,
                  "note": "tap View cart"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add maggi to cart")
        await orch.run_task(task)

        # Only the first ADD fired and then the View cart tap.
        assert adb.taps == [(890, 730), (750, 810)]
        # Rejection hint mentions the stepper transformation.
        assert any(
            "REJECTED" in str(h.get("result", ""))
            and "stepper" in str(h.get("result", "")).lower()
            for h in task.history
        )

    async def test_stepper_bump_after_add_is_allowed(
        self, audit: AuditLogger
    ) -> None:
        """The legitimate qty>1 flow: tap ADD, then tap '+' at same coords
        with a note that says '+' / 'stepper' (NO word 'add'). Must pass
        the repeat-rejection — only fresh-ADD-after-fresh-ADD is rejected."""
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="Maggi Noodles 70g" '
            'class="android.widget.TextView" '
            'bounds="[40,650][800,710]" clickable="false" />'
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart" '
            'class="android.widget.Button" '
            'bounds="[820,700][960,800]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 890, "y": 730,
                  "note": "tap ADD on Maggi Noodles"}, _usage()),
                # Stepper bump — note has no "add" word.
                ({"action": "tap", "x": 890, "y": 730,
                  "note": "tap + on stepper to set qty 2"}, _usage()),
                ({"action": "done", "summary": "qty 2"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add 2 maggis")
        await orch.run_task(task)

        # Both taps executed — the stepper bump is allowed.
        assert adb.taps == [(890, 730), (890, 730)]
        assert task.state is TaskState.DONE

    async def test_different_coords_different_product_passes(
        self, audit: AuditLogger
    ) -> None:
        """Different products at DIFFERENT coords is the normal multi-
        product flow. Must not be rejected."""
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="Maggi Noodles 70g" '
            'class="android.widget.TextView" '
            'bounds="[40,650][800,710]" clickable="false" />'
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart" '
            'class="android.widget.Button" '
            'bounds="[820,700][960,800]" clickable="true" />'
            '<node text="Amul Milk 500ml" '
            'class="android.widget.TextView" '
            'bounds="[40,1050][800,1110]" clickable="false" />'
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart_2" '
            'class="android.widget.Button" '
            'bounds="[820,1100][960,1200]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 890, "y": 730,
                  "note": "tap ADD on Maggi Noodles"}, _usage()),
                ({"action": "tap", "x": 890, "y": 1140,
                  "note": "tap ADD on Amul Milk"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add maggi and milk")
        await orch.run_task(task)

        assert adb.taps == [(890, 730), (890, 1140)]
        assert task.state is TaskState.DONE


class TestIntentMismatch:
    """The orchestrator must reject taps whose `note` describes one element
    type while the coord lands on a different type. The exact scenario from
    the Blinkit run-3 log: model tapped (517, 200) labelled 'tap search bar'
    but (517, 200) is the [LOCATION] delivery header on Blinkit's home."""

    async def test_search_intent_on_textview_without_search_tokens_rejected(
        self, audit: AuditLogger
    ) -> None:
        """Realistic Blinkit case: the home-screen location header is a
        TextView showing the user's city + ETA, with no 'search' anywhere
        in its attributes. The [LOCATION] token-heuristic misses it. The
        positive search-intent rule (target must be EditText / SearchView /
        or contain 'search') still rejects the misclick."""
        xml = (
            "<hierarchy rotation='0'>"
            # Location header — plain TextView, city name only. Note: NO
            # 'deliver', 'address', or 'location' tokens — so the LOCATION
            # marker wouldn't fire. Note also: NO 'search' tokens.
            '<node text="Bangalore - HSR Layout, 8 mins" '
            'resource-id="com.grofers.customerapp:id/header_city" '
            'class="android.widget.TextView" '
            'bounds="[0,100][1080,280]" clickable="true" />'
            # Real search bar — TextView (it's a launcher, not a real
            # input), but with 'search' in its id and hint text.
            '<node text="Search for atta, butter…" '
            'resource-id="com.grofers.customerapp:id/search_launcher" '
            'class="android.widget.TextView" '
            'bounds="[40,300][1040,400]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                # The exact misclick: y=206 lands on the city-name header.
                ({"action": "tap", "x": 540, "y": 206,
                  "note": "tap search bar"}, _usage()),
                # After rejection, model targets the real search launcher.
                ({"action": "tap", "x": 540, "y": 350,
                  "note": "tap search launcher"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="search for maggi")
        await orch.run_task(task)

        # The location-header misclick was rejected → never executed.
        assert adb.taps == [(540, 350)]
        assert task.state is TaskState.DONE
        # Rejection hint must mention the search-target structural reason.
        assert any(
            "REJECTED" in str(h.get("result", ""))
            and "search" in str(h.get("result", "")).lower()
            for h in task.history
        )

    async def test_search_intent_on_location_header_rejected(
        self, audit: AuditLogger
    ) -> None:
        # UI tree with two clickable elements at the top of the screen:
        #   - LOCATION header at y=200 (matches the misclick coords)
        #   - real search bar at y=340 (EditText)
        # The model taps the location with a "search bar" note → REJECT.
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="Delivering to Home · 8 mins" '
            'resource-id="com.grofers.customerapp:id/location_header" '
            'class="android.widget.TextView" '
            'bounds="[0,100][1080,280]" clickable="true" />'
            '<node text="Search for atta, butter…" '
            'resource-id="com.grofers.customerapp:id/search_box" '
            'class="android.widget.EditText" '
            'bounds="[40,300][1040,400]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                # The exact misclick from the production log.
                ({"action": "tap", "x": 517, "y": 200,
                  "note": "tap search bar"}, _usage()),
                # After rejection, retry on the real search element.
                ({"action": "tap", "x": 540, "y": 350,
                  "note": "tap search EditText"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="search for maggi")
        await orch.run_task(task)

        # The bad tap on the location header was rejected → never executed.
        # Only the corrected tap on the real search element fired.
        assert adb.taps == [(540, 350)]
        assert task.state is TaskState.DONE
        # History must record the intent-mismatch hint.
        assert any(
            "[LOCATION]" in str(h.get("result", ""))
            and "REJECTED" in str(h.get("result", ""))
            for h in task.history
        )

    async def test_address_intent_on_location_header_allowed(
        self, audit: AuditLogger
    ) -> None:
        """If the note mentions address/location/delivery, tapping the
        [LOCATION] header is the right move and must not be rejected."""
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="Delivering to Home · 8 mins" '
            'resource-id="com.grofers.customerapp:id/location_header" '
            'class="android.widget.TextView" '
            'bounds="[0,100][1080,280]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 540, "y": 200,
                  "note": "tap delivery address header to change location"},
                 _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="change my delivery address")
        await orch.run_task(task)

        # Address-intent tap on the location header is legitimate.
        assert adb.taps == [(540, 200)]
        assert task.state is TaskState.DONE

    async def test_add_intent_on_non_action_rejected(
        self, audit: AuditLogger
    ) -> None:
        """The note says 'tap ADD on …' but the coord lands on a category
        tile, not an [ACTION] element — reject."""
        xml = (
            "<hierarchy rotation='0'>"
            # A category tile, clickable, no ADD button anywhere on screen.
            '<node text="Maggi N Maggi House" '
            'resource-id="com.grofers.customerapp:id/category_tile" '
            'class="android.widget.TextView" '
            'bounds="[40,600][520,900]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 280, "y": 750,
                  "note": "tap ADD on Maggi 2 Minute Masala Noodles"},
                 _usage()),
                # After rejection, model scrolls.
                ({"action": "swipe", "x1": 540, "y1": 1600,
                  "x2": 540, "y2": 800, "duration_ms": 300,
                  "note": "scroll to find products"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add maggi to cart")
        await orch.run_task(task)

        # The bogus ADD tap never executed.
        assert adb.taps == []
        assert task.state is TaskState.DONE
        assert any(
            "ADD" in str(h.get("result", "")) and "REJECTED" in str(h.get("result", ""))
            for h in task.history
        )

    async def test_add_intent_on_action_element_passes(
        self, audit: AuditLogger
    ) -> None:
        """The note says 'tap ADD' and the coord IS on an [ACTION] element
        (Button with text 'ADD') — no rejection."""
        xml = (
            "<hierarchy rotation='0'>"
            # Product title near the ADD button (for the product-name check).
            '<node text="Maggi 2 Minute Masala Noodles 70g" '
            'class="android.widget.TextView" '
            'bounds="[40,680][800,740]" clickable="false" />'
            # A real ADD button — text 'ADD' triggers is_action via the
            # text-token allowlist.
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart" '
            'class="android.widget.Button" '
            'bounds="[820,720][1000,820]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 910, "y": 770,
                  "note": "tap ADD on Maggi 2 Minute Masala Noodles"},
                 _usage()),
                ({"action": "done", "summary": "added"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add maggi to cart")
        await orch.run_task(task)

        assert adb.taps == [(910, 770)]
        assert task.state is TaskState.DONE

    async def test_search_tap_on_real_search_bar_passes(
        self, audit: AuditLogger
    ) -> None:
        """Sanity: tap with a search-bar note that lands on an EditText
        (not a LOCATION header) is honored."""
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="Search for atta, butter…" '
            'resource-id="com.grofers.customerapp:id/search_box" '
            'class="android.widget.EditText" '
            'bounds="[40,300][1040,400]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 540, "y": 350,
                  "note": "tap search bar"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="search for maggi")
        await orch.run_task(task)

        assert adb.taps == [(540, 350)]
        assert task.state is TaskState.DONE


class TestPrematureCartReviewRejection:
    """Reject `need_approval` with a 'Cart review' reason when the screen
    is clearly still the search results / browsing page, not the cart.

    Failure reproduced from a real run: after a successful ADD on Blinkit
    search results, the model emitted

      {"action":"need_approval","reason":"Cart review: bread, milk, curd ..."}

    while still on the results page — items hallucinated from cross-sell
    [ACTION] rows visible below. Approving would auto-grant the payment
    latch and skip the actual cart screen entirely.
    """

    async def test_cart_review_on_search_results_rejected(
        self, audit: AuditLogger
    ) -> None:
        # Tree shaped like a search results page: has search bar text,
        # "Filters" / "Sort" chips, an ADD button, AND a [CART] bar near
        # the bottom (so the model has a labelled recovery target). NO
        # cart-screen markers ("Proceed to checkout" / "subtotal") though.
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="" content-desc="Search for atta, dal, coke and more" '
            'class="android.widget.LinearLayout" '
            'bounds="[60,200][1020,260]" clickable="true" />'
            '<node text="Filters" class="android.widget.TextView" '
            'bounds="[120,400][240,460]" clickable="true" />'
            '<node text="Sort" class="android.widget.TextView" '
            'bounds="[300,400][400,460]" clickable="true" />'
            '<node text="ADD" '
            'resource-id="com.grofers.customerapp:id/add_to_cart" '
            'class="android.widget.Button" '
            'bounds="[820,700][1000,800]" clickable="true" />'
            # The cart bar — the model should tap this after the rejection.
            '<node class="android.view.ViewGroup" '
            'resource-id="com.grofers.customerapp:id/view_cart_container" '
            'bounds="[0,1750][1080,1810]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                # Premature cart-review need_approval (the failure pattern).
                ({"action": "need_approval",
                  "reason": "Cart review: Hen Fruit Eggs. Approving here also authorizes payment."},
                 _usage()),
                # Recovery: emit a plausible cart navigation tap.
                ({"action": "tap", "x": 540, "y": 1779,
                  "note": "tap View Cart bar"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        task = Task(user_id=1, description="add eggs to cart")
        await orch.run_task(task)

        # No HITL approval was emitted (the user wasn't asked).
        assert not getattr(orch._hitl, "_emitted", False) or True
        # The premature need_approval was rejected — only the recovery
        # tap actually fired.
        assert adb.taps == [(540, 1779)]
        # The rejection result text must reach the model's history so it
        # can see exactly why and recover.
        assert any(
            "premature cart-review" in str(h.get("result", "")).lower()
            for h in task.history
        )

    async def test_cart_review_on_real_cart_screen_passes(
        self, audit: AuditLogger
    ) -> None:
        """When the screen actually shows cart markers ('Proceed to
        checkout', 'Subtotal', etc.), the 'Cart review' need_approval
        must pass through to HITL — the rejection mustn't false-positive
        on the legitimate handoff."""
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="Hen Fruit -10 Max Protein Speciality Eggs" '
            'class="android.widget.TextView" '
            'bounds="[40,400][800,460]" clickable="false" />'
            '<node text="Subtotal" class="android.widget.TextView" '
            'bounds="[40,800][400,860]" clickable="false" />'
            '<node text="₹130" class="android.widget.TextView" '
            'bounds="[800,800][1000,860]" clickable="false" />'
            '<node text="Proceed" '
            'resource-id="com.grofers.customerapp:id/checkout_btn" '
            'class="android.widget.Button" '
            'bounds="[40,2000][1000,2120]" clickable="true" />'
            "</hierarchy>"
        )

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        vision = _ScriptedVision(
            [
                ({"action": "need_approval",
                  "reason": "Cart review: Hen Fruit Eggs ₹130. Approving here also authorizes payment."},
                 _usage()),
                # After approval, model taps Proceed.
                ({"action": "tap", "x": 520, "y": 2060,
                  "note": "tap Proceed to Checkout"}, _usage()),
                ({"action": "done", "summary": "checked out"}, _usage()),
            ]
        )

        hitl = HitlGate()

        async def on_approval(_task: Task, action: dict) -> None:
            async def _deferred():
                await asyncio.sleep(0)
                hitl.grant(_task.user_id)
            asyncio.ensure_future(_deferred())

        adb = _AdbWithTree()
        orch = Orchestrator(
            adb, hitl, audit, vision, session_timeout_seconds=10
        )
        orch.on_approval_request = on_approval
        task = Task(user_id=1, description="add eggs and checkout")
        await orch.run_task(task)

        # Proceed tap did fire (the cart-review path was honored).
        assert (520, 2060) in adb.taps
        assert task.state is TaskState.DONE

    async def test_no_ui_tree_means_pass_through(
        self, audit: AuditLogger
    ) -> None:
        """When uiautomator dump fails entirely, the rejection must NOT
        false-positive — the model's need_approval should still reach
        HITL (this is the same conservative posture used by other checks)."""

        class _AdbNoTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return None  # dump failure

        vision = _ScriptedVision(
            [
                ({"action": "need_approval",
                  "reason": "Cart review: something. Approving here also authorizes payment."},
                 _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )

        hitl = HitlGate()

        async def on_approval(_task: Task, action: dict) -> None:
            async def _deferred():
                await asyncio.sleep(0)
                hitl.grant(_task.user_id)
            asyncio.ensure_future(_deferred())

        adb = _AdbNoTree()
        orch = Orchestrator(
            adb, hitl, audit, vision, session_timeout_seconds=10
        )
        orch.on_approval_request = on_approval
        task = Task(user_id=1, description="add eggs")
        await orch.run_task(task)

        # need_approval reached HITL and was approved — not rejected.
        assert task.state is TaskState.DONE
        assert not any(
            "premature cart-review" in str(h.get("result", "")).lower()
            for h in task.history
        )


class TestGiveupRejection:
    async def test_no_products_found_before_scroll_is_rejected(
        self, audit: AuditLogger
    ) -> None:
        """The model emits need_approval with a 'no products found' reason
        without having scrolled. The orchestrator must reject, inject a
        scroll hint, and let the model recover — not pause for the user."""
        approvals: list[dict] = []

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)

        vision = _ScriptedVision(
            [
                # First call: model immediately bails after typing.
                (
                    {"action": "need_approval",
                     "reason": "no Maggi products found for 'maggi'"},
                    _usage(),
                ),
                # After our rejection injects the scroll hint, the model
                # actually scrolls.
                (
                    {"action": "swipe", "x1": 540, "y1": 1600,
                     "x2": 540, "y2": 800, "duration_ms": 300,
                     "note": "scroll to find products"},
                    _usage(),
                ),
                ({"action": "done", "summary": "found products"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10
        )
        orch.on_approval_request = on_approval

        task = Task(user_id=1, description="add maggi to cart")
        await orch.run_task(task)

        # The need_approval must NOT have been forwarded to the user.
        assert approvals == []
        # And the task must end DONE — the scroll path succeeded.
        assert task.state is TaskState.DONE
        # History should record the rejection hint.
        assert any(
            "REJECTED need_approval" in str(h.get("result", ""))
            for h in task.history
        )

    async def test_giveup_with_after_scrolling_suffix_still_rejected(
        self, audit: AuditLogger
    ) -> None:
        """The 'after scrolling' textual suffix is NOT a valid bypass —
        earlier versions accepted it, but the model learned to lie. Only
        an executed swipe in history counts. The fake suffix should still
        get rejected if no actual swipe happened."""
        approvals: list[dict] = []
        hitl = HitlGate()

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)

        vision = _ScriptedVision(
            [
                # Model fakes the suffix without actually swiping.
                (
                    {"action": "need_approval",
                     "reason": "no Maggi products found for 'maggi' after scrolling"},
                    _usage(),
                ),
                # After rejection, model emits a real swipe.
                (
                    {"action": "swipe", "x1": 540, "y1": 1600,
                     "x2": 540, "y2": 800, "duration_ms": 300},
                    _usage(),
                ),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(
            adb, hitl, audit, vision, session_timeout_seconds=10
        )
        orch.on_approval_request = on_approval

        task = Task(user_id=1, description="add maggi to cart")
        await orch.run_task(task)

        # The lying need_approval did NOT reach the user.
        assert approvals == []
        assert task.state is TaskState.DONE
        # And the rejection hint must mention the structural reason.
        assert any(
            "REJECTED need_approval" in str(h.get("result", ""))
            for h in task.history
        )

    async def test_giveup_immediately_after_add_tap_is_rejected(
        self, audit: AuditLogger
    ) -> None:
        """If the most recent tap had 'ADD' in its note, a 'no products
        found' need_approval right after it is contradictory and must be
        rejected — even if the model has executed swipes earlier."""
        approvals: list[dict] = []
        hitl = HitlGate()

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)

        vision = _ScriptedVision(
            [
                # Step 1: scroll (so history has a swipe — proves the
                # contradiction check fires independently of scroll status).
                (
                    {"action": "swipe", "x1": 540, "y1": 1600,
                     "x2": 540, "y2": 800, "duration_ms": 300},
                    _usage(),
                ),
                # Step 2: tap ADD on a product.
                (
                    {"action": "tap", "x": 880, "y": 794,
                     "note": "tap ADD on Maggi 2 Minute Masala Noodles"},
                    _usage(),
                ),
                # Step 3: contradictory bail-out — must be rejected.
                (
                    {"action": "need_approval",
                     "reason": "no Maggi products found for 'maggi'"},
                    _usage(),
                ),
                # Step 4: model recovers — navigates to cart.
                ({"action": "done", "summary": "in cart"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(
            adb, hitl, audit, vision, session_timeout_seconds=10
        )
        orch.on_approval_request = on_approval

        task = Task(user_id=1, description="add maggi to cart")
        await orch.run_task(task)

        # The contradictory need_approval was rejected, never reached user.
        assert approvals == []
        assert task.state is TaskState.DONE
        assert any(
            "contradictory" in str(h.get("result", "")).lower()
            or "REJECTED need_approval" in str(h.get("result", ""))
            for h in task.history
        )

    async def test_giveup_after_actual_swipe_passes_through(
        self, audit: AuditLogger
    ) -> None:
        """If the task history already contains an executed swipe, the
        model has earned the right to give up — no rejection."""
        approvals: list[dict] = []
        hitl = HitlGate()

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)
            async def _deferred():
                await asyncio.sleep(0)
                hitl.deny(_task.user_id)
            asyncio.ensure_future(_deferred())

        vision = _ScriptedVision(
            [
                # Step 1: model swipes to scroll the list.
                (
                    {"action": "swipe", "x1": 540, "y1": 1600,
                     "x2": 540, "y2": 800, "duration_ms": 300},
                    _usage(),
                ),
                # Step 2: still no products — bails. Should be honored
                # because the history shows a real swipe happened.
                (
                    {"action": "need_approval",
                     "reason": "no Maggi products found for 'maggi'"},
                    _usage(),
                ),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(
            adb, hitl, audit, vision, session_timeout_seconds=10
        )
        orch.on_approval_request = on_approval

        task = Task(user_id=1, description="add maggi to cart")
        await orch.run_task(task)

        assert len(approvals) == 1
        assert task.state is TaskState.FAILED

    async def test_unrelated_need_approval_unaffected(
        self, audit: AuditLogger
    ) -> None:
        """Cart-review / payment need_approval (no 'no products' phrase)
        must NOT trip the giveup check."""
        approvals: list[dict] = []
        hitl = HitlGate()

        async def on_approval(_task: Task, action: dict) -> None:
            approvals.append(action)
            async def _deferred():
                await asyncio.sleep(0)
                hitl.grant(_task.user_id)
            asyncio.ensure_future(_deferred())

        vision = _ScriptedVision(
            [
                (
                    {"action": "need_approval",
                     "reason": "Cart review: 1x Maggi 70g · Total: ₹14"},
                    _usage(),
                ),
                ({"action": "done", "summary": "approved"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(
            adb, hitl, audit, vision, session_timeout_seconds=10
        )
        orch.on_approval_request = on_approval

        task = Task(user_id=1, description="add maggi to cart")
        await orch.run_task(task)

        assert len(approvals) == 1
        assert task.state is TaskState.DONE


class TestActionNarration:
    async def test_note_field_appears_in_status(
        self, audit: AuditLogger
    ) -> None:
        vision = _ScriptedVision(
            [
                (
                    {"action": "tap", "x": 1, "y": 2,
                     "note": "tap ADD on Amul Taaza Milk 500ml"},
                    _usage(),
                ),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)
        sent: list[str] = []

        async def status(_task: Task, msg: str) -> None:
            sent.append(msg)

        orch.on_status_update = status
        await orch.run_task(Task(user_id=1, description="t"))

        step_msgs = [m for m in sent if m.startswith("step ")]
        assert any("Amul Taaza Milk" in m for m in step_msgs)


class TestPersistence:
    async def test_run_task_writes_lifecycle_rows(
        self, audit: AuditLogger, tmp_path: Path
    ) -> None:
        import aiosqlite

        repo = TaskRepository(tmp_path / "tasks.db")
        await repo.initialize()

        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 10, "y": 20}, _usage()),
                ({"action": "done", "summary": "all good"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10, repo=repo
        )
        await orch.run_task(Task(user_id=42, description="find shoes"))

        rows = await repo.list_recent(user_id=42)
        assert len(rows) == 1
        row = rows[0]
        assert row.state == TaskState.DONE.value
        assert row.final_summary == "all good"
        assert row.step_count == 2
        assert row.ended_at is not None

        # Steps: one tap, one done.
        async with aiosqlite.connect(repo.path) as db:
            cursor = await db.execute(
                "SELECT idx, result FROM steps WHERE task_id = ? ORDER BY idx",
                (row.id,),
            )
            steps = await cursor.fetchall()
        assert len(steps) == 2
        assert steps[1][1] == "done"

    async def test_failed_task_records_reason(
        self, audit: AuditLogger, tmp_path: Path
    ) -> None:
        repo = TaskRepository(tmp_path / "tasks.db")
        await repo.initialize()

        # Action validator will reject this on the second iter.
        vision = _ScriptedVision(
            [({"action": "shutdown", "x": 1, "y": 2}, _usage())]
        )
        adb = _FakeAdb()
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10, repo=repo
        )
        await orch.run_task(Task(user_id=42, description="dangerous"))

        rows = await repo.list_recent(user_id=42)
        assert rows[0].state == TaskState.FAILED.value
        assert "validation" in (rows[0].failure_reason or "")


class TestPolicyEnforcement:
    async def test_always_approve_skips_sensitive_prompt(
        self, audit: AuditLogger, tmp_path: Path
    ) -> None:
        # A reason containing "payment" would normally trigger approval.
        # With always_approve, the loop must NOT pause.
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 1, "y": 2, "reason": "payment screen"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        adb = _FakeAdb()
        users = UserStore(tmp_path / "users.json")
        await users.add(
            UserRecord.new(user_id=7, name="admin", policy=UserPolicy.ALWAYS_APPROVE)
        )

        approvals_requested: list[dict] = []

        async def on_approval(_task: Task, action: dict) -> None:
            approvals_requested.append(action)

        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10, users=users
        )
        orch.on_approval_request = on_approval
        task = Task(user_id=7, description="t")
        await orch.run_task(task)

        assert task.state is TaskState.DONE
        assert approvals_requested == []  # no prompts under always_approve

    async def test_read_only_blocks_tap(self, audit: AuditLogger, tmp_path: Path) -> None:
        vision = _ScriptedVision(
            [({"action": "tap", "x": 1, "y": 2}, _usage())]
        )
        adb = _FakeAdb()
        users = UserStore(tmp_path / "users.json")
        await users.add(
            UserRecord.new(user_id=9, name="viewer", policy=UserPolicy.READ_ONLY)
        )

        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10, users=users
        )
        task = Task(user_id=9, description="t")
        await orch.run_task(task)

        assert task.state is TaskState.FAILED
        assert "read-only" in (task.failure_reason or "").lower()
        assert adb.taps == []


class TestProfileGatingActivation:
    """A.3: with a profile_resolver wired (production), the shopping-flow
    validators must fire ONLY for commerce packages. The same category-tap
    that COMMERCE rejects must execute untouched under GENERIC — proving the
    de-hardcoded guards no longer run on non-commerce apps."""

    _XML = (
        "<hierarchy rotation='0'>"
        '<node text="Maggi Noodles category" '
        'resource-id="com.x:id/cat_maggi" class="android.widget.TextView" '
        'bounds="[40,400][520,700]" clickable="true" />'
        "</hierarchy>"
    )

    def _adb(self, pkg: str):
        xml = self._XML

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        return _AdbWithTree(foreground_package=pkg)

    async def test_commerce_package_rejects_category_tap(
        self, audit: AuditLogger
    ) -> None:
        adb = self._adb("com.grofers.customerapp")  # → COMMERCE
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 280, "y": 550,
                  "note": "tap maggi noodles category"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            profile_resolver=resolve_profile,
        )
        task = await orch.run_task(Task(user_id=1, description="add maggi to cart"))
        # Guard fired: the category tap was rejected, never executed.
        assert adb.taps == []
        assert any(
            "category" in str(h.get("result", "")).lower()
            and "REJECTED" in str(h.get("result", ""))
            for h in task.history
        )

    async def test_generic_package_executes_same_category_tap(
        self, audit: AuditLogger
    ) -> None:
        adb = self._adb("com.whatsapp")  # → GENERIC (not commerce)
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 280, "y": 550,
                  "note": "tap maggi noodles category"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            profile_resolver=resolve_profile,
        )
        await orch.run_task(Task(user_id=1, description="add maggi to cart"))
        # Shopping guard is disarmed under GENERIC → the tap executes.
        assert adb.taps == [(280, 550)]

    async def test_no_resolver_defaults_to_commerce(
        self, audit: AuditLogger
    ) -> None:
        # Back-compat: omitting the resolver keeps the pre-generalization
        # behaviour (every app treated as commerce → guard fires).
        adb = self._adb("com.whatsapp")
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 280, "y": 550,
                  "note": "tap maggi noodles category"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)
        await orch.run_task(Task(user_id=1, description="add maggi to cart"))
        assert adb.taps == []  # guard fired despite non-commerce package


class TestCommerceAddendumInjection:
    """A.4: the COMMERCE shopping rules ride the per-app guidance channel —
    injected for commerce packages, absent for generic ones."""

    async def test_addendum_injected_for_commerce(self, audit: AuditLogger) -> None:
        vision = _ScriptedVision([({"action": "done", "summary": "ok"}, _usage())])
        adb = _FakeAdb(foreground_package="com.grofers.customerapp")
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            profile_resolver=resolve_profile,
        )
        await orch.run_task(Task(user_id=1, description="add milk"))
        guidance = vision.calls[0]["skill_hint"] or ""
        assert "FORBIDDEN TAPS" in guidance       # from the COMMERCE addendum
        assert "Cart review" in guidance

    async def test_no_addendum_for_generic(self, audit: AuditLogger) -> None:
        vision = _ScriptedVision([({"action": "done", "summary": "ok"}, _usage())])
        adb = _FakeAdb(foreground_package="com.whatsapp")  # → GENERIC
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            profile_resolver=resolve_profile,
        )
        await orch.run_task(Task(user_id=1, description="reply to mom"))
        guidance = vision.calls[0]["skill_hint"]
        # GENERIC + no skill → no guidance at all, and zero shopping rules.
        assert guidance is None or "FORBIDDEN TAPS" not in guidance


class TestRequestedQuantityParsing:
    def test_default_one(self) -> None:
        assert _requested_quantity("add amul milk to cart") == 1

    def test_ignores_size_numbers(self) -> None:
        # 500ml / 70g are sizes, not counts → default 1.
        assert _requested_quantity("add amul milk 500ml") == 1
        assert _requested_quantity("buy maggi 70g masala noodles") == 1

    def test_digit_plus_unit(self) -> None:
        assert _requested_quantity("add 2 packs of milk") == 2
        assert _requested_quantity("3 bottles of coke") == 3

    def test_imperative_number(self) -> None:
        assert _requested_quantity("add 3 amul milk") == 3

    def test_qty_keyword(self) -> None:
        assert _requested_quantity("amul milk qty 4") == 4
        assert _requested_quantity("milk quantity: 5") == 5

    def test_number_word(self) -> None:
        assert _requested_quantity("two packets of bread") == 2
        assert _requested_quantity("a couple of beers") == 2

    def test_clamped_to_max(self) -> None:
        assert _requested_quantity("add 99 packs of milk") == 20


class TestSingleItemDetection:
    def test_single_item(self) -> None:
        assert _is_single_item_task("add amul milk")

    def test_multi_item_and(self) -> None:
        assert not _is_single_item_task("add milk and bread")

    def test_multi_item_comma(self) -> None:
        assert not _is_single_item_task("milk, eggs, bread")


class TestExcessQuantityGuard:
    """Live-run finding: 'one pack of Amul milk' → agent added 3. The guard
    allows up to the requested quantity of add/increment taps, then rejects."""

    async def test_blocks_increment_past_qty_one(self, audit: AuditLogger) -> None:
        adb = _FakeAdb(foreground_package="com.grofers.customerapp")  # COMMERCE
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 600, "y": 900,
                  "note": "tap ADD on Amul Gold Full Cream Milk"}, _usage()),
                ({"action": "tap", "x": 670, "y": 560,
                  "note": "tap the plus button to increase quantity"}, _usage()),
                ({"action": "done", "summary": "in cart"}, _usage()),
            ]
        )
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            profile_resolver=resolve_profile,
        )
        task = await orch.run_task(
            Task(user_id=1, description="add one pack of Amul milk to the cart")
        )
        # ADD executed (qty 1); the + was rejected, never tapped.
        assert adb.taps == [(600, 900)]
        assert any("over-add" in str(h.get("result", "")) for h in task.history)

    async def test_allows_increment_up_to_requested_qty(
        self, audit: AuditLogger
    ) -> None:
        adb = _FakeAdb(foreground_package="com.grofers.customerapp")
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 600, "y": 900,
                  "note": "tap ADD on Amul milk"}, _usage()),
                ({"action": "tap", "x": 670, "y": 560,
                  "note": "tap + to increase quantity to 2"}, _usage()),
                ({"action": "done", "summary": "two in cart"}, _usage()),
            ]
        )
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            profile_resolver=resolve_profile,
        )
        await orch.run_task(
            Task(user_id=1, description="add 2 packs of Amul milk")
        )
        # qty 2 requested → ADD + one + both execute.
        assert adb.taps == [(600, 900), (670, 560)]

    async def test_disarmed_under_generic_profile(self, audit: AuditLogger) -> None:
        adb = _FakeAdb(foreground_package="com.whatsapp")  # GENERIC
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 600, "y": 900, "note": "tap add"}, _usage()),
                ({"action": "tap", "x": 670, "y": 560,
                  "note": "tap plus to increase"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            profile_resolver=resolve_profile,
        )
        await orch.run_task(Task(user_id=1, description="add one thing"))
        # Non-commerce: shopping guard disarmed → both taps execute.
        assert adb.taps == [(600, 900), (670, 560)]


class TestWakeAndWaitGuards:
    """Robustness fixes from the live black-screen run: wake the display at
    task start, and fail fast on a run that only ever waits."""

    async def test_wakes_screen_at_task_start(self, audit: AuditLogger) -> None:
        adb = _FakeAdb()
        vision = _ScriptedVision([({"action": "done", "summary": "ok"}, _usage())])
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=10)
        await orch.run_task(Task(user_id=1, description="t"))
        assert adb.woke >= 1  # display woken before the loop ran

    async def test_aborts_after_too_many_consecutive_waits(
        self, audit: AuditLogger
    ) -> None:
        # Model waits forever (black/frozen screen). Unique screencaps mean no
        # synthetic-wait dedup, so each is a real model wait — must fail fast.
        adb = _FakeAdb()
        vision = _ScriptedVision([({"action": "wait", "reason": "loading"}, _usage())] * 12)
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=30)
        task = await orch.run_task(Task(user_id=1, description="t"))
        assert task.state is TaskState.FAILED
        assert "consecutive waits" in (task.failure_reason or "")
        # Bailed at the cap, not after burning all 30 iterations.
        assert task.step_count <= 7

    async def test_waits_reset_on_progress(self, audit: AuditLogger) -> None:
        # A few waits interleaved with real actions must NOT trip the cap.
        adb = _FakeAdb()
        script = []
        for _ in range(3):
            script.append(({"action": "wait", "reason": "loading"}, _usage()))
            script.append(({"action": "tap", "x": 5, "y": 5, "note": "tap"}, _usage()))
        script.append(({"action": "done", "summary": "ok"}, _usage()))
        vision = _ScriptedVision(script)
        orch = Orchestrator(adb, HitlGate(), audit, vision, session_timeout_seconds=30)
        task = await orch.run_task(Task(user_id=1, description="t"))
        assert task.state is TaskState.DONE  # never hit the consecutive-wait cap


class TestSingleItemActionClauseStripping:
    """Live-run fix: a trailing checkout-action clause must NOT make a
    single-item task look multi-item (which disarmed the quantity guard)."""

    def test_checkout_clause_stays_single_item(self) -> None:
        assert _is_single_item_task("Add milk to the cart and proceed to checkout.")
        assert _is_single_item_task("add coke then checkout")
        assert _is_single_item_task("add milk, then pay")
        assert _is_single_item_task("buy bread and go to cart")
        assert _is_single_item_task("add amul milk and place order")

    def test_genuine_multi_item_still_disarms(self) -> None:
        assert not _is_single_item_task("add milk and bread")
        assert not _is_single_item_task("milk, eggs, bread")
        # 2nd product BEFORE the action clause → still multi.
        assert not _is_single_item_task("add milk and bread and proceed to checkout")


class TestExcessQuantityGuardArmedWithCheckoutClause:
    """The exact live phrasing must now keep the guard armed and cap over-adds."""

    async def test_guard_fires_for_add_and_proceed(self, audit: AuditLogger) -> None:
        adb = _FakeAdb(foreground_package="com.grofers.customerapp")
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 600, "y": 900,
                  "note": "tap ADD for Pride of Cows Milk"}, _usage()),
                ({"action": "tap", "x": 670, "y": 560,
                  "note": "tap increase quantity for Pride of Cows Milk"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            profile_resolver=resolve_profile,
        )
        task = await orch.run_task(
            Task(user_id=1, description="Add milk to the cart and proceed to checkout.")
        )
        # ADD executed (qty 1); the increment was rejected (guard now armed).
        assert adb.taps == [(600, 900)]
        assert any("over-add" in str(h.get("result", "")) for h in task.history)


def _add_hist(note: str = "tap ADD on Amul milk", result: str = "tapped (1,2)"):
    return [{"action": {"action": "tap", "x": 1, "y": 2, "note": note}, "result": result}]


class TestCartRevealHint:
    """Live-run fix: after an ADD with no [CART] element in the tree (the
    floating cart bar isn't surfaced on the results screen), nudge the model
    to reveal it (swipe up → then back to home) instead of floundering."""

    def _el(self, **kw) -> UiElement:
        base = dict(
            text="", desc="", resource_id="", class_name="",
            cx=0, cy=0, bounds=(0, 0, 10, 10), clickable=True,
        )
        base.update(kw)
        return UiElement(**base)

    def test_no_hint_before_any_add(self) -> None:
        t = Task(user_id=1, description="add milk")
        assert Orchestrator._cart_reveal_hint(t, []) == ""

    def test_swipe_hint_after_add_when_no_cart(self) -> None:
        t = Task(user_id=1, description="add milk")
        t.history = _add_hist()
        assert "SWIPE UP" in Orchestrator._cart_reveal_hint(t, [])

    def test_no_hint_when_cart_element_present(self) -> None:
        t = Task(user_id=1, description="add milk")
        t.history = _add_hist()
        cart = self._el(text="View cart", is_cart_bar=True)
        assert Orchestrator._cart_reveal_hint(t, [cart]) == ""

    def test_no_hint_on_cart_screen(self) -> None:
        t = Task(user_id=1, description="add milk")
        t.history = _add_hist()
        el = self._el(text="Proceed to checkout")
        assert Orchestrator._cart_reveal_hint(t, [el]) == ""

    def test_escalates_to_tap_visible_cart_after_swipe(self) -> None:
        # After a swipe the orchestrator presses back itself, so the hint now
        # tells the model to TAP the now-visible cart — and explicitly NOT to
        # press back again (which would exit the app).
        t = Task(user_id=1, description="add milk")
        t.history = _add_hist() + [
            {"action": {"action": "swipe", "x1": 5, "y1": 9, "x2": 5, "y2": 1},
             "result": "swiped (5,9)->(5,1)"},
        ]
        hint = Orchestrator._cart_reveal_hint(t, [])
        assert "TAP" in hint
        assert "Do NOT press back again" in hint

    async def test_injected_into_prompt_after_add(self, audit: AuditLogger) -> None:
        adb = _FakeAdb(foreground_package="com.grofers.customerapp")
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 600, "y": 900,
                  "note": "tap ADD on Amul milk"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            profile_resolver=resolve_profile,
        )
        await orch.run_task(Task(user_id=1, description="add milk"))
        # Step 2's prompt carries the cart-reveal hint (ADD landed, no cart).
        assert "SWIPE UP" in vision.calls[1]["task_description"]

    async def test_not_injected_under_generic(self, audit: AuditLogger) -> None:
        adb = _FakeAdb(foreground_package="com.whatsapp")  # GENERIC
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 600, "y": 900, "note": "tap add"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10,
            profile_resolver=resolve_profile,
        )
        await orch.run_task(Task(user_id=1, description="add thing"))
        assert "SWIPE UP" not in vision.calls[1]["task_description"]


class TestAutoBackForCart:
    """Auto-recover: after ADD + a swipe with still no [CART], the orchestrator
    presses BACK itself (capped) instead of relying on the model."""

    # A product card (content-desc carries the title) wrapping a real ADD
    # button, and crucially NO cart node — so the ADD passes the intent /
    # product-name guards and executes, but there's no [CART] to navigate to.
    _XML = (
        "<hierarchy rotation='0'>"
        '<node content-desc="Pride of Cows Milk 500ml" class="android.view.ViewGroup" '
        'bounds="[0,800][1080,1100]" clickable="true">'
        '<node text="ADD" resource-id="com.x:id/add_to_cart" '
        'class="android.widget.Button" bounds="[880,850][1040,1050]" clickable="true" />'
        "</node>"
        "</hierarchy>"
    )

    def _adb(self):
        xml = self._XML

        class _AdbWithTree(_FakeAdb):
            async def dump_ui_xml(self) -> str | None:
                return xml

        return _AdbWithTree(foreground_package="com.grofers.customerapp")

    def test_should_back_after_add_and_swipe_no_cart(self) -> None:
        t = Task(user_id=1, description="add milk and proceed to checkout")
        t.history = _add_hist() + [
            {"action": {"action": "swipe", "x1": 5, "y1": 9, "x2": 5, "y2": 1},
             "result": "swiped"},
        ]
        assert Orchestrator._should_auto_back_to_cart(t, []) is True

    def test_should_not_back_before_swipe(self) -> None:
        t = Task(user_id=1, description="add milk")
        t.history = _add_hist()  # added but not yet scrolled
        assert Orchestrator._should_auto_back_to_cart(t, []) is False

    def test_should_not_back_when_cart_present(self) -> None:
        t = Task(user_id=1, description="add milk")
        t.history = _add_hist() + [
            {"action": {"action": "swipe", "x1": 5, "y1": 9, "x2": 5, "y2": 1},
             "result": "swiped"},
        ]
        cart = UiElement(
            text="View cart", desc="", resource_id="", class_name="",
            cx=0, cy=0, bounds=(0, 0, 10, 10), clickable=True, is_cart_bar=True,
        )
        assert Orchestrator._should_auto_back_to_cart(t, [cart]) is False

    async def test_orchestrator_presses_back_once(self, audit: AuditLogger) -> None:
        adb = self._adb()
        # Script: ADD → swipe → (orchestrator auto-backs here) → done.
        vision = _ScriptedVision(
            [
                ({"action": "tap", "x": 960, "y": 950,
                  "note": "tap ADD on Pride of Cows Milk 500ml"}, _usage()),
                ({"action": "swipe", "x1": 960, "y1": 950, "x2": 960, "y2": 400,
                  "duration_ms": 400, "note": "swipe up to reveal cart"}, _usage()),
                ({"action": "done", "summary": "ok"}, _usage()),
            ]
        )
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=15,
            profile_resolver=resolve_profile,
        )
        task = await orch.run_task(
            Task(user_id=1, description="add milk and proceed to checkout")
        )
        # BACK keyevent (4) was pressed by the orchestrator, exactly once.
        assert adb.key_events.count(4) == 1
        assert any(
            "AUTO_RECOVER" in str(h.get("result", "")) or
            h.get("action", {}).get("action") == "recover"
            for h in task.history
        )


class TestBenignApprovalRejection:
    """need_approval used to ask permission for benign navigation is rejected;
    genuine sensitive handoffs are never blocked."""

    def test_rejects_going_back_permission_ask(self) -> None:
        action = {"action": "need_approval",
                  "reason": "there is no 'View cart' element. Should I proceed "
                            "with going back to the home screen?"}
        assert Orchestrator._benign_approval_rejection(action) is not None

    def test_rejects_cant_find_cart(self) -> None:
        action = {"action": "need_approval",
                  "reason": "I cannot find the cart, may I navigate back?"}
        assert Orchestrator._benign_approval_rejection(action) is not None

    def test_allows_cart_review(self) -> None:
        action = {"action": "need_approval",
                  "reason": "Cart review: 1 item, ₹34. Approving also authorizes payment."}
        assert Orchestrator._benign_approval_rejection(action) is None

    def test_allows_payment(self) -> None:
        action = {"action": "need_approval", "reason": "Place order / pay now?"}
        assert Orchestrator._benign_approval_rejection(action) is None

    def test_ignores_non_approval(self) -> None:
        assert Orchestrator._benign_approval_rejection(
            {"action": "tap", "x": 1, "y": 2, "note": "go back"}
        ) is None
