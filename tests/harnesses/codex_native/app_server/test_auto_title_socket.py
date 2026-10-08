"""Auto-title socket env and relay tests for the Codex app server."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native import app_server as codex_native_app_server
from omnigent.harnesses.codex_native.app_server import (
    CODEX_AUTO_TITLE_SOCKET_ENV,
    CodexNativeAppServer,
)
from tests.harnesses.codex_native.app_server._support import (
    _disable_codex_startup_rpc,
    _FakeStartupClient,
    _set_codex_version,
    _test_app_server,
)

# A pid that exists on no host: reaping must not signal a real process group.
_FAKE_PID = 999_999_999


@dataclass
class _FakeAppServerProcess:
    """Subprocess stand-in so the spawned env can be captured."""

    pid: int
    stderr: asyncio.StreamReader
    returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return self.returncode


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """Short /tmp dir: pytest's tmp_path exceeds the 104-char AF_UNIX limit on macOS."""
    path = Path(tempfile.mkdtemp(prefix="cxat", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def neutral_codex_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the config source at an empty dir so no real user config is read."""
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    return source_home


@pytest.fixture
def stubbed_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_codex_version(monkeypatch, (0, 147, 0))
    _disable_codex_startup_rpc(monkeypatch)


def _capture_spawn_env(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    """Record the env of the app-server spawn via a fake subprocess."""

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeAppServerProcess:
        captured["env"] = kwargs["env"]
        stderr = asyncio.StreamReader()
        stderr.feed_eof()
        return _FakeAppServerProcess(pid=_FAKE_PID, stderr=stderr)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)


async def _echo_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Return every received byte until the peer disconnects."""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()


def _unused_loopback_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return int(port)


@pytest.mark.usefixtures("neutral_codex_source", "stubbed_startup")
async def test_unix_mode_passes_auto_title_socket_env_and_starts_no_relay(
    short_socket_dir: Path,
    tmp_path: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook env names this session's socket even over an inherited value."""
    captured: dict[str, Any] = {}
    _capture_spawn_env(monkeypatch, captured)
    server = _test_app_server(
        short_socket_dir,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
        env={CODEX_AUTO_TITLE_SOCKET_ENV: "/other/app-server.sock"},
    )

    await server.start()
    try:
        env = captured["env"]
        assert env[CODEX_AUTO_TITLE_SOCKET_ENV] == str(short_socket_dir / "codex.sock")
        assert server.auto_title_relay is None
    finally:
        await server.close()


@pytest.mark.usefixtures("neutral_codex_source", "stubbed_startup")
async def test_start_drops_inherited_agent_pid_and_resolves_native_pid(
    short_socket_dir: Path,
    tmp_path: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inherited pid is dropped; the resolved native pid names this server."""
    captured: dict[str, Any] = {}
    _capture_spawn_env(monkeypatch, captured)
    native_pid_calls: list[tuple[object, ...]] = []

    def _fake_native_server_pid(*args: object) -> int:
        native_pid_calls.append(args)
        return 4242

    monkeypatch.setattr("omnigent.inner._proc.native_server_pid", _fake_native_server_pid)
    server = _test_app_server(
        short_socket_dir,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
        env={"COLLAB_AGENT_PID": "999", "KEEP_ME": "ok"},
    )

    await server.start()
    try:
        assert "COLLAB_AGENT_PID" not in captured["env"]
        assert captured["env"]["KEEP_ME"] == "ok"
        assert server.agent_pid == 4242
        assert native_pid_calls == [(_FAKE_PID, "app-server")]
    finally:
        await server.close()
    assert server.agent_pid is None


@pytest.mark.usefixtures("neutral_codex_source", "stubbed_startup")
async def test_ws_mode_relays_bytes_over_the_auto_title_socket(
    short_socket_dir: Path,
    tmp_path: Path,
    workspace: Path,
) -> None:
    """A unix client reaches the ws app-server through the relay both ways."""
    echo_server = await asyncio.start_server(_echo_connection, "127.0.0.1", 0)
    port = echo_server.sockets[0].getsockname()[1]
    server = _test_app_server(
        short_socket_dir,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.listen_url = f"ws://127.0.0.1:{port}"

    await server.start()
    try:
        assert server.auto_title_relay is not None
        assert stat.S_IMODE(server.socket_path.stat().st_mode) == 0o600
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        try:
            writer.write(b"ping-frame")
            await writer.drain()
            assert await reader.read(64) == b"ping-frame"
        finally:
            writer.close()
    finally:
        await server.close()
        echo_server.close()
        await echo_server.wait_closed()
    assert not server.socket_path.exists()
    assert server.auto_title_relay is None


@pytest.mark.usefixtures("neutral_codex_source", "stubbed_startup")
async def test_ws_mode_cancelled_relay_start_closes_the_startup_client(
    short_socket_dir: Path,
    tmp_path: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation raised while starting the relay still closes the startup client."""
    startup_client = _FakeStartupClient()

    async def _wait_until_ready(self: CodexNativeAppServer) -> _FakeStartupClient:
        return startup_client

    async def _cancelled_relay_start(*args: Any, **kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _wait_until_ready)
    monkeypatch.setattr(
        codex_native_app_server, "_start_app_server_socket_relay", _cancelled_relay_start
    )
    server = _test_app_server(
        short_socket_dir,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.listen_url = f"ws://127.0.0.1:{_unused_loopback_port()}"

    with pytest.raises(asyncio.CancelledError):
        await server.start()
    assert startup_client.close_calls == 1
    assert server.auto_title_relay is None


@pytest.mark.usefixtures("neutral_codex_source", "stubbed_startup")
async def test_ws_mode_relay_closes_the_client_when_upstream_is_down(
    short_socket_dir: Path,
    tmp_path: Path,
    workspace: Path,
) -> None:
    """A refused upstream connection ends the relay client at EOF; start() survives."""
    server = _test_app_server(
        short_socket_dir,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.listen_url = f"ws://127.0.0.1:{_unused_loopback_port()}"

    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        try:
            assert await reader.read(64) == b""
        finally:
            writer.close()
    finally:
        await server.close()


@pytest.mark.usefixtures("neutral_codex_source", "stubbed_startup")
async def test_ws_mode_start_survives_a_relay_bind_failure(
    short_socket_dir: Path,
    tmp_path: Path,
    workspace: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A socket path under a missing directory only logs; startup completes."""
    server = _test_app_server(
        short_socket_dir / "missing",
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.listen_url = f"ws://127.0.0.1:{_unused_loopback_port()}"

    with caplog.at_level(logging.WARNING, logger=codex_native_app_server.__name__):
        await server.start()
    try:
        assert server.auto_title_relay is None
        assert any(
            "auto-title" in record.getMessage() and record.levelno == logging.WARNING
            for record in caplog.records
        )
    finally:
        await server.close()


@pytest.mark.usefixtures("neutral_codex_source", "stubbed_startup")
async def test_ws_mode_older_close_preserves_the_newer_relay_socket(
    short_socket_dir: Path,
    tmp_path: Path,
    workspace: Path,
) -> None:
    """Closing the older of two overlapping instances keeps the newer relay."""
    echo_server = await asyncio.start_server(_echo_connection, "127.0.0.1", 0)
    port = echo_server.sockets[0].getsockname()[1]
    server_a = _test_app_server(
        short_socket_dir,
        tmp_path / "codex-home-a",
        tmp_path / "bridge-a",
        workspace,
    )
    server_a.listen_url = f"ws://127.0.0.1:{port}"
    server_b = _test_app_server(
        short_socket_dir,
        tmp_path / "codex-home-b",
        tmp_path / "bridge-b",
        workspace,
    )
    server_b.listen_url = f"ws://127.0.0.1:{port}"

    await server_a.start()
    await server_b.start()
    try:
        await server_a.close()
        assert server_b.socket_path.exists()
        reader, writer = await asyncio.open_unix_connection(str(server_b.socket_path))
        try:
            writer.write(b"ping-frame")
            await writer.drain()
            assert await reader.read(64) == b"ping-frame"
        finally:
            writer.close()
    finally:
        await server_b.close()
        echo_server.close()
        await echo_server.wait_closed()
    assert not server_b.socket_path.exists()


@pytest.mark.usefixtures("neutral_codex_source", "stubbed_startup")
async def test_ws_mode_relay_setup_failure_leaves_no_listener_behind(
    short_socket_dir: Path,
    tmp_path: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A post-bind relay failure closes the listener and removes its socket."""
    echo_server = await asyncio.start_server(_echo_connection, "127.0.0.1", 0)
    port = echo_server.sockets[0].getsockname()[1]
    server = _test_app_server(
        short_socket_dir,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.listen_url = f"ws://127.0.0.1:{port}"
    real_chmod = os.chmod

    def _chmod_fails_on_relay_socket(path: Any, mode: int) -> None:
        if Path(path) == server.socket_path:
            raise PermissionError(f"test denies chmod on {path}")
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", _chmod_fails_on_relay_socket)

    with caplog.at_level(logging.WARNING, logger=codex_native_app_server.__name__):
        await server.start()
    try:
        assert server.auto_title_relay is None
        assert any(
            "auto-title" in record.getMessage() and record.levelno == logging.WARNING
            for record in caplog.records
        )
        assert not server.socket_path.exists()
        with pytest.raises((FileNotFoundError, ConnectionRefusedError)):
            await asyncio.open_unix_connection(str(server.socket_path))
    finally:
        await server.close()
        echo_server.close()
        await echo_server.wait_closed()
