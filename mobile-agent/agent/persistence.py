"""SQLite-backed persistence for tasks and per-step history.

Canonical record of what the agent did. Lives alongside the JSON audit log
(`AuditLogger`), which captures every action including blocked/denied ones;
this DB only tracks the task lifecycle the user can /history.

Schema is created on first connect; safe to point at an existing DB.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from agent.state_machine import Task, TaskState


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    final_summary TEXT,
    failure_reason TEXT,
    step_count INTEGER NOT NULL DEFAULT 0,
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_user_started
    ON tasks (user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    action_json TEXT NOT NULL,
    result TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_steps_task ON steps (task_id, idx);

CREATE TABLE IF NOT EXISTS comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    query TEXT NOT NULL,
    category TEXT NOT NULL,
    ranking_key TEXT NOT NULL,
    quotes_json TEXT NOT NULL,
    winner_app_id TEXT,
    chosen_app_id TEXT,
    created_at TEXT NOT NULL,
    ordered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_comparisons_user_created
    ON comparisons (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS saved_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    label TEXT NOT NULL,
    app_id TEXT,
    task_id TEXT,
    param TEXT,
    raw_description TEXT NOT NULL,
    launch_package TEXT,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    run_count INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_user_slug
    ON saved_tasks (user_id, slug);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    freq TEXT NOT NULL,             -- once | daily | weekly | monthly
    at_minute INTEGER NOT NULL,     -- local minutes since midnight (0..1439)
    weekday INTEGER,                -- 0=Mon..6=Sun (weekly)
    day_of_month INTEGER,           -- 1..31 (monthly)
    tz TEXT NOT NULL,               -- IANA tz the recurrence is expressed in
    app_id TEXT,
    task_id TEXT,
    param TEXT,
    raw_description TEXT NOT NULL,
    launch_package TEXT,
    pay_automatically INTEGER NOT NULL DEFAULT 0,
    next_run_at TEXT NOT NULL,      -- absolute UTC ISO timestamp
    last_run_at TEXT,
    last_state TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sched_due
    ON schedules (enabled, next_run_at);
"""

# Terminal states — used to flag what counts as a "still-running" orphan at
# startup. Anything not in this set is treated as alive and needs reconciling.
_TERMINAL_STATES: frozenset[str] = frozenset(
    {
        TaskState.DONE.value,
        TaskState.FAILED.value,
        TaskState.TIMED_OUT.value,
    }
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TaskRow:
    id: int
    user_id: int
    description: str
    state: str
    started_at: str
    ended_at: str | None
    final_summary: str | None
    failure_reason: str | None
    step_count: int
    total_input_tokens: int
    total_output_tokens: int
    launch_package: str | None = None
    artifact_dir: str | None = None

    def duration_seconds(self) -> float | None:
        if self.ended_at is None:
            return None
        try:
            start = datetime.fromisoformat(self.started_at)
            end = datetime.fromisoformat(self.ended_at)
        except ValueError:
            return None
        return (end - start).total_seconds()


@dataclass(frozen=True)
class ComparisonRow:
    id: int
    user_id: int
    query: str
    category: str
    ranking_key: str
    quotes_json: str
    winner_app_id: str | None
    chosen_app_id: str | None
    created_at: str
    ordered_at: str | None

    def quotes(self) -> list[dict]:
        """Parsed per-app quotes; [] if the JSON is somehow malformed."""
        try:
            data = json.loads(self.quotes_json)
        except (json.JSONDecodeError, TypeError):
            return []
        return data if isinstance(data, list) else []


@dataclass(frozen=True)
class SavedTaskRow:
    id: int
    user_id: int
    slug: str
    label: str
    app_id: str | None
    task_id: str | None
    param: str | None
    raw_description: str
    launch_package: str | None
    created_at: str
    last_run_at: str | None
    run_count: int


@dataclass(frozen=True)
class ScheduleRow:
    id: int
    user_id: int
    name: str
    freq: str
    at_minute: int
    weekday: int | None
    day_of_month: int | None
    tz: str
    app_id: str | None
    task_id: str | None
    param: str | None
    raw_description: str
    launch_package: str | None
    pay_automatically: int
    next_run_at: str
    last_run_at: str | None
    last_state: str | None
    enabled: int
    created_at: str


class TaskRepository:
    """Async DAO. One instance per process; connections are short-lived."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    @property
    def path(self) -> Path:
        return self._path

    async def initialize(self) -> None:
        """Create schema if missing. Safe to call repeatedly."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(_SCHEMA)
            # Idempotent column adds for an existing tasks table (CREATE TABLE
            # IF NOT EXISTS won't alter one that already exists). Used by the
            # dashboard to attribute a task to its app and find its screenshots.
            cursor = await db.execute("PRAGMA table_info(tasks)")
            existing = {row[1] for row in await cursor.fetchall()}
            for col in ("launch_package", "artifact_dir"):
                if col not in existing:
                    await db.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
            await db.commit()

    async def insert_task(self, task: Task) -> int:
        """Insert the initial row for a task. Returns the new row id."""
        started = task.start_time.isoformat()
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                INSERT INTO tasks (user_id, description, state, started_at,
                                   step_count, launch_package, artifact_dir)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.user_id, task.description, task.state.value, started,
                    task.step_count, task.launch_package, task.artifact_dir,
                ),
            )
            await db.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    async def update_state(self, task_id: int, task: Task) -> None:
        """Sync the row to the task's current fields."""
        is_terminal = task.state.value in _TERMINAL_STATES
        ended_at = _utcnow() if is_terminal else None
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE tasks
                SET state = ?,
                    ended_at = COALESCE(?, ended_at),
                    final_summary = ?,
                    failure_reason = ?,
                    step_count = ?,
                    total_input_tokens = ?,
                    total_output_tokens = ?
                WHERE id = ?
                """,
                (
                    task.state.value,
                    ended_at,
                    task.final_summary,
                    task.failure_reason,
                    task.step_count,
                    task.total_input_tokens,
                    task.total_output_tokens,
                    task_id,
                ),
            )
            await db.commit()

    async def append_step(
        self, task_id: int, idx: int, action: dict, result: str
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO steps (task_id, idx, action_json, result, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    idx,
                    json.dumps(action, default=str),
                    result,
                    _utcnow(),
                ),
            )
            await db.commit()

    _TASK_COLS = (
        "id, user_id, description, state, started_at, ended_at, "
        "final_summary, failure_reason, step_count, total_input_tokens, "
        "total_output_tokens, launch_package, artifact_dir"
    )

    async def list_recent(self, user_id: int, limit: int = 10) -> list[TaskRow]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT {self._TASK_COLS}
                FROM tasks
                WHERE user_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [TaskRow(**dict(r)) for r in rows]

    async def list_paged(
        self,
        user_id: int,
        *,
        launch_package: str | None = None,
        state: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[TaskRow]:
        """History for the dashboard, optionally filtered by app/state."""
        clauses = ["user_id = ?"]
        params: list = [user_id]
        if launch_package:
            clauses.append("launch_package = ?")
            params.append(launch_package)
        if state:
            clauses.append("state = ?")
            params.append(state)
        params.extend([limit, offset])
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT {self._TASK_COLS}
                FROM tasks
                WHERE {' AND '.join(clauses)}
                ORDER BY started_at DESC
                LIMIT ? OFFSET ?
                """,
                params,
            )
            return [TaskRow(**dict(r)) for r in await cursor.fetchall()]

    async def get_task_row(self, task_id: int) -> TaskRow | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT {self._TASK_COLS} FROM tasks WHERE id = ?", (task_id,)
            )
            row = await cursor.fetchone()
            return TaskRow(**dict(row)) if row else None

    async def list_steps(self, task_id: int) -> list[dict]:
        """All persisted steps for a task, oldest-first (for replay)."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT idx, action_json, result, timestamp
                FROM steps WHERE task_id = ? ORDER BY idx ASC
                """,
                (task_id,),
            )
            out: list[dict] = []
            for r in await cursor.fetchall():
                try:
                    action = json.loads(r["action_json"])
                except (json.JSONDecodeError, TypeError):
                    action = {}
                out.append(
                    {
                        "idx": r["idx"],
                        "action": action,
                        "result": r["result"],
                        "timestamp": r["timestamp"],
                    }
                )
            return out

    async def app_usage(self, user_id: int) -> list[dict]:
        """Aggregate per-app usage for the 'preferred apps' analytics view."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT launch_package,
                       COUNT(*) AS run_count,
                       SUM(CASE WHEN state = 'done' THEN 1 ELSE 0 END) AS done_count,
                       MAX(started_at) AS last_used_at,
                       AVG(step_count) AS avg_steps
                FROM tasks
                WHERE user_id = ?
                GROUP BY launch_package
                ORDER BY run_count DESC
                """,
                (user_id,),
            )
            return [dict(r) for r in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # Cross-app comparison records (feature #6)

    async def insert_comparison(
        self,
        *,
        user_id: int,
        query: str,
        category: str,
        ranking_key: str,
        quotes: list[dict],
        winner_app_id: str | None,
    ) -> int:
        """Persist a completed comparison; returns the new row id."""
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                INSERT INTO comparisons
                    (user_id, query, category, ranking_key, quotes_json,
                     winner_app_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    query,
                    category,
                    ranking_key,
                    json.dumps(quotes, default=str),
                    winner_app_id,
                    _utcnow(),
                ),
            )
            await db.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    async def set_comparison_chosen(
        self, comparison_id: int, chosen_app_id: str
    ) -> None:
        """Record which app the user chose to order from (and when)."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE comparisons
                SET chosen_app_id = ?, ordered_at = ?
                WHERE id = ?
                """,
                (chosen_app_id, _utcnow(), comparison_id),
            )
            await db.commit()

    async def list_recent_comparisons(
        self, user_id: int, limit: int = 10
    ) -> list[ComparisonRow]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, user_id, query, category, ranking_key, quotes_json,
                       winner_app_id, chosen_app_id, created_at, ordered_at
                FROM comparisons
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [ComparisonRow(**dict(r)) for r in rows]

    async def recover_orphans(self) -> int:
        """Flip any non-terminal rows to FAILED("bot restarted while running").

        Returns the number of rows reconciled. Idempotent.
        """
        marker = "bot restarted while running"
        ended = _utcnow()
        async with aiosqlite.connect(self._path) as db:
            placeholders = ",".join("?" * len(_TERMINAL_STATES))
            cursor = await db.execute(
                f"""
                UPDATE tasks
                SET state = ?,
                    ended_at = COALESCE(ended_at, ?),
                    failure_reason = COALESCE(failure_reason, ?)
                WHERE state NOT IN ({placeholders})
                """,
                (TaskState.FAILED.value, ended, marker, *sorted(_TERMINAL_STATES)),
            )
            await db.commit()
            return cursor.rowcount or 0


class SavedTaskRepository:
    """Async DAO for user-saved quick tasks (feature #4).

    Shares the same SQLite file as TaskRepository; the `saved_tasks` table is
    created by TaskRepository.initialize() via the shared _SCHEMA, so this
    class only does CRUD. Uniqueness is per (user_id, slug).
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    async def upsert(
        self,
        *,
        user_id: int,
        slug: str,
        label: str,
        raw_description: str,
        app_id: str | None = None,
        task_id: str | None = None,
        param: str | None = None,
        launch_package: str | None = None,
    ) -> None:
        """Create or replace the saved task at (user_id, slug)."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO saved_tasks
                    (user_id, slug, label, app_id, task_id, param,
                     raw_description, launch_package, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, slug) DO UPDATE SET
                    label = excluded.label,
                    app_id = excluded.app_id,
                    task_id = excluded.task_id,
                    param = excluded.param,
                    raw_description = excluded.raw_description,
                    launch_package = excluded.launch_package
                """,
                (
                    user_id, slug, label, app_id, task_id, param,
                    raw_description, launch_package, _utcnow(),
                ),
            )
            await db.commit()

    async def get(self, user_id: int, slug: str) -> SavedTaskRow | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM saved_tasks WHERE user_id = ? AND slug = ?",
                (user_id, slug),
            )
            row = await cursor.fetchone()
            return SavedTaskRow(**dict(row)) if row else None

    async def list_for(self, user_id: int) -> list[SavedTaskRow]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM saved_tasks
                WHERE user_id = ?
                ORDER BY last_run_at DESC, created_at DESC
                """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [SavedTaskRow(**dict(r)) for r in rows]

    async def delete(self, user_id: int, slug: str) -> bool:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "DELETE FROM saved_tasks WHERE user_id = ? AND slug = ?",
                (user_id, slug),
            )
            await db.commit()
            return (cursor.rowcount or 0) > 0

    async def mark_run(self, user_id: int, slug: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE saved_tasks
                SET last_run_at = ?, run_count = run_count + 1
                WHERE user_id = ? AND slug = ?
                """,
                (_utcnow(), user_id, slug),
            )
            await db.commit()


class ScheduleRepository:
    """Async DAO for recurring/scheduled tasks (feature #5).

    Shares the SQLite file with TaskRepository (schema in _SCHEMA). `next_run_at`
    is an absolute UTC ISO timestamp and is the dispatch key — the scheduler
    polls `list_due` and advances it after each fire.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    async def insert(
        self,
        *,
        user_id: int,
        name: str,
        freq: str,
        at_minute: int,
        tz: str,
        raw_description: str,
        next_run_at: str,
        weekday: int | None = None,
        day_of_month: int | None = None,
        app_id: str | None = None,
        task_id: str | None = None,
        param: str | None = None,
        launch_package: str | None = None,
        pay_automatically: bool = False,
    ) -> int:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                INSERT INTO schedules
                    (user_id, name, freq, at_minute, weekday, day_of_month, tz,
                     app_id, task_id, param, raw_description, launch_package,
                     pay_automatically, next_run_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, name, freq, at_minute, weekday, day_of_month, tz,
                    app_id, task_id, param, raw_description, launch_package,
                    1 if pay_automatically else 0, next_run_at, _utcnow(),
                ),
            )
            await db.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    async def list_for(self, user_id: int) -> list[ScheduleRow]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM schedules
                WHERE user_id = ?
                ORDER BY enabled DESC, next_run_at ASC
                """,
                (user_id,),
            )
            return [ScheduleRow(**dict(r)) for r in await cursor.fetchall()]

    async def list_due(self, now_utc_iso: str) -> list[ScheduleRow]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM schedules
                WHERE enabled = 1 AND next_run_at <= ?
                ORDER BY next_run_at ASC
                """,
                (now_utc_iso,),
            )
            return [ScheduleRow(**dict(r)) for r in await cursor.fetchall()]

    async def get(self, schedule_id: int) -> ScheduleRow | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            )
            row = await cursor.fetchone()
            return ScheduleRow(**dict(row)) if row else None

    async def delete(self, user_id: int, schedule_id: int) -> bool:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "DELETE FROM schedules WHERE user_id = ? AND id = ?",
                (user_id, schedule_id),
            )
            await db.commit()
            return (cursor.rowcount or 0) > 0

    async def advance(
        self,
        schedule_id: int,
        *,
        next_run_at: str,
        last_run_at: str,
        enabled: bool = True,
    ) -> None:
        """Move a schedule forward after it fires (or disable a one-shot)."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE schedules
                SET next_run_at = ?, last_run_at = ?, enabled = ?
                WHERE id = ?
                """,
                (next_run_at, last_run_at, 1 if enabled else 0, schedule_id),
            )
            await db.commit()

    async def set_last_state(self, schedule_id: int, state: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE schedules SET last_state = ? WHERE id = ?",
                (state, schedule_id),
            )
            await db.commit()
