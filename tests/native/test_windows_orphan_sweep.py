"""Windows orphan recovery uses recorded ownership, never a bare PID."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.harnesses.codex_native import process_registry as registry
from omnigent.native import owner_claim
from omnigent.runtime.harnesses import process_manager as manager


@pytest.fixture
def windows_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner_claim, "IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(owner_claim, "IS_LINUX", False)
    monkeypatch.setattr(owner_claim, "_windows_boot_id", lambda: "boot-a", raising=False)


def test_windows_claim_requires_machine_and_boot(
    windows_identity: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner_claim.write_owner_claim(tmp_path)
    claim = owner_claim.read_owner_claim(tmp_path)
    assert claim is not None
    assert claim.pid_ns == "windows:boot-a"
    assert claim.boot_id == "boot-a"
    alive = Mock(return_value=False)
    assert owner_claim.owner_is_gone(claim, process_alive=alive)
    monkeypatch.setattr(owner_claim, "_windows_boot_id", lambda: "boot-b")
    assert not owner_claim.owner_is_gone(claim, process_alive=alive)
    monkeypatch.setattr(owner_claim, "_windows_boot_id", lambda: None)
    assert not owner_claim.owner_is_gone(claim, process_alive=alive)


def test_windows_codex_owner_lock_records_claim(
    windows_identity: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(registry, "IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(registry, "fcntl", None)
    monkeypatch.setattr(registry, "_codex_native_state_root", lambda: tmp_path)
    lock = registry.acquire_codex_native_process_owner_lock()
    assert lock is not None
    claim = owner_claim.read_owner_claim(lock.path.parent, filename=lock.path.name + ".owner")
    assert claim is not None
    assert registry._owner_lock_held(str(lock.path))
    lock.close()


@pytest.mark.parametrize("ownership", ["dead", "live", "missing", "foreign", "bare"])
async def test_windows_harness_sweep_requires_qualified_dead_owner(
    windows_identity: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ownership: str
) -> None:
    monkeypatch.setattr(manager, "IS_WINDOWS", True)
    instance = tmp_path / "ap-orphan"
    instance.mkdir()
    if ownership != "missing":
        owner_claim.write_owner_claim(instance, filename="AP_PID")
        marker = instance / "AP_PID"
        raw = marker.read_text().replace(str(owner_claim.os.getpid()), "4242", 1)
        if ownership == "foreign":
            raw = raw.replace("boot-a", "boot-b")
        if ownership == "bare":
            raw = "4242"
        marker.write_text(raw)
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: ownership == "live")
    kill = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "_kill_orphan_runners", kill)
    await manager.sweep_orphaned_harness_processes(tmp_parent=tmp_path)
    assert kill.call_count == (1 if ownership == "dead" else 0)
    assert instance.exists() is (ownership != "dead")


@pytest.mark.parametrize("complete", [True, False])
async def test_windows_tcp_runner_uses_birth_identity_and_preserves_failed_reap(
    windows_identity: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, complete: bool
) -> None:
    from omnigent.native import windows_processes

    monkeypatch.setattr(manager, "IS_WINDOWS", True)
    instance = tmp_path / "ap-orphan"
    instance.mkdir()
    owner_claim.write_owner_claim(instance, filename="AP_PID")
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: False)
    (instance / "conv-example.process.json").write_text(
        json.dumps({"pid": 6000, "create_time": 123.5})
    )
    reap = Mock(return_value=complete)
    monkeypatch.setattr(windows_processes, "reap_tree", reap)
    await manager.sweep_orphaned_harness_processes(tmp_parent=tmp_path)
    reap.assert_called_once_with(6000, 123.5, record_path=instance / "conv-example.process.json")
    assert instance.exists() is (not complete)


@pytest.mark.parametrize("ownership", ["dead", "live", "missing", "foreign"])
def test_windows_codex_registry_reaps_only_claimed_dead_owner(
    windows_identity: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ownership: str
) -> None:
    from omnigent.native import windows_processes

    monkeypatch.setattr(registry, "IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 6000 or ownership == "live")
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: "windows:123.5")
    monkeypatch.setattr(registry, "_owner_lock_held", lambda _path: False)
    lock = tmp_path / "owner.lock"
    if ownership != "missing":
        owner_claim.write_owner_claim(tmp_path, filename="owner.lock.owner")
        if ownership == "foreign":
            marker = tmp_path / "owner.lock.owner"
            marker.write_text(marker.read_text().replace("boot-a", "boot-b"))
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=6000, pgid=6000, session_tag="tag-a", owner_lock_path=lock, registry_path=path
    )
    reap = Mock(return_value=True)
    monkeypatch.setattr(windows_processes, "reap_tree", reap)
    assert registry.reconcile_codex_native_process_registry(registry_path=path) == (
        1 if ownership == "dead" else 0
    )
    assert reap.call_count == (1 if ownership == "dead" else 0)


def test_windows_tree_never_adopts_reused_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.native import windows_processes

    root = SimpleNamespace(create_time=lambda: 999.0, children=Mock(), terminate=Mock())
    monkeypatch.setattr(windows_processes.psutil, "Process", lambda _pid: root)
    assert not windows_processes.reap_tree(6000, 123.5)
    root.children.assert_not_called()
    root.terminate.assert_not_called()


def test_windows_terminal_sweep_does_not_probe_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner import terminal

    monkeypatch.setattr(terminal, "IS_WINDOWS", True)
    tmux = Mock(side_effect=AssertionError("Windows has no native tmux terminals"))
    monkeypatch.setattr(terminal, "_tmux_available", tmux)
    assert terminal.reap_orphaned_terminals() == 0
    tmux.assert_not_called()


@pytest.mark.parametrize("denied", [False, True])
def test_windows_tree_reaps_descendants_and_escalates_only_survivors(
    monkeypatch: pytest.MonkeyPatch, denied: bool
) -> None:
    from omnigent.native import windows_processes

    root = Mock(pid=6000)
    child = Mock(pid=6001)
    root.create_time.return_value = 123.5
    root.is_running.return_value = True
    root.children.return_value = [child]
    child.create_time.return_value = 123.6
    if denied:
        root.children.side_effect = windows_processes.psutil.AccessDenied(6000)
    monkeypatch.setattr(
        windows_processes.psutil, "Process", lambda pid: root if pid == 6000 else child
    )
    wait = Mock(side_effect=[([root], [child]), ([child], [])])
    monkeypatch.setattr(windows_processes.psutil, "wait_procs", wait)
    assert windows_processes.reap_tree(6000, 123.5) is (not denied)
    if denied:
        root.terminate.assert_not_called()
        child.terminate.assert_not_called()
    else:
        root.terminate.assert_called_once()
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        root.kill.assert_not_called()


def test_windows_boot_guid_uses_native_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    def query(kind: int, buffer: object, length: int, returned: object) -> int:
        assert kind == 90
        assert length == 32
        ctypes.memmove(buffer, bytes(range(16)), 16)
        return 0

    call = Mock(side_effect=query)
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda _name: SimpleNamespace(NtQuerySystemInformation=call),
        raising=False,
    )
    assert owner_claim._windows_boot_id() == "000102030405060708090a0b0c0d0e0f"
    call.side_effect = lambda *args: -1
    assert owner_claim._windows_boot_id() is None


async def test_windows_spawn_records_identity_until_confirmed_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.native import windows_processes

    monkeypatch.setattr(manager, "IS_WINDOWS", True)
    monkeypatch.setattr(windows_processes, "process_birth", lambda _pid: 123.5)
    monkeypatch.setattr(manager, "_wait_for_bind", AsyncMock())
    monkeypatch.setattr(manager, "write_owner_entry", Mock())
    monkeypatch.setattr(manager, "_pick_free_tcp_port", lambda: 12345)
    process = SimpleNamespace(pid=6000, returncode=0)
    pm = manager.HarnessProcessManager(tmp_parent=tmp_path)
    await pm.start(sweep_orphans=False)
    monkeypatch.setattr(pm, "_spawn_harness_process", AsyncMock(return_value=process))
    try:
        entry = await pm._spawn_entry("example", "claude-sdk", None)
        spawn_argv = pm._spawn_harness_process.call_args.args[0]
        record = entry.process_record_path
        assert record is not None
        assert spawn_argv[-2:] == ["--process-record", str(record)]
        assert json.loads(record.read_text()) == {"pid": 6000, "create_time": 123.5}
        assert await pm._close_entry(entry)
        assert not record.exists()
    finally:
        await pm.shutdown()


def test_windows_reap_retries_recorded_child_after_root_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.native import windows_processes

    root, child = Mock(pid=6000), Mock(pid=6001)
    root.create_time.return_value = 123.5
    root.is_running.return_value = True
    root.children.return_value = [child]
    child.create_time.return_value = 123.6
    child.kill.side_effect = windows_processes.psutil.AccessDenied(6001)
    live = {6000: root, 6001: child}

    def process(pid: int) -> Mock:
        if pid not in live:
            raise windows_processes.psutil.NoSuchProcess(pid)
        return live[pid]

    def wait(processes: list, timeout: float) -> tuple[list, list]:
        live.pop(6000, None)
        return [root], [child]

    monkeypatch.setattr(windows_processes.psutil, "Process", process)
    monkeypatch.setattr(windows_processes.psutil, "wait_procs", wait)
    record = tmp_path / "conv-example.process.json"
    record.write_text(json.dumps({"pid": 6000, "create_time": 123.5}))
    assert not windows_processes.reap_tree(6000, 123.5, record_path=record)
    assert {item["pid"] for item in json.loads(record.read_text())["members"]} == {6000, 6001}
    child.kill.side_effect = None
    monkeypatch.setattr(
        windows_processes.psutil, "wait_procs", Mock(side_effect=[([], [child]), ([child], [])])
    )
    assert windows_processes.reap_tree(6000, 123.5, record_path=record)
    assert child.kill.call_count == 2
    assert root.terminate.call_count == 1


def test_windows_codex_scan_reaps_known_survivor_despite_protected_process(
    windows_identity: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.native import windows_processes

    monkeypatch.setattr(registry, "IS_WINDOWS", True)
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: False)
    owner_claim.write_owner_claim(tmp_path, filename="owner.lock.owner")
    current, protected, survivor = Mock(), Mock(), Mock(pid=6001)
    current.username.return_value = "user-a"
    protected.username.side_effect = windows_processes.psutil.AccessDenied(4)
    survivor.username.return_value = "user-a"
    survivor.cmdline.return_value = [
        "codex.exe",
        registry.codex_native_session_tag_cmdline_arg("tag-a"),
    ]
    survivor.create_time.return_value = 123.6
    monkeypatch.setattr(windows_processes.psutil, "Process", lambda: current)
    monkeypatch.setattr(
        windows_processes.psutil, "process_iter", lambda: iter([protected, survivor])
    )
    reap = Mock(return_value=True)
    monkeypatch.setattr(windows_processes, "reap_tree", reap)
    entry = registry.CodexNativeProcessEntry(
        6000, 6000, None, "tag-a", "windows:123.5", str(tmp_path / "owner.lock")
    )
    assert registry._reap_windows_entry(entry) is None
    reap.assert_called_once()


@pytest.mark.parametrize(
    "raw", ["{", "[]", '{"pid": true, "create_time": 1}', '{"pid": 6000, "create_time": NaN}']
)
def test_windows_invalid_record_never_signals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    from omnigent.native import windows_processes

    record = tmp_path / "conv-example.process.json"
    record.write_text(raw)
    process = Mock(side_effect=AssertionError("invalid record must not select a process"))
    monkeypatch.setattr(windows_processes.psutil, "Process", process)
    assert not windows_processes.reap_record(record)
    process.assert_not_called()


def test_windows_root_gone_before_first_census_keeps_unclaimed_children(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.native import windows_processes

    record = tmp_path / "conv-example.process.json"
    original = json.dumps({"pid": 6000, "create_time": 123.5})
    record.write_text(original)
    monkeypatch.setattr(
        windows_processes.psutil,
        "Process",
        Mock(side_effect=windows_processes.psutil.NoSuchProcess(6000)),
    )
    wait = Mock(side_effect=AssertionError("unclaimed descendants must remain untouched"))
    monkeypatch.setattr(windows_processes.psutil, "wait_procs", wait)
    assert not windows_processes.reap_record(record)
    assert record.read_text() == original
    wait.assert_not_called()


@pytest.mark.parametrize("failure", ["read", "json"])
@pytest.mark.parametrize("operation", ["close", "register", "unregister", "reconcile"])
def test_windows_owner_close_retains_retry_evidence_when_registry_is_unknown(
    windows_identity: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
    operation: str,
) -> None:
    monkeypatch.setattr(registry, "IS_WINDOWS", True)
    monkeypatch.setattr(registry, "fcntl", None)
    monkeypatch.setattr(registry, "_codex_native_state_root", lambda: tmp_path)
    lock = registry.acquire_codex_native_process_owner_lock()
    assert lock is not None
    marker = lock.path.with_name(lock.path.name + ".owner")
    census = lock.path.with_name(lock.path.name + ".6000.tree.json")
    census.write_text('{"pid": 6000, "create_time": 123.5}')
    path = registry.codex_native_process_registry_path()
    path.write_text("{")
    read_text = Path.read_text

    def read(candidate: Path, *args: object, **kwargs: object) -> str:
        if candidate == path and failure == "read":
            raise PermissionError("registry temporarily unavailable")
        return read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: "windows:123.5")
    if operation == "register":
        registry.register_codex_native_process(
            pid=6000, pgid=6000, session_tag="tag-a", owner_lock_path=lock.path
        )
    elif operation == "unregister":
        registry.unregister_codex_native_process("tag-a")
    elif operation == "reconcile":
        assert registry.reconcile_codex_native_process_registry() == 0
    lock.close()
    assert marker.exists()
    assert census.exists()
    assert read_text(path) == "{"
