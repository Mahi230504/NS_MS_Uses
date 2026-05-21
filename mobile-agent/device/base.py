"""DeviceController Protocol — common contract for ADB / idb / future backends.

Every device backend (Android via adb, iOS via idb, ...) implements the same
async surface. The orchestrator and action executor depend on this Protocol,
never on a concrete class — switching platforms is a settings choice, not a
code change in the loop.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class DeviceError(RuntimeError):
    """Raised when a device-side command (adb / idb / etc.) fails.

    Concrete controllers raise more specific subclasses; the orchestrator
    catches this base in its retry / recovery path.
    """


@runtime_checkable
class DeviceController(Protocol):
    """Async wrappers around a phone control surface.

    Implementations must not block the event loop — wrap any sync subprocess
    work with asyncio.create_subprocess_exec or to_thread.
    """

    async def screencap(self) -> bytes: ...
    async def tap(self, x: int, y: int) -> None: ...
    async def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int
    ) -> None: ...
    async def type_text(self, text: str) -> None: ...
    async def get_screen_size(self) -> tuple[int, int]: ...
    async def get_foreground_package(self) -> str | None: ...
    async def recover(self) -> None:
        """Best-effort "back out of whatever you're stuck on".

        Android: KEYCODE_BACK. iOS: home button. Used by the orchestrator's
        unchanged-streak recovery, which doesn't care which it is.
        """
        ...
