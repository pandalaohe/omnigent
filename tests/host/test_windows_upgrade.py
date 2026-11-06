"""Windows autostart shutdown and failed-installer recovery without live processes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import psutil
import pytest
from rich.console import Console

from omnigent.host import windows_upgrade as windows
from omnigent.update_check import _run_upgrade_command


class Process:
    def __init__(self, pid: int, args: list[str], children: list[Process] | None = None):
        self.pid = pid
        self.args = args
        self.descendants = children or []
        self.alive = True

    def cmdline(self) -> list[str]:
        return self.args

    def children(self, *, recursive: bool) -> list[Process]:
        assert recursive
        return self.descendants

    def terminate(self) -> None:
        self.alive = False

    def kill(self) -> None:
        self.alive = False


def test_unrecorded_autostart_tree_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path("/opt/user")
    runner = Process(30, ["python", "runner"])
    host = Process(
        20, ["/opt/user/.local/bin/omni.exe", "host", "--server", "https://example.test"], [runner]
    )
    loop = Process(
        10,
        ["pwsh", "-File", str(home / ".omnigent/autostart/Start-OmnigentHostDefault.ps1")],
        [host, runner],
    )
    unrelated = Process(40, ["/opt/other/omni.exe", "host"])
    monkeypatch.setattr(windows.Path, "home", lambda: home)
    monkeypatch.setattr(windows.shutil, "which", lambda name: f"/opt/user/.local/bin/{name}.exe")
    monkeypatch.setattr(windows.psutil, "process_iter", lambda: [loop, host, runner, unrelated])
    monkeypatch.setattr(windows.psutil.Process, "parents", lambda _: [])
    monkeypatch.setattr(
        windows.psutil,
        "wait_procs",
        lambda processes, timeout: (
            [p for p in processes if not p.alive],
            [p for p in processes if p.alive],
        ),
    )
    captured = windows.capture_host_tree()
    assert {p.pid for p in captured} == {10, 20, 30}
    windows.stop_host_tree(captured)
    assert not any(p.alive for p in (loop, host, runner))
    assert unrelated.alive


def test_upgrade_inside_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = Process(10, ["pwsh", "/opt/user/.omnigent/autostart/Start-OmnigentHostDefault.ps1"])
    monkeypatch.setattr(windows.Path, "home", lambda: Path("/opt/user"))
    monkeypatch.setattr(windows.psutil, "process_iter", lambda: [loop])
    monkeypatch.setattr(windows.psutil.Process, "parents", lambda _: [loop])
    with pytest.raises(RuntimeError, match="outside the Host"):
        windows.capture_host_tree()
    assert loop.alive


def test_tree_that_will_not_exit_blocks_install(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Process(10, ["omni", "host"])
    monkeypatch.setattr(process, "terminate", lambda: None)
    monkeypatch.setattr(process, "kill", lambda: None)
    monkeypatch.setattr(windows.psutil, "wait_procs", lambda processes, timeout: ([], processes))
    with pytest.raises(RuntimeError, match="did not exit"):
        windows.stop_host_tree([process])


def test_daemon_replaced_during_drain_is_also_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.mark.parametrize("outcome", ["success", "failed", "missing-installer"])
@pytest.mark.parametrize("verb", ["install", "upgrade"])
def test_windows_reinstall_restores_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, verb: str
) -> None:
    from omnigent.cli import _upgrade_host_service

    prefix = tmp_path / "tool"
    prefix.mkdir()
    (prefix / "uv-receipt.toml").write_text("old receipt")
    (prefix / "pyvenv.cfg").write_text("old environment")
    launcher = tmp_path / "omni.exe"
    launcher.write_bytes(b"old launcher")
    monkeypatch.setattr(windows.sys, "prefix", str(prefix))
    monkeypatch.setattr(
        windows.shutil, "which", lambda name: str(launcher) if name == "omni" else None
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
        launcher.write_bytes(b"replacement")
        return subprocess.CompletedProcess(args, 2 if outcome == "failed" and calls == 1 else 0)

    monkeypatch.setattr("omnigent.update_check.subprocess.run", install)
    command = f"uv tool {verb} omnigent"
    with _upgrade_host_service(force=True):
        code = _run_upgrade_command(command, Console())
    assert events == ["stop", "start"]
    assert code == {"success": 0, "failed": 2, "missing-installer": -1}[outcome]
    if outcome != "success":
        assert launcher.read_bytes() == b"old launcher"
        assert (prefix / "pyvenv.cfg").read_text() == "old environment"
        with _upgrade_host_service(force=True):
            assert _run_upgrade_command(command, Console()) == 0
        assert events == ["stop", "start", "stop", "start"]
    assert launcher.read_bytes() == b"replacement"


def test_vanished_process_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = Process(1, [])

    def vanished() -> list[str]:
        raise psutil.NoSuchProcess(1)

    monkeypatch.setattr(proc, "cmdline", vanished)
    monkeypatch.setattr(windows.psutil, "process_iter", lambda: [proc])
    monkeypatch.setattr(windows.psutil.Process, "parents", lambda _: [])
    assert windows.capture_host_tree() == []
