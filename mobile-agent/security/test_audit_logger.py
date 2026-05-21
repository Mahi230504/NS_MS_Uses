"""Unit tests for the audit logger: truncation + key redaction."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from security.audit_logger import AuditLogger


@pytest.fixture(autouse=True)
def _reset_audit_logger():
    # The logger uses logging.getLogger("mobile_agent.audit") — a process
    # singleton. Detach all handlers before and after each test so they don't
    # accumulate across instantiations within the same test process.
    name = "mobile_agent.audit"
    log = logging.getLogger(name)
    for h in list(log.handlers):
        log.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    yield
    for h in list(log.handlers):
        log.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def _read_lines(log_dir: Path) -> list[str]:
    return (log_dir / "audit.log").read_text(encoding="utf-8").strip().splitlines()


class TestAuditLogger:
    def test_writes_jsonl_record(self, tmp_path: Path) -> None:
        al = AuditLogger(tmp_path)
        al.log_action(123, "find shoes", {"action": "tap", "x": 1, "y": 2}, "ok")
        lines = _read_lines(tmp_path)
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["user_id"] == 123
        assert rec["task"] == "find shoes"
        assert rec["action"] == {"action": "tap", "x": 1, "y": 2}
        assert rec["result"] == "ok"
        assert "timestamp" in rec

    def test_long_text_field_is_truncated(self, tmp_path: Path) -> None:
        al = AuditLogger(tmp_path)
        long_text = "x" * 200
        al.log_action(1, "t", {"action": "type", "text": long_text}, "r")
        rec = json.loads(_read_lines(tmp_path)[0])
        assert rec["action"]["text"].endswith("...[truncated]")
        assert len(rec["action"]["text"]) < len(long_text)

    def test_long_task_is_truncated(self, tmp_path: Path) -> None:
        al = AuditLogger(tmp_path)
        al.log_action(1, "y" * 300, {"action": "wait", "reason": "loading"}, "ok")
        rec = json.loads(_read_lines(tmp_path)[0])
        assert rec["task"].endswith("...[truncated]")

    def test_anthropic_key_is_redacted(self, tmp_path: Path) -> None:
        al = AuditLogger(tmp_path)
        # Put a fake key inside a non-content field so truncation doesn't hide it.
        al.log_action(
            1,
            "short task",
            {"action": "tap", "x": 1, "y": 2, "note": "sk-ant-AAAAAAAAA_bbb"},
            "ok",
        )
        line = _read_lines(tmp_path)[0]
        assert "sk-ant-AAAAAAAAA_bbb" not in line
        assert "[REDACTED:ANTHROPIC_KEY]" in line

    def test_telegram_token_is_redacted(self, tmp_path: Path) -> None:
        al = AuditLogger(tmp_path)
        token = "123456789:" + ("A" * 35)
        al.log_action(1, "short", {"action": "tap", "x": 1, "y": 2, "note": token}, "r")
        line = _read_lines(tmp_path)[0]
        assert token not in line
        assert "[REDACTED:TELEGRAM_TOKEN]" in line

    def test_idempotent_handler_attachment(self, tmp_path: Path) -> None:
        AuditLogger(tmp_path)
        AuditLogger(tmp_path)
        AuditLogger(tmp_path)
        # All three instances share the singleton logger; ensure only one
        # rotating handler is attached.
        from logging.handlers import TimedRotatingFileHandler

        log = logging.getLogger("mobile_agent.audit")
        rotaters = [h for h in log.handlers if isinstance(h, TimedRotatingFileHandler)]
        assert len(rotaters) == 1
