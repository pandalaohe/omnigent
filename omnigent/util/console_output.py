"""Decode captured console output, including localized Windows tools."""

from __future__ import annotations

import ctypes

from omnigent._platform import IS_WINDOWS


def _windows_code_pages() -> tuple[int, ...]:
    if not IS_WINDOWS:
        return ()
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        return (kernel.GetConsoleOutputCP(), kernel.GetOEMCP(), kernel.GetACP())
    except (AttributeError, OSError):
        return ()


def decode_console_output(output: bytes | str) -> str:
    """Prefer UTF-8, then Windows console/OEM and ANSI, with a lossy fallback.

    Already decoded output from a text-mode subprocess is returned unchanged.
    """
    if isinstance(output, str):
        return output
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for code_page in dict.fromkeys(_windows_code_pages()):
        if not code_page:
            continue
        try:
            return output.decode(f"cp{code_page}")
        except (UnicodeDecodeError, LookupError):
            continue
    return output.decode("utf-8", errors="replace")
