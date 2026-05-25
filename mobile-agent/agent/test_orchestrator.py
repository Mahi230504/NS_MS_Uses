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
