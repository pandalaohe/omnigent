"""Unit tests for :mod:`omnigent.terminals.user_shells`."""

from __future__ import annotations

from omnigent.inner.datamodel import AgentDef, TerminalEnvSpec
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME
from omnigent.spec.types import AgentSpec
from omnigent.terminals.user_shells import (
    DEFAULT_USER_SHELL,
    session_offers_host_shells,
    user_shell_terminals,
)


def test_declared_terminals_are_offered_verbatim_in_spec_order() -> None:
    declared = {
        "zsh": TerminalEnvSpec(command="zsh"),
        "py": TerminalEnvSpec(command="python3"),
    }
    offered = user_shell_terminals(AgentSpec(spec_version=1, terminals=declared))

    assert list(offered) == ["zsh", "py"]
    assert offered["zsh"] is declared["zsh"]
    assert offered["py"] is declared["py"]


def test_agent_without_terminals_offers_default_shell_that_inherits_os_env() -> None:
    offered = user_shell_terminals(AgentSpec(spec_version=1))

    assert list(offered) == [DEFAULT_USER_SHELL]
    shell = offered[DEFAULT_USER_SHELL]
    assert shell.command == DEFAULT_USER_SHELL
    # No os_env of its own: the launch inherits the agent's os_env and sandbox.
    assert shell.os_env is None
    assert shell.allow_cwd_override is False
    assert shell.allow_sandbox_override is False


def test_legacy_agent_def_without_terminals_offers_default_shell() -> None:
    assert list(user_shell_terminals(AgentDef(name="plain"))) == [DEFAULT_USER_SHELL]


def test_no_resolvable_spec_offers_nothing() -> None:
    assert user_shell_terminals(None) == {}
    assert user_shell_terminals(None, host_shells=["pwsh", "cmd"]) == {}


def test_agent_without_terminals_offers_the_hosts_shells_in_order() -> None:
    """A host-bound SDK session gets exactly the host's shells (TS7)."""
    offered = user_shell_terminals(AgentSpec(spec_version=1), host_shells=["pwsh", "cmd"])

    assert list(offered) == ["pwsh", "cmd"]


def test_host_shells_are_added_to_declared_terminals() -> None:
    declared = {"py": TerminalEnvSpec(command="python3")}
    offered = user_shell_terminals(
        AgentSpec(spec_version=1, terminals=declared),
        host_shells=["zsh", "bash"],
    )

    assert list(offered) == ["py", "zsh", "bash"]
    assert offered["py"] is declared["py"]


def test_host_shells_replace_a_native_wrappers_portable_declarations() -> None:
    portable = {"bash": TerminalEnvSpec(command="bash", os_env="inherit")}
    offered = user_shell_terminals(
        AgentSpec(spec_version=1, name=CLAUDE_NATIVE_AGENT_NAME, terminals=portable),
        host_shells=["zsh", "bash"],
    )

    assert list(offered) == ["zsh", "bash"]
    # The host's own shell, not the portable declaration it shadows.
    assert offered["bash"] is not portable["bash"]


def test_host_shells_do_not_replace_a_declared_terminal() -> None:
    """A declared terminal keeps its operator spec when the host has one too."""
    declared_bash = TerminalEnvSpec(
        command="bash", args=["-l"], env={"OPERATOR": "set"}, os_env="inherit"
    )
    offered = user_shell_terminals(
        AgentSpec(spec_version=1, name="codex-sdk", terminals={"bash": declared_bash}),
        host_shells=["zsh", "bash"],
    )

    assert list(offered) == ["bash", "zsh"]
    assert offered["bash"] is declared_bash


def test_host_with_no_shells_offers_no_default_shell() -> None:
    """An empty host inventory is not the same as no host: no bash fallback."""
    assert user_shell_terminals(AgentSpec(spec_version=1), host_shells=[]) == {}

    declared = {"py": TerminalEnvSpec(command="python3")}
    offered = user_shell_terminals(AgentSpec(spec_version=1, terminals=declared), host_shells=[])
    assert list(offered) == ["py"]
    assert offered["py"] is declared["py"]

    # A native wrapper's portable declarations are only a hostless stand-in, so
    # a host that reports nothing leaves nothing to offer.
    native = AgentSpec(
        spec_version=1,
        name=CLAUDE_NATIVE_AGENT_NAME,
        terminals={"bash": TerminalEnvSpec(command="bash")},
    )
    assert user_shell_terminals(native, host_shells=[]) == {}


def test_host_shells_run_unsandboxed_as_the_users_own() -> None:
    offered = user_shell_terminals(AgentSpec(spec_version=1), host_shells=["pwsh"])

    shell = offered["pwsh"]
    assert shell.allow_cwd_override is True
    assert shell.os_env is not None
    assert shell.os_env.sandbox is not None
    assert shell.os_env.sandbox.type == "none"


def test_session_offers_host_shells_only_for_a_resolved_host_bound_agent() -> None:
    assert session_offers_host_shells("codex-sdk", "host_1") is True
    assert session_offers_host_shells(CLAUDE_NATIVE_AGENT_NAME, "host_1") is True
    assert session_offers_host_shells("codex-sdk", None) is False
    assert session_offers_host_shells(None, "host_1") is False
