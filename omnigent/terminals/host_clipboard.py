"""Read plain text from the host desktop on behalf of one terminal attachment."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import subprocess
import sys
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

HOST_CLIPBOARD_MAX_BYTES = 256 * 1024
_READ_TIMEOUT_S = 2


async def _clipboard_command(*args: str, max_bytes: int) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdout is not None
    try:
        output = await process.stdout.readexactly(max_bytes + 1)
        return 0, output
    except asyncio.IncompleteReadError as exc:
        return await process.wait(), exc.partial
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            # Drain the remaining pipe data so asyncio can reap the child.
            while await process.stdout.read(8192):
                pass
            await process.wait()


async def read_host_clipboard() -> tuple[str, bool]:
    """Return text and success, or a stable status without command output."""
    try:
        async with asyncio.timeout(_READ_TIMEOUT_S):
            commands: list[tuple[str, ...]] = []
            if sys.platform == "darwin":
                commands.append(("/usr/bin/pbpaste",))
            elif sys.platform == "win32":
                # Isolate delayed rendering in a process that can be stopped by the timeout.
                commands.append((sys.executable, "-P", "-m", __name__))
            elif sys.platform == "linux":
                if os.environ.get("WAYLAND_DISPLAY"):
                    commands.append(("wl-paste", "--no-newline", "--type", "text"))
                if os.environ.get("DISPLAY"):
                    commands.append(("xclip", "-o", "-selection", "clipboard"))

            for command in commands:
                try:
                    code, data = await _clipboard_command(
                        *command, max_bytes=HOST_CLIPBOARD_MAX_BYTES
                    )
                except FileNotFoundError:
                    continue
                if sys.platform == "win32" and code == 2:
                    return "too-large", False
                if code != 0:
                    continue
                if len(data) > HOST_CLIPBOARD_MAX_BYTES:
                    return "too-large", False
                if not data:
                    return "empty", False
                text = data.decode("utf-8", errors="replace")
                if len(text.encode("utf-8")) > HOST_CLIPBOARD_MAX_BYTES:
                    return "too-large", False
                return text, True
    except (OSError, TimeoutError):
        pass
    return "unavailable", False


class HostClipboard:
    """Route one clipboard read at a time back to its requesting socket."""

    def __init__(self, websocket: WebSocket, send_lock: asyncio.Lock, *, read_only: bool) -> None:
        self._websocket = websocket
        self._send_lock = send_lock
        self._read_only = read_only
        self._task: asyncio.Task[None] | None = None

    async def advertise(self) -> None:
        async with self._send_lock:
            await self._websocket.send_json({"type": "capabilities", "hostClipboard": 1})

    async def handle(self, frame: dict[str, Any]) -> None:
        request_id = frame.get("id")
        if (
            not isinstance(request_id, str)
            or not 0 < len(request_id) <= 64
            or not request_id.isascii()
        ):
            return
        if self._read_only:
            await self._reply(request_id, "read-only", False)
        elif self._task is not None and not self._task.done():
            await self._reply(request_id, "busy", False)
        else:
            self._task = asyncio.create_task(self._read(request_id))

    async def _read(self, request_id: str) -> None:
        value, ok = await read_host_clipboard()
        await self._reply(request_id, value, ok)

    async def _reply(self, request_id: str, value: str, ok: bool) -> None:
        frame = {
            "type": "host-clipboard",
            "id": request_id,
            "ok": ok,
            ("text" if ok else "reason"): value,
        }
        with contextlib.suppress(WebSocketDisconnect, OSError, RuntimeError):
            async with self._send_lock:
                await self._websocket.send_json(frame)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


def _read_windows_clipboard() -> int:
    """Write bounded CF_UNICODETEXT to the parent's pipe."""
    from ctypes import wintypes

    user = ctypes.WinDLL("user32")  # type: ignore[attr-defined]
    kernel = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
    user.GetProcessWindowStation.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    flags = (wintypes.DWORD * 3)()
    needed = wintypes.DWORD()
    if (
        not user.GetUserObjectInformationW(
            user.GetProcessWindowStation(),
            1,
            ctypes.byref(flags),
            ctypes.sizeof(flags),
            ctypes.byref(needed),
        )
        or not flags[2] & 1
    ):  # UOI_FLAGS / WSF_VISIBLE
        return 1

    user.GetClipboardData.restype = wintypes.HANDLE
    kernel.GlobalLock.argtypes = kernel.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    kernel.GlobalLock.restype = wintypes.LPVOID
    kernel.GlobalSize.argtypes = (wintypes.HGLOBAL,)
    kernel.GlobalSize.restype = ctypes.c_size_t
    if not user.OpenClipboard(None):
        return 1
    try:
        if not user.IsClipboardFormatAvailable(13):  # CF_UNICODETEXT
            return 0
        handle = user.GetClipboardData(13)
        if not handle or not (pointer := kernel.GlobalLock(handle)):
            return 1
        try:
            limit = (HOST_CLIPBOARD_MAX_BYTES + 1) * 2
            size = min(kernel.GlobalSize(handle), limit)
            data = ctypes.string_at(pointer, size - size % 2)
            text = data.decode("utf-16-le", errors="replace").split("\0", 1)[0].encode("utf-8")
            if len(text) > HOST_CLIPBOARD_MAX_BYTES:
                return 2
            sys.stdout.buffer.write(text)
            return 0
        finally:
            kernel.GlobalUnlock(handle)
    finally:
        user.CloseClipboard()


if __name__ == "__main__" and sys.platform == "win32":
    raise SystemExit(_read_windows_clipboard())
