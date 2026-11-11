"""Kernel guard contention and verified ownership handoffs on disposable files."""

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from omnigent.host import windows_upgrade_lock as locks


def test_competing_stale_takeover_is_serialized(tmp_path):
    path = tmp_path / "upgrade.lock"
    path.write_text(json.dumps({"pid": 12345, "create_time": -1}))
    script = """
import contextlib, sys
from pathlib import Path
from omnigent.host import windows_upgrade_lock as locks
path, role = Path(sys.argv[1]), sys.argv[2]
if role == 'first':
    original = locks._owner_live
    def inspect(owner):
        if owner['create_time'] == -1:
            print('inspected', flush=True)
            sys.stdin.readline()
        return original(owner)
    locks._owner_live = inspect
else:
    original = locks.owner_guard
    @contextlib.contextmanager
    def guard(path):
        print('attempting', flush=True)
        with original(path):
            yield
    locks.owner_guard = guard
try:
    locks.acquire_lock(path)
except RuntimeError:
    print('refused', flush=True)
else:
    print('won', flush=True)
    sys.stdin.readline()
"""
    processes = []
    try:
        first = subprocess.Popen(
            [sys.executable, "-c", script, str(path), "first"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(first)
        assert first.stdin is not None and first.stdout is not None
        assert first.stdout.readline().strip() == "inspected"
        second = subprocess.Popen(
            [sys.executable, "-c", script, str(path), "second"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(second)
        assert second.stdin is not None and second.stdout is not None
        assert second.stdout.readline().strip() == "attempting"
        first.stdin.write("continue\n")
        first.stdin.flush()
        assert first.stdout.readline().strip() == "won"
        assert second.stdout.readline().strip() == "refused"
        assert second.wait(timeout=10) == 0
        assert json.loads(path.read_text())["pid"] == first.pid
        first.stdin.write("exit\n")
        first.stdin.flush()
        assert first.wait(timeout=10) == 0
        # Process exit releases the sidecar's kernel lock; stale retry succeeds.
        owner = locks.acquire_lock(path)
        assert owner["pid"] == os.getpid()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


def test_handoff_transitions_validate_full_identity(monkeypatch, tmp_path):
    path = tmp_path / "upgrade.lock"
    identities = {10: 100.0, 20: 200.0, 30: 300.0}
    pid = [10]
    monkeypatch.setattr(locks.os, "getpid", lambda: pid[0])
    monkeypatch.setattr(
        locks.psutil,
        "Process",
        lambda number: SimpleNamespace(create_time=lambda: identities[number]),
    )
    shim = locks.acquire_lock(path)
    uv = locks.transfer_lock(path, shim, 20)
    assert uv == {"pid": 20, "create_time": 200.0}
    pid[0] = 30
    helper = locks.acquire_lock(path, handoff_from=[20])
    assert helper == {"pid": 30, "create_time": 300.0}
    locks.release_lock(path, shim)
    locks.release_lock(path, uv)
    assert json.loads(path.read_text()) == helper
    locks.release_lock(path, helper)
    assert not path.exists()
    assert path.with_suffix(".lock.guard").exists()


def test_pid_reuse_is_stale_even_for_handoff(monkeypatch, tmp_path):
    path = tmp_path / "upgrade.lock"
    path.write_text(json.dumps({"pid": 12345, "create_time": 1}))
    original = locks.psutil.Process
    monkeypatch.setattr(
        locks.psutil,
        "Process",
        lambda pid: SimpleNamespace(create_time=lambda: 2) if pid == 12345 else original(pid),
    )
    owner = locks.acquire_lock(path, handoff_from=[12345])
    assert owner["pid"] == os.getpid()


def test_live_unrelated_owner_is_not_a_handoff(monkeypatch, tmp_path):
    path = tmp_path / "upgrade.lock"
    owner = locks.acquire_lock(path)
    monkeypatch.setattr(locks.os, "getpid", lambda: 12345)
    with pytest.raises(RuntimeError, match="already running"):
        locks.acquire_lock(path)
    assert json.loads(path.read_text()) == owner


def test_windows_guard_locks_one_byte_and_unlocks_after_error(monkeypatch, tmp_path):
    import importlib

    calls = []
    module = SimpleNamespace(LK_LOCK=1, LK_UNLCK=0)

    def locking(descriptor, mode, count):
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
        assert os.fstat(descriptor).st_size >= 1
        calls.append((mode, count))

    module.locking = locking
    original = importlib.import_module
    monkeypatch.setattr(locks, "IS_WINDOWS", True)
    monkeypatch.setattr(
        importlib, "import_module", lambda name: module if name == "msvcrt" else original(name)
    )
    path = tmp_path / "upgrade.lock"
    with pytest.raises(RuntimeError, match="body failed"):
        with locks.owner_guard(path):
            raise RuntimeError("body failed")
    with locks.owner_guard(path):
        pass
    assert calls == [(1, 1), (0, 1), (1, 1), (0, 1)]


def test_transfer_refuses_changed_owner(monkeypatch, tmp_path):
    path = tmp_path / "upgrade.lock"
    original = locks.acquire_lock(path)
    changed = {**original, "create_time": original["create_time"] + 1}
    path.write_text(json.dumps(changed))
    with pytest.raises(RuntimeError, match="owner changed"):
        locks.transfer_lock(path, original, os.getpid())
    assert json.loads(path.read_text()) == changed
