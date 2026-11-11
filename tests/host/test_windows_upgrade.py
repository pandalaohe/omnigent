"""Windows autostart shutdown and failed-installer recovery without live processes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import psutil
import pytest
from rich.console import Console

from omnigent.host import service as services
from omnigent.host import windows_upgrade as windows
from omnigent.update_check import _run_upgrade_command

TEST_SERVICE = services.HostService("windows_task", Path("/opt/work/task.xml"), "test-task")


@pytest.fixture(autouse=True)
def installer_location(monkeypatch: pytest.MonkeyPatch) -> None:
    # Installer subprocesses are explicit fakes in this module. Location itself
    # is exercised at the real resolver boundary in test_windows_task.
    monkeypatch.setattr(windows, "resolve_uv_executable", lambda: "uv")


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

    def memory_maps(self) -> list[object]:
        return []

    def open_files(self) -> list[object]:
        return []

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


@pytest.mark.parametrize("options", [[], ["-P"]])
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
            "/opt/tools/omnigent/Scripts/pythonw.exe",
            *options,
            "-m",
            "omnigent.host.service_entry",
        ],
        [host, runner],
    )
    unrelated = Process(40, ["/opt/other/omni.exe", "host"])
    monkeypatch.setattr(windows.Path, "home", lambda: home)
    monkeypatch.setattr(windows.sys, "prefix", "/opt/tools/omnigent")
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
            "/opt/tools/omnigent/Scripts/pythonw.exe",
            "-m",
            "omnigent.host.service_entry",
        ],
    )
    monkeypatch.setattr(windows.Path, "home", lambda: Path("/opt/user"))
    monkeypatch.setattr(windows, "installed_launchers", list)
    process_table.append(loop)
    monkeypatch.setattr(windows.psutil.Process, "parents", lambda _: [loop])
    with pytest.raises(RuntimeError, match="outside the Host"):
        windows.capture_host_tree()
    assert loop.alive


def test_forced_upgrade_stops_detached_install_users_and_preserves_updater(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process]
) -> None:
    from types import SimpleNamespace

    prefix = "/opt/tools/omnigent"
    launcher = Process(10, ["/opt/bin/omni.exe", "up", "--force"])
    updater = Process(
        windows.os.getpid(), [prefix + "/Scripts/python.exe", "/opt/bin/omni.exe", "up"]
    )
    daemon = Process(
        20, [prefix + "/Scripts/python.exe", "-P", "-m", "omnigent.host._daemon_entry"]
    )
    runner = Process(30, [prefix + "/Scripts/python.exe", "-P", "-m", "omnigent.runner._entry"])
    orphan = Process(40, ["/opt/python/python.exe", "-P", "-m", "omnigent.runner._entry"])
    other = Process(50, ["/opt/other/Scripts/python.exe", "-m", "omnigent.runner._entry"])
    editor = Process(60, ["/opt/editor.exe", "/opt/bin/omni.exe"])
    monkeypatch.setattr(
        orphan, "memory_maps", lambda: [SimpleNamespace(path=prefix + "/Lib/psutil.pyd")]
    )
    monkeypatch.setattr(windows.sys, "prefix", prefix)
    monkeypatch.setattr(windows.psutil.Process, "parents", lambda _: [launcher])
    monkeypatch.setattr(windows, "installed_launchers", lambda: [Path("/opt/bin/omni.exe")])
    process_table.extend([launcher, updater, daemon, runner, orphan, other, editor])
    captured = windows.capture_host_tree(include_install_users=True)
    windows.stop_host_tree(captured)
    assert not any(proc.alive for proc in (daemon, runner, orphan))
    assert launcher.alive and updater.alive and other.alive and editor.alive


def test_upgrade_inside_detached_runner_is_refused(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process]
) -> None:
    runner = Process(
        20, ["/opt/tools/omnigent/Scripts/python.exe", "-P", "-m", "omnigent.runner._entry"]
    )
    monkeypatch.setattr(windows.psutil.Process, "parents", lambda _: [runner])
    process_table.append(runner)
    with pytest.raises(RuntimeError, match="outside the Host"):
        windows.capture_host_tree(include_install_users=True)
    assert runner.alive


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
        "omnigent.host.service.installed_user_host_service", lambda: (TEST_SERVICE, None)
    )
    monkeypatch.setattr("omnigent.cli._drain_and_stop_local_server", lambda **_: None)
    monkeypatch.setattr(services, "stop_user_host_service", lambda *_: None)
    monkeypatch.setattr(services, "start_user_host_service", lambda *_: None)
    monkeypatch.setattr(windows, "capture_host_tree", lambda **_: next(snapshots))
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
        "omnigent.host.service.installed_user_host_service", lambda: (TEST_SERVICE, None)
    )
    monkeypatch.setattr("omnigent.cli._drain_and_stop_local_server", lambda **_: None)
    monkeypatch.setattr(windows, "capture_host_tree", lambda **_: [])
    events: list[str] = []

    def service(_wrapper: Path, action: str) -> None:
        events.append(action)
        if action == "start":
            assert launcher.exists()
            assert (prefix / "pyvenv.cfg").exists()

    monkeypatch.setattr(services, "stop_user_host_service", lambda svc: service(svc, "stop"))
    monkeypatch.setattr(services, "start_user_host_service", lambda svc: service(svc, "start"))
    calls = 0

    def install(
        args: list[str], *, check: bool, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
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
    monkeypatch.setattr("omnigent.update_check._web_ui_bundled", lambda: False)
    monkeypatch.setattr("omnigent.update_check._installed_cli_imports", lambda: True)
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
        ["/opt/editor.exe", "omnigent.host.service_entry"],
        [
            "/opt/bin/pwsh.exe",
            "-Command",
            "omnigent.host.service_entry",
        ],
        ["/opt/bin/pwsh.exe", "-File", "unrelated.module"],
        [
            "/opt/bin/pwsh.exe",
            "-Command",
            "echo",
            "-File",
            "omnigent.host.service_entry",
        ],
        [
            "/opt/python.exe",
            "-File",
            "omnigent.host.service_entry",
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


def test_no_service_upgrades_without_windows_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.cli import _upgrade_host_service

    monkeypatch.setattr("omnigent.cli.IS_WINDOWS", True)
    monkeypatch.setattr(services, "installed_user_host_service", lambda: None)
    monkeypatch.setattr(windows, "capture_host_tree", lambda **_: [])
    monkeypatch.setattr("omnigent.cli._drain_and_stop_local_server", lambda **_: None)

    def unexpected(*_: object) -> None:
        pytest.fail("no service stop or start without an installed task")

    monkeypatch.setattr(services, "stop_user_host_service", unexpected)
    monkeypatch.setattr(services, "start_user_host_service", unexpected)
    with _upgrade_host_service(force=False):
        pass


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


def test_live_reused_parent_is_rejected_after_clock_steps_back(
    monkeypatch: pytest.MonkeyPatch, process_table: list[Process]
) -> None:
    host = Process(100, ["captured host"])
    reused = Process(100, ["unrelated reused parent"])
    child = Process(500, ["unrelated worker"], parent=100)
    monkeypatch.setattr(reused, "create_time", lambda: 241.0)
    monkeypatch.setattr(child, "create_time", lambda: 242.0)
    clock = {"now": 250.0}
    monkeypatch.setattr(windows, "time", lambda: clock["now"])

    def wait(processes: list[Process], timeout: float) -> tuple[list[Process], list[Process]]:
        if not host.alive:
            clock["now"] = 242.0
            process_table[:] = [reused, child]
        return [p for p in processes if not p.alive], [p for p in processes if p.alive]

    monkeypatch.setattr(windows.psutil, "wait_procs", wait)
    windows.stop_host_tree([host])
    assert not host.alive
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


@pytest.mark.parametrize("transient", [True, False])
@pytest.mark.parametrize("manager_available", [True, False])
def test_launcher_share_lock_retries_then_restores_and_names_users(
    monkeypatch: pytest.MonkeyPatch,
    uv_layout: tuple[Path, Path],
    transient: bool,
    process_table: list[Process],
    manager_available: bool,
) -> None:
    prefix, bindir = uv_layout
    launcher = bindir / "omnigent.exe"
    original_rename = Path.rename
    attempts = 0
    monkeypatch.setattr(windows, "sleep", lambda _: None)
    process_table.append(Process(987, [str(prefix / "Scripts/python.exe")]))

    def file_users(_path: Path) -> str:
        if not manager_available:
            raise OSError(29, "Restart Manager could not start")
        return "PID 987 (scanner)"

    monkeypatch.setattr(windows, "file_users", file_users)

    def rename(path: Path, target: Path) -> Path:
        nonlocal attempts
        if path == launcher:
            attempts += 1
            if not transient or attempts < 3:
                error = PermissionError("sharing violation")
                error.winerror = 32
                raise error
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    if transient:
        with windows.retained_install(["uv", "tool", "install", "omnigent"]):
            pass
        assert attempts == 3
    else:
        with pytest.raises(OSError, match=r"PID 987 \(") as error:
            with windows.retained_install(["uv", "tool", "install", "omnigent"]):
                pytest.fail("installer must not start while the launcher is locked")
        assert attempts == 6
        if not manager_available:
            assert "Restart Manager unavailable" in str(error.value)
            assert str(prefix / "Scripts/python.exe") in str(error.value)
    for name in ("omni", "omnigent"):
        assert (bindir / f"{name}.exe").read_bytes() == b"current environment"


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
        "omnigent.host.service.installed_user_host_service", lambda: (TEST_SERVICE, None)
    )
    monkeypatch.setattr("omnigent.cli._drain_and_stop_local_server", lambda **_: None)
    monkeypatch.setattr(services, "stop_user_host_service", lambda svc: service(svc, "stop"))
    monkeypatch.setattr(services, "start_user_host_service", lambda svc: service(svc, "start"))
    monkeypatch.setattr(windows, "capture_host_tree", lambda **_: [host])
    monkeypatch.setattr("omnigent.update_check.subprocess.run", unexpected_install)
    with _upgrade_host_service(force=True):
        code = _run_upgrade_command("uv tool install --reinstall omnigent", Console())
    assert code == -1
    assert not host.alive and orphan.alive
    assert events == ["stop", "start"]
    assert "File is in use by the surviving worker" in capsys.readouterr().out


def test_release_closes_launcher_zip_opened_by_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib
    import importlib.metadata
    import zipfile

    # uv's launcher runs `python <omni.exe>`, so the zip-bearing exe is sys.path[0].
    launcher = tmp_path / "omni.exe"
    with zipfile.ZipFile(launcher, "w") as archive:
        archive.writestr("__main__.py", "")
    monkeypatch.syspath_prepend(str(launcher))

    def held() -> bool:
        return any(Path(f.path) == launcher for f in psutil.Process().open_files())

    with pytest.raises(importlib.metadata.PackageNotFoundError):
        importlib.metadata.version("bug271-absent-distribution")
    assert held(), "importlib.metadata keeps the launcher zip open"
    windows._release_launcher_zip()
    assert not held()
    importlib.invalidate_caches()


def test_retained_install_releases_launcher_before_first_rename(
    monkeypatch: pytest.MonkeyPatch, uv_layout: tuple[Path, Path]
) -> None:
    events: list[str] = []
    original_rename = Path.rename

    def rename(path: Path, target: Path) -> Path:
        events.append("rename")
        return original_rename(path, target)

    monkeypatch.setattr(windows, "_release_launcher_zip", lambda: events.append("release"))
    monkeypatch.setattr(Path, "rename", rename)
    with windows.retained_install(["uv", "tool", "install", "omnigent"]):
        pass
    assert events[0] == "release" and "rename" in events


def test_verified_backups_become_trash_that_the_next_upgrade_removes(
    monkeypatch: pytest.MonkeyPatch, uv_layout: tuple[Path, Path]
) -> None:
    prefix, bindir = uv_layout
    hex_id = "0" * 32
    recovery = prefix.with_name(f"tool.previous-{hex_id}")
    recovery.mkdir()
    decoy = prefix.with_name("tool.trash-notours")
    decoy.mkdir()
    outside = prefix.parent / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    linked = prefix.with_name(f"tool.trash-{'1' * 32}")
    linked.symlink_to(outside, target_is_directory=True)
    original_unlink = Path.unlink

    def unlink(path: Path, missing_ok: bool = False) -> None:
        if path.name.startswith("omni.exe.previous-"):
            raise PermissionError("running launcher image")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    receipt = (prefix / "uv-receipt.toml").read_text()

    def install() -> None:
        (prefix / "Scripts").mkdir(parents=True)
        (prefix / "uv-receipt.toml").write_text(receipt)
        for name in ("omni", "omnigent"):
            (bindir / f"{name}.exe").write_bytes(b"replacement")
            (prefix / "Scripts" / f"{name}.exe").write_bytes(b"replacement")

    with windows.retained_install(["uv", "tool", "install", "omnigent"]):
        install()
    # Recovery copies and anything not named by this module stay; this run's leftovers are trash.
    assert [path.name for path in prefix.parent.glob("tool.previous-*")] == [recovery.name]
    first_trash = set(prefix.parent.glob(f"tool.trash-{'[0-9a-f]' * 32}")) - {linked}
    assert len(first_trash) == 1 and decoy.exists() and linked.is_symlink()
    assert len(list(bindir.glob("omni.exe.trash-*"))) == 1
    assert not list(bindir.glob("*.previous-*"))
    monkeypatch.setattr(Path, "unlink", original_unlink)

    with windows.retained_install(["uv", "tool", "install", "omnigent"]):
        install()
    assert not list(bindir.glob("*.trash-*")) and not list(bindir.glob("*.previous-*"))
    assert not any(path.exists() for path in first_trash)
    assert recovery.exists() and decoy.exists() and linked.is_symlink()
    assert (outside / "keep").read_text() == "keep"
    assert len(set(prefix.parent.glob(f"tool.trash-{'[0-9a-f]' * 32}")) - {linked}) == 1


@pytest.mark.parametrize("bundled", [False, True])
@pytest.mark.parametrize("first_exit", [0, 1])
def test_windows_web_ui_build_cannot_block_the_install(
    monkeypatch: pytest.MonkeyPatch, uv_layout: tuple[Path, Path], bundled: bool, first_exit: int
) -> None:
    prefix, bindir = uv_layout
    calls: list[dict[str, str] | None] = []

    def install(args: list[str], *, check: bool, env: dict[str, str] | None = None):
        calls.append(env)
        prefix.mkdir(exist_ok=True)
        (bindir / "omni.exe").write_bytes(b"replacement")
        code = first_exit if len(calls) == 1 else 0
        return subprocess.CompletedProcess(args, code)

    monkeypatch.setattr("omnigent.update_check.IS_WINDOWS", True)
    monkeypatch.setattr("omnigent.update_check._web_ui_bundled", lambda: bundled)
    monkeypatch.setattr("omnigent.update_check._installed_cli_imports", lambda: True)
    monkeypatch.setattr("omnigent.update_check.subprocess.run", install)
    assert _run_upgrade_command("uv tool install --reinstall omnigent", Console()) == (
        first_exit if not bundled else 0
    )
    skip = [env is not None and env.get("OMNIGENT_SKIP_WEB_UI") == "true" for env in calls]
    if not bundled:
        assert skip == [True]
    elif first_exit:
        assert skip == [False, True]
    else:
        assert skip == [False]


def test_windows_install_that_cannot_start_is_rolled_back(
    monkeypatch: pytest.MonkeyPatch, uv_layout: tuple[Path, Path]
) -> None:
    prefix, bindir = uv_layout

    def install(args: list[str], *, check: bool, env: dict[str, str] | None = None):
        (prefix / "Scripts").mkdir(parents=True)
        (bindir / "omni.exe").write_bytes(b"broken replacement")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("omnigent.update_check.IS_WINDOWS", True)
    monkeypatch.setattr("omnigent.update_check._web_ui_bundled", lambda: False)
    monkeypatch.setattr("omnigent.update_check._installed_cli_imports", lambda: False)
    monkeypatch.setattr("omnigent.update_check.subprocess.run", install)
    assert _run_upgrade_command("uv tool install --reinstall omnigent", Console()) != 0
    assert (bindir / "omni.exe").read_bytes() == b"current environment"
    assert (prefix / "Scripts" / "omni.exe").read_bytes() == b"current environment"


def test_host_restarts_when_draining_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import click

    from omnigent.cli import _upgrade_host_service

    events: list[str] = []

    def drain(**_: object) -> None:
        raise click.ClickException("drain interrupted")

    monkeypatch.setattr("omnigent.cli.IS_WINDOWS", True)
    monkeypatch.setattr(
        "omnigent.host.service.installed_user_host_service", lambda: (TEST_SERVICE, None)
    )
    monkeypatch.setattr("omnigent.cli.shutil.which", lambda _: "/opt/bin/pwsh.exe")
    monkeypatch.setattr("omnigent.cli._drain_and_stop_local_server", drain)
    monkeypatch.setattr(services, "start_user_host_service", lambda _svc: events.append("start"))
    monkeypatch.setattr(windows, "capture_host_tree", lambda **_: [])
    with pytest.raises(click.ClickException, match="drain interrupted"):
        with _upgrade_host_service(force=False):
            pytest.fail("the install must not run after a failed drain")
    assert events == ["start"]


def test_installed_cli_import_check_reports_a_missing_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.update_check import _installed_cli_imports

    assert _installed_cli_imports()
    monkeypatch.setattr("omnigent.update_check.sys.executable", str(tmp_path / "python.exe"))
    assert not _installed_cli_imports()
