"""Tests for the dashboard read/stream API."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.persistence import (
    SavedTaskRepository,
    ScheduleRepository,
    TaskRepository,
)
from agent.state_machine import Task, TaskState
from bot.dashboard_api import build_dashboard_app
from bot.events import EventBus

TOKEN = "dash-secret"
OWNER = 42
PKG = "com.grofers.customerapp"


@pytest.fixture
async def ctx(tmp_path):
    db = tmp_path / "t.db"
    base = TaskRepository(db)
    await base.initialize()

    artdir = tmp_path / "art"
    artdir.mkdir()
    (artdir / "step_01.png").write_bytes(b"PNGBYTES")

    task = Task(
        user_id=OWNER, description="Add milk to the cart",
        state=TaskState.RUNNING, launch_package=PKG, artifact_dir=str(artdir),
    )
    tid = await base.insert_task(task)
    await base.append_step(
        tid, 1, {"action": "tap", "x": 10, "y": 20, "note": "tap ADD"}, "tapped (10,20)"
    )
    task.state = TaskState.DONE
    task.final_summary = "added"
    await base.update_state(tid, task)

    saved = SavedTaskRepository(db)
    await saved.upsert(
        user_id=OWNER, slug="sunday", label="Sunday order",
        raw_description="add milk", launch_package=PKG,
    )
    sched = ScheduleRepository(db)
    await sched.insert(
        user_id=OWNER, name="Morning", freq="daily", at_minute=540,
        tz="Asia/Kolkata", raw_description="add milk",
        next_run_at="2030-01-01T00:00:00+00:00", launch_package=PKG,
    )
    await base.insert_comparison(
        user_id=OWNER, query="milk", category="groceries", ranking_key="cheapest",
        quotes=[{"app_id": "blinkit", "price": 99}], winner_app_id="blinkit",
    )

    bus = EventBus()
    app = build_dashboard_app(
        repo=base, saved_repo=saved, schedule_repo=sched, event_bus=bus,
        token=TOKEN, owner_user_id=OWNER, dist_dir=None,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield SimpleNamespace(client=client, bus=bus, tid=tid)
    finally:
        await client.close()


def _q(token: str = TOKEN) -> dict:
    return {"token": token}


class TestAuth:
    async def test_health_open(self, ctx) -> None:
        r = await ctx.client.get("/api/health")
        assert r.status == 200 and (await r.json())["ok"] is True

    async def test_api_requires_token(self, ctx) -> None:
        r = await ctx.client.get("/api/tasks")
        assert r.status == 401

    async def test_bearer_header_works(self, ctx) -> None:
        r = await ctx.client.get(
            "/api/tasks", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert r.status == 200

    async def test_query_token_works(self, ctx) -> None:
        r = await ctx.client.get("/api/tasks", params=_q())
        assert r.status == 200

    async def test_options_preflight(self, ctx) -> None:
        r = await ctx.client.options("/api/tasks")
        assert r.status == 204
        assert "Access-Control-Allow-Origin" in r.headers


class TestEndpoints:
    async def test_tasks_lists_with_app_meta(self, ctx) -> None:
        r = await ctx.client.get("/api/tasks", params=_q())
        items = (await r.json())["items"]
        assert len(items) == 1
        assert items[0]["app_name"] == "Blinkit" and items[0]["emoji"] == "🛒"
        assert items[0]["duration_seconds"] is not None

    async def test_tasks_app_filter(self, ctx) -> None:
        hit = await (await ctx.client.get("/api/tasks", params={**_q(), "app": PKG})).json()
        miss = await (await ctx.client.get("/api/tasks", params={**_q(), "app": "com.nope"})).json()
        assert len(hit["items"]) == 1 and miss["items"] == []

    async def test_task_detail_steps(self, ctx) -> None:
        r = await ctx.client.get(f"/api/tasks/{ctx.tid}", params=_q())
        body = await r.json()
        assert body["task"]["id"] == ctx.tid
        step = body["steps"][0]
        assert step["coords"]["tap"] == {"x": 10, "y": 20}
        assert step["screenshot_url"].endswith(f"/api/tasks/{ctx.tid}/steps/1/screenshot")

    async def test_task_detail_not_found(self, ctx) -> None:
        r = await ctx.client.get("/api/tasks/9999", params=_q())
        assert r.status == 404

    async def test_screenshot_served(self, ctx) -> None:
        r = await ctx.client.get(
            f"/api/tasks/{ctx.tid}/steps/1/screenshot", params=_q()
        )
        assert r.status == 200 and r.headers["Content-Type"] == "image/png"
        assert await r.read() == b"PNGBYTES"

    async def test_screenshot_missing_404(self, ctx) -> None:
        r = await ctx.client.get(
            f"/api/tasks/{ctx.tid}/steps/9/screenshot", params=_q()
        )
        assert r.status == 404

    async def test_analytics_apps(self, ctx) -> None:
        apps = (await (await ctx.client.get("/api/analytics/apps", params=_q())).json())["apps"]
        blink = next(a for a in apps if a["launch_package"] == PKG)
        assert blink["run_count"] == 1 and blink["success_count"] == 1

    async def test_active_false_when_idle(self, ctx) -> None:
        body = await (await ctx.client.get("/api/active", params=_q())).json()
        assert body["active"] is False

    async def test_active_true_with_running_snapshot(self, ctx) -> None:
        ctx.bus.publish({"type": "state", "state": "running", "step": 3})
        body = await (await ctx.client.get("/api/active", params=_q())).json()
        assert body["active"] is True and body["last"]["step"] == 3

    async def test_saved_schedules_comparisons(self, ctx) -> None:
        saved = (await (await ctx.client.get("/api/saved", params=_q())).json())["items"]
        sched = (await (await ctx.client.get("/api/schedules", params=_q())).json())["items"]
        comps = (await (await ctx.client.get("/api/comparisons", params=_q())).json())["items"]
        assert saved[0]["label"] == "Sunday order"
        assert sched[0]["freq"] == "daily" and sched[0]["app_name"] == "Blinkit"
        assert comps[0]["winner_app_id"] == "blinkit" and comps[0]["quotes"]
