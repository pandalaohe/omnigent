"""Atomic owner-only files, including Windows destinations with inherited ACLs."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import tempfile
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from uuid import uuid4

IS_WINDOWS = os.name == "nt"


@cache
def windows_user_sid() -> str:
    """Read the invoking user's SID without depending on localized account names."""
    result = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    match = re.search(rb"S-1-\d+(?:-\d+)+", result.stdout)
    if match is None:
        raise OSError("Cannot determine the current Windows user SID.")
    return match[0].decode("ascii")


@contextmanager
def _windows_descriptor() -> Iterator[ctypes.c_void_p]:
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    sddl = f"D:P(A;;FA;;;SY)(A;;FA;;;{windows_user_sid()})"
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise OSError("Cannot create an owner-only Windows security descriptor.")
    try:
        yield descriptor
    finally:
        kernel.LocalFree(descriptor)


def protect_windows_file(path: Path) -> None:
    """Replace inherited permissions with current-user and SYSTEM full control."""
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    apply = advapi.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    with _windows_descriptor() as descriptor:
        # PROTECTED_DACL_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION.
        if not apply(str(path), 0x80000004, descriptor):
            raise OSError(f"Cannot protect credential file {path}.")


def _create_windows_private_temp(path: Path) -> tuple[int, Path]:
    """Create with the DACL already applied; no inherited-access window exists."""
    import msvcrt
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", ctypes.c_void_p),
            ("inherit", wintypes.BOOL),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    temp = path.with_name(f".{path.name}.{uuid4().hex}")
    with _windows_descriptor() as descriptor:
        security = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        # GENERIC_WRITE, no sharing, CREATE_NEW, FILE_ATTRIBUTE_NORMAL.
        handle = create(str(temp), 0x40000000, 0, ctypes.byref(security), 1, 0x80, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise OSError(f"Cannot create a protected temporary file for {path}.")
    try:
        # Ownership transfers to the CRT descriptor, then to os.fdopen.
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)  # type: ignore[attr-defined]
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return fd, temp


def atomic_write_private(path: Path, content: bytes) -> None:
    """Protect the actual destination and empty temporary file before writing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"Refusing a symbolic-link credential destination: {path}")
    if path.exists():
        if IS_WINDOWS:
            protect_windows_file(path)
        else:
            path.chmod(0o600)
    if IS_WINDOWS:
        fd, temp = _create_windows_private_temp(path)
    else:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp = Path(name)
    try:
        if not IS_WINDOWS:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        if IS_WINDOWS:
            protect_windows_file(path)
        else:
            path.chmod(0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp.unlink(missing_ok=True)
        except OSError as exc:
            warnings.warn(f"Could not remove temporary file {temp}: {exc}", stacklevel=2)
