"""Unit tests for SessionStore."""
from __future__ import annotations

from bot.session import Session, SessionState, SessionStore


class TestSessionStore:
    def test_fresh_user_starts_idle(self) -> None:
        s = SessionStore()
        sess = s.get(42)
        assert isinstance(sess, Session)
        assert sess.state is SessionState.IDLE
        assert sess.app_id is None
        assert sess.task_id is None

    def test_get_returns_same_instance(self) -> None:
        s = SessionStore()
        a = s.get(42)
        a.state = SessionState.CHOOSING_TASK
        a.app_id = "blinkit"
        b = s.get(42)
        assert b is a
        assert b.app_id == "blinkit"

    def test_reset_clears_state(self) -> None:
        s = SessionStore()
        sess = s.get(42)
        sess.state = SessionState.AWAITING_PARAM
        sess.app_id = "blinkit"
        s.reset(42)
        # Re-getting yields a fresh session, not the old one.
        new = s.get(42)
        assert new.state is SessionState.IDLE
        assert new.app_id is None

    def test_different_users_isolated(self) -> None:
        s = SessionStore()
        a = s.get(1)
        b = s.get(2)
        a.app_id = "blinkit"
        assert b.app_id is None
