"""Tests for the dashboard Google/cloud routes (OAuth, contacts, command box).

The GoogleAuthManager and the command trigger are hand-rolled duck-typed fakes
— no services/ import, no network — so these tests stand alone. The contact and
cloud-action repos are the real SQLite DAOs against tmp_path.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from agent.persistence import (
    CloudActionRepository,
    ContactRepository,
    ScheduleRepository,
    TaskRepository,
)
from bot.dashboard_api import build_dashboard_app
from bot.events import EventBus

TOKEN = "dash-secret"
OWNER = 42
PKG = "com.grofers.customerapp"
# Same derivation as the server's: only a caller holding the dashboard token
# can mint the state Google round-trips back to the callback.
STATE = hmac.new(TOKEN.encode(), b"google-oauth", hashlib.sha256).hexdigest()[:32]

_ACCOUNT = SimpleNamespace(
    email="me@example.com",
    scopes=("https://www.googleapis.com/auth/gmail.send",),
    connected_at="2026-06-11T00:00:00+00:00",
)


class _FakeGoogleAuth:
    """Duck-typed GoogleAuthManager — records calls instead of talking OAuth."""

    def __init__(
        self,
        *,
        configured: bool = True,
        account=None,
        exchange_error: Exception | None = None,
    ) -> None:
        self.configured = configured
        self._account = account
        self._exchange_error = exchange_error
        self.exchange_codes: list[str] = []
        self.disconnected = False

    def status(self):
        return self._account

    def build_auth_url(self, state: str) -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    async def exchange_code(self, code: str):
        self.exchange_codes.append(code)
        if self._exchange_error is not None:
            raise self._exchange_error
        self._account = _ACCOUNT
        return _ACCOUNT

    def disconnect(self) -> bool:
        self.disconnected = True
        existed = self._account is not None
        self._account = None
        return existed


class _FakeTrigger:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def handle_external_run(self, user_id: int, text: str) -> str:
        self.calls.append((user_id, text))
        return "On it."


async def _client(tmp_path, **overrides) -> TestClient:
    db = tmp_path / "t.db"
    base = TaskRepository(db)
    await base.initialize()
    kwargs = dict(
        repo=base, saved_repo=None, schedule_repo=ScheduleRepository(db),
        event_bus=EventBus(), token=TOKEN, owner_user_id=OWNER, dist_dir=None,
        cloud_repo=CloudActionRepository(db), contacts_repo=ContactRepository(db),
    )
    kwargs.update(overrides)
    client = TestClient(TestServer(build_dashboard_app(**kwargs)))
    await client.start_server()
    return client


def _q() -> dict:
    return {"token": TOKEN}


class TestGoogleStatus:
    async def test_unconfigured_when_auth_absent(self, tmp_path) -> None:
        client = await _client(tmp_path)
        try:
            body = await (await client.get("/api/google/status", params=_q())).json()
            assert body == {"configured": False, "connected": False,
                            "email": None, "scopes": [], "connected_at": None}
        finally:
            await client.close()

    async def test_configured_but_disconnected(self, tmp_path) -> None:
        client = await _client(tmp_path, google_auth=_FakeGoogleAuth())
        try:
            body = await (await client.get("/api/google/status", params=_q())).json()
            assert body["configured"] is True and body["connected"] is False
            assert body["email"] is None and body["scopes"] == []
        finally:
            await client.close()

    async def test_connected(self, tmp_path) -> None:
        client = await _client(tmp_path, google_auth=_FakeGoogleAuth(account=_ACCOUNT))
        try:
            body = await (await client.get("/api/google/status", params=_q())).json()
            assert body["connected"] is True
            assert body["email"] == "me@example.com"
            assert body["scopes"] == list(_ACCOUNT.scopes)
            assert body["connected_at"] == _ACCOUNT.connected_at
        finally:
            await client.close()

    async def test_requires_token(self, tmp_path) -> None:
        client = await _client(tmp_path, google_auth=_FakeGoogleAuth())
        try:
            r = await client.get("/api/google/status")
            assert r.status == 401
        finally:
            await client.close()


class TestGoogleConnect:
    async def test_auth_url_carries_the_state(self, tmp_path) -> None:
        client = await _client(tmp_path, google_auth=_FakeGoogleAuth())
        try:
            r = await client.post("/api/google/connect", params=_q())
            assert r.status == 200
            assert STATE in (await r.json())["auth_url"]
        finally:
            await client.close()

    async def test_400_when_unconfigured(self, tmp_path) -> None:
        client = await _client(tmp_path)
        try:
            r = await client.post("/api/google/connect", params=_q())
            assert r.status == 400
        finally:
            await client.close()


class TestOauthCallback:
    """The callback is auth-exempt — no token is passed in any request here."""

    async def test_bad_state_rejected_without_exchange(self, tmp_path) -> None:
        auth = _FakeGoogleAuth()
        client = await _client(tmp_path, google_auth=auth)
        try:
            r = await client.get(
                "/api/google/oauth/callback",
                params={"code": "abc", "state": "forged"},
                allow_redirects=False,
            )
            assert 400 <= r.status < 500
            assert auth.exchange_codes == []
        finally:
            await client.close()

    async def test_good_state_exchanges_and_redirects(self, tmp_path) -> None:
        auth = _FakeGoogleAuth()
        client = await _client(tmp_path, google_auth=auth)
        try:
            r = await client.get(
                "/api/google/oauth/callback",
                params={"code": "abc", "state": STATE},
                allow_redirects=False,
            )
            assert r.status == 302
            assert r.headers["Location"] == "/settings?google=connected"
            assert auth.exchange_codes == ["abc"]
        finally:
            await client.close()

    async def test_exchange_failure_redirects_to_error(self, tmp_path) -> None:
        auth = _FakeGoogleAuth(exchange_error=RuntimeError("google said no"))
        client = await _client(tmp_path, google_auth=auth)
        try:
            r = await client.get(
                "/api/google/oauth/callback",
                params={"code": "abc", "state": STATE},
                allow_redirects=False,
            )
            assert r.status == 302
            assert r.headers["Location"] == "/settings?google=error"
        finally:
            await client.close()

    async def test_missing_code_400(self, tmp_path) -> None:
        auth = _FakeGoogleAuth()
        client = await _client(tmp_path, google_auth=auth)
        try:
            r = await client.get(
                "/api/google/oauth/callback",
                params={"state": STATE},
                allow_redirects=False,
            )
            assert r.status == 400 and auth.exchange_codes == []
        finally:
            await client.close()


class TestGoogleDisconnect:
    async def test_disconnects(self, tmp_path) -> None:
        auth = _FakeGoogleAuth(account=_ACCOUNT)
        client = await _client(tmp_path, google_auth=auth)
        try:
            r = await client.post("/api/google/disconnect", params=_q())
            assert (await r.json())["ok"] is True
            assert auth.disconnected is True
        finally:
            await client.close()

    async def test_ok_even_without_auth(self, tmp_path) -> None:
        client = await _client(tmp_path)
        try:
            r = await client.post("/api/google/disconnect", params=_q())
            assert (await r.json())["ok"] is True
        finally:
            await client.close()


class TestContacts:
    async def test_add_list_delete_roundtrip(self, tmp_path) -> None:
        client = await _client(tmp_path)
        try:
            r = await client.post(
                "/api/contacts", params=_q(),
                json={"name": "Ayush", "email": "ayush@example.com"},
            )
            assert r.status == 200 and (await r.json())["ok"] is True

            items = (await (await client.get("/api/contacts", params=_q())).json())["items"]
            assert len(items) == 1
            assert items[0]["name"] == "ayush"  # stored lowercase
            assert items[0]["email"] == "ayush@example.com"

            r = await client.delete("/api/contacts/Ayush", params=_q())
            assert (await r.json()) == {"ok": True, "deleted": True}
            items = (await (await client.get("/api/contacts", params=_q())).json())["items"]
            assert items == []
        finally:
            await client.close()

    async def test_invalid_email_400(self, tmp_path) -> None:
        client = await _client(tmp_path)
        try:
            r = await client.post(
                "/api/contacts", params=_q(),
                json={"name": "Ayush", "email": "not-an-address"},
            )
            assert r.status == 400
        finally:
            await client.close()

    async def test_missing_name_400(self, tmp_path) -> None:
        client = await _client(tmp_path)
        try:
            r = await client.post(
                "/api/contacts", params=_q(), json={"email": "a@b.com"}
            )
            assert r.status == 400
        finally:
            await client.close()

    async def test_delete_unknown_reports_not_deleted(self, tmp_path) -> None:
        client = await _client(tmp_path)
        try:
            r = await client.delete("/api/contacts/nobody", params=_q())
            assert (await r.json()) == {"ok": True, "deleted": False}
        finally:
            await client.close()


class TestCloudActions:
    async def test_lists_owner_rows_with_parsed_json(self, tmp_path) -> None:
        client = await _client(tmp_path)
        cloud = CloudActionRepository(tmp_path / "t.db")
        await cloud.insert(
            user_id=OWNER, kind="email",
            payload={"to": ["a@b.com"], "subject": "Demo ready"},
            result={"id": "m1"}, status="ok",
        )
        await cloud.insert(
            user_id=99, kind="email", payload={"subject": "not mine"},
            result=None, status="error", error="boom",
        )
        try:
            items = (await (await client.get("/api/cloud-actions", params=_q())).json())["items"]
            assert len(items) == 1
            assert items[0]["kind"] == "email" and items[0]["status"] == "ok"
            assert items[0]["payload"]["subject"] == "Demo ready"
            assert items[0]["result"] == {"id": "m1"}
        finally:
            await client.close()


class TestCommand:
    async def test_runs_through_the_trigger(self, tmp_path) -> None:
        trig = _FakeTrigger()
        client = await _client(tmp_path, trigger=trig)
        try:
            r = await client.post(
                "/api/command", params=_q(), json={"text": "email bob hi"}
            )
            assert (await r.json()) == {"ok": True, "message": "On it."}
            assert trig.calls == [(OWNER, "email bob hi")]
        finally:
            await client.close()

    async def test_blank_text_400(self, tmp_path) -> None:
        trig = _FakeTrigger()
        client = await _client(tmp_path, trigger=trig)
        try:
            r = await client.post("/api/command", params=_q(), json={"text": "  "})
            assert r.status == 400 and trig.calls == []
        finally:
            await client.close()

    async def test_no_trigger_degrades(self, tmp_path) -> None:
        client = await _client(tmp_path)
        try:
            body = await (await client.post(
                "/api/command", params=_q(), json={"text": "hello"}
            )).json()
            assert body == {"ok": False, "message": "command runner unavailable"}
        finally:
            await client.close()


class TestSchedulesSerializer:
    async def test_exposes_action_kind_and_cloud_payload(self, tmp_path) -> None:
        client = await _client(tmp_path)
        sched = ScheduleRepository(tmp_path / "t.db")
        await sched.insert(
            user_id=OWNER, name="Morning", freq="daily", at_minute=540,
            tz="Asia/Kolkata", raw_description="add milk",
            next_run_at="2030-01-01T00:00:00+00:00", launch_package=PKG,
        )
        payload = {"to": ["team@example.com"], "subject": "Standup", "body": "notes"}
        await sched.insert(
            user_id=OWNER, name="Email: Standup", freq="once", at_minute=480,
            tz="Asia/Kolkata", raw_description="email the team",
            next_run_at="2030-01-02T00:00:00+00:00",
            action_kind="email", payload_json=json.dumps(payload),
        )
        try:
            items = (await (await client.get("/api/schedules", params=_q())).json())["items"]
            by_name = {i["name"]: i for i in items}
            device = by_name["Morning"]
            assert device["action_kind"] == "device" and "payload" not in device
            cloud = by_name["Email: Standup"]
            assert cloud["action_kind"] == "email" and cloud["payload"] == payload
        finally:
            await client.close()
