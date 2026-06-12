"""Google OAuth2 for the connected account — manual flow over aiohttp, no SDK.

The dashboard Settings page drives the consent flow: it opens the auth URL,
Google redirects back to the dashboard callback with a code, and
`exchange_code` turns that code into tokens plus the account email (the
connected account IS the default Gmail sender). Tokens live in a single JSON
file under the config dir, written atomically with mode 0o600 — the only
secret this project ever puts on disk.

All HTTP goes through one injectable seam (`http=` ctor kwarg) so tests run
with zero network; the default seam owns a lazily created aiohttp
ClientSession (never opened at import time, reusable across calls).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import aiohttp


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Refresh when the access token has less than this long to live, so a token
# that expires mid-request never reaches the Gmail/Calendar call.
REFRESH_LEEWAY_SECONDS = 60


class GoogleAuthError(Exception):
    """OAuth problem the user can act on (not connected, refresh rejected)."""


@dataclass(frozen=True)
class GoogleAccount:
    email: str
    scopes: tuple[str, ...]
    connected_at: str  # ISO UTC


class _AiohttpSeam:
    """Default HTTP seam: one lazy ClientSession, (status, json-dict) out.

    `data` is either a form dict (token endpoint) or a pre-encoded JSON
    string/bytes (the workspace services set their own Content-Type header).
    Non-JSON response bodies come back as {"raw": <text>} so error mapping
    still has something to show.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def __call__(
        self, method: str, url: str, *, data=None, headers=None
    ) -> tuple[int, dict]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        async with self._session.request(method, url, data=data, headers=headers) as resp:
            status = resp.status
            text = await resp.text()
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"raw": text}
        if not isinstance(payload, dict):
            payload = {"raw": text}
        return status, payload

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


class GoogleAuthManager:
    """Owns the OAuth dance and the on-disk token file for ONE Google account."""

    SCOPES = (
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar.events",
        # Read-only Workspace directory search — resolves a spoken name
        # ("email Mohan") to a colleague's address via the People API. Adding
        # this scope means a connected account must RE-CONSENT (reconnect from
        # the dashboard) before directory resolution works.
        "https://www.googleapis.com/auth/directory.readonly",
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
    )

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        token_path: Path,
        http: Callable | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._token_path = token_path
        self._http = http if http is not None else _AiohttpSeam()
        self._clock = clock if clock is not None else (
            lambda: datetime.now(timezone.utc)
        )

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def status(self) -> GoogleAccount | None:
        """Parse the token file into a GoogleAccount; None if absent/corrupt."""
        data = self._load()
        if data is None or not data.get("email"):
            return None
        return GoogleAccount(
            email=str(data["email"]),
            scopes=tuple(data.get("scopes") or ()),
            connected_at=str(data.get("connected_at", "")),
        )

    def build_auth_url(self, state: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> GoogleAccount:
        """Trade the callback code for tokens, learn the email, persist."""
        status, payload = await self._http(
            "POST",
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if status != 200:
            raise GoogleAuthError(
                f"Google code exchange failed (HTTP {status}): {_oauth_reason(payload)}"
            )
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not access_token or not refresh_token:
            # No refresh_token means we can't act later without the browser —
            # treat as a failed connect rather than storing a half-usable file.
            raise GoogleAuthError(
                "Google did not return both an access and refresh token"
            )

        status, info = await self._http(
            "GET",
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        email = info.get("email") if status == 200 else None
        if not email:
            raise GoogleAuthError(
                f"couldn't read the account email (HTTP {status}): {_oauth_reason(info)}"
            )

        now = self._clock()
        granted = tuple(str(payload.get("scope", "")).split()) or self.SCOPES
        expires_in = int(payload.get("expires_in", 3600))
        self._write(
            {
                "email": str(email),
                "scopes": list(granted),
                "refresh_token": refresh_token,
                "access_token": access_token,
                "expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
                "connected_at": now.isoformat(),
            }
        )
        return GoogleAccount(
            email=str(email), scopes=granted, connected_at=now.isoformat()
        )

    async def get_access_token(self) -> str:
        """Return a live access token, refreshing if it expires within 60s."""
        data = self._load()
        if data is None or not data.get("refresh_token"):
            raise GoogleAuthError(
                "Google account is not connected — connect it from the "
                "dashboard Settings page."
            )
        now = self._clock()
        expires_at = _parse_iso(data.get("expires_at"))
        if (
            data.get("access_token")
            and expires_at is not None
            and (expires_at - now).total_seconds() > REFRESH_LEEWAY_SECONDS
        ):
            return str(data["access_token"])

        status, payload = await self._http(
            "POST",
            GOOGLE_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": data["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        if status != 200 or not payload.get("access_token"):
            raise GoogleAuthError(
                f"Google token refresh failed (HTTP {status}): {_oauth_reason(payload)}"
            )
        data["access_token"] = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
        data["expires_at"] = (now + timedelta(seconds=expires_in)).isoformat()
        # Google occasionally rotates the refresh token; keep the newest one.
        if payload.get("refresh_token"):
            data["refresh_token"] = payload["refresh_token"]
        self._write(data)
        return str(data["access_token"])

    def disconnect(self) -> bool:
        """Delete the token file; True if it existed."""
        try:
            self._token_path.unlink()
        except FileNotFoundError:
            return False
        return True

    async def close(self) -> None:
        """Close the default seam's ClientSession (no-op for injected seams)."""
        if isinstance(self._http, _AiohttpSeam):
            await self._http.close()

    # ------------------------------------------------------------------
    # Token file

    def _load(self) -> dict | None:
        try:
            data = json.loads(self._token_path.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write(self, data: dict) -> None:
        # tmp + rename so a crash mid-write never leaves a truncated token
        # file; chmod BEFORE the rename so the final path is never world-readable.
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._token_path.with_name(self._token_path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._token_path)


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    # A naive timestamp can't be compared against the UTC clock — treat it
    # as corrupt (forces a refresh, which rewrites a clean value).
    return parsed if parsed.tzinfo is not None else None


def _oauth_reason(payload: dict) -> str:
    """Best human-readable line from a Google OAuth error body."""
    return str(
        payload.get("error_description")
        or payload.get("error")
        or payload.get("raw")
        or payload
    )
