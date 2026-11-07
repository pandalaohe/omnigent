"""Host-selected terminal elevation, never selected by an agent spec."""

from __future__ import annotations

import logging
from dataclasses import replace

from omnigent._platform import IS_POSIX, IS_WINDOWS
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec

ELEVATED_TERMINALS_ENV = "OMNIGENT_ELEVATED_TERMINALS"
ELEVATED_INPUT_IDLE_S = 15 * 60
AGENT_ACCESS_REQUIRED = "agent access not granted; ask with sys_terminal_request_access"
logger = logging.getLogger(__name__)


def elevated_method_supported(method: str | None) -> bool:
    """Include the Windows enrollment gate before advertising or forwarding opt-in."""
    if method == "sudo" and IS_POSIX:
        return True
    if method == "windows_ssh" and IS_WINDOWS:
        from omnigent.terminals.windows_ssh import windows_ssh_launcher

        try:
            windows_ssh_launcher().validate()
        except (OSError, ValueError):
            logger.warning("Windows admin terminals disabled: invalid or missing SSH enrollment")
            return False
        return True
    return False


def elevated_terminal_spec(spec: TerminalEnvSpec, *, method: str | None) -> TerminalEnvSpec:
    """Replace the requested program with the host's enabled elevation launcher."""
    if not elevated_method_supported(method):
        raise ValueError("admin terminals are not enabled on this host")
    command, args, env_unset = "sudo", ["-i"], list(spec.env_unset)
    if method == "windows_ssh":
        from omnigent.terminals.windows_ssh import SSH_ENV_UNSET, windows_ssh_launcher

        command, *args = windows_ssh_launcher().argv()
        env_unset.extend(SSH_ENV_UNSET)
    cwd = spec.os_env.cwd if isinstance(spec.os_env, OSEnvSpec) else None
    return replace(
        spec,
        command=command,
        args=args,
        os_env=OSEnvSpec(type="caller_process", cwd=cwd, sandbox=OSEnvSandboxSpec(type="none")),
        env={},
        env_unset=env_unset,
        tmux_start_on_attach=False,
        tmux_allow_passthrough=False,
        keep_alive_after_exit=False,
    )
