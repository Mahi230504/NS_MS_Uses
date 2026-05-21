"""iOS device control via idb (Facebook's iOS Development Bridge).

idb gives us the same primitives as adb — tap, swipe, type, screenshot —
on iOS Simulators (and on real iPhones if WebDriverAgent is installed via
Xcode). Install on macOS with:

    brew tap facebook/fb
    brew install idb-companion
    pip install fb-idb

The controller targets a single UDID at a time. List targets with
    idb list-targets

Real-iPhone notes: idb still works, but the device must have WDA installed
and trusted — that's a one-time Xcode + Apple Developer setup outside the
scope of this controller.
"""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from device.base import DeviceError


# `idb describe --udid <id> --json` returns dict with screen_dimensions =
# {"width": int, "height": int, "density": float} on simulators.
_SIM_BUNDLE_RE = re.compile(
    r"\"bundle_id\"\s*:\s*\"([\w.\-]+)\".*?\"foregrounded\"\s*:\s*true",
    re.DOTALL,
)


class IdbError(DeviceError):
    """Raised when an idb command exits non-zero."""


class IdbController:
    """Async wrappers around `idb` for a single bound device/simulator."""

    def __init__(self, udid: str) -> None:
        self._udid = udid
        self._screen_size: tuple[int, int] | None = None

    def get_device_id(self) -> str:
        return self._udid

    async def _run(self, *args: str) -> bytes:
        try:
            proc = await asyncio.create_subprocess_exec(
                "idb",
                *args,
                "--udid",
                self._udid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise IdbError(
                "`idb` not found on PATH. Install: "
                "`brew tap facebook/fb && brew install idb-companion && "
                "pip install fb-idb`."
            )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise IdbError(
                f"idb {' '.join(args)} failed (exit {proc.returncode}): {err}"
            )
        return stdout

    async def screencap(self) -> bytes:
        # idb writes the screenshot to a file path; capture into a tempfile,
        # read, unlink. Stdout-pipe isn't supported by `idb screenshot`.
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp = Path(f.name)
        try:
            await self._run("screenshot", str(tmp))
            return tmp.read_bytes()
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    async def tap(self, x: int, y: int) -> None:
        await self._run("ui", "tap", str(x), str(y))

    async def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int
    ) -> None:
        # idb wants duration in SECONDS as a float.
        duration_s = max(0.05, duration_ms / 1000.0)
        await self._run(
            "ui",
            "swipe",
            "--duration",
            f"{duration_s:.3f}",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
        )

    async def type_text(self, text: str) -> None:
        # idb forwards text to the keyboard focused field. Unlike adb's input
        # text, idb handles spaces and most special chars natively; we still
        # pass it as a single argument so the shell doesn't split it.
        await self._run("ui", "text", text)

    async def recover(self) -> None:
        """Press the home button — drops back to springboard, escaping any
        stuck modal or app. The closest analog to Android's KEYCODE_BACK in
        a platform that lacks a system back button.
        """
        await self._run("ui", "button", "HOME")

    async def get_screen_size(self) -> tuple[int, int]:
        """Return (width, height) in pixels. Cached after first call."""
        if self._screen_size is not None:
            return self._screen_size
        out = await self._run("describe", "--json")
        try:
            info = json.loads(out.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise IdbError(f"could not parse `idb describe` output: {e}")
        dims = info.get("screen_dimensions") or {}
        try:
            w = int(dims["width"])
            h = int(dims["height"])
        except (KeyError, TypeError, ValueError) as e:
            raise IdbError(f"missing screen_dimensions in idb describe: {e}")
        self._screen_size = (w, h)
        return self._screen_size

    async def get_foreground_package(self) -> str | None:
        """Return the bundle ID of the foregrounded app, or None.

        iOS doesn't expose this as cleanly as Android — we ask idb for the
        list of installed-and-running apps and pick the foregrounded one.
        Returns None on simulators without a focused app (e.g. springboard).
        """
        try:
            out = await self._run("list-apps", "--json")
        except IdbError:
            return None
        text = out.decode("utf-8", errors="replace")
        m = _SIM_BUNDLE_RE.search(text)
        return m.group(1) if m else None
