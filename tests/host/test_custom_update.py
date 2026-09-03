"""Tests for the fork-only ``omni host update custom`` workflow."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from omnigent.cli import _HostDaemonRecord, cli
from omnigent.update_check import _InstalledWheelInfo


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
    assert "pandalaohe/omnigent.git@local/host-custom" in result.output
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
