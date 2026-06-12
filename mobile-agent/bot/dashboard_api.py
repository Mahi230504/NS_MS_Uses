"""Personal-assistant dashboard: in-process aiohttp read/stream API + SPA host.

Runs inside the bot's own event loop (started from main via post_init, same as
the trigger webhook) so it can tap the orchestrator's live EventBus with no IPC.
It is a VIEW: it never mutates device state and never grants approvals — those
stay on Telegram. Single owner; every query is scoped to `owner_user_id` and the
request body/query cannot choose a different user.

Surfaces:
  GET  /api/health                                  liveness (no auth)
  GET  /api/me                                       owner + active flag
  GET  /events                                       SSE: step/state/approval/lag
  GET  /api/active                                   active-task snapshot
  GET  /api/tasks?app=<pkg>&state=&limit=&offset=    history (per-app filter)
  GET  /api/tasks/{id}                               task detail + steps (replay)
  GET  /api/tasks/{id}/steps/{idx}/screenshot        step PNG bytes
  GET  /api/analytics/apps                           preferred/most-used apps
  GET  /api/analytics/summary                        headline tiles
  GET  /api/saved | /api/schedules | /api/comparisons
  GET  /api/google/status                            OAuth client + account state
  POST /api/google/connect                           start OAuth (returns auth_url)
  GET  /api/google/oauth/callback                    Google redirects here (no auth)
  POST /api/google/disconnect                        forget the connected account
  GET  /api/cloud-actions                            executed email/Meet history
  GET|POST /api/contacts, DELETE /api/contacts/{name}  name → email address book
  POST /api/command                                  run a typed command via the bot
  (static)  the built SPA, when a dist dir is provided

Auth: a bearer token (Authorization: Bearer <t> or ?token=<t> for SSE/<img>).
Everything except /api/health, the OAuth callback (the browser arrives from
Google with no Bearer — it's gated by the HMAC state instead) and the static
bundle requires it.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from pathlib import Path

from aiohttp import web

from bot.apps import APPS

log = logging.getLogger("mobile_agent.dashboard")

_PKG_TO_APP = {a.package: a for a in APPS}


def _app_meta(launch_package: str | None) -> dict:
    app = _PKG_TO_APP.get(launch_package or "")
    if app is None:
        return {"app_id": None, "app_name": "Other / free-form",
                "emoji": "•", "category": None}
    return {"app_id": app.id, "app_name": app.name,
            "emoji": app.emoji, "category": app.category}


def _task_dict(row) -> dict:
    meta = _app_meta(row.launch_package)
    return {
        "id": row.id, "description": row.description, "state": row.state,
        "started_at": row.started_at, "ended_at": row.ended_at,
        "final_summary": row.final_summary, "failure_reason": row.failure_reason,
        "step_count": row.step_count,
        "tokens": {"in": row.total_input_tokens, "out": row.total_output_tokens},
        "duration_seconds": row.duration_seconds(),
        "launch_package": row.launch_package, **meta,
    }


def _step_dict(task_id: int, step: dict) -> dict:
    action = step.get("action") or {}
    atype = action.get("action")
    coords: dict = {}
    if atype == "tap":
        coords["tap"] = {"x": action.get("x"), "y": action.get("y")}
    elif atype == "swipe":
        coords["swipe"] = {k: action.get(k) for k in ("x1", "y1", "x2", "y2")}
    elif atype == "type":
        coords["text"] = action.get("text")
    result = step.get("result")
    return {
        "idx": step.get("idx"),
        "action_type": atype,
        "note": str(action.get("note", "")),
        "result": result,
        "rejected": isinstance(result, str) and result.startswith("REJECTED"),
        "coords": coords,
        "screenshot_url": f"/api/tasks/{task_id}/steps/{step.get('idx')}/screenshot",
        "timestamp": step.get("timestamp"),
    }


def build_dashboard_app(
    *,
    repo,
    saved_repo,
    schedule_repo,
    event_bus,
    token: str,
    owner_user_id: int,
    hitl=None,
    cors_origin: str = "http://localhost:5173",
    dist_dir: Path | None = None,
    google_auth=None,
    cloud_repo=None,
    contacts_repo=None,
    trigger=None,
) -> web.Application:
    if not token:
        raise ValueError("dashboard token must be non-empty")

    # OAuth CSRF state: derived from the dashboard token so it needs no extra
    # secret or storage, and only someone who already holds the token (i.e.
    # clicked Connect on an authed page) can mint a callback Google will pass.
    oauth_state = hmac.new(
        token.encode(), b"google-oauth", hashlib.sha256
    ).hexdigest()[:32]

    def _cors(resp: web.StreamResponse) -> web.StreamResponse:
        resp.headers["Access-Control-Allow-Origin"] = cors_origin
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        return resp

    def _authorized(request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        provided = header[7:].strip() if header.startswith("Bearer ") else ""
        provided = provided or request.query.get("token", "")
        return bool(provided) and hmac.compare_digest(provided, token)

    @web.middleware
    async def gate(request: web.Request, handler):
        if request.method == "OPTIONS":
            return _cors(web.Response(status=204))
        path = request.path
        needs_auth = path not in (
            "/api/health", "/api/google/oauth/callback"
        ) and (path.startswith("/api") or path == "/events")
        if needs_auth and not _authorized(request):
            return _cors(web.json_response(
                {"ok": False, "error": "unauthorized"}, status=401))
        resp = await handler(request)
        # The SSE handler is already prepared (headers sent + CORS applied), so
        # don't touch it again — only decorate normal, not-yet-sent responses.
        if not getattr(resp, "prepared", False):
            _cors(resp)
        return resp

    # ----- handlers ---------------------------------------------------

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def me(_: web.Request) -> web.Response:
        snap = event_bus.latest_snapshot() if event_bus else None
        active = bool(snap and snap.get("state") == "running")
        return web.json_response({"user_id": owner_user_id, "has_active_task": active})

    async def active(_: web.Request) -> web.Response:
        snap = event_bus.latest_snapshot() if event_bus else None
        if not snap or snap.get("state") != "running":
            return web.json_response({"active": False})
        return web.json_response({"active": True, "last": snap})

    async def events(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })
        _cors(resp)
        await resp.prepare(request)
        sub = event_bus.subscribe()
        try:
            async for ev in sub:
                payload = json.dumps(ev)
                await resp.write(
                    f"event: {ev.get('type', 'message')}\ndata: {payload}\n\n".encode()
                )
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            sub.close()
        return resp

    async def tasks(request: web.Request) -> web.Response:
        try:
            limit = min(100, int(request.query.get("limit", "25")))
            offset = max(0, int(request.query.get("offset", "0")))
        except ValueError:
            limit, offset = 25, 0
        rows = await repo.list_paged(
            owner_user_id,
            launch_package=request.query.get("app") or None,
            state=request.query.get("state") or None,
            limit=limit, offset=offset,
        )
        return web.json_response({"items": [_task_dict(r) for r in rows]})

    async def task_detail(request: web.Request) -> web.Response:
        tid = int(request.match_info["id"])
        row = await repo.get_task_row(tid)
        if row is None or row.user_id != owner_user_id:
            return web.json_response({"error": "not found"}, status=404)
        steps = await repo.list_steps(tid)
        return web.json_response({
            "task": _task_dict(row),
            "steps": [_step_dict(tid, s) for s in steps],
        })

    async def screenshot(request: web.Request) -> web.Response:
        tid = int(request.match_info["id"])
        idx = int(request.match_info["idx"])
        row = await repo.get_task_row(tid)
        if row is None or row.user_id != owner_user_id or not row.artifact_dir:
            return web.Response(status=404)
        path = Path(row.artifact_dir) / f"step_{idx:02d}.png"
        if not path.is_file():
            return web.Response(status=404)
        return web.Response(body=path.read_bytes(), content_type="image/png")

    async def analytics_apps(_: web.Request) -> web.Response:
        rows = await repo.app_usage(owner_user_id)
        out = []
        for r in rows:
            meta = _app_meta(r.get("launch_package"))
            runs = r.get("run_count") or 0
            done = r.get("done_count") or 0
            out.append({
                **meta,
                "launch_package": r.get("launch_package"),
                "run_count": runs,
                "success_count": done,
                "success_rate": (done / runs) if runs else 0.0,
                "last_used_at": r.get("last_used_at"),
                "avg_steps": round(r.get("avg_steps") or 0, 1),
            })
        return web.json_response({"apps": out})

    async def analytics_summary(_: web.Request) -> web.Response:
        rows = await repo.app_usage(owner_user_id)
        total = sum((r.get("run_count") or 0) for r in rows)
        done = sum((r.get("done_count") or 0) for r in rows)
        top = max(rows, key=lambda r: r.get("run_count") or 0, default=None)
        top_meta = _app_meta(top.get("launch_package")) if top else None
        return web.json_response({
            "total_tasks": total,
            "success_rate": (done / total) if total else 0.0,
            "top_app": top_meta,
        })

    async def saved(_: web.Request) -> web.Response:
        if saved_repo is None:
            return web.json_response({"items": []})
        rows = await saved_repo.list_for(owner_user_id)
        return web.json_response({"items": [
            {"id": r.id, "slug": r.slug, "label": r.label,
             "description": r.raw_description, "run_count": r.run_count,
             "last_run_at": r.last_run_at, **_app_meta(r.launch_package)}
            for r in rows
        ]})

    async def schedules(_: web.Request) -> web.Response:
        if schedule_repo is None:
            return web.json_response({"items": []})
        rows = await schedule_repo.list_for(owner_user_id)
        items = []
        for r in rows:
            item = {
                "id": r.id, "name": r.name, "freq": r.freq, "at_minute": r.at_minute,
                "weekday": r.weekday, "day_of_month": r.day_of_month,
                "next_run_at": r.next_run_at, "last_run_at": r.last_run_at,
                "enabled": bool(r.enabled), "pay_automatically": bool(r.pay_automatically),
                "description": r.raw_description, "action_kind": r.action_kind,
                **_app_meta(r.launch_package),
            }
            if r.action_kind != "device":
                try:
                    payload = json.loads(r.payload_json or "")
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                item["payload"] = payload if isinstance(payload, dict) else {}
            items.append(item)
        return web.json_response({"items": items})

    async def approval(request: web.Request) -> web.Response:
        """Approve/deny the pending HITL request from the dashboard.

        Mirrors the Telegram Approve/Deny buttons — grants/denies the SAME gate
        the orchestrator is awaiting. No-op (applied=false) when nothing is
        pending, so a stale tap can't grant a future approval.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        decision = str((body or {}).get("decision", "")).strip().lower()
        if decision not in ("approve", "deny"):
            return web.json_response(
                {"ok": False, "error": "decision must be 'approve' or 'deny'"},
                status=400,
            )
        if hitl is None or not hitl.has_pending(owner_user_id):
            return web.json_response({"ok": True, "applied": False})
        if decision == "approve":
            hitl.grant(owner_user_id)
        else:
            hitl.deny(owner_user_id)
        return web.json_response({"ok": True, "applied": True, "decision": decision})

    async def comparisons(_: web.Request) -> web.Response:
        rows = await repo.list_recent_comparisons(owner_user_id, limit=25)
        return web.json_response({"items": [
            {"id": r.id, "query": r.query, "category": r.category,
             "ranking_key": r.ranking_key, "winner_app_id": r.winner_app_id,
             "chosen_app_id": r.chosen_app_id, "created_at": r.created_at,
             "ordered_at": r.ordered_at, "quotes": r.quotes()}
            for r in rows
        ]})

    async def google_status(_: web.Request) -> web.Response:
        if google_auth is None:
            return web.json_response({
                "configured": False, "connected": False,
                "email": None, "scopes": [], "connected_at": None,
            })
        account = google_auth.status()
        return web.json_response({
            "configured": bool(google_auth.configured),
            "connected": account is not None,
            "email": account.email if account else None,
            "scopes": list(account.scopes) if account else [],
            "connected_at": account.connected_at if account else None,
        })

    async def google_connect(_: web.Request) -> web.Response:
        if google_auth is None or not google_auth.configured:
            return web.json_response(
                {"ok": False, "error": "google not configured"}, status=400)
        return web.json_response({"auth_url": google_auth.build_auth_url(oauth_state)})

    async def google_callback(request: web.Request) -> web.Response:
        """Google's redirect target — the one auth-exempt route besides health.

        The HMAC state (minted by /api/google/connect, round-tripped through
        Google) is the gate here: a mismatch means the flow wasn't started from
        an authed dashboard page, so reject before touching the code.
        """
        if google_auth is None:
            return web.json_response(
                {"ok": False, "error": "google not configured"}, status=400)
        state = request.query.get("state", "")
        if not hmac.compare_digest(state, oauth_state):
            return web.json_response(
                {"ok": False, "error": "bad state"}, status=403)
        code = request.query.get("code", "")
        if not code:
            return web.json_response(
                {"ok": False, "error": "missing code"}, status=400)
        try:
            await google_auth.exchange_code(code)
        except Exception:
            log.exception("Google OAuth code exchange failed")
            raise web.HTTPFound("/settings?google=error") from None
        raise web.HTTPFound("/settings?google=connected")

    async def google_disconnect(_: web.Request) -> web.Response:
        if google_auth is not None:
            google_auth.disconnect()
        return web.json_response({"ok": True})

    async def cloud_actions(_: web.Request) -> web.Response:
        if cloud_repo is None:
            return web.json_response({"items": []})
        rows = await cloud_repo.list_for(owner_user_id)
        return web.json_response({"items": [
            {"id": r.id, "kind": r.kind, "status": r.status, "error": r.error,
             "created_at": r.created_at, "payload": r.payload(),
             "result": r.result()}
            for r in rows
        ]})

    async def contacts_list(_: web.Request) -> web.Response:
        if contacts_repo is None:
            return web.json_response({"items": []})
        rows = await contacts_repo.list_for(owner_user_id)
        return web.json_response({"items": [
            {"id": r.id, "name": r.name, "email": r.email,
             "created_at": r.created_at}
            for r in rows
        ]})

    async def contacts_add(request: web.Request) -> web.Response:
        if contacts_repo is None:
            return web.json_response(
                {"ok": False, "error": "contacts unavailable"}, status=503)
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = str((body or {}).get("name", "")).strip()
        email = str((body or {}).get("email", "")).strip()
        if not name or "@" not in email:
            return web.json_response(
                {"ok": False, "error": "name and a valid email are required"},
                status=400,
            )
        await contacts_repo.upsert(user_id=owner_user_id, name=name, email=email)
        return web.json_response({"ok": True})

    async def contacts_delete(request: web.Request) -> web.Response:
        if contacts_repo is None:
            return web.json_response({"ok": False, "deleted": False})
        deleted = await contacts_repo.delete(
            owner_user_id, request.match_info["name"]
        )
        return web.json_response({"ok": True, "deleted": deleted})

    async def command(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = str((body or {}).get("text", "")).strip()
        if not text:
            return web.json_response(
                {"ok": False, "error": "text must be non-empty"}, status=400)
        if trigger is None:
            return web.json_response(
                {"ok": False, "message": "command runner unavailable"})
        message = await trigger.handle_external_run(owner_user_id, text)
        return web.json_response({"ok": True, "message": message})

    app = web.Application(middlewares=[gate])
    r = app.router
    r.add_get("/api/health", health)
    r.add_get("/api/me", me)
    r.add_get("/api/active", active)
    r.add_get("/events", events)
    r.add_get("/api/tasks", tasks)
    r.add_get("/api/tasks/{id}", task_detail)
    r.add_get("/api/tasks/{id}/steps/{idx}/screenshot", screenshot)
    r.add_get("/api/analytics/apps", analytics_apps)
    r.add_get("/api/analytics/summary", analytics_summary)
    r.add_get("/api/saved", saved)
    r.add_get("/api/schedules", schedules)
    r.add_get("/api/comparisons", comparisons)
    r.add_post("/api/approval", approval)
    r.add_get("/api/google/status", google_status)
    r.add_post("/api/google/connect", google_connect)
    r.add_get("/api/google/oauth/callback", google_callback)
    r.add_post("/api/google/disconnect", google_disconnect)
    r.add_get("/api/cloud-actions", cloud_actions)
    r.add_get("/api/contacts", contacts_list)
    r.add_post("/api/contacts", contacts_add)
    r.add_delete("/api/contacts/{name}", contacts_delete)
    r.add_post("/api/command", command)

    # Serve the built SPA (single process) when present. Unknown non-/api paths
    # fall back to index.html for client-side routing.
    if dist_dir is not None and Path(dist_dir).is_dir():
        dist = Path(dist_dir)
        index = dist / "index.html"

        async def spa(request: web.Request) -> web.Response:
            rel = request.match_info.get("tail", "")
            candidate = (dist / rel) if rel else index
            if candidate.is_file() and candidate.suffix:
                return web.FileResponse(candidate)
            return web.FileResponse(index)

        r.add_get("/", spa)
        r.add_get("/{tail:.*}", spa)

    return app
