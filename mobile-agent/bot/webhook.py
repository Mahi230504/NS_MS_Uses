"""HTTP webhook for external triggers (Android voice/widget via HTTP Shortcuts).

Design: the trigger only *proposes* and *confirms*; Telegram stays the approval
+ result surface. A request authenticates with a shared secret and maps to a
single configured owner user, then runs the same router -> orchestrator path as
a Telegram message. HITL gates (payment/OTP/delete) still fire on Telegram — the
trigger never bypasses them.

Two-phase so the user hears what was understood before anything runs:
    POST /trigger
    X-Webhook-Secret: <secret>          (or {"secret": "..."} in the body)
    {"text": "order milk on blinkit"}   -> parse + propose; speak the reply back
    {"confirm": "yes"}                  -> launch the pending task
    {"confirm": "no"}                   -> drop it

Every response is {"ok": <bool>, "message": "<short line to speak>"}.

For a USB phone, run `adb reverse tcp:8765 tcp:8765` (the agent does this on
startup) and POST to http://127.0.0.1:8765 from the device — nothing is exposed
on any network.
"""
from __future__ import annotations

import hmac
import logging
from typing import Protocol

from aiohttp import web

log = logging.getLogger("mobile_agent.webhook")

# Spoken/typed words we treat as an affirmative confirmation. Anything else
# (including "no", "cancel", silence transcribed as junk) is a decline.
_AFFIRMATIVE = frozenset(
    {"yes", "y", "yeah", "yep", "yup", "confirm", "ok", "okay", "sure", "go", "true", "1"}
)


def _truthy(value: object) -> bool:
    """Loose truthiness for the autorun flag.

    HTTP Shortcuts often can't send a real JSON boolean — the field may
    arrive as the string "true"/"1"/"yes" (or a real bool). Treat all of
    those as on; everything else (missing, "", "false", "0") as off.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


class _Trigger(Protocol):
    async def handle_external_trigger(self, user_id: int, text: str) -> str: ...
    async def handle_external_confirm(self, user_id: int, approve: bool) -> str: ...
    async def handle_external_run(self, user_id: int, text: str) -> str: ...


def build_webhook_app(
    handlers: _Trigger, *, secret: str, owner_user_id: int
) -> web.Application:
    """Build the aiohttp app exposing /trigger and /health.

    `secret` must be non-empty; the caller (main) only builds this when a
    secret is configured. `owner_user_id` is the single user every trigger
    acts as — the request body cannot choose a different user.
    """
    if not secret:
        raise ValueError("webhook secret must be non-empty")

    async def trigger(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        # Accept the secret as a header (either spelling — some clients balk at
        # the hyphenated form) or in the JSON body, whichever is easiest to set.
        provided = (
            request.headers.get("X-Webhook-Secret")
            or request.headers.get("WebhookSecret")
            or str(body.get("secret", ""))
        )
        if not hmac.compare_digest(provided, secret):
            return web.json_response({"ok": False, "message": "unauthorized"}, status=401)

        # Temporary diagnostic (Phase 5): log the literal recognized text so we can
        # see exactly what STT produced when chasing wake-word / filter mismatches.
        # Remove once the trigger client is stable.
        log.info(
            "trigger body: text=%r confirm=%r autorun=%r",
            body.get("text"),
            body.get("confirm"),
            body.get("autorun"),
        )

        # A confirm payload answers a previous proposal; otherwise it's a new
        # spoken task to propose. A BLANK confirm ("" / whitespace) counts as
        # absent: the Android client sends a single static body
        # `{"text":"{{input}}","confirm":"{{confirm}}"}` on every call, leaving
        # `confirm` empty for a fresh task and populating it only on the
        # re-trigger. Treating "" as present would misread the first call as a
        # decline, so we require a non-blank value here.
        confirm_raw = body.get("confirm")
        has_confirm = confirm_raw is not None and str(confirm_raw).strip() != ""
        # autorun collapses the two-phase confirm into a single call — for
        # clients that can't show a reliable confirmation prompt. Send
        # {"text": "...", "autorun": true} and the task launches immediately;
        # Telegram HITL still gates payment/OTP/delete.
        autorun = _truthy(body.get("autorun"))
        try:
            if has_confirm:
                approve = str(confirm_raw).strip().lower() in _AFFIRMATIVE
                message = await handlers.handle_external_confirm(owner_user_id, approve)
            else:
                text = str(body.get("text", "")).strip()
                if not text:
                    return web.json_response(
                        {"ok": False, "message": "missing 'text' or 'confirm'"},
                        status=400,
                    )
                if autorun:
                    message = await handlers.handle_external_run(owner_user_id, text)
                else:
                    message = await handlers.handle_external_trigger(owner_user_id, text)
        except Exception:
            log.exception("external trigger failed")
            return web.json_response(
                {"ok": False, "message": "internal error"}, status=500
            )
        return web.json_response({"ok": True, "message": message})

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/trigger", trigger)
    app.router.add_get("/health", health)
    return app
