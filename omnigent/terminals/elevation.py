"""Host-selected terminal elevation, never selected by an agent spec."""

from __future__ import annotations

from dataclasses import replace

from omnigent._platform import IS_POSIX
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec

ELEVATED_TERMINALS_ENV = "OMNIGENT_ELEVATED_TERMINALS"
ELEVATED_INPUT_IDLE_S = 15 * 60
AGENT_ACCESS_REQUIRED = "agent access not granted; ask with sys_terminal_request_access"
USER_SHELL_READ_ONLY = (
    "this shell belongs to the user: you can list and read it, not type into it or close it"
)


def elevated_terminal_spec(spec: TerminalEnvSpec, *, method: str | None) -> TerminalEnvSpec:
    """Replace the requested program with the host's enabled elevation launcher."""
    if not IS_POSIX or method != "sudo":
        raise ValueError("admin terminals are not enabled on this host")
    cwd = spec.os_env.cwd if isinstance(spec.os_env, OSEnvSpec) else None
    return replace(
        spec,
        command="sudo",
        args=["-i"],
        os_env=OSEnvSpec(type="caller_process", cwd=cwd, sandbox=OSEnvSandboxSpec(type="none")),
        env={},
        tmux_start_on_attach=False,
        tmux_allow_passthrough=False,
        keep_alive_after_exit=False,
    )
