"""Install the Omnigent host as a per-user operating-system service."""

from __future__ import annotations

import os
import platform
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from omnigent.process_logging import data_dir

LAUNCHD_LABEL = "ai.omnigent.host"
SYSTEMD_UNIT = "omnigent-host.service"
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LAUNCHD_STOP_TIMEOUT_S = 5.0
_LAUNCHD_STOP_POLL_S = 0.1


class HostServiceError(RuntimeError):
    """Raised when a host service cannot be installed or removed."""


@dataclass(frozen=True)
class HostService:
    """Description of the current platform's per-user host service."""

    kind: Literal["launchd", "systemd_user"]
    path: Path
    label: str
    log_path: Path | None = None


def _current_uid() -> int:
    """Return the POSIX uid used to address a launchd GUI domain."""
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise HostServiceError("The current platform does not expose a POSIX user id.")
    return int(getuid())


def _service_for_current_platform() -> HostService:
    """Return the current platform's per-user service description."""
    system = platform.system()
    if system == "Darwin":
        home = Path(os.environ.get("HOME", Path.home())).expanduser()
        log_path = data_dir() / "logs" / "host" / "service.log"
        return HostService(
            kind="launchd",
            path=home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist",
            label=LAUNCHD_LABEL,
            log_path=log_path,
        )
    if system == "Linux":
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return HostService(
            kind="systemd_user",
            path=config_home / "systemd" / "user" / SYSTEMD_UNIT,
            label=SYSTEMD_UNIT,
        )
    raise HostServiceError(
        f"Host services are supported on macOS and Linux, not {system or 'this platform'}."
    )


def _service_command(server_url: str | None) -> list[str]:
    """Build the persistent service entry-point command."""
    mode = ["--server", server_url] if server_url else ["--local"]
    return [sys.executable, "-m", "omnigent.host.service_entry", *mode]


def _clean_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Validate environment values before persisting them in a service file."""
    cleaned: dict[str, str] = {}
    for key, value in environment.items():
        if not _ENV_NAME_RE.fullmatch(key):
            raise HostServiceError(f"Cannot persist invalid environment variable name {key!r}.")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise HostServiceError(f"Cannot persist multiline environment variable {key!r}.")
        cleaned[key] = value
    return dict(sorted(cleaned.items()))


def _launchd_payload(
    service: HostService,
    *,
    command: Sequence[str],
    environment: Mapping[str, str],
) -> bytes:
    """Render a launchd user-agent plist."""
    assert service.log_path is not None
    payload = {
        "Label": service.label,
        "ProgramArguments": list(command),
        "EnvironmentVariables": dict(environment),
        "RunAtLoad": True,
        # service_entry maps permanent host failures to a successful exit.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(service.log_path),
        "StandardErrorPath": str(service.log_path),
        # No ProcessType: the default (Standard) keeps runner/harness children
        # out of the background QoS band, which would starve their deadlines.
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _systemd_quote(value: str, *, escape_dollar: bool = False) -> str:
    """Quote one systemd unit value without invoking a shell."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if escape_dollar:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def _systemd_unit(
    *,
    command: Sequence[str],
    environment: Mapping[str, str],
) -> bytes:
    """Render a systemd user service unit."""
    env_lines = [
        f"Environment={_systemd_quote(f'{key}={value}')}" for key, value in environment.items()
    ]
    lines = [
        "[Unit]",
        "Description=Omnigent host",
        "Wants=network-online.target",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        *env_lines,
        "ExecStart=" + " ".join(_systemd_quote(part, escape_dollar=True) for part in command),
        "Restart=on-failure",
        "RestartPreventExitStatus=78 143",
        "RestartSec=10s",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines).encode()


def _atomic_write(path: Path, content: bytes) -> None:
    """Atomically write a private service definition."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path.exists():
            temp_path.unlink()


def _run_checked(args: Sequence[str]) -> None:
    """Run one service-manager command and surface a concise failure."""
    try:
        subprocess.run(
            list(args),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise HostServiceError(f"Required service manager {args[0]!r} was not found.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise HostServiceError(
            f"Service manager command failed ({' '.join(args)}){suffix}"
        ) from exc


def _run_best_effort(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run an idempotent service-manager cleanup command."""
    try:
        return subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise HostServiceError(f"Required service manager {args[0]!r} was not found.") from exc


def _wait_for_launchd_stop(service_target: str) -> None:
    """Wait for launchd's asynchronous ``bootout`` to remove a service."""
    deadline = time.monotonic() + _LAUNCHD_STOP_TIMEOUT_S
    while _run_best_effort(["launchctl", "print", service_target]).returncode == 0:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HostServiceError(
                f"launchd service {LAUNCHD_LABEL!r} is still running after "
                f"{_LAUNCHD_STOP_TIMEOUT_S:g}s."
            )
        time.sleep(min(_LAUNCHD_STOP_POLL_S, remaining))


def _restore_file(path: Path, previous: bytes | None) -> None:
    """Restore a service definition after a manager command fails."""
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write(path, previous)


def _enable_launchd(service: HostService, content: bytes) -> None:
    assert service.log_path is not None
    service.log_path.parent.mkdir(parents=True, exist_ok=True)
    previous = service.path.read_bytes() if service.path.exists() else None
    domain = f"gui/{_current_uid()}"
    _run_best_effort(["launchctl", "bootout", f"{domain}/{service.label}"])
    _atomic_write(service.path, content)
    try:
        _run_checked(["launchctl", "bootstrap", domain, str(service.path)])
    except HostServiceError:
        _restore_file(service.path, previous)
        if previous is not None:
            _run_best_effort(["launchctl", "bootstrap", domain, str(service.path)])
        raise


def _enable_systemd(service: HostService, content: bytes) -> None:
    previous = service.path.read_bytes() if service.path.exists() else None
    changed = previous != content
    _atomic_write(service.path, content)
    try:
        _run_checked(["systemctl", "--user", "daemon-reload"])
        _run_checked(["systemctl", "--user", "enable", "--now", service.label])
        if previous is not None and changed:
            _run_checked(["systemctl", "--user", "restart", service.label])
    except HostServiceError:
        _restore_file(service.path, previous)
        _run_best_effort(["systemctl", "--user", "daemon-reload"])
        raise


def _record_service(service: HostService) -> None:
    """Add the service to the uninstall ledger."""
    from omnigent.install_ledger import LaunchAgentEntry, record_launch_agent

    try:
        record_launch_agent(
            LaunchAgentEntry(
                kind=service.kind,
                path=str(service.path),
                label=service.label,
                source="recorded",
                confidence="certain",
            )
        )
    except OSError as exc:
        raise HostServiceError(
            f"The service was enabled, but its uninstall record could not be written: {exc}"
        ) from exc


def _forget_service(service: HostService) -> None:
    """Remove the service from install ledgers."""
    from omnigent.install_ledger import remove_launch_agent

    try:
        remove_launch_agent(kind=service.kind, label=service.label)
    except OSError as exc:
        raise HostServiceError(
            f"The service was disabled, but its uninstall record could not be updated: {exc}"
        ) from exc


def enable_user_host_service(
    server_url: str | None,
    *,
    environment: Mapping[str, str],
) -> HostService:
    """Install, enable, and start the current user's host service."""
    service = _service_for_current_platform()
    command = _service_command(server_url)
    clean_environment = _clean_environment(environment)
    if service.kind == "launchd":
        content = _launchd_payload(
            service,
            command=command,
            environment=clean_environment,
        )
        _enable_launchd(service, content)
    else:
        content = _systemd_unit(command=command, environment=clean_environment)
        _enable_systemd(service, content)
    _record_service(service)
    return service


def disable_user_host_service() -> HostService:
    """Stop, disable, and remove the current user's host service."""
    service = _service_for_current_platform()
    if service.kind == "launchd":
        domain = f"gui/{_current_uid()}"
        service_target = f"{domain}/{service.label}"
        _run_best_effort(["launchctl", "bootout", service_target])
        _wait_for_launchd_stop(service_target)
        service.path.unlink(missing_ok=True)
    else:
        disable_args = ["systemctl", "--user", "disable", "--now", service.label]
        if service.path.exists():
            _run_checked(disable_args)
        else:
            _run_best_effort(disable_args)
        service.path.unlink(missing_ok=True)
        _run_checked(["systemctl", "--user", "daemon-reload"])
    _forget_service(service)
    return service


def pause_user_host_service() -> HostService | None:
    """Stop the current user's host service without changing its definition.

    Returns ``None`` when no service definition is installed.  The custom
    updater uses this narrower operation so an upgrade can stop the process,
    replace the tool, and start the exact same launchd/systemd definition.
    """
    service = _service_for_current_platform()
    if not service.path.exists():
        return None
    if service.kind == "launchd":
        domain = f"gui/{_current_uid()}"
        service_target = f"{domain}/{service.label}"
        _run_best_effort(["launchctl", "bootout", service_target])
        _wait_for_launchd_stop(service_target)
    else:
        _run_checked(["systemctl", "--user", "stop", service.label])
    return service


def resume_user_host_service(service: HostService) -> None:
    """Start a service previously returned by :func:`pause_user_host_service`."""
    if not service.path.exists():
        raise HostServiceError(f"Host service definition disappeared: {service.path}")
    if service.kind == "launchd":
        _run_checked(
            [
                "launchctl",
                "bootstrap",
                f"gui/{_current_uid()}",
                str(service.path),
            ]
        )
    else:
        _run_checked(["systemctl", "--user", "daemon-reload"])
        _run_checked(["systemctl", "--user", "start", service.label])
