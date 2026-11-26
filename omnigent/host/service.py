"""Install the Omnigent host as a per-user operating-system service."""

from __future__ import annotations

import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from omnigent.cli_invocation import WRAPPER_COMMAND_ENV
from omnigent.config import global_config_path, load_global_config
from omnigent.process_logging import data_dir

LAUNCHD_LABEL = "ai.omnigent.host"
SYSTEMD_UNIT = "omnigent-host.service"
# launchctl bootout returns before the job is gone; poll for the unload to land.
_LAUNCHD_UNLOAD_TIMEOUT = 10.0
_LAUNCHD_UNLOAD_POLL_INTERVAL = 0.2
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class HostServiceError(RuntimeError):
    """Raised when a host service cannot be installed or removed."""


@dataclass(frozen=True)
class HostService:
    """Description of the current platform's per-user host service."""

    kind: Literal["launchd", "systemd_user", "windows_task"]
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
    if system == "Windows":
        from omnigent.host.windows_task import description

        try:
            return description()
        except (OSError, subprocess.SubprocessError) as exc:
            raise HostServiceError(f"Cannot identify the Windows host service: {exc}") from exc
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
        "Host services are supported on macOS, Linux and Windows, "
        f"not {system or 'this platform'}."
    )


def _wrapper_command(environment: Mapping[str, str]) -> list[str] | None:
    """Return the configured CLI wrapper, resolved to an absolute path.

    A wrapper's cache venv can be deleted between boots, so don't pin its interpreter.
    """
    configured = environment.get(WRAPPER_COMMAND_ENV, "").strip()
    if not configured:
        return None
    try:
        executable, *arguments = shlex.split(configured)
    except ValueError as exc:
        raise HostServiceError(f"Cannot parse {WRAPPER_COMMAND_ENV}={configured!r}.") from exc
    resolved = shutil.which(executable, path=environment.get("PATH"))
    if resolved is None:
        raise HostServiceError(
            f"Cannot find {executable!r} from {WRAPPER_COMMAND_ENV} on PATH; "
            "the host service launches through it."
        )
    # Resolve parent traversal through symlinks, preserving the wrapper's final symlink.
    try:
        parent = os.path.realpath(os.path.dirname(resolved), strict=True)
    except OSError as exc:
        raise HostServiceError(f"Cannot resolve wrapper directory for {executable!r}.") from exc
    absolute = os.path.join(parent, os.path.basename(resolved))
    if shutil.which(absolute) is None:
        raise HostServiceError(f"Cannot execute the resolved wrapper {absolute!r}.")
    return [absolute, *arguments]


def _service_command(server_url: str | None, environment: Mapping[str, str]) -> list[str]:
    """Build the persistent service entry-point command."""
    mode = ["--server", server_url] if server_url else ["--local"]
    wrapper = _wrapper_command(environment)
    if wrapper is not None:
        return [*wrapper, "host-service-entry", *mode]
    return [sys.executable, "-P", "-m", "omnigent.host.service_entry", *mode]


def launchd_service_launcher_prefix() -> list[str]:
    """Read and validate the host owner's optional launchd argv prefix."""
    path = global_config_path()
    try:
        config = load_global_config(path)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise HostServiceError(f"Cannot read host service configuration from {path}.") from exc
    host = config.get("host") if isinstance(config, dict) else None
    if not isinstance(host, dict) or "service_launcher" not in host:
        return []

    prefix = host["service_launcher"]
    setting = f"host.service_launcher in {path}"
    if (
        not isinstance(prefix, list)
        or not prefix
        or not all(isinstance(arg, str) for arg in prefix)
    ):
        raise HostServiceError(f"{setting} must be a nonempty list of string arguments.")
    if any(_CONTROL_CHAR_RE.search(arg) for arg in prefix):
        raise HostServiceError(f"{setting} arguments must not contain control characters.")
    executable = os.path.expanduser(prefix[0])
    if not os.path.isabs(executable):
        raise HostServiceError(f"{setting} must start with an absolute executable path.")
    if shutil.which(executable) is None:
        raise HostServiceError(f"{setting} executable is missing or not executable: {executable}")
    return [executable, *prefix[1:]]


def _clean_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Validate environment values before persisting them in a service file."""
    cleaned: dict[str, str] = {}
    for key, value in environment.items():
        valid_name = (
            bool(key) and "=" not in key and "\x00" not in key
            if platform.system() == "Windows"
            else _ENV_NAME_RE.fullmatch(key) is not None
        )
        if not valid_name:
            raise HostServiceError(f"Cannot persist invalid environment variable name {key!r}.")
        # launchd plists turn a carriage return into a newline; NUL cannot reach a process env.
        if "\x00" in value or "\r" in value:
            raise HostServiceError(
                f"Cannot persist environment variable {key!r} containing NUL or a carriage return."
            )
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
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("%", "%%")
    )
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
        # systemd expands dollars in arguments, but treats argv[0] literally.
        "ExecStart="
        + " ".join(
            _systemd_quote(part, escape_dollar=index > 0) for index, part in enumerate(command)
        ),
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


def _wait_for_launchd_unload(service_target: str) -> bool:
    """Wait for launchd to finish unloading a booted-out job.

    ``launchctl bootout`` unloads asynchronously, so a ``launchctl print``
    issued right after it can still report the job during the unload window.
    """
    deadline = time.monotonic() + _LAUNCHD_UNLOAD_TIMEOUT
    while True:
        if _run_best_effort(["launchctl", "print", service_target]).returncode != 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_LAUNCHD_UNLOAD_POLL_INTERVAL)


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


def validate_user_host_service(
    server_url: str | None,
    *,
    environment: Mapping[str, str],
) -> None:
    """Raise :class:`HostServiceError` if the host service cannot be defined.

    :param server_url: Remote server URL, or ``None`` for local mode.
    :param environment: Environment the service would persist.
    """
    service = _service_for_current_platform()
    clean_environment = _clean_environment(environment)
    if service.kind != "windows_task":
        _service_command(server_url, clean_environment)
    if service.kind == "launchd":
        launchd_service_launcher_prefix()


def enable_user_host_service(
    server_url: str | None,
    *,
    environment: Mapping[str, str],
) -> HostService:
    """Install, enable, and start the current user's host service."""
    service = _service_for_current_platform()
    clean_environment = _clean_environment(environment)
    if service.kind == "launchd":
        content = _launchd_payload(
            service,
            command=[
                *launchd_service_launcher_prefix(),
                *_service_command(server_url, clean_environment),
            ],
            environment=clean_environment,
        )
        _enable_launchd(service, content)
    elif service.kind == "systemd_user":
        command = _service_command(server_url, clean_environment)
        content = _systemd_unit(command=command, environment=clean_environment)
        _enable_systemd(service, content)
    else:
        from omnigent.host.windows_task import enable

        try:
            enable(service, server_url, clean_environment)
        except OSError as exc:
            raise HostServiceError(str(exc)) from exc
    _record_service(service)
    return service


def disable_user_host_service() -> HostService:
    """Stop, disable, and remove the current user's host service."""
    service = _service_for_current_platform()
    still_running = False
    if service.kind == "windows_task":
        from omnigent.host.windows_task import disable, installed

        snapshot = installed(service)
        if snapshot is not None:
            service = snapshot[0]
        else:
            from omnigent.install_ledger import backfill_ledger_path, ledger_path, load_ledger

            # A failed file removal keeps its ledger row even though the task
            # is already gone, including a previous config-home destination.
            for path in (ledger_path(), backfill_ledger_path()):
                ledger = load_ledger(path)
                if ledger is None:
                    continue
                entry = next(
                    (
                        entry
                        for entry in ledger.entries.launch_agents
                        if entry.kind == service.kind and entry.label == service.label
                    ),
                    None,
                )
                if entry is not None:
                    service = HostService(service.kind, Path(entry.path), service.label)
                    break
        disable(service)
    elif service.kind == "launchd":
        domain = f"gui/{_current_uid()}"
        service_target = f"{domain}/{service.label}"
        _run_best_effort(["launchctl", "bootout", service_target])
        still_running = not _wait_for_launchd_unload(service_target)
        # Unlink even when the job lingers: a retained RunAtLoad plist would
        # silently restore the service at the next login.
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
    if still_running:
        raise HostServiceError(
            f"launchd service {service.label!r} is still running; its definition "
            "was removed, so it will not return at the next login."
        )
    return service


def _read_posix_service(
    service: HostService,
) -> tuple[list[str], str | None, str | None]:
    """Read the installed argv, server target and optional saved wrapper setting."""
    try:
        content = service.path.read_bytes()
        command: list[str] = []
        wrapper_setting = None
        if service.kind == "launchd":
            payload = plistlib.loads(content)
            if not isinstance(payload, dict):
                raise ValueError("Expected a plist dictionary")
            command = payload.get("ProgramArguments", [])
            if not isinstance(command, list) or not all(isinstance(p, str) for p in command):
                raise ValueError("Expected program arguments")
            environment = payload.get("EnvironmentVariables")
            if isinstance(environment, dict):
                recorded = environment.get(WRAPPER_COMMAND_ENV)
                if isinstance(recorded, str):
                    wrapper_setting = recorded
        else:
            for line in content.decode().splitlines():
                if line.startswith("ExecStart="):
                    # Match _systemd_unit: argv[0] never double-escapes dollars.
                    parts = shlex.split(line[len("ExecStart=") :])
                    command = [
                        (part.replace("$$", "$") if index else part).replace("%%", "%")
                        for index, part in enumerate(parts)
                    ]
                    break
        # The service mode comes after any launcher or wrapper flags.
        if command[-2:-1] == ["--server"]:
            target = command[-1]
        elif command[-1:] == ["--local"]:
            target = None
        else:
            raise ValueError("Expected a host service mode")
    except (OSError, ValueError, IndexError, plistlib.InvalidFileException) as exc:
        raise HostServiceError(
            f"Cannot read the target of the installed host service {service.path}."
        ) from exc
    return command, target, wrapper_setting


def installed_user_host_service() -> tuple[HostService, str | None] | None:
    """Read the installed service and its target; unsupported platforms return None."""
    if platform.system() not in ("Darwin", "Linux", "Windows"):
        return None
    service = _service_for_current_platform()
    if service.kind == "windows_task":
        from omnigent.host.windows_task import installed

        return installed(service)
    if not service.path.exists():
        return None
    _command, target, _wrapper = _read_posix_service(service)
    return service, target


def installed_posix_wrapper_command(service: HostService) -> str | None:
    """Recover the exact wrapper argv for a service refresh from an unwrapped CLI."""
    command, target, wrapper_setting = _read_posix_service(service)
    arguments = command[: -(2 if target is not None else 1)]
    if arguments[-1:] != ["host-service-entry"]:
        return None
    wrapper = arguments[:-1]
    if service.kind == "launchd":
        if wrapper_setting is not None and wrapper_setting.strip():
            try:
                count = len(shlex.split(wrapper_setting))
            except ValueError as exc:
                raise HostServiceError("Cannot parse the installed host wrapper setting.") from exc
            wrapper = wrapper[-count:]
        else:
            prefix = launchd_service_launcher_prefix()
            if wrapper[: len(prefix)] == prefix:
                wrapper = wrapper[len(prefix) :]
    return shlex.join(wrapper) if wrapper else None


def stop_user_host_service(service: HostService) -> None:
    """Stop the service while preserving its installed definition."""
    if service.kind == "windows_task":
        from omnigent.host.windows_task import stop

        stop(service)
    elif service.kind == "launchd":
        domain = f"gui/{_current_uid()}"
        service_target = f"{domain}/{service.label}"
        _run_best_effort(["launchctl", "bootout", service_target])
        if not _wait_for_launchd_unload(service_target):
            raise HostServiceError(
                f"launchd service {service.label!r} did not stop before the update."
            )
    else:
        _run_checked(["systemctl", "--user", "stop", service.label])


def start_user_host_service(service: HostService) -> None:
    """Start the installed service definition as-is."""
    if service.kind == "windows_task":
        from omnigent.host.windows_task import start

        start(service)
        return
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
