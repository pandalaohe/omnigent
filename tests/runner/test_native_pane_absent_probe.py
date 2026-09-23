"""Ground-truth probe for absent panes at the native reaper.

``close_terminal_detailed`` returning ``"absent"`` no longer proves the pane
is gone: ``TerminalRegistry.close`` pops before awaiting the inner close, so
a concurrent closer's pop reads as absent while the pane is still alive. The
reaper must probe ``pane.instance.probe_alive()`` before retiring anything.
"""

from __future__ import annotations

import asyncio
import errno
import importlib
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner.app import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.terminals.pane_reaper import NativePaneReaper, NativePaneStillAlive, PaneRef
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.helpers import NullServerClient


def _build_app_with_reaper(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]):
    from omnigent.runner import tool_dispatch
    from omnigent.runner.native import orchestration

    async def _fake_teardown(session_id: str) -> None:
        calls["teardown"].append(session_id)

    def _fake_publish_event(**kwargs: Any) -> None:
        calls["events"].append(kwargs)

    monkeypatch.setattr(orchestration, "teardown_codex_native_app_server", _fake_teardown)
    monkeypatch.setattr(tool_dispatch, "_publish_terminal_deleted_event", _fake_publish_event)
    terminal_registry = TerminalRegistry()
    resource_registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    app = create_runner_app(
        terminal_registry=terminal_registry,
        resource_registry=resource_registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    app.state.test_resource_registry = resource_registry
    return app, terminal_registry


def _alive_instance(tmp_path: Path, *, alive: bool | None, probe_log: list[str]) -> Any:
    async def _probe_alive() -> bool | None:
        probe_log.append("probed")
        return alive

    async def _is_alive() -> bool:
        return bool(alive)

    async def _close() -> None:
        return None

    return SimpleNamespace(
        running=True,
        socket_path=tmp_path / "tmux.sock",
        close=_close,
        probe_alive=_probe_alive,
        is_alive=_is_alive,
    )


async def test_absent_but_alive_raises_without_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent + probe True: raise, no app-server teardown, no event."""
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, _ = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    probe_log: list[str] = []
    pane = PaneRef(
        "conv_absent_alive",
        terminal_resource_id("codex", "main"),
        "codex",
        tmp_path / "tmux.sock",
        instance=_alive_instance(tmp_path, alive=True, probe_log=probe_log),
    )
    with pytest.raises(NativePaneStillAlive) as exc:
        await reaper._reap(pane)
    assert exc.value.probe is True
    assert probe_log == ["probed"]
    assert calls["teardown"] == []
    assert calls["events"] == []


async def test_absent_and_dead_retires_session_pieces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent + probe False: retire both, as today, after probing."""
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, _ = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    probe_log: list[str] = []
    pane = PaneRef(
        "conv_absent_dead",
        terminal_resource_id("codex", "main"),
        "codex",
        tmp_path / "tmux.sock",
        instance=_alive_instance(tmp_path, alive=False, probe_log=probe_log),
    )
    await reaper._reap(pane)
    assert probe_log == ["probed"]
    assert calls["teardown"] == ["conv_absent_dead"]
    assert [e["conversation_id"] for e in calls["events"]] == ["conv_absent_dead"]


async def test_release_now_absent_but_alive_fails_and_stays_managed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Through the real caller: absent-but-alive release_now -> failed, stays managed."""
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, _ = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    probe_log: list[str] = []
    pane = PaneRef(
        "conv_release_alive",
        terminal_resource_id("codex", "main"),
        "codex",
        tmp_path / "tmux.sock",
        instance=_alive_instance(tmp_path, alive=True, probe_log=probe_log),
    )

    async def _not_busy(_pane: PaneRef) -> bool:
        return False

    releasing = NativePaneReaper(
        list_native_panes=lambda: [pane],
        is_busy=_not_busy,
        reap=reaper._reap,
    )
    releasing.manage("conv_release_alive")
    assert await releasing.release_now("conv_release_alive") == "failed"
    assert "conv_release_alive" in releasing._managed_conversations
    assert calls["teardown"] == []
    assert calls["events"] == []


async def _wait_until_empty(reg: TerminalRegistry, conv: str) -> None:
    for _ in range(500):
        if reg.list_for_conversation(conv) == []:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("close did not pop the instance in time")


async def test_popped_but_alive_pane_is_not_torn_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cross-generation: a pane another closer popped but still alive is not retired."""
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, terminal_registry = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_close() -> None:
        started.set()
        await release.wait()

    async def _still_alive() -> bool:
        return True

    instance = SimpleNamespace(
        running=True,
        socket_path=tmp_path / "tmux.sock",
        close=_slow_close,
        probe_alive=_still_alive,
    )
    terminal_registry._by_conversation.setdefault("conv_race", {})[("codex", "main")] = instance
    terminal_registry._instance_locks[("conv_race", "codex", "main")] = threading.Lock()
    pane = PaneRef(
        "conv_race",
        terminal_resource_id("codex", "main"),
        "codex",
        tmp_path / "tmux.sock",
        instance=instance,
    )
    task = asyncio.create_task(terminal_registry.close("conv_race", "codex", "main"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await _wait_until_empty(terminal_registry, "conv_race")
        with pytest.raises(RuntimeError, match="still alive"):
            await reaper._reap(pane)
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=5)
    assert calls["teardown"] == []
    assert calls["events"] == []


async def test_absent_probe_unknown_does_not_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, _ = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    instance = _alive_instance(tmp_path, alive=None, probe_log=[])
    pane = PaneRef(
        "conv_unknown",
        terminal_resource_id("codex", "main"),
        "codex",
        instance.socket_path,
        instance,
    )
    with pytest.raises(NativePaneStillAlive) as exc:
        await reaper._reap(pane)
    assert exc.value.instance is instance
    assert exc.value.probe is None
    assert calls == {"teardown": [], "events": []}


class _TmuxProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.hang = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.hang:
            await asyncio.Future()
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.parametrize(
    ("stdout", "stderr", "returncode", "expected"),
    [
        (b"0\n", b"", 0, True),
        (b"1\n", b"", 0, False),
        (b"", b"can't find pane: main", 1, False),
        (b"", b"error connecting to /tmp/test.sock (No such file or directory)\n", 1, False),
        (b"", b"permission denied", 1, None),
        (b"", b"", 0, None),
    ],
)
async def test_probe_alive_uses_tmux_truth_when_running_is_false(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: bytes,
    stderr: bytes,
    returncode: int,
    expected: bool | None,
) -> None:
    proc = _TmuxProcess(stdout, stderr, returncode)

    async def spawn(*_args: Any, **_kwargs: Any) -> _TmuxProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    instance = TerminalInstance("codex", "main", tmp_path / "socket", tmp_path, running=False)
    assert await instance.probe_alive() is expected
    assert instance.running is False


async def test_probe_spawn_failure_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def spawn(*_args: Any, **_kwargs: Any) -> _TmuxProcess:
        raise OSError(errno.ENOENT, "tmux missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    instance = TerminalInstance("codex", "main", tmp_path / "socket", tmp_path, running=True)
    assert await instance.probe_alive() is None
    assert instance.running is True


async def test_probe_communication_failure_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _BrokenTmuxProcess(_TmuxProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            raise OSError(errno.EIO, "probe failed")

    async def spawn(*_args: Any, **_kwargs: Any) -> _TmuxProcess:
        return _BrokenTmuxProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    instance = TerminalInstance("codex", "main", tmp_path / "socket", tmp_path, running=True)
    assert await instance.probe_alive() is None
    assert instance.running is True


async def test_probe_timeout_kills_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from omnigent.inner import terminal

    proc = _TmuxProcess()
    proc.hang = True

    async def spawn(*_args: Any, **_kwargs: Any) -> _TmuxProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(terminal, "_PROBE_TIMEOUT_S", 0.01)
    instance = TerminalInstance("codex", "main", tmp_path / "socket", tmp_path)
    assert await instance.probe_alive() is None
    assert proc.killed


async def test_kill_server_is_bounded_and_does_not_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.inner import terminal

    proc = _TmuxProcess()
    proc.hang = True
    args: list[tuple[Any, ...]] = []

    async def spawn(*argv: Any, **_kwargs: Any) -> _TmuxProcess:
        args.append(argv)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(terminal, "_PROBE_TIMEOUT_S", 0.01)
    instance = TerminalInstance("codex", "main", tmp_path / "socket", tmp_path, running=True)
    with pytest.raises(TimeoutError):
        await instance.kill_server()
    assert proc.killed
    assert args[0][-1] == "kill-server"
    assert instance.running
    assert instance.private_dir == tmp_path


async def test_kill_server_raises_on_tmux_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def spawn(*_args: Any, **_kwargs: Any) -> _TmuxProcess:
        return _TmuxProcess(stderr=b"permission denied", returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    instance = TerminalInstance("codex", "main", tmp_path / "socket", tmp_path, running=True)
    with pytest.raises(RuntimeError, match="permission denied"):
        await instance.kill_server()
    assert instance.running
    assert instance.private_dir == tmp_path


def _dead_retiring_instance(tmp_path: Path, closes: list[str]) -> Any:
    async def probe() -> bool:
        return False

    async def close() -> None:
        closes.append("closed")

    return SimpleNamespace(
        socket_path=tmp_path / "old.sock",
        probe_alive=probe,
        close=close,
    )


async def test_retired_tail_does_not_run_after_generation_moves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, _ = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    lifecycle = app.state.cli_runtime_lifecycle
    closes: list[str] = []
    instance = _dead_retiring_instance(tmp_path, closes)
    pane = PaneRef(
        "conv_generation",
        terminal_resource_id("codex", "main"),
        "codex",
        instance.socket_path,
        instance,
    )
    reaper._record_pending(pane, instance)
    lifecycle.mark_starting("conv_generation")
    await reaper._retire_pending()
    assert closes == ["closed"]
    assert not reaper._pending_retire
    assert calls == {"teardown": [], "events": []}


async def test_retired_tail_skips_successor_pane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, registry = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    closes: list[str] = []
    old = _dead_retiring_instance(tmp_path, closes)
    pane = PaneRef(
        "conv_successor", terminal_resource_id("codex", "main"), "codex", old.socket_path, old
    )
    reaper._record_pending(pane, old)
    successor = SimpleNamespace(socket_path=tmp_path / "new.sock")
    registry._by_conversation.setdefault("conv_successor", {})[("codex", "main")] = successor
    app.state.test_resource_registry._terminal_roles[("conv_successor", pane.terminal_id)] = (
        "codex-native"
    )
    await reaper._retire_pending()
    assert registry.get("conv_successor", "codex", "main") is successor
    assert closes == ["closed"]
    assert not reaper._pending_retire
    assert calls == {"teardown": [], "events": []}


async def test_retired_tail_failure_retries_with_new_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, _ = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    closes: list[str] = []
    instance = _dead_retiring_instance(tmp_path, closes)
    pane = PaneRef(
        "conv_retry_tail",
        terminal_resource_id("codex", "main"),
        "codex",
        instance.socket_path,
        instance,
    )
    reaper._record_pending(pane, instance)
    original_token = reaper._pending_retire[id(instance)].tail_token
    runner_app = importlib.import_module("omnigent.runner.app")
    attempts = [0]

    async def delete_bridge_dirs(**_kwargs: Any) -> None:
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError("bridge cleanup failed")

    monkeypatch.setattr(runner_app, "_delete_native_bridge_dirs", delete_bridge_dirs)
    await reaper._retire_pending()
    assert reaper._pending_retire[id(instance)].tail_token != original_token
    assert app.state.cli_runtime_lifecycle.phase("conv_retry_tail") == "live"
    await reaper._retire_pending()
    assert attempts == [2]
    assert not reaper._pending_retire
    assert app.state.cli_runtime_lifecycle.phase("conv_retry_tail") == "absent"


async def test_retired_tail_retries_after_shared_lock_releases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, _ = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    lifecycle = app.state.cli_runtime_lifecycle
    instance = _dead_retiring_instance(tmp_path, [])
    pane = PaneRef(
        "conv_held", terminal_resource_id("codex", "main"), "codex", instance.socket_path, instance
    )
    reaper._record_pending(pane, instance)
    record = reaper._pending_retire[id(instance)]
    token = record.tail_token
    async with lifecycle.lock_for("conv_held").shared():
        await asyncio.wait_for(reaper._retire_pending(), timeout=1.5)
        assert reaper._pending_retire[id(instance)].tail_token == token
        assert lifecycle.phase("conv_held") == "absent"
        assert calls == {"teardown": [], "events": []}
    await asyncio.wait_for(reaper._retire_pending(), timeout=1)
    assert not reaper._pending_retire
    assert lifecycle.phase("conv_held") == "absent"
    assert calls["teardown"] == ["conv_held", "conv_held"]
