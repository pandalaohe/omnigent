"""Unit tests for the host resource sampler."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from omnigent.host import resource_sampler as sampler_mod
from omnigent.host.frames import ResourceProcessRow
from omnigent.host.resource_sampler import (
    ResourceSampler,
    _apply_payload_cap,
    child_pids,
)
from omnigent.runner.owner_file import write_owner_entry

_PARENT_WITH_CHILD_CODE = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _spawn_runner_with_child() -> tuple[subprocess.Popen[bytes], int]:
    """Start a fake runner that spawns one child and prints its pid."""
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT_WITH_CHILD_CODE],
        stdout=subprocess.PIPE,
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline().strip())
    return parent, child_pid


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """Stop a test child, escalating to a kill if it does not exit."""
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill a process and all of its descendants (deepest first)."""
    try:
        for child in psutil.Process(proc.pid).children(recursive=True):
            with contextlib.suppress(psutil.Error):
                child.kill()
    except psutil.Error:
        pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


def test_runner_descendants_inherit_the_runner_session(tmp_path: Path) -> None:
    """A runner's child is a ``child`` row attributed to the runner's session."""
    parent, child_pid = _spawn_runner_with_child()
    try:
        sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())
        frame = sampler.sample(
            runner_sessions={parent.pid: "conv_r"},
            zygote_pid=None,
            interval_s=60,
        )
    finally:
        _kill_tree(parent)

    by_pid = {row.pid: row for row in frame.processes}
    assert by_pid[parent.pid].role == "runner"
    assert by_pid[parent.pid].session_id == "conv_r"
    assert by_pid[child_pid].role == "child"
    assert by_pid[child_pid].session_id == "conv_r"


def test_owner_file_root_wins_over_inherited_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness recorded in an owner file keeps its conversation, not None."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    parent, harness_pid = _spawn_runner_with_child()
    try:
        write_owner_entry(pid=harness_pid, conversation_id="conv_h", kind="harness")
        sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())
        # The runner has no primary session, so without the owner record the
        # harness would inherit None.
        frame = sampler.sample(
            runner_sessions={parent.pid: None},
            zygote_pid=None,
            interval_s=60,
        )
    finally:
        _kill_tree(parent)

    by_pid = {row.pid: row for row in frame.processes}
    assert by_pid[harness_pid].role == "harness"
    assert by_pid[harness_pid].session_id == "conv_h"


def test_child_pids_lists_a_known_child() -> None:
    """The native child list (or its documented absence) matches the platform."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        found = child_pids(os.getpid())
    finally:
        _terminate(child)

    if sys.platform.startswith("linux") or sys.platform == "darwin":
        assert found is not None
        assert child.pid in found
    else:
        assert found is None


def test_pid_cache_drops_vanished_pids(tmp_path: Path) -> None:
    """CPU baselines are dropped for pids that did not answer a later sample."""
    parent, child_pid = _spawn_runner_with_child()
    sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())
    try:
        sampler.sample(
            runner_sessions={parent.pid: "conv_r"},
            zygote_pid=None,
            interval_s=60,
        )
        assert any(pid == child_pid for pid, _create_time in sampler._proc_cache)

        _kill_tree(parent)
        sampler.sample(runner_sessions={}, zygote_pid=None, interval_s=60)
    finally:
        if parent.poll() is None:
            _kill_tree(parent)

    assert not any(pid == child_pid for pid, _create_time in sampler._proc_cache)


def test_fallback_tree_used_when_child_pids_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a native child list the psutil tree is walked once per 60 s."""
    monkeypatch.setattr(sampler_mod, "child_pids", lambda pid: None)
    table_walks: list[bool] = []

    class _ProcProxy:
        def __init__(self, process: psutil.Process) -> None:
            self._process = process

        def children(self, recursive: bool = False) -> list[psutil.Process]:
            table_walks.append(recursive)
            return self._process.children(recursive=recursive)

        def __getattr__(self, name: str) -> object:
            return getattr(self._process, name)

    class _PsutilProxy:
        def Process(self, *args: object, **kwargs: object) -> _ProcProxy:
            return _ProcProxy(psutil.Process(*args, **kwargs))

        def __getattr__(self, name: str) -> object:
            return getattr(psutil, name)

    monkeypatch.setattr(sampler_mod, "psutil", _PsutilProxy())

    clock = SimpleNamespace(value=1000.0)

    class _TimeProxy:
        def monotonic(self) -> float:
            return clock.value

        def __getattr__(self, name: str) -> object:
            return getattr(time, name)

    monkeypatch.setattr(sampler_mod, "time", _TimeProxy())

    parent, child_pid = _spawn_runner_with_child()
    sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())
    try:
        first = sampler.sample(
            runner_sessions={parent.pid: None},
            zygote_pid=None,
            interval_s=60,
        )
        assert len(table_walks) == 1

        clock.value += 1.0
        sampler.sample(runner_sessions={parent.pid: None}, zygote_pid=None, interval_s=60)
        assert len(table_walks) == 1, "the tree must be reused inside 60 s"

        clock.value += 61.0
        sampler.sample(runner_sessions={parent.pid: None}, zygote_pid=None, interval_s=60)
        assert len(table_walks) == 2
    finally:
        _kill_tree(parent)

    by_pid = {row.pid: row for row in first.processes}
    assert by_pid[child_pid].role == "child"


def test_sample_skips_a_failing_pid(tmp_path: Path) -> None:
    """A missing runner / zygote pid is skipped, not raised."""
    sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())

    frame = sampler.sample(
        runner_sessions={1_073_741_824: None},
        zygote_pid=1_073_741_825,
        interval_s=60,
    )

    assert all(row.pid not in (1_073_741_824, 1_073_741_825) for row in frame.processes)


def _row(
    pid: int,
    ppid: int,
    cpu_pct: float,
    *,
    rss: int = 0,
    role: str = "child",
    session_id: str | None = None,
) -> ResourceProcessRow:
    return ResourceProcessRow(
        pid=pid,
        ppid=ppid,
        name=f"p{pid}",
        role=role,
        session_id=session_id,
        cpu_pct=cpu_pct,
        rss=rss,
    )


def test_payload_cap_keeps_ancestors_of_kept_rows() -> None:
    """A hot descendant pulls its quiet ancestors into the capped payload."""
    rows = [_row(1, 0, 0.0, role="daemon")]
    rows.append(_row(2, 1, 0.01))
    rows.append(_row(3, 2, 99.0))
    rows.extend(_row(pid, 1, 1.0) for pid in range(100, 310))

    capped = _apply_payload_cap(rows, root_pids={1})

    kept_pids = {row.pid for row in capped}
    assert 2 in kept_pids
    assert 3 in kept_pids
    assert len([row for row in capped if row.role != "folded"]) == 200


def test_payload_cap_folds_remaining_rows_under_nearest_kept_parent() -> None:
    """One folded row per kept parent sums the dropped descendants."""
    rows = [_row(1, 0, 0.0, role="daemon")]
    rows.extend(_row(pid, 1, float(pid), rss=pid) for pid in range(2, 205))

    capped = _apply_payload_cap(rows, root_pids={1})

    folded = [row for row in capped if row.role == "folded"]
    assert len(folded) == 1
    (summary,) = folded
    assert summary.pid == 0
    assert summary.ppid == 1
    assert summary.name == "其他 4 个进程"
    assert summary.session_id is None
    assert summary.cpu_pct == 14.0
    assert summary.rss == 14
