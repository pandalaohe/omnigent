"""Only a verified supervisor can advertise host restart."""

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
    "service,marker,expected",
    [
        (True, "windows_task", "windows_task"),
        (False, "windows_task", None),
        (True, "", None),
    ],
)
def test_windows_task_supervisor_marker(monkeypatch, service, marker, expected):
    monkeypatch.setattr(restart.sys, "platform", "win32")
    monkeypatch.setattr(restart, "_service_entry", service)
    monkeypatch.setenv("OMNIGENT_HOST_SERVICE_BACKEND", marker)
    assert restart.supervisor_kind() == expected
