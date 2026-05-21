"""Persistent user store backing the pairing-auth flow.

`UserStore` owns the JSON file at `<config_dir>/users.json`. Writes go through
a tmp-file + os.replace dance so a crash mid-write never leaves a half-written
file. The orchestrator and handlers read through `get()` / `is_allowed()`;
admin commands mutate via `add()` / `remove()`.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable


class UserPolicy(str, Enum):
    """How aggressively HitlGate should pause for this user."""

    ALWAYS_APPROVE = "always_approve"   # auto-approve everything (admin)
    CONFIRM_SENSITIVE = "confirm_sensitive"  # default — payment/OTP/etc.
    READ_ONLY = "read_only"            # blocks any tap/type/swipe entirely


@dataclass(frozen=True)
class UserRecord:
    user_id: int
    name: str
    policy: UserPolicy
    paired_at: str  # ISO8601 UTC

    @classmethod
    def new(
        cls,
        user_id: int,
        name: str,
        policy: UserPolicy = UserPolicy.CONFIRM_SENSITIVE,
    ) -> "UserRecord":
        return cls(
            user_id=user_id,
            name=name,
            policy=policy,
            paired_at=datetime.now(timezone.utc).isoformat(),
        )

    def to_json(self) -> dict:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "policy": self.policy.value,
            "paired_at": self.paired_at,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "UserRecord":
        return cls(
            user_id=int(raw["user_id"]),
            name=str(raw.get("name", "")),
            policy=UserPolicy(raw.get("policy", UserPolicy.CONFIRM_SENSITIVE.value)),
            paired_at=str(raw.get("paired_at", "")),
        )


class UserStore:
    """JSON-backed user registry. All mutations are atomic and async-locked."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._cache: dict[int, UserRecord] = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[int, UserRecord]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        records: dict[int, UserRecord] = {}
        for item in raw.get("users", []):
            try:
                rec = UserRecord.from_json(item)
            except (KeyError, ValueError):
                continue
            records[rec.user_id] = rec
        return records

    def _atomic_write(self) -> None:
        # Same dir as target so os.replace is atomic on every supported FS.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".users-", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                payload = {"users": [r.to_json() for r in self._cache.values()]}
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, user_id: int) -> UserRecord | None:
        return self._cache.get(user_id)

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self._cache

    def policy_for(self, user_id: int) -> UserPolicy:
        rec = self._cache.get(user_id)
        return rec.policy if rec else UserPolicy.CONFIRM_SENSITIVE

    def list_all(self) -> list[UserRecord]:
        return sorted(self._cache.values(), key=lambda r: r.paired_at)

    async def add(self, record: UserRecord) -> None:
        async with self._lock:
            self._cache[record.user_id] = record
            self._atomic_write()

    async def remove(self, user_id: int) -> bool:
        async with self._lock:
            if user_id not in self._cache:
                return False
            del self._cache[user_id]
            self._atomic_write()
            return True

    async def seed_from_env(
        self, user_ids: Iterable[int], *, policy: UserPolicy = UserPolicy.CONFIRM_SENSITIVE
    ) -> int:
        """One-time migration from TELEGRAM_ALLOWED_USER_IDS.

        Returns the number of records inserted. Existing users are left alone.
        Caller is responsible for only invoking this when the cache is empty.
        """
        async with self._lock:
            inserted = 0
            for uid in user_ids:
                if uid in self._cache:
                    continue
                self._cache[uid] = UserRecord.new(
                    user_id=uid, name=f"migrated:{uid}", policy=policy
                )
                inserted += 1
            if inserted:
                self._atomic_write()
            return inserted
