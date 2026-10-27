"""SDK startup containment, using fake assignment and termination targets."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.inner import windows_jobobject_sandbox as jobs
from omnigent.native import windows_processes as processes


@pytest.mark.parametrize("root_state", ["gone", "reused"])
def test_job_receipt_recovers_root_before_first_census(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root_state: str
) -> None:
    record = tmp_path / "conv-6000.process.json"
    record.write_text(json.dumps({"pid": 6000, "create_time": 123.5}))
    (tmp_path / "conv-6000.job.json").write_text(
        json.dumps(
            {"pid": 6000, "create_time": 123.5, "job_name": "Global\\omnigent-sdk-" + "a" * 32}
        )
    )
    root = Mock()
    root.create_time.return_value = 999.0
    lookup = Mock(return_value=root)
    if root_state == "gone":
        lookup.side_effect = processes.psutil.NoSuchProcess(6000)
    monkeypatch.setattr(processes.psutil, "Process", lookup)
    absent = Mock(return_value=True)
    monkeypatch.setattr(jobs, "job_has_exited", absent, raising=False)
    assert processes.reap_record(record)
    absent.assert_called_once_with("Global\\omnigent-sdk-" + "a" * 32)
    root.terminate.assert_not_called()


@pytest.mark.parametrize("contained", [True, False])
def test_startup_binds_before_harness_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contained: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from fastapi import FastAPI

    from omnigent.runtime.harnesses import _runner

    events: list[str] = []
    handle = Mock()
    monkeypatch.setattr(_runner, "IS_WINDOWS", True)
    monkeypatch.setattr(_runner, "_WINDOWS_JOB", None)
    monkeypatch.setattr(_runner, "_configure_logging", lambda *_args: None)
    monkeypatch.setattr(processes, "process_birth", lambda _pid: 123.5)

    def assign(_pid: int, *, name: str | None = None) -> object:
        assert name and name.startswith("Global\\omnigent-sdk-")
        events.append("containment confirmed" if contained else "containment failed")
        return handle if contained else None

    monkeypatch.setattr(jobs, "create_kill_on_close_job", assign, raising=False)

    def load(*_args: object) -> FastAPI:
        assert events, "CLI imports must wait for containment or an explicit fallback"
        if not contained:
            assert "keeping census-only recovery" in caplog.text
        events.append("CLI import")
        return FastAPI()

    monkeypatch.setattr(_runner, "_load_harness_app", load)
    monkeypatch.setattr(_runner.gc, "freeze", lambda: None)
    monkeypatch.setattr(_runner, "_HardExitServer", Mock())
    _runner.main(
        [
            "--harness",
            "test",
            "--module",
            "unused",
            "--bind",
            "127.0.0.1:12345",
            "--conversation-id",
            "example",
            "--process-record",
            str(tmp_path / "conv-launch.process.json"),
        ]
    )
    assert events[-1] == "CLI import"
    handle.close.assert_not_called()
    receipts = list(tmp_path.glob("conv-*.job.json"))
    assert bool(receipts) is contained
    if contained:
        assert json.loads(receipts[0].read_text())["create_time"] == 123.5


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "CreateJobObjectW",
        "collision",
        "SetInformationJobObject",
        "OpenProcess",
        "AssignProcessToJobObject",
    ],
)
def test_job_assignment_and_failure_cleanup(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    from types import SimpleNamespace

    job_handle, process_handle = 2**40 + 1, 2**40 + 2
    api = SimpleNamespace(
        **{
            name: Mock(return_value=value)
            for name, value in {
                "CreateJobObjectW": job_handle,
                "SetInformationJobObject": 1,
                "OpenProcess": process_handle,
                "AssignProcessToJobObject": 1,
                "CloseHandle": 1,
                "GetLastError": 183 if failure == "collision" else 0,
            }.items()
        }
    )
    if failure and failure != "collision":
        getattr(api, failure).return_value = 0
    monkeypatch.setattr(jobs, "_kernel32", lambda: api)
    handle = jobs.create_kill_on_close_job(6000, name="Global\\omnigent-sdk-" + "a" * 32)
    closed = [call.args[0].value for call in api.CloseHandle.call_args_list]
    if failure is None:
        assert handle is not None
        assert closed == [process_handle]
        info = api.SetInformationJobObject.call_args.args[2]._obj
        assert info.BasicLimitInformation.LimitFlags == 0x2000
        assert api.CreateJobObjectW.call_args.args[0] is None  # Non-inheritable.
        assert api.AssignProcessToJobObject.call_args.args[0].value == job_handle
        handle.close()
        handle.close()
        assert api.CloseHandle.call_args.args[0].value == job_handle
        assert api.CloseHandle.call_count == 2
    else:
        assert handle is None
        assert (job_handle in closed) is (failure != "CreateJobObjectW")
        assert (process_handle in closed) is (failure == "AssignProcessToJobObject")
        if failure == "collision":
            api.SetInformationJobObject.assert_not_called()
            api.AssignProcessToJobObject.assert_not_called()


@pytest.mark.parametrize(
    ("handle", "error", "exited"), [(0, 2, True), (0, 5, False), (0, 0, False), (2**40, 0, False)]
)
def test_job_exit_requires_absence_not_access_denied(
    monkeypatch: pytest.MonkeyPatch, handle: int, error: int, exited: bool
) -> None:
    from types import SimpleNamespace

    api = SimpleNamespace(
        OpenJobObjectW=Mock(return_value=handle),
        GetLastError=Mock(return_value=error),
        CloseHandle=Mock(),
    )
    monkeypatch.setattr(jobs, "_kernel32", lambda: api)
    assert jobs.job_has_exited("Global\\omnigent-sdk-" + "a" * 32) is exited
    assert api.CloseHandle.call_count == bool(handle)
    api.OpenJobObjectW.assert_called_once_with(4, False, "Global\\omnigent-sdk-" + "a" * 32)


@pytest.mark.parametrize("record_exists", [False, True])
@pytest.mark.parametrize("job_exited", [False, True])
async def test_sweep_uses_job_receipt_even_without_parent_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record_exists: bool, job_exited: bool
) -> None:
    from omnigent.runtime.harnesses import process_manager as manager

    monkeypatch.setattr(manager, "IS_WINDOWS", True)
    record = tmp_path / "conv-6000.process.json"
    identity = {"pid": 6000, "create_time": 123.5}
    if record_exists:
        record.write_text(json.dumps(identity))
    (tmp_path / "conv-6000.job.json").write_text(
        json.dumps({**identity, "job_name": "Global\\omnigent-sdk-" + "a" * 32})
    )
    monkeypatch.setattr(
        processes.psutil, "Process", Mock(side_effect=processes.psutil.NoSuchProcess(6000))
    )
    absent = Mock(return_value=job_exited)
    monkeypatch.setattr(jobs, "job_has_exited", absent)
    assert await manager._kill_orphan_runners(tmp_path) is job_exited
    absent.assert_called_once()


@pytest.mark.parametrize(
    "fault", ["bad json", "wrong pid", "wrong birth", "bad name", "read denied"]
)
def test_invalid_job_receipt_keeps_evidence_without_signals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str
) -> None:
    record = tmp_path / "conv-6000.process.json"
    record.write_text(json.dumps({"pid": 6000, "create_time": 123.5}))
    receipt = tmp_path / "conv-6000.job.json"
    data = {"pid": 6000, "create_time": 123.5, "job_name": "Global\\omnigent-sdk-" + "a" * 32}
    if fault == "wrong pid":
        data["pid"] = True
    if fault == "wrong birth":
        data["create_time"] = float("nan")
    if fault == "bad name":
        data["job_name"] = "other-service"
    receipt.write_text("{" if fault == "bad json" else json.dumps(data))
    if fault == "read denied":
        read = Path.read_text

        def denied(path: Path, *args: object, **kwargs: object) -> str:
            if path == receipt:
                raise PermissionError("receipt unreadable")
            return read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
    lookup = Mock(side_effect=AssertionError("invalid receipt must not select a process"))
    monkeypatch.setattr(processes.psutil, "Process", lookup)
    assert not processes.reap_record(record)
    lookup.assert_not_called()
    assert record.exists() and receipt.exists()


@pytest.mark.windows_only
def test_native_empty_job_roundtrip() -> None:
    """Exercise native ABI/name lookup without assigning or killing a process."""
    import uuid
    from ctypes import wintypes

    name = "Global\\omnigent-sdk-" + uuid.uuid4().hex
    api = jobs._kernel32()
    handle = api.CreateJobObjectW(None, name)
    assert handle
    try:
        assert not jobs.job_has_exited(name)
    finally:
        assert api.CloseHandle(wintypes.HANDLE(handle))
    assert jobs.job_has_exited(name)


@pytest.mark.parametrize("fault", ["assign", "receipt"])
def test_setup_errors_keep_kernel_handle_and_report_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, fault: str
) -> None:
    from omnigent.runtime.harnesses import _runner

    monkeypatch.setattr(_runner, "IS_WINDOWS", True)
    monkeypatch.setattr(_runner, "_WINDOWS_JOB", None)
    handle = Mock()
    assign = Mock(return_value=handle)
    if fault == "assign":
        assign.side_effect = OSError("Win32 unavailable")
    monkeypatch.setattr(jobs, "create_kill_on_close_job", assign)
    monkeypatch.setattr(processes, "process_birth", lambda _pid: 123.5)
    # A file instead of a directory makes receipt creation fail after assignment.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    _runner._bind_windows_job(str(blocked / "conv-launch.process.json"))
    assert "keeping census-only recovery" in caplog.text
    assert (_runner._WINDOWS_JOB is handle) is (fault == "receipt")
    handle.close.assert_not_called()
    assert not list(tmp_path.glob("conv-*.job.json"))


def test_posix_startup_leaves_job_api_unused(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.runtime.harnesses import _runner

    monkeypatch.setattr(_runner, "IS_WINDOWS", False)
    assign = Mock(side_effect=AssertionError("POSIX containment must remain unchanged"))
    monkeypatch.setattr(jobs, "create_kill_on_close_job", assign)
    _runner._bind_windows_job(None)
    assign.assert_not_called()


@pytest.mark.parametrize(("reaped", "exited"), [(False, False), (True, False), (True, True)])
def test_live_job_root_reaps_by_birth_then_confirms_whole_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reaped: bool, exited: bool
) -> None:
    record = tmp_path / "conv-6000.process.json"
    data = {"pid": 6000, "create_time": 123.5}
    record.write_text(json.dumps(data))
    (tmp_path / "conv-6000.job.json").write_text(
        json.dumps({**data, "job_name": "Global\\omnigent-sdk-" + "a" * 32})
    )
    root = Mock()
    root.create_time.return_value = 123.5
    monkeypatch.setattr(processes.psutil, "Process", Mock(return_value=root))
    reap = Mock(return_value=reaped)
    monkeypatch.setattr(processes, "reap_tree", reap)
    absent = Mock(return_value=exited)
    monkeypatch.setattr(jobs, "job_has_exited", absent)
    assert processes.reap_record(record) is (reaped and exited)
    reap.assert_called_once_with(6000, 123.5, record_path=tmp_path / "conv-6000.job.json")
    assert absent.call_count == reaped


@pytest.mark.parametrize("scenario", ["redirector", "pid_reuse"])
async def test_launch_receipts_cover_redirector_and_pid_reuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from omnigent.runtime.harnesses import _runner
    from omnigent.runtime.harnesses import process_manager as manager

    monkeypatch.setattr(manager, "IS_WINDOWS", True)
    monkeypatch.setattr(_runner, "IS_WINDOWS", True)
    monkeypatch.setattr(_runner, "_WINDOWS_JOB", None)
    monkeypatch.setattr(manager, "_wait_for_bind", AsyncMock())
    monkeypatch.setattr(manager, "write_owner_entry", Mock())
    monkeypatch.setattr(manager, "_pick_free_tcp_port", lambda: 12345)
    monkeypatch.setattr(jobs, "create_kill_on_close_job", Mock(return_value=Mock()))
    monkeypatch.setattr(processes, "process_birth", lambda _pid: 123.5)
    monkeypatch.setattr(
        _runner, "os", SimpleNamespace(getpid=lambda: 6000 if scenario == "pid_reuse" else 6001)
    )
    pm = manager.HarnessProcessManager(tmp_parent=tmp_path)
    await pm.start(sweep_orphans=False)

    async def spawn(argv: list[str], _env: dict[str, str]) -> object:
        path = argv[argv.index("--process-record") + 1]
        _runner._bind_windows_job(path)
        return SimpleNamespace(pid=6000, returncode=0)

    monkeypatch.setattr(pm, "_spawn_harness_process", spawn)
    first = await pm._spawn_entry("example", "claude-sdk", None)
    if scenario == "pid_reuse":
        assert await pm._close_entry(first)
        monkeypatch.setattr(jobs, "create_kill_on_close_job", Mock(return_value=None))
        monkeypatch.setattr(processes, "process_birth", lambda _pid: 999.0)
        second = await pm._spawn_entry("example", "claude-sdk", None)
        assert second.process_record_path != first.process_record_path
    # Make root identities absent so only durable launch ownership can complete recovery.
    monkeypatch.setattr(
        processes.psutil, "Process", Mock(side_effect=processes.psutil.NoSuchProcess(6000))
    )
    monkeypatch.setattr(jobs, "job_has_exited", lambda _name: True)
    if scenario == "redirector":
        # Both redirector P and interpreter C exited, with no first census.
        assert await manager._kill_orphan_runners(pm.instance_dir)
    else:
        # A distinct launch record must still take the old census path.
        root = Mock(pid=6000)
        root.create_time.return_value = 999.0
        root.is_running.return_value = True
        root.children.return_value = []
        monkeypatch.setattr(processes.psutil, "Process", Mock(return_value=root))
        monkeypatch.setattr(processes.psutil, "wait_procs", Mock(return_value=([root], [])))
        assert await manager._kill_orphan_runners(pm.instance_dir)
        root.terminate.assert_called_once()
    await pm.shutdown()


def test_job_census_keeps_job_name_for_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = tmp_path / "conv-launch.job.json"
    name = "Global\\omnigent-sdk-" + "a" * 32
    receipt.write_text(json.dumps({"pid": 6000, "create_time": 123.5, "job_name": name}))
    root = Mock(pid=6000)
    root.create_time.return_value = 123.5
    root.is_running.return_value = True
    root.children.return_value = []
    monkeypatch.setattr(processes.psutil, "Process", Mock(return_value=root))
    monkeypatch.setattr(processes.psutil, "wait_procs", Mock(return_value=([root], [])))
    monkeypatch.setattr(jobs, "job_has_exited", lambda _name: False)
    record = tmp_path / "conv-launch.process.json"
    assert not processes.reap_record(record)
    data = json.loads(receipt.read_text())
    assert data["job_name"] == name
    assert data["members"] == [{"pid": 6000, "create_time": 123.5}]
    root.terminate.assert_called_once()
