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
            await db.commit()

    async def insert_task(self, task: Task) -> int:
        """Insert the initial row for a task. Returns the new row id."""
        started = task.start_time.isoformat()
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                INSERT INTO tasks (user_id, description, state, started_at, step_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task.user_id, task.description, task.state.value, started, task.step_count),
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

    async def list_recent(self, user_id: int, limit: int = 10) -> list[TaskRow]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, user_id, description, state, started_at, ended_at,
                       final_summary, failure_reason, step_count,
                       total_input_tokens, total_output_tokens
                FROM tasks
                WHERE user_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [TaskRow(**dict(r)) for r in rows]

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
