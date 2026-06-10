"""Per-user conversational state for the menu flow.

In-memory only — if the bot restarts, users just `/start` again. The actual
task records persist in SQLite (Phase 4), this only holds the menu navigation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SessionState(str, Enum):
    IDLE = "idle"
    CHOOSING_APP = "choosing_app"
    CHOOSING_TASK = "choosing_task"
    AWAITING_PARAM = "awaiting_param"
    AWAITING_SAVE_NAME = "awaiting_save_name"  # naming a "save as quick task"
    RUNNING = "running"


@dataclass
class Session:
    state: SessionState = SessionState.IDLE
    app_id: str | None = None
    task_id: str | None = None
    # Last bot-sent menu message id, used so `Back` can edit-in-place rather
    # than spawn fresh messages.
    menu_message_id: int | None = None


class SessionStore:
    """Process-local map of user_id → Session."""

    def __init__(self) -> None:
        self._sessions: dict[int, Session] = {}

    def get(self, user_id: int) -> Session:
        sess = self._sessions.get(user_id)
        if sess is None:
            sess = Session()
            self._sessions[user_id] = sess
        return sess

    def reset(self, user_id: int) -> None:
        self._sessions[user_id] = Session()
