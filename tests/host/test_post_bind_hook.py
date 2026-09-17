"""Tests for the host-side post-bind hook runner.

Every case drives a real stub executable (an absolute-path POSIX shell script
written into ``tmp_path``) that records its argv, cwd and ``OMNIGENT_*``
environment, so the assertions cover the child's actual runtime facts, not
the runner's intent.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from omnigent.host import post_bind_hook
from omnigent.host.frames import HostPostBindHookFrame
from omnigent.host.post_bind_hook import PostBindHookRunner
from omnigent.project_context import MANIFEST_VERSION

_RECORD_ENV = "POST_BIND_STUB_RECORD"
_EXIT_ENV = "POST_BIND_STUB_EXIT"
_RAN_ENV = "POST_BIND_STUB_RAN"

_STUB = """#!/bin/sh
{
  printf 'cwd=%s\\n' "$PWD"
  printf 'argv0=%s\\n' "$0"
  for arg in "$@"; do printf 'arg=%s\\n' "$arg"; done
  env | grep -E '^(OMNIGENT|OMNIGENTS|OMNIAGENTS)_' | sort
} > "$POST_BIND_STUB_RECORD"
printf 'stub-output'
exit "${POST_BIND_STUB_EXIT:-0}"
"""

_SLEEPING_STUB = """#!/bin/sh
printf 'run\\n' >> "$POST_BIND_STUB_RAN"
sleep 1
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


def _write_config(path: Path, command: object) -> Path:
    """Write a host config with (or without) ``post_bind_command``."""
    host: dict[str, object] = {"host_id": "a" * 32, "name": "test-box"}
    if command is not None:
        host["post_bind_command"] = command
    path.write_text(yaml.safe_dump({"host": host}))
    return path


def _write_manifest(
    workspace: Path,
    *,
    identity_id: object = None,
    raw: str | None = None,
    path: str = ".agents/project/manifest.json",
) -> Path:
    """Write a manifest into the workspace (raw text wins over fields)."""
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        target.write_text(raw)
    else:
        body: dict[str, object] = {"version": MANIFEST_VERSION}
        if identity_id is not None:
            body["identity"] = {"id": identity_id}
        target.write_text(json.dumps(body))
    return target


def _frame(
    workspace: Path,
    *,
    project_id: str = "proj_1",
    binding_name: str = "primary",
    binding_id: str = "bind_1",
    revision: int = 1,
    is_primary: bool = True,
    context_manifest_path: str = ".agents/project/manifest.json",
) -> HostPostBindHookFrame:
    """Build a hook request for a workspace path."""
    return HostPostBindHookFrame(
        request_id="req_pb_1",
        project_id=project_id,
        binding_name=binding_name,
        binding_id=binding_id,
        revision=revision,
        repository_name="root",
        workspace=str(workspace),
        is_primary=is_primary,
        context_manifest_path=context_manifest_path,
    )


def _stub_record(path: Path) -> tuple[str, list[str], dict[str, str]]:
    """Parse the stub's record into (cwd, argv, env)."""
    cwd: str | None = None
    argv: list[str] = []
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line.startswith("cwd="):
            cwd = line[4:]
        elif line.startswith("argv0="):
            argv.append(line[6:])
        elif line.startswith("arg="):
            argv.append(line[4:])
        elif "=" in line:
            key, value = line.split("=", 1)
            env[key] = value
    assert cwd is not None
    return cwd, argv, env


def _await_file(path: Path, timeout_s: float = 5.0) -> None:
    """Wait until ``path`` exists (the stub has started)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"stub never started; {path} missing")


# ── T1 happy path ─────────────────────────────────────────


def test_happy_path_runs_with_binding_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute stub runs in the workspace with the D6 facts and no token."""
    record = tmp_path / "record.txt"
    monkeypatch.setenv(_RECORD_ENV, str(record))
    monkeypatch.setenv("OMNIGENT_HOST_TOKEN", "host-secret-token")
    monkeypatch.delenv("OMNIGENT_PROJECT_IDENTITY_ID", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_manifest(workspace, identity_id="identity-abc")
    stub = _write_stub(tmp_path / "hook.sh", _STUB)
    config = _write_config(tmp_path / "config.yaml", [str(stub)])

    result = PostBindHookRunner(config).run(_frame(workspace, revision=4))

    assert result.status == "ok"
    assert result.exit_code == 0
    assert "stub-output" in (result.output or "")
    cwd, argv, env = _stub_record(record)
    assert cwd == str(workspace)
    assert argv == [str(stub)]
    assert env["OMNIGENT_PROJECT_ID"] == "proj_1"
    assert env["OMNIGENT_BINDING_NAME"] == "primary"
    assert env["OMNIGENT_BINDING_REVISION"] == "4"
    assert env["OMNIGENT_BINDING_PRIMARY"] == "true"
    assert env["OMNIGENT_BINDING_WORKSPACE"] == str(workspace)
    assert env["OMNIGENT_REPOSITORY_NAME"] == "root"
    assert env["OMNIGENT_PROJECT_IDENTITY_ID"] == "identity-abc"
    assert "OMNIGENT_HOST_TOKEN" not in env


def test_legacy_env_aliases_never_reach_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child omnigent process cannot restore a cleared name from a legacy prefix."""
    record = tmp_path / "record.txt"
    monkeypatch.setenv(_RECORD_ENV, str(record))
    monkeypatch.setenv("OMNIGENTS_HOST_TOKEN", "legacy-token")
    monkeypatch.setenv("OMNIAGENTS_HOST_TOKEN", "older-legacy-token")
    monkeypatch.setenv("OMNIGENTS_PROJECT_IDENTITY_ID", "legacy-identity")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_manifest(workspace)
    stub = _write_stub(tmp_path / "hook.sh", _STUB)
    config = _write_config(tmp_path / "config.yaml", [str(stub)])

    result = PostBindHookRunner(config).run(_frame(workspace))

    assert result.status == "ok"
    _, _, env = _stub_record(record)
    for name in (
        "OMNIGENT_HOST_TOKEN",
        "OMNIGENTS_HOST_TOKEN",
        "OMNIAGENTS_HOST_TOKEN",
        "OMNIGENT_PROJECT_IDENTITY_ID",
        "OMNIGENTS_PROJECT_IDENTITY_ID",
    ):
        assert name not in env, name


# ── T2/T3/T4/T5 config and argv checks ────────────────────


def test_not_configured_without_key_or_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent config file or key means not_configured, and nothing runs."""
    record = tmp_path / "record.txt"
    monkeypatch.setenv(_RECORD_ENV, str(record))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _write_config(tmp_path / "config.yaml", None)

    for path in (tmp_path / "missing.yaml", config):
        result = PostBindHookRunner(path).run(_frame(workspace))
        assert result.status == "not_configured", path
    assert not record.exists()


def test_string_command_is_never_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A string command fails loudly; the runner never parses it into argv."""
    record = tmp_path / "record.txt"
    monkeypatch.setenv(_RECORD_ENV, str(record))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _write_config(tmp_path / "config.yaml", "collab root join")

    result = PostBindHookRunner(config).run(_frame(workspace))

    assert result.status == "failed"
    assert "post_bind_command" in (result.error or "")
    assert not record.exists()


def test_relative_executable_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace-relative executable is never run."""
    record = tmp_path / "record.txt"
    monkeypatch.setenv(_RECORD_ENV, str(record))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_stub(workspace / "hook", _STUB)
    config = _write_config(tmp_path / "config.yaml", ["./hook"])

    result = PostBindHookRunner(config).run(_frame(workspace))

    assert result.status == "failed"
    assert not record.exists()


@pytest.mark.parametrize("executable", ["C:\\hooks\\hook.cmd", "/opt/hook.CMD", "./hook.bat"])
def test_batch_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executable: str
) -> None:
    """A .bat/.cmd argv[0] fails the check, case-insensitively."""
    record = tmp_path / "record.txt"
    monkeypatch.setenv(_RECORD_ENV, str(record))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _write_config(tmp_path / "config.yaml", [executable])

    result = PostBindHookRunner(config).run(_frame(workspace))

    assert result.status == "failed"
    assert not record.exists()


# ── T6/T7/T8 identity id ──────────────────────────────────


@pytest.mark.parametrize("manifest_kind", ["missing", "invalid"])
def test_identity_id_absent_without_a_valid_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest_kind: str
) -> None:
    """An inherited identity id never survives a missing or invalid manifest."""
    record = tmp_path / "record.txt"
    monkeypatch.setenv(_RECORD_ENV, str(record))
    monkeypatch.setenv("OMNIGENT_PROJECT_IDENTITY_ID", "other")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if manifest_kind == "invalid":
        _write_manifest(workspace, raw="{not json")
    stub = _write_stub(tmp_path / "hook.sh", _STUB)
    config = _write_config(tmp_path / "config.yaml", [str(stub)])

    result = PostBindHookRunner(config).run(_frame(workspace))

    assert result.status == "ok"
    _, _, env = _stub_record(record)
    assert "OMNIGENT_PROJECT_IDENTITY_ID" not in env


def test_unsafe_identity_id_is_not_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An id failing the host charset check stays out of the child env."""
    record = tmp_path / "record.txt"
    monkeypatch.setenv(_RECORD_ENV, str(record))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_manifest(workspace, identity_id="a & b")
    stub = _write_stub(tmp_path / "hook.sh", _STUB)
    config = _write_config(tmp_path / "config.yaml", [str(stub)])

    result = PostBindHookRunner(config).run(_frame(workspace))

    assert result.status == "ok"
    _, _, env = _stub_record(record)
    assert "OMNIGENT_PROJECT_IDENTITY_ID" not in env


def test_manifest_symlink_escape_is_not_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest symlink pointing outside the workspace yields no id."""
    record = tmp_path / "record.txt"
    monkeypatch.setenv(_RECORD_ENV, str(record))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = _write_manifest(tmp_path / "outside", identity_id="identity-abc")
    link = workspace / ".agents/project/manifest.json"
    link.parent.mkdir(parents=True)
    os.symlink(outside, link)
    stub = _write_stub(tmp_path / "hook.sh", _STUB)
    config = _write_config(tmp_path / "config.yaml", [str(stub)])

    result = PostBindHookRunner(config).run(_frame(workspace))

    assert result.status == "ok"
    _, _, env = _stub_record(record)
    assert "OMNIGENT_PROJECT_IDENTITY_ID" not in env


# ── T9/T12 workspace and executable failures ──────────────


def test_non_canonical_workspace_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked workspace path never reaches the command."""
    record = tmp_path / "record.txt"
    monkeypatch.setenv(_RECORD_ENV, str(record))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = tmp_path / "link"
    os.symlink(workspace, link)
    stub = _write_stub(tmp_path / "hook.sh", _STUB)
    config = _write_config(tmp_path / "config.yaml", [str(stub)])

    result = PostBindHookRunner(config).run(_frame(link))

    assert result.status == "failed"
    assert not record.exists()


def test_missing_executable_reports_the_os_error(tmp_path: Path) -> None:
    """An absolute argv[0] that does not exist fails with the OS message."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = tmp_path / "missing-hook"
    config = _write_config(tmp_path / "config.yaml", [str(missing)])

    result = PostBindHookRunner(config).run(_frame(workspace))

    assert result.status == "failed"
    assert str(missing) in (result.error or "")
    assert result.exit_code is None


# ── T10 non-zero exit ─────────────────────────────────────


def test_non_zero_exit_carries_code_and_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing command reports the exit code and the output tail."""
    record = tmp_path / "record.txt"
    monkeypatch.setenv(_RECORD_ENV, str(record))
    monkeypatch.setenv(_EXIT_ENV, "3")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stub = _write_stub(tmp_path / "hook.sh", _STUB)
    config = _write_config(tmp_path / "config.yaml", [str(stub)])

    result = PostBindHookRunner(config).run(_frame(workspace))

    assert result.status == "failed"
    assert result.exit_code == 3
    assert "stub-output" in (result.output or "")
    assert "3" in (result.error or "")


# ── T11 timeout ───────────────────────────────────────────


def test_timeout_kills_and_returns_without_waiting_for_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A descendant holding the output open cannot extend the bounded wait."""
    monkeypatch.setattr(post_bind_hook, "POST_BIND_TIMEOUT_S", 0.3)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stub = _write_stub(tmp_path / "hook.sh", _TIMEOUT_STUB)
    config = _write_config(tmp_path / "config.yaml", [str(stub)])

    started = time.monotonic()
    result = PostBindHookRunner(config).run(_frame(workspace))
    elapsed = time.monotonic() - started

    assert result.status == "timed_out"
    assert elapsed < 2.5


# ── T13 supersession ──────────────────────────────────────


def test_older_revision_is_superseded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A request older than one already started returns superseded, no run."""
    ran = tmp_path / "ran.txt"
    monkeypatch.setenv(_RAN_ENV, str(ran))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stub = _write_stub(tmp_path / "hook.sh", _SLEEPING_STUB)
    config = _write_config(tmp_path / "config.yaml", [str(stub)])
    runner = PostBindHookRunner(config)

    with ThreadPoolExecutor(max_workers=1) as pool:
        newer = pool.submit(runner.run, _frame(workspace, revision=2))
        _await_file(ran)
        stale = runner.run(_frame(workspace, revision=1))
        assert newer.result().status == "ok"

    assert stale.status == "superseded"
    assert ran.read_text() == "run\n"


def test_recreated_binding_runs_from_revision_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new binding row restarts its revision sequence and is not superseded."""
    ran = tmp_path / "ran.txt"
    monkeypatch.setenv(_RAN_ENV, str(ran))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stub = _write_stub(tmp_path / "hook.sh", _SLEEPING_STUB)
    config = _write_config(tmp_path / "config.yaml", [str(stub)])
    runner = PostBindHookRunner(config)

    first = runner.run(_frame(workspace, binding_id="bind_1", revision=2))
    second = runner.run(_frame(workspace, binding_id="bind_2", revision=1))

    assert first.status == "ok"
    assert second.status == "ok"
    assert ran.read_text() == "run\nrun\n"


def test_interleaved_rows_keep_independent_watermarks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two rows of one binding name do not supersede each other's revisions."""
    ran = tmp_path / "ran.txt"
    monkeypatch.setenv(_RAN_ENV, str(ran))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stub = _write_stub(tmp_path / "hook.sh", _SLEEPING_STUB)
    config = _write_config(tmp_path / "config.yaml", [str(stub)])
    runner = PostBindHookRunner(config)

    statuses = [
        runner.run(_frame(workspace, binding_id=binding_id, revision=revision)).status
        for binding_id, revision in (
            ("bind_a", 2),
            ("bind_b", 2),
            ("bind_a", 1),
            ("bind_b", 1),
        )
    ]

    assert statuses == ["ok", "ok", "superseded", "superseded"]
    assert ran.read_text() == "run\nrun\n"
