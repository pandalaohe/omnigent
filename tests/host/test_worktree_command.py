"""Tests for the configured external worktree command.

Every case drives a real child process (the fake script in this directory),
so the assertions cover what the command actually receives and returns.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from omnigent.host import worktree_command
from omnigent.host.worktree_command import (
    WorktreeCommandError,
    load_worktree_command,
    run_worktree_command,
)

_FAKE = Path(__file__).resolve().parent / "_fake_worktree_command.py"

# Deterministic identity so the tests don't depend on the developer's
# global git config (user.name / init.defaultBranch).
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> None:
    """Run git in ``repo``, raising on failure."""
    subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        check=True,
        capture_output=True,
    )


def _fake_command(record: Path, behave: str = "ok") -> list[str]:
    """Build the fake command's argv for one record file and behave mode."""
    return [sys.executable, str(_FAKE), f"--fake-record={record}", f"--fake-behave={behave}"]


def _write_config(tmp_path: Path, command: object) -> Path:
    """Write a host config carrying ``worktree_add_command`` verbatim."""
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"host": {"worktree_add_command": command}}))
    return config


def _records(record: Path) -> list[dict[str, object]]:
    """Parse every JSON record the fake command appended."""
    return [json.loads(line) for line in record.read_text().splitlines()]


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Create a one-commit git repo and return its resolved root."""
    repo = (tmp_path / "myrepo").resolve()
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


# ── loader ────────────────────────────────────────────────


def test_absent_file_or_key_is_none(tmp_path: Path) -> None:
    """A missing config file or a config without the key means not configured."""
    assert load_worktree_command(tmp_path / "missing.yaml") is None
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"host": {"host_id": "a" * 32}}))
    assert load_worktree_command(config) is None


def test_valid_list_is_returned(tmp_path: Path) -> None:
    """A non-empty absolute argv list is returned as-is."""
    command = _fake_command(tmp_path / "record.jsonl")
    assert load_worktree_command(_write_config(tmp_path, command)) == command


@pytest.mark.parametrize(
    "raw",
    [
        "collab worktree add",
        [],
        ["/opt/tools/wt", 1],
        ["./relative-tool"],
        ["/opt/tools/wt.cmd"],
    ],
)
def test_malformed_item_is_config_invalid(tmp_path: Path, raw: object) -> None:
    """A string, an empty/non-str list, a relative exe or a .cmd is refused."""
    with pytest.raises(WorktreeCommandError) as exc:
        load_worktree_command(_write_config(tmp_path, raw))
    assert exc.value.code == "config_invalid"
    assert "host.worktree_add_command" in exc.value.message


def test_unreadable_yaml_is_config_invalid(tmp_path: Path) -> None:
    """A config file that does not parse is refused, not read as absent."""
    config = tmp_path / "config.yaml"
    config.write_text("host: [unclosed\n")
    with pytest.raises(WorktreeCommandError) as exc:
        load_worktree_command(config)
    assert exc.value.code == "config_invalid"
    assert "host.worktree_add_command" in exc.value.message


# ── failure classes ───────────────────────────────────────


def _run(
    command: list[str],
    *,
    source: Path,
    topic: str = "t",
    entry: str | None = None,
    mode: list[str] | None = None,
) -> str:
    """Run the command with a single-branch mode by default."""
    return run_worktree_command(
        command,
        source=str(source),
        topic=topic,
        entry=entry,
        mode=mode if mode is not None else [f"--new-branch={topic}"],
    )


def test_missing_executable_is_unavailable(tmp_path: Path) -> None:
    """An absolute argv[0] that cannot start reports the OS error."""
    with pytest.raises(WorktreeCommandError) as exc:
        _run(["/nonexistent/kit"], source=tmp_path)
    assert exc.value.code == "unavailable"
    assert exc.value.message.startswith("worktree command could not start:")


def test_timeout_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sleeping command is killed at the wall-clock bound."""
    monkeypatch.setattr(worktree_command, "WORKTREE_COMMAND_TIMEOUT_S", 0.5)
    record = tmp_path / "record.jsonl"
    started = time.monotonic()
    with pytest.raises(WorktreeCommandError) as exc:
        _run(_fake_command(record, "sleep"), source=tmp_path)
    elapsed = time.monotonic() - started
    assert exc.value.code == "timeout"
    assert "timed out" in exc.value.message
    assert elapsed < 5


def test_refusal_carries_code_and_detail(tmp_path: Path) -> None:
    """A non-zero exit with an envelope keeps its code and detail."""
    record = tmp_path / "record.jsonl"
    with pytest.raises(WorktreeCommandError) as exc:
        _run(_fake_command(record, "refuse:EXISTS"), source=tmp_path)
    assert exc.value.code == "EXISTS"
    assert exc.value.detail == {"source": str(tmp_path)}
    assert exc.value.message.startswith("worktree command refused (EXISTS): ")


def test_nonzero_without_envelope_reports_stderr(tmp_path: Path) -> None:
    """A non-zero exit with no envelope reports the exit code and stderr tail."""
    record = tmp_path / "record.jsonl"
    with pytest.raises(WorktreeCommandError) as exc:
        _run(_fake_command(record, "exit1"), source=tmp_path)
    assert exc.value.code == "failed"
    assert exc.value.message == "worktree command exited 1: fake worktree command exploded"


def test_exit_zero_garbage_is_malformed(tmp_path: Path) -> None:
    """Exit 0 with non-JSON stdout yields no usable path."""
    record = tmp_path / "record.jsonl"
    with pytest.raises(WorktreeCommandError) as exc:
        _run(_fake_command(record, "garbage"), source=tmp_path)
    assert exc.value.code == "malformed"
    assert "no usable path" in exc.value.message


def test_success_envelope_with_missing_path_is_malformed(tmp_path: Path) -> None:
    """A success envelope whose path does not exist is refused."""
    record = tmp_path / "record.jsonl"
    with pytest.raises(WorktreeCommandError) as exc:
        _run(_fake_command(record, "nopath"), source=tmp_path)
    assert exc.value.code == "malformed"
    assert "is not a directory" in exc.value.message


def test_path_outside_entry_is_malformed(tmp_path: Path) -> None:
    """A success path outside the entry is refused."""
    source = tmp_path / "source"
    source.mkdir()
    entry = tmp_path / "entry"
    entry.mkdir()
    record = tmp_path / "record.jsonl"
    with pytest.raises(WorktreeCommandError) as exc:
        _run(_fake_command(record, "outside"), source=source, entry=str(entry))
    assert exc.value.code == "malformed"
    assert "outside the entry" in exc.value.message
    assert str(source) in exc.value.message


def test_success_returns_the_reported_path(git_repo: Path, tmp_path: Path) -> None:
    """A success envelope's path is returned and the branch is checked out."""
    record = tmp_path / "record.jsonl"
    path = _run(
        _fake_command(record),
        source=git_repo,
        topic="feature/login",
        mode=["--new-branch=feature/login"],
    )
    expected = git_repo / ".worktrees" / "myrepo" / "feature-login"
    assert path == str(expected)
    assert Path(path).is_dir()
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch == "feature/login"


# ── invocation shape ──────────────────────────────────────


@pytest.mark.parametrize(
    ("with_entry", "mode", "expected"),
    [
        (False, ["--new-branch=t/x"], ["--source={source}", "--topic=t/x", "--new-branch=t/x"]),
        (
            False,
            ["--new-branch=t/x", "--base=main"],
            ["--source={source}", "--topic=t/x", "--new-branch=t/x", "--base=main"],
        ),
        (False, ["--branch=t/x"], ["--source={source}", "--topic=t/x", "--branch=t/x"]),
        (
            False,
            ["--detach=" + "a" * 40],
            ["--source={source}", "--topic=t/x", "--detach=" + "a" * 40],
        ),
        (
            True,
            ["--new-branch=t/x"],
            ["--source={source}", "--topic=t/x", "--entry={entry}", "--new-branch=t/x"],
        ),
    ],
)
def test_invocation_argv_shape(
    tmp_path: Path, with_entry: bool, mode: list[str], expected: list[str]
) -> None:
    """Every flag is sent in the ``--flag=value`` form, in contract order."""
    source = tmp_path / "source"
    source.mkdir()
    entry = tmp_path / "entry"
    entry.mkdir()
    record = tmp_path / "record.jsonl"
    with pytest.raises(WorktreeCommandError):
        _run(
            _fake_command(record, "refuse:GIT_REFUSED"),
            source=source,
            topic="t/x",
            entry=str(entry) if with_entry else None,
            mode=mode,
        )
    assert _records(record)[-1]["argv"] == [
        item.format(source=source, entry=entry) for item in expected
    ]


def test_host_token_is_absent_from_the_child_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No host-token spelling reaches the command's environment."""
    record = tmp_path / "record.jsonl"
    monkeypatch.setenv("OMNIGENT_HOST_TOKEN", "host-secret-token")
    monkeypatch.setenv("OMNIGENTS_HOST_TOKEN", "legacy-token")
    monkeypatch.setenv("OMNIAGENTS_HOST_TOKEN", "older-legacy-token")
    with pytest.raises(WorktreeCommandError):
        _run(_fake_command(record, "refuse:EXISTS"), source=tmp_path)
    assert _records(record)[-1]["host_token_present"] is False
