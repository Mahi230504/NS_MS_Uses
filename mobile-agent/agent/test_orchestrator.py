"""Unit tests for the orchestrator loop: usage accumulation + step status throttle."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent.orchestrator import Orchestrator
from agent.persistence import TaskRepository
from agent.providers.base import ProviderResponse, RequestUsage
from agent.state_machine import Task, TaskState
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
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10, skills=skills
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
        orch = Orchestrator(
            adb, HitlGate(), audit, vision, session_timeout_seconds=10, skills=skills
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


class TestIntentMismatch:
    """The orchestrator must reject taps whose `note` describes one element
    type while the coord lands on a different type. The exact scenario from
    the Blinkit run-3 log: model tapped (517, 200) labelled 'tap search bar'
    but (517, 200) is the [LOCATION] delivery header on Blinkit's home."""

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
