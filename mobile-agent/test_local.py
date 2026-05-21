"""Local smoke test: ADB → screenshot → get_next_action. No Telegram, no orchestrator."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.computer_use import get_next_action
from device.adb_controller import AdbController
from device.emulator import check_emulator_running


async def main() -> int:
    load_dotenv()

    print("[1/3] Checking ADB for emulator...")
    try:
        emulators = await check_emulator_running()
    except RuntimeError as e:
        print(f"  FAIL: {e}")
        return 1
    print(f"  OK — emulators: {emulators}")

    target = os.environ.get("ANDROID_DEVICE_ID")
    if target and target in emulators:
        device_id = target
    else:
        device_id = emulators[0]
        if target:
            print(f"  note: ANDROID_DEVICE_ID={target!r} not running; using {device_id}")

    adb = AdbController(device_id)

    print(f"[2/3] Taking screenshot from {device_id}...")
    png = await adb.screencap()
    out = Path("test_screenshot.png")
    out.write_bytes(png)
    print(f"  OK — saved {out} ({len(png)} bytes)")

    if not os.environ.get("GEMINI_API_KEY"):
        print("[3/3] SKIP — GEMINI_API_KEY not set; cannot call get_next_action()")
        return 0

    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    print(f"[3/3] Calling get_next_action(task='open the search bar') via {model}...")
    action = await get_next_action(
        screenshot_bytes=png,
        task_description="open the search bar",
        step_history=[],
    )
    print(f"  OK — action: {action}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
