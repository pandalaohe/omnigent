"""Exercise the real spawn transaction with a fake Win32 API on every platform."""

import subprocess
from unittest.mock import Mock

import pytest

from omnigent.inner import windows_jobobject_sandbox as jobs
from omnigent.terminals import conpty_process as process


@pytest.fixture
def win32(monkeypatch):
    api = Mock()
    kernel = api.kernel
    api.pipe.side_effect = [(11, 12), (13, 14)]

    def check(result, operation):
        if not result:
            raise OSError(operation)

    api.check.side_effect = check

    def console(size, read, write, flags, handle):
        handle._obj.value = 21
        return 0

    kernel.CreatePseudoConsole.side_effect = console

    def attributes(buffer, count, flags, size):
        size._obj.value = 64
        return buffer is not None

    kernel.InitializeProcThreadAttributeList.side_effect = attributes

    def create(app, command, pa, ta, inherit, flags, env, cwd, startup, info):
        api.spawn_args = (
            command.value,
            inherit,
            flags,
            env[:],
            cwd,
            startup._obj.StartupInfo.dwFlags,
            (
                startup._obj.StartupInfo.hStdInput,
                startup._obj.StartupInfo.hStdOutput,
                startup._obj.StartupInfo.hStdError,
            ),
        )
        info._obj.hProcess, info._obj.hThread, info._obj.dwProcessId = 31, 32, 41
        return True

    kernel.CreateProcessW.side_effect = create
    kernel.ResumeThread.return_value = 1
    kernel.WaitForSingleObject.return_value = 0
    kernel.ReadFile.return_value = False
    kernel.GetLastError.return_value = 109
    monkeypatch.setattr(process, "_WindowsApi", lambda: api)
    monkeypatch.setattr(jobs, "_kernel32", lambda: kernel)
    monkeypatch.setattr(jobs, "_get_last_error", lambda: 0)
    kernel.CreateJobObjectW.return_value = 51
    kernel.SetInformationJobObject.return_value = True
    kernel.OpenProcess.return_value = 52
    kernel.AssignProcessToJobObject.return_value = True
    return api


@pytest.mark.parametrize(
    "failure",
    ["CreateJobObjectW", "SetInformationJobObject", "OpenProcess", "AssignProcessToJobObject"],
)
def test_job_failure_never_resumes_and_closes_spawn_handles(win32, failure):
    getattr(win32.kernel, failure).return_value = 0
    with pytest.raises(OSError, match="could not contain"):
        process.PseudoConsoleProcess.spawn(["cmd.exe"], "C:\\work", {}, 80, 24)
    win32.kernel.ResumeThread.assert_not_called()
    win32.kernel.TerminateProcess.assert_called_once()
    win32.kernel.ClosePseudoConsole.assert_called_once()
    closed = [call.args[0] for call in win32.close_handle.call_args_list]
    assert all(handle in closed for handle in (11, 12, 13, 14, 31, 32))
    win32.kernel.DeleteProcThreadAttributeList.assert_called_once()


def test_spawn_quotes_argv_normalizes_verbatim_cwd_and_assigns_before_resume(win32):
    events = []
    win32.kernel.AssignProcessToJobObject.side_effect = lambda *args: events.append("job") or True
    win32.kernel.ResumeThread.side_effect = lambda *args: events.append("resume") or 1
    argv = ["C:\\Program Files\\PowerShell\\pwsh.exe", "-NoLogo", "a b", 'a"b', "末尾"]
    child = process.PseudoConsoleProcess.spawn(
        argv, "\\\\?\\C:\\work space", {"PATH": "test"}, 90, 30
    )
    try:
        command, inherit, flags, env, cwd, std_flags, std_handles = win32.spawn_args
        assert command == subprocess.list2cmdline(argv)
        assert "PATH=test\0" in env
        assert not inherit and flags & process._CREATE_SUSPENDED
        assert std_flags & 0x100 and cwd == "C:\\work space"
        assert std_handles == (None, None, None)
        assert events == ["job", "resume"]
        assert child.pid == 41
    finally:
        child.close()
    assert process._windows_cwd("\\\\?\\UNC\\server\\share") == "\\\\server\\share"


def test_resume_failure_terminates_contained_child(win32):
    win32.kernel.ResumeThread.return_value = process._INFINITE
    with pytest.raises(OSError, match="ResumeThread"):
        process.PseudoConsoleProcess.spawn(["cmd.exe"], "C:\\work", {}, 80, 24)
    win32.kernel.TerminateProcess.assert_called_once()
    assert win32.kernel.CloseHandle.call_count >= 2  # process lookup + owned job


def test_console_resize_rejects_invalid_dimensions():
    for cols, rows in [(0, 24), (80, 0), (32768, 24), (-1, 24)]:
        with pytest.raises(ValueError):
            process._console_size(cols, rows)
