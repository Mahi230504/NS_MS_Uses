"""Unit tests for pure-logic AdbController helpers + cached lookups."""
from __future__ import annotations

import asyncio
import base64

import pytest

from device.adb_controller import AdbController, AdbError, _escape_for_input_text


class TestEscapeForInputText:
    def test_plain_word(self) -> None:
        assert _escape_for_input_text("hello") == "hello"

    def test_space_becomes_percent_s(self) -> None:
        assert _escape_for_input_text("hello world") == "hello%sworld"

    def test_apostrophe_escaped(self) -> None:
        assert _escape_for_input_text("can't") == "can\\'t"

    def test_double_quote_escaped(self) -> None:
        assert _escape_for_input_text('say "hi"') == 'say%s\\"hi\\"'

    def test_ampersand_and_pipe(self) -> None:
        assert _escape_for_input_text("a&b|c") == "a\\&b\\|c"

    def test_parens_and_redirects(self) -> None:
        assert _escape_for_input_text("(a) > b") == "\\(a\\)%s\\>%sb"

    def test_unicode_passes_through(self) -> None:
        assert _escape_for_input_text("café") == "café"

    def test_backslash_doubled(self) -> None:
        # Lone backslashes must themselves be escaped so the shell doesn't
        # eat them before `input text` sees them.
        assert _escape_for_input_text("a\\b") == "a\\\\b"


class TestGetScreenSize:
    async def test_parses_physical_size(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")

        async def fake_run(*args: str) -> bytes:
            assert args == ("shell", "wm", "size")
            return b"Physical size: 1080x1920\n"

        monkeypatch.setattr(adb, "_run", fake_run)
        assert await adb.get_screen_size() == (1080, 1920)

    async def test_prefers_override_when_present(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")

        async def fake_run(*args: str) -> bytes:
            return b"Physical size: 1080x1920\nOverride size: 720x1280\n"

        monkeypatch.setattr(adb, "_run", fake_run)
        # Override (last match) is what apps actually see.
        assert await adb.get_screen_size() == (720, 1280)

    async def test_is_cached(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")
        calls = {"n": 0}

        async def fake_run(*args: str) -> bytes:
            calls["n"] += 1
            return b"Physical size: 1080x1920"

        monkeypatch.setattr(adb, "_run", fake_run)
        await adb.get_screen_size()
        await adb.get_screen_size()
        await adb.get_screen_size()
        assert calls["n"] == 1

    async def test_unparseable_raises(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")

        async def fake_run(*args: str) -> bytes:
            return b"no dimensions here\n"

        monkeypatch.setattr(adb, "_run", fake_run)
        with pytest.raises(AdbError):
            await adb.get_screen_size()


class TestTypeText:
    async def test_broadcasts_when_adbkeyboard_already_active(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str) -> bytes:
            calls.append(args)
            if args[:4] == ("shell", "ime", "list", "-s"):
                return b"com.android.adbkeyboard/.AdbIME\ncom.example.other/.Other\n"
            if args[:5] == ("shell", "settings", "get", "secure", "default_input_method"):
                return b"com.android.adbkeyboard/.AdbIME\n"
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        await adb.type_text("can't & won't")

        # No `ime set` calls — already on ADBKeyboard.
        assert not [c for c in calls if c[:3] == ("shell", "ime", "set")]
        broadcasts = [c for c in calls if c[:2] == ("shell", "am")]
        assert len(broadcasts) == 1
        decoded = base64.standard_b64decode(broadcasts[0][-1]).decode("utf-8")
        assert decoded == "can't & won't"

    async def test_falls_back_to_input_text_when_gboard_is_active(
        self, monkeypatch
    ) -> None:
        """ADBKeyboard exists but isn't current: don't try to swap mid-typing.

        The mid-task IME swap is unreliable on some OEMs (InputConnection
        drops), so type_text falls back to `input text` rather than swapping.
        The orchestrator is responsible for swapping ADBKeyboard in at task
        start via use_adbkeyboard_for_task().
        """
        adb = AdbController("emulator-5554")
        gboard = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str) -> bytes:
            calls.append(args)
            if args[:5] == ("shell", "settings", "get", "secure", "default_input_method"):
                return f"{gboard}\n".encode()
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        await adb.type_text("milk")

        # No swap, no broadcast — just the fallback.
        assert not [c for c in calls if c[:3] == ("shell", "ime", "set")]
        assert not [c for c in calls if c[:2] == ("shell", "am")]
        input_calls = [c for c in calls if c[:3] == ("shell", "input", "text")]
        assert input_calls == [("shell", "input", "text", "milk")]


class TestUseAdbkeyboardForTask:
    async def test_swaps_when_enabled_and_not_active(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")
        gboard = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str) -> bytes:
            calls.append(args)
            if args[:4] == ("shell", "ime", "list", "-s"):
                return f"{gboard}\ncom.android.adbkeyboard/.AdbIME\n".encode()
            if args[:5] == ("shell", "settings", "get", "secure", "default_input_method"):
                return f"{gboard}\n".encode()
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        original = await adb.use_adbkeyboard_for_task()
        assert original == gboard
        ime_sets = [c for c in calls if c[:3] == ("shell", "ime", "set")]
        assert ime_sets == [("shell", "ime", "set", "com.android.adbkeyboard/.AdbIME")]

    async def test_noop_when_already_active(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")

        async def fake_run(*args: str) -> bytes:
            if args[:4] == ("shell", "ime", "list", "-s"):
                return b"com.android.adbkeyboard/.AdbIME\n"
            if args[:5] == ("shell", "settings", "get", "secure", "default_input_method"):
                return b"com.android.adbkeyboard/.AdbIME\n"
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        original = await adb.use_adbkeyboard_for_task()
        assert original is None  # nothing to restore

    async def test_noop_when_adbkeyboard_not_enabled(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")

        async def fake_run(*args: str) -> bytes:
            if args[:4] == ("shell", "ime", "list", "-s"):
                return b"com.google.android.inputmethod.latin/...\n"
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        assert await adb.use_adbkeyboard_for_task() is None


class TestForceOffAdbkeyboard:
    """Safety net for crashed prior runs / uncaptured original IME — when
    the device is stuck on ADBKeyboard, switch to anything else enabled."""

    async def test_switches_to_first_non_adbkeyboard_enabled(
        self, monkeypatch
    ) -> None:
        adb = AdbController("emulator-5554")
        gboard = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str) -> bytes:
            calls.append(args)
            if args[:5] == ("shell", "settings", "get", "secure", "default_input_method"):
                return b"com.android.adbkeyboard/.AdbIME\n"
            if args[:4] == ("shell", "ime", "list", "-s"):
                return f"com.android.adbkeyboard/.AdbIME\n{gboard}\n".encode()
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        await adb.force_off_adbkeyboard()

        ime_sets = [c for c in calls if c[:3] == ("shell", "ime", "set")]
        assert ime_sets == [("shell", "ime", "set", gboard)]

    async def test_noop_when_already_off_adbkeyboard(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")
        gboard = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str) -> bytes:
            calls.append(args)
            if args[:5] == ("shell", "settings", "get", "secure", "default_input_method"):
                return f"{gboard}\n".encode()
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        await adb.force_off_adbkeyboard()

        # Current IME isn't ADBKeyboard — no list query, no ime set.
        assert not [c for c in calls if c[:3] == ("shell", "ime", "set")]
        assert not [c for c in calls if c[:4] == ("shell", "ime", "list", "-s")]

    async def test_noop_when_no_alternative_enabled(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str) -> bytes:
            calls.append(args)
            if args[:5] == ("shell", "settings", "get", "secure", "default_input_method"):
                return b"com.android.adbkeyboard/.AdbIME\n"
            if args[:4] == ("shell", "ime", "list", "-s"):
                # Only ADBKeyboard enabled — nothing to swap to.
                return b"com.android.adbkeyboard/.AdbIME\n"
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        await adb.force_off_adbkeyboard()
        # No ime set called because there's nothing else to swap to.
        assert not [c for c in calls if c[:3] == ("shell", "ime", "set")]

    async def test_restore_ime_falls_back_when_id_is_none(
        self, monkeypatch
    ) -> None:
        """restore_ime(None) should now invoke the force-off safety net,
        not just return early. Otherwise a task that started with
        ADBKeyboard already active never switches off."""
        adb = AdbController("emulator-5554")
        gboard = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str) -> bytes:
            calls.append(args)
            if args[:5] == ("shell", "settings", "get", "secure", "default_input_method"):
                return b"com.android.adbkeyboard/.AdbIME\n"
            if args[:4] == ("shell", "ime", "list", "-s"):
                return f"com.android.adbkeyboard/.AdbIME\n{gboard}\n".encode()
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        await adb.restore_ime(None)
        ime_sets = [c for c in calls if c[:3] == ("shell", "ime", "set")]
        assert ime_sets == [("shell", "ime", "set", gboard)]


_VALID_DUMP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<hierarchy rotation="0">'
    '<node bounds="[0,0][100,100]" text="OK" '
    'class="android.widget.Button" clickable="true" />'
    '</hierarchy>'
)


async def _no_sleep(_seconds: float) -> None:
    """Drop the 250ms inter-pass retry sleep so dump tests run instantly."""
    return None


class TestDumpUiXml:
    """The UI dump is fused into ONE `adb exec-out sh -c` round-trip per pass
    (was four: killall + rm + uiautomator dump + cat). These pin that shape and
    the 3-pass hybrid (uncompressed, uncompressed-after-retry, compressed)."""

    async def test_single_round_trip_on_clean_dump(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str, timeout: float | None = None) -> bytes:
            calls.append(args)
            return _VALID_DUMP.encode("utf-8")

        monkeypatch.setattr(adb, "_run", fake_run)
        out = await adb.dump_ui_xml()

        # One adb invocation total — not four — when pass 1 succeeds.
        assert len(calls) == 1
        assert calls[0][:3] == ("exec-out", "sh", "-c")
        script = calls[0][3]
        assert "killall uiautomator" in script
        assert "rm -f /sdcard/atlas_ui_dump.xml" in script
        assert "uiautomator dump /sdcard/atlas_ui_dump.xml" in script
        assert "cat /sdcard/atlas_ui_dump.xml" in script
        assert "--compressed" not in script  # pass 1 is full fidelity
        assert out is not None
        assert out.startswith("<?xml")
        assert out.endswith("</hierarchy>")

    async def test_falls_back_to_compressed_when_uncompressed_empty(
        self, monkeypatch
    ) -> None:
        adb = AdbController("emulator-5554")
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str, timeout: float | None = None) -> bytes:
            calls.append(args)
            script = args[3]
            # Uncompressed passes 1 & 2 hit the idle-state error (no file
            # written → empty read); the compressed pass 3 returns a tree.
            if "--compressed" in script:
                return _VALID_DUMP.encode("utf-8")
            return b"ERROR: could not get idle state."

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(adb, "_run", fake_run)
        out = await adb.dump_ui_xml()

        assert len(calls) == 3
        assert "--compressed" not in calls[0][3]
        assert "--compressed" not in calls[1][3]
        assert "--compressed" in calls[2][3]
        assert out is not None and out.endswith("</hierarchy>")

    async def test_returns_none_when_every_pass_empty(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")

        async def fake_run(*args: str, timeout: float | None = None) -> bytes:
            return b""  # nothing ever written

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(adb, "_run", fake_run)
        assert await adb.dump_ui_xml() is None

    async def test_pipeline_adb_error_is_swallowed_as_empty(
        self, monkeypatch
    ) -> None:
        adb = AdbController("emulator-5554")

        async def fake_run(*args: str, timeout: float | None = None) -> bytes:
            raise AdbError("cat: /sdcard/atlas_ui_dump.xml: No such file")

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(adb, "_run", fake_run)
        # Each pass raises → each returns "" → extraction yields None overall.
        assert await adb.dump_ui_xml() is None


class TestGetForegroundPackage:
    """Foreground-app resolution must survive OEM quirks. On realme/ColorOS,
    mCurrentFocus pins to `NotificationShade` whenever an overlay (shade, IME,
    dialog) holds focus; mFocusedApp / ResumedActivity still name the real app.
    Without the fallback chain every app would resolve to None → GENERIC
    profile, silently disabling all per-app grounding."""

    async def _pkg_for(self, monkeypatch, dump: str) -> str | None:
        adb = AdbController("emulator-5554")

        async def fake_run(*args: str, timeout: float | None = None) -> bytes:
            assert args[:2] == ("shell", "dumpsys")
            return dump.encode("utf-8")

        monkeypatch.setattr(adb, "_run", fake_run)
        return await adb.get_foreground_package()

    async def test_current_focus_preferred_when_present(self, monkeypatch) -> None:
        dump = (
            "  mCurrentFocus=Window{abc u0 com.grofers.customerapp/.MainActivity}\n"
            "  mFocusedApp=ActivityRecord{def u0 com.android.launcher/.Launcher t5}\n"
        )
        assert await self._pkg_for(monkeypatch, dump) == "com.grofers.customerapp"

    async def test_falls_back_to_focused_app_when_overlay_focused(
        self, monkeypatch
    ) -> None:
        # The real ColorOS shape that returned None before the fix.
        dump = (
            "  mCurrentFocus=Window{ae751d4 u0 NotificationShade}\n"
            "  mFocusedApp=ActivityRecord{77ed4e2 u0 com.android.launcher/.Launcher t5}\n"
        )
        assert await self._pkg_for(monkeypatch, dump) == "com.android.launcher"

    async def test_falls_back_to_resumed_activity(self, monkeypatch) -> None:
        dump = (
            "  mCurrentFocus=Window{ae751d4 u0 NotificationShade}\n"
            "  ResumedActivity: ActivityRecord{77ed4e2 u0 in.swiggy.android/.MainActivity}\n"
        )
        assert await self._pkg_for(monkeypatch, dump) == "in.swiggy.android"

    async def test_returns_none_when_nothing_matches(self, monkeypatch) -> None:
        assert await self._pkg_for(monkeypatch, "no focus info here") is None


class TestWakeScreen:
    async def test_sends_wakeup_keyevent(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str, timeout: float | None = None) -> bytes:
            calls.append(args)
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        await adb.wake_screen()
        # KEYCODE_WAKEUP = 224 (turns screen on; no-op if already on).
        assert calls == [("shell", "input", "keyevent", "224")]
