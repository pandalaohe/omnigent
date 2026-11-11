"""Supervisor admission and bounded draining for host restarts."""

from __future__ import annotations

import os
import sys

REQUESTER_GRACE_S = 10
_service_entry = False


def mark_service_entry() -> None:
    """Mark this process as the foreground service entry, never its children."""
    global _service_entry
    _service_entry = True


def supervisor_kind() -> str | None:
    """Return a supervisor only when this process was launched as its service."""
    if _service_entry:
        if sys.platform == "darwin" and os.getppid() == 1:
            return "launchd"
        if sys.platform == "linux" and os.environ.get("INVOCATION_ID"):
            return "systemd"
        if (
            sys.platform == "win32"
            and os.environ.get("OMNIGENT_HOST_SERVICE_BACKEND") == "windows_task"
        ):
            return "windows_task"
    return None
