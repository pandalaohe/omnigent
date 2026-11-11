"""Only a verified supervisor can advertise host restart."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.host import restart
from omnigent.host.frames import (
    HostRestartFrame,
    HostRestartResultFrame,
    decode_host_frame,
    encode_host_frame,
)


@pytest.mark.parametrize(
    "frame",
    [
        HostRestartFrame("preview"),
        HostRestartFrame("real", False, "caller", 120, True),
        HostRestartResultFrame(
            "real", True, supervisor="launchd", running_sessions=["caller"], runner_count=1
        ),
    ],
)
def test_restart_frames_round_trip(frame):
    assert decode_host_frame(encode_host_frame(frame)) == frame


@pytest.mark.parametrize(
    "platform,service,parent,invocation,expected",
    [
        ("darwin", True, 1, "", "launchd"),
        ("darwin", False, 1, "", None),
        ("darwin", True, 10, "", None),
        ("linux", True, 1, "unit", "systemd"),
        ("linux", True, 1, "", None),
    ],
)
def test_supervisor_admission(monkeypatch, platform, service, parent, invocation, expected):
    monkeypatch.setattr(restart.sys, "platform", platform)
    monkeypatch.setattr(restart, "_service_entry", service)
    monkeypatch.setattr(restart.os, "getppid", lambda: parent)
    monkeypatch.setenv("INVOCATION_ID", invocation)
    assert restart.supervisor_kind() == expected


@pytest.mark.parametrize(
    "disabled,ancestor,enabled",
    [(False, True, True), (True, True, True), (False, False, True), (False, True, False)],
)
def test_windows_existing_restart_loop(monkeypatch, tmp_path, disabled, ancestor, enabled):
    tmp_path = tmp_path / "用户"
    root = tmp_path / ".omnigent" / "autostart"
    root.mkdir(parents=True)
    loop = root / "Start-OmnigentHostDefault.ps1"
    launcher = root / "Launch-OmnigentHost.pyw"
    loop.write_text("# test loop")
    launcher.write_text("# test launcher")
    if disabled:
        (root / "disabled").touch()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(restart.sys, "platform", "win32")
    parent = SimpleNamespace(
        cmdline=lambda: ["pwsh", "-File", str(loop)] if ancestor else ["pwsh"]
    )
    monkeypatch.setattr(
        restart.psutil, "Process", lambda: SimpleNamespace(parents=lambda: [parent])
    )
    monkeypatch.setattr(
        restart.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=(
                f'<?xml version="1.0" encoding="UTF-16"?><Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions><Exec><Arguments>"{launcher}"</Arguments></Exec></Actions><Settings><Enabled>{str(enabled).lower()}</Enabled></Settings></Task>'
            ).encode("utf-16"),
        ),
    )
    assert restart.supervisor_kind() == (
        "windows-wrapper" if ancestor and enabled and not disabled else None
    )
