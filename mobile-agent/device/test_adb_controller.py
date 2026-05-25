"""Unit tests for pure-logic AdbController helpers + cached lookups."""
from __future__ import annotations

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

    async def test_swaps_ime_then_restores(self, monkeypatch) -> None:
        """ADBKeyboard is enabled but Gboard is active → swap, broadcast, swap back."""
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
        await adb.type_text("milk")

        ime_sets = [c for c in calls if c[:3] == ("shell", "ime", "set")]
        # Exactly two: swap to adbkeyboard, then restore Gboard.
        assert len(ime_sets) == 2
        assert ime_sets[0] == ("shell", "ime", "set", "com.android.adbkeyboard/.AdbIME")
        assert ime_sets[1] == ("shell", "ime", "set", gboard)
        # Broadcast happened between the swap and the restore.
        broadcasts = [c for c in calls if c[:2] == ("shell", "am")]
        assert len(broadcasts) == 1

    async def test_falls_back_to_input_text_when_adbkeyboard_not_enabled(
        self, monkeypatch
    ) -> None:
        adb = AdbController("emulator-5554")
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str) -> bytes:
            calls.append(args)
            if args[:4] == ("shell", "ime", "list", "-s"):
                # ADBKeyboard is NOT in the enabled list.
                return b"com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME\n"
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        await adb.type_text("hello world")

        # Only the input-text fallback should fire — no `ime set`, no broadcast.
        assert not [c for c in calls if c[:2] == ("shell", "am")]
        assert not [c for c in calls if c[:3] == ("shell", "ime", "set")]
        input_calls = [c for c in calls if c[:3] == ("shell", "input", "text")]
        assert input_calls == [("shell", "input", "text", "hello%sworld")]

    async def test_falls_back_when_ime_list_errors(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")

        async def fake_run(*args: str) -> bytes:
            if args[:4] == ("shell", "ime", "list", "-s"):
                raise AdbError("ime command not supported")
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        # Should NOT raise — IME enumeration failure falls back to input text.
        await adb.type_text("hello")
