"""Host opt-in controls both advertised capability and runner launch method."""

from __future__ import annotations

import pytest

from omnigent.host.connect import HostProcess, _build_runner_env
from omnigent.host.frames import CAP_TERMINAL_ELEVATED
from omnigent.host.identity import HostIdentity, load_elevated_terminals_method
from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.terminals.elevation import ELEVATED_TERMINALS_ENV, elevated_terminal_spec


@pytest.mark.parametrize("method", [None, "sudo"])
def test_host_elevation_opt_in_capability_and_runner_environment(tmp_path, monkeypatch, method):
    config = tmp_path / "config.yaml"
    config.write_text("host: {}\n" if method is None else "host:\n  elevated_terminals: sudo\n")
    host = HostProcess(
        HostIdentity("a" * 32, "test-host"),
        "http://localhost",
        interactive_shells=["bash"],
        config_path=config,
    )
    assert load_elevated_terminals_method(config) == method
    assert (CAP_TERMINAL_ELEVATED in host._terminal_capabilities()) is (method is not None)
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)
    env = _build_runner_env(
        {ELEVATED_TERMINALS_ENV: "sudo"},
        server_url="http://server",
        runner_id="runner",
        binding_token="synthetic-token",
        workspace="/opt/work/project",
        parent_pid=1,
        elevated_terminals=method,
    )
    assert env.get(ELEVATED_TERMINALS_ENV) == method
    if method is not None:
        spec = elevated_terminal_spec(
            TerminalEnvSpec(
                command="untrusted-command", args=["untrusted-arg"], env={"UNTRUSTED": "value"}
            ),
            method=method,
        )
        assert (spec.command, spec.args, spec.env) == ("sudo", ["-i"], {})
        assert not spec.keep_alive_after_exit


@pytest.mark.parametrize("method", [None, "unknown"])
def test_disabled_or_unknown_elevation_method_cannot_start_a_launcher(method):
    with pytest.raises(ValueError, match="not enabled"):
        elevated_terminal_spec(TerminalEnvSpec(), method=method)
