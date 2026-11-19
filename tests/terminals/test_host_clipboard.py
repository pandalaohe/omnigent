"""Host clipboard readers report bounded, explicit results to terminal attaches."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from omnigent.terminals import host_clipboard


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("manager", "payload", "expected"),
    [
        (b"Background\n", b"unused", ("unavailable", False)),
        (b"Aqua\n", b"pwd", ("pwd", True)),
        (b"Aqua\n", b"", ("empty", False)),
        (b"Aqua\n", b"x" * (host_clipboard.HOST_CLIPBOARD_MAX_BYTES + 1), ("too-large", False)),
    ],
    ids=["background", "desktop", "empty", "too-large"],
)
async def test_mac_clipboard_requires_aqua_and_limits_response(
    monkeypatch, manager, payload, expected
):
    monkeypatch.setattr(host_clipboard, "sys", SimpleNamespace(platform="darwin"))
    commands = []

    async def clipboard_command(*args, max_bytes):
        commands.append(args)
        return 0, manager if args[0] == "/bin/launchctl" else payload

    monkeypatch.setattr(host_clipboard, "_clipboard_command", clipboard_command)
    assert await host_clipboard.read_host_clipboard() == expected
    assert commands == [("/bin/launchctl", "managername")] + (
        [("/usr/bin/pbpaste",)] if manager == b"Aqua\n" else []
    )


@pytest.mark.asyncio
async def test_linux_falls_back_to_x11_and_times_out(monkeypatch):
    monkeypatch.setattr(host_clipboard, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":1")
    monkeypatch.setattr(host_clipboard, "_READ_TIMEOUT_S", 0.02)
    commands = []
    cancelled = asyncio.Event()

    async def clipboard_command(*args, max_bytes):
        commands.append(args)
        if args[0] == "wl-paste":
            raise FileNotFoundError
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(host_clipboard, "_clipboard_command", clipboard_command)
    assert await host_clipboard.read_host_clipboard() == ("unavailable", False)
    assert commands == [
        ("wl-paste", "--no-newline", "--type", "text"),
        ("xclip", "-o", "-selection", "clipboard"),
    ]
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_clipboard_process_limits_bytes_even_when_writer_has_more():
    code, data = await host_clipboard._clipboard_command(
        sys.executable, "-c", "print('x' * 500000)", max_bytes=64
    )
    assert code == 0
    assert data == b"x" * 65
