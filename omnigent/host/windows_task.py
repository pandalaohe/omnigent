"""Per-user Windows Task Scheduler implementation of the host service."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import subprocess
import sys
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import psutil

from omnigent.host.service import HostService, HostServiceError
from omnigent.util.console_output import decode_console_output
from omnigent.util.private_files import atomic_write_private, windows_user_sid

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
SUPERVISOR_ENV = "OMNIGENT_HOST_SERVICE_BACKEND"


def description() -> HostService:
    from omnigent.config import global_config_path

    return HostService(
        "windows_task",
        global_config_path().parent / "host-service" / "task.xml",
        rf"\Omnigent Host ({windows_user_sid()})",
    )


def env_path(service: HostService) -> Path:
    return service.path.with_name("environment.json")


def _run(args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["schtasks.exe", *args],
            capture_output=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostServiceError(f"Windows task scheduler unavailable: {exc}") from exc
    if check and result.returncode:
        raise HostServiceError(
            f"Windows task scheduler failed to {args[0]} ({result.returncode})."
        )
    return result


def query(service: HostService) -> ET.Element | None:
    result = _run(["/Query", "/TN", service.label, "/XML"], check=False)
    if result.returncode:
        # A missing task and access denied have the same exit code. A successful
        # enumeration must also show the name absent before we call it missing.
        listing = _run(["/Query", "/FO", "CSV", "/NH"])
        rows = csv.reader(io.StringIO(decode_console_output(listing.stdout)))
        if any(row and row[0].casefold() == service.label.casefold() for row in rows):
            raise HostServiceError("Cannot read the installed Windows host task.")
        return None
    try:
        return ET.fromstring(result.stdout)
    except ET.ParseError as exc:
        raise HostServiceError("Cannot parse the Windows host task definition.") from exc


def installed(service: HostService) -> tuple[HostService, str | None] | None:
    task = query(service)
    if task is None:
        return None
    try:
        metadata = json.loads(
            task.findtext(f"{{{TASK_NS}}}RegistrationInfo/{{{TASK_NS}}}Description") or ""
        )
        target = metadata["server_url"]
        if target is not None and not isinstance(target, str):
            raise ValueError("Invalid server URL")
        saved_env = Path(metadata["env_file"])
        if not saved_env.is_absolute() or saved_env.name != "environment.json":
            raise ValueError("Invalid environment file")
        service = replace(service, path=saved_env.with_name("task.xml"))
    except (ValueError, KeyError, TypeError) as exc:
        raise HostServiceError("Cannot read the installed Windows host task target.") from exc
    return service, target


def payload(service: HostService, server_url: str | None) -> bytes:
    ET.register_namespace("", TASK_NS)
    task = ET.Element(f"{{{TASK_NS}}}Task", version="1.2")

    def node(parent: ET.Element, name: str, value: str | None = None) -> ET.Element:
        child = ET.SubElement(parent, f"{{{TASK_NS}}}{name}")
        child.text = value
        return child

    info = node(task, "RegistrationInfo")
    node(
        info,
        "Description",
        json.dumps({"server_url": server_url or None, "env_file": str(env_path(service))}),
    )
    trigger = node(node(task, "Triggers"), "LogonTrigger")
    node(trigger, "UserId", windows_user_sid())
    principal = ET.SubElement(node(task, "Principals"), f"{{{TASK_NS}}}Principal", id="User")
    node(principal, "UserId", windows_user_sid())
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    settings = node(task, "Settings")
    for name, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("ExecutionTimeLimit", "PT0S"),
        ("Enabled", "true"),
        ("Hidden", "true"),
    ):
        node(settings, name, value)
    actions = ET.SubElement(task, f"{{{TASK_NS}}}Actions", Context="User")
    action = node(actions, "Exec")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise HostServiceError(f"The tool environment has no windowless interpreter: {pythonw}")
    node(action, "Command", str(pythonw))
    mode = ["--server", server_url] if server_url else ["--local"]
    node(
        action,
        "Arguments",
        subprocess.list2cmdline(
            [
                "-P",
                "-m",
                "omnigent.host.service_entry",
                *mode,
                "--env-file",
                str(env_path(service)),
            ]
        ),
    )
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def _cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        warnings.warn(f"Could not remove host service file {path}: {exc}", stacklevel=2)


def enable(service: HostService, server_url: str | None, environment: Mapping[str, str]) -> None:
    content = payload(service, server_url)
    previous = query(service)
    snapshot = installed(service) if previous is not None else None
    previous_service = snapshot[0] if snapshot else service
    previous_env = (
        env_path(previous_service).read_bytes() if env_path(previous_service).exists() else None
    )
    if previous is not None:
        stop(service)
    try:
        atomic_write_private(env_path(service), json.dumps(dict(environment)).encode("utf-8"))
        atomic_write_private(service.path, content)
        _run(["/Create", "/TN", service.label, "/XML", str(service.path), "/F"])
        start(service)
    except (HostServiceError, OSError):
        if previous is not None:
            try:
                if previous_env is not None:
                    atomic_write_private(env_path(previous_service), previous_env)
                atomic_write_private(
                    previous_service.path, ET.tostring(previous, encoding="utf-16")
                )
                _run(["/Create", "/TN", service.label, "/XML", str(previous_service.path), "/F"])
                start(service)
            except (HostServiceError, OSError) as recovery:
                warnings.warn(
                    f"Could not restore the previous host task: {recovery}", stacklevel=2
                )
        else:
            # If registration succeeded but Run failed, do not leave an enabled
            # task whose environment we are about to remove.
            if query(service) is not None:
                stop(service)
                _run(["/Delete", "/TN", service.label, "/F"])
            _cleanup(service.path)
            _cleanup(env_path(service))
        raise
    if previous_service.path != service.path:
        _cleanup(env_path(previous_service))
        _cleanup(previous_service.path)


def stop(service: HostService) -> None:
    from omnigent.host.windows_upgrade import capture_host_tree, stop_host_tree

    suspended: list[psutil.Process] = []
    try:
        processes = capture_host_tree()
        task = query(service)
        if task is not None:
            _run(["/Change", "/TN", service.label, "/DISABLE"])
            task = query(service)
            if (
                task is not None
                and task.findtext(f"{{{TASK_NS}}}Settings/{{{TASK_NS}}}Enabled") != "false"
            ):
                raise HostServiceError("Windows host task did not become disabled.")
            # Freeze every supervisor/trampoline before the final child capture.
            # End alone kills only the scheduler action, leaving its children.
            for proc in capture_host_tree():
                try:
                    args = proc.cmdline()
                    if "omnigent.host.service_entry" in args and "--service-worker" not in args:
                        proc.suspend()
                        suspended.append(proc)
                except psutil.NoSuchProcess:
                    continue
            processes.extend(capture_host_tree())
            if task is not None:
                _run(["/End", "/TN", service.label])
        stop_host_tree(processes)
    except (psutil.Error, OSError, RuntimeError) as exc:
        if isinstance(exc, HostServiceError):
            raise
        raise HostServiceError(f"Windows host tree could not stop: {exc}") from exc
    finally:
        for proc in suspended:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                proc.resume()


def start(service: HostService) -> None:
    _run(["/Change", "/TN", service.label, "/ENABLE"])
    _run(["/Run", "/TN", service.label])


def disable(service: HostService) -> None:
    stop(service)
    if query(service) is not None:
        _run(["/Delete", "/TN", service.label, "/F"])
    try:
        env_path(service).unlink(missing_ok=True)
        service.path.unlink(missing_ok=True)
    except OSError as exc:
        raise HostServiceError(
            "The Windows host task was removed, but its saved files remain. "
            f"Run host disable again after releasing the file lock: {exc}"
        ) from exc
