"""Unit tests for cloud-action persistence: schedules migration, cloud actions,
contacts (Atlas Cloud Actions)."""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from agent.persistence import (
    CloudActionRepository,
    CloudActionRow,
    ContactRepository,
    ScheduleRepository,
    TaskRepository,
)

IST = "Asia/Kolkata"

# The schedules table exactly as it shipped BEFORE action_kind/payload_json —
# used to prove initialize() upgrades a live DB in place without data loss.
_OLD_SCHEDULES_SQL = """
CREATE TABLE schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    freq TEXT NOT NULL,
    at_minute INTEGER NOT NULL,
    weekday INTEGER,
    day_of_month INTEGER,
    tz TEXT NOT NULL,
    app_id TEXT,
    task_id TEXT,
    param TEXT,
    raw_description TEXT NOT NULL,
    launch_package TEXT,
    pay_automatically INTEGER NOT NULL DEFAULT 0,
    next_run_at TEXT NOT NULL,
    last_run_at TEXT,
    last_state TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_sched_due ON schedules (enabled, next_run_at);
"""


async def _make_old_db(path: Path) -> None:
    """Hand-roll a pre-migration DB with one device schedule in it."""
    async with aiosqlite.connect(path) as db:
        await db.executescript(_OLD_SCHEDULES_SQL)
        await db.execute(
            """
            INSERT INTO schedules
                (user_id, name, freq, at_minute, tz, app_id, task_id, param,
                 raw_description, launch_package, next_run_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1, "Milk", "daily", 540, IST, "blinkit", "order", "milk",
                "order milk", "com.grofers.customerapp",
                "2026-06-10T03:30:00+00:00", "2026-06-01T00:00:00+00:00",
            ),
        )
        await db.commit()


async def _columns(path: Path, table: str) -> set[str]:
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in await cursor.fetchall()}


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await TaskRepository(path).initialize()
    return path


class TestSchemaMigration:
    async def test_old_db_gains_columns_and_old_rows_hydrate(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "old.db"
        await _make_old_db(path)
        await TaskRepository(path).initialize()

        cols = await _columns(path, "schedules")
        assert {"action_kind", "payload_json"} <= cols

        # Pre-migration row hydrates via SELECT * with the new defaults.
        rows = await ScheduleRepository(path).list_for(1)
        assert len(rows) == 1
        assert rows[0].name == "Milk"
        assert rows[0].action_kind == "device"
        assert rows[0].payload_json is None

    async def test_initialize_is_idempotent_after_migration(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "old.db"
        await _make_old_db(path)
        repo = TaskRepository(path)
        await repo.initialize()
        await repo.initialize()  # second pass must not re-ALTER
        assert len(await ScheduleRepository(path).list_for(1)) == 1

    async def test_fresh_db_has_cloud_tables_and_columns(
        self, db_path: Path
    ) -> None:
        assert {"action_kind", "payload_json"} <= await _columns(
            db_path, "schedules"
        )
        assert {"kind", "payload_json", "result_json", "status"} <= await _columns(
            db_path, "cloud_actions"
        )
        assert {"name", "email"} <= await _columns(db_path, "contacts")


class TestScheduleCloudColumns:
    async def test_cloud_schedule_round_trips(self, db_path: Path) -> None:
        repo = ScheduleRepository(db_path)
        payload = {"to": ["a@x.com"], "subject": "Standup", "body": "Notes."}
        sid = await repo.insert(
            user_id=1, name="Email: Standup", freq="once", at_minute=480,
            tz=IST, raw_description="email the team the standup notes",
            next_run_at="2026-06-12T02:30:00+00:00",
            action_kind="email", payload_json=json.dumps(payload),
        )
        row = await repo.get(sid)
        assert row is not None
        assert row.action_kind == "email"
        assert json.loads(row.payload_json) == payload

    async def test_insert_defaults_to_device_kind(self, db_path: Path) -> None:
        repo = ScheduleRepository(db_path)
        sid = await repo.insert(
            user_id=1, name="Milk", freq="daily", at_minute=540, tz=IST,
            raw_description="order milk",
            next_run_at="2026-06-10T03:30:00+00:00",
        )
        row = await repo.get(sid)
        assert row is not None
        assert row.action_kind == "device" and row.payload_json is None


class TestCloudActionRepository:
    async def test_insert_and_list_round_trip(self, db_path: Path) -> None:
        repo = CloudActionRepository(db_path)
        rid = await repo.insert(
            user_id=1, kind="email",
            payload={"to": ["a@x.com"], "subject": "Hi", "body": "Hello"},
            result={"id": "m1", "thread_id": "t1"}, status="ok",
        )
        assert rid >= 1
        rows = await repo.list_for(1)
        assert len(rows) == 1
        row = rows[0]
        assert row.kind == "email" and row.status == "ok" and row.error is None
        assert row.payload()["subject"] == "Hi"
        assert row.result()["id"] == "m1"

    async def test_error_row_keeps_reason_and_empty_result(
        self, db_path: Path
    ) -> None:
        repo = CloudActionRepository(db_path)
        await repo.insert(
            user_id=1, kind="meeting", payload={"title": "Launch"},
            result=None, status="error", error="couldn't parse meeting time",
        )
        row = (await repo.list_for(1))[0]
        assert row.status == "error"
        assert row.error == "couldn't parse meeting time"
        assert row.result_json is None and row.result() == {}

    async def test_list_filters_user_and_limits(self, db_path: Path) -> None:
        repo = CloudActionRepository(db_path)
        for i in range(3):
            await repo.insert(
                user_id=1, kind="email", payload={"subject": f"s{i}"},
                result=None, status="ok",
            )
        await repo.insert(
            user_id=2, kind="email", payload={"subject": "other"},
            result=None, status="ok",
        )
        rows = await repo.list_for(1, limit=2)
        assert len(rows) == 2
        # Newest first.
        assert rows[0].payload()["subject"] == "s2"
        assert all(r.user_id == 1 for r in rows)

    def test_malformed_json_tolerated(self) -> None:
        row = CloudActionRow(
            id=1, user_id=1, kind="email", payload_json="not json",
            result_json="[1, 2]", status="ok", error=None,
            created_at="2026-06-10T00:00:00+00:00",
        )
        assert row.payload() == {}
        assert row.result() == {}  # non-dict JSON is also rejected


class TestContactRepository:
    async def test_upsert_and_case_insensitive_get(self, db_path: Path) -> None:
        repo = ContactRepository(db_path)
        await repo.upsert(user_id=1, name="  Ayush ", email="ayush@x.com")
        row = await repo.get(1, "AYUSH")
        assert row is not None
        assert row.name == "ayush" and row.email == "ayush@x.com"

    async def test_upsert_replaces_email(self, db_path: Path) -> None:
        repo = ContactRepository(db_path)
        await repo.upsert(user_id=1, name="ayush", email="old@x.com")
        await repo.upsert(user_id=1, name="Ayush", email="new@x.com")
        rows = await repo.list_for(1)
        assert len(rows) == 1 and rows[0].email == "new@x.com"

    async def test_list_sorted_and_scoped_to_user(self, db_path: Path) -> None:
        repo = ContactRepository(db_path)
        await repo.upsert(user_id=1, name="zara", email="z@x.com")
        await repo.upsert(user_id=1, name="alice", email="a@x.com")
        await repo.upsert(user_id=2, name="bob", email="b@x.com")
        assert [r.name for r in await repo.list_for(1)] == ["alice", "zara"]

    async def test_delete(self, db_path: Path) -> None:
        repo = ContactRepository(db_path)
        await repo.upsert(user_id=1, name="ayush", email="a@x.com")
        assert await repo.delete(1, " Ayush ") is True
        assert await repo.delete(1, "ayush") is False
        assert await repo.get(1, "ayush") is None

    async def test_unknown_name_is_none(self, db_path: Path) -> None:
        assert await ContactRepository(db_path).get(1, "nobody") is None
