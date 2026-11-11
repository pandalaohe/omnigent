"""Atomic text files restricted to the current user (and SYSTEM on Windows)."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import uuid
from pathlib import Path


def _windows_private_temp(path: Path) -> tuple[int, Path]:
    if sys.platform != "win32":
        raise OSError("Windows private files require Windows")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", wintypes.LPVOID),
            ("inherit", wintypes.BOOL),
        ]

    win_dll = ctypes.WinDLL  # type: ignore[missing-attribute]
    win_error = ctypes.WinError  # type: ignore[missing-attribute]
    last_error = ctypes.get_last_error  # type: ignore[missing-attribute]
    kernel = win_dll("kernel32", use_last_error=True)
    advapi = win_dll("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        wintypes.LPVOID,
    ]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE

    token = wintypes.HANDLE()
    sid_text = wintypes.LPWSTR()
    descriptor = wintypes.LPVOID()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise win_error(last_error())
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise win_error(last_error())
        user = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, user, size, ctypes.byref(size)):
            raise win_error(last_error())
        sid = ctypes.cast(user, ctypes.POINTER(wintypes.LPVOID))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise win_error(last_error())
        sddl = f"O:{sid_text.value}D:P(A;;FA;;;{sid_text.value})(A;;FA;;;SY)"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise win_error(last_error())
        # Tighten an existing destination before writing any replacement bytes.
        if path.exists() and not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise win_error(last_error())
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        handle = kernel.CreateFileW(
            str(temp), 0x40000000, 0, ctypes.byref(attributes), 1, 0x80, None
        )
        if handle == wintypes.HANDLE(-1).value:
            raise win_error(last_error())
        try:
            open_handle = msvcrt.open_osfhandle  # type: ignore[missing-attribute]
            binary_mode = os.O_BINARY  # type: ignore[missing-attribute]
            return open_handle(handle, os.O_WRONLY | binary_mode), temp
        except BaseException:
            kernel.CloseHandle(handle)
            temp.unlink(missing_ok=True)
            raise
    finally:
        if token:
            kernel.CloseHandle(token)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
        if descriptor:
            kernel.LocalFree(descriptor)


def write_private_text(path: Path, text: str) -> None:
    """Replace a text file atomically, with private permissions from creation."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        fd, temp = _windows_private_temp(path)
    else:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp = Path(name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
            if path.exists():
                path.chmod(0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
