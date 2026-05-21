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
    async def test_uses_adbkeyboard_when_default(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str) -> bytes:
            calls.append(args)
            if args[:5] == ("shell", "settings", "get", "secure", "default_input_method"):
                return b"com.android.adbkeyboard/.AdbIME\n"
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        await adb.type_text("can't & won't")

        broadcasts = [c for c in calls if c[:2] == ("shell", "am")]
        assert len(broadcasts) == 1
        payload = broadcasts[0][-1]
        # The base64 payload is the last positional arg of the broadcast call.
        decoded = base64.standard_b64decode(payload).decode("utf-8")
        assert decoded == "can't & won't"

    async def test_falls_back_to_input_text_when_ime_missing(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str) -> bytes:
            calls.append(args)
            if args[:5] == ("shell", "settings", "get", "secure", "default_input_method"):
                return b"com.example.SomeOtherIme/.Service\n"
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        await adb.type_text("hello world")

        input_calls = [c for c in calls if c[:3] == ("shell", "input", "text")]
        assert input_calls == [("shell", "input", "text", "hello%sworld")]

    async def test_falls_back_when_ime_lookup_errors(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")

        async def fake_run(*args: str) -> bytes:
            if args[:5] == ("shell", "settings", "get", "secure", "default_input_method"):
                raise AdbError("settings unavailable")
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        # Should NOT raise — IME detection failure means "fall back".
        await adb.type_text("hello")
