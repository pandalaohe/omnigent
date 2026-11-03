"""Inbox ConPTY process ownership. Importing this module needs no Windows DLLs."""

from __future__ import annotations

import ctypes
import subprocess
import threading
from collections.abc import Mapping
from ctypes import wintypes
from typing import Protocol, cast

from omnigent.inner.sandbox import ContainmentHandle
from omnigent.inner.windows_jobobject_sandbox import create_kill_on_close_job

_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_PSEUDOCONSOLE_ATTRIBUTE = 0x00020016
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF


class _LibraryLoader(Protocol):
    def __call__(self, name: str, *, use_last_error: bool) -> ctypes.CDLL: ...


class _Coord(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _WindowsApi:
    def __init__(self) -> None:
        loader = cast(_LibraryLoader, vars(ctypes)["WinDLL"])
        self.kernel = loader("kernel32", use_last_error=True)
        handle, pointer, dword = wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD
        signatures = {
            "CreatePipe": (
                [ctypes.POINTER(handle), ctypes.POINTER(handle), pointer, dword],
                wintypes.BOOL,
            ),
            "CreatePseudoConsole": (
                [_Coord, handle, handle, dword, ctypes.POINTER(handle)],
                ctypes.c_long,
            ),
            "ResizePseudoConsole": ([handle, _Coord], ctypes.c_long),
            "ClosePseudoConsole": ([handle], None),
            "InitializeProcThreadAttributeList": (
                [pointer, dword, dword, ctypes.POINTER(ctypes.c_size_t)],
                wintypes.BOOL,
            ),
            "UpdateProcThreadAttribute": (
                [pointer, dword, ctypes.c_size_t, pointer, ctypes.c_size_t, pointer, pointer],
                wintypes.BOOL,
            ),
            "DeleteProcThreadAttributeList": ([pointer], None),
            "CreateProcessW": (
                [
                    wintypes.LPCWSTR,
                    wintypes.LPWSTR,
                    pointer,
                    pointer,
                    wintypes.BOOL,
                    dword,
                    pointer,
                    wintypes.LPCWSTR,
                    pointer,
                    ctypes.POINTER(_ProcessInfo),
                ],
                wintypes.BOOL,
            ),
            "ReadFile": ([handle, pointer, dword, ctypes.POINTER(dword), pointer], wintypes.BOOL),
            "WriteFile": ([handle, pointer, dword, ctypes.POINTER(dword), pointer], wintypes.BOOL),
            "CloseHandle": ([handle], wintypes.BOOL),
            "WaitForSingleObject": ([handle, dword], dword),
            "GetExitCodeProcess": ([handle, ctypes.POINTER(dword)], wintypes.BOOL),
            "TerminateProcess": ([handle, wintypes.UINT], wintypes.BOOL),
            "ResumeThread": ([handle], dword),
            "GetCurrentProcess": ([], handle),
            "DuplicateHandle": (
                [handle, handle, handle, ctypes.POINTER(handle), dword, wintypes.BOOL, dword],
                wintypes.BOOL,
            ),
            "GetLastError": ([], dword),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result

    def check(self, result: object, operation: str) -> None:
        if not result:
            raise OSError(int(self.kernel.GetLastError()), operation)

    def close_handle(self, handle: int | None) -> None:
        if handle is not None:
            self.kernel.CloseHandle(wintypes.HANDLE(handle))

    def pipe(self) -> tuple[int, int]:
        read, write = wintypes.HANDLE(), wintypes.HANDLE()
        self.check(
            self.kernel.CreatePipe(ctypes.byref(read), ctypes.byref(write), None, 0), "CreatePipe"
        )
        assert read.value is not None and write.value is not None
        return read.value, write.value


def _console_size(cols: int, rows: int) -> _Coord:
    if not (1 <= cols <= 32767 and 1 <= rows <= 32767):
        raise ValueError("terminal dimensions must be between 1 and 32767")
    return _Coord(cols, rows)


def _windows_cwd(cwd: str) -> str:
    if cwd.startswith("\\\\?\\UNC\\"):
        return "\\\\" + cwd[8:]
    return cwd[4:] if cwd.startswith("\\\\?\\") else cwd


def registered_user_environment() -> dict[str, str]:
    """Read the process token's registered login environment, without logging it."""
    api = _WindowsApi()
    loader = cast(_LibraryLoader, vars(ctypes)["WinDLL"])
    security = loader("advapi32", use_last_error=True)
    userenv = loader("userenv", use_last_error=True)
    security.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    security.OpenProcessToken.restype = wintypes.BOOL
    userenv.CreateEnvironmentBlock.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.BOOL,
    ]
    userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
    userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL
    token, block = wintypes.HANDLE(), ctypes.c_void_p()
    api.check(
        security.OpenProcessToken(api.kernel.GetCurrentProcess(), 0x000A, ctypes.byref(token)),
        "OpenProcessToken",
    )
    try:
        api.check(
            userenv.CreateEnvironmentBlock(ctypes.byref(block), token, False),
            "CreateEnvironmentBlock",
        )
        assert block.value is not None
        result: dict[str, str] = {}
        address = block.value
        while entry := ctypes.wstring_at(address):
            address += (len(entry) + 1) * ctypes.sizeof(ctypes.c_wchar)
            name, separator, value = entry.partition("=")
            if name and separator:
                result[name] = value
        return result
    finally:
        if block.value is not None:
            userenv.DestroyEnvironmentBlock(block)
        api.close_handle(token.value)


class PseudoConsoleProcess:
    """A raw-byte pseudoconsole whose suspended child joins a job before resume."""

    def __init__(self, api: _WindowsApi) -> None:
        self._api = api
        self._console: int | None = None
        self._input: int | None = None
        self._output: int | None = None
        self._process: int | None = None
        self._job: ContainmentHandle | None = None
        self._console_closed: threading.Event | None = None
        self._close_lock = threading.Lock()
        self._exit_status: int | None = None
        self.pid = 0

    @classmethod
    def spawn(
        cls, argv: list[str], cwd: str, env: Mapping[str, str], cols: int, rows: int
    ) -> PseudoConsoleProcess:
        if not argv or any("\0" in value for value in [*argv, cwd, *env, *env.values()]):
            raise ValueError("invalid terminal process arguments")
        if any("=" in name or not name for name in env):
            raise ValueError("invalid terminal environment name")
        api = _WindowsApi()
        result = cls(api)
        input_read: int | None = None
        output_write: int | None = None
        thread_handle: int | None = None
        attributes = None
        initialized = False
        try:
            input_read, result._input = api.pipe()
            result._output, output_write = api.pipe()
            console = wintypes.HANDLE()
            hr = api.kernel.CreatePseudoConsole(
                _console_size(cols, rows),
                wintypes.HANDLE(input_read),
                wintypes.HANDLE(output_write),
                0,
                ctypes.byref(console),
            )
            if hr != 0:
                raise OSError(hr, "CreatePseudoConsole")
            result._console = console.value
            api.close_handle(input_read)
            input_read = None
            api.close_handle(output_write)
            output_write = None
            size = ctypes.c_size_t()
            api.kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attributes = ctypes.create_string_buffer(size.value)
            api.check(
                api.kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)),
                "InitializeProcThreadAttributeList",
            )
            initialized = True
            api.check(
                api.kernel.UpdateProcThreadAttribute(
                    attributes,
                    0,
                    _PSEUDOCONSOLE_ATTRIBUTE,
                    wintypes.HANDLE(result._console),
                    ctypes.sizeof(wintypes.HANDLE),
                    None,
                    None,
                ),
                "UpdateProcThreadAttribute",
            )
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            process = _ProcessInfo()
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            environment = ctypes.create_unicode_buffer(
                "\0".join(
                    f"{name}={value}"
                    for name, value in sorted(env.items(), key=lambda item: item[0].upper())
                )
                + "\0\0"
            )
            api.check(
                api.kernel.CreateProcessW(
                    None,
                    command,
                    None,
                    None,
                    False,
                    _CREATE_SUSPENDED
                    | _CREATE_UNICODE_ENVIRONMENT
                    | _EXTENDED_STARTUPINFO_PRESENT,
                    environment,
                    _windows_cwd(cwd),
                    ctypes.byref(startup),
                    ctypes.byref(process),
                ),
                "CreateProcessW",
            )
            result._process, thread_handle, result.pid = (
                process.hProcess,
                process.hThread,
                process.dwProcessId,
            )
            result._job = create_kill_on_close_job(result.pid)
            if result._job is None:
                raise OSError("could not contain terminal process in a Job Object")
            if api.kernel.ResumeThread(wintypes.HANDLE(thread_handle)) == _INFINITE:
                raise OSError(int(api.kernel.GetLastError()), "ResumeThread")
            return result
        except BaseException:
            if result._process is not None:
                api.kernel.TerminateProcess(wintypes.HANDLE(result._process), 1)
            # Even failed startup may have console output which ClosePseudoConsole waits on.
            if result._output is not None:
                threading.Thread(target=result._discard_output, daemon=True).start()
            result.close()
            raise
        finally:
            if initialized:
                api.kernel.DeleteProcThreadAttributeList(attributes)
            api.close_handle(thread_handle)
            api.close_handle(input_read)
            api.close_handle(output_write)

    def _discard_output(self) -> None:
        try:
            while self.read_chunk():
                pass
        except OSError:
            pass

    def read_chunk(self) -> bytes:
        output = self._output
        if output is None:
            return b""
        buffer, count = ctypes.create_string_buffer(65536), wintypes.DWORD()
        ok = self._api.kernel.ReadFile(
            wintypes.HANDLE(output), buffer, len(buffer), ctypes.byref(count), None
        )
        if not ok:
            error = int(self._api.kernel.GetLastError())
            if error not in (6, 109, 232, 995):
                raise OSError(error, "ReadFile")
            return b""
        return buffer.raw[: count.value]

    def write(self, data: bytes) -> None:
        while data:
            if self._input is None:
                raise OSError("terminal input is closed")
            chunk, written = data[:65536], wintypes.DWORD()
            self._api.check(
                self._api.kernel.WriteFile(
                    wintypes.HANDLE(self._input), chunk, len(chunk), ctypes.byref(written), None
                ),
                "WriteFile",
            )
            if not written.value:
                raise OSError("terminal input made no progress")
            data = data[written.value :]

    def resize(self, cols: int, rows: int) -> None:
        size = _console_size(cols, rows)
        with self._close_lock:
            if self._console is None:
                return
            hr = self._api.kernel.ResizePseudoConsole(wintypes.HANDLE(self._console), size)
            if hr != 0:
                raise OSError(hr, "ResizePseudoConsole")

    def exit_code(self) -> int | None:
        with self._close_lock:
            if self._process is None:
                return self._exit_status
            state = self._api.kernel.WaitForSingleObject(wintypes.HANDLE(self._process), 0)
            if state == _WAIT_TIMEOUT:
                return None
            if state != 0:
                raise OSError("terminal process wait failed")
            code = wintypes.DWORD()
            self._api.check(
                self._api.kernel.GetExitCodeProcess(
                    wintypes.HANDLE(self._process), ctypes.byref(code)
                ),
                "GetExitCodeProcess",
            )
            self._exit_status = code.value
            return code.value

    def wait(self) -> int:
        # Closing a handle while another thread waits on it is undefined in Win32.
        duplicate = wintypes.HANDLE()
        with self._close_lock:
            if self._process is None:
                return self._exit_status if self._exit_status is not None else 1
            current = self._api.kernel.GetCurrentProcess()
            self._api.check(
                self._api.kernel.DuplicateHandle(
                    current,
                    wintypes.HANDLE(self._process),
                    current,
                    ctypes.byref(duplicate),
                    0,
                    False,
                    2,
                ),
                "DuplicateHandle",
            )
        try:
            if self._api.kernel.WaitForSingleObject(duplicate, _INFINITE) != 0:
                raise OSError("terminal process wait failed")
            code = wintypes.DWORD()
            self._api.check(
                self._api.kernel.GetExitCodeProcess(duplicate, ctypes.byref(code)),
                "GetExitCodeProcess",
            )
            self._exit_status = code.value
            return code.value
        finally:
            self._api.close_handle(duplicate.value)

    def close(self, *, output_drained: threading.Event | None = None) -> None:
        with self._close_lock:
            if self._console is not None:
                console, self._console = self._console, None
                closed = self._console_closed = threading.Event()

                def close_console() -> None:
                    self._api.kernel.ClosePseudoConsole(wintypes.HANDLE(console))
                    closed.set()

                threading.Thread(target=close_console, daemon=True).start()
            done = self._console_closed
            if done is not None and not done.wait(5):
                if self._job is not None:
                    self._job.close()
                if not done.wait(5):
                    raise TimeoutError("pseudoconsole close did not finish")
            if self._job is not None:
                self._job.close()
                self._job = None
            if output_drained is not None and not output_drained.wait(5):
                raise TimeoutError("terminal output did not drain")
            if self._process is not None:
                self._api.kernel.WaitForSingleObject(wintypes.HANDLE(self._process), 5000)
                code = wintypes.DWORD()
                if self._api.kernel.GetExitCodeProcess(
                    wintypes.HANDLE(self._process), ctypes.byref(code)
                ):
                    self._exit_status = code.value
            for name in ("_input", "_output", "_process"):
                self._api.close_handle(getattr(self, name))
                setattr(self, name, None)
