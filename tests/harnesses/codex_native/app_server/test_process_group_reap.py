"""Process-group reap tests for Codex app server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from omnigent.harnesses.codex_native.app_server import (
    CodexNativeAppServer,
)
from omnigent.inner import _proc
from tests._helpers.polling import poll_until, poll_until_async
from tests.harnesses.codex_native.app_server._support import _test_app_server


def _isolated_discovery_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, dict[str, object]]:
    """Redirect probe registry IO to *tmp_path* and record reconcile/register calls."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.harnesses.codex_native import process_registry as codex_process_registry

    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(codex_process_registry, "_ps_output", lambda _columns: "")

    def absent_group(_pgid: int, sig: int) -> None:
        if sig == 0:
            raise ProcessLookupError
        pytest.fail("unexpected real process-group signal")

    monkeypatch.setattr(codex_process_registry.os, "killpg", absent_group)
    registry_path = tmp_path / "registry.json"
    real_reconcile = codex_process_registry.reconcile_codex_native_process_registry
    real_register = codex_process_registry.register_codex_native_process
    real_unregister = codex_process_registry.unregister_codex_native_process
    calls: dict[str, object] = {"reconciled": 0}

    def _reconcile() -> None:
        calls["reconciled"] = int(calls["reconciled"]) + 1
        real_reconcile(registry_path=registry_path)

    def _register(
        *,
        pid: int,
        pgid: int,
        session_tag: str,
        owner_lock_path: object,
        **kwargs: object,
    ) -> None:
        del kwargs
        calls["pid"] = pid
        calls["pgid"] = pgid
        calls["session_tag"] = session_tag
        real_register(
            pid=pid,
            pgid=pgid,
            session_tag=session_tag,
            owner_lock_path=owner_lock_path,  # type: ignore[arg-type]
            registry_path=registry_path,
        )

    def _unregister(session_tag: str, **kwargs: object) -> None:
        del kwargs
        real_unregister(session_tag, registry_path=registry_path)

    monkeypatch.setattr(
        codex_native_app_server, "reconcile_codex_native_process_registry", _reconcile
    )
    monkeypatch.setattr(codex_native_app_server, "register_codex_native_process", _register)
    monkeypatch.setattr(codex_native_app_server, "unregister_codex_native_process", _unregister)
    return registry_path, calls


def _read_discovery_registry_entries(registry_path: Path) -> list[dict[str, object]]:
    try:
        raw = registry_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    payload = json.loads(raw)
    assert isinstance(payload, list)
    return [item for item in payload if isinstance(item, dict)]


@pytest.mark.posix_only
async def test_model_discovery_spawn_registers_crash_safe_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The probe spawn registers a tagged entry whose tag is in the argv list."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.harnesses.codex_native import process_registry as codex_process_registry

    registry_path, calls = _isolated_discovery_registry(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> object:
        captured["args"] = list(args)
        captured["kwargs"] = dict(kwargs)
        stderr = asyncio.StreamReader()
        stderr.feed_eof()

        class _FakeProcess:
            pid = 1234567
            returncode: int | None = None

            def terminate(self) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = -1

            async def wait(self) -> int:
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        process = _FakeProcess()
        process.stderr = stderr  # type: ignore[attr-defined]
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    listen_url = "ws://127.0.0.1:12345"

    discovery = await codex_native_app_server._start_codex_model_discovery_process(
        codex_path="/test/codex",
        listen_url=listen_url,
        env={},
        cwd=tmp_path,
        config_overrides=('model="probe-model"',),
    )
    try:
        assert calls["reconciled"] == 1
        assert isinstance(captured["args"], list)
        assert isinstance(captured["kwargs"], dict)
        args = [str(arg) for arg in captured["args"]]
        kwargs = captured["kwargs"]
        assert kwargs["executable"] is None
        assert args[:1] == [sys.executable]
        assert Path(args[1]).name == "_liveness_exec.py"
        assert args[2] == "--liveness-fd"
        read_fd = int(args[3])
        assert kwargs["pass_fds"] == (read_fd,)
        with pytest.raises(OSError):
            os.fstat(read_fd)
        assert discovery.liveness_fd is not None
        os.fstat(discovery.liveness_fd)
        args = args[4:]
        tag = discovery.session_tag
        assert tag is not None and tag.startswith(codex_process_registry._model_probe_tag_prefix())
        needle = codex_process_registry.codex_native_session_tag_cmdline_arg(tag)
        assert args[0] == "/test/codex"
        assert "omnigent_crash_teardown_tag" not in args[0]
        assert args[1:] == [
            "app-server",
            "-c",
            needle,
            "--listen",
            listen_url,
            "-c",
            'model="probe-model"',
        ]
        assert discovery.owner_lock is not None
        entries = _read_discovery_registry_entries(registry_path)
        assert len(entries) == 1
        assert entries[0]["session_tag"] == tag == calls["session_tag"]
        assert entries[0]["pid"] == 1234567 == calls["pid"]
        assert entries[0]["pgid"] == calls["pgid"]
        assert entries[0]["owner_lock_path"] == str(discovery.owner_lock.path)
    finally:
        await codex_native_app_server._stop_codex_model_discovery_process(discovery)


@pytest.mark.posix_only
async def test_model_discovery_stop_removes_entry_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Stopping the probe removes its registry entry and releases the lock."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    registry_path, _calls = _isolated_discovery_registry(monkeypatch, tmp_path)

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> object:
        del args, kwargs
        stderr = asyncio.StreamReader()
        stderr.feed_eof()

        class _FakeProcess:
            pid = 1234568
            returncode: int | None = None

            def terminate(self) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = -1

            async def wait(self) -> int:
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        process = _FakeProcess()
        process.stderr = stderr  # type: ignore[attr-defined]
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    discovery = await codex_native_app_server._start_codex_model_discovery_process(
        codex_path="/test/codex",
        listen_url="ws://127.0.0.1:12346",
        env={},
        cwd=tmp_path,
    )
    assert discovery.session_tag is not None
    assert discovery.owner_lock is not None
    liveness_fd = discovery.liveness_fd
    assert liveness_fd is not None
    lock_path = Path(discovery.owner_lock.path)
    assert lock_path.exists()
    assert len(_read_discovery_registry_entries(registry_path)) == 1

    await codex_native_app_server._stop_codex_model_discovery_process(discovery)

    assert _read_discovery_registry_entries(registry_path) == []
    assert not lock_path.exists()
    assert discovery.liveness_fd is None
    with pytest.raises(OSError):
        os.fstat(liveness_fd)


@pytest.mark.posix_only
async def test_model_discovery_stop_keeps_entry_when_group_signal_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed teardown keeps the entry for reconcile but still releases the lock."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.harnesses.codex_native import process_registry as codex_process_registry

    registry_path, _calls = _isolated_discovery_registry(monkeypatch, tmp_path)
    owner_lock = codex_native_app_server.acquire_codex_native_process_owner_lock()
    assert owner_lock is not None
    session_tag = "codex-model-probe-failing-teardown"
    codex_process_registry.register_codex_native_process(
        pid=1234569,
        pgid=1234569,
        session_tag=session_tag,
        owner_lock_path=owner_lock.path,
        registry_path=registry_path,
    )

    class _FailingProcess:
        pid = 1234569
        returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    async def _empty_stderr() -> str:
        return ""

    monkeypatch.setattr(
        codex_process_registry,
        "_ps_output",
        lambda _columns: (
            f" 1234569 1234569 {os.getuid()} node omnigent_crash_teardown_tag={session_tag}\n"
        ),
    )
    monkeypatch.setattr(
        codex_native_app_server.os,
        "killpg",
        lambda _pgid, _sig: (_ for _ in ()).throw(PermissionError()),
    )
    monkeypatch.setattr(
        codex_native_app_server._proc,
        "terminate_tree",
        lambda _process: pytest.fail("unverified tree termination"),
    )
    discovery = codex_native_app_server._CodexModelDiscoveryProcess(
        process=_FailingProcess(),  # type: ignore[arg-type]
        stderr_tail=asyncio.create_task(_empty_stderr()),
        session_tag=session_tag,
        owner_lock=owner_lock,
        process_group_id=1234569,
    )

    await codex_native_app_server._stop_codex_model_discovery_process(discovery)

    entries = _read_discovery_registry_entries(registry_path)
    assert len(entries) == 1
    assert entries[0]["session_tag"] == session_tag
    assert not Path(owner_lock.path).exists()


@pytest.mark.posix_only
async def test_model_discovery_stop_reaps_group_after_wrapper_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A wrapper that exits on SIGTERM must not strand the tree it started.

    ``codex`` is a node wrapper: SIGTERM kills it while the app-server it
    started keeps running, so waiting on the spawned process alone reads a
    live tree as gone. The stop helper must SIGKILL the recorded group after
    the leader exits.
    """
    import contextlib
    import signal

    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.harnesses.codex_native import process_registry as codex_process_registry
    from omnigent.inner import _proc

    monkeypatch.setattr(codex_native_app_server, "_PROCESS_GROUP_TERM_GRACE_SECONDS", 0.5)
    ready_marker = tmp_path / "child-started"
    tag = "codex-model-probe-own-group"
    tag_arg = codex_process_registry.codex_native_session_tag_cmdline_arg(tag)
    child = (
        "import os,pathlib,signal,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(300)"
    )
    wrapper = (
        "import pathlib, subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', "
        f"{child!r}, {str(ready_marker)!r}, {tag_arg!r}]); "
        "time.sleep(300)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        wrapper,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        **_proc.spawn_kwargs(),
    )
    assert process.stderr is not None
    pgid = codex_native_app_server._process_group_id(process)
    killed: list[int] = []
    real_killpg = os.killpg
    group_gone = False

    def _kill_own_group(group: int, sig: int) -> None:
        nonlocal group_gone
        assert group == pgid
        if sig != 0:
            killed.append(sig)
        real_killpg(group, sig)
        if sig == signal.SIGKILL:
            group_gone = True

    def _fake_ps(_columns: str) -> str:
        if not ready_marker.exists() or group_gone:
            return ""
        child_pid = int(ready_marker.read_text())
        return f" {child_pid} {pgid} {os.getuid()} python {tag_arg}\n"

    monkeypatch.setattr(codex_process_registry, "_ps_output", _fake_ps)
    monkeypatch.setattr(codex_native_app_server.os, "killpg", _kill_own_group)
    discovery = codex_native_app_server._CodexModelDiscoveryProcess(
        process=process,
        stderr_tail=asyncio.create_task(
            codex_native_app_server._capture_codex_discovery_stderr_tail(process.stderr)
        ),
        process_group_id=pgid,
        session_tag=tag,
    )
    try:
        for _ in range(250):
            if ready_marker.exists():
                break
            await asyncio.sleep(0.02)
        assert ready_marker.exists(), "wrapper never started its child"

        await codex_native_app_server._stop_codex_model_discovery_process(discovery)

        assert killed == [signal.SIGTERM, signal.SIGKILL]
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            real_killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await process.wait()


@pytest.mark.posix_only
async def test_app_server_close_escalates_recorded_group_before_unregister(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close reaps the recorded group and drops the entry only once it is empty."""
    import signal

    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.harnesses.codex_native import process_registry as codex_process_registry

    killed: list[tuple[int, int]] = []

    def _killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            return  # the synthetic group never dies
        killed.append((pgid, sig))

    unregistered: list[str] = []
    monkeypatch.setattr(codex_native_app_server, "_PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(codex_native_app_server, "_PROCESS_GROUP_KILL_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(codex_native_app_server.os, "killpg", _killpg)
    monkeypatch.setattr(
        codex_process_registry,
        "_ps_output",
        lambda _columns: (
            f" 1234567 456789 {os.getuid()} "
            "codex omnigent_crash_teardown_tag=codex-native-test-tag\n"
        ),
    )
    monkeypatch.setattr(
        codex_native_app_server, "unregister_codex_native_process", unregistered.append
    )

    class _ExitedProcess:
        pid = 1234567
        returncode = 0

    def _server(group_id: int) -> CodexNativeAppServer:
        return CodexNativeAppServer(
            codex_path="/test/codex",
            socket_path=tmp_path / "server.sock",
            codex_home=tmp_path / "codex-home",
            env={},
            config_overrides=[],
            cwd=tmp_path,
            bridge_dir=tmp_path / "bridge",
            proc=_ExitedProcess(),  # type: ignore[arg-type]
            process_registry_tag="codex-native-test-tag",
            process_group_id=group_id,
        )

    original = _server(456789)
    replacement = _server(456790)
    assert original.resident_incarnation_id != replacement.resident_incarnation_id
    await original.close()
    assert not original.exit_confirmed

    # The spawned wrapper already exited, but its group survives: TERM then
    # KILL, and the registry entry is kept for the next reconcile.
    assert killed == [(456789, signal.SIGTERM), (456789, signal.SIGKILL)]
    assert unregistered == []

    def _killpg_missing(_pgid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(codex_native_app_server.os, "killpg", _killpg_missing)
    monkeypatch.setattr(codex_process_registry, "_ps_output", lambda _columns: "")

    await replacement.close()
    assert replacement.exit_confirmed
    await replacement.close()
    assert replacement.exit_confirmed

    assert unregistered == ["codex-native-test-tag"]


@pytest.mark.posix_only
async def test_discovery_stop_reaps_before_bounded_stderr_drain(monkeypatch) -> None:
    """A surviving child can keep stderr open after its wrapper exits."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    class _ExitedProcess:
        returncode = 0

        async def wait(self) -> int:
            return 0

    reaped: list[int | None] = []

    async def _reap(pgid: int | None, _tag: str | None) -> bool:
        reaped.append(pgid)
        return True

    async def _hold_stderr() -> str:
        await asyncio.Event().wait()
        return ""

    monkeypatch.setattr(codex_native_app_server._proc, "terminate_tree", lambda _proc: None)
    monkeypatch.setattr(codex_native_app_server, "_reap_process_group_survivors", _reap)
    stderr_task = asyncio.create_task(_hold_stderr())
    discovery = codex_native_app_server._CodexModelDiscoveryProcess(
        process=_ExitedProcess(),  # type: ignore[arg-type]
        stderr_tail=stderr_task,
        process_group_id=456,
        session_tag="test-tag",
    )
    monkeypatch.setattr(
        codex_native_app_server, "unregister_codex_native_process", lambda _tag: None
    )

    await asyncio.wait_for(
        codex_native_app_server._stop_codex_model_discovery_process(discovery), timeout=1.5
    )
    assert reaped == [456]
    assert stderr_task.cancelled()


@pytest.mark.parametrize("has_exited_process", [False, True])
async def test_non_posix_reclose_does_not_confirm_descendant_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, has_exited_process: bool
) -> None:
    from omnigent.harnesses.codex_native import app_server as module

    async def no_posix_group(*_args) -> bool:
        return True

    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module, "_reap_process_group_survivors", no_posix_group)
    server = _test_app_server(tmp_path, tmp_path / "home", tmp_path / "bridge", tmp_path)
    if has_exited_process:
        server.proc = SimpleNamespace(returncode=0)  # type: ignore[assignment]
    await server.close()
    assert not server.exit_confirmed
    assert server.process_exit_confirmed is has_exited_process
    await server.close()
    assert not server.exit_confirmed


async def test_close_serializes_restart_and_skips_stale_queued_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.harnesses.codex_native import app_server as module

    server = _test_app_server(tmp_path, tmp_path / "home", tmp_path / "bridge", tmp_path)
    server.proc = SimpleNamespace(returncode=0)  # type: ignore[assignment]
    server._resident_spawned = True
    old_id = server.resident_incarnation_id
    entered, resume = asyncio.Event(), asyncio.Event()
    groups: list[int | None] = []

    async def reap(group, _tag):
        groups.append(group)
        entered.set()
        await resume.wait()
        return True

    new_process = SimpleNamespace(returncode=None)

    async def spawn():
        server.resident_incarnation_id = "new-incarnation"
        server.proc = new_process  # type: ignore[assignment]
        server.exit_confirmed = False

    monkeypatch.setattr(module, "_reap_process_group_survivors", reap)
    monkeypatch.setattr(server, "_start", spawn)
    closing = asyncio.create_task(server.close())
    await asyncio.wait_for(entered.wait(), timeout=1)
    starting = asyncio.create_task(server.start())
    stale_close = asyncio.create_task(server.close())
    try:
        await asyncio.sleep(0)
        assert not starting.done()
        assert groups == [None]
    finally:
        resume.set()
        await asyncio.gather(closing, starting, stale_close)
    assert server.resident_incarnation_id != old_id
    assert server.proc is new_process
    assert not server.exit_confirmed
    assert groups == [None]


@pytest.mark.posix_only
async def test_close_surviving_posix_child_finishes_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.harnesses.codex_native import app_server as module

    class SurvivingProcess:
        returncode = None
        waits = 0

        async def wait(self):
            self.waits += 1
            if self.waits == 1:
                raise asyncio.TimeoutError
            await asyncio.Event().wait()

    class OwnerLock:
        closed = False

        def close(self):
            self.closed = True

    async def surviving_group(*_args):
        return False

    monkeypatch.setattr(module, "_reap_process_group_survivors", surviving_group)
    unregistered: list[str] = []
    monkeypatch.setattr(module, "unregister_codex_native_process", unregistered.append)
    monkeypatch.setattr(module, "_kill_process_tree", lambda _proc: pytest.fail("unverified kill"))
    process, lock = SurvivingProcess(), OwnerLock()
    server = CodexNativeAppServer(
        codex_path="/test/codex",
        socket_path=tmp_path / "server.sock",
        codex_home=tmp_path / "codex-home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
        proc=process,
        process_registry_tag="test-tag",
        process_group_id=456,
        process_owner_lock=lock,
    )
    server._resident_spawned = True
    incarnation_id = server.resident_incarnation_id
    await asyncio.wait_for(server.close(), timeout=0.5)
    assert process.waits == 1
    assert lock.closed
    assert server.proc is None
    assert server.process_owner_lock is None
    assert not server.exit_confirmed
    assert server.process_group_id == 456 and server.process_registry_tag == "test-tag"
    with pytest.raises(RuntimeError, match="exit is not confirmed"):
        await server.start()
    assert server.resident_incarnation_id == incarnation_id
    assert unregistered == []


@pytest.mark.posix_only
async def test_close_live_process_uses_verified_group_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import signal

    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.harnesses.codex_native import process_registry as codex_process_registry

    class _LiveProcess:
        returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    alive = True
    signals: list[int] = []

    def _ps(_columns: str) -> str:
        if alive:
            return f" 222 456 {os.getuid()} node omnigent_crash_teardown_tag=test-tag\n"
        return ""

    def _killpg(_pgid: int, sig: int) -> None:
        nonlocal alive
        if sig == 0:
            if not alive:
                raise ProcessLookupError
            return
        signals.append(sig)
        alive = False

    monkeypatch.setattr(codex_process_registry, "_ps_output", _ps)
    monkeypatch.setattr(codex_native_app_server.os, "killpg", _killpg)
    monkeypatch.setattr(
        codex_native_app_server,
        "_terminate_process_tree",
        lambda _process: pytest.fail("unverified tree termination"),
    )
    unregistered: list[str] = []
    monkeypatch.setattr(
        codex_native_app_server, "unregister_codex_native_process", unregistered.append
    )
    server = CodexNativeAppServer(
        codex_path="/test/codex",
        socket_path=tmp_path / "server.sock",
        codex_home=tmp_path / "codex-home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
        proc=_LiveProcess(),  # type: ignore[arg-type]
        process_registry_tag="test-tag",
        process_group_id=456,
    )

    await server.close()
    assert signals == [signal.SIGTERM]
    assert unregistered == ["test-tag"]
    assert server.exit_confirmed


@pytest.mark.posix_only
@pytest.mark.parametrize("returncode", [None, 0], ids=["live", "exited"])
async def test_close_cancellation_cleans_stderr_and_context_tasks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int | None
) -> None:
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    reached = asyncio.Event()

    async def _reap(_pgid: int | None, _tag: str | None) -> bool:
        reached.set()
        await asyncio.Event().wait()
        return True

    async def _pending() -> None:
        await asyncio.Event().wait()

    class _Process:
        def __init__(self) -> None:
            self.returncode = returncode

    closed: list[bool] = []
    unregistered: list[str] = []
    monkeypatch.setattr(codex_native_app_server, "_reap_process_group_survivors", _reap)
    monkeypatch.setattr(
        codex_native_app_server, "unregister_codex_native_process", unregistered.append
    )
    server = CodexNativeAppServer(
        codex_path="/test/codex",
        socket_path=tmp_path / "server.sock",
        codex_home=tmp_path / "codex-home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
        proc=_Process(),  # type: ignore[arg-type]
        process_registry_tag="test-tag",
        process_group_id=456,
        process_owner_lock=type("Lock", (), {"close": lambda _self: closed.append(True)})(),  # type: ignore[arg-type]
    )
    stderr_task = asyncio.create_task(_pending())
    context_task = asyncio.create_task(_pending())
    server.stderr_task = stderr_task
    server.context_catalog_task = context_task

    close_task = asyncio.create_task(server.close())
    await asyncio.wait_for(reached.wait(), timeout=1.0)
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert closed == [True]
    assert unregistered == []
    assert stderr_task.cancelled()
    assert context_task.cancelled()
    assert not server.exit_confirmed


@pytest.mark.posix_only
async def test_app_server_group_reaper_refuses_unsafe_and_untagged_groups(monkeypatch) -> None:
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.harnesses.codex_native import process_registry as codex_process_registry

    monkeypatch.setattr(
        codex_process_registry,
        "_ps_output",
        lambda _columns: f" 222 456 {os.getuid()} node not_omnigent_crash_teardown_tag=tag-123\n",
    )
    killed: list[int] = []
    monkeypatch.setattr(
        codex_native_app_server.os, "killpg", lambda _pgid, sig: killed.append(sig)
    )

    assert not await codex_native_app_server._reap_process_group_survivors(456, "tag-123")
    assert not await codex_native_app_server._reap_process_group_survivors(1, "tag-123")
    assert not await codex_native_app_server._reap_process_group_survivors(os.getpgrp(), "tag-123")
    assert killed == [0]


@pytest.mark.posix_only
@pytest.mark.parametrize(
    "outcome", ["absent", "present", "denied", "failed", "enumeration_unknown"]
)
async def test_group_exit_receipt_requires_kernel_absence(monkeypatch, outcome):
    from omnigent.harnesses.codex_native import app_server as module

    monkeypatch.setattr(
        module,
        "_tagged_process_group_members",
        lambda *_: None if outcome == "enumeration_unknown" else [],
    )
    probes = []

    def probe(pgid, sig):
        assert (pgid, sig) == (456, 0)
        probes.append(pgid)
        if outcome == "absent":
            raise ProcessLookupError
        if outcome == "denied":
            raise PermissionError
        if outcome == "failed":
            raise OSError("unavailable")

    monkeypatch.setattr(module.os, "killpg", probe)
    assert await module._reap_process_group_survivors(456, "fixture-tag") is (outcome == "absent")
    assert probes == ([] if outcome == "enumeration_unknown" else [456])


async def test_non_posix_group_exit_is_unavailable(monkeypatch):
    from omnigent.harnesses.codex_native import app_server as module

    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    assert not await module._reap_process_group_survivors(456, "fixture-tag")


@pytest.mark.posix_only
async def test_unconfirmed_close_retries_the_same_recorded_group(monkeypatch, tmp_path):
    from omnigent.harnesses.codex_native import app_server as module

    server = _test_app_server(tmp_path, tmp_path / "home", tmp_path / "bridge", tmp_path)
    server.proc = SimpleNamespace(returncode=0)
    server._resident_spawned = True
    server.process_group_id, server.process_registry_tag = 456, "fixture-tag"
    incarnation = server.resident_incarnation_id
    alive, probes, unregistered = True, [], []
    monkeypatch.setattr(module, "_tagged_process_group_members", lambda *_: [])

    def probe(pgid, sig):
        assert (pgid, sig) == (456, 0)
        probes.append(pgid)
        if not alive:
            raise ProcessLookupError

    monkeypatch.setattr(module.os, "killpg", probe)
    monkeypatch.setattr(module, "unregister_codex_native_process", unregistered.append)
    await server.close()
    assert server.process_exit_confirmed and not server.exit_confirmed
    assert (server.process_group_id, server.process_registry_tag) == (456, "fixture-tag")
    assert server.proc is server.process_owner_lock is None
    assert unregistered == []
    with pytest.raises(RuntimeError, match="exit is not confirmed"):
        await server.start()
    alive = False
    await server.close()
    assert server.exit_confirmed and server.resident_incarnation_id == incarnation
    assert server.process_group_id is server.process_registry_tag is None
    assert unregistered == ["fixture-tag"] and probes == [456, 456]


@pytest.mark.posix_only
async def test_real_untagged_group_is_not_an_exit_receipt(monkeypatch):
    from omnigent.harnesses.codex_native import app_server as module

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True
    )
    monkeypatch.setattr(module, "_tagged_process_group_members", lambda *_: [])
    try:
        assert not await module._reap_process_group_survivors(process.pid, "fixture-tag")
        assert process.returncode is None
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    assert await module._reap_process_group_survivors(process.pid, "fixture-tag")


@pytest.mark.posix_only
async def test_model_discovery_spawn_failure_releases_owner_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed probe spawn releases the owner lock instead of leaking it."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.harnesses.codex_native import process_registry as codex_process_registry

    registry_path, calls = _isolated_discovery_registry(monkeypatch, tmp_path)
    real_acquire = codex_process_registry.acquire_codex_native_process_owner_lock
    acquired: list[Path] = []

    def _acquire() -> object:
        lock = real_acquire()
        if lock is not None:
            acquired.append(Path(lock.path))
        return lock

    async def _failing_spawn(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError("exec failed")

    monkeypatch.setattr(
        codex_native_app_server, "acquire_codex_native_process_owner_lock", _acquire
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _failing_spawn)

    with pytest.raises(OSError, match="exec failed"):
        await codex_native_app_server._start_codex_model_discovery_process(
            codex_path="/test/codex",
            listen_url="ws://127.0.0.1:12347",
            env={},
            cwd=tmp_path,
        )

    assert len(acquired) == 1
    assert not acquired[0].exists()
    assert _read_discovery_registry_entries(registry_path) == []
    assert calls["reconciled"] == 1


@pytest.mark.posix_only
def test_model_probe_parent_kill_reaps_only_its_own_tree(tmp_path: Path) -> None:
    """A killed test/dev owner cannot strand a probe, even in another session."""
    parent_code = """import asyncio, os, sys
from pathlib import Path
from omnigent.harnesses.codex_native.app_server import _start_codex_model_discovery_process
async def run():
 root = Path(sys.argv[1])
 os.environ['OMNIGENT_CODEX_NATIVE_STATE_DIR'] = str(root / 'state')
 discovery = await _start_codex_model_discovery_process(
  codex_path=str(root / 'codex'), listen_url='ws://127.0.0.1:1',
  env=dict(os.environ), cwd=root)
 (root / 'wrapper').write_text(str(discovery.process.pid))
 await asyncio.Event().wait()
asyncio.run(run())
"""
    parents: list[subprocess.Popen] = []
    trees: list[list[psutil.Process]] = []
    try:
        for name in ("killed", "live"):
            root = tmp_path / name
            root.mkdir()
            fake = root / "codex"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import json, os, signal, subprocess, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)'], start_new_session=True)\n"
                f"with open({str(root / 'children')!r}, 'w') as f:\n"
                " f.write(json.dumps([os.getpid(), child.pid]))\n"
                "time.sleep(60)\n"
            )
            fake.chmod(0o755)
            parent = subprocess.Popen([sys.executable, "-c", parent_code, str(root)])
            parents.append(parent)
            poll_until(
                lambda root=root: (root / "children").exists() and (root / "wrapper").exists(),
                timeout_s=15,
                interval_s=0.05,
            )
            pids = [
                int((root / "wrapper").read_text()),
                *json.loads((root / "children").read_text()),
            ]
            trees.append([psutil.Process(pid) for pid in pids])
        # Let the existing wrapper census observe the child that called setsid.
        time.sleep(0.3)
        parents[0].kill()
        parents[0].wait(timeout=5)
        poll_until(
            lambda: all(not _proc.process_alive(proc.pid) for proc in trees[0]),
            timeout_s=8,
            interval_s=0.05,
        )
        assert parents[1].poll() is None
        assert all(
            proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE for proc in trees[1]
        )
    finally:
        for parent in parents:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
        for tree in trees:
            for proc in tree:
                with contextlib.suppress(psutil.NoSuchProcess):
                    proc.send_signal(signal.SIGKILL)


@pytest.mark.posix_only
async def test_cancelled_probe_spawn_closes_its_liveness_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancellation before the spawn returns still stops the unregistered child."""
    from omnigent.harnesses.codex_native import app_server

    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(app_server, "reconcile_codex_native_process_registry", lambda: None)
    ready = tmp_path / "ready"
    fake = tmp_path / "codex"
    fake.write_text(
        f"#!{sys.executable}\nimport os, time\n"
        f"open({str(ready)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    )
    fake.chmod(0o755)
    real_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []

    async def delayed_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        await poll_until_async(ready.exists, timeout_s=10)
        spawned.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(
        app_server._start_codex_model_discovery_process(
            codex_path=str(fake),
            listen_url="ws://127.0.0.1:1",
            env=dict(os.environ),
            cwd=tmp_path,
        )
    )
    child = None
    try:
        await asyncio.wait_for(spawned.wait(), timeout=15)
        child = psutil.Process(int(ready.read_text()))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(processes[0].wait(), timeout=8)
        await poll_until_async(lambda: not _proc.process_alive(child.pid), timeout_s=3)
        assert not list((tmp_path / "state" / "process-owners").glob("*.lock"))
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if child is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
