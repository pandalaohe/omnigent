"""Upgrade transactions use disposable installs and never invoke a real Host."""

import json
import os
from pathlib import Path
from typing import cast

import psutil
import pytest

from omnigent.host import windows_upgrade_helper as helper
from omnigent.host import windows_upgrade_launch as shim
from omnigent.update_check import _InstalledWheelInfo

STOP_TASK = helper.windows_task.stop
REQUIRE_HOST_ALIVE = helper.require_host_alive


def test_helper_absent_task_with_gbk_listing(transaction, monkeypatch):
    import subprocess

    from omnigent.host.service import HostService
    from omnigent.util import console_output

    args, _, _, root = transaction
    service = HostService("windows_task", root / "task.xml", "test-task")
    monkeypatch.setattr(helper.windows_task, "description", lambda: service)
    monkeypatch.setattr(helper.windows_task, "stop", STOP_TASK)
    monkeypatch.setattr(console_output, "_windows_code_pages", lambda: (936,))

    def task(args, **kwargs):
        listing = args == ["/Query", "/FO", "CSV", "/NH"]
        return subprocess.CompletedProcess(
            args,
            0 if listing else 1,
            '"其他任务","不适用"\r\n'.encode("gbk") if listing else b"",
            b"",
        )

    monkeypatch.setattr(helper.windows_task, "_run", task)
    assert helper.run(args) == 0


@pytest.mark.parametrize(
    "registered,alive,online",
    [(False, True, False), (True, False, False), (True, True, False), (True, True, True)],
)
def test_host_verification_uses_registration_and_liveness(
    transaction, monkeypatch, registered, alive, online
):
    from types import SimpleNamespace

    from omnigent import cli
    from omnigent.host import daemon_lifecycle

    args, _, _, _ = transaction
    monkeypatch.setattr(
        helper.windows_task, "installed", lambda _: (None, args.server) if registered else None
    )
    record = SimpleNamespace(mode="server", target=args.server, pid=123, started_at=1)
    monkeypatch.setattr(daemon_lifecycle, "read_daemon_records", lambda: [record])
    monkeypatch.setattr(daemon_lifecycle, "pid_is_recorded_daemon", lambda _: alive)
    monkeypatch.setattr(helper, "pid_is_recorded_daemon", lambda _: alive)
    monkeypatch.setattr(helper, "require_host_alive", REQUIRE_HOST_ALIVE)
    times = iter([0, 1, 100] if not alive else [0, 1, 2, 3, 100])
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(helper.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        cli,
        "_daemon_status_payload",
        lambda *a, **k: {"host_status": "online" if online else "offline"},
    )
    # Exercise the real verifier rather than the transaction's stub.
    if not registered or not alive:
        with pytest.raises(RuntimeError):
            VERIFY_HOST(args)
    else:
        assert VERIFY_HOST(args)[0] == ("online" if online else "not-online-yet")


VERIFY_HOST = helper.verify_host


@pytest.mark.parametrize("source", ["cargo", "UV"])
def test_uv_additional_locations(monkeypatch, tmp_path, source):
    uv = tmp_path / (".cargo/bin/uv.exe" if source == "cargo" else "custom/uv.exe")
    uv.parent.mkdir(parents=True)
    uv.touch()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(helper.windows_upgrade.shutil, "which", lambda _: None)
    monkeypatch.delenv("UV", raising=False)
    if source == "UV":
        monkeypatch.setenv("UV", str(uv))
    assert helper.windows_upgrade.resolve_uv_executable() == str(uv)


@pytest.fixture
def transaction(monkeypatch, tmp_path):
    tool = tmp_path / "tools" / "omnigent"
    tool.mkdir(parents=True)
    (tool / "old.txt").write_text("previous install")
    (tool / "uv-receipt.toml").write_text("[tool]\n")
    bins = tmp_path / "bin"
    bins.mkdir()
    for name in ("omni.exe", "omnigent.exe"):
        (bins / name).write_text("old launcher")
    args = helper.parser().parse_args(
        [
            "--install-spec",
            "git+https://example.test/repo.git@" + "b" * 40 + "#egg=omnigent[all]",
            "--target",
            "b" * 40,
            "--previous",
            "a" * 40,
            "--tool-dir",
            str(tool),
            "--bin-dir",
            str(bins),
            "--python",
            "3.12",
            "--log",
            str(tmp_path / "upgrade.log"),
            "--server",
            "https://example.test",
            "--force",
        ]
    )
    monkeypatch.setattr(helper, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(helper, "wait_for_parents", lambda _: None)
    monkeypatch.setattr(helper.windows_upgrade, "capture_host_tree", lambda **_: [])
    monkeypatch.setattr(helper.windows_upgrade, "stop_host_tree", lambda _: None)
    monkeypatch.setattr(helper.windows_upgrade, "resolve_uv_executable", lambda: "/opt/bin/uv.exe")
    monkeypatch.setattr(helper.windows_task, "description", lambda: None)
    monkeypatch.setattr(helper.windows_task, "stop", lambda _: None)
    commands = []

    def command(argv, env):
        commands.append((argv, env))
        if argv[1:3] == ["tool", "install"]:
            tool.mkdir(exist_ok=True)
            (tool / "new.txt").write_text("new install")

    monkeypatch.setattr(helper, "_run", command)
    monkeypatch.setattr(
        helper, "_probe_installed_distribution", lambda *a, **k: ("1", args.target)
    )
    monkeypatch.setattr(helper, "verify_host", lambda _: ("not-online-yet", None))
    monkeypatch.setattr(helper, "require_host_alive", lambda _: None)
    return args, tool, commands, tmp_path


def test_success_preserves_env_and_server(transaction, monkeypatch):
    args, tool, commands, root = transaction
    monkeypatch.setenv(helper.ORIGINAL_PATH, "/opt/bin")
    monkeypatch.setenv("PATH", "/uv/cache/bin:/opt/bin")
    monkeypatch.setenv("VIRTUAL_ENV", "/uv/cache/env")
    assert helper.run(args) == 0
    assert not (tool / "old.txt").exists()
    assert not list(tool.parent.glob("*.previous-*"))
    assert commands[-1][0][-2:] == ["--server", args.server]
    assert all(env["PATH"] == "/opt/bin" and "VIRTUAL_ENV" not in env for _, env in commands)
    record = json.loads((root / "updates/last-upgrade.json").read_text())
    assert record["result"] == "ok"
    assert record["host"] == "not-online-yet"


@pytest.mark.parametrize("failure", ["install", "commit", "register"])
def test_failure_restores_snapshot_without_network(transaction, monkeypatch, failure):
    args, tool, commands, root = transaction
    original = helper._run

    def command(argv, env):
        original(argv, env)
        if failure == "install" and argv[1:3] == ["tool", "install"]:
            raise RuntimeError("network failed")

    monkeypatch.setattr(helper, "_run", command)
    if failure == "commit":
        monkeypatch.setattr(
            helper, "_probe_installed_distribution", lambda *a, **k: ("1", "wrong")
        )
    if failure == "register":
        monkeypatch.setattr(
            helper, "verify_host", lambda _: (_ for _ in ()).throw(RuntimeError("not registered"))
        )
    assert helper.run(args) == 1
    assert (tool / "old.txt").read_text() == "previous install"
    assert not (tool / "new.txt").exists()
    assert (Path(args.bin_dir) / "omni.exe").read_text() == "old launcher"
    assert sum(cmd[1:3] == ["tool", "install"] for cmd, _ in commands) == 1
    assert commands[-1][0][1:3] == ["host", "enable"]
    assert json.loads((root / "updates/last-upgrade.json").read_text())["result"] == "failed"


def test_wait_timeout_stops_nothing(transaction, monkeypatch):
    args, tool, commands, _ = transaction
    monkeypatch.setattr(
        helper, "wait_for_parents", lambda _: (_ for _ in ()).throw(RuntimeError("timeout"))
    )
    monkeypatch.setattr(helper.windows_task, "stop", lambda _: pytest.fail("stopped before wait"))
    assert helper.run(args) == 1
    assert (tool / "old.txt").exists()
    assert commands == []


def test_rename_refused_names_holders_and_restarts(transaction, monkeypatch, capsys):
    args, tool, commands, _ = transaction
    original = Path.rename

    def rename(path, dest):
        if path == tool:
            raise PermissionError("held")
        return original(path, dest)

    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(helper, "holders", lambda _: "PID 42 (holder)")
    assert helper.run(args) == 1
    assert "PID 42" in capsys.readouterr().err
    assert commands[-1][0][1:3] == ["host", "enable"]
    assert (tool / "old.txt").exists()


def test_dry_run_and_unknown_flags(transaction, monkeypatch):
    args, tool, commands, root = transaction
    args.dry_run = True
    monkeypatch.setattr(helper.windows_task, "stop", lambda _: pytest.fail("dry run stopped task"))
    assert helper.run(args) == 0
    assert commands == [] and (tool / "old.txt").exists()
    record = json.loads((root / "updates/last-upgrade.json").read_text())
    assert record["result"] == "dry-run-ok" and record["helper_version"] == 1
    parsed, unknown = helper.parser().parse_known_args(
        [
            "--install-spec",
            "spec",
            "--target",
            "sha",
            "--tool-dir",
            "/opt/tool",
            "--bin-dir",
            "/opt/bin",
            "--python",
            "3.12",
            "--log",
            "/opt/log",
            "--local",
            "--future-flag",
        ]
    )
    assert parsed.local and unknown == ["--future-flag"]


def test_lock_live_and_stale(monkeypatch, tmp_path):
    path = tmp_path / "upgrade.lock"
    owner = shim.acquire_lock(path)
    with pytest.raises(RuntimeError, match="already running"):
        shim.acquire_lock(path)
    helper.release_lock(path, owner)
    path.write_text(json.dumps({"pid": 12345, "create_time": 0}))
    real = psutil.Process

    def process(pid=None):
        if pid == 12345:
            raise psutil.NoSuchProcess(pid)
        return real() if pid is None else real(pid)

    monkeypatch.setattr(psutil, "Process", process)
    shim.acquire_lock(path)
    assert json.loads(path.read_text())["pid"] == os.getpid()


@pytest.mark.parametrize("result", ["ok", "failed", None])
def test_status_line(transaction, result):
    _, _, _, root = transaction
    path = root / "updates/last-upgrade.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {"from": "old", "to": "new", "step": "install", "result": result, "log": "upgrade.log"}
        )
    )
    line = helper.last_upgrade_line()
    assert line is not None
    assert "old→new" in line
    assert (result or "did not finish at install") in line


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from omnigent import cli
    from omnigent.host import service

    for name in ("OMNIGENT_RUNNER_ID", "OMNIGENT_SESSION_ID", "OMNIGENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shim, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(shim, "in_job", lambda: False)
    monkeypatch.setattr(shim, "_web_ui_bundled", lambda: False)
    monkeypatch.setattr(shim.windows_upgrade, "resolve_uv_executable", lambda: "/opt/bin/uv.exe")
    monkeypatch.setattr(
        shim.windows_upgrade, "installed_launchers", lambda: [Path("/opt/bin/omni.exe")]
    )
    monkeypatch.setattr(shim.windows_upgrade, "capture_host_tree", lambda **_: [])
    monkeypatch.setattr(shim.psutil.Process, "parents", lambda _: [])
    monkeypatch.setattr(
        service, "installed_user_host_service", lambda: (None, "https://example.test")
    )
    monkeypatch.setattr(shim.shutil, "which", lambda _: None)
    events: list[object] = []
    monkeypatch.setattr(cli, "_drain_and_stop_local_server", lambda **_: events.append("drain"))

    def popen(argv, **kwargs):
        events.append((argv, kwargs))
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(shim.subprocess, "Popen", popen)
    info = _InstalledWheelInfo(
        install_time_epoch=0,
        installer="uv",
        vcs_url="git+https://example.test/repo.git@local/host-custom",
        commit_sha="a" * 40,
        is_editable=False,
        package_version="1",
        detected_installer="uv",
        extras=("all",),
    )
    return info, events, tmp_path


@pytest.mark.parametrize("dry_run", [False, True])
def test_shim_argv_env_and_detach(launcher, dry_run, monkeypatch):
    info, events, root = launcher
    from types import SimpleNamespace

    monkeypatch.setattr(
        "omnigent.update_check.sys.version_info", SimpleNamespace(major=3, minor=12)
    )
    shim.launch(info, "b" * 40, extras=("slack",), force=False, dry_run=dry_run)
    if not dry_run:
        assert events.pop(0) == "drain"
    assert len(events) == 1
    argv, options = events[0]
    source = "git+https://example.test/repo.git@" + "b" * 40
    assert argv[argv.index("--with") + 1] == source
    assert argv[argv.index("--install-spec") + 1] == source + "#egg=omnigent[all,slack]"
    assert [argv[i + 1] for i, value in enumerate(argv) if value == "--python"] == ["3.12", "3.12"]
    assert options["creationflags"] == 0x09000200
    assert options["cwd"] == root / "updates"
    assert options["env"][helper.ORIGINAL_PATH] == options["env"]["PATH"]
    assert options["env"]["OMNIGENT_SKIP_WEB_UI"] == "true"
    assert ("--dry-run" in argv) == dry_run
    assert argv[argv.index("--server") + 1] == "https://example.test"


@pytest.mark.parametrize("job", [False, True])
def test_breakaway_failure_refuses_job_or_falls_back(launcher, monkeypatch, job):
    from types import SimpleNamespace

    info, events, root = launcher
    monkeypatch.setattr(shim, "in_job", lambda: job)

    def popen(argv, **kwargs):
        events.append(kwargs["creationflags"])
        if kwargs["creationflags"] & 0x01000000:
            raise PermissionError("breakaway denied")
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(shim.subprocess, "Popen", popen)
    if job:
        with pytest.raises(OSError):
            shim.launch(info, "b" * 40, extras=(), force=False, dry_run=False)
        assert "drain" not in events
        assert not (root / "updates/upgrade.lock").exists()
    else:
        shim.launch(info, "b" * 40, extras=(), force=True, dry_run=False)
        assert events == [0x09000200, 0x08000200]


@pytest.mark.parametrize("previous", ["", "a" * 40])
def test_rename_back_failure_emits_recovery(transaction, monkeypatch, capsys, previous):
    args, _tool, commands, _ = transaction
    args.previous = previous
    monkeypatch.setattr(helper, "_probe_installed_distribution", lambda *a, **k: ("1", "wrong"))
    original = Path.rename

    def rename(path, dest):
        if ".previous-" in path.name:
            raise PermissionError("restore denied")
        return original(path, dest)

    monkeypatch.setattr(Path, "rename", rename)
    assert helper.run(args) == 1
    installs = [cmd for cmd, _ in commands if cmd[1:3] == ["tool", "install"]]
    assert len(installs) == (2 if previous else 1)
    assert "Recovery: & '/opt/bin/uv.exe' 'tool' 'install'" in capsys.readouterr().err


def test_steps_written_before_actions(transaction, monkeypatch):
    args, _, _, root = transaction
    original = helper._run
    observed = []

    def command(argv, env):
        observed.append(json.loads((root / "updates/last-upgrade.json").read_text())["step"])
        original(argv, env)

    monkeypatch.setattr(helper, "_run", command)
    assert helper.run(args) == 0
    assert observed == ["install", "verify", "register"]


def test_retry_without_web_ui(transaction, monkeypatch):
    args, _, commands, _ = transaction
    args.web_ui = "try"
    original = helper._run

    def command(argv, env):
        original(argv, env)
        if argv[1:3] == ["tool", "install"] and "OMNIGENT_SKIP_WEB_UI" not in env:
            raise RuntimeError("web UI build failed")

    monkeypatch.setattr(helper, "_run", command)
    assert helper.run(args) == 0
    installs = [env for cmd, env in commands if cmd[1:3] == ["tool", "install"]]
    assert len(installs) == 2
    assert installs[1]["OMNIGENT_SKIP_WEB_UI"] == "true"


def test_probe_uses_installed_python_and_clean_env(monkeypatch):
    import subprocess

    from omnigent.update_check import _probe_installed_distribution

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "1\n" + "b" * 40 + "\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    env = {"PATH": "/opt/bin"}
    assert _probe_installed_distribution("/opt/tool/Scripts/python.exe", env=env) == (
        "1",
        "b" * 40,
    )
    assert calls[0][0][:3] == ["/opt/tool/Scripts/python.exe", "-I", "-c"]
    assert calls[0][1]["env"] is env
    assert calls[0][1]["encoding"] == "utf-8"


def test_cli_host_status_includes_unfinished_upgrade(transaction, monkeypatch):
    from click.testing import CliRunner

    from omnigent import cli

    _, _, _, root = transaction
    path = root / "updates/last-upgrade.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"step": "install", "from": "old", "to": "new"}))
    monkeypatch.setattr(cli, "IS_WINDOWS", True)
    monkeypatch.setattr(cli, "_selected_daemon_records", lambda **_: [])
    result = CliRunner().invoke(cli.cli, ["host", "status"])
    assert result.exit_code == 0, result.output
    assert "did not finish at install" in result.output


def test_capture_uses_explicit_tool_prefix(monkeypatch, tmp_path):
    from omnigent.host import daemon_lifecycle
    from tests.host.test_windows_upgrade import Process

    prefix, bins = tmp_path / "tool", tmp_path / "bin"
    scripts = prefix / "Scripts"
    scripts.mkdir(parents=True)
    bins.mkdir()
    for name in ("omni.exe", "omnigent.exe"):
        (scripts / name).write_bytes(b"launcher")
        (bins / name).write_bytes(b"launcher")
    process = Process(
        12345, [str(prefix / "Scripts/python.exe"), "-m", "omnigent.host.service_entry"]
    )
    monkeypatch.setattr(daemon_lifecycle, "read_daemon_records", list)
    monkeypatch.setattr(helper.windows_upgrade, "_fresh_processes", lambda: iter([process]))
    monkeypatch.setattr(psutil.Process, "parents", lambda _: [])
    assert helper.windows_upgrade.installed_launchers(prefix, bins) == [
        bins / "omni.exe",
        bins / "omnigent.exe",
    ]
    assert helper.windows_upgrade.capture_host_tree(prefix=prefix, bin_dir=bins) == [process]


def test_shim_external_terminal_refusal(monkeypatch):
    monkeypatch.setenv("OMNIGENT_RUNNER_ID", "test")
    with pytest.raises(RuntimeError, match="outside Omnigent"):
        shim.launch(
            cast(_InstalledWheelInfo, None), "b" * 40, extras=(), force=False, dry_run=False
        )


@pytest.mark.parametrize(
    "failure", ["stop-task", "capture", "stop-tree", "restart", "restore-tool"]
)
def test_recovery_faults_are_journaled_and_do_not_skip_actions(transaction, monkeypatch, failure):
    from omnigent.host.service import HostServiceError

    args, tool, commands, root = transaction
    monkeypatch.setattr(helper, "_probe_installed_distribution", lambda *a, **k: ("1", "wrong"))
    journal = root / "updates/last-upgrade.json"
    actions = []

    def observe(name):
        state = json.loads(journal.read_text())
        assert state["step"] == "recovery-" + name
        assert state["failed_step"] == "verify"
        assert state["snapshot"].startswith(str(tool) + ".previous-")
        actions.append(name)
        if failure == name:
            raise HostServiceError(f"recovery {name} failed")

    def stop(_):
        if json.loads(journal.read_text())["step"].startswith("recovery-"):
            observe("stop-task")

    def capture(**kwargs):
        if json.loads(journal.read_text())["step"].startswith("recovery-"):
            observe("capture")
        return []

    def stop_tree(_):
        if json.loads(journal.read_text())["step"].startswith("recovery-"):
            observe("stop-tree")

    original_run, original_rename, original_remove, original_copy = (
        helper._run,
        Path.rename,
        helper.shutil.rmtree,
        helper.shutil.copy2,
    )

    def command(argv, env):
        if argv[1:3] == ["host", "enable"]:
            observe("restart")
        elif json.loads(journal.read_text())["step"] == "recovery-reinstall":
            observe("reinstall")
        original_run(argv, env)

    def rename(path, destination):
        if ".previous-" in path.name:
            observe("restore-tool")
        return original_rename(path, destination)

    def remove(path, **kwargs):
        observe("remove-partial")
        return original_remove(path, **kwargs)

    def copy(source, destination, **kwargs):
        if ".previous-" in source.name:
            observe("restore-" + destination.name)
        return original_copy(source, destination, **kwargs)

    monkeypatch.setattr(helper.windows_task, "stop", stop)
    monkeypatch.setattr(helper.windows_upgrade, "capture_host_tree", capture)
    monkeypatch.setattr(helper.windows_upgrade, "stop_host_tree", stop_tree)
    monkeypatch.setattr(helper, "_run", command)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(helper.shutil, "rmtree", remove)
    monkeypatch.setattr(helper.shutil, "copy2", copy)
    assert helper.run(args) == 1
    state = json.loads(journal.read_text())
    assert state["step"] == "recovery-complete" and state["result"] == "failed"
    assert state["failed_step"] == "verify"
    assert state["recovery_result"] == "incomplete"
    assert state["recovery"]["recovery-" + failure].startswith("failed:")
    assert state["recovery_command"].startswith(
        "& '/opt/bin/uv.exe' 'tool' 'install' '--reinstall' '--python' '3.12' "
    )
    assert actions[:6] == [
        "stop-task",
        "capture",
        "stop-tree",
        "remove-partial",
        "restore-tool",
        "restore-omni.exe",
    ]
    assert "restore-omnigent.exe" in actions and "restart" in actions
    if failure == "restore-tool":
        assert (Path(state["snapshot"]) / "old.txt").exists()
        assert "reinstall" in actions
    else:
        assert (tool / "old.txt").read_text() == "previous install"
        assert sum(cmd[1:3] == ["tool", "install"] for cmd, _ in commands) == 1


@pytest.mark.parametrize("reused", [False, True])
def test_direct_helper_retries_interrupted_owner(transaction, monkeypatch, reused):
    args, _, _, root = transaction
    args.dry_run = True
    lock = root / "updates/upgrade.lock"
    lock.parent.mkdir()
    lock.write_text(json.dumps({"pid": 12345, "create_time": 1}))
    from omnigent.host import windows_upgrade_lock as locks

    original = locks.process_owner

    def identity(pid):
        if pid == 12345:
            if reused:
                return {"pid": pid, "create_time": 2.0}
            raise psutil.NoSuchProcess(pid)
        return original(pid)

    monkeypatch.setattr(locks, "process_owner", identity)
    assert helper.run(args) == 0
    assert not lock.exists()


def test_host_dying_during_connection_wait_rolls_back(transaction, monkeypatch):
    from types import SimpleNamespace

    from omnigent import cli
    from omnigent.host import daemon_lifecycle

    args, tool, _, root = transaction
    live = [True]
    record = SimpleNamespace(mode="server", target=args.server, pid=12345, started_at=1)
    monkeypatch.setattr(helper, "verify_host", VERIFY_HOST)
    monkeypatch.setattr(helper, "require_host_alive", REQUIRE_HOST_ALIVE)
    monkeypatch.setattr(helper, "pid_is_recorded_daemon", lambda _: live[0])
    monkeypatch.setattr(daemon_lifecycle, "pid_is_recorded_daemon", lambda _: live[0])
    monkeypatch.setattr(daemon_lifecycle, "read_daemon_records", lambda: [record])
    monkeypatch.setattr(helper.windows_task, "installed", lambda _: (None, args.server))
    monkeypatch.setattr(helper.time, "sleep", lambda _: None)
    polls = []

    def status(*args, **kwargs):
        polls.append("disconnected")
        live[0] = False
        return {"host_status": "offline", "process": "offline"}

    monkeypatch.setattr(cli, "_daemon_status_payload", status)
    assert helper.run(args) == 1
    assert polls == ["disconnected"]
    assert (tool / "old.txt").exists()
    state = json.loads((root / "updates/last-upgrade.json").read_text())
    assert state["failed_step"] == "register" and state["result"] == "failed"


def test_host_death_at_commit_preserves_rollback(transaction, monkeypatch):
    args, tool, _, root = transaction
    monkeypatch.setattr(
        helper, "require_host_alive", lambda _: (_ for _ in ()).throw(RuntimeError("Host died"))
    )
    assert helper.run(args) == 1
    assert (tool / "old.txt").exists()
    state = json.loads((root / "updates/last-upgrade.json").read_text())
    assert state["failed_step"] == "commit" and state["result"] == "failed"


def test_recovery_command_quotes_powershell_paths(transaction, monkeypatch):
    args, _, _, root = transaction
    monkeypatch.setattr(
        helper.windows_upgrade, "resolve_uv_executable", lambda: "/opt/Tool Dir/O'Brien/uv.exe"
    )
    monkeypatch.setattr(helper, "_probe_installed_distribution", lambda *a, **k: ("1", "wrong"))
    assert helper.run(args) == 1
    state = json.loads((root / "updates/last-upgrade.json").read_text())
    assert state["recovery_command"].startswith(
        "& '/opt/Tool Dir/O''Brien/uv.exe' 'tool' 'install'"
    )
    assert "'#egg" not in state["recovery_command"]
    assert state["recovery_command"].endswith("#egg=omnigent[all]'")


RECOVERY_JOURNAL_FAULTS = [
    (name, occurrence)
    for name in (
        "recovery-stop-task",
        "recovery-capture",
        "recovery-stop-tree",
        "recovery-remove-partial",
        "recovery-restore-tool",
        "recovery-restore-omni.exe",
        "recovery-restore-omnigent.exe",
        "recovery-restart",
    )
    for occurrence in (1, 2)
] + [("recovery-complete", 1)]


@pytest.mark.parametrize("phase,occurrence", RECOVERY_JOURNAL_FAULTS)
@pytest.mark.parametrize("persistent", [False, True])
def test_journal_io_failure_never_stops_recovery(
    transaction, monkeypatch, capsys, phase, occurrence, persistent
):
    args, tool, commands, root = transaction
    monkeypatch.setattr(helper, "_probe_installed_distribution", lambda *a, **k: ("1", "wrong"))
    original = helper.atomic_write_json
    attempts = []
    states = []
    faulted = [False]
    seen = [0]

    def write(path, state):
        current = state["step"]
        attempts.append(current)
        states.append(json.loads(json.dumps(state)))
        if current == phase:
            seen[0] += 1
        if (current == phase and seen[0] == occurrence) or (persistent and faulted[0]):
            faulted[0] = True
            raise OSError("journal storage unavailable")
        original(path, state)

    monkeypatch.setattr(helper, "atomic_write_json", write)
    assert helper.run(args) == 1
    assert faulted[0]
    assert (tool / "old.txt").read_text() == "previous install"
    assert not (tool / "new.txt").exists()
    assert (Path(args.bin_dir) / "omni.exe").read_text() == "old launcher"
    assert commands[-1][0][1:3] == ["host", "enable"]
    assert sum(cmd[1:3] == ["tool", "install"] for cmd, _ in commands) == 1
    assert attempts[-1] == "recovery-complete"
    final = states[-1]
    assert final["result"] == "failed" and final["failed_step"] == "verify"
    assert final["recovery"]["recovery-restore-tool"] == "ok"
    assert final["recovery"]["recovery-restart"] == "ok"
    if persistent:
        assert final["journal_errors"]
    else:
        persisted = json.loads((root / "updates/last-upgrade.json").read_text())
        assert persisted["step"] == "recovery-complete" and persisted["result"] == "failed"
        assert persisted["journal_errors"][0]["step"] == phase
    output = capsys.readouterr().err
    assert "Could not persist/report recovery step " + phase in output
    assert "Recovery: & '/opt/bin/uv.exe'" in output
    assert "journal storage unavailable" in Path(args.log).read_text()


@pytest.mark.parametrize("phase", ["stop", "holders", "install"])
def test_forward_journal_failure_precedes_its_action(transaction, monkeypatch, phase):
    args, tool, commands, root = transaction
    original_write, original_rename = helper.atomic_write_json, Path.rename
    stopped, renamed = [], []

    def write(path, state):
        if state["step"] == phase:
            raise OSError("forward journal unavailable")
        original_write(path, state)

    def rename(path, destination):
        renamed.append((path, destination))
        return original_rename(path, destination)

    monkeypatch.setattr(helper, "atomic_write_json", write)
    monkeypatch.setattr(helper.windows_task, "stop", lambda _: stopped.append("stop"))
    monkeypatch.setattr(Path, "rename", rename)
    assert helper.run(args) == 1
    assert (tool / "old.txt").exists()
    assert not any(cmd[1:3] == ["tool", "install"] for cmd, _ in commands)
    if phase == "stop":
        assert not stopped and not renamed and not commands
    elif phase == "holders":
        assert stopped == ["stop"] and not renamed
    else:
        assert renamed[0][0] == tool and renamed[-1][1] == tool
    state = json.loads((root / "updates/last-upgrade.json").read_text())
    assert state["result"] == "failed" and state["failed_step"] == phase


def test_recovery_continues_when_all_diagnostic_outputs_fail(transaction, monkeypatch):
    args, tool, commands, _ = transaction
    monkeypatch.setattr(helper, "_probe_installed_distribution", lambda *a, **k: ("1", "wrong"))
    original = helper.atomic_write_json

    def write(path, state):
        if state["step"].startswith("recovery-"):
            raise OSError("journal full")
        original(path, state)

    class ClosedOutput:
        def write(self, text):
            raise OSError("output closed")

        def flush(self):
            raise OSError("output closed")

    original_open = Path.open

    def open_path(path, *a, **kw):
        if path == Path(args.log):
            raise PermissionError("log locked")
        return original_open(path, *a, **kw)

    monkeypatch.setattr(helper, "atomic_write_json", write)
    monkeypatch.setattr(helper.sys, "stderr", ClosedOutput())
    monkeypatch.setattr(Path, "open", open_path)
    assert helper.run(args) == 1
    assert (tool / "old.txt").exists()
    assert commands[-1][0][1:3] == ["host", "enable"]


def test_reinstall_journal_failure_retains_snapshot_and_restarts(transaction, monkeypatch):
    args, _tool, commands, root = transaction
    monkeypatch.setattr(helper, "_probe_installed_distribution", lambda *a, **k: ("1", "wrong"))
    original_write, original_rename = helper.atomic_write_json, Path.rename

    def write(path, state):
        if state["step"] == "recovery-reinstall":
            raise OSError("fallback journal locked")
        original_write(path, state)

    def rename(path, destination):
        if ".previous-" in path.name:
            raise PermissionError("restore locked")
        return original_rename(path, destination)

    monkeypatch.setattr(helper, "atomic_write_json", write)
    monkeypatch.setattr(Path, "rename", rename)
    assert helper.run(args) == 1
    state = json.loads((root / "updates/last-upgrade.json").read_text())
    assert (Path(state["snapshot"]) / "old.txt").exists()
    assert state["result"] == "failed"
    assert state["recovery"]["recovery-reinstall"] == "ok"
    assert commands[-1][0][1:3] == ["host", "enable"]
    assert sum(cmd[1:3] == ["tool", "install"] for cmd, _ in commands) == 2
