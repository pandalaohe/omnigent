"""Supervisor admission and bounded draining for host restarts."""

from __future__ import annotations

import os
import subprocess
import sys

import psutil

REQUESTER_GRACE_S = 10
_service_entry = False


def mark_service_entry() -> None:
    """Mark this process as the foreground service entry, never its children."""
    global _service_entry
    _service_entry = True


def _launchd_job_is_ancestor() -> bool:
    """Check the launchd job PID when a launcher spawns the service entry."""
    from omnigent.host.service import LAUNCHD_LABEL

    try:
        jobs = subprocess.run(
            ["/bin/launchctl", "list"], check=True, capture_output=True, text=True, timeout=5
        )
        for row in jobs.stdout.splitlines():
            columns = row.split()
            if len(columns) == 3 and columns[2] == LAUNCHD_LABEL:
                return any(str(parent.pid) == columns[0] for parent in psutil.Process().parents())
    except (OSError, subprocess.SubprocessError, psutil.Error):
        return False
    return False


def supervisor_kind() -> str | None:
    """Return a supervisor only when this process was launched as its service."""
    if _service_entry:
        if sys.platform == "darwin" and (os.getppid() == 1 or _launchd_job_is_ancestor()):
            return "launchd"
        if sys.platform == "linux" and os.environ.get("INVOCATION_ID"):
            return "systemd"
        if (
            sys.platform == "win32"
            and os.environ.get("OMNIGENT_HOST_SERVICE_BACKEND") == "windows_task"
        ):
            return "windows_task"
    return None
