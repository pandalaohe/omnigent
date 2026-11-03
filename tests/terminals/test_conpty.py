"""Windows shells share the terminal contract without starting native/REPL panes."""

import asyncio
import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnigent.inner import terminal
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_CODING_AGENT
from omnigent.terminals import conpty
from omnigent.terminals.conpty import ConPtyTerminalInstance, _OutputState
from omnigent.terminals.conpty_bridge import bridge_conpty_to_websocket
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


@pytest.mark.asyncio
async def test_launch_read_send_exit_and_cleanup(instance, monkeypatch):
    process = _Process()
    spawn = Mock(return_value=process)
    monkeypatch.setattr(conpty.PseudoConsoleProcess, "spawn", spawn)
    monkeypatch.setattr(conpty, "registered_user_environment", dict)
    callback = threading.Event()
    await instance.launch(cwd=instance.private_dir)
    instance.start_idle_watcher_thread(on_exit=callback.set)
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

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, frame):
        self.frames.append(frame)

    async def send_bytes(self, frame):
        self.frames.append(frame)

    async def close(self, code=1000):
        self.code = code


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
    assert not instance._subscribers
