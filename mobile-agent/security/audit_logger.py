"""Structured JSON log of every action taken."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}")
_MAX_CONTENT_CHARS = 50
_CONTENT_FIELDS: tuple[str, ...] = ("text", "reason", "summary")


def _redact(line: str) -> str:
    line = _ANTHROPIC_KEY_RE.sub("[REDACTED:ANTHROPIC_KEY]", line)
    line = _TELEGRAM_TOKEN_RE.sub("[REDACTED:TELEGRAM_TOKEN]", line)
    return line


def _truncate(value: object) -> object:
    if isinstance(value, str) and len(value) > _MAX_CONTENT_CHARS:
        return value[:_MAX_CONTENT_CHARS] + "...[truncated]"
    return value


def _scrub_action(action: dict) -> dict:
    return {
        k: (_truncate(v) if k in _CONTENT_FIELDS else v)
        for k, v in action.items()
    }


class AuditLogger:
    """JSON-lines audit log with daily rotation and key/content redaction."""

    def __init__(self, log_dir: str | Path = "./logs", backup_count: int = 30) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        handler = TimedRotatingFileHandler(
            filename=str(self._log_dir / "audit.log"),
            when="midnight",
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
            utc=True,
        )
        handler.suffix = "%Y-%m-%d"
        handler.setFormatter(logging.Formatter("%(message)s"))

        self._logger = logging.getLogger("mobile_agent.audit")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        # Idempotent: only attach our handler once even if instantiated multiple times.
        if not any(
            isinstance(h, TimedRotatingFileHandler)
            for h in self._logger.handlers
        ):
            self._logger.addHandler(handler)

    def log_action(
        self,
        user_id: int,
        task: str,
        action: dict,
        result: str,
        timestamp: datetime | None = None,
    ) -> None:
        ts = (timestamp or datetime.now(timezone.utc)).isoformat()
        record = {
            "timestamp": ts,
            "user_id": user_id,
            "task": _truncate(task),
            "action": _scrub_action(action) if isinstance(action, dict) else action,
            "result": result,
        }
        line = json.dumps(record, default=str, ensure_ascii=False)
        self._logger.info(_redact(line))
