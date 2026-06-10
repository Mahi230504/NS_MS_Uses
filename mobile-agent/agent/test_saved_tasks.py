"""Unit tests for SavedTaskRepository (feature #4 persistence)."""
from __future__ import annotations

import pytest

from agent.persistence import SavedTaskRepository, TaskRepository


async def _repo(tmp_path) -> SavedTaskRepository:
    # TaskRepository.initialize() creates all tables (shared _SCHEMA).
    base = TaskRepository(tmp_path / "t.db")
    await base.initialize()
    return SavedTaskRepository(tmp_path / "t.db")


class TestSavedTaskRepository:
    async def test_upsert_and_get(self, tmp_path) -> None:
        repo = await _repo(tmp_path)
        await repo.upsert(
            user_id=1, slug="sunday-order", label="Sunday order",
            raw_description="Add milk and bread", app_id="blinkit",
            task_id="order", param="milk and bread",
            launch_package="com.grofers.customerapp",
        )
        row = await repo.get(1, "sunday-order")
        assert row is not None
        assert row.label == "Sunday order"
        assert row.app_id == "blinkit" and row.param == "milk and bread"
        assert row.run_count == 0 and row.last_run_at is None

    async def test_get_missing_returns_none(self, tmp_path) -> None:
        repo = await _repo(tmp_path)
        assert await repo.get(1, "nope") is None

    async def test_upsert_updates_in_place(self, tmp_path) -> None:
        repo = await _repo(tmp_path)
        await repo.upsert(user_id=1, slug="x", label="First", raw_description="a")
        await repo.upsert(user_id=1, slug="x", label="Second", raw_description="b")
        rows = await repo.list_for(1)
        assert len(rows) == 1
        assert rows[0].label == "Second" and rows[0].raw_description == "b"

    async def test_mark_run_increments(self, tmp_path) -> None:
        repo = await _repo(tmp_path)
        await repo.upsert(user_id=1, slug="x", label="X", raw_description="a")
        await repo.mark_run(1, "x")
        await repo.mark_run(1, "x")
        row = await repo.get(1, "x")
        assert row.run_count == 2 and row.last_run_at is not None

    async def test_delete(self, tmp_path) -> None:
        repo = await _repo(tmp_path)
        await repo.upsert(user_id=1, slug="x", label="X", raw_description="a")
        assert await repo.delete(1, "x") is True
        assert await repo.get(1, "x") is None
        assert await repo.delete(1, "x") is False  # already gone

    async def test_list_scoped_per_user(self, tmp_path) -> None:
        repo = await _repo(tmp_path)
        await repo.upsert(user_id=1, slug="a", label="A", raw_description="a")
        await repo.upsert(user_id=2, slug="b", label="B", raw_description="b")
        assert {r.slug for r in await repo.list_for(1)} == {"a"}
        assert {r.slug for r in await repo.list_for(2)} == {"b"}

    async def test_same_slug_different_users_coexist(self, tmp_path) -> None:
        repo = await _repo(tmp_path)
        await repo.upsert(user_id=1, slug="order", label="U1", raw_description="a")
        await repo.upsert(user_id=2, slug="order", label="U2", raw_description="b")
        assert (await repo.get(1, "order")).label == "U1"
        assert (await repo.get(2, "order")).label == "U2"
