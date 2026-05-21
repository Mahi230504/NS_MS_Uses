"""Unit tests for TaskRepository: round-trip, recovery, listing."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.persistence import TaskRepository
from agent.state_machine import Task, TaskState


@pytest.fixture
async def repo(tmp_path: Path) -> TaskRepository:
    r = TaskRepository(tmp_path / "tasks.db")
    await r.initialize()
    return r


class TestInsertUpdate:
    async def test_insert_returns_id_and_persists(self, repo: TaskRepository) -> None:
        task = Task(user_id=1, description="find shoes")
        tid = await repo.insert_task(task)
        assert tid >= 1

        rows = await repo.list_recent(user_id=1)
        assert len(rows) == 1
        assert rows[0].description == "find shoes"
        assert rows[0].state == TaskState.IDLE.value
        assert rows[0].ended_at is None

    async def test_update_state_sets_terminal_ended_at(self, repo: TaskRepository) -> None:
        task = Task(user_id=1, description="t")
        tid = await repo.insert_task(task)

        task.state = TaskState.DONE
        task.final_summary = "all done"
        task.total_input_tokens = 100
        task.total_output_tokens = 20
        task.step_count = 3
        await repo.update_state(tid, task)

        rows = await repo.list_recent(user_id=1)
        assert rows[0].state == TaskState.DONE.value
        assert rows[0].ended_at is not None
        assert rows[0].final_summary == "all done"
        assert rows[0].total_input_tokens == 100
        assert rows[0].step_count == 3

    async def test_update_state_non_terminal_keeps_ended_at_null(
        self, repo: TaskRepository
    ) -> None:
        task = Task(user_id=1, description="t")
        tid = await repo.insert_task(task)

        task.state = TaskState.RUNNING
        await repo.update_state(tid, task)

        rows = await repo.list_recent(user_id=1)
        assert rows[0].ended_at is None

    async def test_failure_reason_preserved_through_recovery(
        self, repo: TaskRepository
    ) -> None:
        task = Task(user_id=1, description="t")
        tid = await repo.insert_task(task)

        task.state = TaskState.FAILED
        task.failure_reason = "user denied approval"
        await repo.update_state(tid, task)

        # Recovery must not clobber an existing failure_reason.
        await repo.recover_orphans()

        rows = await repo.list_recent(user_id=1)
        assert rows[0].failure_reason == "user denied approval"


class TestSteps:
    async def test_append_step_persists(self, repo: TaskRepository) -> None:
        task = Task(user_id=1, description="t")
        tid = await repo.insert_task(task)

        await repo.append_step(tid, 1, {"action": "tap", "x": 1, "y": 2}, "tapped (1,2)")
        await repo.append_step(tid, 2, {"action": "done", "summary": "ok"}, "done")

        # Direct query — TaskRepository doesn't expose steps publicly; verify
        # by counting rows in the steps table.
        import aiosqlite

        async with aiosqlite.connect(repo.path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM steps WHERE task_id = ?", (tid,)
            )
            (count,) = await cursor.fetchone()
            assert count == 2


class TestRecoverOrphans:
    async def test_running_rows_become_failed(self, repo: TaskRepository) -> None:
        task1 = Task(user_id=1, description="alive")
        tid1 = await repo.insert_task(task1)
        task1.state = TaskState.RUNNING
        await repo.update_state(tid1, task1)

        task2 = Task(user_id=2, description="finished")
        tid2 = await repo.insert_task(task2)
        task2.state = TaskState.DONE
        task2.final_summary = "ok"
        await repo.update_state(tid2, task2)

        # Simulate a crash + restart.
        n = await repo.recover_orphans()
        assert n == 1

        rows1 = await repo.list_recent(user_id=1)
        assert rows1[0].state == TaskState.FAILED.value
        assert rows1[0].failure_reason == "bot restarted while running"
        assert rows1[0].ended_at is not None

        rows2 = await repo.list_recent(user_id=2)
        assert rows2[0].state == TaskState.DONE.value  # untouched

    async def test_recover_orphans_is_idempotent(self, repo: TaskRepository) -> None:
        task = Task(user_id=1, description="alive")
        tid = await repo.insert_task(task)
        task.state = TaskState.RUNNING
        await repo.update_state(tid, task)

        assert await repo.recover_orphans() == 1
        assert await repo.recover_orphans() == 0  # already terminal


class TestListRecent:
    async def test_ordered_by_started_at_desc(self, repo: TaskRepository) -> None:
        from datetime import datetime, timedelta, timezone

        # Insert tasks with explicit, increasing start times by mutating the
        # Task dataclass before inserting.
        for i in range(5):
            t = Task(user_id=1, description=f"task {i}")
            t.start_time = datetime.now(timezone.utc) - timedelta(seconds=10 - i)
            await repo.insert_task(t)

        rows = await repo.list_recent(user_id=1, limit=3)
        assert [r.description for r in rows] == ["task 4", "task 3", "task 2"]

    async def test_filters_by_user_id(self, repo: TaskRepository) -> None:
        await repo.insert_task(Task(user_id=1, description="a"))
        await repo.insert_task(Task(user_id=2, description="b"))
        rows = await repo.list_recent(user_id=1)
        assert [r.description for r in rows] == ["a"]

    async def test_empty_user_returns_empty(self, repo: TaskRepository) -> None:
        assert await repo.list_recent(user_id=999) == []


class TestDurationSeconds:
    async def test_terminal_row_reports_duration(self, repo: TaskRepository) -> None:
        from datetime import datetime, timedelta, timezone

        task = Task(user_id=1, description="t")
        # Start a minute ago so update_state's ended_at (real wall clock) is
        # strictly later → positive duration.
        task.start_time = datetime.now(timezone.utc) - timedelta(seconds=60)
        tid = await repo.insert_task(task)

        task.state = TaskState.DONE
        await repo.update_state(tid, task)

        rows = await repo.list_recent(user_id=1)
        dur = rows[0].duration_seconds()
        assert dur is not None and dur >= 0

    async def test_in_flight_row_has_no_duration(self, repo: TaskRepository) -> None:
        await repo.insert_task(Task(user_id=1, description="t"))
        rows = await repo.list_recent(user_id=1)
        assert rows[0].duration_seconds() is None
