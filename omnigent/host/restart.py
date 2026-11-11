"""Supervisor admission and bounded draining for host restarts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import psutil

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
    if sys.platform == "win32" and _windows_loop_supervises():
        return "windows-wrapper"
    return None


def _windows_loop_supervises() -> bool:
    """Admit the known scheduled-task loop only for one of its descendants."""
    root = Path.home() / ".omnigent" / "autostart"
    loop_script = root / "Start-OmnigentHostDefault.ps1"
    launcher = root / "Launch-OmnigentHost.pyw"
    if (root / "disabled").exists() or not loop_script.is_file() or not launcher.is_file():
        return False
    try:
        ancestor_uses_loop = any(
            str(loop_script).casefold() in {arg.casefold() for arg in parent.cmdline()}
            for parent in psutil.Process().parents()
        )
        if not ancestor_uses_loop:
            return False
        task = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", r"\Omnigent Host (default server)", "/XML"],
            capture_output=True,
            timeout=3,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if task.returncode != 0:
            return False
        xml = ElementTree.fromstring(task.stdout)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        arguments = xml.findtext("t:Actions/t:Exec/t:Arguments", default="", namespaces=ns)
        enabled = xml.findtext("t:Settings/t:Enabled", default="true", namespaces=ns)
        expanded = os.path.expandvars(arguments).replace('"', "")
        return enabled.lower() == "true" and str(launcher).casefold() in expanded.casefold()
    except (OSError, psutil.Error, subprocess.SubprocessError, ElementTree.ParseError):
        return False
