"""Unit tests for the host resource sampler."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from omnigent.host import resource_sampler as sampler_mod
from omnigent.host.frames import ResourceProcessRow
from omnigent.host.load_level import FACTORY_LINES, LevelState
from omnigent.host.resource_sampler import (
    ResourceSampler,
    _apply_payload_cap,
    _cpu_busy_total,
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
    parent_create_time = psutil.Process(parent.pid).create_time()
    child_create_time = psutil.Process(child_pid).create_time()
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
    assert by_pid[parent.pid].started_at == parent_create_time
    assert by_pid[child_pid].started_at == child_create_time


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


def test_fallback_edge_with_a_reused_pid_is_not_attributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached child whose real ppid moved elsewhere is skipped entirely."""
    monkeypatch.setattr(sampler_mod, "child_pids", lambda pid: None)
    parent, _child_pid = _spawn_runner_with_child()
    other_parent, other_child_pid = _spawn_runner_with_child()
    sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())
    monkeypatch.setattr(
        sampler,
        "_fallback_children_map",
        lambda: {parent.pid: [other_child_pid]},
    )
    queried: list[int] = []
    children_of = sampler._children_of

    def _spy(pid: int) -> list[int]:
        queried.append(pid)
        return children_of(pid)

    monkeypatch.setattr(sampler, "_children_of", _spy)
    try:
        frame = sampler.sample(
            runner_sessions={parent.pid: "conv_r"},
            zygote_pid=None,
            interval_s=60,
        )
    finally:
        _kill_tree(parent)
        _kill_tree(other_parent)

    by_pid = {row.pid: row for row in frame.processes}
    assert parent.pid in by_pid
    assert other_child_pid not in by_pid
    assert other_child_pid not in queried, "a mismatched child must not be walked"


def test_sample_skips_a_failing_pid(tmp_path: Path) -> None:
    """A missing runner / zygote pid is skipped, not raised."""
    sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())

    frame = sampler.sample(
        runner_sessions={1_073_741_824: None},
        zygote_pid=1_073_741_825,
        interval_s=60,
    )

    assert all(row.pid not in (1_073_741_824, 1_073_741_825) for row in frame.processes)


def test_non_posix_load_waits_for_emulation_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first Windows zeros are unknown; a warmed-up zero is valid."""
    monkeypatch.setattr(sampler_mod, "IS_POSIX", False)
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(
        sampler_mod,
        "time",
        SimpleNamespace(monotonic=lambda: clock.value, time=lambda: clock.value),
    )
    getloadavg = Mock(
        side_effect=[(0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (2.5, 0.0, 0.0), (0.0, 0.0, 0.0)]
    )
    monkeypatch.setattr(sampler_mod.psutil, "getloadavg", getloadavg)
    sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())
    getloadavg.assert_not_called()

    assert sampler._machine_metrics().load1 is None
    getloadavg.assert_called_once_with()
    clock.value = 105.0
    assert sampler._machine_metrics().load1 is None
    clock.value = 106.0
    assert sampler._machine_metrics().load1 == 2.5
    clock.value = 107.0
    assert sampler._machine_metrics().load1 == 0.0


def test_non_posix_load_retries_failed_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a successful emulation start begins the warmup clock."""
    monkeypatch.setattr(sampler_mod, "IS_POSIX", False)
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(
        sampler_mod,
        "time",
        SimpleNamespace(monotonic=lambda: clock.value, time=lambda: clock.value),
    )
    getloadavg = Mock(
        side_effect=[
            OSError("load unavailable"),
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            (3.25, 0.0, 0.0),
        ]
    )
    monkeypatch.setattr(sampler_mod.psutil, "getloadavg", getloadavg)
    sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())

    assert sampler._machine_metrics().load1 is None
    clock.value = 200.0
    assert sampler._machine_metrics().load1 is None
    clock.value = 205.0
    assert sampler._machine_metrics().load1 is None
    clock.value = 206.0
    assert sampler._machine_metrics().load1 == 3.25
    assert getloadavg.call_count == 4


@pytest.mark.parametrize("failure_at", [0, 1])
def test_non_posix_load_failure_preserves_the_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_at: int
) -> None:
    """Startup and later load errors preserve the frame's other metrics."""
    monkeypatch.setattr(sampler_mod, "IS_POSIX", False)
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(
        sampler_mod,
        "time",
        SimpleNamespace(monotonic=lambda: clock.value, time=lambda: clock.value),
    )
    readings: list[object] = [(0.0, 0.0, 0.0), (1.5, 0.0, 0.0), (2.0, 0.0, 0.0)]
    readings[failure_at] = RuntimeError("load unavailable")
    getloadavg = Mock(side_effect=readings)
    monkeypatch.setattr(sampler_mod.psutil, "getloadavg", getloadavg)
    monkeypatch.setattr(
        sampler_mod.psutil, "virtual_memory", lambda: SimpleNamespace(total=800, available=300)
    )
    monkeypatch.setattr(
        sampler_mod.psutil, "disk_usage", lambda path: SimpleNamespace(used=200, total=1000)
    )
    sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())
    monkeypatch.setattr(sampler, "_machine_cpu_pct", lambda: 25.0)

    for index in range(3):
        clock.value = 100.0 + index * 6.0
        frame = sampler.sample(runner_sessions={}, zygote_pid=None, interval_s=60)
        assert frame.machine.cpu_pct == 25.0
        assert (frame.machine.mem_used, frame.machine.mem_total) == (500, 800)
        assert (frame.machine.disk_used, frame.machine.disk_total) == (200, 1000)
        assert any(row.pid == os.getpid() for row in frame.processes)
        if index == failure_at:
            assert frame.machine.load1 is None
        if index == 2:
            assert frame.machine.load1 == 2.0
    assert getloadavg.call_count == 3


@pytest.mark.parametrize("load", [1.75, 0.0, None])
def test_posix_load_uses_os_without_emulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, load: float | None
) -> None:
    """POSIX keeps its immediate load value and OSError handling."""
    monkeypatch.setattr(sampler_mod, "IS_POSIX", True)
    getloadavg = (
        Mock(side_effect=OSError("load unavailable"))
        if load is None
        else Mock(return_value=(load, 0.0, 0.0))
    )
    monkeypatch.setattr(sampler_mod.os, "getloadavg", getloadavg, raising=False)
    emulated_load = Mock(side_effect=AssertionError("POSIX must use os.getloadavg"))
    monkeypatch.setattr(sampler_mod.psutil, "getloadavg", emulated_load)
    sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())

    assert sampler._machine_metrics().load1 == load
    getloadavg.assert_called_once_with()
    emulated_load.assert_not_called()


def _row(
    pid: int,
    ppid: int,
    cpu_pct: float,
    *,
    rss: int = 0,
    role: str = "child",
    session_id: str | None = None,
    started_at: float | None = None,
) -> ResourceProcessRow:
    return ResourceProcessRow(
        pid=pid,
        ppid=ppid,
        name=f"p{pid}",
        role=role,
        session_id=session_id,
        cpu_pct=cpu_pct,
        rss=rss,
        started_at=started_at,
    )


def test_cpu_busy_total_does_not_double_count_linux_guest_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux guest / guest_nice are already inside user / nice."""
    cpu_times_type = namedtuple(
        "cpu_times",
        "user nice system idle iowait irq softirq steal guest guest_nice",
    )
    cpu_times = cpu_times_type(10.0, 2.0, 3.0, 50.0, 5.0, 1.0, 2.0, 0.0, 4.0, 1.0)

    monkeypatch.setattr(sampler_mod.sys, "platform", "linux")
    busy, total = _cpu_busy_total(cpu_times)
    assert total == 73.0
    assert busy == 18.0

    monkeypatch.setattr(sampler_mod.sys, "platform", "darwin")
    busy, total = _cpu_busy_total(cpu_times)
    assert total == 78.0
    assert busy == 23.0


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
    rows = [_row(1, 0, 0.0, role="daemon", started_at=123.0)]
    rows.extend(_row(pid, 1, float(pid), rss=pid, started_at=456.0) for pid in range(2, 205))

    capped = _apply_payload_cap(rows, root_pids={1})

    (daemon_row,) = [row for row in capped if row.pid == 1]
    assert daemon_row.started_at == 123.0
    folded = [row for row in capped if row.role == "folded"]
    assert len(folded) == 1
    (summary,) = folded
    assert summary.pid == 0
    assert summary.ppid == 1
    assert summary.name == "4 other processes"
    assert summary.session_id is None
    assert summary.cpu_pct == 14.0
    assert summary.rss == 14
    assert summary.started_at is None


@pytest.mark.parametrize("cadence", [10, 60])
def test_windows_estimate_uses_minute_cpu_mean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cadence: int,
) -> None:
    monkeypatch.setattr(sampler_mod, "IS_POSIX", False)
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(
        sampler_mod,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock.value,
            time=lambda: clock.value,
        ),
    )
    monkeypatch.setattr(sampler_mod.psutil, "getloadavg", lambda: (2.0, 0.0, 0.0))
    monkeypatch.setattr(sampler_mod.psutil, "cpu_count", lambda **kwargs: 24)
    monkeypatch.setattr(
        sampler_mod.psutil, "swap_memory", lambda: SimpleNamespace(used=8, total=16)
    )
    times = namedtuple("CpuTimes", "user system idle")
    monkeypatch.setattr(
        sampler_mod.psutil, "cpu_times", lambda: times(clock.value * 12, 0, clock.value * 12)
    )
    sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())
    assert sampler._machine_metrics().load_per_core is None
    for second in range(cadence, 121, cadence):
        clock.value = float(second)
        machine = sampler._machine_metrics()
        assert machine.cpu_count == 24
        assert machine.load_equiv == 14.0
        assert machine.load_per_core == pytest.approx(14 / 24)
        assert machine.estimate is True
        assert (machine.swap_used, machine.swap_total) == (8, 16)


def test_windows_varying_cpu_is_averaged_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sampler_mod, "IS_POSIX", False)
    clock = SimpleNamespace(value=0.0, busy=0.0, idle=0.0)
    monkeypatch.setattr(
        sampler_mod,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock.value,
            time=lambda: clock.value,
        ),
    )
    monkeypatch.setattr(sampler_mod.psutil, "getloadavg", lambda: (88.8, 0.0, 0.0))
    monkeypatch.setattr(sampler_mod.psutil, "cpu_count", lambda **kwargs: 24)
    times = namedtuple("CpuTimes", "user system idle")
    monkeypatch.setattr(sampler_mod.psutil, "cpu_times", lambda: times(clock.busy, 0, clock.idle))
    sampler = ResourceSampler(data_dir=tmp_path, daemon_pid=os.getpid())
    sampler._machine_metrics()
    state = LevelState()
    for minute in range(3):
        for index, second in enumerate((10, 20, 30, 40, 50, 59)):
            clock.value = float(minute * 60 + second)
            clock.busy += 24 if index == 0 else 0
            clock.idle += 0 if index == 0 else 24
            machine = sampler._machine_metrics()
            state.add_sample(clock.value, sampler.sample_load_per_core, 50)
        # Queue/core 3.7 plus mean CPU share 1/6 stays below the heavy line.
        assert machine.load_per_core == pytest.approx(3.8666666667)
        assert state.step((minute + 1) * 60, FACTORY_LINES) == ("normal", None)
