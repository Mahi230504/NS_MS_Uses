"""tap, swipe, type, screencap, key_event wrappers."""
from __future__ import annotations

import asyncio
import base64
import logging
import re

from device.base import DeviceError


_log = logging.getLogger("mobile_agent.adb")

# Per-call adb timeout. Real-world adb commands finish in <2s on a healthy
# device; if one is still pending after 25s we assume the daemon, USB link,
# or device is stuck and bail rather than freezing the whole agent loop for
# the full session timeout. The orchestrator catches AdbError and retries.
_ADB_CALL_TIMEOUT_SECONDS = 25.0
# `uiautomator dump` gets a much shorter timeout. A dump that's going to
# succeed returns in ~2-3s; if it hasn't returned by 8s it's stuck waiting
# for an idle state that never comes (Blinkit's cart/product screens animate
# continuously) and would only hang to the full 25s. The dump_ui_xml hybrid
# does up to three passes, so capping each at 8s bounds the worst case at
# ~24s instead of ~75s — the difference between "one slow step" and "the
# whole session times out with socket-closed errors".
_DUMP_TIMEOUT_SECONDS = 8.0

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

    async def _run(self, *args: str, timeout: float = _ADB_CALL_TIMEOUT_SECONDS) -> bytes:
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
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            # Subprocess is still alive — terminate it so we don't leak a
            # zombie adb process. SIGTERM first, then SIGKILL if it ignores.
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
            _log.warning("adb %s timed out after %.0fs", " ".join(args), timeout)
            raise AdbError(
                f"adb {' '.join(args)} timed out after {timeout:.0f}s "
                "(device may be sleeping, USB unstable, or daemon stuck)"
            )
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

        If ADBKeyboard is the currently active IME (which the orchestrator
        ensures during a task via use_adbkeyboard_for_task), we deliver via
        the ADB_INPUT_B64 broadcast — robust against any focus race because
        commitText goes to whichever EditText currently holds focus through
        the existing InputConnection.

        If ADBKeyboard isn't active, we don't try to swap it in mid-typing —
        that mid-task swap is unreliable on some OEMs because the
        InputConnection drops during the IME change and may not re-establish
        before the broadcast lands. Instead, fall back to `adb shell input
        text` and accept the focus-race risk.
        """
        current = await self._current_ime()
        if current == _ADBKEYBOARD_IME:
            payload = base64.standard_b64encode(text.encode("utf-8")).decode("ascii")
            await self._run(
                "shell", "am", "broadcast", "-a", "ADB_INPUT_B64",
                "--es", "msg", payload,
            )
            return
        await self._run("shell", "input", "text", _escape_for_input_text(text))

    async def use_adbkeyboard_for_task(self) -> str | None:
        """Switch the default IME to ADBKeyboard for the duration of a task.

        Returns the original IME id (so the caller can restore it on task
        exit), or None if ADBKeyboard isn't enabled / switching failed.
        Idempotent: calling when already on ADBKeyboard is a no-op that
        returns None (nothing to restore).
        """
        if not await self._adbkeyboard_is_enabled():
            return None
        original = await self._current_ime()
        if original == _ADBKEYBOARD_IME or not original:
            return None
        try:
            await self._run("shell", "ime", "set", _ADBKEYBOARD_IME)
        except AdbError:
            return None
        # Brief wait so the IME swap is fully active before the agent loop
        # starts tapping into text fields.
        await asyncio.sleep(0.5)
        return original

    async def restore_ime(self, ime_id: str | None) -> None:
        """Restore the IME previously captured by use_adbkeyboard_for_task.

        If `ime_id` is None (e.g., we couldn't capture the original, or
        ADBKeyboard was already active before the task started), falls
        through to `force_off_adbkeyboard` so the device doesn't stay on
        ADBKeyboard forever — the user's normal keyboard always wins on
        task exit regardless of what we captured at start.
        """
        if not ime_id:
            await self.force_off_adbkeyboard()
            return
        try:
            await self._run("shell", "ime", "set", ime_id)
        except AdbError:
            # Captured IME failed to apply (e.g., it was uninstalled
            # mid-task). Fall back to any other enabled IME.
            await self.force_off_adbkeyboard()

    async def force_off_adbkeyboard(self) -> None:
        """Switch IME to any non-ADBKeyboard enabled IME, if needed.

        No-op when ADBKeyboard isn't the current IME. Otherwise lists all
        enabled IMEs and picks the first one that isn't ADBKeyboard. This
        is the safety net for:
          1. Crashed prior bot runs (Ctrl+C / kill / OS reboot before our
             `finally` block ran) — called on bot startup to clean up.
          2. In-process restoration when the original IME wasn't captured
             because ADBKeyboard was already active at task start, or the
             query failed.

        Best-effort: any adb hiccup is swallowed — never raises.
        """
        try:
            current = await self._current_ime()
        except Exception:
            return
        if current != _ADBKEYBOARD_IME:
            return
        try:
            enabled_raw = await self._run("shell", "ime", "list", "-s")
        except AdbError:
            return
        enabled = [
            line.strip()
            for line in enabled_raw.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        fallback = next(
            (ime for ime in enabled if ime != _ADBKEYBOARD_IME),
            None,
        )
        if fallback is None:
            # No alternative enabled. We could try `ime reset` here but
            # that requires the right permission. Leave it; user can
            # manually switch in Settings.
            return
        try:
            await self._run("shell", "ime", "set", fallback)
        except AdbError:
            pass

    async def key_event(self, keycode: int) -> None:
        await self._run("shell", "input", "keyevent", str(keycode))

    async def reverse_tcp(self, port: int) -> bool:
        """Map the device's localhost:<port> to the host's localhost:<port>.

        Lets an on-device trigger (e.g. an HTTP Shortcuts POST) reach the
        agent's webhook over the USB cable, with nothing exposed on any
        network. Best-effort: returns True on success, False on any adb
        failure — never raises, so a missing reverse can't block startup.
        """
        try:
            await self._run("reverse", f"tcp:{port}", f"tcp:{port}")
            return True
        except AdbError:
            _log.warning("adb reverse tcp:%d failed; on-device trigger won't reach the webhook", port)
            return False

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

    async def dump_ui_xml(self) -> str | None:
        """Return the on-screen UI hierarchy as XML, or None on failure.

        `uiautomator dump` is Android's accessibility-tree export. It produces
        an XML document where each <node> carries text, content-desc,
        resource-id, class, bounds, and clickability — far more precise than
        what a vision model can extract from pixels. Feeding this alongside
        the screenshot is the single biggest reliability win for grounding.

        Hybrid strategy. The default `uiautomator dump` (no flags) produces
        a *complete* tree but requires the screen to be "idle" — it errors
        out with "could not get idle state" on apps with constant
        animations (banners, carousels, skeleton loaders). The
        `--compressed` variant bypasses the idle check, but on real
        devices it has been observed to STRIP elements we care about
        (specifically: EditText on Blinkit's search screen disappears
        from the compressed dump, even though it's visible in the
        screenshot and the user can tap it). So:

          1. Try the normal dump first → best case, get a clean complete
             tree with all EditTexts/SearchViews present.
          2. If normal returns nothing (idle-state error), retry once
             after a 250ms pause — small animations may have finished.
          3. If normal still fails, fall back to `--compressed` so we at
             least get *something* the model can act on. The tree may be
             missing inputs, but it'll still have ADD buttons / nav
             elements, which is better than tapping blind.

        Returns None only if all attempts fail (e.g., OEM strips
        uiautomator entirely).
        """
        # Pass 1+2: normal dump (with one retry). Best fidelity.
        for attempt in range(2):
            text = await self._try_dump(compressed=False)
            extracted = self._extract_hierarchy(text)
            if extracted is not None:
                return extracted
            if attempt == 0:
                await asyncio.sleep(0.25)
        # Pass 3: compressed fallback. Lower fidelity (missing some
        # element types) but works on non-idle screens.
        text = await self._try_dump(compressed=True)
        return self._extract_hierarchy(text)

    async def _try_dump(self, *, compressed: bool) -> str:
        """Run one uiautomator dump invocation, return decoded output.

        Each invocation spawns a ~110MB `uiautomator` process on the device.
        If a previous invocation hung or got OOM-killed, its pid lingers and
        the next dump returns "Killed" (because Android refuses to launch
        another while one is still around or because OOM-killer reaped the
        new one to keep memory low).

        Mitigation: best-effort `killall uiautomator` before each dump so
        only one ever exists at a time. Real fix for the long-running case
        would be to use UIAutomator2 (server APK), but that requires an
        install step we'd rather avoid.
        """
        await self._kill_stale_uiautomator()
        args = ["exec-out", "uiautomator", "dump"]
        if compressed:
            args.append("--compressed")
        args.append("/dev/tty")
        try:
            out = await self._run(*args, timeout=_DUMP_TIMEOUT_SECONDS)
        except AdbError:
            return ""
        return out.decode("utf-8", errors="replace").strip()

    async def _kill_stale_uiautomator(self) -> None:
        """Reap any leftover uiautomator processes before spawning a new one.

        Errors are swallowed: `killall` returns non-zero when no matching
        process exists (the normal case on a fresh device), and we don't
        care if the kill itself fails — the worst outcome is a stuck dump,
        which is what we already have.
        """
        try:
            await self._run("shell", "killall", "uiautomator")
        except AdbError:
            pass

    @staticmethod
    def _extract_hierarchy(text: str) -> str | None:
        """Pull the <?xml ... </hierarchy> chunk out of raw dump output.

        uiautomator prepends "ERROR: could not get idle state." in failure
        cases (and sometimes appends "UI hierchary dumped to: ..." after
        success). Locate the XML by tag, not by line index.
        """
        if not text:
            return None
        start = text.find("<?xml")
        if start == -1:
            start = text.find("<hierarchy")
        end = text.rfind("</hierarchy>")
        if start == -1 or end == -1 or end <= start:
            return None
        return text[start : end + len("</hierarchy>")]

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

    async def _adbkeyboard_is_enabled(self) -> bool:
        """Cached: is ADBKeyboard listed as an enabled IME?"""
        if self._adbkeyboard_active is not None:
            return self._adbkeyboard_active
        try:
            out = (await self._run("shell", "ime", "list", "-s")).decode(
                "utf-8", errors="replace"
            )
        except AdbError:
            self._adbkeyboard_active = False
            return False
        self._adbkeyboard_active = _ADBKEYBOARD_IME in out
        return self._adbkeyboard_active

    async def _current_ime(self) -> str | None:
        """The IME currently active on the device. NOT cached — can change."""
        try:
            out = await self._run(
                "shell", "settings", "get", "secure", "default_input_method"
            )
        except AdbError:
            return None
        ime = out.decode("utf-8", errors="replace").strip()
        return ime or None


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
