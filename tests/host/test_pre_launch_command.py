"""Tests for the host's pre-launch command.

Every case drives a real stub executable (an absolute-path POSIX shell script
written into ``tmp_path``) that records its cwd and ``OMNIGENT_*`` environment,
so the assertions cover the child's actual runtime facts.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest
import yaml

from omnigent.host import pre_launch_command
from omnigent.host.pre_launch_command import run_pre_launch_command

_STUB = """#!/bin/sh
{
  printf 'cwd=%s\\n' "$PWD"
  env | grep -E '^(OMNIGENT|OMNIGENTS|OMNIAGENTS)_HOST_TOKEN' | sort
} >> "$PRE_LAUNCH_STUB_RECORD"
printf 'stub-output'
exit "${PRE_LAUNCH_STUB_EXIT:-0}"
"""

_TIMEOUT_STUB = """#!/bin/sh
( sleep 5 ) &
sleep 5
"""


def _write_stub(path: Path, body: str) -> Path:
    """Write an executable stub script and return its absolute path."""
    path.write_text(body)
    path.chmod(0o755)
    return path


def _write_config(path: Path, **host_keys: object) -> Path:
    """Write a host config whose ``host`` section carries ``host_keys``."""
    host: dict[str, object] = {"host_id": "a" * 32, "name": "test-box", **host_keys}
    path.write_text(yaml.safe_dump({"host": host}))
    return path


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A canonical launch workspace."""
    path = tmp_path / "workspace"
    path.mkdir()
    return path.resolve()


@pytest.fixture()
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The file the stub appends one record to per run."""
    path = tmp_path / "record.txt"
    monkeypatch.setenv("PRE_LAUNCH_STUB_RECORD", str(path))
    return path


def test_runs_once_in_the_workspace_without_the_host_token(
    tmp_path: Path, workspace: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_TOKEN", "host-token")
    monkeypatch.setenv("OMNIGENTS_HOST_TOKEN", "legacy-token")
    stub = _write_stub(tmp_path / "hook.sh", _STUB)
    config = _write_config(tmp_path / "config.yaml", pre_launch_command=[str(stub)])

    run_pre_launch_command(config, workspace)

    assert record.read_text() == f"cwd={workspace}\n"


def test_not_run_without_its_own_key_or_a_config_file(
    tmp_path: Path, workspace: Path, record: Path
) -> None:
    stub = _write_stub(tmp_path / "hook.sh", _STUB)
    config = _write_config(tmp_path / "config.yaml", post_bind_command=[str(stub)])

    run_pre_launch_command(config, workspace)
    run_pre_launch_command(tmp_path / "missing.yaml", workspace)

    assert not record.exists()


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("collab root worktree repair", "must be a non-empty list of strings"),
        (["./hook.sh"], "must start with an absolute path"),
        (["/opt/tools/hook.bat"], "must not be a .bat/.cmd file"),
    ],
)
def test_malformed_key_is_logged_and_nothing_runs(
    tmp_path: Path,
    workspace: Path,
    record: Path,
    caplog: pytest.LogCaptureFixture,
    command: object,
    message: str,
) -> None:
    config = _write_config(tmp_path / "config.yaml", pre_launch_command=command)

    with caplog.at_level(logging.WARNING, logger=pre_launch_command.__name__):
        run_pre_launch_command(config, workspace)

    assert f"host.pre_launch_command {message}" in caplog.text
    assert not record.exists()


def test_failure_is_logged_with_its_output(
    tmp_path: Path,
    workspace: Path,
    record: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("PRE_LAUNCH_STUB_EXIT", "3")
    stub = _write_stub(tmp_path / "hook.sh", _STUB)
    config = _write_config(tmp_path / "config.yaml", pre_launch_command=[str(stub)])

    with caplog.at_level(logging.WARNING, logger=pre_launch_command.__name__):
        run_pre_launch_command(config, workspace)

    assert record.exists()
    assert "Pre-launch command exited 3" in caplog.text
    assert "stub-output" in caplog.text


def test_missing_executable_is_logged(
    tmp_path: Path, workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _write_config(
        tmp_path / "config.yaml", pre_launch_command=[str(tmp_path / "missing.sh")]
    )

    with caplog.at_level(logging.WARNING, logger=pre_launch_command.__name__):
        run_pre_launch_command(config, workspace)

    assert "Pre-launch command failed to start" in caplog.text


def test_timeout_kills_and_returns_without_waiting_for_descendants(
    tmp_path: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(pre_launch_command, "PRE_LAUNCH_TIMEOUT_S", 0.3)
    stub = _write_stub(tmp_path / "hook.sh", _TIMEOUT_STUB)
    config = _write_config(tmp_path / "config.yaml", pre_launch_command=[str(stub)])

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=pre_launch_command.__name__):
        run_pre_launch_command(config, workspace)

    assert time.monotonic() - started < 2.5
    assert "Pre-launch command timed out" in caplog.text
