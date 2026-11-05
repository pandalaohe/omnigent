"""Terminals a user may open from the workspace rail for a session's agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.native.native_coding_agents import (
    native_coding_agent_for_agent_name,
    native_shell_terminal_specs,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from omnigent.inner.datamodel import AgentDef
    from omnigent.spec.types import AgentSpec

#: Shell offered to the user when no host inventory is known and the agent
#: declares no ``terminals:``.
DEFAULT_USER_SHELL = "bash"


def session_offers_host_shells(agent_name: str | None, host_id: str | None) -> bool:
    """Return whether a session's user-shell offer follows its host inventory.

    Every host-bound session offers the host's shells, on any OS: a native
    wrapper's portable declarations are replaced by them and an agent that
    declares none gains them. A session without a resolved agent has no spec to
    offer anything from (see :func:`user_shell_terminals`).

    :param agent_name: The session agent's name, or ``None`` when no spec
        resolves for the session.
    :param host_id: The host the session runs on, or ``None`` when hostless.
    :returns: ``True`` when the host's shell inventory shapes the offer.
    """
    return agent_name is not None and host_id is not None


def user_shell_terminals(
    spec: AgentSpec | AgentDef | None,
    *,
    host_shells: Sequence[str] | None = None,
) -> dict[str, TerminalEnvSpec]:
    """Return the terminals a user may launch for *spec*, keyed by name.

    Declared ``terminals:`` are offered as they are and always win: a host shell
    of the same name only adds the name, never its own command, arguments,
    environment or sandbox. A host's inventory replaces a native wrapper's
    portable declarations wholesale, and is added after the declared ones for
    any other agent, so a host-bound SDK session offers the shells a native one
    does; those run unsandboxed, as the human's own shell. Only when no host
    inventory is known does an agent that declares none get the default
    ``bash``, which inherits the agent's ``os_env``.

    Agent tools preserve declared terminals, or reuse this host-shell mapping
    when none are declared and the runner reports shells. The hostless default
    remains human-only. Callers that only need names read the keys; the runner
    launches the same mapping, so offer and validation cannot drift.

    :param spec: The session agent's spec, or ``None`` when none resolves.
    :param host_shells: The host's ordered shell inventory. ``None`` means no
        host inventory is known (a hostless session); an empty sequence means a
        host was consulted and reports no shells, which offers none rather than
        falling back to a shell that host cannot launch.
    :returns: Terminal specs in offer order; empty when *spec* is ``None``.
    """
    if spec is None:
        return {}
    declared = dict(spec.terminals or {})
    if host_shells is not None:
        host = native_shell_terminal_specs(list(host_shells))
        if native_coding_agent_for_agent_name(spec.name) is not None:
            return host
        for name, terminal in host.items():
            declared.setdefault(name, terminal)
        return declared
    if declared:
        return declared
    return {DEFAULT_USER_SHELL: TerminalEnvSpec(command=DEFAULT_USER_SHELL)}
