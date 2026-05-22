"""tap, swipe, type, screencap, key_event wrappers."""
from __future__ import annotations

import asyncio
import base64
import re

from device.base import DeviceError


_WM_SIZE_RE = re.compile(r"(\d+)x(\d+)")
# Special shell metacharacters that `adb shell input text` and the surrounding
# shell mangle if left alone. We backslash-escape these on the way through.
_INPUT_TEXT_ESCAPE = ("\\", "'", '"', "&", ";", "<", ">", "(", ")", "|", "*", "?", "$", "`", "[", "]")
_ADBKEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
# `dumpsys window` prints mCurrentFocus like:
#   mCurrentFocus=Window{abc123 u0 com.blinkit.markets/.MainActivity}
# We pull the package out of the slash-separated component name.
_CURRENT_FOCUS_RE = re.compile(r"mCurrentFocus=Window\{[^}]*\s+([\w.]+)/")

# Android KEYCODE_BACK; used by the recovery path in the orchestrator.
KEYCODE_BACK = 4


class AdbError(DeviceError):
    """Raised when an adb command exits non-zero."""


class AdbController:
    """Async wrappers around `adb` for a single bound device."""

    def __init__(self, device_id: str) -> None:
        self._device_id = device_id
        self._screen_size: tuple[int, int] | None = None
        self._adbkeyboard_active: bool | None = None

    def get_device_id(self) -> str:
        return self._device_id

    async def _run(self, *args: str) -> bytes:
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb",
                "-s",
                self._device_id,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise AdbError(
                "`adb` not found on PATH. Install Android platform-tools and retry."
            )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise AdbError(
                f"adb {' '.join(args)} failed (exit {proc.returncode}): {err}"
            )
        return stdout

    async def screencap(self) -> bytes:
        # exec-out + `screencap -p` returns raw PNG bytes without CRLF translation.
        return await self._run("exec-out", "screencap", "-p")

    async def tap(self, x: int, y: int) -> None:
        await self._run("shell", "input", "tap", str(x), str(y))

    async def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int
    ) -> None:
        await self._run(
            "shell",
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
        )

    async def type_text(self, text: str) -> None:
        """Send text to the device's focused input field.

        Path 1 (preferred): if com.android.adbkeyboard is the active IME, send
        the text base64-encoded via its ADB_INPUT_B64 broadcast — handles all
        unicode, spaces, punctuation cleanly.
        Path 2 (fallback): `adb shell input text` with shell-escaping. Spaces
        map to %s (input's own token-separator escape); other shell-special
        chars are backslash-escaped to survive the shell layer.
        """
        if await self._adbkeyboard_is_default():
            payload = base64.standard_b64encode(text.encode("utf-8")).decode("ascii")
            await self._run(
                "shell",
                "am",
                "broadcast",
                "-a",
                "ADB_INPUT_B64",
                "--es",
                "msg",
                payload,
            )
            return

        await self._run("shell", "input", "text", _escape_for_input_text(text))

    async def key_event(self, keycode: int) -> None:
        await self._run("shell", "input", "keyevent", str(keycode))

    async def recover(self) -> None:
        """Back-button — the standard "undo whatever I just did" gesture."""
        await self.key_event(KEYCODE_BACK)

    async def launch_package(self, package: str) -> str:
        """Bring an app to the foreground deterministically.

        Tries the hardcoded package first. If that fails (wrong variant,
        regional rename), greps the installed package list for a close match
        and tries that. Returns the package name that actually launched, or
        raises AdbError if nothing worked.
        """
        try:
            await self._run(
                "shell", "monkey", "-p", package, "-c",
                "android.intent.category.LAUNCHER", "1",
            )
            return package
        except AdbError:
            pass
        # Fallback: search the installed package list for a partial match on
        # the most distinctive segment (e.g. "blinkit" out of com.blinkit.markets).
        stem = _package_stem(package)
        try:
            out = await self._run("shell", "pm", "list", "packages")
        except AdbError:
            raise AdbError(
                f"launch_package({package!r}) failed and `pm list packages` is unavailable."
            )
        installed = _parse_pm_list(out.decode("utf-8", errors="replace"))
        for candidate in installed:
            if stem in candidate.lower():
                try:
                    await self._run(
                        "shell", "monkey", "-p", candidate, "-c",
                        "android.intent.category.LAUNCHER", "1",
                    )
                    return candidate
                except AdbError:
                    continue
        raise AdbError(
            f"Could not launch {package!r} — not installed and no close match "
            f"found in the installed package list."
        )

    async def get_screen_size(self) -> tuple[int, int]:
        """Return (width, height) in pixels. Cached after the first call."""
        if self._screen_size is not None:
            return self._screen_size
        out = (await self._run("shell", "wm", "size")).decode("utf-8", errors="replace")
        # `wm size` may print both "Physical size" and "Override size" lines;
        # the last match (Override) reflects what apps actually see.
        matches = _WM_SIZE_RE.findall(out)
        if not matches:
            raise AdbError(f"could not parse `wm size` output: {out!r}")
        w, h = matches[-1]
        self._screen_size = (int(w), int(h))
        return self._screen_size

    async def get_foreground_package(self) -> str | None:
        """Return the package name of the foregrounded activity, or None.

        Uses `dumpsys window`'s mCurrentFocus line. Returns None if parsing
        fails so callers can degrade gracefully (e.g. no skill injection).
        """
        try:
            out = (await self._run("shell", "dumpsys", "window")).decode(
                "utf-8", errors="replace"
            )
        except AdbError:
            return None
        m = _CURRENT_FOCUS_RE.search(out)
        return m.group(1) if m else None

    async def _adbkeyboard_is_default(self) -> bool:
        if self._adbkeyboard_active is not None:
            return self._adbkeyboard_active
        try:
            out = (
                await self._run(
                    "shell", "settings", "get", "secure", "default_input_method"
                )
            ).decode("utf-8", errors="replace").strip()
        except AdbError:
            self._adbkeyboard_active = False
            return False
        self._adbkeyboard_active = out == _ADBKEYBOARD_IME
        return self._adbkeyboard_active


def _package_stem(package: str) -> str:
    """The most distinctive lowercase segment of a package name.

    For com.blinkit.markets → "blinkit". Falls back to the last segment if
    no segment is clearly distinctive.
    """
    parts = [p for p in package.lower().split(".") if p not in ("com", "in", "io", "net", "org", "app", "android")]
    if not parts:
        return package.lower().rsplit(".", 1)[-1]
    # Heuristic: the longest remaining segment is the brand most of the time.
    return max(parts, key=len)


def _parse_pm_list(output: str) -> list[str]:
    """Parse `pm list packages` output ('package:com.foo.bar' per line)."""
    pkgs: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("package:"):
            pkgs.append(line[len("package:"):])
    return pkgs


def _escape_for_input_text(text: str) -> str:
    """Make `text` safe for `adb shell input text <text>`.

    `input text` parses spaces as token separators; %s is its escape for a
    literal space. The surrounding shell then eats backslashes and quotes
    unless they're escaped — so backslash-escape any shell metacharacters
    before substituting spaces.
    """
    out = []
    for ch in text:
        if ch in _INPUT_TEXT_ESCAPE:
            out.append("\\" + ch)
        elif ch == " ":
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)
