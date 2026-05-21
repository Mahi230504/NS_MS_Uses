"""Unit tests for UserStore: atomic JSON persistence + seed migration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.users import UserPolicy, UserRecord, UserStore


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestUserStore:
    def test_empty_store_on_missing_file(self, tmp_path: Path) -> None:
        s = UserStore(tmp_path / "users.json")
        assert s.list_all() == []
        assert not s.is_allowed(1)
        assert s.policy_for(1) == UserPolicy.CONFIRM_SENSITIVE

    async def test_add_persists_and_reloads(self, tmp_path: Path) -> None:
        path = tmp_path / "users.json"
        s = UserStore(path)
        rec = UserRecord.new(user_id=42, name="alice", policy=UserPolicy.READ_ONLY)
        await s.add(rec)

        assert s.is_allowed(42)
        assert s.policy_for(42) == UserPolicy.READ_ONLY

        # New instance reads what the first one wrote.
        s2 = UserStore(path)
        assert s2.is_allowed(42)
        assert s2.policy_for(42) == UserPolicy.READ_ONLY
        assert s2.get(42).name == "alice"

    async def test_remove(self, tmp_path: Path) -> None:
        s = UserStore(tmp_path / "users.json")
        await s.add(UserRecord.new(user_id=1, name="a"))
        await s.add(UserRecord.new(user_id=2, name="b"))
        assert await s.remove(1) is True
        assert not s.is_allowed(1)
        assert s.is_allowed(2)
        assert await s.remove(999) is False  # not present

    async def test_atomic_write_uses_replace(self, tmp_path: Path) -> None:
        # After a successful write, only the canonical file remains — no .tmp leftovers.
        path = tmp_path / "users.json"
        s = UserStore(path)
        await s.add(UserRecord.new(user_id=1, name="a"))
        leftover = list(tmp_path.glob(".users-*.tmp"))
        assert leftover == []
        assert path.exists()

    def test_corrupt_file_is_treated_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "users.json"
        path.write_text("not json at all")
        s = UserStore(path)
        assert s.list_all() == []

    def test_unknown_policy_is_rejected(self, tmp_path: Path) -> None:
        # Records with invalid policy strings are silently skipped (don't
        # crash the bot on a hand-edited file with a typo).
        path = tmp_path / "users.json"
        path.write_text(
            json.dumps(
                {
                    "users": [
                        {"user_id": 1, "name": "ok", "policy": "confirm_sensitive", "paired_at": "now"},
                        {"user_id": 2, "name": "bad", "policy": "not_a_policy", "paired_at": "now"},
                    ]
                }
            )
        )
        s = UserStore(path)
        ids = {r.user_id for r in s.list_all()}
        assert ids == {1}

    async def test_seed_from_env_inserts_missing(self, tmp_path: Path) -> None:
        s = UserStore(tmp_path / "users.json")
        n = await s.seed_from_env([10, 20, 30])
        assert n == 3
        assert s.is_allowed(10) and s.is_allowed(20) and s.is_allowed(30)
        # Re-seeding is a no-op for already-present ids.
        n2 = await s.seed_from_env([20, 30, 40])
        assert n2 == 1  # only 40 added
        assert s.is_allowed(40)

    async def test_seed_writes_to_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "users.json"
        s = UserStore(path)
        await s.seed_from_env([5, 6])
        on_disk = _read(path)
        assert {u["user_id"] for u in on_disk["users"]} == {5, 6}
