"""Unit tests for GoogleAuthManager: auth URL, exchange, refresh, token file."""
from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from services.google_auth import (
    GOOGLE_TOKEN_URL,
    GOOGLE_USERINFO_URL,
    GoogleAccount,
    GoogleAuthError,
    GoogleAuthManager,
)

NOW = datetime(2026, 6, 10, 2, 30, tzinfo=timezone.utc)
REDIRECT = "http://127.0.0.1:8090/api/google/oauth/callback"


class FakeHttp:
    """Scripted HTTP seam: records every call, pops queued (status, payload)."""

    def __init__(self, responses: list[tuple[int, dict]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    async def __call__(self, method, url, *, data=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "data": data, "headers": headers}
        )
        return self.responses.pop(0)


def make_manager(
    tmp_path: Path, responses: list[tuple[int, dict]] | None = None
) -> tuple[GoogleAuthManager, FakeHttp]:
    http = FakeHttp(responses)
    mgr = GoogleAuthManager(
        client_id="cid",
        client_secret="sec",
        redirect_uri=REDIRECT,
        token_path=tmp_path / "google_token.json",
        http=http,
        clock=lambda: NOW,
    )
    return mgr, http


def write_token(
    tmp_path: Path,
    *,
    access_token: str = "at-old",
    refresh_token: str = "rt-1",
    expires_at: str = "",
) -> Path:
    path = tmp_path / "google_token.json"
    path.write_text(
        json.dumps(
            {
                "email": "me@example.com",
                "scopes": list(GoogleAuthManager.SCOPES),
                "refresh_token": refresh_token,
                "access_token": access_token,
                "expires_at": expires_at,
                "connected_at": "2026-06-01T00:00:00+00:00",
            }
        )
    )
    return path


class TestConfiguredAndAuthUrl:
    def test_configured_requires_both_secrets(self, tmp_path: Path) -> None:
        mgr, _ = make_manager(tmp_path)
        assert mgr.configured is True
        bare = GoogleAuthManager(
            client_id="", client_secret="sec", redirect_uri=REDIRECT,
            token_path=tmp_path / "t.json",
        )
        assert bare.configured is False

    def test_auth_url_carries_offline_consent_and_state(self, tmp_path: Path) -> None:
        mgr, _ = make_manager(tmp_path)
        url = mgr.build_auth_url("state-123")
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
            "https://accounts.google.com/o/oauth2/v2/auth"
        )
        q = parse_qs(parts.query)
        assert q["client_id"] == ["cid"]
        assert q["redirect_uri"] == [REDIRECT]
        assert q["response_type"] == ["code"]
        assert q["access_type"] == ["offline"]
        assert q["prompt"] == ["consent"]
        assert q["state"] == ["state-123"]
        assert q["scope"] == [" ".join(GoogleAuthManager.SCOPES)]


class TestExchangeCode:
    async def test_happy_path_persists_and_returns_account(self, tmp_path: Path) -> None:
        mgr, http = make_manager(
            tmp_path,
            [
                (200, {
                    "access_token": "at-1", "refresh_token": "rt-1",
                    "expires_in": 3600, "scope": " ".join(GoogleAuthManager.SCOPES),
                }),
                (200, {"email": "me@example.com"}),
            ],
        )
        account = await mgr.exchange_code("code-xyz")

        assert account == GoogleAccount(
            email="me@example.com",
            scopes=GoogleAuthManager.SCOPES,
            connected_at=NOW.isoformat(),
        )

        token_post, userinfo_get = http.calls
        assert token_post["method"] == "POST"
        assert token_post["url"] == GOOGLE_TOKEN_URL
        assert token_post["data"] == {
            "code": "code-xyz", "client_id": "cid", "client_secret": "sec",
            "redirect_uri": REDIRECT, "grant_type": "authorization_code",
        }
        assert userinfo_get["method"] == "GET"
        assert userinfo_get["url"] == GOOGLE_USERINFO_URL
        assert userinfo_get["headers"]["Authorization"] == "Bearer at-1"

        path = tmp_path / "google_token.json"
        saved = json.loads(path.read_text())
        assert saved["email"] == "me@example.com"
        assert saved["refresh_token"] == "rt-1"
        assert saved["access_token"] == "at-1"
        assert saved["expires_at"] == (NOW + timedelta(seconds=3600)).isoformat()
        assert saved["connected_at"] == NOW.isoformat()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    async def test_token_endpoint_error_raises(self, tmp_path: Path) -> None:
        mgr, _ = make_manager(
            tmp_path, [(400, {"error": "invalid_grant", "error_description": "Bad code"})]
        )
        with pytest.raises(GoogleAuthError, match="HTTP 400.*Bad code"):
            await mgr.exchange_code("stale")
        assert not (tmp_path / "google_token.json").exists()

    async def test_missing_refresh_token_raises(self, tmp_path: Path) -> None:
        mgr, _ = make_manager(tmp_path, [(200, {"access_token": "at-1"})])
        with pytest.raises(GoogleAuthError, match="refresh token"):
            await mgr.exchange_code("code")

    async def test_userinfo_error_raises(self, tmp_path: Path) -> None:
        mgr, _ = make_manager(
            tmp_path,
            [
                (200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 60}),
                (401, {"error": "invalid_token"}),
            ],
        )
        with pytest.raises(GoogleAuthError, match="email"):
            await mgr.exchange_code("code")


class TestStatus:
    def test_absent_file_is_none(self, tmp_path: Path) -> None:
        mgr, _ = make_manager(tmp_path)
        assert mgr.status() is None

    def test_corrupt_file_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "google_token.json").write_text("{not json")
        mgr, _ = make_manager(tmp_path)
        assert mgr.status() is None

    def test_connected_file_parses(self, tmp_path: Path) -> None:
        write_token(tmp_path, expires_at=(NOW + timedelta(hours=1)).isoformat())
        mgr, _ = make_manager(tmp_path)
        account = mgr.status()
        assert account is not None
        assert account.email == "me@example.com"
        assert account.scopes == GoogleAuthManager.SCOPES
        assert account.connected_at == "2026-06-01T00:00:00+00:00"


class TestGetAccessToken:
    async def test_not_connected_raises(self, tmp_path: Path) -> None:
        mgr, http = make_manager(tmp_path)
        with pytest.raises(GoogleAuthError, match="not connected"):
            await mgr.get_access_token()
        assert http.calls == []

    async def test_fresh_token_returned_without_network(self, tmp_path: Path) -> None:
        write_token(tmp_path, expires_at=(NOW + timedelta(hours=1)).isoformat())
        mgr, http = make_manager(tmp_path)
        assert await mgr.get_access_token() == "at-old"
        assert http.calls == []

    async def test_expired_token_refreshes_and_persists(self, tmp_path: Path) -> None:
        write_token(tmp_path, expires_at=(NOW - timedelta(minutes=5)).isoformat())
        mgr, http = make_manager(
            tmp_path, [(200, {"access_token": "at-new", "expires_in": 3600})]
        )
        assert await mgr.get_access_token() == "at-new"

        (refresh_post,) = http.calls
        assert refresh_post["method"] == "POST"
        assert refresh_post["url"] == GOOGLE_TOKEN_URL
        assert refresh_post["data"] == {
            "client_id": "cid", "client_secret": "sec",
            "refresh_token": "rt-1", "grant_type": "refresh_token",
        }

        saved = json.loads((tmp_path / "google_token.json").read_text())
        assert saved["access_token"] == "at-new"
        assert saved["expires_at"] == (NOW + timedelta(seconds=3600)).isoformat()
        assert saved["refresh_token"] == "rt-1"  # preserved when Google omits it
        assert stat.S_IMODE((tmp_path / "google_token.json").stat().st_mode) == 0o600

    async def test_token_expiring_within_leeway_refreshes(self, tmp_path: Path) -> None:
        write_token(tmp_path, expires_at=(NOW + timedelta(seconds=30)).isoformat())
        mgr, http = make_manager(
            tmp_path, [(200, {"access_token": "at-new", "expires_in": 3600})]
        )
        assert await mgr.get_access_token() == "at-new"
        assert len(http.calls) == 1

    async def test_rotated_refresh_token_is_kept(self, tmp_path: Path) -> None:
        write_token(tmp_path, expires_at=(NOW - timedelta(minutes=5)).isoformat())
        mgr, _ = make_manager(
            tmp_path,
            [(200, {"access_token": "at-new", "refresh_token": "rt-2", "expires_in": 60})],
        )
        await mgr.get_access_token()
        saved = json.loads((tmp_path / "google_token.json").read_text())
        assert saved["refresh_token"] == "rt-2"

    async def test_refresh_rejection_raises(self, tmp_path: Path) -> None:
        write_token(tmp_path, expires_at=(NOW - timedelta(minutes=5)).isoformat())
        mgr, _ = make_manager(
            tmp_path,
            [(400, {"error": "invalid_grant", "error_description": "Token revoked"})],
        )
        with pytest.raises(GoogleAuthError, match="HTTP 400.*Token revoked"):
            await mgr.get_access_token()


class TestDisconnect:
    def test_deletes_existing_file(self, tmp_path: Path) -> None:
        write_token(tmp_path, expires_at=NOW.isoformat())
        mgr, _ = make_manager(tmp_path)
        assert mgr.disconnect() is True
        assert not (tmp_path / "google_token.json").exists()
        assert mgr.status() is None

    def test_absent_file_is_false(self, tmp_path: Path) -> None:
        mgr, _ = make_manager(tmp_path)
        assert mgr.disconnect() is False

    async def test_close_is_safe_with_injected_seam(self, tmp_path: Path) -> None:
        mgr, _ = make_manager(tmp_path)
        await mgr.close()  # no session was ever created; must not raise
