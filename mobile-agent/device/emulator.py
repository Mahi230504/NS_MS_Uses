"""Check emulator is running, get device ID."""
from __future__ import annotations

import asyncio


class EmulatorError(RuntimeError):
    """Raised when the device check fails the emulator-only safety rule."""


async def check_emulator_running() -> list[str]:
    """Return the list of running emulator device IDs.

    Enforces the emulator-only safety rule:
      - raises EmulatorError if any physical device is attached
      - raises EmulatorError if no emulator is attached
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "adb",
            "devices",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise EmulatorError(
            "`adb` not found on PATH. Install Android platform-tools "
            "(`brew install --cask android-platform-tools` on macOS) and retry."
        )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise EmulatorError(f"`adb devices` failed (exit {proc.returncode}): {err}")

    emulators: list[str] = []
    physical: list[str] = []
    lines = stdout.decode("utf-8", errors="replace").splitlines()
    # First line is the "List of devices attached" header.
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        device_id, status = parts[0], parts[1]
        if status != "device":
            continue  # offline / unauthorized / no permissions
        if device_id.startswith("emulator-"):
            emulators.append(device_id)
        else:
            physical.append(device_id)

    if physical:
        raise EmulatorError(
            f"Refusing to run: physical device(s) attached: {physical}. "
            "This agent is emulator-only for safety."
        )
    if not emulators:
        raise EmulatorError(
            "No Android emulator detected. Start one (e.g. `emulator -avd <name>`) and retry."
        )
    return emulators
