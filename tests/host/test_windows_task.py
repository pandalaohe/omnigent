"""Windows task lifecycle with an explicit scheduler and process table."""

from __future__ import annotations

import json
import locale
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from omnigent.host import service, service_entry, windows_task, windows_upgrade
from omnigent.util import console_output, private_files


@pytest.fixture
def scheduler(monkeypatch, tmp_path):
    state = SimpleNamespace(
        xml=None, listing=b"", calls=[], failure=None, recorded=[], forgotten=[]
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service.platform, "system", lambda: "Windows")
    monkeypatch.setattr(windows_task, "windows_user_sid", lambda: "S-1-5-21-1")
    python = tmp_path / "Scripts" / "python.exe"
    python.parent.mkdir()
    python.touch()
    python.with_name("pythonw.exe").touch()
    monkeypatch.setattr(windows_task.sys, "executable", str(python))
    monkeypatch.setattr(windows_upgrade, "capture_host_tree", lambda **_: [])
    monkeypatch.setattr(windows_upgrade, "stop_host_tree", lambda _: None)
    monkeypatch.setattr(service, "_record_service", state.recorded.append)
    monkeypatch.setattr(service, "_forget_service", state.forgotten.append)

    def run(args, *, check=True):
        state.calls.append(args)
        if state.failure and state.failure in args:
            raise service.HostServiceError("scheduler failed")
        if args[0] == "/Query":
            if "/XML" in args:
                return subprocess.CompletedProcess(args, 0 if state.xml else 1, state.xml or b"")
            return subprocess.CompletedProcess(args, 0, state.listing)
        if args[0] == "/Create":
            state.xml = Path(args[args.index("/XML") + 1]).read_bytes()
        elif args[0] == "/Delete":
            state.xml = None
        elif args[0] == "/Change":
            root = ET.fromstring(state.xml)
            root.find(
                f"{{{windows_task.TASK_NS}}}Settings/{{{windows_task.TASK_NS}}}Enabled"
            ).text = "false" if "/DISABLE" in args else "true"
            state.xml = ET.tostring(root)
        return subprocess.CompletedProcess(args, 0, b"")

    monkeypatch.setattr(windows_task, "_run", run)
    return state


@pytest.mark.parametrize("target", [None, "https://server.example/work?q=1&b=2"])
def test_enable_query_start_disable_windows_task(scheduler, target, monkeypatch):
    monkeypatch.setattr(locale, "getpreferredencoding", lambda _: "utf-8")
    scheduler.listing = '"\\其他任务","不适用","就绪"\r\n'.encode("gbk")
    environment = {"PATH": r"C:\Tools\node;C:\Tools\uv", "SYSTEMROOT": r"C:\Windows"}
    installed = service.enable_user_host_service(target, environment=environment)
    assert installed.kind == "windows_task"
    assert service.installed_user_host_service() == (installed, target)
    task = ET.fromstring(scheduler.xml)
    ns = {"t": windows_task.TASK_NS}
    for path, expected in {
        "t:Triggers/t:LogonTrigger/t:UserId": "S-1-5-21-1",
        "t:Principals/t:Principal/t:LogonType": "InteractiveToken",
        "t:Principals/t:Principal/t:RunLevel": "LeastPrivilege",
        "t:Settings/t:MultipleInstancesPolicy": "IgnoreNew",
        "t:Settings/t:ExecutionTimeLimit": "PT0S",
        "t:Settings/t:Hidden": "true",
        "t:Settings/t:DisallowStartIfOnBatteries": "false",
        "t:Settings/t:StopIfGoingOnBatteries": "false",
    }.items():
        assert task.findtext(path, namespaces=ns) == expected
    assert task.findtext("t:Actions/t:Exec/t:Command", namespaces=ns).endswith("pythonw.exe")
    assert "--env-file" in task.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
    assert json.loads(windows_task.env_path(installed).read_text()) == environment
    assert scheduler.recorded == [installed]
    service.stop_user_host_service(installed)
    assert task is not None
    assert service.installed_user_host_service() == (installed, target)
    service.start_user_host_service(installed)
    service.disable_user_host_service()
    assert scheduler.xml is None
    assert not installed.path.exists()
    assert not windows_task.env_path(installed).exists()
    assert scheduler.forgotten == [installed]


@pytest.mark.parametrize(
    "encoding,description",
    [
        ("utf-16", "中文"),
        ("utf-16-le", "中文"),
        ("utf-16-be", "中文"),
        ("gbk", "中文"),
        ("ascii", "plain"),
    ],
)
def test_query_decodes_scheduler_xml_bytes(monkeypatch, tmp_path, encoding, description):
    service_info = service.HostService("windows_task", tmp_path / "task.xml", "test-task")
    xml = (
        '<?xml version="1.0" encoding="UTF-16"?>\r\r\n'
        f"<Task><Description>{description}</Description></Task>"
    ).encode(encoding)

    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, xml)

    monkeypatch.setattr(windows_task, "_run", run)
    monkeypatch.setattr(console_output, "_windows_code_pages", lambda: (936,))

    task = windows_task.query(service_info)

    assert task is not None
    assert task.tag == "Task"
    assert task.findtext("Description") == description


def test_failed_task_refresh_restores_previous_environment(scheduler):
    installed = service.enable_user_host_service(None, environment={"PATH": "old"})
    original = scheduler.xml
    # Fail only the first Run so restoration can restart the previous task.
    run = windows_task._run
    failed = False

    def once(args, **kwargs):
        nonlocal failed
        if args[0] == "/Run" and not failed:
            failed = True
            raise service.HostServiceError("run failed")
        return run(args, **kwargs)

    from unittest.mock import patch

    with patch.object(windows_task, "_run", once):
        with pytest.raises(service.HostServiceError, match="run failed"):
            service.enable_user_host_service("https://new.example", environment={"PATH": "new"})
    assert windows_task.env_path(installed).read_text() == '{"PATH": "old"}'
    assert ET.tostring(ET.fromstring(scheduler.xml)) == ET.tostring(ET.fromstring(original))


def test_disable_after_config_home_change_removes_original_files(scheduler, monkeypatch, tmp_path):
    installed = service.enable_user_host_service(None, environment={"PATH": "old"})
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "different"))
    assert service.installed_user_host_service() == (installed, None)
    removed = service.disable_user_host_service()
    assert removed == installed
    assert not windows_task.env_path(installed).exists()
    assert not installed.path.exists()


@pytest.mark.parametrize("failure", ["/DISABLE", "still-enabled", "/End"])
def test_stop_failure_never_claims_quiescence(scheduler, monkeypatch, failure):
    installed = service.enable_user_host_service(None, environment={})
    run = windows_task._run
    stopped = []
    if failure == "still-enabled":
        monkeypatch.setattr(
            windows_task,
            "_run",
            lambda args, **kw: (
                subprocess.CompletedProcess(args, 0, b"")
                if "/DISABLE" in args
                else run(args, **kw)
            ),
        )
    else:
        scheduler.failure = failure
    monkeypatch.setattr(windows_upgrade, "stop_host_tree", stopped.append)
    with pytest.raises(service.HostServiceError):
        service.stop_user_host_service(installed)
    assert stopped == []


def test_supervisor_frozen_before_final_snapshot_and_task_end(scheduler, monkeypatch):
    installed = service.enable_user_host_service(None, environment={})
    events = []
    child = SimpleNamespace(pid=20)
    supervisor = SimpleNamespace(
        pid=10,
        cmdline=lambda: ["pythonw", "-m", "omnigent.host.service_entry"],
        suspend=lambda: events.append("suspend"),
        resume=lambda: events.append("resume"),
    )
    snapshots = 0

    def capture(**_):
        nonlocal snapshots
        snapshots += 1
        if snapshots == 3:
            assert "suspend" in events
            return [supervisor, child]
        return [supervisor]

    run = windows_task._run

    def manager(args, **kwargs):
        if args[0] == "/End":
            events.append("end")
        return run(args, **kwargs)

    monkeypatch.setattr(windows_upgrade, "capture_host_tree", capture)
    monkeypatch.setattr(windows_task, "_run", manager)

    def finish(tree):
        events.append(("tree", tree))

    monkeypatch.setattr(windows_upgrade, "stop_host_tree", finish)
    service.stop_user_host_service(installed)
    assert events[:2] == ["suspend", "end"]
    tree = next(event[1] for event in events if isinstance(event, tuple))
    assert child in tree


@pytest.mark.parametrize("encoding", ["gbk", "cp932", "utf-8"])
@pytest.mark.parametrize("present", [False, True])
def test_query_localized_listing_preserves_denial(monkeypatch, tmp_path, encoding, present):
    installed = service.HostService(
        "windows_task", tmp_path / "task.xml", r"\Omnigent Host (S-1-5-21-1)"
    )
    monkeypatch.setattr(locale, "getpreferredencoding", lambda _: "utf-8")
    listing = '"\\定期,タスク""確認","N/A","準備"\r\n'
    listing += f'"{installed.label}-other","N/A","準備"\r\n'
    if present:
        listing += f'"{installed.label.upper()}","N/A","準備"\r\n'

    def run(args, **_):
        if "/XML" in args:
            return subprocess.CompletedProcess(args, 1, b"")
        return subprocess.CompletedProcess(args, 0, listing.encode(encoding))

    monkeypatch.setattr(windows_task, "_run", run)
    if present:
        with pytest.raises(service.HostServiceError, match="Cannot read"):
            windows_task.query(installed)
    else:
        assert windows_task.query(installed) is None


def test_private_file_acl_precedes_bytes_and_denial_preserves_old_file(monkeypatch, tmp_path):
    target = tmp_path / "redirected" / "environment.json"
    target.parent.mkdir()
    target.write_bytes(b"old")
    protected = []
    monkeypatch.setattr(private_files, "IS_WINDOWS", True)

    def protect(path):
        protected.append((path, path.read_bytes()))

    def create(path):
        fd, name = tempfile.mkstemp(dir=path.parent)
        protected.append((Path(name), b""))
        return fd, Path(name)

    monkeypatch.setattr(private_files, "protect_windows_file", protect)
    monkeypatch.setattr(private_files, "_create_windows_private_temp", create)
    private_files.atomic_write_private(target, b"new")
    assert protected[0] == (target, b"old")
    assert protected[1][1] == b""
    assert protected[-1] == (target, b"new")

    def deny(path):
        raise PermissionError("ACL refused")

    monkeypatch.setattr(private_files, "protect_windows_file", deny)
    with pytest.raises(PermissionError, match="ACL refused"):
        private_files.atomic_write_private(target, b"must not be written")
    assert target.read_bytes() == b"new"


def test_windows_dacl_contains_only_current_user_and_system(monkeypatch, tmp_path):
    calls = []

    class Function:
        def __init__(self, invoke):
            self.invoke = invoke

        def __call__(self, *args):
            return self.invoke(*args)

    advapi = SimpleNamespace(
        ConvertStringSecurityDescriptorToSecurityDescriptorW=Function(
            lambda sddl, *_: calls.append(sddl) or True
        ),
        SetFileSecurityW=Function(lambda path, flags, _: calls.append((path, flags)) or True),
    )
    kernel = SimpleNamespace(LocalFree=Function(lambda _: None))
    monkeypatch.setattr(
        private_files.ctypes,
        "WinDLL",
        lambda name, **_: advapi if name == "advapi32" else kernel,
        raising=False,
    )
    monkeypatch.setattr(private_files, "windows_user_sid", lambda: "S-1-5-21-1")
    file = tmp_path / "environment.json"
    private_files.protect_windows_file(file)
    assert calls == ["D:P(A;;FA;;;SY)(A;;FA;;;S-1-5-21-1)", (str(file), 0x80000004)]


def test_temporary_acl_failure_writes_no_bytes(monkeypatch, tmp_path):
    target = tmp_path / "environment.json"
    target.write_bytes(b"old")
    monkeypatch.setattr(private_files, "IS_WINDOWS", True)

    def refuse(path):
        raise PermissionError("temporary ACL refused")

    monkeypatch.setattr(private_files, "protect_windows_file", lambda _: None)
    monkeypatch.setattr(private_files, "_create_windows_private_temp", refuse)
    with pytest.raises(PermissionError, match="temporary ACL"):
        private_files.atomic_write_private(target, b"new")
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_createfile_receives_dacl_at_creation_without_handle_inheritance(monkeypatch, tmp_path):
    import ctypes

    class Function:
        def __init__(self, invoke):
            self.invoke = invoke

        def __call__(self, *args):
            return self.invoke(*args)

    def descriptor(sddl, version, pointer, size):
        assert sddl == "D:P(A;;FA;;;SY)(A;;FA;;;S-1-5-21-1)"
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(123)
        return True

    def create(path, access, sharing, attributes, disposition, flags, template):
        security = attributes._obj
        assert security.length == ctypes.sizeof(security)
        assert security.descriptor == 123
        assert not security.inherit
        assert (access, sharing, disposition, flags, template) == (0x40000000, 0, 1, 0x80, None)
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

    kernel = SimpleNamespace(
        CreateFileW=Function(create),
        CloseHandle=Function(os.close),
        LocalFree=Function(lambda _: None),
    )
    advapi = SimpleNamespace(
        ConvertStringSecurityDescriptorToSecurityDescriptorW=Function(descriptor)
    )
    monkeypatch.setattr(
        private_files.ctypes,
        "WinDLL",
        lambda name, **_: advapi if name == "advapi32" else kernel,
        raising=False,
    )
    monkeypatch.setattr(private_files, "windows_user_sid", lambda: "S-1-5-21-1")
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=lambda handle, flags: handle)
    )
    monkeypatch.setattr(os, "O_BINARY", 0, raising=False)
    fd, path = private_files._create_windows_private_temp(tmp_path / "environment.json")
    with os.fdopen(fd, "wb") as output:
        assert path.read_bytes() == b""
        output.write(b"private")
    assert path.read_bytes() == b"private"


def test_durable_cleanup_failure_preserves_ledger_and_retry_path(scheduler, monkeypatch, tmp_path):
    from omnigent import install_ledger

    monkeypatch.setattr(service, "_record_service", lambda _: None)
    installed = service.enable_user_host_service(None, environment={})
    ledger = install_ledger.new_ledger(source="installer", strategy="install", deep=False)
    ledger.entries.launch_agents = [
        install_ledger.LaunchAgentEntry(
            kind=installed.kind,
            label=installed.label,
            path=str(installed.path),
        )
    ]
    stale = install_ledger.new_ledger(source="backfill", strategy="install", deep=False)
    stale.entries.launch_agents = [
        install_ledger.LaunchAgentEntry(
            kind=installed.kind, label=installed.label, path=str(tmp_path / "stale/task.xml")
        )
    ]
    monkeypatch.setattr(
        install_ledger,
        "load_ledger",
        lambda path: ledger if path == install_ledger.ledger_path() else stale,
    )
    unlink = Path.unlink
    locked = windows_task.env_path(installed)

    def deny(path, **kwargs):
        if path == locked:
            raise PermissionError("file locked")
        return unlink(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny)
    with pytest.raises(service.HostServiceError, match="saved files remain"):
        service.disable_user_host_service()
    assert scheduler.xml is None
    assert locked.exists()
    assert scheduler.forgotten == []
    # The task is gone and the shell changed config home; the retained ledger
    # still locates the unfinished cleanup.
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "new-home"))
    assert service.disable_user_host_service() == installed
    assert not locked.exists()
    assert scheduler.forgotten == [installed]


def test_full_windows_allowlist_can_be_persisted_and_loaded(scheduler, monkeypatch):
    from omnigent import cli
    from omnigent._platform import WINDOWS_ENV_PASSTHROUGH

    captured = {name: f"value-for-{name}" for name in WINDOWS_ENV_PASSTHROUGH}
    captured["PROGRAMFILES(X86)"] = "program-files-x86"
    safe_paths = {
        name: value
        for name, value in cli.os.environ.items()
        if name
        in (
            "HOME",
            "OMNIGENT_CONFIG_HOME",
            "OMNIGENT_DATA_DIR",
        )
    }
    monkeypatch.setattr(
        cli.os, "environ", {**safe_paths, **captured, "UNRELATED": "not-forwarded"}
    )
    environment = cli._build_host_daemon_env(server_url=None)
    assert set(captured) <= set(environment)
    assert "UNRELATED" not in environment
    installed = service.enable_user_host_service(None, environment=environment)
    service_entry._load_environment(windows_task.env_path(installed))
    assert dict(service_entry.os.environ) == environment


def test_temporary_cleanup_warning_preserves_success(monkeypatch, tmp_path):
    target = tmp_path / "environment.json"
    unlink = Path.unlink

    def locked(path, **kwargs):
        if path.name.startswith(".environment.json."):
            raise PermissionError("scanner holds temporary file")
        return unlink(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.warns(UserWarning, match="Could not remove temporary file"):
        private_files.atomic_write_private(target, b"new")
    assert target.read_bytes() == b"new"


@pytest.mark.parametrize("codes", [[1, 75, 0], [78]])
def test_windows_restart_loop_restarts_only_transient_failures(monkeypatch, codes):
    results = iter(codes)
    calls, sleeps = [], []

    def run(args, **_):
        calls.append(args)
        return subprocess.CompletedProcess(args, next(results))

    monkeypatch.setattr(service_entry.subprocess, "run", run)
    monkeypatch.setattr(service_entry.time, "sleep", sleeps.append)
    assert service_entry._windows_supervise(["--local", "--env-file", "environment.json"]) == 0
    assert len(calls) == len(codes)
    assert sleeps == [service_entry._WINDOWS_RESTART_BACKOFF_S] * (len(codes) - 1)
    assert all("--service-worker" in args for args in calls)


def test_service_environment_loads_before_cli(monkeypatch, tmp_path):
    from omnigent import cli

    file = tmp_path / "environment.json"
    file.write_text('{"PATH": "captured"}')
    monkeypatch.setattr(
        service_entry.sys,
        "argv",
        [
            "service-entry",
            "--local",
            "--env-file",
            str(file),
        ],
    )
    # monkeypatch restores the environment after the service replaces it.
    monkeypatch.setattr(service_entry.os, "environ", {"PATH": "before"})
    observed = []
    monkeypatch.setattr(
        cli.cli, "main", lambda **_: observed.append(service_entry.os.environ["PATH"])
    )
    assert service_entry.main() == 0
    assert observed == ["captured"]


def test_uv_winget_location_without_path(monkeypatch, tmp_path):
    local = tmp_path / "LocalAppData"
    uv = local / "Microsoft/WinGet/Links/uv.exe"
    uv.parent.mkdir(parents=True)
    uv.touch()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(windows_upgrade.shutil, "which", lambda _: None)
    assert windows_upgrade.resolve_uv_executable() == str(uv)


@pytest.mark.parametrize("force", [False, True])
def test_windows_upgrade_captures_before_drain_and_keeps_remote_wait(monkeypatch, tmp_path, force):
    from omnigent import cli

    installed = service.HostService("windows_task", tmp_path / "task.xml", "test-task")
    events = []
    original = SimpleNamespace(pid=10)
    monkeypatch.setattr(cli, "IS_WINDOWS", True)
    monkeypatch.setattr(
        service, "installed_user_host_service", lambda: (installed, "https://remote.example")
    )
    monkeypatch.setattr(
        windows_upgrade, "capture_host_tree", lambda **_: events.append("capture") or [original]
    )
    monkeypatch.setattr(cli, "_drain_and_stop_local_server", lambda **_: events.append("drain"))
    monkeypatch.setattr(cli, "_find_daemon_record", lambda _: object())
    monkeypatch.setattr(cli, "_count_running_daemon_sessions", lambda _: 1)
    monkeypatch.setattr(
        cli,
        "_wait_for_running_sessions",
        lambda count, **_: events.append("wait") if count() else None,
    )
    monkeypatch.setattr(service, "stop_user_host_service", lambda _: events.append("stop"))
    monkeypatch.setattr(service, "start_user_host_service", lambda _: events.append("start"))
    monkeypatch.setattr(
        windows_upgrade,
        "stop_host_tree",
        lambda tree: (
            events.append("tree") if original in tree else pytest.fail("lost original tree")
        ),
    )
    with cli._upgrade_host_service(force=force):
        events.append("install")
    assert events[:2] == ["capture", "drain"]
    assert ("wait" in events) is not force
    assert (
        events.index("stop")
        < events.index("tree")
        < events.index("install")
        < events.index("start")
    )
