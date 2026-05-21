"""Translates Claude's action output to ADB commands."""
from __future__ import annotations

import asyncio

from device.adb_controller import AdbController


class ActionExecutionError(RuntimeError):
    """Raised when an action cannot be dispatched."""


async def execute(action: dict, adb: AdbController) -> str:
    """Dispatch a validated action via ADB. Returns a short result string."""
    action_type = action.get("action")

    if action_type == "tap":
        x, y = int(action["x"]), int(action["y"])
        await adb.tap(x, y)
        return f"tapped ({x},{y})"

    if action_type == "type":
        text = str(action["text"])
        await adb.type_text(text)
        return f"typed {len(text)} char(s)"

    if action_type == "swipe":
        x1, y1 = int(action["x1"]), int(action["y1"])
        x2, y2 = int(action["x2"]), int(action["y2"])
        duration = int(action.get("duration_ms", 300))
        await adb.swipe(x1, y1, x2, y2, duration)
        return f"swiped ({x1},{y1})->({x2},{y2}) in {duration}ms"

    if action_type == "wait":
        await asyncio.sleep(1.0)
        return "waited 1s"

    if action_type in ("done", "need_approval"):
        # Terminal / control actions — handled by orchestrator, not dispatched to the device.
        return action_type

    raise ActionExecutionError(f"unknown action type: {action_type!r}")
