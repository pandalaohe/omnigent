"""
Tests for :class:`omnigent.runtime.harnesses.process_manager.HarnessProcessManager`.

Covers the lifecycle / behavior surface defined by §Process
management of ``designs/SERVER_HARNESS_CONTRACT.md``: lazy spawn,
caching, crash detection, release, idle reaping, orphan sweep.

The tests use the real fixture harness module
``tests.runtime.harnesses._test_harness`` (a minimal FastAPI app
with ``/health`` + introspection endpoints) wired through a real
runner subprocess. End-to-end with real uvicorn rather than a mock
because the spawn handshake (waiting for the socket to appear) is
what's most likely to break.

Tests intentionally use a short tmp parent dir (``/tmp/omnigent-tests-...``)
rather than pytest's :data:`tmp_path` because macOS's
``AF_UNIX`` socket path limit is ~104 chars and pytest's
per-test temp dirs (under ``/private/var/folders/...``) push the
full socket path past that ceiling. Each test cleans up its
parent dir on teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.native.owner_claim import current_pid_namespace, read_owner_claim, write_owner_claim
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.runtime.harnesses.paths import resolve_harness_tmp_parent
from omnigent.runtime.harnesses.process_manager import (
    _AP_PID_FILE,
    _TMP_PARENT_ENV_VAR,
    HarnessProcessManager,
    NoLiveHarnessError,
    _default_tmp_parent,
    _kill_orphan_runners,
    _model_env_key,
    _pid_alive,
    _pids_holding_socket,
    _SubprocessEntry,
    sweep_orphaned_harness_processes,
)

_TEST_HARNESS_NAME = "test"
_TEST_HARNESS_MODULE = "tests.runtime.harnesses._test_harness"


@pytest.fixture
def register_test_harness() -> Iterator[None]:
    """
    Add the test fixture harness to ``_HARNESS_MODULES`` for the
    test, removing it on teardown so other tests see a clean
    registry.

    :yields: Nothing — fixture exists for the side effect.
    """
    _HARNESS_MODULES[_TEST_HARNESS_NAME] = _TEST_HARNESS_MODULE
    try:
        yield
    finally:
        _HARNESS_MODULES.pop(_TEST_HARNESS_NAME, None)


@pytest.fixture
def short_tmp_parent() -> Iterator[Path]:
    """
    Per-test parent directory under a short writable temp root.

    macOS limits AF_UNIX socket paths to ~104 characters; pytest's
    :data:`tmp_path` resolves to ``/private/var/folders/...``
    which already eats most of that budget. Use a short
    ``/tmp/omni-pm-<short_uuid>`` parent when possible, falling
    back to :func:`tempfile.gettempdir` for sandboxes where the
    host's real ``/tmp`` is not writable.
    """
    roots = [Path("/tmp")]
    temp_root = Path(tempfile.gettempdir())
    if temp_root not in roots:
        roots.append(temp_root)

    last_error: OSError | None = None
    for root in roots:
        parent = root / f"omni-pm-{uuid.uuid4().hex[:8]}"
        try:
            parent.mkdir(mode=0o700)
        except OSError as exc:
            last_error = exc
            continue
        try:
            yield parent
        finally:
            shutil.rmtree(parent, ignore_errors=True)
        return

    assert last_error is not None
    raise last_error


@pytest.fixture
def manager(
    short_tmp_parent: Path,
    register_test_harness: None,
) -> HarnessProcessManager:
    """
    Manager rooted in an isolated tmp dir, with the test harness
    pre-registered.

    Tests own ``await manager.start()`` / ``shutdown()`` so they
    can assert state across the lifecycle.
    """
    return HarnessProcessManager(
        # Aggressive defaults so reaper / orphan tests don't have
        # to wait minutes. Individual tests override per-case.
        idle_timeout_s=60.0,
        reaper_interval_s=60.0,
        tmp_parent=short_tmp_parent,
    )


# ── Boot / shutdown ─────────────────────────────────────────────


async def test_get_client_before_start_raises(
    manager: HarnessProcessManager,
) -> None:
    """Calling get_client before start() is a programming error.

    Catches a regression where the lazy-init path silently
    initialized state without booting the reaper — the reaper
    not running would then leak subprocesses indefinitely.
    """
    with pytest.raises(RuntimeError, match="before start"):
        await manager.get_client("conv_x", _TEST_HARNESS_NAME)


async def test_start_creates_instance_dir_with_sentinel(
    manager: HarnessProcessManager,
) -> None:
    """start() creates the per-AP-instance dir and writes AP_PID.

    The sentinel is what the orphan sweep keys off; without it,
    a subsequent Omnigent boot can't tell live instances from dead.
    """
    await manager.start()
    try:
        assert manager.instance_dir.is_dir()
        sentinel = manager.instance_dir / _AP_PID_FILE
        assert sentinel.exists()
        # The recorded owner is this process in this PID namespace — proves the
        # sweep on a sibling Omnigent boot would correctly identify us as alive.
        claim = read_owner_claim(manager.instance_dir, filename=_AP_PID_FILE)
        assert claim is not None
        assert claim.pid == os.getpid()
        assert claim.pid_ns == current_pid_namespace()
    finally:
        await manager.shutdown()


async def test_start_can_delegate_orphan_sweep_to_host(short_tmp_parent: Path) -> None:
    """Host-spawned runners can start without scanning machine-global state."""
    stale_dir = short_tmp_parent / "ap-dead"
    stale_dir.mkdir(mode=0o700)
    write_owner_claim(stale_dir, filename=_AP_PID_FILE)
    marker = stale_dir / _AP_PID_FILE
    marker.write_text(marker.read_text().replace(str(os.getpid()), "99999999", 1))

    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await manager.start(sweep_orphans=False)
    try:
        assert stale_dir.exists()
        assert manager.instance_dir.exists()
    finally:
        await manager.shutdown()

    await sweep_orphaned_harness_processes(tmp_parent=short_tmp_parent)
    assert not stale_dir.exists()


async def test_start_is_idempotent(manager: HarnessProcessManager) -> None:
    """A second start() is a no-op; doesn't recreate / relaunch.

    Lifespan handlers sometimes fire start() more than once
    during AP's startup; the second call must not clobber state.
    """
    await manager.start()
    try:
        first_dir = manager.instance_dir
        await manager.start()
        # Same dir, still has sentinel, still serves clients.
        assert manager.instance_dir == first_dir
        assert (manager.instance_dir / _AP_PID_FILE).exists()
    finally:
        await manager.shutdown()


async def test_start_uses_harness_tmp_parent_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The production default socket root can be moved by env.

    Local and hosted runtimes sometimes deny writes to the host's
    real ``/tmp``. The process manager should honor the deployment
    knob without requiring the FastAPI server to thread a test-only
    constructor argument through production wiring.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_TMP_PARENT_ENV_VAR, "harness-sockets")
    manager = HarnessProcessManager()
    await manager.start()
    try:
        assert manager.instance_dir.parent == Path("harness-sockets")
        assert (manager.instance_dir / _AP_PID_FILE).read_text(encoding="utf-8")
    finally:
        await manager.shutdown()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Per-uid tmp parent is POSIX-only; Windows uses gettempdir().",
)
def test_default_tmp_parent_is_per_uid_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default socket root is namespaced per Unix uid.

    A shared ``/tmp/omnigent`` breaks multi-user hosts: the first
    user's runner creates it ``0700`` and every other user's runner
    then dies in ``_sweep_orphans`` stat()ing foreign instance dirs.
    The POSIX default must carry the uid so each user gets a private
    parent. Regression guard against the pre-fix bare ``/tmp/omnigent``.
    """
    monkeypatch.delenv(_TMP_PARENT_ENV_VAR, raising=False)
    parent = _default_tmp_parent()
    assert parent == Path(f"/tmp/omnigent-{os.getuid()}")
    # The shared parent that locked out other users must be gone.
    assert parent != Path("/tmp/omnigent")


@pytest.mark.skipif(sys.platform == "win32", reason="Symlink path is POSIX-only.")
def test_resolve_harness_tmp_parent_preserves_symlink_spelling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "private" / "tmp"
    target.mkdir(parents=True)
    short_root = tmp_path / "tmp"
    short_root.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv(_TMP_PARENT_ENV_VAR, str(short_root))

    resolved = resolve_harness_tmp_parent()

    assert resolved == short_root
    assert resolved != target.resolve()


def test_resolve_harness_tmp_parent_makes_relative_path_absolute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_TMP_PARENT_ENV_VAR, "nested/../harness-sockets")

    resolved = resolve_harness_tmp_parent()

    assert resolved == tmp_path / "harness-sockets"


async def test_shutdown_without_start_is_noop(
    manager: HarnessProcessManager,
) -> None:
    """shutdown() before start() should not raise.

    Defensive — AP's lifespan teardown might run after a failed
    boot where start() never completed. shutdown() should be
    safe to call regardless.
    """
    # No start(); just shutdown.
    await manager.shutdown()


# ── Spawn / cache / crash ──────────────────────────────────────


async def _ping_health(client) -> None:  # type: ignore[no-untyped-def]
    """Drive a /health round-trip; raises on non-200 to fail the test."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_get_client_spawns_and_serves(
    manager: HarnessProcessManager,
) -> None:
    """First get_client spawns the runner; /health round-trips.

    End-to-end smoke test through the real runner. If this fails
    most likely culprits are: PYTHONPATH not propagating to the
    subprocess (the test fixture module won't import); the spawn
    handshake (socket appearance polling) breaking; or the
    runner CLI plumbing breaking.
    """
    await manager.start()
    try:
        client = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        await _ping_health(client)
        # The runner stashed the conversation id on app.state per
        # the contract; verify the round-trip works.
        cid_resp = await client.get("/conversation-id")
        assert cid_resp.json() == {"conversation_id": "conv_a"}
    finally:
        await manager.shutdown()


async def test_get_client_emits_harness_started_event(
    manager: HarnessProcessManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful spawn emits one ``harness_started`` debug event.

    This is the observable "harness bound and ready" edge used to measure
    startup latency; the row carries the spawn->ready time and, for the
    first spawn on a cold runner, the manager-start-relative time.
    """
    await manager.start()
    try:
        with caplog.at_level(logging.INFO, logger="omnigent.runtime.harnesses.process_manager"):
            await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        started = [
            r for r in caplog.records if getattr(r, "event_name", None) == "harness_started"
        ]
        assert len(started) == 1
        record = started[0]
        assert record.session_id == "conv_a"
        attrs = record.attributes
        assert attrs["harness"] == _TEST_HARNESS_NAME
        assert isinstance(attrs["pid"], int)
        assert attrs["spawn_ms"] >= 0
        assert attrs["since_manager_start_ms"] >= 0
    finally:
        await manager.shutdown()


async def test_get_client_caches_subprocess(
    manager: HarnessProcessManager,
) -> None:
    """Subsequent get_client calls reuse the same subprocess.

    Verified by hitting /pid twice and asserting the same PID —
    if the manager were respawning per call, the PIDs would
    differ. This test exists because spawn cost is non-trivial
    (1–3s per uvicorn boot) and the contract explicitly says
    one subprocess per conversation, lazy.
    """
    await manager.start()
    try:
        client_a = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        pid_first = (await client_a.get("/pid")).json()["pid"]
        # Same conv_id again — should hit the cached client.
        client_a_again = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        assert client_a_again is client_a
        pid_second = (await client_a_again.get("/pid")).json()["pid"]
        # Same subprocess proves no respawn happened on the
        # second call.
        assert pid_first == pid_second
    finally:
        await manager.shutdown()


async def test_get_client_isolates_per_conversation(
    manager: HarnessProcessManager,
) -> None:
    """Different conversations get different subprocesses.

    Each conversation_id maps to its own subprocess per the
    contract (one-conversation-one-process). The isolation is
    enforced by separate Unix sockets keyed on conv_id; this
    test verifies the keying actually hits distinct PIDs.
    """
    await manager.start()
    try:
        client_a = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        client_b = await manager.get_client("conv_b", _TEST_HARNESS_NAME)
        assert client_a is not client_b
        pid_a = (await client_a.get("/pid")).json()["pid"]
        pid_b = (await client_b.get("/pid")).json()["pid"]
        # Different subprocesses → different PIDs. If they matched
        # we'd be running both conversations through one process,
        # violating the isolation guarantee.
        assert pid_a != pid_b
    finally:
        await manager.shutdown()


async def test_release_terminates_subprocess(
    manager: HarnessProcessManager,
) -> None:
    """release() ends the subprocess and cleans up the socket.

    Verifies both halves of the teardown contract: the OS
    process exits AND the socket file is removed. Either gap
    leaks resources across conversation lifetimes.
    """
    await manager.start()
    try:
        client = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        # Capture the subprocess PID via the introspection endpoint;
        # if release worked, the PID won't be alive after.
        pid = (await client.get("/pid")).json()["pid"]
        socket_path = manager.instance_dir / "conv-conv_a.sock"
        assert socket_path.exists()
        await manager.release("conv_a")
        # Socket cleanup is part of release's contract — leaving
        # the file behind would fail uvicorn binding on a
        # subsequent spawn for the same conv id.
        assert not socket_path.exists()
        # Process should be gone — give the OS a brief moment
        # since SIGTERM → wait is async.
        for _ in range(20):
            if not _pid_alive(pid):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(pid)
    finally:
        await manager.shutdown()


async def test_close_entry_kills_process_when_aclose_raises(
    manager: HarnessProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``client.aclose()`` must not skip the subprocess kill.

    ``_close_entry`` used to ``await client.aclose()`` first with no guard, so a
    raise there (a broken transport, a wedged client) skipped the SIGTERM/SIGKILL
    below and the subprocess leaked un-killed — and untracked, since ``release``
    already popped the entry. Force ``aclose()`` to raise and assert the process
    is still terminated (and ``release`` itself doesn't raise, since teardown is
    now best-effort).
    """
    await manager.start()
    try:
        client = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        pid = (await client.get("/pid")).json()["pid"]
        entry = manager._entries["conv_a"]

        async def _boom() -> None:
            raise RuntimeError("simulated aclose failure")

        monkeypatch.setattr(entry.client, "aclose", _boom)

        # release() -> _close_entry(): the aclose failure must not prevent the
        # kill, and best-effort teardown means release itself completes.
        await manager.release("conv_a")
        assert "conv_a" not in manager._entries

        for _ in range(20):
            if not _pid_alive(pid):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(pid), "subprocess survived a teardown where aclose() raised"
    finally:
        await manager.shutdown()


async def test_release_reaps_term_resistant_process_when_graceful_wait_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled graceful wait still force-kills and reaps a real child."""
    from omnigent.runtime.harnesses import process_manager as process_manager_module

    class _Client:
        async def aclose(self) -> None:
            return None

    class _Endpoint:
        def __init__(self) -> None:
            self.cleaned = False

        def cleanup(self) -> None:
            self.cleaned = True

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        (
            "import signal, sys, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); "
            "time.sleep(60)"
        ),
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert await asyncio.wait_for(process.stdout.readline(), timeout=10.0) == b"ready\n"
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    endpoint = _Endpoint()
    transport_closes: list[object] = []
    terminate_started = asyncio.Event()
    real_terminate_tree = process_manager_module._proc.terminate_tree
    real_close_transport = process_manager_module.close_subprocess_transport

    def terminate_tree(target: object) -> None:
        real_terminate_tree(target)  # type: ignore[arg-type]
        terminate_started.set()

    def close_transport(target: object) -> None:
        transport_closes.append(target)
        real_close_transport(target)

    monkeypatch.setattr(process_manager_module._proc, "terminate_tree", terminate_tree)
    monkeypatch.setattr(process_manager_module, "close_subprocess_transport", close_transport)
    manager = object.__new__(HarnessProcessManager)
    manager._entries = {}
    manager._closing_entries = {}
    manager._exit_coverage_complete = True
    manager._spawn_locks = {"conv_real": asyncio.Lock()}
    manager._registry_lock = asyncio.Lock()
    manager._release_generations = {}
    manager._in_flight_response_ids = {}
    manager._retention_managed = {}
    entry = process_manager_module._SubprocessEntry(
        process,
        _Client(),
        endpoint,
        "test",
    )
    manager._entries["conv_real"] = entry
    closing = asyncio.create_task(manager.release("conv_real"))
    try:
        await asyncio.wait_for(terminate_started.wait(), timeout=10.0)
        assert process.returncode is None, "fixture child unexpectedly honored SIGTERM"
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, timeout=10.0)

        assert process.returncode is not None
        assert await asyncio.wait_for(process.wait(), timeout=1.0) == process.returncode
        assert "conv_real" not in manager._entries
        assert transport_closes == [process]
        assert endpoint.cleaned
    finally:
        if not closing.done():
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("cancel_stage", ["force_wait", "client_close"])
async def test_release_reaps_process_when_teardown_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    cancel_stage: str,
) -> None:
    """Cancellation during force wait or client close still cleans up."""
    from omnigent.runtime.harnesses import process_manager as process_manager_module

    class _BlockingProcess:
        pid = 424242
        returncode: int | None = None

        def __init__(self) -> None:
            self.wait_started = asyncio.Event()
            self.force_wait_started = asyncio.Event()
            self.wait_calls = 0
            self.dead = False

        async def wait(self) -> None:
            self.wait_calls += 1
            if self.wait_calls == 1:
                self.wait_started.set()
                if self.returncode is None:
                    await asyncio.Event().wait()
            elif cancel_stage == "force_wait":
                self.force_wait_started.set()
                await asyncio.Event().wait()
            return self.returncode

    class _Client:
        def __init__(self) -> None:
            self.aclose_started = asyncio.Event()

        async def aclose(self) -> None:
            if cancel_stage == "client_close":
                self.aclose_started.set()
                await asyncio.Event().wait()

    class _Endpoint:
        def __init__(self) -> None:
            self.cleaned = False

        def cleanup(self) -> None:
            self.cleaned = True

    process = _BlockingProcess()
    endpoint = _Endpoint()
    client = _Client()
    transport_closes: list[object] = []

    def terminate_tree(_process: object) -> None:
        if cancel_stage == "client_close":
            process.returncode = -15

    def kill_tree(_process: object) -> None:
        process.dead = True
        process.returncode = -9

    monkeypatch.setattr(process_manager_module._proc, "terminate_tree", terminate_tree)
    monkeypatch.setattr(process_manager_module._proc, "kill_tree", kill_tree)
    monkeypatch.setattr(
        process_manager_module,
        "close_subprocess_transport",
        lambda process: transport_closes.append(process),
    )
    manager = object.__new__(HarnessProcessManager)
    manager._entries = {}
    manager._closing_entries = {}
    manager._exit_coverage_complete = True
    manager._spawn_locks = {"conv_a": asyncio.Lock()}
    manager._registry_lock = asyncio.Lock()
    manager._release_generations = {}
    manager._in_flight_response_ids = {}
    manager._retention_managed = {}
    entry = process_manager_module._SubprocessEntry(
        process,
        client,
        endpoint,
        "test",
    )
    manager._entries["conv_a"] = entry

    closing = asyncio.create_task(manager.release("conv_a"))
    if cancel_stage == "client_close":
        await asyncio.wait_for(client.aclose_started.wait(), timeout=1.0)
    else:
        await asyncio.wait_for(process.wait_started.wait(), timeout=1.0)
        closing.cancel()
        await asyncio.wait_for(process.force_wait_started.wait(), timeout=1.0)
    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert "conv_a" not in manager._entries
    assert process.returncode is not None
    if cancel_stage == "force_wait":
        assert process.dead
    assert process.wait_calls == (2 if cancel_stage == "force_wait" else 1)
    assert transport_closes == [process]
    assert endpoint.cleaned


@pytest.mark.parametrize("response_id", [None, "resp_crashed"])
async def test_get_client_respawns_after_crash(
    manager: HarnessProcessManager,
    caplog: pytest.LogCaptureFixture,
    response_id: str | None,
) -> None:
    """If the subprocess died, the next get_client respawns.

    This covers the "harness crashed mid-conversation" branch in
    get_client. We simulate the crash by sending SIGKILL to the
    runner subprocess directly, then call get_client again and
    verify a new PID comes back.
    """
    await manager.start()
    try:
        client = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        original_pid = (await client.get("/pid")).json()["pid"]
        if response_id is not None:
            manager.mark_in_flight("conv_a", response_id)
            assert await manager.get_client("conv_a", _TEST_HARNESS_NAME) is client
            assert manager.has_active_turn("conv_a")
        os.kill(original_pid, signal.SIGKILL)
        # Wait for the OS to mark the process dead so the next
        # get_client's ``returncode`` check sees it.
        for _ in range(40):
            if not _pid_alive(original_pid):
                break
            await asyncio.sleep(0.05)
        # Now get_client should detect the corpse and respawn.
        new_client = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        new_pid = (await new_client.get("/pid")).json()["pid"]
        # New PID proves a fresh subprocess; same PID would mean
        # crash detection is broken.
        assert new_pid != original_pid
        assert _pid_alive(new_pid)
        assert not manager.has_active_turn("conv_a")
        assert await manager.forward_cancel("conv_a") is False
        manager._entries["conv_a"].last_used_at = time.monotonic() - 120.0
        await manager.release("conv_a", only_if_idle_cutoff=time.monotonic() - 60.0)
        assert "conv_a" not in manager._entries
        exits = [
            r for r in caplog.records if getattr(r, "event_name", None) == "harness_exit_detected"
        ]
        assert len(exits) == 1
        assert exits[0].session_id == "conv_a"
        assert exits[0].attributes == {
            "harness": _TEST_HARNESS_NAME,
            "pid": original_pid,
            "returncode": -signal.SIGKILL,
            "tracked_response_id": response_id,
        }
    finally:
        await manager.shutdown()


@pytest.mark.posix_only
@pytest.mark.parametrize("response_id", [None, "resp_socket_lost"])
async def test_get_client_respawns_when_socket_pathname_vanishes(
    manager: HarnessProcessManager,
    caplog: pytest.LogCaptureFixture,
    response_id: str | None,
) -> None:
    """A live harness whose socket file was removed is replaced on the next turn.

    The subprocess never exits, so ``returncode`` stays ``None``; only the
    bound Unix socket pathname disappears (a ``/tmp`` cleaner). Once the
    pooled keepalive connection expires, a cached client can no longer
    connect, so ``get_client`` must retire the stale entry and hand back a
    client for a fresh harness. A turn in flight on the retired process is
    reported through the respawn hook so the runner can resync it.
    """
    hook_calls: list[tuple[str, str, str]] = []

    async def record_respawn(conv_id: str, reason: str, replaced: str) -> None:
        hook_calls.append((conv_id, reason, replaced))

    manager.set_respawn_hook(record_respawn)
    await manager.start()
    try:
        client = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        original_pid = (await client.get("/pid")).json()["pid"]
        if response_id is not None:
            manager.mark_in_flight("conv_a", response_id)
        socket_path = manager.socket_path("conv_a")
        assert socket_path.exists()
        socket_path.unlink()
        assert _pid_alive(original_pid)

        await asyncio.sleep(httpx.Limits().keepalive_expiry + 1.0)
        assert _pid_alive(original_pid), "fixture harness exited during the keepalive wait"

        # Control callers never trigger recovery and keep the live turn's client.
        assert await manager.get_client("conv_a", "any") is client
        assert _pid_alive(original_pid)

        new_client = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        new_pid = (await new_client.get("/pid")).json()["pid"]
        assert new_pid != original_pid
        assert _pid_alive(new_pid)
        assert socket_path.exists()
        assert not _pid_alive(original_pid)
        assert not manager.has_active_turn("conv_a")
        if response_id is None:
            assert hook_calls == []
        else:
            assert hook_calls == [("conv_a", "harness_respawn_socket_missing", response_id)]
        lost = [
            r for r in caplog.records if getattr(r, "event_name", None) == "harness_socket_missing"
        ]
        assert len(lost) == 1
        assert lost[0].session_id == "conv_a"
        assert lost[0].attributes == {
            "harness": _TEST_HARNESS_NAME,
            "pid": original_pid,
            "socket_path": str(socket_path),
            "tracked_response_id": response_id,
        }
    finally:
        await manager.shutdown()


async def test_release_clears_marker_before_same_session_replacement(
    manager: HarnessProcessManager,
) -> None:
    """An explicit retirement does not make a replacement look in-flight."""
    await manager.start()
    try:
        await manager.get_client("conv_release", _TEST_HARNESS_NAME)
        manager.mark_in_flight("conv_release", "resp_old")
        await manager.release("conv_release")
        assert not manager.has_active_turn("conv_release")

        replacement = await manager.get_client("conv_release", _TEST_HARNESS_NAME)
        assert replacement is not None
        assert not manager.has_active_turn("conv_release")
        assert await manager.forward_cancel("conv_release") is False
    finally:
        await manager.shutdown()


async def test_get_client_respawns_on_harness_change(
    manager: HarnessProcessManager,
) -> None:
    """A different harness for the same conversation respawns the subprocess.

    The socket is keyed by conversation only, so without the harness-change
    branch in get_client an in-place agent switch (which resolves a new
    harness for the same conv) would keep serving the OLD harness's
    subprocess. We register a second harness name pointing at the same
    fixture app, call get_client with each, and assert the PID changed —
    proving the old subprocess was torn down and a new one spawned.
    """
    # Second registry entry → same fixture app, different harness NAME.
    _HARNESS_MODULES["test2"] = _TEST_HARNESS_MODULE
    await manager.start()
    try:
        client_first = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        pid_first = (await client_first.get("/pid")).json()["pid"]
        manager.mark_in_flight("conv_a", "resp_switch")

        # Same conversation, DIFFERENT harness → must respawn.
        client_second = await manager.get_client("conv_a", "test2")
        pid_second = (await client_second.get("/pid")).json()["pid"]

        # Different PID proves the harness-change branch tore down the old
        # subprocess and spawned a new one. Same PID would mean the switch
        # kept serving the old harness (the bug this branch fixes).
        assert pid_second != pid_first
        assert not manager.has_active_turn("conv_a")
        assert _pid_alive(pid_second)
        # The original subprocess was terminated by the respawn's close.
        for _ in range(40):
            if not _pid_alive(pid_first):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(pid_first)
    finally:
        await manager.shutdown()
        _HARNESS_MODULES.pop("test2", None)


async def test_get_client_respawns_on_model_change(
    manager: HarnessProcessManager,
) -> None:
    """A different model for the same conversation respawns the subprocess.

    Mirrors the harness-change branch above: the model is baked into the
    subprocess env at spawn time, so a later turn with a different model
    must respawn, or the cached process keeps serving the old one. Covers
    the real ``/model`` switch path via ``post_responses`` in production,
    previously untested. Also checks the inverse: same model, no respawn.
    """
    await manager.start()
    try:
        client_first = await manager.get_client(
            "conv_a",
            _TEST_HARNESS_NAME,
            env={"HARNESS_TEST_MODEL": "model-a"},
        )
        pid_first = (await client_first.get("/pid")).json()["pid"]

        # Same conversation, SAME model → must reuse the cached subprocess.
        client_same = await manager.get_client(
            "conv_a",
            _TEST_HARNESS_NAME,
            env={"HARNESS_TEST_MODEL": "model-a"},
        )
        pid_same = (await client_same.get("/pid")).json()["pid"]
        assert pid_same == pid_first

        # Same conversation, DIFFERENT model → must respawn.
        client_second = await manager.get_client(
            "conv_a",
            _TEST_HARNESS_NAME,
            env={"HARNESS_TEST_MODEL": "model-b"},
        )
        pid_second = (await client_second.get("/pid")).json()["pid"]

        # Different PID proves the model-change branch tore down the old
        # subprocess and spawned a new one. Same PID would mean the model
        # switch kept serving the old model (the bug this branch guards).
        assert pid_second != pid_first
        assert _pid_alive(pid_second)
        # The original subprocess was terminated by the respawn's close.
        for _ in range(40):
            if not _pid_alive(pid_first):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(pid_first)
    finally:
        await manager.shutdown()


async def test_get_client_any_harness_sentinel_reuses_subprocess(
    manager: HarnessProcessManager,
) -> None:
    """``get_client(conv, "any")`` reuses the live subprocess — never respawns.

    ``"any"`` is the harness-AGNOSTIC sentinel that steering / cancel /
    interrupt callers pass to reach the already-running subprocess (it is not
    a real harness). It must NOT count as a harness mismatch — otherwise the
    harness-change branch tears down and respawns the harness on every such
    call, killing the in-flight turn. That is the regression that broke
    queued-message streaming: sending a 2nd message mid-turn issues a
    ``get_client(conv, "any")`` steering call, which respawned the live
    openai-agents subprocess and left the assistant with no output.
    """
    await manager.start()
    try:
        client_first = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        pid_first = (await client_first.get("/pid")).json()["pid"]

        # Harness-agnostic sentinel → must hit the cached client, not respawn.
        client_any = await manager.get_client("conv_a", "any")
        assert client_any is client_first
        pid_any = (await client_any.get("/pid")).json()["pid"]
        # Same PID proves no respawn. A different PID means the "any" sentinel
        # spuriously tripped the harness-change branch (the bug this guards).
        assert pid_any == pid_first
        assert _pid_alive(pid_first)
    finally:
        await manager.shutdown()


async def test_get_client_any_harness_sentinel_no_subprocess_raises(
    manager: HarnessProcessManager,
) -> None:
    """``get_client(conv, "any")`` raises ``NoLiveHarnessError`` when no
    subprocess is live.

    Before the fix, this fell through to ``_spawn_entry("any", ...)``
    which called ``_resolve_module_path("any")`` and raised the misleading
    ``RuntimeError: unknown harness 'any'; registered names: [...]``.
    """
    await manager.start()
    try:
        with pytest.raises(NoLiveHarnessError, match="no live harness subprocess"):
            await manager.get_client("conv_never_spawned", "any")
    finally:
        await manager.shutdown()


async def test_get_client_concurrent_first_calls_share_subprocess(
    manager: HarnessProcessManager,
) -> None:
    """Concurrent first get_client calls don't race two subprocesses.

    The per-conversation spawn lock should serialize the lazy-init
    window so only one subprocess gets created. Verified by
    issuing two get_client calls concurrently from different
    asyncio tasks and asserting the same client instance comes
    back from both.
    """
    await manager.start()
    try:
        # asyncio.gather schedules both calls before either has a
        # chance to populate the cache — exercises the spawn
        # lock's serialization.
        client_a, client_b = await asyncio.gather(
            manager.get_client("conv_a", _TEST_HARNESS_NAME),
            manager.get_client("conv_a", _TEST_HARNESS_NAME),
        )
        # Identity equality proves only one entry was created;
        # without the lock, the second call would race a second
        # subprocess and either race to bind the same socket
        # (one fails) or succeed with two distinct entries.
        assert client_a is client_b
    finally:
        await manager.shutdown()


async def test_get_client_seeds_model_and_reuses_without_respawn(
    manager: HarnessProcessManager,
) -> None:
    """A seeded model env bakes into the first spawn; an identical later
    call reuses it without a respawn.

    When the initial spawn is seeded with the persisted ``/model``
    override, the first turn requesting that same model must NOT tear
    the subprocess down and respawn it.
    """
    model_key = _model_env_key(_TEST_HARNESS_NAME)
    await manager.start()
    try:
        client_first = await manager.get_client(
            "conv_a", _TEST_HARNESS_NAME, env={model_key: "model-x"}
        )
        entry = manager._entries["conv_a"]
        # The first spawn baked the seeded model into the entry.
        assert entry.model == "model-x"
        pid_first = (await client_first.get("/pid")).json()["pid"]

        # Identical model env → cached entry, no respawn.
        client_second = await manager.get_client(
            "conv_a", _TEST_HARNESS_NAME, env={model_key: "model-x"}
        )
        assert client_second is client_first
        assert manager._entries["conv_a"] is entry
        pid_second = (await client_second.get("/pid")).json()["pid"]
        # Same PID proves no respawn happened on the second identical call.
        assert pid_second == pid_first
    finally:
        await manager.shutdown()


# ── Idle reaping ───────────────────────────────────────────────


async def test_idle_reaper_releases_stale_entries(
    register_test_harness: None,
    short_tmp_parent: Path,
) -> None:
    """An entry untouched past idle_timeout_s gets reaped.

    Sets a short idle timeout and a fast reaper interval so the
    test completes promptly without letting the reaper kill the
    subprocess before the setup health probe has completed. After
    reaping, the conversation is no longer registered and its
    socket file is gone.
    """
    fast = HarnessProcessManager(
        idle_timeout_s=2.0,
        reaper_interval_s=0.1,
        tmp_parent=short_tmp_parent,
    )
    await fast.start()
    try:
        await fast.get_client("conv_a", _TEST_HARNESS_NAME)
        # No HTTP ping: with idle_timeout_s=0.0 the reaper can fire during an
        # inline HTTP call and yank the client mid-request. Socket-existence
        # loop below is the real "entry was reaped" assertion.
        socket_path = fast.instance_dir / "conv-conv_a.sock"
        assert socket_path.exists()
        # Wait long enough for the 2s idle window plus multiple
        # reaper passes. A 0s timeout races with subprocess startup
        # under CI load and can close the client before the socket is
        # ready to service requests.
        for _ in range(60):
            if not socket_path.exists():
                break
            await asyncio.sleep(0.1)
        # If this assertion flips, the reaper isn't running OR
        # isn't acting on stale entries — both regressions in
        # the contract.
        assert not socket_path.exists()
    finally:
        await fast.shutdown()


async def test_idle_reaper_survives_release_error(
    register_test_harness: None,
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``release`` failure during reaping must not kill the reaper loop.

    ``_idle_reaper_loop`` awaits ``self.release(conv_id)`` for each stale
    entry with no guard around it. ``release`` -> ``_close_entry`` awaits
    ``client.aclose()`` and ``process.wait()``, any of which can raise
    (broken transport, dead process, ``ProcessLookupError``). An unguarded
    raise propagates out of the ``while True`` loop and the reaper task
    exits permanently — and silently, since nothing awaits the dead task —
    so the AP instance never reclaims another idle subprocess for the rest
    of its lifetime (FD / memory / socket leak).

    Inject a one-shot ``release`` failure on the first reaper-triggered
    call and assert the loop keeps going: the still-stale entry is reaped
    on a later pass. Before the fix the socket never disappears (the loop
    died); after it, a subsequent pass reclaims it.
    """
    fast = HarnessProcessManager(
        idle_timeout_s=2.0,
        reaper_interval_s=0.1,
        tmp_parent=short_tmp_parent,
    )
    await fast.start()
    try:
        await fast.get_client("conv_a", _TEST_HARNESS_NAME)
        socket_path = fast.instance_dir / "conv-conv_a.sock"
        assert socket_path.exists()

        # Make the first reaper-triggered release raise, then defer to the
        # real release on later calls — a transient teardown failure.
        real_release = fast.release
        calls = {"n": 0}

        async def flaky_release(conversation_id: str, **kw: object) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated close failure")
            await real_release(conversation_id, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(fast, "release", flaky_release)

        # Across many reaper passes: with the bug the first raise kills the
        # loop and the socket lingers; with the guard a later pass reaps it.
        for _ in range(60):
            if not socket_path.exists():
                break
            await asyncio.sleep(0.1)
        assert calls["n"] >= 1, "reaper never attempted to release the stale entry"
        assert not socket_path.exists(), (
            "reaper died on the first release error and never reclaimed the "
            "stale subprocess on a later pass"
        )
    finally:
        await fast.shutdown()


async def test_idle_reaper_skips_in_flight_turn(
    register_test_harness: None,
    short_tmp_parent: Path,
) -> None:
    """A conversation with a live harness turn is never reaped mid-flight.

    Regression test for #1414. ``last_used_at`` is stamped once per turn at
    ``get_client``, so a turn that runs longer than ``idle_timeout_s`` looks
    "idle" to the reaper. The only guard against killing it —
    ``conv_id in _in_flight_response_ids`` — had no writers and was always
    empty, so long turns were ``SIGTERM``'d mid-stream. ``mark_in_flight`` /
    ``clear_in_flight`` populate that guard (the runner calls them from
    ``proxy_stream`` on ``response.created`` and from ``_on_proxy_stream_end``).

    Marks a turn in-flight, holds it well past the 2 s idle window across many
    reaper passes, and asserts the subprocess survives; then clears the marker
    and asserts the now-genuinely-idle entry is reaped (so the fix doesn't
    leak entries that never get reclaimed — the inverse failure, cf. #1349).
    """
    fast = HarnessProcessManager(
        idle_timeout_s=2.0,
        reaper_interval_s=0.1,
        tmp_parent=short_tmp_parent,
    )
    await fast.start()
    try:
        await fast.get_client("conv_a", _TEST_HARNESS_NAME)
        socket_path = fast.instance_dir / "conv-conv_a.sock"
        assert socket_path.exists()
        # Mark the turn live, as the runner does on ``response.created``.
        fast.mark_in_flight("conv_a", "resp_x")
        assert fast.has_active_turn("conv_a")
        # Hold past the 2 s idle window across ~40 reaper passes (~4 s). An
        # unguarded reaper would have reaped this stale-looking entry; the
        # in-flight guard must keep the subprocess alive the whole time.
        for _ in range(40):
            await asyncio.sleep(0.1)
            assert socket_path.exists(), "in-flight turn was reaped mid-flight"
        # Turn ends: clear the marker (as ``_on_proxy_stream_end`` does). The
        # entry is now genuinely idle and must become reapable.
        fast.clear_in_flight("conv_a")
        assert not fast.has_active_turn("conv_a")
        for _ in range(60):
            if not socket_path.exists():
                break
            await asyncio.sleep(0.1)
        assert not socket_path.exists()
    finally:
        await fast.shutdown()


async def test_native_activity_refresh_prevents_idle_reaping(tmp_path: Path) -> None:
    """Native terminal activity keeps a harness alive without proxy response ids."""
    mgr = HarnessProcessManager(idle_timeout_s=0.5, reaper_interval_s=0.05, tmp_parent=tmp_path)
    entry = _SubprocessEntry(
        _FakeReapProc(),
        _SlowCloseClient(0.0),
        _FakeEndpoint(),
        "codex-native",
    )  # type: ignore[arg-type]
    entry.last_used_at = time.monotonic()
    mgr._entries = {"conv_native": entry}

    reaper = asyncio.create_task(mgr._idle_reaper_loop())
    try:
        for _ in range(10):
            await asyncio.sleep(0.1)
            mgr.note_activity("conv_native")
            assert not entry.process.killed, "active native harness was reaped"
        assert not mgr.has_active_turn("conv_native")

        deadline = time.monotonic() + 2.0
        while not entry.process.killed:
            assert time.monotonic() < deadline, "inactive native harness was never reaped"
            await asyncio.sleep(0.02)
    finally:
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper


class _FakeReapProc:
    """Minimal process stand-in recording whether the reaper killed it.

    Implements the full ``_proc._ProcessLike`` protocol (``pid``/``returncode``/
    ``terminate``/``kill``) because teardown is tree-aware. ``pid`` is ``None``
    on purpose: it routes ``_proc.terminate_tree`` / ``kill_tree`` down their
    no-pid branch, which signals this object directly and makes no OS calls at
    all. A made-up integer pid would be actively dangerous here — the helpers
    would resolve it against the real process table, and a pid that happened to
    exist would get a live process group signalled by the test suite.
    """

    def __init__(self) -> None:
        self.pid: int | None = None
        self.returncode: int | None = None
        self.killed = False
        self._done = asyncio.Event()

    def send_signal(self, sig: int) -> None:
        self.killed = True
        self.returncode = -15
        self._done.set()

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int | None:
        await self._done.wait()
        return self.returncode


class _SlowCloseClient:
    """httpx-client stand-in whose aclose() stalls, holding the reaper pass open."""

    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s

    async def aclose(self) -> None:
        await asyncio.sleep(self._delay_s)


class _FakeEndpoint:
    def cleanup(self) -> None:
        pass


async def test_idle_reaper_spares_turn_started_during_pass(tmp_path: Path) -> None:
    """A turn that starts while an earlier stale entry tears down is not reaped.

    The reaper snapshots its stale list under the registry lock, then releases
    each entry outside it; a single teardown can hold the pass open for seconds
    (graceful-SIGTERM wait). A turn that starts on a later-listed conversation
    during that window refreshes ``last_used_at`` and marks itself in flight —
    but the snapshot has already been taken, and ``release`` used to tear the
    entry down without re-checking, SIGTERMing the subprocess mid-turn
    ("Harness stream connection error" seconds after messaging an idle
    session). ``only_if_idle_cutoff`` re-checks idleness atomically with the
    unregister, so the now-active entry is spared; the genuinely idle entry in
    the same pass is still reaped, and the spared one is reclaimed by a later
    pass once it goes idle again.
    """
    mgr = HarnessProcessManager(idle_timeout_s=0.5, reaper_interval_s=0.2, tmp_parent=tmp_path)
    e1 = _SubprocessEntry(_FakeReapProc(), _SlowCloseClient(0.6), _FakeEndpoint(), "h")  # type: ignore[arg-type]
    e2 = _SubprocessEntry(_FakeReapProc(), _SlowCloseClient(0.0), _FakeEndpoint(), "h")  # type: ignore[arg-type]
    e1.last_used_at = time.monotonic() - 100.0
    e2.last_used_at = time.monotonic() - 100.0
    mgr._entries = {"conv1": e1, "conv2": e2}

    reaper = asyncio.create_task(mgr._idle_reaper_loop())
    try:
        # Wait for the pass to claim conv1 and enter its slow teardown.
        deadline = time.monotonic() + 3.0
        while "conv1" in mgr._entries:
            assert time.monotonic() < deadline, "reaper never started a pass"
            await asyncio.sleep(0.01)

        # While conv1 tears down, activity refreshes conv2's lease and the
        # runner marks the response in flight.
        mgr.note_activity("conv2")
        mgr.mark_in_flight("conv2", "resp_live")

        await asyncio.sleep(1.0)
        assert e1.process.killed, "the genuinely idle entry must still be reaped"
        assert not e2.process.killed, (
            "reaper SIGTERMed a subprocess whose turn started during the pass"
        )
        assert "conv2" in mgr._entries

        # Once the turn ends and the entry goes idle again, a later pass
        # reclaims it — sparing is a deferral, not an exemption.
        mgr.clear_in_flight("conv2")
        e2.last_used_at = time.monotonic() - 100.0
        deadline = time.monotonic() + 3.0
        while not e2.process.killed:
            assert time.monotonic() < deadline, "spared entry never reaped later"
            await asyncio.sleep(0.05)
    finally:
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper


async def test_idle_reaper_disabled_when_timeout_zero(
    register_test_harness: None,
    short_tmp_parent: Path,
) -> None:
    """A non-positive idle window disables reaping (``OMNIGENT_HARNESS_IDLE_TIMEOUT_S=0``).

    Regression: ``0`` must mean "never reap", not "reap everything". Without the
    ``idle_timeout_s <= 0`` guard the reaper computes ``cutoff = now - 0 == now``,
    and since every ``last_used_at`` is <= now it reaps every entry on the first
    pass. The spawned entry must survive many fast reaper passes.
    """
    fast = HarnessProcessManager(
        idle_timeout_s=0.0,
        reaper_interval_s=0.05,
        tmp_parent=short_tmp_parent,
    )
    await fast.start()
    try:
        await fast.get_client("conv_a", _TEST_HARNESS_NAME)
        socket_path = fast.instance_dir / "conv-conv_a.sock"
        assert socket_path.exists()
        # ~20 reaper passes at 0.05 s. With the bug the socket is gone almost
        # immediately; with the guard it survives because reaping is disabled.
        await asyncio.sleep(1.0)
        assert socket_path.exists(), (
            "idle_timeout_s=0 must DISABLE reaping, not reap every entry each pass"
        )
    finally:
        await fast.shutdown()


async def test_orphan_sweep_removes_dead_omnigent_dirs(
    short_tmp_parent: Path,
) -> None:
    """A sibling Omnigent dir with a non-running PID gets cleaned.

    Plants a fake AP-instance dir under tmp_parent with an
    AP_PID sentinel pointing at a non-running PID, then boots a
    fresh manager. start() runs the orphan sweep, which should
    remove the dead dir while leaving its own intact.

    Uses ``99999999`` as the non-running PID — a valid integer
    that's almost certainly not allocated. If this becomes
    flaky on a future host with that pid, increase the value.
    """
    fake_dir = short_tmp_parent / "ap-deaduuid"
    fake_dir.mkdir(mode=0o700)
    write_owner_claim(fake_dir, filename=_AP_PID_FILE)
    marker = fake_dir / _AP_PID_FILE
    marker.write_text(marker.read_text().replace(str(os.getpid()), "99999999", 1))
    # Plant a stale socket file too so the sweep has something to
    # try-and-clean (no live runner to kill, but the dir removal
    # path is what matters).
    (fake_dir / "conv-orphan.sock").write_text("", encoding="utf-8")

    fresh = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await fresh.start()
    try:
        # The dead sibling dir is gone; ours is fresh.
        assert not fake_dir.exists()
        assert fresh.instance_dir.exists()
    finally:
        await fresh.shutdown()


@pytest.mark.parametrize("owner_namespace", ["ours", "foreign"])
async def test_orphan_sweep_judges_a_dead_looking_owner_only_from_its_namespace(
    short_tmp_parent: Path,
    owner_namespace: str,
) -> None:
    """Only a sweeper in the owner's PID namespace may conclude the owner is gone."""
    claimed_dir = short_tmp_parent / "ap-claimed"
    claimed_dir.mkdir(mode=0o700)
    write_owner_claim(claimed_dir, filename=_AP_PID_FILE)
    sentinel = claimed_dir / _AP_PID_FILE
    lines = ["99999999", *sentinel.read_text(encoding="utf-8").splitlines()[1:]]
    if owner_namespace == "foreign":
        # How a live owner looks to a sweeper inside an agent sandbox.
        lines = ["pid_ns=pid:[1]" if line.startswith("pid_ns=") else line for line in lines]
    sentinel.write_text("\n".join(lines) + "\n", encoding="utf-8")

    await sweep_orphaned_harness_processes(tmp_parent=short_tmp_parent)

    assert claimed_dir.exists() is (owner_namespace == "foreign")


async def test_orphan_sweep_preserves_live_omnigent_dirs(
    short_tmp_parent: Path,
) -> None:
    """A sibling Omnigent dir with a live PID is left alone.

    Plants a fake AP-instance dir whose AP_PID sentinel points at
    *this test process* (which is live by definition). The
    sweep on a fresh manager's start() should leave the sibling
    intact — that's the zero-downtime-restart / multi-tenant
    isolation guarantee from §Process management.
    """
    sibling_dir = short_tmp_parent / "ap-livepid"
    sibling_dir.mkdir(mode=0o700)
    (sibling_dir / _AP_PID_FILE).write_text(str(os.getpid()), encoding="utf-8")

    fresh = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await fresh.start()
    try:
        # If sweep removed the sibling, a concurrent Omnigent would
        # have its dir deleted out from under it — exactly the
        # bug the live-PID check is meant to prevent.
        assert sibling_dir.exists()
        assert (sibling_dir / _AP_PID_FILE).exists()
    finally:
        await fresh.shutdown()


def _unshare_pid_namespace_available() -> bool:
    """Whether this host allows an unprivileged user + PID namespace."""
    if sys.platform != "linux" or shutil.which("unshare") is None:
        return False
    import subprocess

    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc", "true"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


async def test_orphan_sweep_from_another_pid_namespace_keeps_a_live_harness(
    manager: HarnessProcessManager,
) -> None:
    """A sweep that cannot see our PID must not delete a live session's socket.

    Agent sandboxes (e.g. Codex's Linux sandbox) run commands in their own PID
    namespace with ``/tmp`` writable. A server or test booted there sweeps the
    shared harness root, sees every live ``AP_PID`` as dead, and deletes each
    session's socket while its runner and harness keep running.
    """
    import subprocess

    if not _unshare_pid_namespace_available():
        pytest.skip("needs Linux with unprivileged user + PID namespaces")
    await manager.start()
    try:
        conversation_id = "conv-live-in-host-ns"
        client = await manager.get_client(conversation_id, _TEST_HARNESS_NAME)
        harness_pid = (await client.get("/pid")).json()["pid"]

        sweep = (
            "import asyncio, pathlib, sys\n"
            "from omnigent.runtime.harnesses.process_manager import "
            "sweep_orphaned_harness_processes\n"
            "asyncio.run(sweep_orphaned_harness_processes(tmp_parent=pathlib.Path(sys.argv[1])))\n"
        )
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "unshare",
                "--user",
                "--map-root-user",
                "--pid",
                "--fork",
                "--mount-proc",
                sys.executable,
                "-c",
                sweep,
                str(manager.instance_dir.parent),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

        socket_path = manager.socket_path(conversation_id)
        assert socket_path.exists()
        # Fresh connection: the cached client's keepalive could otherwise
        # reuse a socket opened before the sweep ran.
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(socket_path)),
            base_url="http://harness.local",
        ) as fresh:
            assert (await fresh.get("/pid")).json()["pid"] == harness_pid
    finally:
        await manager.shutdown()


@pytest.mark.posix_only
async def test_orphan_sweep_survives_unreadable_tmp_parent(
    short_tmp_parent: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warn and continue startup when the configured parent cannot be listed."""
    if os.geteuid() == 0:
        pytest.skip("permission checks do not apply to root")
    locked_parent = short_tmp_parent / "fp"
    locked_parent.mkdir(mode=0o700)
    (locked_parent / "ap-unreachable").mkdir(mode=0o700)
    # Write+search without read: our own instance dir can be created,
    # but the sweep cannot enumerate the configured shared parent.
    locked_parent.chmod(0o333)
    manager = HarnessProcessManager(tmp_parent=locked_parent)
    try:
        await manager.start()
        assert any(
            "cannot enumerate" in record.getMessage() and str(locked_parent) in record.getMessage()
            for record in caplog.records
        )
    finally:
        locked_parent.chmod(0o700)
        with contextlib.suppress(Exception):
            await manager.shutdown()


async def test_orphan_sweep_survives_tmp_parent_stat_error(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tmp-parent stat race must not abort manager startup."""
    real_exists = Path.exists
    raised = False

    def _racy_exists(self: Path) -> bool:
        nonlocal raised
        if self == short_tmp_parent and not raised:
            raised = True
            raise OSError("tmp parent changed during sweep")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _racy_exists)
    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    try:
        await manager.start()
        assert manager.instance_dir.exists()
        assert any(
            "cannot access" in record.getMessage() and str(short_tmp_parent) in record.getMessage()
            for record in caplog.records
        )
    finally:
        with contextlib.suppress(Exception):
            await manager.shutdown()


@pytest.mark.posix_only
async def test_orphan_sweep_skips_unreadable_sibling_and_still_sweeps(
    short_tmp_parent: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Skip a mode-0000 sibling, remove a readable dead orphan, and retain a live one."""
    if os.geteuid() == 0:
        pytest.skip("permission checks do not apply to root")
    inaccessible = short_tmp_parent / "ap-inaccessible"
    inaccessible.mkdir(mode=0o700)
    write_owner_claim(inaccessible, filename=_AP_PID_FILE)
    marker = inaccessible / _AP_PID_FILE
    marker.write_text(marker.read_text().replace(str(os.getpid()), "99999999", 1))
    inaccessible.chmod(0o000)

    dead = short_tmp_parent / "ap-deadsibling"
    dead.mkdir(mode=0o700)
    write_owner_claim(dead, filename=_AP_PID_FILE)
    marker = dead / _AP_PID_FILE
    marker.write_text(marker.read_text().replace(str(os.getpid()), "99999999", 1))

    live = short_tmp_parent / "ap-livesibling"
    live.mkdir(mode=0o700)
    (live / _AP_PID_FILE).write_text(str(os.getpid()), encoding="utf-8")

    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    try:
        await manager.start()
        assert inaccessible.exists(), "unreadable sibling must be skipped, not removed"
        assert not dead.exists(), "readable dead orphan must still be swept"
        assert live.exists(), "live sibling must remain untouched"
        assert any(
            "cannot inspect" in record.getMessage() and str(inaccessible) in record.getMessage()
            for record in caplog.records
        )
    finally:
        inaccessible.chmod(0o700)
        with contextlib.suppress(Exception):
            await manager.shutdown()


async def test_orphan_sweep_treats_vanishing_child_as_benign_race(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An injected is_dir race must not prevent cleanup of the next dead orphan."""
    vanishing = short_tmp_parent / "ap-avanishing"
    vanishing.mkdir(mode=0o700)
    write_owner_claim(vanishing, filename=_AP_PID_FILE)
    marker = vanishing / _AP_PID_FILE
    marker.write_text(marker.read_text().replace(str(os.getpid()), "99999999", 1))
    dead = short_tmp_parent / "ap-zzdead"
    dead.mkdir(mode=0o700)
    write_owner_claim(dead, filename=_AP_PID_FILE)
    marker = dead / _AP_PID_FILE
    marker.write_text(marker.read_text().replace(str(os.getpid()), "99999999", 1))

    real_is_dir = Path.is_dir

    def _racy_is_dir(self: Path, **kwargs: object) -> bool:
        if self.name == "ap-avanishing":
            raise FileNotFoundError(2, "vanished mid-sweep", str(self))
        return real_is_dir(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "is_dir", _racy_is_dir)
    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    try:
        await manager.start()
        assert not dead.exists(), "sweep must continue past the racy child"
    finally:
        monkeypatch.undo()
        with contextlib.suppress(Exception):
            await manager.shutdown()


# ── Helper-level tests (small, fast) ───────────────────────────


def test_pid_alive_for_self() -> None:
    """The running process is, by definition, alive.

    Sanity check that the helper is wired up correctly — if this
    flips, the orphan-sweep "is sibling alive?" check is broken.
    """
    assert _pid_alive(os.getpid())


def test_pid_alive_for_unallocated_pid() -> None:
    """An almost-certainly-unallocated PID is not alive.

    Uses ``99999999`` as a sentinel that's a valid integer pid
    but vanishingly unlikely to exist. If this becomes flaky
    raise the number.
    """
    assert not _pid_alive(99999999)


async def test_pids_holding_socket_returns_empty_for_missing(
    short_tmp_parent: Path,
) -> None:
    """``lsof`` against a nonexistent socket yields no PIDs.

    Locks in the best-effort contract: the orphan sweep must
    not crash if a socket file disappears between glob and
    lookup. Empty list is the expected result.
    """
    nonexistent = short_tmp_parent / "no-such.sock"
    pids = await _pids_holding_socket(nonexistent)
    assert pids == []


async def test_pids_holding_socket_returns_empty_when_lsof_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    short_tmp_parent: Path,
) -> None:
    """Missing ``lsof`` is best-effort cleanup noise, not a boot failure."""

    async def missing_lsof(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError(2, "No such file or directory", "lsof")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_lsof)

    pids = await _pids_holding_socket(short_tmp_parent / "conv-stale.sock")
    assert pids == []


# ── Per-spawn env override ─────────────────────────────────────


async def test_get_client_env_override_propagates_to_subprocess(
    manager: HarnessProcessManager,
) -> None:
    """``get_client(env=...)`` threads env vars into the spawned subprocess.

    Verifies the v1 spec-config flow: Omnigent passes per-spec env vars
    via ``env`` to ``get_client``, and the spawned subprocess
    sees them in its own ``os.environ``. Without this propagation,
    Omnigent would have to mutate its own ``os.environ`` (which races
    across concurrent conversations with different specs).
    """
    await manager.start()
    try:
        client = await manager.get_client(
            "conv_env",
            _TEST_HARNESS_NAME,
            env={"HARNESS_TEST_CUSTOM": "marker_alpha"},
        )
        # Subprocess saw the override in its env.
        resp = await client.get("/env/HARNESS_TEST_CUSTOM")
        assert resp.json() == {"value": "marker_alpha"}
    finally:
        await manager.shutdown()


@pytest.mark.parametrize("desktop_granted", [False, True])
async def test_spawned_harness_requires_desktop_session_grant(
    manager: HarnessProcessManager, monkeypatch: pytest.MonkeyPatch, desktop_granted: bool
) -> None:
    session_env = {
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    for name, value in session_env.items():
        monkeypatch.setenv(name, value)
    auth_command = "printf %s test-provider-key"
    await manager.start()
    try:
        client = await manager.get_client(
            "conv_keyring",
            _TEST_HARNESS_NAME,
            env={
                **(session_env if desktop_granted else {}),
                "HARNESS_CODEX_GATEWAY_AUTH_COMMAND": auth_command,
            },
        )
        for name, value in session_env.items():
            response = await client.get(f"/env/{name}")
            assert response.json() == {"value": value if desktop_granted else None}
            assert os.environ[name] == value
        response = await client.get("/env/HARNESS_CODEX_GATEWAY_AUTH_COMMAND")
        assert response.json() == {"value": auth_command}
    finally:
        await manager.shutdown()


async def test_get_client_env_override_is_per_conversation(
    manager: HarnessProcessManager,
) -> None:
    """Different conversations get their own ``env`` overrides.

    Spawns two subprocesses with different env values for the
    same env-var name; each subprocess should see ITS OWN value.
    Without per-spawn isolation, AP's only option would be to
    mutate ``os.environ``, which would race when concurrent
    conversations want different config — the failure mode this
    test guards against.
    """
    await manager.start()
    try:
        client_a = await manager.get_client(
            "conv_a",
            _TEST_HARNESS_NAME,
            env={"HARNESS_TEST_CUSTOM": "alpha"},
        )
        client_b = await manager.get_client(
            "conv_b",
            _TEST_HARNESS_NAME,
            env={"HARNESS_TEST_CUSTOM": "beta"},
        )
        resp_a = await client_a.get("/env/HARNESS_TEST_CUSTOM")
        resp_b = await client_b.get("/env/HARNESS_TEST_CUSTOM")
        assert resp_a.json()["value"] == "alpha"
        assert resp_b.json()["value"] == "beta"
    finally:
        await manager.shutdown()


# ── Runner subprocess cleanup ──────────────────────


async def test_runner_subprocess_exits_on_sigterm(
    manager: HarnessProcessManager,
) -> None:
    """A harness runner exits promptly after a plain SIGTERM.

    This catches the failure mode where ``pkill`` left
    ``omnigent.runtime.harnesses._runner`` processes alive because
    shutdown never reached uvicorn's normal exit path.
    """
    await manager.start()
    try:
        client = await manager.get_client("conv_sigterm", _TEST_HARNESS_NAME)
        runner_pid = (await client.get("/pid")).json()["pid"]
        os.kill(runner_pid, signal.SIGTERM)

        for _ in range(60):
            if not _pid_alive(runner_pid):
                break
            await asyncio.sleep(0.1)
        assert not _pid_alive(runner_pid)
    finally:
        await manager.shutdown()


@pytest.mark.flaky(reruns=2, reruns_delay=0)
async def test_runner_subprocess_exits_when_spawning_parent_exits(
    short_tmp_parent: Path,
    register_test_harness: None,
) -> None:
    """A harness runner exits when its spawning parent process exits.

    The helper process below starts a real ``HarnessProcessManager``
    and real ``_runner`` child, then exits without shutting the
    manager down. The runner's ``--parent-pid`` watchdog should
    observe OS reparenting and terminate itself, preventing the
    orphan accumulation.
    """
    import subprocess
    import textwrap

    parent_script = textwrap.dedent(
        f"""
        import asyncio
        import os
        import pathlib
        from omnigent.runtime.harnesses import _HARNESS_MODULES
        from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

        async def main():
            _HARNESS_MODULES[{_TEST_HARNESS_NAME!r}] = {_TEST_HARNESS_MODULE!r}
            mgr = HarnessProcessManager(tmp_parent=pathlib.Path({str(short_tmp_parent)!r}))
            await mgr.start()
            client = await mgr.get_client('conv_parent_death', {_TEST_HARNESS_NAME!r})
            pid = (await client.get('/pid')).json()['pid']
            print(pid, flush=True)
            os._exit(0)

        asyncio.run(main())
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", parent_script],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    runner_pid = int(proc.stdout.strip().splitlines()[-1])

    try:
        for _ in range(60):
            if not _pid_alive(runner_pid):
                break
            await asyncio.sleep(0.1)
        assert not _pid_alive(runner_pid)
    finally:
        if _pid_alive(runner_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(runner_pid, signal.SIGKILL)


async def test_runner_subprocess_hard_exits_when_sigterm_shutdown_wedges(
    manager: HarnessProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGTERM'ed runner hard-exits even if uvicorn shutdown wedges.

    The fixture starts a background task that ignores cancellation,
    which prevents uvicorn's graceful shutdown from reaching lifespan
    teardown. This exercises the fallback for plain
    ``pkill -f omnigent.runtime.harnesses._runner``: the runner
    should not remain alive forever just because graceful shutdown is
    stuck.
    """
    monkeypatch.setenv("OMNIGENT_HARNESS_SHUTDOWN_TIMEOUT_S", "0.2")
    monkeypatch.setenv("OMNIGENT_HARNESS_HARD_EXIT_TIMEOUT_S", "0.5")
    await manager.start()
    try:
        client = await manager.get_client("conv_stuck_sigterm", _TEST_HARNESS_NAME)
        runner_pid = (await client.get("/pid")).json()["pid"]
        resp = await client.get("/stuck-shutdown")
        assert resp.json()["status"] == "stuck_task_started"
        os.kill(runner_pid, signal.SIGTERM)

        for _ in range(40):
            if not _pid_alive(runner_pid):
                break
            await asyncio.sleep(0.1)
        assert not _pid_alive(runner_pid)
    finally:
        await manager.shutdown()


async def test_orphan_sweep_escalates_to_sigkill(
    short_tmp_parent: Path,
    register_test_harness: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orphan sweep SIGKILLs runners that survive SIGTERM."""
    from omnigent.runtime.harnesses import process_manager as pm_mod

    killed: list[tuple[int, signal.Signals]] = []
    calls = 0

    async def fake_pids_holding_socket(socket_path: Path) -> list[int]:
        assert socket_path.name == "conv-stale.sock"
        return [12345]

    def fake_pid_alive(pid: int) -> bool:
        assert pid == 12345
        return True

    def fake_kill(pid: int, sig: signal.Signals) -> None:
        nonlocal calls
        calls += 1
        assert pid == 12345
        killed.append((pid, sig))

    monkeypatch.setattr(pm_mod, "_ORPHAN_SIGTERM_GRACE_S", 0)
    monkeypatch.setattr(pm_mod, "_pids_holding_socket", fake_pids_holding_socket)
    monkeypatch.setattr(pm_mod, "_pid_alive", fake_pid_alive)
    monkeypatch.setattr(pm_mod.os, "kill", fake_kill)

    instance_dir = short_tmp_parent / "ap-dead"
    instance_dir.mkdir()
    (instance_dir / "conv-stale.sock").touch()

    await _kill_orphan_runners(instance_dir)

    assert calls == 2
    assert killed == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]


# ── Mid-spawn cancellation ──────────────────────────────────────


async def test_mid_spawn_cancellation_reaps_subprocess(
    manager: HarnessProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Cancelling ``get_client`` while the spawn is still waiting for
    the runner to bind must kill the just-spawned subprocess.

    The subprocess exists from ``create_subprocess_exec`` onward but
    is only registered in ``_entries`` after ``_spawn_entry``
    returns; a cancellation landing inside ``_wait_for_bind`` used
    to leak it — unregistered, so ``release()`` no-ops and the idle
    reaper never sees it.
    """
    from omnigent.runtime.harnesses import process_manager as pm_mod

    await manager.start()
    try:
        spawned: list[asyncio.subprocess.Process] = []
        real_exec = asyncio.create_subprocess_exec

        async def capturing_exec(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            process = await real_exec(*args, **kwargs)  # type: ignore[arg-type]
            spawned.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing_exec)

        in_bind = asyncio.Event()

        async def hanging_bind(*args: object, **kwargs: object) -> None:
            in_bind.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(pm_mod, "_wait_for_bind", hanging_bind)

        task = asyncio.create_task(manager.get_client("conv_leak", "test"))
        await asyncio.wait_for(in_bind.wait(), timeout=10.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert spawned, "spawn was never reached"
        process = spawned[0]
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        finally:
            # Never leak the subprocess out of the test, even when
            # the assertion below is about to fail on main.
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode is not None
        assert not manager.has_session("conv_leak")
    finally:
        await manager.shutdown()


async def test_mid_spawn_double_cancellation_still_reaps(
    manager: HarnessProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A second cancellation arriving while the first one's cleanup is
    reaping the subprocess must not abort the reap: the corpse-wait
    is shielded, so the process is still collected and the task
    still ends cancelled.
    """
    from omnigent.runtime.harnesses import process_manager as pm_mod

    await manager.start()
    try:
        spawned: list[asyncio.subprocess.Process] = []
        real_exec = asyncio.create_subprocess_exec

        async def capturing_exec(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            process = await real_exec(*args, **kwargs)  # type: ignore[arg-type]
            spawned.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing_exec)

        in_bind = asyncio.Event()

        async def hanging_bind(*args: object, **kwargs: object) -> None:
            in_bind.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(pm_mod, "_wait_for_bind", hanging_bind)

        task = asyncio.create_task(manager.get_client("conv_leak2", "test"))
        await asyncio.wait_for(in_bind.wait(), timeout=10.0)
        task.cancel()
        # Let the first cancellation reach the cleanup path, then
        # cancel again so the second one lands on its awaits.
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert spawned, "spawn was never reached"
        process = spawned[0]
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode is not None
        assert not manager.has_session("conv_leak2")
    finally:
        await manager.shutdown()


# ── Release / shutdown vs cold-spawn races ───────────────────────


async def _cancel_pending(*tasks: asyncio.Task[object] | None) -> None:
    """Cancel any still-pending tasks so a failed assertion cannot hang teardown."""
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                # Bind the result so static analysis does not treat the await
                # as a dead statement (teardown only cares that the task settles).
                _settled = await task
                del _settled


async def test_release_during_spawn_leaves_no_live_process(
    manager: HarnessProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``release`` during cold spawn must not lose to a late registration.

    ``get_client`` holds the per-conversation spawn lock across
    ``_wait_for_bind``, but ``release`` used to only inspect ``_entries``
    under the registry lock. A release that arrived mid-bind saw no entry
    and returned; the spawn then registered a live process that nobody
    owned. Barriers pin the race: release is queued while bind is gated,
    then bind completes so both sides settle under the shared spawn lock.
    """
    from omnigent.runtime.harnesses import process_manager as pm_mod

    await manager.start()
    get_task: asyncio.Task[object] | None = None
    release_task: asyncio.Task[bool] | None = None
    allow_bind = asyncio.Event()
    try:
        spawned: list[asyncio.subprocess.Process] = []
        real_exec = asyncio.create_subprocess_exec

        async def capturing_exec(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            process = await real_exec(*args, **kwargs)  # type: ignore[arg-type]
            spawned.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing_exec)

        in_bind = asyncio.Event()
        real_wait_for_bind = pm_mod._wait_for_bind

        async def gated_bind(*args: object, **kwargs: object) -> None:
            in_bind.set()
            await allow_bind.wait()
            await real_wait_for_bind(*args, **kwargs)

        monkeypatch.setattr(pm_mod, "_wait_for_bind", gated_bind)

        conv_id = "conv_release_during_spawn"
        get_task = asyncio.create_task(manager.get_client(conv_id, _TEST_HARNESS_NAME))
        await asyncio.wait_for(in_bind.wait(), timeout=10.0)
        assert spawned, "spawn was never reached"
        process = spawned[0]
        pid = process.pid

        # Queued behind the spawn lock until bind is allowed to finish.
        release_task = asyncio.create_task(manager.release(conv_id))
        await asyncio.sleep(0)
        assert not release_task.done(), "release returned before spawn released the lock"

        allow_bind.set()
        client = await get_task
        assert client is not None
        assert await release_task is True

        socket_path = manager.instance_dir / f"conv-{conv_id}.sock"
        assert not manager.has_session(conv_id)
        assert conv_id not in manager._entries
        assert not socket_path.exists()
        for _ in range(40):
            if not _pid_alive(pid):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(pid), "release-during-spawn left a live harness process"
        assert process.returncode is not None
    finally:
        allow_bind.set()
        await _cancel_pending(get_task, release_task)
        await manager.shutdown()


async def test_release_invalidates_queued_get_client(
    manager: HarnessProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``get_client`` queued behind ``release`` must not respawn after teardown.

    Getter A holds the spawn lock mid-bind; ``release`` queues; getter B
    queues behind release. Without a release generation, A registers,
    release closes it, then B acquires the lock and spawns again —
    leaving ``has_session`` True after release. B must fail; a fresh
    ``get_client`` after release may still respawn.
    """
    from omnigent.runtime.harnesses import process_manager as pm_mod

    await manager.start()
    get_a: asyncio.Task[object] | None = None
    get_b: asyncio.Task[object] | None = None
    release_task: asyncio.Task[bool] | None = None
    allow_bind = asyncio.Event()
    try:
        spawned: list[asyncio.subprocess.Process] = []
        real_exec = asyncio.create_subprocess_exec

        async def capturing_exec(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            process = await real_exec(*args, **kwargs)  # type: ignore[arg-type]
            spawned.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing_exec)

        in_bind = asyncio.Event()
        real_wait_for_bind = pm_mod._wait_for_bind

        async def gated_bind(*args: object, **kwargs: object) -> None:
            in_bind.set()
            await allow_bind.wait()
            await real_wait_for_bind(*args, **kwargs)

        monkeypatch.setattr(pm_mod, "_wait_for_bind", gated_bind)

        conv_id = "conv_release_queued_waiter"
        get_a = asyncio.create_task(manager.get_client(conv_id, _TEST_HARNESS_NAME))
        await asyncio.wait_for(in_bind.wait(), timeout=10.0)
        assert spawned, "first spawn was never reached"

        release_task = asyncio.create_task(manager.release(conv_id))
        await asyncio.sleep(0)
        get_b = asyncio.create_task(manager.get_client(conv_id, _TEST_HARNESS_NAME))
        await asyncio.sleep(0)
        assert not release_task.done()
        assert not get_b.done()

        allow_bind.set()
        client_a = await get_a
        assert client_a is not None
        assert await release_task is True

        done, pending = await asyncio.wait({get_b})
        assert not pending
        assert done == {get_b}
        exc = get_b.exception()
        assert isinstance(exc, RuntimeError)
        assert "was released while get_client waited" in str(exc)

        assert len(spawned) == 1, "queued get_client respawned after release"
        assert not manager.has_session(conv_id)
        assert conv_id not in manager._entries
        for process in spawned:
            for _ in range(40):
                if process.returncode is not None and not _pid_alive(process.pid):
                    break
                await asyncio.sleep(0.05)
            assert process.returncode is not None
            assert not _pid_alive(process.pid)

        # A call that starts after release must still be allowed to respawn.
        client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
        assert manager.has_session(conv_id)
        await _ping_health(client)
        assert len(spawned) == 2
    finally:
        allow_bind.set()
        await _cancel_pending(get_a, get_b, release_task)
        await manager.shutdown()


@pytest.mark.parametrize("release_kind", ["explicit", "idle", "retention"])
async def test_new_consumer_during_release_can_reacquire(
    manager: HarnessProcessManager,
    monkeypatch: pytest.MonkeyPatch,
    release_kind: str,
) -> None:
    """An acquisition arriving after close starts belongs to the next generation."""
    from omnigent.runtime.harnesses import process_manager as pm_mod

    await manager.start()
    release_task = consumer_task = None
    close_entered, allow_close, consumer_waiting = (asyncio.Event() for _ in range(3))
    conversation_id = "conv_acquire_during_release"
    original_close = manager._close_entry
    original_lock = manager._get_spawn_lock

    async def gated_close(entry: _SubprocessEntry) -> bool:
        close_entered.set()
        await allow_close.wait()
        return await original_close(entry)

    async def observed_lock(conv_id: str) -> asyncio.Lock:
        consumer_waiting.set()
        return await original_lock(conv_id)

    try:
        await manager.get_client(conversation_id, _TEST_HARNESS_NAME)
        monkeypatch.setattr(manager, "_close_entry", gated_close)
        if release_kind == "retention":
            monkeypatch.setattr(
                pm_mod, "cli_family_for_resident_harness", lambda _harness: "codex"
            )
            token = manager._retention_activity_token(
                conversation_id, manager._entries[conversation_id]
            )
            release = manager.release_if_retention_idle(
                conversation_id, idle_threshold_s=0, expected_activity_token=token
            )
        else:
            release = manager.release(
                conversation_id,
                only_if_idle_cutoff=time.monotonic() if release_kind == "idle" else None,
            )
        release_task = asyncio.create_task(release)
        await asyncio.wait_for(close_entered.wait(), timeout=10)
        monkeypatch.setattr(manager, "_get_spawn_lock", observed_lock)
        consumer_task = asyncio.create_task(
            manager.get_client(conversation_id, _TEST_HARNESS_NAME)
        )
        await asyncio.wait_for(consumer_waiting.wait(), timeout=10)
        allow_close.set()
        assert await asyncio.wait_for(release_task, timeout=20) in (True, "released")
        client = await asyncio.wait_for(consumer_task, timeout=20)
        assert manager.has_session(conversation_id)
        await _ping_health(client)
    finally:
        allow_close.set()
        await _cancel_pending(consumer_task, release_task)
        await manager.shutdown()


async def test_release_keeps_unconfirmed_process_registered_for_retry(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that survives terminate and kill must remain addressable."""
    from omnigent.runtime.harnesses import process_manager as pm_mod

    class _StuckProcess:
        returncode = None

        async def wait(self) -> None:
            await asyncio.Event().wait()

    class _Client:
        async def aclose(self) -> None:
            return None

    class _Endpoint:
        def __init__(self) -> None:
            self.cleanup_calls = 0

        def cleanup(self) -> None:
            self.cleanup_calls += 1

    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    process = _StuckProcess()
    endpoint = _Endpoint()
    entry = _SubprocessEntry(
        process=process,  # type: ignore[arg-type]
        client=_Client(),  # type: ignore[arg-type]
        endpoint=endpoint,  # type: ignore[arg-type]
        harness="codex",
    )
    conversation_id = "conv_stuck_release"
    manager._entries[conversation_id] = entry
    manager.manage_for_retention(conversation_id)
    saved_expiry = manager._retention_managed[conversation_id]
    monkeypatch.setattr(pm_mod, "_RELEASE_GRACE_S", 0.001)
    monkeypatch.setattr(pm_mod._proc, "terminate_tree", lambda _process: None)
    monkeypatch.setattr(pm_mod._proc, "kill_tree", lambda _process: None)

    assert await manager.release(conversation_id) is False
    assert manager._entries[conversation_id] is entry
    assert conversation_id in manager._retention_managed
    assert manager._retention_managed[conversation_id] == saved_expiry
    assert endpoint.cleanup_calls == 0


async def test_agent_switch_does_not_overwrite_process_that_survived_teardown(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Process:
        returncode = None

    class _Endpoint:
        socket_path = None

    entry = _SubprocessEntry(
        process=_Process(),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        endpoint=_Endpoint(),  # type: ignore[arg-type]
        harness="claude-sdk",
    )
    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    manager._started = True
    manager._entries["conv_switch"] = entry
    spawned = False

    async def _failed_close(_entry: _SubprocessEntry) -> bool:
        assert _entry is entry
        return False

    async def _unexpected_spawn(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("surviving process must block successor spawn")

    monkeypatch.setattr(manager, "_close_entry", _failed_close)
    monkeypatch.setattr(manager, "_spawn_entry", _unexpected_spawn)

    with pytest.raises(RuntimeError, match="survived agent-switch teardown"):
        await manager.get_client("conv_switch", "openai-agents")
    assert manager._entries["conv_switch"] is entry
    assert spawned is False


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param("socket-missing", marks=pytest.mark.posix_only),
        "ACP-config",
    ],
)
async def test_respawn_preserves_surviving_entry_after_failed_close(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    class Process:
        returncode = None
        pid = 12345

    class Endpoint:
        socket_path = None

    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    manager._started = True
    endpoint = Endpoint()
    if reason == "socket-missing":
        endpoint.socket_path = manager._instance_dir / "missing.sock"
    harness = "acp" if reason == "ACP-config" else "claude-sdk"
    entry = _SubprocessEntry(
        process=Process(),
        client=object(),
        endpoint=endpoint,
        harness=harness,
        acp_config={"HARNESS_ACP_COMMAND": "old-command"},
    )
    manager._entries["conv_retry"] = entry
    manager.mark_in_flight("conv_retry", "resp_original")

    async def failed_close(_entry):
        assert _entry is entry
        return False

    async def unexpected_spawn(*_args, **_kwargs):
        pytest.fail("a surviving harness must block successor spawn")

    monkeypatch.setattr(manager, "_close_entry", failed_close)
    monkeypatch.setattr(manager, "_spawn_entry", unexpected_spawn)
    with pytest.raises(RuntimeError, match=f"survived {reason} teardown"):
        await manager.get_client(
            "conv_retry",
            harness,
            env={"HARNESS_ACP_COMMAND": "new-command"},
        )
    assert manager._entries["conv_retry"] is entry
    assert manager.has_active_turn("conv_retry")


async def test_failed_retention_close_keeps_activity_token_retryable(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Process:
        returncode = None

    entry = _SubprocessEntry(
        process=_Process(),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        endpoint=object(),  # type: ignore[arg-type]
        harness="codex",
    )
    entry.last_used_at = time.monotonic() - 120
    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    manager._entries["conv_retry"] = entry
    manager.manage_for_retention("conv_retry")
    saved_expiry = manager._retention_managed["conv_retry"]
    activity_token = manager._retention_activity_token("conv_retry", entry)

    async def _failed_close(_entry: _SubprocessEntry) -> bool:
        assert _entry is entry
        return False

    monkeypatch.setattr(manager, "_close_entry", _failed_close)

    assert (
        await manager.release_if_retention_idle(
            "conv_retry",
            idle_threshold_s=60,
            expected_activity_token=activity_token,
        )
        == "failed"
    )
    assert manager._entries["conv_retry"] is entry
    assert manager._retention_activity_token("conv_retry", entry) == activity_token
    assert manager._retention_managed["conv_retry"] == saved_expiry


@pytest.mark.parametrize("release_kind", ["explicit", "retention"])
@pytest.mark.parametrize("raises", [False, True])
async def test_failed_harness_release_preserves_concurrent_renewal(
    short_tmp_parent: Path, monkeypatch: pytest.MonkeyPatch, release_kind: str, raises: bool
) -> None:
    class _Process:
        returncode = None

    entry = _SubprocessEntry(
        process=_Process(),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        endpoint=object(),  # type: ignore[arg-type]
        harness="codex",
    )
    entry.last_used_at = time.monotonic() - 120
    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    manager._entries["conv_renew"] = entry
    manager.manage_for_retention("conv_renew")
    saved_expiry = manager._retention_managed["conv_renew"]
    activity_token = manager._retention_activity_token("conv_renew", entry)
    before = await manager.resident_inventory()
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def fail(_entry: _SubprocessEntry) -> bool:
        entered.set()
        await resume.wait()
        if raises:
            raise RuntimeError("close failed")
        return False

    monkeypatch.setattr(manager, "_close_entry", fail)
    if release_kind == "explicit":
        releasing = asyncio.create_task(manager.release("conv_renew"))
    else:
        releasing = asyncio.create_task(
            manager.release_if_retention_idle(
                "conv_renew", idle_threshold_s=60, expected_activity_token=activity_token
            )
        )
    await asyncio.wait_for(entered.wait(), timeout=1)
    closing = await manager.resident_inventory()
    assert len(closing.owners) == 1
    assert closing.owners[0].incarnation_id == before.owners[0].incarnation_id
    assert closing.owners[0].closing and not closing.owners[0].exit_confirmed
    manager.manage_for_retention("conv_renew")
    renewed_expiry = manager._retention_managed["conv_renew"]
    resume.set()
    if raises:
        with pytest.raises(RuntimeError, match="close failed"):
            await releasing
    elif release_kind == "explicit":
        assert await releasing is False
    else:
        assert await releasing == "failed"
    assert renewed_expiry > saved_expiry
    assert manager._entries["conv_renew"] is entry
    assert manager._retention_managed["conv_renew"] == renewed_expiry
    assert manager._retention_activity_token("conv_renew", entry) == activity_token
    assert await manager.resident_inventory() == before
    assert not manager._closing_entries


async def test_resident_inventory_is_immutable_and_does_not_probe_or_manage(
    short_tmp_parent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runtime.harnesses import process_manager as pm_mod

    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    entry = _SubprocessEntry(
        process=SimpleNamespace(returncode=None),
        client=object(),  # type: ignore[arg-type]
        endpoint=object(),  # type: ignore[arg-type]
        harness="unsupported-harness",
    )
    manager._entries["conv_owned"] = entry
    manager._retention_managed["conv_owned"] = 0.0
    manager.mark_in_flight("conv_owned", "response-owned")

    def unexpected(*_args, **_kwargs):
        pytest.fail("inventory must only read existing owner state")

    for name in ("start", "get_client", "_spawn_entry", "_retention_expiry", "note_activity"):
        monkeypatch.setattr(manager, name, unexpected)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected)
    monkeypatch.setattr(pm_mod._proc, "process_alive", unexpected)
    monkeypatch.setattr(pm_mod, "cli_family_for_resident_harness", unexpected)

    snapshot = await manager.resident_inventory()
    assert len(snapshot.owners) == 1
    assert snapshot.owners[0].session_id == "conv_owned"
    assert snapshot.owners[0].harness == "unsupported-harness"
    assert not snapshot.owners[0].closing and not snapshot.owners[0].exit_confirmed
    assert manager._retention_managed == {"conv_owned": 0.0}
    assert manager._in_flight_response_ids == {"conv_owned": "response-owned"}
    assert entry.last_used_at == 0.0
    assert not manager._started
    with pytest.raises(FrozenInstanceError):
        snapshot.owners[0].closing = True  # type: ignore[misc]

    entry.process.returncode = 1
    exited_wrapper = (await manager.resident_inventory()).owners[0]
    assert exited_wrapper.wrapper_exited and not exited_wrapper.exit_confirmed
    assert not snapshot.owners[0].exit_confirmed
    entry.exit_confirmed = True
    entry.process.returncode = None
    assert (await manager.resident_inventory()).owners[0].exit_confirmed

    replacement = _SubprocessEntry(
        process=SimpleNamespace(returncode=None),
        client=object(),  # type: ignore[arg-type]
        endpoint=object(),  # type: ignore[arg-type]
        harness="codex",
    )
    manager._entries["conv_owned"] = replacement
    assert (await manager.resident_inventory()).owners[0].incarnation_id != snapshot.owners[
        0
    ].incarnation_id


@pytest.mark.parametrize("release_kind", ["explicit", "retention"])
async def test_inventory_records_unknown_exit_without_retaining_the_wrapper(
    short_tmp_parent: Path, monkeypatch: pytest.MonkeyPatch, release_kind: str
) -> None:
    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    entry = _SubprocessEntry(
        process=SimpleNamespace(returncode=None),
        client=_NoopHarnessClient(),  # type: ignore[arg-type]
        endpoint=_CleanupCountingEndpoint(),  # type: ignore[arg-type]
        harness="codex",
    )
    manager._entries["conv_wrapper"] = entry
    close = manager._close_entry

    async def failed_close(_entry: _SubprocessEntry) -> bool:
        _entry.process.returncode = 0
        raise RuntimeError("owned exit unconfirmed")

    monkeypatch.setattr(manager, "_close_entry", failed_close)
    with pytest.raises(RuntimeError, match="owned exit unconfirmed"):
        if release_kind == "explicit":
            await manager.release("conv_wrapper")
        else:
            await manager.release_if_retention_idle(
                "conv_wrapper",
                idle_threshold_s=0,
                expected_activity_token=manager._retention_activity_token("conv_wrapper", entry),
            )
    snapshot = await manager.resident_inventory()
    (retired,) = snapshot.owners
    assert retired.incarnation_id == entry.incarnation_id
    assert retired.session_id == "conv_wrapper"
    assert retired.closing and retired.wrapper_exited and not retired.exit_confirmed
    assert not snapshot.exit_coverage_complete
    assert not manager._closing_entries
    assert await close(entry)
    # A wrapper-only late close cannot establish the vendor/descendant lifetime.
    assert (await manager.resident_inventory()).owners == (retired,)
    replacement = _SubprocessEntry(
        process=SimpleNamespace(returncode=None),
        client=_NoopHarnessClient(),  # type: ignore[arg-type]
        endpoint=_CleanupCountingEndpoint(),  # type: ignore[arg-type]
        harness="codex",
    )
    manager._entries["conv_wrapper"] = replacement
    current = await manager.resident_inventory(context_reads=())
    assert {owner.incarnation_id for owner in current.owners} == {
        retired.incarnation_id,
        replacement.incarnation_id,
    }
    assert current.contexts == (None, None)
    assert not (await manager.resident_inventory()).exit_coverage_complete


async def test_expired_harness_lease_returns_to_sdk_idle_reaper(
    short_tmp_parent: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _Process:
        returncode = None

    manager = HarnessProcessManager(
        tmp_parent=short_tmp_parent, idle_timeout_s=1, reaper_interval_s=0.01
    )
    entry = _SubprocessEntry(
        process=_Process(),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        endpoint=object(),  # type: ignore[arg-type]
        harness="codex",
    )
    entry.last_used_at = time.monotonic() - 7200
    manager._entries["conv_expired"] = entry
    started = time.monotonic()
    manager.manage_for_retention("conv_expired")
    released: list[_SubprocessEntry] = []

    async def close(value: _SubprocessEntry) -> bool:
        released.append(value)
        return True

    monkeypatch.setattr(manager, "_close_entry", close)
    with caplog.at_level("INFO"):
        with monkeypatch.context() as clock:
            clock.setattr(time, "monotonic", lambda: started + 1260)
            assert not manager.has_retention_managed_sessions()
        task = asyncio.create_task(manager._idle_reaper_loop())
        try:
            for _ in range(100):
                if released:
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await task
    assert released == [entry]
    assert sum("retention lease expired" in row.message for row in caplog.records) == 1


@pytest.mark.parametrize("release_kind", ["explicit", "retention"])
@pytest.mark.parametrize("cancel_during_close", [False, True])
async def test_release_reaps_zygote_harness_with_cancelled_exit_poller(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_kind: str,
    cancel_during_close: bool,
) -> None:
    """A cancelled zygote exit-poller must not strand its entry forever.

    ``ZygoteHarnessProc.wait()`` suppresses the poller's
    ``CancelledError`` and returns its fallback code WITHOUT recording
    ``returncode``. Once the graceful wait's cancellation lands in the
    poller, every later ``wait()`` returns the fallback the same way —
    so the return-code teardown gate reads a dead process as live,
    ``release`` restores the entry, and socket/endpoint cleanup stays
    unreachable no matter how often release is retried.
    """
    from omnigent.runtime.harnesses import process_manager as pm_mod

    process = _CancelledPollerZygoteProc()
    endpoint = _CleanupCountingEndpoint()
    entry = _SubprocessEntry(
        process=process,  # type: ignore[arg-type]
        client=_NoopHarnessClient(),  # type: ignore[arg-type]
        endpoint=endpoint,  # type: ignore[arg-type]
        harness="codex",
    )
    conversation_id = "conv_cancelled_poller"
    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    manager._entries[conversation_id] = entry
    monkeypatch.setattr(pm_mod, "_RELEASE_GRACE_S", 0.02)
    monkeypatch.setattr(pm_mod._proc, "terminate_tree", lambda _process: None)
    monkeypatch.setattr(pm_mod._proc, "kill_tree", lambda _process: None)
    # The harness pid is gone: the process has actually exited.
    monkeypatch.setattr(pm_mod._proc, "process_alive", lambda _pid: False)

    if cancel_during_close:

        async def cancelled_close() -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(entry.client, "aclose", cancelled_close)
    if release_kind == "retention":
        release = manager.release_if_retention_idle(
            conversation_id,
            idle_threshold_s=0,
            expected_activity_token=manager._retention_activity_token(conversation_id, entry),
        )
    else:
        release = manager.release(conversation_id)
    if cancel_during_close:
        with pytest.raises(asyncio.CancelledError):
            await release
    else:
        assert await release in (True, "released")
    assert conversation_id not in manager._entries
    assert endpoint.cleanup_calls == 1
    assert process.returncode is None
    assert manager._release_generations[conversation_id] == 1

    # A second release must not find a re-registered entry.
    assert await manager.release(conversation_id) is True
    assert conversation_id not in manager._entries


async def test_release_keeps_live_zygote_harness_with_cancelled_poller_registered(
    short_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled poller alone must not tear down a live harness process.

    The pid probe is what distinguishes "poller gone AND process dead"
    (reapable) from "poller gone but process alive" (retry later) —
    recording the fallback code unconditionally would kill a live
    process.
    """
    from omnigent.runtime.harnesses import process_manager as pm_mod

    process = _CancelledPollerZygoteProc()
    endpoint = _CleanupCountingEndpoint()
    entry = _SubprocessEntry(
        process=process,  # type: ignore[arg-type]
        client=_NoopHarnessClient(),  # type: ignore[arg-type]
        endpoint=endpoint,  # type: ignore[arg-type]
        harness="codex",
    )
    conversation_id = "conv_cancelled_poller_live"
    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    manager._entries[conversation_id] = entry
    monkeypatch.setattr(pm_mod, "_RELEASE_GRACE_S", 0.02)
    monkeypatch.setattr(pm_mod._proc, "terminate_tree", lambda _process: None)
    monkeypatch.setattr(pm_mod._proc, "kill_tree", lambda _process: None)
    monkeypatch.setattr(pm_mod._proc, "process_alive", lambda _pid: True)

    assert await manager.release(conversation_id) is False
    assert manager._entries[conversation_id] is entry
    assert endpoint.cleanup_calls == 0


class _CancelledPollerZygoteProc:
    """``ZygoteHarnessProc`` stand-in whose exit-poller was cancelled.

    ``wait()`` is faithful to the real shim: the poller's
    ``CancelledError`` is suppressed and the fallback code is returned
    WITHOUT recording ``returncode``.
    """

    def __init__(self) -> None:
        self.pid: int | None = 987654321
        self.returncode: int | None = None
        # close_subprocess_transport(proc) getattrs this; None is handled.
        self._transport = None

        async def _parked() -> None:
            await asyncio.Event().wait()

        self._poll_task: asyncio.Task[None] = asyncio.ensure_future(_parked())
        self._poll_task.cancel()

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    async def wait(self) -> int:
        if self._poll_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        return 254


class _NoopHarnessClient:
    async def aclose(self) -> None:
        return None


class _CleanupCountingEndpoint:
    def __init__(self) -> None:
        self.cleanup_calls = 0

    def cleanup(self) -> None:
        self.cleanup_calls += 1


@pytest.mark.parametrize("outcome", ["ready", "raises", "cancelled"])
async def test_inventory_keeps_spawn_unknown_until_registration(
    short_tmp_parent: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    manager._started = True
    entry = _SubprocessEntry(
        process=SimpleNamespace(returncode=None),
        client=_NoopHarnessClient(),  # type: ignore[arg-type]
        endpoint=_CleanupCountingEndpoint(),  # type: ignore[arg-type]
        harness="codex",
    )
    entered, resume = asyncio.Event(), asyncio.Event()

    async def spawn(*_args, **_kwargs):
        entered.set()
        await resume.wait()
        if outcome == "raises":
            raise RuntimeError("spawn ownership unconfirmed")
        return entry

    monkeypatch.setattr(manager, "_spawn_entry", spawn)
    task = asyncio.create_task(manager.get_client("preparing", "codex"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        pending = await manager.resident_inventory()
        assert pending.owners == ()
        assert not pending.exit_coverage_complete
        assert manager._entries == {}
        if outcome == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            resume.set()
            if outcome == "raises":
                with pytest.raises(RuntimeError, match="spawn ownership unconfirmed"):
                    await task
            else:
                assert await task is entry.client
        after = await manager.resident_inventory()
        assert after.exit_coverage_complete is (outcome == "ready")
        assert len(after.owners) == (1 if outcome == "ready" else 0)
        assert not pending.exit_coverage_complete and pending.owners == ()
        if outcome != "ready":

            async def retry(*_args, **_kwargs):
                return entry

            monkeypatch.setattr(manager, "_spawn_entry", retry)
            assert await manager.get_client("preparing", "codex") is entry.client
            assert len((await manager.resident_inventory()).owners) == 1
            assert not (await manager.resident_inventory()).exit_coverage_complete
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_inventory_waits_for_every_overlapping_spawn(
    short_tmp_parent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    manager._started = True
    entered = {key: asyncio.Event() for key in ("one", "two")}
    resume = {key: asyncio.Event() for key in entered}

    async def spawn(session, harness, _env):
        entered[session].set()
        await resume[session].wait()
        return _SubprocessEntry(
            process=SimpleNamespace(returncode=None),
            client=_NoopHarnessClient(),  # type: ignore[arg-type]
            endpoint=_CleanupCountingEndpoint(),  # type: ignore[arg-type]
            harness=harness,
        )

    monkeypatch.setattr(manager, "_spawn_entry", spawn)
    tasks = {key: asyncio.create_task(manager.get_client(key, "codex")) for key in entered}
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 1)
        assert not (await manager.resident_inventory()).exit_coverage_complete
        resume["one"].set()
        await tasks["one"]
        partial = await manager.resident_inventory()
        assert [owner.session_id for owner in partial.owners] == ["one"]
        assert not partial.exit_coverage_complete
        resume["two"].set()
        await tasks["two"]
        complete = await manager.resident_inventory()
        assert {owner.session_id for owner in complete.owners} == {"one", "two"}
        assert complete.exit_coverage_complete
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


@pytest.mark.parametrize("outcome", ["confirmed", "raises", "cancelled"])
async def test_shutdown_spawn_close_remains_in_inventory(
    short_tmp_parent: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    manager._started = True
    entry = _SubprocessEntry(
        process=SimpleNamespace(returncode=None),
        client=_NoopHarnessClient(),  # type: ignore[arg-type]
        endpoint=_CleanupCountingEndpoint(),  # type: ignore[arg-type]
        harness="codex",
    )
    entered, resume = asyncio.Event(), asyncio.Event()

    async def spawn(*_args, **_kwargs):
        manager._shutting_down = True
        return entry

    async def close(_entry):
        entered.set()
        await resume.wait()
        if outcome == "raises":
            raise RuntimeError("close failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        entry.exit_confirmed = True
        return True

    monkeypatch.setattr(manager, "_spawn_entry", spawn)
    monkeypatch.setattr(manager, "_close_entry", close)
    task = asyncio.create_task(manager.get_client("closing_spawn", "codex"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        snapshot = await manager.resident_inventory()
        assert len(snapshot.owners) == 1
        assert snapshot.owners[0].incarnation_id == entry.incarnation_id
        assert snapshot.owners[0].closing
    finally:
        resume.set()
        with pytest.raises(asyncio.CancelledError if outcome == "cancelled" else RuntimeError):
            await task
    snapshot = await manager.resident_inventory()
    assert [owner.incarnation_id for owner in snapshot.owners] == (
        [] if outcome == "confirmed" else [entry.incarnation_id]
    )
    assert all(owner.closing and not owner.exit_confirmed for owner in snapshot.owners)
    assert snapshot.exit_coverage_complete is (outcome == "confirmed")
    assert not manager._closing_entries


async def test_retired_owner_candidate_budget_never_becomes_empty_proof(
    short_tmp_parent, monkeypatch
):
    from omnigent.runtime.harnesses import process_manager

    manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
    monkeypatch.setattr(process_manager, "_MAX_RETIRED_OWNER_CANDIDATES", 1)

    async def failed_close(entry):
        entry.process.returncode = 0
        raise RuntimeError("owned exit unconfirmed")

    monkeypatch.setattr(manager, "_close_entry", failed_close)
    for session in ("first", "second"):
        manager._entries[session] = _SubprocessEntry(
            SimpleNamespace(returncode=None),
            _NoopHarnessClient(),  # type: ignore[arg-type]
            _CleanupCountingEndpoint(),  # type: ignore[arg-type]
            "codex",
        )
        with pytest.raises(RuntimeError, match="owned exit unconfirmed"):
            await manager.release(session)
    snapshot = await manager.resident_inventory(context_reads=())
    assert len(snapshot.owners) == 1 and snapshot.owners[0].session_id == "first"
    assert snapshot.contexts == (None,)
    assert not snapshot.exit_coverage_complete
    assert not manager._entries and not manager._closing_entries


async def test_shutdown_during_spawn_leaves_no_live_process(
    manager: HarnessProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shutdown`` during cold spawn must not leave an unregistered process.

    Shutdown used to walk only ``_entries``. A spawn still awaiting
    readiness was invisible, so shutdown could finish and the spawn
    could then register a live process against a torn-down manager.
    Barriers pin the race; after both settle there must be no process,
    socket, or ``_entries`` record.
    """
    from omnigent.runtime.harnesses import process_manager as pm_mod

    await manager.start()
    get_task: asyncio.Task[object] | None = None
    shutdown_task: asyncio.Task[None] | None = None
    allow_bind = asyncio.Event()
    try:
        spawned: list[asyncio.subprocess.Process] = []
        real_exec = asyncio.create_subprocess_exec

        async def capturing_exec(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            process = await real_exec(*args, **kwargs)  # type: ignore[arg-type]
            spawned.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing_exec)

        in_bind = asyncio.Event()
        real_wait_for_bind = pm_mod._wait_for_bind

        async def gated_bind(*args: object, **kwargs: object) -> None:
            in_bind.set()
            await allow_bind.wait()
            await real_wait_for_bind(*args, **kwargs)

        monkeypatch.setattr(pm_mod, "_wait_for_bind", gated_bind)

        conv_id = "conv_shutdown_during_spawn"
        get_task = asyncio.create_task(manager.get_client(conv_id, _TEST_HARNESS_NAME))
        await asyncio.wait_for(in_bind.wait(), timeout=10.0)
        assert spawned, "spawn was never reached"
        process = spawned[0]
        pid = process.pid
        socket_path = manager.instance_dir / f"conv-{conv_id}.sock"

        shutdown_task = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0)
        assert not shutdown_task.done(), "shutdown finished before in-flight spawn drained"

        allow_bind.set()
        # Spawn either registers-then-gets-released, or discards on the
        # shutting-down check — assert the shutdown-specific errors only.
        done, pending = await asyncio.wait({get_task})
        assert not pending
        assert done == {get_task}
        exc = get_task.exception()
        assert isinstance(exc, RuntimeError)
        assert "shutdown" in str(exc).lower()
        assert await shutdown_task is None

        assert not manager.has_session(conv_id)
        assert conv_id not in manager._entries
        assert not socket_path.exists()
        for _ in range(40):
            if not _pid_alive(pid):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(pid), "shutdown-during-spawn left a live harness process"
        assert process.returncode is not None
    finally:
        allow_bind.set()
        await _cancel_pending(get_task, shutdown_task)
        # Idempotent if the test already shut down; still safe if it failed early.
        await manager.shutdown()


@pytest.fixture
async def resident_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import omnigent.harness_plugins as hp
    from omnigent.inner.executor import Executor, ExecutorResidentInventory, ExecutorResidentOwner
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.runtime.harnesses.process_manager import _HarnessEndpoint

    # This fixture's registry is already loaded; individual cases may replace it.
    contribution = hp.HarnessContribution(
        name="fixture-registry",
        valid_harnesses=frozenset(_HARNESS_MODULES),
        harness_modules=dict(_HARNESS_MODULES),
    )
    monkeypatch.setattr(hp, "_state", hp.HarnessPluginState((contribution,), {}))

    class Owner(Executor):
        inventory = ExecutorResidentInventory(
            owners=(
                ExecutorResidentOwner(
                    "context-a", ("owner-key", "ui-key"), "thread-a", True, True, False
                ),
            ),
        )

        def resident_inventory(self):
            return self.inventory

    owner = Owner()
    adapter = ExecutorAdapter(lambda: pytest.fail("read must not construct an executor"))
    adapter._executor = owner
    app = adapter.build()
    app.state.conversation_id = "conversation"
    app.state.harness_auth_token = "test-auth"
    requests = []

    async def record(request):
        requests.append((request.method, request.url.path))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"Authorization": "Bearer test-auth"},
        event_hooks={"request": [record]},
    ) as client:
        manager = HarnessProcessManager(tmp_parent=tmp_path)
        manager._started = True
        entry = _SubprocessEntry(_FakeReapProc(), client, _HarnessEndpoint(), "codex")
        manager._entries["conversation"] = entry
        manager._retention_managed["conversation"] = 0.0
        manager.mark_in_flight("conversation", "response-working")

        def forbidden(*_args, **_kwargs):
            pytest.fail("read must not spawn, probe, or mutate retention")

        for name in ("start", "get_client", "_spawn_entry", "_retention_expiry", "note_activity"):
            monkeypatch.setattr(manager, name, forbidden)
        yield manager, entry, owner, app, requests


async def _read_selected_inventory(manager, selected, threads, *, timeout_s=5.0):
    if threads:
        return (await manager.prepare_resident_thread_inventory(selected, timeout_s=timeout_s))()
    return await manager.read_resident_inventory(selected, timeout_s=timeout_s)


@pytest.fixture
def context_wire_v2():
    from omnigent.startup_protocol import unknown_context_evidence_v2

    return unknown_context_evidence_v2("conversation", "read", "unimplemented").model_dump(
        mode="json"
    )


@pytest.fixture
def context_record_v2():
    return {
        "key": {"source_owner_incarnation": "owner", "context_incarnation": "birth-a"},
        "vendor_id": {"namespace": "provider/store", "value": "literal:shared/id"},
        "operation": {"operation_id": "operation-a", "action": "create", "parent": None},
        "execution_keys": ["execution-a"],
        "provenance": "primary",
        "lifecycle": "live",
        "aliases": [],
        "revision": 0,
        "stop_proof": None,
        "continuity_proof": None,
    }


@pytest.fixture
def complete_context_wire_v2(context_wire_v2, context_record_v2):
    import copy

    from omnigent.startup_protocol import ContextAuthorityV2, ScopeKeyV2

    authority = ContextAuthorityV2(
        session_id="conversation",
        account_id="account",
        host_id="host",
        host_generation="host-generation",
        runner_id="runner",
        runner_generation="runner-generation",
        connection_generation="current",
    )
    raw = context_wire_v2
    raw["authority"] = authority.model_dump(mode="json")
    first = context_record_v2
    second = copy.deepcopy(first)
    second["key"]["context_incarnation"] = "birth-b"
    second["operation"]["operation_id"] = "operation-b"
    second["execution_keys"] = ["execution-b"]
    for record, views in ((first, ("view-a", "view-a-2")), (second, ("view-b",))):
        record["aliases"] = [
            {
                "view_key": view,
                "generation": "current",
                "target": copy.deepcopy(record["key"]),
                "read_only": True,
                "operation_id": view + "-operation",
                "receipt_id": view + "-receipt",
            }
            for view in views
        ]
    raw["sources"][0].update(
        producer_incarnation="owner",
        coverage="complete",
        contexts=[first, second],
        unresolved=[],
        membership_revision=1,
        context_revision=2,
        revision_authority="authoritative_membership",
        age_upper_bound_s=0.0,
    )
    scopes = (ScopeKeyV2.model_validate(raw["expected_scopes"][0]),)
    return raw, authority, scopes


def _decode_complete_context_wire_v2(raw, authority, scopes):
    import json

    from omnigent.startup_protocol import decode_context_evidence_v2

    return decode_context_evidence_v2(
        json.dumps(raw).encode(),
        read_id="read",
        authority=authority,
        expected_scopes=scopes,
    )


@pytest.mark.parametrize("case", ["two_targets", "another_execution"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_context_inventory_v2_source_alias_conflicts_rejected_by_decoder(
    complete_context_wire_v2,
    case,
    reverse,
):
    raw, authority, scopes = complete_context_wire_v2
    assert (
        _decode_complete_context_wire_v2(raw, authority, scopes).sources[0].coverage == "complete"
    )
    first, second = raw["sources"][0]["contexts"]
    if case == "two_targets":
        second["aliases"][0]["view_key"] = first["aliases"][0]["view_key"]
    else:
        first["aliases"][0]["view_key"] = second["execution_keys"][0]
    if reverse:
        raw["sources"][0]["contexts"].reverse()
    with pytest.raises(ValueError, match="conflicting source alias"):
        _decode_complete_context_wire_v2(raw, authority, scopes)


def test_context_inventory_v2_source_aliases_keep_distinct_read_only_views(
    complete_context_wire_v2,
):
    raw, authority, scopes = complete_context_wire_v2
    evidence = _decode_complete_context_wire_v2(raw, authority, scopes)
    first, second = evidence.sources[0].contexts
    assert [alias.view_key for alias in first.aliases] == ["view-a", "view-a-2"]
    assert all(alias.target == first.key and alias.read_only for alias in first.aliases)
    assert second.aliases[0].target == second.key and first.key != second.key
    assert first.vendor_id == second.vendor_id


def test_context_inventory_v2_source_aliases_remain_scoped_to_independent_sources(
    complete_context_wire_v2,
):
    import copy

    from omnigent.startup_protocol import ScopeKeyV2

    raw, authority, scopes = complete_context_wire_v2
    other = copy.deepcopy(raw["sources"][0])
    other["scope"]["namespace"] = "independent-provider"
    other["producer_incarnation"] = "other-owner"
    for record in other["contexts"]:
        record["key"]["source_owner_incarnation"] = "other-owner"
        for alias in record["aliases"]:
            alias["target"]["source_owner_incarnation"] = "other-owner"
    # Cross-source strings can coincide without identifying the same execution or view.
    other["contexts"][0]["execution_keys"] = ["view-a"]
    other["contexts"][0]["aliases"][0]["view_key"] = "execution-a"
    raw["sources"].append(other)
    raw["expected_scopes"].append(copy.deepcopy(other["scope"]))
    scopes += (ScopeKeyV2.model_validate(other["scope"]),)
    evidence = _decode_complete_context_wire_v2(raw, authority, scopes)
    assert len(evidence.sources) == 2
    assert evidence.sources[0].contexts[0].key != evidence.sources[1].contexts[0].key
    assert evidence.sources[0].contexts[1].aliases[0].view_key == "view-b"
    assert evidence.sources[1].contexts[1].aliases[0].view_key == "view-b"


def _decode_context_wire_v2(payload):
    from omnigent.startup_protocol import (
        decode_context_evidence_v2,
        executor_context_scope_v2,
        harness_context_authority_v2,
    )

    return decode_context_evidence_v2(
        payload,
        read_id="read",
        authority=harness_context_authority_v2("conversation"),
        expected_scopes=(executor_context_scope_v2("conversation"),),
    )


@pytest.mark.parametrize(
    "case",
    [
        "version_float",
        "version_bool",
        "version_old",
        "unknown_field",
        "missing_field",
        "wrong_read",
        "wrong_authority",
        "manifest_shrunk",
        "duplicate_scope",
        "duplicate_source",
        "bool_revision",
        "float_revision",
        "negative_revision",
        "unknown_coverage",
        "missing_reason",
        "complete_observation",
        "complete_unbound",
        "negative_age",
        "infinite_age",
        "bool_age",
        "too_many_contexts",
        "null_contexts",
        "too_many_scopes",
        "oversize_id",
    ],
)
def test_context_inventory_v2_strict_wire(context_wire_v2, case):
    import json

    raw = context_wire_v2
    source = raw["sources"][0]
    if case.startswith("version_"):
        raw["wire_version"] = {"version_float": 2.0, "version_bool": True, "version_old": 1}[case]
    elif case == "unknown_field":
        raw["safe_to_restart"] = True
    elif case == "missing_field":
        del source["work_bindings"]
    elif case == "wrong_read":
        raw["read_id"] = "completed-read"
    elif case == "wrong_authority":
        raw["authority"]["host_generation"] = "previous"
    elif case == "manifest_shrunk":
        raw["expected_scopes"] = []
    elif case == "duplicate_scope":
        raw["expected_scopes"] *= 2
    elif case == "duplicate_source":
        raw["sources"] *= 2
    elif case.endswith("revision"):
        source["membership_revision"] = {
            "bool_revision": True,
            "float_revision": 1.0,
            "negative_revision": -1,
        }[case]
    elif case == "unknown_coverage":
        source["coverage"] = "ready"
    elif case == "missing_reason":
        source["unresolved"] = []
    elif case.startswith("complete_"):
        source.update(
            coverage="complete",
            producer_incarnation="owner",
            membership_revision=1,
            context_revision=1,
            age_upper_bound_s=0.0,
            unresolved=[],
        )
        if case == "complete_unbound":
            source["revision_authority"] = "authoritative_membership"
    elif case.endswith("age"):
        source["age_upper_bound_s"] = {
            "negative_age": -1,
            "infinite_age": float("inf"),
            "bool_age": True,
        }[case]
    elif case == "too_many_contexts":
        source["contexts"] = [{}] * 1001
    elif case == "null_contexts":
        source["contexts"] = None
    elif case == "too_many_scopes":
        raw["expected_scopes"] *= 65
    elif case == "oversize_id":
        raw["read_id"] = "r" * 129
    with pytest.raises(ValueError):
        _decode_context_wire_v2(json.dumps(raw).encode())


@pytest.mark.parametrize(
    "payload",
    [
        b'{"wire_version":2,"wire_version":2}',
        b'{"sources":[{"coverage":"partial","coverage":"complete"}]}',
        b"[" * 1500 + b"]" * 1500,
        b" " * (1024 * 1024 + 1),
    ],
    ids=["duplicate_root", "duplicate_nested", "nesting", "bytes"],
)
def test_context_inventory_v2_rejects_duplicate_fields_and_resource_overflow(payload):
    with pytest.raises(ValueError):
        _decode_context_wire_v2(payload)


def test_context_inventory_v2_retains_missing_extra_scopes_and_literal_independent_ids(
    context_wire_v2,
    context_record_v2,
):
    import copy
    import json

    raw = context_wire_v2
    source = raw["sources"][0]
    source["producer_incarnation"] = "owner"
    context = context_record_v2
    other = copy.deepcopy(context)
    other["key"]["context_incarnation"] = "birth-b"
    other["operation"]["operation_id"] = "operation-b"
    other["execution_keys"] = ["execution-b"]
    source["contexts"] = [context, other]
    evidence = _decode_context_wire_v2(json.dumps(raw).encode())
    records = evidence.sources[0].contexts
    assert records[0].key != records[1].key and records[0].vendor_id == records[1].vendor_id
    assert len(records) == 2 and not records[0].aliases
    with pytest.raises(ValueError):
        evidence.read_id = "changed"
    raw["sources"] = []
    assert _decode_context_wire_v2(json.dumps(raw).encode()).expected_scopes
    source["scope"] = {**source["scope"], "population": "unmatched_actual_scope"}
    raw["sources"] = [source]
    assert (
        _decode_context_wire_v2(json.dumps(raw).encode()).sources[0].scope
        != evidence.expected_scopes[0]
    )


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_context",
        "foreign_owner",
        "execution_alias",
        "writable_alias",
        "numeric_alias",
        "stale_alias",
        "foreign_alias",
        "stopped_without_proof",
        "foreign_stop",
        "stale_stop",
        "wrong_execution",
        "duplicate_execution",
        "unproven_side_chat",
        "unknown_lifecycle",
        "foreign_work_binding",
    ],
)
def test_context_inventory_v2_rejects_conflicting_identity_and_proofs(
    context_wire_v2,
    context_record_v2,
    case,
):
    import copy
    import json

    from omnigent.startup_protocol import ContextEvidenceV2

    raw, context = context_wire_v2, context_record_v2
    source = raw["sources"][0]
    source.update(producer_incarnation="owner", contexts=[context])
    if case == "duplicate_context":
        source["contexts"].append(copy.deepcopy(context))
    elif case == "foreign_owner":
        context["key"]["source_owner_incarnation"] = "other-owner"
    elif case.endswith("alias"):
        raw["authority"]["connection_generation"] = "current"
        alias = {
            "view_key": "view",
            "generation": "current",
            "target": copy.deepcopy(context["key"]),
            "read_only": True,
            "operation_id": "view-operation",
            "receipt_id": "view-receipt",
        }
        context["aliases"] = [alias]
        if case == "execution_alias":
            alias["view_key"] = "execution-a"
        elif case == "writable_alias":
            alias["read_only"] = False
        elif case == "numeric_alias":
            alias["read_only"] = 1
        elif case == "stale_alias":
            alias["generation"] = "old"
        else:
            alias["target"]["context_incarnation"] = "other-context"
    elif case == "stopped_without_proof":
        context["lifecycle"] = "stopped"
    elif case in {"foreign_stop", "stale_stop", "wrong_execution"}:
        context["lifecycle"] = "stopped"
        proof = {
            "context": copy.deepcopy(context["key"]),
            "receipt_id": "stop-receipt",
            "authority": copy.deepcopy(raw["authority"]),
            "execution_keys": ["execution-a"],
        }
        context["stop_proof"] = proof
        if case == "foreign_stop":
            proof["context"]["context_incarnation"] = "other-context"
        elif case == "stale_stop":
            proof["authority"]["connection_generation"] = "previous"
        else:
            proof["execution_keys"] = []
    elif case == "duplicate_execution":
        context["execution_keys"] *= 2
    elif case == "unproven_side_chat":
        context["provenance"] = "managed_side_chat"
    elif case == "unknown_lifecycle":
        context["lifecycle"] = "safe_to_close"
    else:
        source["work_bindings"] = [
            {
                "context": {**context["key"], "context_incarnation": "other-context"},
                "runtime_id": "runtime",
                "generation": "work-generation",
            }
        ]
    with pytest.raises(ValueError):
        ContextEvidenceV2.model_validate_json(json.dumps(raw), strict=True)


@pytest.fixture
def authoritative_resident_context_reader(
    resident_reader, context_wire_v2, context_record_v2, monkeypatch
):
    import copy
    import json

    from omnigent.startup_protocol import ContextEvidenceV2

    manager, entry, owner, app, requests = resident_reader

    async def prepare(session_id, read_id, *, timeout_s=5.0):
        raw = copy.deepcopy(context_wire_v2)
        raw["read_id"] = read_id
        raw["sources"][0].update(
            producer_incarnation="owner",
            coverage="partial",
            contexts=[context_record_v2],
            unresolved=[{"candidate": None, "reason": "owned_scope_incomplete"}],
            membership_revision=1,
            context_revision=1,
            revision_authority="authoritative_membership",
            age_upper_bound_s=0.0,
            work_revision=2,
            work_bindings=[
                {
                    "context": context_record_v2["key"],
                    "runtime_id": "fixture-runtime",
                    "generation": "fixture-work-generation",
                }
            ],
        )
        evidence = ContextEvidenceV2.model_validate_json(json.dumps(raw), strict=True)
        return lambda: evidence

    monkeypatch.setattr(owner, "prepare_context_inventory_v2", prepare)
    return manager, entry, owner, app, requests


async def test_context_inventory_v2_plugin_error_cannot_clear_source_denominator(
    authoritative_resident_context_reader, monkeypatch
):
    import omnigent.harness_plugins as hp

    manager, entry, _owner, _app, requests = authoritative_resident_context_reader
    monkeypatch.setattr(
        hp, "_state", hp.HarnessPluginState((), {"broken-plugin": "not disclosed"})
    )
    selected = (await manager.resident_inventory()).owners[0]
    source = (await manager.prepare_context_inventory_v2(selected))().evidence.sources[0]
    assert source.coverage == "partial"
    assert source.contexts[0].vendor_id.value == "literal:shared/id"
    assert source.revision_authority == "observation_only"
    assert source.membership_revision is source.context_revision is source.work_bindings is None
    assert "harness_plugin_load_errors" in {gap.reason for gap in source.unresolved}
    assert requests == [("GET", "/v2/sessions/conversation/resident-context-inventory")]
    assert entry.last_used_at == 0.0


@pytest.mark.parametrize(
    "case",
    [
        "known",
        "registered_alias",
        "generic_acp",
        "missing",
        "unloaded",
        "declared_missing",
        "declaration_removed",
        "declarations_empty",
        "module_change",
        "contribution_change",
    ],
)
async def test_context_inventory_v2_reconciles_loaded_manifest_without_losing_contexts(
    authoritative_resident_context_reader, monkeypatch, case
):
    import omnigent.harness_plugins as hp

    manager, entry, _owner, _app, requests = authoritative_resident_context_reader
    expected = None
    if case == "registered_alias":
        monkeypatch.setitem(_HARNESS_MODULES, "fixture-alias", _HARNESS_MODULES["codex"])
        contribution = hp.HarnessContribution(
            name="fixture-registry",
            harness_modules=dict(_HARNESS_MODULES),
            aliases={"fixture-alias": "codex"},
        )
        monkeypatch.setattr(hp, "_state", hp.HarnessPluginState((contribution,), {}))
        entry.harness = "fixture-alias"
    elif case == "generic_acp":
        entry.harness = "acp:fixture"
    elif case == "missing":
        entry.harness = "missing-fixture-harness"
        expected = "harness_source_unregistered"
    elif case == "unloaded":
        monkeypatch.setattr(hp, "_state", None)
        expected = "harness_source_manifest_unavailable"
    elif case == "declared_missing":
        contribution = hp.HarnessContribution(
            name="fixture-plugin",
            valid_harnesses=frozenset({"fixture"}),
            harness_modules={"fixture": "omnigent.community.fixture"},
        )
        monkeypatch.setattr(hp, "_state", hp.HarnessPluginState((contribution,), {}))
        expected = "harness_registry_manifest_mismatch"
    elif case in {"declaration_removed", "declarations_empty"}:
        modules = dict(_HARNESS_MODULES)
        modules.pop("codex")
        contributions = (
            (hp.HarnessContribution(name="fixture-registry", harness_modules=modules),)
            if case == "declaration_removed"
            else ()
        )
        monkeypatch.setattr(hp, "_state", hp.HarnessPluginState(contributions, {}))
        expected = "harness_registry_manifest_mismatch"
    selected = (await manager.resident_inventory()).owners[0]
    reconcile = await manager.prepare_context_inventory_v2(selected)
    if case == "module_change":
        monkeypatch.setitem(_HARNESS_MODULES, "new-fixture", "omnigent.community.fixture")
        expected = "harness_source_manifest_changed"
    elif case == "contribution_change":
        monkeypatch.setattr(
            hp,
            "_state",
            hp.HarnessPluginState(
                (
                    hp.HarnessContribution(
                        name="new-fixture-plugin", valid_harnesses=frozenset({"new"})
                    ),
                ),
                {},
            ),
        )
        expected = "harness_source_manifest_changed"
    source = reconcile().evidence.sources[0]
    assert source.coverage == "partial" and len(source.contexts) == 1
    assert source.contexts[0].vendor_id.value == "literal:shared/id"
    if expected is None:
        assert source.revision_authority == "authoritative_membership"
        assert source.membership_revision == source.context_revision == 1
        assert source.age_upper_bound_s is not None
        assert source.work_revision == 2
        assert source.work_bindings[0].runtime_id == "fixture-runtime"
        assert [gap.reason for gap in source.unresolved] == ["owned_scope_incomplete"]
    else:
        assert source.revision_authority == "observation_only"
        assert (
            source.membership_revision is source.context_revision is source.work_bindings is None
        )
        assert source.age_upper_bound_s is None
        assert source.work_revision is None
        assert source.unresolved[-1].reason == expected
    assert requests == [("GET", "/v2/sessions/conversation/resident-context-inventory")]
    assert manager._retention_managed == {"conversation": 0.0}


async def test_context_inventory_v2_manifest_gap_keeps_bounded_diagnostics(
    authoritative_resident_context_reader, monkeypatch
):
    import omnigent.harness_plugins as hp
    from omnigent.startup_protocol import UnresolvedContextV2

    manager, _entry, owner, _app, _requests = authoritative_resident_context_reader
    original = owner.prepare_context_inventory_v2

    async def prepare(*args, **kwargs):
        read = await original(*args, **kwargs)
        evidence = read()
        source = evidence.sources[0].model_copy(
            update={
                "unresolved": tuple(
                    UnresolvedContextV2(candidate=f"candidate-{i}", reason="unresolved")
                    for i in range(1000)
                )
            }
        )
        return lambda: evidence.model_copy(update={"sources": (source,)})

    monkeypatch.setattr(owner, "prepare_context_inventory_v2", prepare)
    monkeypatch.setattr(hp, "_state", hp.HarnessPluginState((), {"broken": "not disclosed"}))
    selected = (await manager.resident_inventory()).owners[0]
    source = (await manager.prepare_context_inventory_v2(selected))().evidence.sources[0]
    assert len(source.unresolved) == 1000 and len(source.contexts) == 1
    assert source.unresolved[-1].reason == "harness_manifest_reason_budget"
    assert source.coverage == "partial" and source.revision_authority == "observation_only"


async def test_context_inventory_v2_real_channel_is_truthful_and_read_only(resident_reader):
    manager, entry, owner, _app, requests = resident_reader
    selected = (await manager.resident_inventory()).owners[0]
    reconcile = await manager.prepare_context_inventory_v2(selected)
    first = reconcile()
    second = (await manager.prepare_context_inventory_v2(selected))()
    assert first.evidence.wire_version == 2
    assert first.evidence.read_id != second.evidence.read_id
    assert first.evidence.sources[0].coverage == "unsupported"
    assert first.evidence.sources[0].unresolved[0].reason == "context_source_unimplemented"
    assert first.evidence.sources[0].contexts == ()
    assert first.evidence.sources[0].producer_incarnation is None
    assert first.evidence.authority.account_id is None
    assert first.owner == selected and first.release_generation == 0
    assert reconcile().request_elapsed_s >= first.request_elapsed_s
    assert owner.resident_inventory().owners[0].vendor_context_id == "thread-a"
    assert requests == [("GET", "/v2/sessions/conversation/resident-context-inventory")] * 2
    assert manager._retention_managed == {"conversation": 0.0}
    assert manager._in_flight_response_ids == {"conversation": "response-working"}
    assert entry.last_used_at == 0.0 and not entry.process.killed


@pytest.mark.parametrize(
    "change", ["release", "process", "client", "closing", "shutdown", "entry"]
)
async def test_context_inventory_v2_reconciles_after_other_sources_wait(resident_reader, change):
    manager, entry, _owner, _app, requests = resident_reader
    selected = (await manager.resident_inventory()).owners[0]
    reconcile = await manager.prepare_context_inventory_v2(selected)
    if change == "release":
        manager._release_generations["conversation"] = 1
    elif change == "process":
        entry.process.returncode = 0
    elif change == "client":
        await entry.client.aclose()
    elif change == "closing":
        entry.teardown_started = True
    elif change == "shutdown":
        manager._shutting_down = True
    else:
        manager._entries.pop("conversation")
    assert reconcile().evidence.sources[0].unresolved[0].reason == "owner_changed"
    assert len(requests) == 1
    next_read = (await manager.prepare_context_inventory_v2(selected))()
    assert next_read.evidence.sources[0].coverage == (
        "unsupported" if change == "release" else "partial"
    )
    assert len(requests) == (2 if change == "release" else 1)


@pytest.mark.parametrize(
    "case",
    [
        "old_peer",
        "wrong_token",
        "wrong_read",
        "unknown_field",
        "timeout",
        "oversized",
        "redirect",
    ],
)
async def test_context_inventory_v2_mixed_peers_fail_closed(resident_reader, monkeypatch, case):
    import json

    from omnigent.startup_protocol import CONTEXT_INVENTORY_V2, unknown_context_evidence_v2

    manager, entry, _owner, _app, requests = resident_reader
    calls = []

    async def reply(request):
        calls.append(request.url)
        if case == "timeout":
            raise httpx.ReadTimeout("no response")
        payload = unknown_context_evidence_v2(
            "conversation", request.url.params["read_id"], "unimplemented", unsupported=True
        ).model_dump(mode="json")
        if case == "wrong_read":
            payload["read_id"] = "old"
        elif case == "unknown_field":
            payload["capacity"] = 0
        return httpx.Response(
            404 if case == "old_peer" else 302 if case == "redirect" else 200,
            headers={
                "Omnigent-Context-Inventory-Contract": "v1"
                if case == "wrong_token"
                else CONTEXT_INVENTORY_V2
            },
            content=b" " * (1024 * 1024 + 1)
            if case == "oversized"
            else json.dumps(payload).encode(),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(reply), base_url="http://existing"
    ) as client:
        monkeypatch.setattr(entry, "client", client)
        selected = (await manager.resident_inventory()).owners[0]
        result = (await manager.prepare_context_inventory_v2(selected))()
    source = result.evidence.sources[0]
    assert source.coverage == ("unsupported" if case == "old_peer" else "partial")
    assert source.contexts == () and source.unresolved
    assert len(calls) == 1 and calls[0].path.startswith("/v2/")
    assert requests == [] and entry.last_used_at == 0.0


@pytest.mark.parametrize(
    "case", ["auth_before", "conversation_before", "auth_after", "conversation_after"]
)
async def test_context_inventory_v2_auth_and_conversation_rechecked(
    resident_reader, monkeypatch, case
):
    from omnigent.startup_protocol import unknown_context_evidence_v2

    _manager, entry, owner, app, _requests = resident_reader
    reads = []

    async def prepare(session_id, read_id, *, timeout_s):
        reads.append(timeout_s)
        if case == "auth_after":
            app.state.harness_auth_token = "changed-auth"
        elif case == "conversation_after":
            app.state.conversation_id = "different"
        return lambda: unknown_context_evidence_v2(session_id, read_id, "unimplemented")

    monkeypatch.setattr(owner, "prepare_context_inventory_v2", prepare)
    headers = {"Authorization": "Bearer wrong"} if case == "auth_before" else {}
    target = "different" if case == "conversation_before" else "conversation"
    response = await entry.client.get(
        f"/v2/sessions/{target}/resident-context-inventory",
        params={"read_id": "read"},
        headers=headers,
    )
    assert response.status_code == (401 if case.startswith("auth") else 404)
    assert len(reads) == (1 if case.endswith("after") else 0)


async def test_context_inventory_v2_budget_includes_registry_wait_and_cancellation(
    resident_reader,
):
    manager, _entry, _owner, _app, requests = resident_reader
    selected = (await manager.resident_inventory()).owners[0]
    async with manager._registry_lock:
        result = (await manager.prepare_context_inventory_v2(selected, timeout_s=0.01))()
        assert result.evidence.sources[0].unresolved[0].reason == "invalid_or_failed_read"
        read = asyncio.create_task(manager.prepare_context_inventory_v2(selected))
        await asyncio.sleep(0)
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read
    assert requests == []


async def test_context_inventory_v2_preserves_source_age_across_reconciliation(
    resident_reader, monkeypatch
):
    from omnigent.startup_protocol import unknown_context_evidence_v2

    manager, _entry, owner, _app, _requests = resident_reader

    async def prepare(session_id, read_id, *, timeout_s):
        assert timeout_s == 5.0
        evidence = unknown_context_evidence_v2(session_id, read_id, "incomplete")
        return lambda: evidence.model_copy(
            update={
                "sources": (evidence.sources[0].model_copy(update={"age_upper_bound_s": 2.0}),),
            }
        )

    monkeypatch.setattr(owner, "prepare_context_inventory_v2", prepare)
    selected = (await manager.resident_inventory()).owners[0]
    reconcile = await manager.prepare_context_inventory_v2(selected)
    first = reconcile()
    second = reconcile()
    assert first.evidence.sources[0].age_upper_bound_s == 2.0 + first.request_elapsed_s
    assert second.evidence.sources[0].age_upper_bound_s == 2.0 + second.request_elapsed_s
    assert second.request_elapsed_s >= first.request_elapsed_s


@pytest.mark.parametrize(
    "query",
    ["", "?read_id=", "?read_id=a&read_id=b", "?read_id=" + "a" * 129],
    ids=["missing", "empty", "duplicate", "too_long"],
)
async def test_context_inventory_v2_rejects_invalid_read_before_producer(
    resident_reader, monkeypatch, query
):
    from unittest.mock import AsyncMock

    _manager, entry, owner, _app, _requests = resident_reader
    monkeypatch.setattr(
        owner, "prepare_context_inventory_v2", AsyncMock(side_effect=AssertionError)
    )
    result = await entry.client.get("/v2/sessions/conversation/resident-context-inventory" + query)
    assert result.status_code == 422


@pytest.mark.parametrize("state", ["detached", "cleanup", "missing", "shutdown"])
async def test_context_inventory_v2_adapter_never_fabricates_missing_owner(state):
    from omnigent.inner.executor import Executor
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(lambda: pytest.fail("inventory must not construct the executor"))
    adapter._executor = Executor()
    prepared = await adapter.prepare_context_inventory_v2("conversation", "read")
    if state == "detached":
        adapter._resident_inventory_detached = True
    elif state == "cleanup":
        adapter._abandoned_executor_cleanup = asyncio.get_running_loop().create_future()
    elif state == "missing":
        adapter._executor = None
    else:
        adapter._shutting_down.set()
    assert prepared().sources[0].unresolved[0].reason == "executor_unbound"
    assert (await adapter.prepare_context_inventory_v2("conversation", "next"))().sources[
        0
    ].coverage == "partial"


@pytest.mark.parametrize(
    "case",
    [
        "loaded",
        "owner-empty",
        "empty",
        "unknown",
        "unsupported",
        "literal",
        "overlap",
        "max",
        "generation-zero",
        "generation-later",
        "unknown-unbound",
        "mixed-generations",
        "unknown-contexts",
        "distinct-context-literals",
    ],
)
async def test_prepare_resident_thread_inventory_uses_existing_channel(resident_reader, case):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from omnigent.inner.executor import ExecutorResidentInventory, ExecutorThreadInventorySnapshot

    manager, entry, owner, _app, requests = resident_reader
    owner.inventory = replace(
        owner.inventory,
        owners=(replace(owner.inventory.owners[0], closing=False, transport_generation=0),),
    )
    ids = (("thread-a", "side"),)
    if case == "owner-empty":
        ids = ((),)
    elif case == "empty":
        owner.inventory = ExecutorResidentInventory(owners=())
        ids = ()
    elif case == "unknown":
        ids = (None,)
    elif case == "unsupported":
        ids = None
    elif case in {"unknown-unbound", "mixed-generations"}:
        first = replace(owner.inventory.owners[0], closing=True, transport_generation=None)
        owners = (first,)
        ids = (None,)
        if case == "mixed-generations":
            owners += (
                replace(first, incarnation_id="context-b", closing=False, transport_generation=7),
            )
            ids += (("side",),)
        owner.inventory = replace(
            owner.inventory,
            owners=owners,
            ambiguous_context_ids=("thread-a",) if len(owners) > 1 else (),
        )
    elif case in {"unknown-contexts", "distinct-context-literals"}:
        first = owner.inventory.owners[0]
        owner.inventory = replace(
            owner.inventory,
            owners=(
                replace(
                    first, vendor_context_id=None if case == "unknown-contexts" else " thread-a "
                ),
                replace(
                    first,
                    incarnation_id="context-b",
                    vendor_context_id=None if case == "unknown-contexts" else "thread-a",
                ),
            ),
        )
        ids = (None, None)
    elif case == "literal":
        ids = ((" thread-a ", "thread-a"),)
        first = owner.inventory.owners[0]
        owner.inventory = replace(
            owner.inventory,
            owners=(replace(first, session_keys=(" main ", "main")),),
            ambiguous_context_ids=(" thread-a ", "thread-a"),
        )
    elif case == "overlap":
        first = owner.inventory.owners[0]
        owner.inventory = replace(
            owner.inventory,
            owners=(first, replace(first, incarnation_id="context-b")),
            ambiguous_context_ids=("thread-a",),
        )
        ids = (("shared",), ("shared",))
    elif case == "max":
        ids = (tuple(f"thread-{i}" for i in range(1000)),)
    elif case.startswith("generation-"):
        owner.inventory = replace(
            owner.inventory,
            owners=(
                replace(
                    owner.inventory.owners[0],
                    closing=False,
                    transport_generation=0 if case == "generation-zero" else 7,
                ),
            ),
        )
    snapshot = ExecutorThreadInventorySnapshot(owner.inventory, ids)
    owner.prepare_resident_thread_inventory = AsyncMock(return_value=lambda: snapshot)
    selected = (await manager.resident_inventory()).owners[0]
    reconcile = await manager.prepare_resident_thread_inventory(selected)
    result = reconcile()
    assert result.context.owner == selected
    assert result.context.release_generation == 0
    assert result.context.inventory == owner.inventory
    assert result.thread_ids == ids
    assert (
        not result.context.inventory.aliases_complete
        and not result.context.inventory.context_coverage_complete
    )
    with pytest.raises(FrozenInstanceError):
        result.thread_ids = None
    assert reconcile() == result
    assert requests == [("GET", "/v1/sessions/conversation/resident-thread-inventory")]
    assert manager._retention_managed == {"conversation": 0.0}
    assert manager._in_flight_response_ids == {"conversation": "response-working"}
    assert entry.last_used_at == 0.0 and not entry.process.killed


@pytest.mark.parametrize("change", ["client", "process", "generation", "teardown"])
async def test_prepare_resident_thread_inventory_rechecks_after_later_await(
    resident_reader, change
):
    manager, entry, _owner, app, requests = resident_reader
    selected = (await manager.resident_inventory()).owners[0]
    reconcile = await manager.prepare_resident_thread_inventory(selected)
    observed = reconcile()
    assert observed is not None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://harness"
    ) as replacement:
        if change == "client":
            entry.client = replacement
        elif change == "process":
            entry.process = _FakeReapProc()
        elif change == "generation":
            manager._release_generations["conversation"] = 1
        else:
            entry.teardown_started = True
        await asyncio.sleep(0)
        assert reconcile() is None
    assert observed.context.release_generation == 0
    assert requests == [("GET", "/v1/sessions/conversation/resident-thread-inventory")]


@pytest.mark.parametrize(
    "failure",
    [
        "legacy-shape",
        "extra-top",
        "alignment",
        "flat",
        "int",
        "bool",
        "empty-id",
        "duplicate-id",
        "too-many",
        "http404",
        "redirect",
        "null-root",
        "unknown-owner",
        "unbound-loaded",
        "unbound-empty",
        "unbound-mixed",
    ],
)
async def test_prepare_resident_thread_inventory_invalid_ids_are_unknown(resident_reader, failure):
    import json
    from dataclasses import asdict

    manager, entry, owner, _app, _requests = resident_reader
    selected = (await manager.resident_inventory()).owners[0]
    payload = {"inventory": asdict(owner.inventory), "thread_ids": [["thread-a"]]}
    item = payload["inventory"]["owners"][0]
    item.update(closing=False, transport_generation=0)
    if failure == "unbound-mixed":
        payload["inventory"]["owners"] += (
            {**item, "incarnation_id": "side", "transport_generation": None},
        )
        payload["thread_ids"] = [None, ["side"]]
        payload["inventory"]["ambiguous_context_ids"] = ["thread-a"]
    elif failure in {"unbound-loaded", "unbound-empty"}:
        item["transport_generation"] = None
        if failure == "unbound-empty":
            payload["thread_ids"] = [[]]
    if failure == "legacy-shape":
        payload.pop("thread_ids")
    elif failure == "extra-top":
        payload["extra"] = True
    elif failure == "null-root":
        payload = None
    elif failure == "unknown-owner":
        payload = {"inventory": None, "thread_ids": None}
    elif failure not in {
        "http404",
        "redirect",
        "unbound-loaded",
        "unbound-empty",
        "unbound-mixed",
    }:
        payload["thread_ids"] = {
            "alignment": [],
            "flat": ["thread-a"],
            "int": [[1]],
            "bool": [[True]],
            "empty-id": [[""]],
            "duplicate-id": [["a", "a"]],
            "too-many": [[f"thread-{i}" for i in range(1001)]],
        }[failure]
    calls = []

    async def respond(request):
        calls.append(request.url.path)
        return httpx.Response(
            404 if failure == "http404" else 307 if failure == "redirect" else 200,
            headers={"Location": "/other"} if failure == "redirect" else {},
            content=json.dumps(payload).encode(),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://harness"
    ) as client:
        entry.client = client
        reconcile = await manager.prepare_resident_thread_inventory(selected)
        assert reconcile() is None
    assert calls == ["/v1/sessions/conversation/resident-thread-inventory"]
    assert entry.last_used_at == 0.0 and not entry.process.killed


async def test_read_resident_inventory_uses_existing_authenticated_owner(resident_reader):
    from dataclasses import asdict

    from omnigent.inner.executor import ExecutorResidentInventory

    manager, entry, owner, _app, requests = resident_reader
    selected = (await manager.resident_inventory()).owners[0]
    result = await manager.read_resident_inventory(selected)
    assert asdict(result) == {
        "owner": {
            "incarnation_id": entry.incarnation_id,
            "session_id": "conversation",
            "harness": "codex",
            "closing": False,
            "wrapper_exited": False,
            "exit_confirmed": False,
        },
        "release_generation": 0,
        "inventory": {
            "owners": (
                {
                    "incarnation_id": "context-a",
                    "session_keys": ("owner-key", "ui-key"),
                    "vendor_context_id": "thread-a",
                    "started": True,
                    "closing": True,
                    "cleanup_complete": False,
                    "transport_generation": None,
                },
            ),
            "ambiguous_context_ids": (),
            "aliases_complete": False,
            "context_coverage_complete": False,
        },
    }
    assert await manager.read_resident_inventory(selected) == result
    with pytest.raises(FrozenInstanceError):
        result.release_generation = 1
    owner.inventory = None
    assert await manager.read_resident_inventory(selected) is None
    owner.inventory = ExecutorResidentInventory(owners=())
    empty = await manager.read_resident_inventory(selected)
    assert empty.inventory.owners == ()
    assert not empty.inventory.aliases_complete and not empty.inventory.context_coverage_complete
    assert requests == [("GET", "/v1/sessions/conversation/resident-inventory")] * 4
    assert manager._retention_managed == {"conversation": 0.0}
    assert manager._in_flight_response_ids == {"conversation": "response-working"}
    assert entry.last_used_at == 0.0
    assert not entry.process.killed


@pytest.mark.parametrize(
    "invalid",
    [
        "absent",
        "incarnation",
        "harness",
        "closing",
        "exited",
        "confirmed",
        "shutdown",
        "closed-client",
    ],
)
@pytest.mark.parametrize("threads", [False, True])
async def test_read_resident_inventory_rejects_invalid_selection_without_http(
    resident_reader, invalid, threads
):
    from dataclasses import replace

    manager, entry, _owner, _app, requests = resident_reader
    selected = (await manager.resident_inventory()).owners[0]
    if invalid == "absent":
        manager._entries.clear()
    elif invalid in {"incarnation", "harness"}:
        selected = replace(
            selected, **{f"{invalid}_id" if invalid == "incarnation" else invalid: "other"}
        )
    elif invalid == "closing":
        selected = replace(selected, closing=True)
    elif invalid == "exited":
        entry.process.returncode = 0
    elif invalid == "confirmed":
        entry.exit_confirmed = True
    elif invalid == "shutdown":
        manager._shutting_down = True
    else:
        await entry.client.aclose()
    assert await _read_selected_inventory(manager, selected, threads) is None
    assert requests == []


@pytest.mark.parametrize("race", ["release", "replacement", "failed-release", "shutdown"])
@pytest.mark.parametrize("threads", [False, True])
async def test_read_resident_inventory_discards_late_response(
    resident_reader, monkeypatch, race, threads
):
    from unittest.mock import AsyncMock

    manager, entry, _owner, _app, _requests = resident_reader
    selected = (await manager.resident_inventory()).owners[0]
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(_response):
        entered.set()
        await asyncio.wait_for(resume.wait(), timeout=2)

    entry.client.event_hooks["response"].append(pause)
    read = asyncio.create_task(_read_selected_inventory(manager, selected, threads))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        # Teardown uses the existing release path and its real _close_entry.
        monkeypatch.setattr(manager, "_retention_expiry", lambda _session: None)
        if race == "failed-release":
            monkeypatch.setattr(entry.client, "aclose", AsyncMock())
            monkeypatch.setattr(entry.process, "terminate", lambda: None)
            monkeypatch.setattr(entry.process, "kill", lambda: None)
            monkeypatch.setattr(
                entry.process, "wait", AsyncMock(side_effect=RuntimeError("still alive"))
            )
            assert await manager.release("conversation") is False
            assert manager._entries["conversation"] is entry
            assert manager._release_generations["conversation"] == 0
            assert not entry.client.is_closed
        elif race == "replacement":
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_app), base_url="http://harness"
            ) as new_client:
                replacement = _SubprocessEntry(
                    _FakeReapProc(), new_client, entry.endpoint, "other"
                )
                monkeypatch.setattr(manager, "_spawn_entry", AsyncMock(return_value=replacement))
                await HarnessProcessManager.get_client(manager, "conversation", "other")
                assert manager._entries["conversation"] is replacement
                resume.set()
                assert await asyncio.wait_for(read, timeout=2) is None
        elif race == "shutdown":
            await manager.shutdown()
        else:
            assert await manager.release("conversation") is True
        resume.set()
        assert await asyncio.wait_for(read, timeout=2) is None
    finally:
        resume.set()
        await _cancel_pending(read)


@pytest.mark.parametrize(
    "failure",
    [
        "auth",
        "session",
        "http500",
        "bad-json",
        "missing",
        "missing-coverage",
        "extra-owner",
        "bool-string",
        "complete-aliases",
        "complete-contexts",
        "duplicate-owner",
        "duplicate-session-keys",
        "empty-ambiguity",
        "duplicate-ambiguity",
        "missing-context-ambiguity",
        "partial-context-ambiguity",
        "literal-context-ambiguity",
        "generation-missing",
        "generation-negative",
        "generation-bool",
        "generation-string",
        "generation-float",
        "generation-not-started",
        "generation-closing",
        "generation-cleaned",
        "http-error",
        "timeout",
    ],
)
@pytest.mark.parametrize("threads", [False, True])
async def test_read_resident_inventory_invalid_response_is_unknown(
    resident_reader, failure, threads
):
    import json
    from dataclasses import asdict

    manager, entry, owner, app, _requests = resident_reader
    selected = (await manager.resident_inventory()).owners[0]
    payload = {"inventory": asdict(owner.inventory)}
    if threads:
        payload["thread_ids"] = None

    async def respond(request):
        assert request.method == "GET"
        suffix = "resident-thread-inventory" if threads else "resident-inventory"
        assert request.url.path == f"/v1/sessions/conversation/{suffix}"
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "http-error":
            raise httpx.ConnectError("channel unavailable", request=request)
        if failure == "missing":
            payload.clear()
        elif failure == "missing-coverage":
            payload["inventory"].pop("context_coverage_complete")
        elif failure == "extra-owner":
            payload["inventory"]["owners"][0]["unexpected"] = "field"
        elif failure == "bool-string":
            payload["inventory"]["owners"][0]["started"] = "false"
        elif failure == "complete-aliases":
            payload["inventory"]["aliases_complete"] = True
        elif failure == "complete-contexts":
            payload["inventory"]["context_coverage_complete"] = True
        elif failure == "duplicate-owner":
            payload["inventory"]["owners"] *= 2
            payload["inventory"]["ambiguous_context_ids"] = ["thread-a"]
        elif failure == "duplicate-session-keys":
            payload["inventory"]["owners"][0]["session_keys"] = ["main", "main"]
        elif failure == "empty-ambiguity":
            payload["inventory"]["ambiguous_context_ids"] = [""]
        elif failure == "duplicate-ambiguity":
            payload["inventory"]["ambiguous_context_ids"] = ["thread-a", "thread-a"]
        elif failure.endswith("context-ambiguity"):
            raw = payload["inventory"]
            first = raw["owners"][0]
            if failure == "literal-context-ambiguity":
                first["vendor_context_id"] = " thread-a "
            raw["owners"] += ({**first, "incarnation_id": "context-b"},)
            if failure != "missing-context-ambiguity":
                raw["ambiguous_context_ids"] = ["thread-a"]
            if failure == "partial-context-ambiguity":
                raw["owners"] += tuple(
                    {**first, "incarnation_id": key, "vendor_context_id": "thread-b"}
                    for key in ("context-c", "context-d")
                )
        elif failure.startswith("generation-"):
            item = payload["inventory"]["owners"][0]
            item.update(closing=False, transport_generation=0)
            if failure == "generation-missing":
                item.pop("transport_generation")
            elif failure in {
                "generation-negative",
                "generation-bool",
                "generation-string",
                "generation-float",
            }:
                item["transport_generation"] = {
                    "generation-negative": -1,
                    "generation-bool": True,
                    "generation-string": "0",
                    "generation-float": 0.0,
                }[failure]
            else:
                item[
                    {
                        "generation-not-started": "started",
                        "generation-closing": "closing",
                        "generation-cleaned": "cleanup_complete",
                    }[failure]
                ] = failure != "generation-not-started"
        return httpx.Response(
            500 if failure == "http500" else 200,
            content=b"not-json" if failure == "bad-json" else json.dumps(payload).encode(),
        )

    if failure == "auth":
        entry.client.headers.clear()
        assert await _read_selected_inventory(manager, selected, threads) is None
    elif failure == "session":
        app.state.conversation_id = "different-conversation"
        assert await _read_selected_inventory(manager, selected, threads) is None
    else:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="http://harness"
        ) as client:
            entry.client = client
            assert (
                await _read_selected_inventory(manager, selected, threads, timeout_s=0.01) is None
            )
    assert manager._retention_managed == {"conversation": 0.0}
    assert entry.last_used_at == 0.0
    assert not entry.process.killed


@pytest.mark.parametrize("threads", [False, True])
async def test_read_resident_inventory_cancellation_propagates(resident_reader, threads):
    manager, entry, _owner, _app, _requests = resident_reader
    selected = (await manager.resident_inventory()).owners[0]
    entered = asyncio.Event()

    async def pause(_response):
        entered.set()
        await asyncio.Event().wait()

    entry.client.event_hooks["response"].append(pause)
    read = asyncio.create_task(_read_selected_inventory(manager, selected, threads))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(read, timeout=2)
        assert not entry.client.is_closed
        assert not entry.process.killed
    finally:
        await _cancel_pending(read)
