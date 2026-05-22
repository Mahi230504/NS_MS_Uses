"""Unit tests for AdbController.launch_package + helpers."""
from __future__ import annotations

import pytest

from device.adb_controller import (
    AdbController,
    AdbError,
    _package_stem,
    _parse_pm_list,
)


class TestPackageStem:
    def test_brand_segment(self) -> None:
        assert _package_stem("com.blinkit.markets") == "blinkit"

    def test_brand_in_app_org(self) -> None:
        assert _package_stem("in.juspay.nammayatri") == "nammayatri"

    def test_falls_back_to_last_segment(self) -> None:
        # If we strip every "common" segment, fall back to the last one.
        assert _package_stem("com.app") == "com.app".rsplit(".", 1)[-1]


class TestParsePmList:
    def test_strips_prefix(self) -> None:
        text = "package:com.blinkit.markets\npackage:com.example.app\n"
        assert _parse_pm_list(text) == ["com.blinkit.markets", "com.example.app"]

    def test_ignores_garbage(self) -> None:
        text = "warning: blah\npackage:com.x\n\nother\n"
        assert _parse_pm_list(text) == ["com.x"]


class TestLaunchPackage:
    async def test_direct_launch_succeeds(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str) -> bytes:
            calls.append(args)
            return b""

        monkeypatch.setattr(adb, "_run", fake_run)
        result = await adb.launch_package("com.blinkit.markets")
        assert result == "com.blinkit.markets"
        # Exactly one call — the direct monkey launch.
        assert len(calls) == 1
        assert calls[0][:3] == ("shell", "monkey", "-p")

    async def test_fallback_grep_discovery(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")
        calls: list[tuple[str, ...]] = []

        async def fake_run(*args: str) -> bytes:
            calls.append(args)
            # First call (direct launch): fail.
            if args[:3] == ("shell", "monkey", "-p") and args[3] == "com.blinkit.markets":
                raise AdbError("not installed")
            # pm list packages: return a sideloaded variant.
            if args[:4] == ("shell", "pm", "list", "packages"):
                return b"package:com.blinkit.regional\npackage:com.other.app\n"
            # Second monkey call (fallback) succeeds.
            if args[:3] == ("shell", "monkey", "-p") and args[3] == "com.blinkit.regional":
                return b""
            raise AdbError(f"unexpected: {args}")

        monkeypatch.setattr(adb, "_run", fake_run)
        result = await adb.launch_package("com.blinkit.markets")
        assert result == "com.blinkit.regional"

    async def test_fallback_no_match_raises(self, monkeypatch) -> None:
        adb = AdbController("emulator-5554")

        async def fake_run(*args: str) -> bytes:
            if args[:3] == ("shell", "monkey", "-p"):
                raise AdbError("not installed")
            if args[:4] == ("shell", "pm", "list", "packages"):
                return b"package:com.other.app\n"
            raise AdbError(f"unexpected: {args}")

        monkeypatch.setattr(adb, "_run", fake_run)
        with pytest.raises(AdbError):
            await adb.launch_package("com.blinkit.markets")
