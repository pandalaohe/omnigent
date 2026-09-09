"""Tests for the fork-only ``omni host update custom`` workflow."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from omnigent.cli import _HostDaemonRecord, cli
from omnigent.update_check import _InstalledWheelInfo

_WINDOWS_PWSH = shutil.which("pwsh") if sys.platform == "win32" else None
windows_pwsh_only = pytest.mark.skipif(
    _WINDOWS_PWSH is None,
    reason="requires Windows and PowerShell 7",
)


def _uv_info(
    *,
    commit: str = "a" * 40,
    extras: tuple[str, ...] = ("all",),
    extras_known: bool = True,
) -> _InstalledWheelInfo:
    return _InstalledWheelInfo(
        install_time_epoch=1.0,
        installer="uv",
        vcs_url="git+https://github.com/pandalaohe/omnigent.git@local/host-custom",
        commit_sha=commit,
        is_editable=False,
        package_version="0.12.0.dev0",
        detected_installer="uv",
        extras=extras,
        extras_known=extras_known,
    )


@pytest.fixture(autouse=True)
def _managed_uv_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Model a uv-tool install; the source checkout running pytest is not one."""
    import omnigent.cli as cli_module

    monkeypatch.setattr(cli_module, "IS_WINDOWS", False)
    monkeypatch.setattr(cli_module, "_uv_tool_receipt_path", lambda: tmp_path / "receipt.toml")
    monkeypatch.setattr(cli_module, "_preflight_custom_host_supervisor", lambda: None)


def test_custom_update_dry_run_targets_fork_channel_and_preserves_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.cli as cli_module

    old = "a" * 40
    new = "b" * 40
    monkeypatch.setattr(cli_module, "_find_repo_root", lambda: None, raising=False)
    monkeypatch.setattr(cli_module, "_read_installed_wheel_info", lambda: _uv_info(commit=old))
    monkeypatch.setattr(cli_module, "_remote_git_head", lambda _url: new, raising=False)

    result = CliRunner().invoke(cli, ["host", "update", "custom", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert f"pandalaohe/omnigent.git@{new}" in result.output
    assert "@local/host-custom" not in result.output
    assert "#egg=omnigent[all]" in result.output
    assert "Would run:" in result.output


def test_custom_update_restarts_supervisor_after_install_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.cli as cli_module

    events: list[str] = []
    old = "a" * 40
    new = "b" * 40
    monkeypatch.setattr(cli_module, "_find_repo_root", lambda: None, raising=False)
    monkeypatch.setattr(cli_module, "_read_installed_wheel_info", lambda: _uv_info(commit=old))
    monkeypatch.setattr(cli_module, "_remote_git_head", lambda _url: new, raising=False)
    monkeypatch.setattr(
        cli_module,
        "_build_upgrade_suggestion",
        lambda *_a, **_k: SimpleNamespace(
            command=(
                "uv tool install --reinstall "
                "git+https://github.com/pandalaohe/omnigent.git@local/host-custom"
            ),
            runnable=True,
        ),
        raising=False,
    )
    monkeypatch.setattr(cli_module, "_list_daemon_records", list)
    monkeypatch.setattr(cli_module, "_load_existing_host_id", lambda: "host-1")
    monkeypatch.setattr(
        cli_module,
        "_write_custom_host_rollback",
        lambda _sha: None,
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "_pause_custom_host_supervisor",
        lambda: events.append("pause") or object(),
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "_run_upgrade_command",
        lambda *_a, **_k: events.append("install") or 3,
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "_probe_installed_distribution",
        lambda: (None, None),
    )
    monkeypatch.setattr(
        cli_module,
        "_resume_custom_host_supervisor",
        lambda _token: events.append("resume"),
        raising=False,
    )

    result = CliRunner().invoke(cli, ["host", "update", "custom"])

    assert result.exit_code != 0
    assert events == ["pause", "install", "resume"]
    assert "pandalaohe/omnigent.git@local/host-custom" in result.output
    assert "omnigent.ai/install.sh" not in result.output


def test_custom_update_verifies_commit_and_host_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.cli as cli_module

    events: list[str] = []
    old = "a" * 40
    new = "b" * 40
    monkeypatch.setattr(cli_module, "_find_repo_root", lambda: None, raising=False)
    monkeypatch.setattr(cli_module, "_read_installed_wheel_info", lambda: _uv_info(commit=old))
    monkeypatch.setattr(cli_module, "_remote_git_head", lambda _url: new, raising=False)
    monkeypatch.setattr(
        cli_module,
        "_build_upgrade_suggestion",
        lambda *_a, **_k: SimpleNamespace(command="uv fake", runnable=True),
        raising=False,
    )
    monkeypatch.setattr(cli_module, "_list_daemon_records", list)
    monkeypatch.setattr(cli_module, "_load_existing_host_id", lambda: "host-1")
    monkeypatch.setattr(
        cli_module,
        "_write_custom_host_rollback",
        lambda _sha: None,
        raising=False,
    )
    monkeypatch.setattr(
        cli_module, "_pause_custom_host_supervisor", lambda: object(), raising=False
    )
    monkeypatch.setattr(cli_module, "_run_upgrade_command", lambda *_a, **_k: 0, raising=False)
    monkeypatch.setattr(
        cli_module, "_probe_installed_distribution", lambda: ("0.12.0.dev0", new), raising=False
    )
    monkeypatch.setattr(
        cli_module,
        "_resume_custom_host_supervisor",
        lambda _token: events.append("resume"),
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "_wait_for_custom_host_online",
        lambda host_id, _records: events.append(f"online:{host_id}"),
        raising=False,
    )

    result = CliRunner().invoke(cli, ["host", "update", "custom"])

    assert result.exit_code == 0, result.output
    assert events == ["resume", "online:host-1"]
    assert f"Updated custom Host: {old[:9]} → {new[:9]}" in result.output
    assert "omni host update custom --rollback" in result.output


def test_custom_update_rollback_uses_saved_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.cli as cli_module

    current = "b" * 40
    previous = "a" * 40
    captured: list[str] = []
    monkeypatch.setattr(cli_module, "_find_repo_root", lambda: None, raising=False)
    monkeypatch.setattr(cli_module, "_read_installed_wheel_info", lambda: _uv_info(commit=current))
    monkeypatch.setattr(cli_module, "_read_custom_host_rollback", lambda: previous, raising=False)
    monkeypatch.setattr(
        cli_module,
        "_build_upgrade_suggestion",
        lambda _info, **kwargs: (
            captured.append(kwargs["target_vcs_url"])
            or SimpleNamespace(command="uv fake", runnable=True)
        ),
        raising=False,
    )

    result = CliRunner().invoke(cli, ["host", "update", "custom", "--rollback", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert captured == [f"git+https://github.com/pandalaohe/omnigent.git@{previous}"]


@pytest.mark.parametrize("extra_args", [[], ["--force"]])
def test_custom_update_refuses_to_pause_when_session_query_fails(
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
) -> None:
    import omnigent.cli as cli_module

    old = "a" * 40
    new = "b" * 40
    record = cli_module._HostDaemonRecord(
        pid=123,
        target="https://example.com",
        mode="server",
        server_url="https://example.com",
        log_path=None,
        started_at=1,
        host_id="host-1",
    )
    paused: list[bool] = []
    monkeypatch.setattr(cli_module, "_find_repo_root", lambda: None)
    monkeypatch.setattr(cli_module, "_read_installed_wheel_info", lambda: _uv_info(commit=old))
    monkeypatch.setattr(cli_module, "_remote_git_head", lambda _url: new)
    monkeypatch.setattr(
        cli_module,
        "_build_upgrade_suggestion",
        lambda *_a, **_k: SimpleNamespace(command="uv fake", runnable=True),
    )
    monkeypatch.setattr(cli_module, "_load_existing_host_id", lambda: "host-1")
    monkeypatch.setattr(cli_module, "_list_daemon_records", lambda: [record])
    monkeypatch.setattr(cli_module, "_daemon_owner_is_live", lambda *_a: True)
    monkeypatch.setattr(
        cli_module,
        "_sessions_for_daemon",
        lambda *_a, **_k: SimpleNamespace(
            base_url="https://example.com",
            sessions=[],
            error="server unavailable",
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "_pause_custom_host_supervisor",
        lambda: paused.append(True),
    )

    result = CliRunner().invoke(cli, ["host", "update", "custom", *extra_args])

    assert result.exit_code != 0
    assert "Host was left running" in result.output
    assert paused == []


def test_force_stops_only_running_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.cli as cli_module

    record = cli_module._HostDaemonRecord(
        pid=123,
        target="https://example.com",
        mode="server",
        server_url="https://example.com",
        log_path=None,
        started_at=1,
        host_id="host-1",
    )
    stopped: list[str] = []
    monkeypatch.setattr(cli_module, "_custom_host_records", lambda _host_id: [record])
    monkeypatch.setattr(
        cli_module,
        "_sessions_for_daemon",
        lambda *_a, **_k: SimpleNamespace(
            base_url="https://example.com",
            error=None,
            sessions=[
                {"id": "running", "status": "running"},
                {"id": "idle", "status": "idle"},
                {"id": "archived", "status": "archived"},
            ],
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "_stop_session_on_server",
        lambda *, base_url, session_id: stopped.append(session_id),
    )

    cli_module._drain_custom_host_sessions("host-1", force=True)

    assert stopped == ["running"]


def test_custom_update_rejects_unknown_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.cli as cli_module

    monkeypatch.setattr(cli_module, "_find_repo_root", lambda: None)
    monkeypatch.setattr(
        cli_module,
        "_read_installed_wheel_info",
        lambda: _uv_info(extras=(), extras_known=False),
    )

    result = CliRunner().invoke(cli, ["host", "update", "custom", "--dry-run"])

    assert result.exit_code != 0
    assert "extras cannot be preserved safely" in result.output


@pytest.mark.parametrize(
    "url",
    [
        "git+https://github.com/pandalaohe/omnigent.git",
        "git+https://github.com/pandalaohe/omnigent",
        "git+ssh://git@github.com/pandalaohe/omnigent.git",
        "git@github.com:pandalaohe/omnigent.git",
    ],
)
def test_custom_fork_url_detection_covers_supported_spellings(url: str) -> None:
    import omnigent.cli as cli_module

    assert cli_module._is_custom_host_vcs_url(url)


def test_custom_rollback_receipt_round_trip_and_schema_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omnigent.cli as cli_module

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    first = "a" * 40
    second = "b" * 40
    cli_module._write_custom_host_rollback(first)
    assert cli_module._read_custom_host_rollback() == first
    cli_module._write_custom_host_rollback(second)
    assert cli_module._read_custom_host_rollback() == second
    assert list((tmp_path / "updates").iterdir()) == [
        tmp_path / "updates" / "custom-host-rollback.json"
    ]

    (tmp_path / "updates" / "custom-host-rollback.json").write_text(
        '{"schema_version": 2, "commit_sha": "' + first + '"}'
    )
    with pytest.raises(cli_module.click.ClickException, match="rollback state is invalid"):
        cli_module._read_custom_host_rollback()


def test_custom_update_checks_supervisor_before_draining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.cli as cli_module

    old = "a" * 40
    new = "b" * 40
    drained: list[bool] = []
    monkeypatch.setattr(cli_module, "_find_repo_root", lambda: None)
    monkeypatch.setattr(cli_module, "_read_installed_wheel_info", lambda: _uv_info(commit=old))
    monkeypatch.setattr(cli_module, "_remote_git_head", lambda _url: new)
    monkeypatch.setattr(
        cli_module,
        "_build_upgrade_suggestion",
        lambda *_a, **_k: SimpleNamespace(command="uv fake", runnable=True),
    )
    monkeypatch.setattr(cli_module, "_load_existing_host_id", lambda: "host-1")
    monkeypatch.setattr(
        cli_module,
        "_preflight_custom_host_supervisor",
        lambda: (_ for _ in ()).throw(cli_module.click.ClickException("no supervisor")),
    )
    monkeypatch.setattr(
        cli_module,
        "_drain_custom_host_sessions",
        lambda *_a, **_k: drained.append(True),
    )

    result = CliRunner().invoke(cli, ["host", "update", "custom", "--force"])

    assert result.exit_code != 0
    assert "no supervisor" in result.output
    assert drained == []


def test_reconnect_requires_new_generation_on_previous_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.cli as cli_module

    def _record(target: str, pid: int, started_at: int) -> _HostDaemonRecord:
        return cli_module._HostDaemonRecord(
            pid=pid,
            target=target,
            mode="server",
            server_url=target,
            log_path=None,
            started_at=started_at,
            host_id="host-1",
        )

    previous = _record("https://managed.example", 10, 100)
    same_generation = _record("https://managed.example", 10, 100)
    unrelated = _record("https://other.example", 20, 200)
    restarted = _record("https://managed.example", 30, 300)
    snapshots = iter([[unrelated, same_generation], [unrelated, restarted]])
    probed: list[str] = []
    monkeypatch.setattr(cli_module, "_custom_host_records", lambda _host_id: next(snapshots))
    monkeypatch.setattr(cli_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        cli_module,
        "_daemon_host_online",
        lambda record, **_kwargs: probed.append(record.target) or True,
    )

    cli_module._wait_for_custom_host_online("host-1", [previous])

    assert probed == ["https://managed.example"]


def test_windows_custom_supervisor_uses_existing_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omnigent.cli as cli_module

    wrapper = tmp_path / "omni-host-service.cmd"
    wrapper.write_text("@echo off\n")
    actions: list[str] = []
    monkeypatch.setattr(cli_module, "IS_WINDOWS", True)
    monkeypatch.setattr(cli_module, "_windows_host_service_wrapper", lambda: wrapper)
    monkeypatch.setattr(
        cli_module,
        "_run_windows_host_service",
        lambda _wrapper, action: actions.append(action),
    )

    token = cli_module._pause_custom_host_supervisor()
    cli_module._resume_custom_host_supervisor(token)

    assert actions == ["stop", "start"]


def test_windows_custom_supervisor_passes_wrapper_through_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PowerShell receives trusted wrapper/action values without broken ``$args`` binding."""
    import omnigent.cli as cli_module

    wrapper = tmp_path / "omni-host-service.cmd"
    wrapper.write_text("@echo off\n")
    captured: dict[str, object] = {}

    def _run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["argv"] = argv
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli_module.shutil, "which", lambda _name: "pwsh.exe")
    monkeypatch.setattr(cli_module.subprocess, "run", _run)

    cli_module._run_windows_host_service(wrapper, "stop")

    assert captured["argv"] == [
        "pwsh.exe",
        "-NoProfile",
        "-Command",
        "& $env:OMNIGENT_HOST_SERVICE_WRAPPER "
        "$env:OMNIGENT_HOST_SERVICE_ACTION; exit $LASTEXITCODE",
    ]
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["OMNIGENT_HOST_SERVICE_WRAPPER"] == str(wrapper)
    assert child_env["OMNIGENT_HOST_SERVICE_ACTION"] == "stop"


def test_windows_custom_update_schedules_detached_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omnigent.cli as cli_module

    old = "a" * 40
    new = "b" * 40
    wrapper = tmp_path / "omni-host-service.cmd"
    wrapper.write_text("@echo off\n")
    launched: list[str] = []
    direct_installs: list[bool] = []
    monkeypatch.setattr(cli_module, "IS_WINDOWS", True)
    monkeypatch.setattr(cli_module, "_find_repo_root", lambda: None)
    monkeypatch.setattr(cli_module, "_read_installed_wheel_info", lambda: _uv_info(commit=old))
    monkeypatch.setattr(cli_module, "_remote_git_head", lambda _url: new)
    monkeypatch.setattr(
        cli_module,
        "_build_upgrade_suggestion",
        lambda *_a, **_k: SimpleNamespace(command="uv fake", runnable=True),
    )
    monkeypatch.setattr(cli_module, "_load_existing_host_id", lambda: "host-1")
    monkeypatch.setattr(cli_module, "_drain_custom_host_sessions", lambda *_a, **_k: None)
    monkeypatch.setattr(cli_module, "_custom_host_records", lambda _host_id: [])
    monkeypatch.setattr(cli_module, "_pause_custom_host_supervisor", lambda: ("windows", wrapper))
    monkeypatch.setattr(
        cli_module,
        "_launch_windows_custom_host_update",
        lambda **kwargs: (
            launched.append(kwargs["install_command"])
            or (321, tmp_path / "result.json", tmp_path / "update.log")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "_run_upgrade_command",
        lambda *_a, **_k: direct_installs.append(True),
    )

    result = CliRunner().invoke(cli, ["host", "update", "custom"])

    assert result.exit_code == 0, result.output
    assert launched == ["uv fake"]
    assert direct_installs == []
    assert "detached helper pid 321" in result.output


def test_windows_custom_helper_launch_preserves_helper_readiness_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launcher never races the helper for the shared ``.tmp`` result file."""
    import omnigent.cli as cli_module

    old = "a" * 40
    new = "b" * 40
    wrapper = tmp_path / "omni-host-service.cmd"
    wrapper.write_text("@echo off\n")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli_module.shutil, "which", lambda name: f"{name}.exe")
    monkeypatch.setattr(
        cli_module,
        "_copy_windows_custom_host_helper",
        lambda path: path.write_text("# synthetic helper\n"),
    )
    monkeypatch.setattr(cli_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 1, raising=False)
    monkeypatch.setattr(cli_module.subprocess, "DETACHED_PROCESS", 2, raising=False)
    monkeypatch.setattr(cli_module.subprocess, "CREATE_NO_WINDOW", 4, raising=False)

    class _ReadyProcess:
        pid = 321

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            raise AssertionError("ready helper must not be terminated")

        def wait(self, timeout: float) -> int:
            raise AssertionError(f"ready helper must not be waited for ({timeout=})")

    def _popen(argv: list[str], **kwargs: object) -> _ReadyProcess:
        paths = cli_module._custom_host_update_paths()
        starting = json.loads(paths["result"].read_text())
        assert starting["status"] == "starting"
        assert starting["helper_pid"] is None
        assert starting["launcher_pid"] == os.getpid()
        paths["result"].write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "running",
                    "old_commit": old,
                    "target_commit": new,
                    "helper_pid": 321,
                    "error": None,
                    "source": "helper",
                }
            )
        )
        assert "-ResultPath" in argv
        assert "-WrapperPath" in argv
        assert kwargs["creationflags"] == 5
        return _ReadyProcess()

    monkeypatch.setattr(cli_module.subprocess, "Popen", _popen)

    helper_pid, result_path, _log_path = cli_module._launch_windows_custom_host_update(
        install_command="uv fake",
        recovery_command="uv fake",
        old_commit=old,
        target_commit=new,
        host_id="host-1",
        previous_records=[],
        wrapper=wrapper,
    )

    assert helper_pid == 321
    assert json.loads(result_path.read_text())["source"] == "helper"


def test_windows_update_guard_blocks_before_any_shared_launch_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two CLI launches cannot overlap even before the starting receipt exists."""
    import omnigent.cli as cli_module

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli_module, "IS_WINDOWS", True)
    acquired = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    calls: list[int] = []
    paths = cli_module._custom_host_update_paths()

    def _pause_before_receipt(*_args: object) -> None:
        calls.append(1)
        paths["lock"].write_text("")
        acquired.set()
        assert release.wait(5), "test did not release the first launcher"

    monkeypatch.setattr(cli_module, "_host_update_custom_impl", _pause_before_receipt)
    callback = cli_module.host_update_custom.callback
    assert callback is not None

    def _first() -> None:
        try:
            callback(False, False, False, False)
        except BaseException as exc:  # capture thread assertions for the test owner
            failures.append(exc)

    first = threading.Thread(target=_first)
    first.start()
    try:
        assert acquired.wait(5)
        with pytest.raises(cli_module.click.ClickException, match="already being started"):
            callback(False, False, False, False)
        assert calls == [1]
        assert paths["lock"].is_file()
        assert not paths["result"].exists()
    finally:
        release.set()
        first.join(5)
    assert not first.is_alive()
    assert not failures
    # An exited launcher releases the OS guard without unlinking its identity.
    with cli_module._custom_host_update_launch_guard():
        assert (paths["root"] / "custom-host-launch.guard").is_file()


def test_windows_custom_helper_starting_receipt_blocks_concurrent_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omnigent.cli as cli_module

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    paths = cli_module._custom_host_update_paths()
    paths["root"].mkdir(parents=True)
    paths["lock"].write_text("")
    paths["result"].write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "starting",
                "launcher_pid": 444,
                "helper_pid": None,
            }
        )
    )
    monkeypatch.setattr(cli_module, "_pid_alive", lambda pid: pid == 444)

    with pytest.raises(cli_module.click.ClickException, match="process pid 444"):
        cli_module._ensure_no_running_custom_host_helper()

    assert paths["lock"].exists()


def _synthetic_helper_argv(
    *,
    helper: Path,
    instruction: Path,
    result: Path,
    log: Path,
    lock: Path,
    wrapper: Path,
    old: str,
    new: str,
    host_id: str,
) -> list[str]:
    assert _WINDOWS_PWSH is not None
    return [
        _WINDOWS_PWSH,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper),
        "-InstructionPath",
        str(instruction),
        "-ResultPath",
        str(result),
        "-LogPath",
        str(log),
        "-LockPath",
        str(lock),
        "-WrapperPath",
        str(wrapper),
        "-OldCommit",
        old,
        "-TargetCommit",
        new,
        "-HostId",
        host_id,
    ]


@windows_pwsh_only
def test_windows_custom_helper_reports_preflight_failure_and_recovers_supervisor(
    tmp_path: Path,
) -> None:
    """Even malformed instructions close the receipt, log, lock, and supervisor path."""
    old = "a" * 40
    new = "b" * 40
    host_id = "host-1"
    helper = Path(__file__).parents[2] / "omnigent" / "host" / "windows_custom_update.ps1"
    instruction = tmp_path / "instruction.json"
    result = tmp_path / "result.json"
    log = tmp_path / "update.log"
    lock = tmp_path / "update.lock"
    wrapper = tmp_path / "omni-host-service.cmd"
    instruction.write_text("{not-json")
    lock.write_text("")
    wrapper.write_text('@echo off\r\n>>"%~dp0supervisor-started.txt" echo %1\r\nexit /b 0\r\n')

    completed = subprocess.run(
        _synthetic_helper_argv(
            helper=helper,
            instruction=instruction,
            result=result,
            log=log,
            lock=lock,
            wrapper=wrapper,
            old=old,
            new=new,
            host_id=host_id,
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 1
    payload = json.loads(result.read_text(encoding="utf-8-sig"))
    assert payload["status"] == "failed"
    assert payload["old_commit"] == old
    assert payload["target_commit"] == new
    assert payload["error"]
    assert "FAILED:" in log.read_text(encoding="utf-8-sig")
    assert not lock.exists()
    assert (tmp_path / "supervisor-started.txt").read_text().strip() == "start"


@windows_pwsh_only
def test_windows_custom_helper_survives_launcher_exit_and_completes(
    tmp_path: Path,
) -> None:
    """The detached helper finishes after its launcher exits and releases tool files."""
    old = "a" * 40
    new = "b" * 40
    host_id = "host-1"
    target = "https://managed.example"
    helper = Path(__file__).parents[2] / "omnigent" / "host" / "windows_custom_update.ps1"
    instruction = tmp_path / "instruction.json"
    result = tmp_path / "result.json"
    log = tmp_path / "update.log"
    lock = tmp_path / "update.lock"
    rollback = tmp_path / "rollback.json"
    installed = tmp_path / "installed.txt"
    wrapper = tmp_path / "omni-host-service.cmd"
    installer = tmp_path / "installer.py"
    status = tmp_path / "status.py"
    launcher = tmp_path / "launcher.py"
    bootstrap_log = tmp_path / "bootstrap.log"

    wrapper.write_text('@echo off\r\n>>"%~dp0supervisor-started.txt" echo %1\r\nexit /b 0\r\n')
    installer.write_text(
        "import os, pathlib, sys\n"
        "assert os.environ['OMNIGENT_SKIP_WEB_UI'] == 'true'\n"
        "pathlib.Path(sys.argv[1]).write_text('installed')\n"
    )
    status.write_text(
        "import json\n"
        + "print(json.dumps({'daemons': [{'host_id': "
        + repr(host_id)
        + ", 'target': "
        + repr(target)
        + ", 'pid': 222, 'process': 'online', 'host_status': 'online'}]}))\n"
    )
    instruction.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "parent_pid": 0,
                "install_argv": [sys.executable, str(installer), str(installed)],
                "recovery_command": "synthetic recovery",
                "old_commit": old,
                "target_commit": new,
                "host_id": host_id,
                "previous_records": [{"target": target, "pid": 111}],
                "probe_argv": [sys.executable, "-c", f"print('{new}')"],
                "status_argv": [sys.executable, str(status)],
                "wrapper_path": str(wrapper),
                "rollback_path": str(rollback),
                "result_path": str(result),
                "log_path": str(log),
                "lock_path": str(lock),
            }
        )
    )
    lock.write_text("")
    helper_argv = _synthetic_helper_argv(
        helper=helper,
        instruction=instruction,
        result=result,
        log=log,
        lock=lock,
        wrapper=wrapper,
        old=old,
        new=new,
        host_id=host_id,
    )
    launcher.write_text(
        "import json, os, pathlib, subprocess, sys\n"
        f"instruction = pathlib.Path({str(instruction)!r})\n"
        "payload = json.loads(instruction.read_text())\n"
        "payload['parent_pid'] = os.getpid()\n"
        "instruction.write_text(json.dumps(payload))\n"
        f"argv = {helper_argv!r}\n"
        "flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW\n"
        f"output = open({str(bootstrap_log)!r}, 'ab', buffering=0)\n"
        "process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, "
        "stdout=output, stderr=output, close_fds=True, creationflags=flags)\n"
        "output.close()\n"
        f"result = pathlib.Path({str(result)!r})\n"
        "import time\n"
        "deadline = time.monotonic() + 10\n"
        "while time.monotonic() < deadline:\n"
        "    if result.exists():\n"
        "        state = json.loads(result.read_text(encoding='utf-8-sig'))\n"
        "        if state.get('helper_pid') == process.pid and state.get('status') == 'running':\n"
        "            break\n"
        "    if process.poll() is not None:\n"
        "        raise SystemExit(f'helper exited {process.returncode}')\n"
        "    time.sleep(0.05)\n"
        "else:\n"
        "    raise SystemExit('helper did not become ready')\n"
        "print(process.pid, flush=True)\n"
    )

    launched = subprocess.run(
        [sys.executable, str(launcher)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    helper_pid = int(launched.stdout.strip())
    deadline = time.monotonic() + 20
    payload: dict[str, object] = {}
    while time.monotonic() < deadline:
        if result.exists():
            payload = json.loads(result.read_text(encoding="utf-8-sig"))
            if payload.get("status") in {"complete", "failed"}:
                break
        time.sleep(0.1)

    assert payload.get("status") == "complete", (
        payload,
        log.read_text(errors="replace") if log.exists() else "",
        bootstrap_log.read_text(errors="replace") if bootstrap_log.exists() else "",
    )
    assert payload["helper_pid"] == helper_pid
    assert installed.read_text() == "installed"
    assert json.loads(rollback.read_text(encoding="utf-8-sig"))["commit_sha"] == old
    assert (tmp_path / "supervisor-started.txt").read_text().strip() == "start"
    assert not lock.exists()
    log_text = log.read_text(encoding="utf-8-sig")
    assert "Waiting for CLI process" in log_text
    assert "Running custom Host installer." in log_text
    assert "Custom Host update completed and reconnected." in log_text
