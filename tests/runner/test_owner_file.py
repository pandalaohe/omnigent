"""Tests for the runner-written owner records read by the host sampler."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from omnigent.runner.owner_file import (
    OwnerEntry,
    read_owner_entries,
    write_owner_entry,
)


def _spawn_sleep() -> subprocess.Popen[bytes]:
    """Start a short-lived child process that stands in for an owned pid."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """Stop a test child, escalating to a kill if it does not exit."""
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def _tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the runner's data dir at a per-test directory."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))


def test_write_two_entries_and_read_back(tmp_path: Path) -> None:
    """Two writes to one runner file accumulate, carrying their kinds."""
    first = _spawn_sleep()
    second = _spawn_sleep()
    try:
        write_owner_entry(pid=first.pid, conversation_id="conv_a", kind="harness")
        write_owner_entry(pid=second.pid, conversation_id="conv_b", kind="tmux")

        entries = read_owner_entries(tmp_path)
    finally:
        _terminate(first)
        _terminate(second)

    assert entries == {
        first.pid: OwnerEntry(pid=first.pid, conversation_id="conv_a", kind="harness"),
        second.pid: OwnerEntry(pid=second.pid, conversation_id="conv_b", kind="tmux"),
    }


def test_dead_entry_dropped_on_next_write(tmp_path: Path) -> None:
    """A vanished pid is pruned by the next write, not read back later."""
    dead = _spawn_sleep()
    write_owner_entry(pid=dead.pid, conversation_id="conv_dead", kind="harness")
    _terminate(dead)

    live = _spawn_sleep()
    try:
        write_owner_entry(pid=live.pid, conversation_id="conv_live", kind="tmux")
        entries = read_owner_entries(tmp_path)
    finally:
        _terminate(live)

    assert entries == {
        live.pid: OwnerEntry(pid=live.pid, conversation_id="conv_live", kind="tmux")
    }


def test_dead_runner_file_is_deleted_on_read(tmp_path: Path) -> None:
    """A file whose runner is gone is removed on read; its entries ignored."""
    runner = _spawn_sleep()
    runner_pid = runner.pid
    runner_create_time = psutil.Process(runner_pid).create_time()
    _terminate(runner)

    owners_dir = tmp_path / "run" / "owners"
    owners_dir.mkdir(parents=True)
    path = owners_dir / f"{runner_pid}.json"
    path.write_text(
        json.dumps(
            {
                "runner_pid": runner_pid,
                "runner_create_time": runner_create_time,
                "entries": [
                    {
                        "pid": os.getpid(),
                        "create_time": psutil.Process().create_time(),
                        "conversation_id": "conv_ghost",
                        "kind": "harness",
                    }
                ],
            }
        )
    )

    assert read_owner_entries(tmp_path) == {}
    assert not path.exists()


def test_entry_with_stale_create_time_is_ignored(tmp_path: Path) -> None:
    """An entry whose create_time no longer matches its pid is dropped."""
    child = _spawn_sleep()
    try:
        owners_dir = tmp_path / "run" / "owners"
        owners_dir.mkdir(parents=True)
        (owners_dir / f"{os.getpid()}.json").write_text(
            json.dumps(
                {
                    "runner_pid": os.getpid(),
                    "runner_create_time": psutil.Process().create_time(),
                    "entries": [
                        {
                            "pid": child.pid,
                            "create_time": psutil.Process(child.pid).create_time() + 100.0,
                            "conversation_id": "conv_reused",
                            "kind": "harness",
                        }
                    ],
                }
            )
        )

        assert read_owner_entries(tmp_path) == {}
    finally:
        _terminate(child)


def test_write_for_dead_pid_is_a_silent_noop(tmp_path: Path) -> None:
    """Recording a vanished pid must never raise and must not create a file."""
    child = _spawn_sleep()
    _terminate(child)

    write_owner_entry(pid=child.pid, conversation_id="conv_dead", kind="harness")

    assert read_owner_entries(tmp_path) == {}
    assert not list((tmp_path / "run" / "owners").glob("*.json"))
