"""Windows shells share the terminal contract without starting native/REPL panes."""

import asyncio
import json
import logging
import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from fastapi import WebSocket

from omnigent.inner import terminal
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_CODING_AGENT
from omnigent.terminals import conpty, conpty_bridge
from omnigent.terminals.conpty import ConPtyTerminalInstance, _OutputState
from omnigent.terminals.conpty_bridge import bridge_conpty_to_websocket
from omnigent.terminals.ws_common import terminal_replay_params
from omnigent.tools.builtins.sys_terminal import _describe_entry, _format_launch_envelope
from tests._helpers.polling import poll_until_async


@pytest.mark.asyncio
async def test_windows_plain_terminal_uses_conpty_without_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal, "IS_WINDOWS", True)

    def require_tmux() -> None:
        pytest.fail("a Windows shell must not require tmux")

    monkeypatch.setattr(terminal, "_require_supported_tmux", require_tmux)
    created = terminal.create_terminal_instance(
        "shell",
        "one",
        TerminalEnvSpec(
            command="cmd.exe",
            os_env=OSEnvSpec(cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none")),
        ),
    )
    try:
        assert created.instance.backend == "conpty"
        assert created.cwd == tmp_path
        assert created.instance._clipboard_bridge is None
    finally:
        await created.instance.close()


@pytest.mark.parametrize("kind", ["native", "repl", "keep_alive", "sandbox", "network"])
def test_windows_refuses_unsupported_contract_before_allocation(monkeypatch, kind):
    monkeypatch.setattr(terminal, "IS_WINDOWS", True)
    allocate = Mock(side_effect=AssertionError("must reject before allocation"))
    monkeypatch.setattr(terminal.tempfile, "mkdtemp", allocate)
    spec = TerminalEnvSpec(
        command="cmd.exe", os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none"))
    )
    name = CLAUDE_NATIVE_CODING_AGENT.terminal_name if kind == "native" else "shell"
    if kind == "repl":
        spec.tmux_start_on_attach = True
    if kind == "keep_alive":
        spec.keep_alive_after_exit = True
    if kind == "sandbox":
        spec.os_env = OSEnvSpec(sandbox=OSEnvSandboxSpec(type="windows_job"))
    if kind == "network":
        spec.os_env = OSEnvSpec(
            sandbox=OSEnvSandboxSpec(type="none", egress_rules=["https://example.com"])
        )
    with pytest.raises(RuntimeError, match="not supported on Windows"):
        terminal.create_terminal_instance(name, "one", spec)
    allocate.assert_not_called()


def test_windows_environment_precedence_and_case_insensitive_exclusions():
    env = conpty.windows_terminal_environment(
        {"Path": "user", "UserName": "owner"},
        {"PATH": "service", "SystemRoot": "C:\\Windows", "ssh_auth_sock": "unused"},
        {"pAtH": "override", "omnigent_runner_tunnel_binding_token": "test-token"},
        ["sSh_AuTh_SoCk"],
        {},
    )
    assert env["PATH"] == "override" and env["USERNAME"] == "owner"
    assert env["SYSTEMROOT"] == "C:\\Windows"
    assert "SSH_AUTH_SOCK" not in env and "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN" not in env


def test_replay_trims_to_line_and_tracks_split_mode_sequences():
    state = _OutputState(limit=24)
    state.append(b"discarded line\r\n")
    state.append(b"partial\r\nkept\r\n")
    assert state.snapshot() == b"kept\r\n"
    state.append(b"\x1b[?104")
    state.append(b"9;2004h")
    assert state.modes[1049] and state.modes[2004]
    state.append(b"\x1b[?;25l")
    assert not state.modes[25]


def test_repeated_overflow_retains_recent_history_at_chunk_boundaries():
    state = _OutputState()
    chunk = (b"x" * 62 + b"\r\n") * 1024
    for index in range(130):
        state.append(chunk)
        if index >= 64:
            assert 4 * 1024 * 1024 - len(chunk) <= state.size <= 4 * 1024 * 1024
            assert state.snapshot().endswith(chunk)


def test_replay_offsets_are_absolute_after_ring_trims_a_line():
    state = _OutputState(limit=16)
    state.append(b"evicted\r\n")
    state.append(b"partial\r\nkept\r\n")
    state.append(b"next")

    assert (state.base_offset, state.end_offset) == (18, 28)
    assert state.read_from(17, 5) is None
    assert state.read_from(29, 5) is None
    assert state.read_from(28, 5) == b""
    assert state.read_from(22, 5) == b"\r\nnex"
    assert state.read_from(24, 2) == b"ne"


def test_replay_rejects_protocol_without_a_catchup_boundary():
    cursor = {"replay_epoch": "0" * 32, "since_offset": "40"}
    assert terminal_replay_params({"conpty_replay": "1", **cursor}) == {}
    assert terminal_replay_params({"conpty_replay": "2", **cursor}) == {
        "conpty_replay": "2",
        **cursor,
    }


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX import boundary")
def test_posix_attach_imports_do_not_load_windows_backend():
    script = """
import importlib, sys
windows = {
    'omnigent.terminals.conpty',
    'omnigent.terminals.conpty_process',
    'omnigent.inner.windows_jobobject_sandbox',
}
for path in (
    'omnigent.runner.host_terminals',
    'omnigent.runner.resource_routes',
    'omnigent.server.routes.terminal_attach',
):
    importlib.import_module(path)
    assert not windows.intersection(sys.modules), path
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)


class _Process:
    pid = 42

    def __init__(self):
        self.output = queue.Queue()
        self.exited = threading.Event()
        self.writes = []
        self.sizes = []
        self.code = None
        self.closed = False

    def read_chunk(self):
        return self.output.get(timeout=5)

    def write(self, data):
        self.writes.append(data)

    def resize(self, cols, rows):
        self.sizes.append((cols, rows))

    def exit_code(self):
        return self.code

    def wait(self):
        assert self.exited.wait(5)
        return self.code

    def close(self, *, output_drained=None):
        if not self.closed:
            self.closed = True
            self.code = self.code if self.code is not None else 1
            self.output.put(b"final\r\n")
            self.output.put(b"")
            self.exited.set()
        if output_drained is not None:
            assert output_drained.wait(5)


@pytest.fixture
def instance(tmp_path):
    return ConPtyTerminalInstance(
        name="shell", session_key="one", private_dir=tmp_path, socket_path=tmp_path / "unused"
    )


def test_conpty_tool_outputs_have_identity_without_tmux_socket(instance):
    entry = _describe_entry("shell", "one", instance)
    launch = json.loads(
        _format_launch_envelope(
            SimpleNamespace(terminal_name="shell", session_key="one"), instance, False
        )
    )
    for result in (entry, launch):
        assert result["instance_id"] == instance.instance_id
        assert "tmux_socket" not in result


@pytest.mark.parametrize("running", [False, True])
def test_startup_diagnostics_read_buffer_without_screen_read(instance, running):
    instance.running = running
    with instance._output_lock:
        instance._output.append(b"\x1b[31mpi-subagents is missing ")
        instance._output.append(b"./native-child-lifecycle\x1b[0m\r\n")
    assert instance._last_pane_snapshot is None
    assert instance.startup_diagnostic_text() == (
        "pi-subagents is missing ./native-child-lifecycle"
    )
    assert instance._last_pane_snapshot is None


@pytest.mark.asyncio
async def test_launch_read_send_exit_and_cleanup(instance, monkeypatch):
    process = _Process()
    spawn = Mock(return_value=process)
    monkeypatch.setattr(conpty.PseudoConsoleProcess, "spawn", spawn)
    monkeypatch.setattr(conpty, "registered_user_environment", dict)
    callback = threading.Event()
    poll_backoff = Mock(return_value=True)
    await instance.launch(cwd=instance.private_dir)
    instance.start_idle_watcher_thread(
        on_exit=callback.set, idle_poll_backoff_allowed=poll_backoff
    )
    process.output.put("\x1b[2J\x1b[H你好\r\nready".encode())
    await poll_until_async(lambda: b"ready" in instance._output.snapshot())
    assert "你好" in (await instance.read())["screen"]
    assert (await instance.send("echo", keys="Enter C-c"))["status"] == "sent"
    assert process.writes == [b"echo\r\x03"]
    assert "error" in await instance.send("never", keys="unknown")
    assert len(process.writes) == 1
    # The pipe deliberately has not reached EOF. Only the process waiter decides exit.
    process.code = 7
    process.exited.set()
    await poll_until_async(callback.is_set)
    poll_backoff.assert_not_called()
    assert instance._last_exit_status == 7 and not instance.running
    assert b"final" in instance._output.snapshot()
    await instance.close()
    await instance.close()
    assert not instance.private_dir.exists()


@pytest.mark.asyncio
async def test_atomic_attach_snapshot_and_alt_screen(instance):
    instance._output.append(b"history")
    seed = instance.attach_snapshot()
    with instance._output_lock:
        instance._output.append(b"next")
        for subscriber in instance._subscribers:
            subscriber.offer(b"next")
    assert seed.output.endswith(b"history")
    assert await seed.queue.get() == b"next"
    instance.unsubscribe(seed.queue)
    instance._output.append(b"\x1b[?1049h")
    alt = instance.attach_snapshot()
    assert b"history" not in alt.output and b"\x1b[?1049h" in alt.output
    instance.unsubscribe(alt.queue)


class _WebSocket:
    def __init__(self, messages=()):
        self.incoming = asyncio.Queue()
        for message in messages:
            self.incoming.put_nowait(message)
        self.frames = []
        self.code = None
        self.reason = None

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, frame):
        self.frames.append(frame)

    async def send_text(self, frame):
        self.frames.append(json.loads(frame))

    async def send_bytes(self, frame):
        self.frames.append(frame)

    async def close(self, code=1000, reason=""):
        self.code = code
        self.reason = reason

    def ack(self, offset):
        self.incoming.put_nowait(
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "output_ack", "offset": offset}),
            }
        )

    def output(self):
        return b"".join(frame for frame in self.frames if isinstance(frame, bytes))


@pytest.mark.asyncio
async def test_resume_only_sends_missed_bytes_and_never_exceeds_ack_window(instance, monkeypatch):
    monkeypatch.setattr(conpty_bridge, "_REPLAY_WINDOW_BYTES", 4)
    process = _Process()
    instance._process, instance.running = process, True
    instance._output.append(b"seen \x1b[?1049h")
    offset = instance._output.end_offset
    instance._output.append(b"ABCDEFG")
    ws = _WebSocket()
    task = asyncio.create_task(
        bridge_conpty_to_websocket(
            cast(WebSocket, ws),
            instance,
            replay={"replay_epoch": instance.instance_id, "since_offset": str(offset)},
        )
    )
    try:
        await poll_until_async(lambda: b"ABCD" in ws.frames)
        assert ws.frames[1] == {
            "type": "replay_meta",
            "epoch": instance.instance_id,
            "offset": offset,
            "through": offset + 7,
            "prelude": None,
        }
        assert [frame for frame in ws.frames if isinstance(frame, bytes)] == [b"ABCD"]

        for frame in (
            {"type": "output_ack", "offset": offset + 5},
            {"type": "ping", "id": "live"},
        ):
            await ws.incoming.put({"type": "websocket.receive", "text": json.dumps(frame)})
        await poll_until_async(lambda: {"type": "pong", "id": "live"} in ws.frames)
        assert [frame for frame in ws.frames if isinstance(frame, bytes)] == [b"ABCD"]
        ws.ack(offset + 4)
        await poll_until_async(lambda: b"EFG" in ws.frames)
        assert [frame for frame in ws.frames if isinstance(frame, bytes)] == [b"ABCD", b"EFG"]
        assert not process.sizes and not process.writes and not instance._subscribers
    finally:
        await ws.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(task, 3)
    assert ws.code == 1000


@pytest.mark.asyncio
async def test_replay_recovers_on_same_socket_after_ring_wrap(instance, monkeypatch):
    monkeypatch.setattr(conpty_bridge, "_REPLAY_WINDOW_BYTES", 4)
    instance._output.limit = 16
    instance._output.append(b"ABCD")
    ws = _WebSocket()
    task = asyncio.create_task(
        bridge_conpty_to_websocket(cast(WebSocket, ws), instance, replay={"conpty_replay": "2"})
    )
    try:
        await poll_until_async(lambda: b"ABCD" in ws.frames)
        instance._output.append(b"evicted\r\n")
        instance._output.append(b"new\r\nmore\r\n")
        offset = instance._output.end_offset
        start = instance._output.base_offset
        assert instance._output.base_offset > 4
        assert (
            sum(
                isinstance(frame, dict) and frame.get("type") == "replay_meta"
                for frame in ws.frames
            )
            == 1
        )
        instance._exit_done.set()
        ws.ack(4)
        await poll_until_async(lambda: ws.output() == b"ABCDmore")
        metadata = [
            frame
            for frame in ws.frames
            if isinstance(frame, dict) and frame.get("type") == "replay_meta"
        ]
        assert metadata[-1]["offset"] == start and metadata[-1]["through"] == offset
        assert metadata[-1]["prelude"] is not None
        ws.ack(start + 4)
        await poll_until_async(lambda: ws.output() == b"ABCDmore\r\n")
        assert ws.code is None
        ws.ack(offset)
        await asyncio.wait_for(task, 3)
    finally:
        await ws.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(task, 3)
    assert ws.code == 4404


def test_replay_rejects_cursor_from_another_process(instance):
    instance._output.append(b"hello")
    offset, through, prelude = instance.replay_start("0" * 32, 4)
    assert offset == 0 and through == 5 and prelude is not None


@pytest.mark.asyncio
async def test_replay_ack_progresses_during_a_blocked_pty_write(instance, monkeypatch):
    monkeypatch.setattr(conpty_bridge, "_REPLAY_WINDOW_BYTES", 4)
    monkeypatch.setattr(conpty_bridge, "_INPUT_QUEUE_BYTES", 4)
    started, release = threading.Event(), threading.Event()
    writes = []

    def write_input(data):
        started.set()
        assert release.wait(5)
        writes.append(data)

    monkeypatch.setattr(instance, "write_input", write_input)
    instance._output.append(b"ABCDEFG")
    ws = _WebSocket()
    task = asyncio.create_task(
        bridge_conpty_to_websocket(cast(WebSocket, ws), instance, replay={"conpty_replay": "2"})
    )
    successor = _WebSocket()
    successor_task = None
    try:
        await poll_until_async(lambda: ws.output() == b"ABCD")
        ws.incoming.put_nowait({"type": "websocket.receive", "bytes": b"first"})
        await poll_until_async(started.is_set)
        ws.incoming.put_nowait({"type": "websocket.receive", "bytes": b"second"})
        ws.ack(4)
        await poll_until_async(lambda: ws.output() == b"ABCDEFG")
        assert not writes
        ws.incoming.put_nowait({"type": "websocket.disconnect"})
        await poll_until_async(lambda: ws.code is not None)
        successor_task = asyncio.create_task(
            bridge_conpty_to_websocket(
                cast(WebSocket, successor), instance, replay={"conpty_replay": "2"}
            )
        )
        successor.incoming.put_nowait({"type": "websocket.receive", "bytes": b"third"})
        await poll_until_async(lambda: bool(successor.output()))
        assert not writes
        release.set()
        await poll_until_async(lambda: len(writes) == 3)
        assert writes == [b"first", b"second", b"third"]
    finally:
        release.set()
        ws.incoming.put_nowait({"type": "websocket.disconnect"})
        await asyncio.wait_for(task, 3)
        if successor_task is not None:
            successor.incoming.put_nowait({"type": "websocket.disconnect"})
            await asyncio.wait_for(successor_task, 3)


def test_replay_resize_dedupes_against_the_current_shared_size(instance):
    process = _Process()
    instance._process = process
    first_started, release_first, second_started = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    resize = process.resize

    def native_resize(cols, rows):
        resize(cols, rows)
        if cols == 100:
            first_started.set()
            assert release_first.wait(3)

    def resize_second():
        second_started.set()
        instance.resize(120, 40, force=False)

    process.resize = native_resize
    first = threading.Thread(target=instance.resize, args=(100, 30))
    second = threading.Thread(target=resize_second)
    first.start()
    try:
        assert first_started.wait(3)
        second.start()
        assert second_started.wait(3)
        assert process.sizes == [(100, 30)]
    finally:
        release_first.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert (instance.cols, instance.rows) == (120, 40)
    instance.resize(100, 30, force=False)
    instance.resize(100, 30, force=False)
    assert process.sizes == [(100, 30), (120, 40), (100, 30)]


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [4, 8])
async def test_replay_exit_is_bounded_without_a_final_ack(instance, monkeypatch, window):
    monkeypatch.setattr(conpty_bridge, "_REPLAY_WINDOW_BYTES", window)
    monkeypatch.setattr(conpty_bridge, "_REPLAY_EXIT_ACK_GRACE_S", 0.05)
    instance._output.append(b"done")
    instance._exit_done.set()
    ws = _WebSocket()
    await asyncio.wait_for(
        bridge_conpty_to_websocket(cast(WebSocket, ws), instance, replay={"conpty_replay": "2"}), 3
    )
    assert ws.output() == b"done" and ws.code == 4404


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows ConPTY")
@pytest.mark.asyncio
async def test_native_conpty_resumes_with_only_new_process_output(instance):
    instance.command = sys.executable
    instance.args = [
        "-u",
        "-c",
        "import sys; print('first output'); sys.stdin.readline(); "
        "print('second output'); sys.stdin.readline()",
    ]
    ws = _WebSocket()
    await instance.launch(cwd=instance.private_dir)
    try:
        await poll_until_async(lambda: b"first output" in (instance.read_replay(0, 4096) or b""))
        _, start, _ = instance.replay_start(None, None)
        assert (await instance.send("go"))["status"] == "sent"
        task = asyncio.create_task(
            bridge_conpty_to_websocket(
                cast(WebSocket, ws),
                instance,
                replay={"replay_epoch": instance.instance_id, "since_offset": str(start)},
            )
        )
        try:
            await poll_until_async(lambda: b"second output" in ws.output())
            assert b"first output" not in ws.output()
            assert ws.frames[1]["offset"] == start and ws.frames[1]["prelude"] is None
        finally:
            await ws.incoming.put({"type": "websocket.disconnect"})
            await asyncio.wait_for(task, 3)
    finally:
        await instance.close()
    assert ws.code == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [False, True])
async def test_bridge_metadata_input_resize_and_detach(instance, read_only):
    process = _Process()
    instance._process, instance.running = process, True
    ws = _WebSocket(
        [
            {"type": "websocket.receive", "bytes": "中文".encode()},
            {"type": "websocket.receive", "text": '{"type":"resize","cols":100,"rows":30}'},
            {"type": "websocket.receive", "text": '{"type":"resize","cols":true,"rows":30}'},
            {"type": "websocket.disconnect"},
        ]
    )
    await asyncio.wait_for(bridge_conpty_to_websocket(ws, instance, read_only=read_only), 3)
    assert ws.frames[0] == {"type": "pty_info", "windows_pty": {"backend": "conpty", "build": 0}}
    assert process.writes == ([] if read_only else ["中文".encode()])
    assert ((100, 30) in process.sizes) is not read_only
    assert len(instance._subscribers) == 0 and not process.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [False, True])
async def test_conpty_host_clipboard_reply_is_not_pty_input(instance, monkeypatch, read_only):
    from omnigent.terminals import host_clipboard

    read_started = asyncio.Event()
    release_read = asyncio.Event()

    async def read_clipboard():
        read_started.set()
        await release_read.wait()
        return "pwd", True

    monkeypatch.setattr(host_clipboard, "read_host_clipboard", read_clipboard)
    process = _Process()
    instance._process, instance.running = process, True
    ws = _WebSocket()
    bridge = asyncio.create_task(bridge_conpty_to_websocket(ws, instance, read_only=read_only))

    def request(request_id):
        ws.incoming.put_nowait(
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "host-clipboard-read", "id": request_id}),
            }
        )

    try:
        capability = {"type": "capabilities", "hostClipboard": 1}
        await poll_until_async(lambda: capability in ws.frames)
        request("first")
        if read_only:
            refusal = {"type": "host-clipboard", "id": "first", "ok": False, "reason": "read-only"}
            await poll_until_async(lambda: refusal in ws.frames)
            assert not read_started.is_set()
        else:
            await asyncio.wait_for(read_started.wait(), 3)
            request("second")
            busy = {"type": "host-clipboard", "id": "second", "ok": False, "reason": "busy"}
            await poll_until_async(lambda: busy in ws.frames)
            release_read.set()
            reply = {"type": "host-clipboard", "id": "first", "ok": True, "text": "pwd"}
            await poll_until_async(lambda: reply in ws.frames)
        assert not process.writes
    finally:
        ws.incoming.put_nowait({"type": "websocket.disconnect"})
        await asyncio.wait_for(bridge, 3)


@pytest.mark.asyncio
async def test_bridge_browser_write_runs_off_the_event_loop_thread(instance):
    """A browser input write must never block the event-loop thread."""
    process = _Process()
    instance._process, instance.running = process, True
    write_threads = []
    direct_write = instance.write_input

    def record_write(data):
        write_threads.append(threading.get_ident())
        direct_write(data)

    instance.write_input = record_write
    ws = _WebSocket(
        [
            {"type": "websocket.receive", "bytes": b"off-loop"},
            {"type": "websocket.disconnect"},
        ]
    )
    await asyncio.wait_for(bridge_conpty_to_websocket(ws, instance), 3)
    assert write_threads and write_threads[0] != threading.get_ident()
    assert process.writes == [b"off-loop"]


@pytest.mark.asyncio
async def test_bridge_input_write_failure_is_logged_without_input_bytes(
    instance, monkeypatch, caplog
):
    """A ConPTY input write OSError becomes an operator-visible warning, no payload."""
    process = _Process()
    instance._process, instance.running = process, True
    payload = b"secret-input-marker"

    def fail_write(_data):
        raise OSError(22, "WriteFile failed")

    monkeypatch.setattr(instance, "write_input", fail_write)
    caplog.set_level(logging.WARNING, logger="omnigent.terminals.conpty_bridge")
    ws = _WebSocket(
        [
            {"type": "websocket.receive", "bytes": payload},
            {"type": "websocket.disconnect"},
        ]
    )
    await asyncio.wait_for(bridge_conpty_to_websocket(ws, instance), 3)
    messages = [record.getMessage() for record in caplog.records]
    assert any("OSError" in message for message in messages), messages
    assert payload.decode() not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [False, True])
async def test_bridge_exit_or_backpressure_close(instance, monkeypatch, overflow):
    ws = _WebSocket()
    task = asyncio.create_task(bridge_conpty_to_websocket(ws, instance))
    await poll_until_async(lambda: len(ws.frames) > 0)
    if overflow:
        monkeypatch.setattr(conpty, "_SUBSCRIBER_BYTES", 4)
        instance._subscribers[0].offer(b"12345")
    else:
        instance._exit_done.set()
        instance._subscribers[0].offer(None)
    await asyncio.wait_for(task, 3)
    assert ws.code == (1013 if overflow else 4404)
    assert ws.reason == ("" if overflow else "terminal process exited")
    assert not instance._subscribers


@pytest.mark.asyncio
async def test_conpty_bridge_replies_pong_to_ping_without_pty_input(instance):
    """Valid ping frames receive a matching pong and never write to the PTY."""
    process = _Process()
    instance._process, instance.running = process, True
    ws = _WebSocket(
        [
            {"type": "websocket.receive", "text": '{"type":"ping","id":"probe-1"}'},
            {"type": "websocket.receive", "text": '{"type":"ping","id":""}'},
            {"type": "websocket.disconnect"},
        ]
    )
    await asyncio.wait_for(bridge_conpty_to_websocket(ws, instance), 3)
    assert {"type": "pong", "id": "probe-1"} in ws.frames
    assert not any(
        isinstance(frame, dict) and frame.get("type") == "pong" and frame.get("id") == ""
        for frame in ws.frames
    )
    assert process.writes == []
