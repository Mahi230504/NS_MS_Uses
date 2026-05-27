"""Tests for the external-trigger HTTP webhook."""
from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.webhook import build_webhook_app


_SECRET = "topsecret-value"
_OWNER = 42


class _StubHandlers:
    """Records calls; lets a test force the returned line or an exception."""

    def __init__(self, reply: str = "started", raise_exc: bool = False) -> None:
        self.reply = reply
        self.raise_exc = raise_exc
        self.calls: list[tuple[int, str]] = []
        self.confirms: list[tuple[int, bool]] = []
        self.runs: list[tuple[int, str]] = []

    async def handle_external_trigger(self, user_id: int, text: str) -> str:
        self.calls.append((user_id, text))
        if self.raise_exc:
            raise RuntimeError("boom")
        return self.reply

    async def handle_external_confirm(self, user_id: int, approve: bool) -> str:
        self.confirms.append((user_id, approve))
        if self.raise_exc:
            raise RuntimeError("boom")
        return "confirmed" if approve else "cancelled"

    async def handle_external_run(self, user_id: int, text: str) -> str:
        self.runs.append((user_id, text))
        if self.raise_exc:
            raise RuntimeError("boom")
        return "running"


async def _client(handlers: _StubHandlers) -> TestClient:
    app = build_webhook_app(handlers, secret=_SECRET, owner_user_id=_OWNER)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


class TestBuild:
    def test_empty_secret_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_webhook_app(_StubHandlers(), secret="", owner_user_id=1)


class TestTrigger:
    async def test_valid_via_header(self) -> None:
        h = _StubHandlers("On it — Blinkit")
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"text": "order milk on blinkit"},
            )
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "message": "On it — Blinkit"}
            assert h.calls == [(_OWNER, "order milk on blinkit")]
        finally:
            await client.close()

    async def test_valid_via_alt_header_name(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"WebhookSecret": _SECRET},
                json={"text": "order milk"},
            )
            assert resp.status == 200
            assert h.calls == [(_OWNER, "order milk")]
        finally:
            await client.close()

    async def test_valid_via_body_secret(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger", json={"secret": _SECRET, "text": "play jazz"}
            )
            assert resp.status == 200
            assert h.calls == [(_OWNER, "play jazz")]
        finally:
            await client.close()

    async def test_body_cannot_override_owner(self) -> None:
        # A caller must not be able to act as a different user.
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"text": "x", "user_id": 9999},
            )
            assert resp.status == 200
            assert h.calls == [(_OWNER, "x")]  # owner, not 9999
        finally:
            await client.close()

    async def test_wrong_secret_401(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": "nope"},
                json={"text": "order milk"},
            )
            assert resp.status == 401
            assert h.calls == []  # never reached the handler
        finally:
            await client.close()

    async def test_missing_secret_401(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post("/trigger", json={"text": "order milk"})
            assert resp.status == 401
            assert h.calls == []
        finally:
            await client.close()

    async def test_missing_text_400(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger", headers={"X-Webhook-Secret": _SECRET}, json={}
            )
            assert resp.status == 400
            assert h.calls == []
        finally:
            await client.close()

    async def test_blank_text_400(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"text": "   "},
            )
            assert resp.status == 400
        finally:
            await client.close()

    async def test_non_json_body_401_without_secret(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger", data="not json", headers={"Content-Type": "text/plain"}
            )
            # No parseable secret anywhere -> unauthorized, not a 500.
            assert resp.status == 401
        finally:
            await client.close()

    async def test_handler_exception_500(self) -> None:
        h = _StubHandlers(raise_exc=True)
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"text": "order milk"},
            )
            assert resp.status == 500
            body = await resp.json()
            assert body["ok"] is False
            assert "internal" in body["message"]  # no leaked detail
        finally:
            await client.close()


class TestConfirm:
    async def test_confirm_yes_routes_to_confirm(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"confirm": "yes"},
            )
            assert resp.status == 200
            assert h.confirms == [(_OWNER, True)]
            assert h.calls == []  # not treated as a new task
        finally:
            await client.close()

    async def test_confirm_no_routes_as_decline(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"confirm": "no"},
            )
            assert resp.status == 200
            assert h.confirms == [(_OWNER, False)]
        finally:
            await client.close()

    async def test_confirm_junk_is_decline(self) -> None:
        # A mis-transcribed confirmation must not accidentally approve.
        h = _StubHandlers()
        client = await _client(h)
        try:
            await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"confirm": "uhh maybe"},
            )
            assert h.confirms == [(_OWNER, False)]
        finally:
            await client.close()

    async def test_confirm_requires_secret(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post("/trigger", json={"confirm": "yes"})
            assert resp.status == 401
            assert h.confirms == []
        finally:
            await client.close()

    async def test_blank_confirm_with_text_is_a_fresh_trigger(self) -> None:
        # The Android client sends ONE static body on every call:
        #   {"text": "{{input}}", "confirm": "{{confirm}}"}
        # On a fresh task `confirm` is empty. That blank value must NOT be
        # read as a decline — it should fall through to the text trigger.
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"text": "order milk on blinkit", "confirm": ""},
            )
            assert resp.status == 200
            assert h.calls == [(_OWNER, "order milk on blinkit")]
            assert h.confirms == []  # NOT treated as a confirmation
        finally:
            await client.close()

    async def test_whitespace_confirm_with_text_is_a_fresh_trigger(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"text": "play jazz", "confirm": "   "},
            )
            assert resp.status == 200
            assert h.calls == [(_OWNER, "play jazz")]
            assert h.confirms == []
        finally:
            await client.close()

    async def test_populated_confirm_with_blank_text_routes_to_confirm(self) -> None:
        # The re-trigger: input is empty, confirm carries the spoken yes/no.
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"text": "", "confirm": "yes"},
            )
            assert resp.status == 200
            assert h.confirms == [(_OWNER, True)]
            assert h.calls == []
        finally:
            await client.close()


class TestAutorun:
    async def test_autorun_true_bool_runs_single_shot(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"text": "order milk on blinkit", "autorun": True},
            )
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "message": "running"}
            assert h.runs == [(_OWNER, "order milk on blinkit")]
            assert h.calls == []  # NOT the two-phase propose path
            assert h.confirms == []
        finally:
            await client.close()

    async def test_autorun_string_true_runs(self) -> None:
        # HTTP Shortcuts may serialise the flag as the string "true".
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"text": "order milk", "autorun": "true"},
            )
            assert resp.status == 200
            assert h.runs == [(_OWNER, "order milk")]
        finally:
            await client.close()

    async def test_autorun_absent_uses_two_phase_propose(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"text": "order milk"},
            )
            assert resp.status == 200
            assert h.calls == [(_OWNER, "order milk")]  # propose, not run
            assert h.runs == []
        finally:
            await client.close()

    async def test_autorun_false_string_uses_two_phase(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger",
                headers={"X-Webhook-Secret": _SECRET},
                json={"text": "order milk", "autorun": "false"},
            )
            assert resp.status == 200
            assert h.calls == [(_OWNER, "order milk")]
            assert h.runs == []
        finally:
            await client.close()

    async def test_autorun_requires_secret(self) -> None:
        h = _StubHandlers()
        client = await _client(h)
        try:
            resp = await client.post(
                "/trigger", json={"text": "x", "autorun": True}
            )
            assert resp.status == 401
            assert h.runs == []
        finally:
            await client.close()


class TestHealth:
    async def test_health_ok_no_secret(self) -> None:
        client = await _client(_StubHandlers())
        try:
            resp = await client.get("/health")
            assert resp.status == 200
            assert await resp.json() == {"ok": True}
        finally:
            await client.close()
