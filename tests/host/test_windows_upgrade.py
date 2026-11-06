"""Windows autostart shutdown and failed-installer recovery without live processes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import psutil
import pytest
from rich.console import Console

from omnigent.host import windows_upgrade as windows
from omnigent.update_check import _run_upgrade_command


class Process:
    def __init__(
        self, pid: int, args: list[str], children: list[Process] | None = None, parent: int = 0
    ):
        self.pid = pid
        self.args = args
        self.descendants = children or []
        self.alive = True
        self.parent = parent

    def cmdline(self) -> list[str]:
        return self.args

    def exe(self) -> str:
        return self.args[0]

    def is_running(self) -> bool:
        return self.alive

    def create_time(self) -> float:
        return float(self.pid)

    def ppid(self) -> int:
        return self.parent

    def children(self, *, recursive: bool) -> list[Process]:
        assert recursive
        return self.descendants

    def terminate(self) -> None:
        self.alive = False

    def kill(self) -> None:
        self.alive = False


@pytest.fixture
def process_table(monkeypatch: pytest.MonkeyPatch) -> list[Process]:
    table: list[Process] = []
    monkeypatch.setattr(windows, "_fresh_processes", lambda: iter(table))
    monkeypatch.setattr(windows.psutil.Process, "parents", lambda _: [])
    monkeypatch.setattr(
        windows.shutil, "which", lambda name: "/opt/bin/pwsh.exe" if name == "pwsh" else None
    )
    monkeypatch.setattr(
        windows.psutil,
        "wait_procs",
        lambda processes, timeout: (
            [p for p in processes if not p.alive],
            [p for p in processes if p.alive],
        ),
    )
    return table


@pytest.mark.parametrize("options", [[], ["-NoProfile", "-ExecutionPolicy", "Bypass"]])
def test_unrecorded_autostart_tree_is_stopped(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process], options: list[str]
) -> None:
    home = Path("/opt/user")
    runner = Process(30, ["python", "runner"])
    host = Process(
        20, ["/opt/user/.local/bin/omni.exe", "host", "--server", "https://example.test"], [runner]
    )
    loop = Process(
        10,
        [
            "/opt/bin/pwsh.exe",
            *options,
            "-File",
            str(home / ".omnigent/autostart/Start-OmnigentHostDefault.ps1"),
        ],
        [host, runner],
    )
    unrelated = Process(40, ["/opt/other/omni.exe", "host"])
    monkeypatch.setattr(windows.Path, "home", lambda: home)
    monkeypatch.setattr(
        windows, "installed_launchers", lambda: [Path("/opt/user/.local/bin/omni.exe")]
    )
    process_table.extend([loop, host, runner, unrelated])
    captured = windows.capture_host_tree()
    assert {p.pid for p in captured} == {10, 20, 30}
    windows.stop_host_tree(captured)
    assert not any(p.alive for p in (loop, host, runner))
    assert unrelated.alive


def test_upgrade_inside_host_is_refused(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process]
) -> None:
    loop = Process(
        10,
        [
            "/opt/bin/pwsh.exe",
            "-File",
            "/opt/user/.omnigent/autostart/Start-OmnigentHostDefault.ps1",
        ],
    )
    monkeypatch.setattr(windows.Path, "home", lambda: Path("/opt/user"))
    monkeypatch.setattr(windows, "installed_launchers", list)
    process_table.append(loop)
    monkeypatch.setattr(windows.psutil.Process, "parents", lambda _: [loop])
    with pytest.raises(RuntimeError, match="outside the Host"):
        windows.capture_host_tree()
    assert loop.alive


def test_tree_that_will_not_exit_blocks_install(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process]
) -> None:
    process = Process(10, ["omni", "host"])
    monkeypatch.setattr(process, "terminate", lambda: None)
    monkeypatch.setattr(process, "kill", lambda: None)
    monkeypatch.setattr(windows.psutil, "wait_procs", lambda processes, timeout: ([], processes))
    with pytest.raises(RuntimeError, match="did not exit"):
        windows.stop_host_tree([process])


def test_daemon_replaced_during_drain_is_also_stopped(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process]
) -> None:
    from omnigent.cli import _upgrade_host_service

    replacement = Process(20, ["omni.exe", "host"])
    snapshots = iter([[], [replacement]])
    monkeypatch.setattr("omnigent.cli.IS_WINDOWS", True)
    monkeypatch.setattr(
        "omnigent.cli._windows_host_service_wrapper", lambda: Path("/opt/wrapper.cmd")
    )
    monkeypatch.setattr("omnigent.cli._drain_and_stop_local_server", lambda **_: None)
    monkeypatch.setattr("omnigent.cli._run_windows_host_service", lambda *_: None)
    monkeypatch.setattr(windows, "capture_host_tree", lambda: next(snapshots))
    monkeypatch.setattr(
        windows.psutil,
        "wait_procs",
        lambda processes, timeout: ([], [p for p in processes if p.alive]),
    )
    with _upgrade_host_service(force=True):
        assert not replacement.alive


@pytest.mark.parametrize("outcome", ["success", "failed", "missing-installer", "no-launchers"])
@pytest.mark.parametrize("verb", ["install", "upgrade"])
def test_windows_reinstall_restores_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, verb: str
) -> None:
    from omnigent.cli import _upgrade_host_service

    prefix = tmp_path / "tool"
    prefix.mkdir()
    scripts = prefix / "Scripts"
    scripts.mkdir()
    (scripts / "omni.exe").write_bytes(b"old launcher")
    (prefix / "pyvenv.cfg").write_text("old environment")
    launcher = tmp_path / "omni.exe"
    launcher.write_bytes(b"old launcher")
    (prefix / "uv-receipt.toml").write_text(
        '[tool]\nentrypoints = [{ name = "omni", install-path = '
        + json.dumps(str(launcher))
        + " }]\n"
    )
    monkeypatch.setattr(windows.sys, "prefix", str(prefix))
    monkeypatch.setattr(
        windows.shutil, "which", lambda name: "/opt/bin/pwsh.exe" if name == "pwsh" else None
    )
    monkeypatch.setattr("omnigent.update_check.IS_WINDOWS", True)
    monkeypatch.setattr("omnigent.cli.IS_WINDOWS", True)
    monkeypatch.setattr(
        "omnigent.cli._windows_host_service_wrapper", lambda: tmp_path / "wrapper.cmd"
    )
    monkeypatch.setattr("omnigent.cli._drain_and_stop_local_server", lambda **_: None)
    monkeypatch.setattr(windows, "capture_host_tree", list)
    events: list[str] = []

    def service(_wrapper: Path, action: str) -> None:
        events.append(action)
        if action == "start":
            assert launcher.exists()
            assert (prefix / "pyvenv.cfg").exists()

    monkeypatch.setattr("omnigent.cli._run_windows_host_service", service)
    calls = 0

    def install(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        assert not launcher.exists(), "locked launcher must be renamed before copying"
        if verb == "install":
            assert not prefix.exists(), "retain the old environment before uv replaces it"
        else:
            assert (prefix / "uv-receipt.toml").exists(), "uv upgrade needs the installed receipt"
        if outcome == "missing-installer" and calls == 1:
            raise FileNotFoundError("uv missing")
        prefix.mkdir(exist_ok=True)
        (prefix / "pyvenv.cfg").write_text("replacement")
        if outcome != "no-launchers":
            launcher.write_bytes(b"replacement")
        return subprocess.CompletedProcess(args, 2 if outcome == "failed" and calls == 1 else 0)

    monkeypatch.setattr("omnigent.update_check.subprocess.run", install)
    command = f"uv tool {verb} omnigent"
    with _upgrade_host_service(force=True):
        code = _run_upgrade_command(command, Console())
    assert events == ["stop", "start"]
    assert code == {"success": 0, "failed": 2, "missing-installer": -1, "no-launchers": 0}[outcome]
    if outcome in ("failed", "missing-installer"):
        assert launcher.read_bytes() == b"old launcher"
        assert (prefix / "pyvenv.cfg").read_text() == "old environment"
        with _upgrade_host_service(force=True):
            assert _run_upgrade_command(command, Console()) == 0
        assert events == ["stop", "start", "stop", "start"]
    assert launcher.read_bytes() == (
        b"old launcher" if outcome == "no-launchers" else b"replacement"
    )


def test_vanished_process_is_ignored(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process]
) -> None:
    proc = Process(1, [])

    def vanished() -> list[str]:
        raise psutil.NoSuchProcess(1)

    monkeypatch.setattr(proc, "cmdline", vanished)
    process_table.append(proc)
    monkeypatch.setattr(windows, "installed_launchers", list)
    assert windows.capture_host_tree() == []


@pytest.mark.parametrize(
    "args",
    [
        ["/opt/editor.exe", "/opt/user/.omnigent/autostart/Start-OmnigentHostDefault.ps1"],
        [
            "/opt/bin/pwsh.exe",
            "-Command",
            "/opt/user/.omnigent/autostart/Start-OmnigentHostDefault.ps1",
        ],
        ["/opt/bin/pwsh.exe", "-File", "/opt/other/Start-OmnigentHostDefault.ps1"],
        [
            "/opt/bin/pwsh.exe",
            "-Command",
            "echo",
            "-File",
            "/opt/user/.omnigent/autostart/Start-OmnigentHostDefault.ps1",
        ],
        [
            "/opt/python.exe",
            "-File",
            "/opt/user/.omnigent/autostart/Start-OmnigentHostDefault.ps1",
        ],
        ["/opt/bin/omni.exe", "host", "update", "custom", "--server", "https://example.test"],
        ["/opt/bin/omni.exe", "host"],
    ],
)
def test_capture_excludes_editors_and_management_commands(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process], args: list[str]
) -> None:
    process_table.append(Process(10, args, [Process(20, ["child"])]))
    monkeypatch.setattr(windows.Path, "home", lambda: Path("/opt/user"))
    monkeypatch.setattr(windows, "installed_launchers", lambda: [Path("/opt/bin/omni.exe")])
    assert windows.capture_host_tree() == []
    assert process_table[0].alive


@pytest.mark.parametrize("missing", ["wrapper", "pwsh"])
def test_missing_restart_path_refuses_before_any_stop(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    import click

    from omnigent.cli import _upgrade_host_service

    monkeypatch.setattr("omnigent.cli.IS_WINDOWS", True)
    monkeypatch.setattr(
        "omnigent.cli._windows_host_service_wrapper",
        lambda: None if missing == "wrapper" else Path("/opt/wrapper.cmd"),
    )
    monkeypatch.setattr(
        windows.shutil, "which", lambda _: None if missing == "pwsh" else "/opt/pwsh.exe"
    )

    def untouched(**_: object) -> None:
        raise AssertionError("must refuse before draining or stopping any process")

    monkeypatch.setattr("omnigent.cli._drain_and_stop_local_server", untouched)
    with pytest.raises(click.ClickException, match="required before upgrading"):
        with _upgrade_host_service(force=True):
            raise AssertionError("installer must not run")


def test_shutdown_collects_late_children_and_grandchildren(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process]
) -> None:
    root = Process(100, ["host"])
    late = Process(200, ["runner"], parent=100)
    grandchild = Process(300, ["worker"], parent=200)
    unrelated = Process(50, ["older process"], parent=100)
    process_table.extend([root, unrelated])

    def wait(processes: list[Process], timeout: float) -> tuple[list[Process], list[Process]]:
        if timeout == 2:
            process_table.append(late)
        return [], [p for p in processes if p.alive]

    def terminate_late() -> None:
        late.alive = False
        process_table.append(grandchild)

    monkeypatch.setattr(late, "terminate", terminate_late)
    monkeypatch.setattr(windows.psutil, "wait_procs", wait)
    windows.stop_host_tree([root])
    assert not any(p.alive for p in [root, late, grandchild])
    assert unrelated.alive


def test_shutdown_does_not_follow_reused_parent_pid(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process]
) -> None:
    old = Process(100, ["old host"])
    reused = Process(100, ["unrelated new process"])
    monkeypatch.setattr(reused, "create_time", lambda: 400.0)
    child = Process(500, ["unrelated child"], parent=100)
    monkeypatch.setattr(windows, "time", lambda: 250.0)
    process_table.extend([reused, child])
    windows.stop_host_tree([old])
    assert reused.alive and child.alive


@pytest.mark.parametrize("born", [99.0, 100.0, 250.0, 250.001, 500.0])
def test_child_of_absent_parent_must_fit_recorded_lifetime(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process], born: float
) -> None:
    host = Process(100, ["captured host"])
    worker = Process(300, ["worker of an exited parent"], parent=100)
    monkeypatch.setattr(worker, "create_time", lambda: born)
    monkeypatch.setattr(windows, "time", lambda: 250.0)
    # The reused parent has also exited; the sweep sees only its worker.
    process_table.append(worker)
    windows.stop_host_tree([host])
    assert not host.alive
    assert worker.alive == (born < 100.0 or born > 250.0)


def test_parent_gone_before_stop_does_not_authorize_unknown_children(
    process_table: list[Process],
) -> None:
    host = Process(100, ["already exited host"])
    host.alive = False
    worker = Process(300, ["unknown worker"], parent=100)
    process_table.append(worker)
    windows.stop_host_tree([host])
    assert worker.alive


def test_capture_uses_fresh_identity_after_pid_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    host = Process(10, ["/opt/bin/omni.exe", "host", "--server", "https://example.test"])
    replacement = Process(10, host.args)
    updater = Process(999, ["updater"])
    monkeypatch.setattr(updater, "parents", list, raising=False)
    created: list[int] = []

    def process(pid: int | None = None) -> Process:
        if pid is None:
            return updater
        created.append(pid)
        return host if len(created) == 1 else replacement

    monkeypatch.setattr(windows.psutil, "Process", process)
    monkeypatch.setattr(windows.psutil, "pids", lambda: [10])
    monkeypatch.setattr(windows, "installed_launchers", lambda: [Path("/opt/bin/omni.exe")])
    monkeypatch.setattr(windows.shutil, "which", lambda _: "/opt/pwsh.exe")
    assert windows.capture_host_tree() == [host]
    host.alive = False
    assert windows.capture_host_tree() == [replacement]
    assert created == [10, 10]


def test_capture_revalidates_command_after_binding_children(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process]
) -> None:
    host = Process(10, ["/opt/bin/omni.exe", "host", "--server", "https://example.test"])
    calls = iter([host.args, ["/opt/bin/omni.exe", "host", "update", "custom"]])
    monkeypatch.setattr(host, "cmdline", lambda: next(calls))
    monkeypatch.setattr(windows, "installed_launchers", lambda: [Path("/opt/bin/omni.exe")])
    process_table.append(host)
    assert windows.capture_host_tree() == []
    assert host.alive


@pytest.fixture
def uv_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    prefix = tmp_path / "tool"
    scripts = prefix / "Scripts"
    scripts.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    entries = []
    for name in ("omni", "omnigent"):
        (scripts / f"{name}.exe").write_bytes(b"current environment")
        (bindir / f"{name}.exe").write_bytes(b"current environment")
        entries.append(
            "{ name = "
            + json.dumps(name)
            + ", install-path = "
            + json.dumps(str(bindir / f"{name}.exe"))
            + " }"
        )
    (prefix / "uv-receipt.toml").write_text("[tool]\nentrypoints = [" + ",".join(entries) + "]\n")
    monkeypatch.setattr(windows.sys, "prefix", str(prefix))
    return prefix, bindir


@pytest.mark.parametrize("blocked", ["environment", "launcher"])
def test_rollback_attempts_every_path_and_keeps_failed_backups(
    monkeypatch: pytest.MonkeyPatch, uv_layout: tuple[Path, Path], blocked: str
) -> None:
    prefix, bindir = uv_layout
    original_rename = Path.rename

    def rename(path: Path, target: Path) -> Path:
        if blocked == "environment" and path == prefix and ".failed-" in target.name:
            raise PermissionError("partial environment still locked")
        if (
            blocked == "launcher"
            and path.name.startswith("omni.exe.previous-")
            and target == bindir / "omni.exe"
        ):
            raise PermissionError("launcher restoration blocked")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError, match="backups kept"):
        with windows.retained_install(["uv", "tool", "install", "omnigent"]):
            prefix.mkdir()
            raise RuntimeError("installer failed")
    assert (bindir / "omnigent.exe").read_bytes() == b"current environment"
    if blocked == "environment":
        assert (bindir / "omni.exe").read_bytes() == b"current environment"
        assert list(prefix.parent.glob("tool.previous-*/Scripts/omni.exe"))
    else:
        assert list(bindir.glob("omni.exe.previous-*"))
        assert (prefix / "Scripts/omni.exe").read_bytes() == b"current environment"


def test_launcher_paths_belong_to_current_receipt_and_environment(
    monkeypatch: pytest.MonkeyPatch, uv_layout: tuple[Path, Path], tmp_path: Path
) -> None:
    _, bindir = uv_layout
    other = tmp_path / "other-omni.exe"
    other.write_bytes(b"other environment")
    monkeypatch.setattr(windows.shutil, "which", lambda _: str(other))
    # Even the recorded destination can have been overwritten by another venv.
    (bindir / "omnigent.exe").write_bytes(b"other environment")
    assert windows.installed_launchers() == [bindir / "omni.exe"]
    with windows.retained_install(["uv", "tool", "upgrade", "omnigent"]):
        pass
    assert (bindir / "omni.exe").read_bytes() == b"current environment"
    assert (bindir / "omnigent.exe").read_bytes() == b"other environment"
    assert other.read_bytes() == b"other environment"


def test_orphan_venv_holder_aborts_install_and_restores(
    monkeypatch: pytest.MonkeyPatch,
    process_table: list[Process],
    uv_layout: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from omnigent.cli import _upgrade_host_service

    prefix, bindir = uv_layout
    host = Process(100, ["captured host"])
    orphan = Process(300, ["worker whose intermediary already exited"], parent=200)
    process_table.append(orphan)
    locked_file = prefix / "Scripts" / "omni.exe"
    original_rename = Path.rename
    events: list[str] = []

    def rename(path: Path, target: Path) -> Path:
        if path == prefix and orphan.alive:
            raise PermissionError(32, "File is in use by the surviving worker", str(locked_file))
        return original_rename(path, target)

    def service(_wrapper: Path, action: str) -> None:
        events.append(action)
        if action == "start":
            assert locked_file.read_bytes() == b"current environment"
            for name in ("omni", "omnigent"):
                assert (bindir / f"{name}.exe").read_bytes() == b"current environment"

    def unexpected_install(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("locked venv must abort before launching the installer")

    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr("omnigent.cli.IS_WINDOWS", True)
    monkeypatch.setattr("omnigent.update_check.IS_WINDOWS", True)
    monkeypatch.setattr(
        "omnigent.cli._windows_host_service_wrapper", lambda: bindir / "wrapper.cmd"
    )
    monkeypatch.setattr("omnigent.cli._drain_and_stop_local_server", lambda **_: None)
    monkeypatch.setattr("omnigent.cli._run_windows_host_service", service)
    monkeypatch.setattr(windows, "capture_host_tree", lambda: [host])
    monkeypatch.setattr("omnigent.update_check.subprocess.run", unexpected_install)
    with _upgrade_host_service(force=True):
        code = _run_upgrade_command("uv tool install --reinstall omnigent", Console())
    assert code == -1
    assert not host.alive and orphan.alive
    assert events == ["stop", "start"]
    assert "File is in use by the surviving worker" in capsys.readouterr().out
