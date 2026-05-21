"""Check that an iOS Simulator is running, get UDIDs.

Enforces the simulator-only safety rule (analogous to emulator.py for Android):
real iPhones are refused even if paired, because they require WebDriverAgent
+ Apple Developer signing that this project deliberately does not address.
"""
from __future__ import annotations

import asyncio
import json


class SimulatorError(RuntimeError):
    """Raised when the device check fails the simulator-only safety rule."""


async def check_simulator_running() -> list[str]:
    """Return the list of booted iOS Simulator UDIDs.

    Raises:
      - SimulatorError if `idb` is not installed
      - SimulatorError if a physical iPhone is paired (safety rule)
      - SimulatorError if no booted simulator is found
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "idb",
            "list-targets",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise SimulatorError(
            "`idb` not found on PATH. Install with `brew tap facebook/fb && "
            "brew install idb-companion && pip install fb-idb` and retry."
        )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise SimulatorError(
            f"`idb list-targets` failed (exit {proc.returncode}): {err}"
        )

    simulators: list[str] = []
    physical: list[str] = []
    # idb prints one JSON object per line.
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("state", "").lower() != "booted":
            continue
        udid = entry.get("udid")
        if not udid:
            continue
        target_type = (entry.get("type") or "").lower()
        if target_type == "simulator":
            simulators.append(udid)
        else:
            physical.append(udid)

    if physical:
        raise SimulatorError(
            f"Refusing to run: physical device(s) attached: {physical}. "
            "This agent is simulator-only for safety (real iPhones need "
            "WebDriverAgent + Apple Developer setup, deliberately not supported)."
        )
    if not simulators:
        raise SimulatorError(
            "No iOS Simulator detected. Boot one (Xcode → Open Developer Tool "
            "→ Simulator, or `xcrun simctl boot <udid>`) and retry."
        )
    return simulators
