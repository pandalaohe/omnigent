"""Real native launch adapters preserve their own runner binding authority."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.runner.identity import (
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    RUNNER_TUNNEL_TOKEN_HEADER,
)


@pytest.mark.parametrize("binding_token", [None, "native-local-owned-binding"])
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_local_cli_threads_only_its_minted_binding_to_forwarder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    harness: str,
    binding_token: str | None,
) -> None:
    import omnigent.chat as chat
    from omnigent import claude_native, codex_native

    module = claude_native if harness == "claude" else codex_native
    handle = chat.LocalServer(
        proc=SimpleNamespace(),  # type: ignore[arg-type]
        log_path=tmp_path / "server.log",
        runner_id="runner_test",
        runner_binding_token=binding_token,
    )
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "unrelated-ambient-binding")
    monkeypatch.setattr(chat, "_find_free_port", lambda: 8765)
    monkeypatch.setattr(chat, "_start_local_server", lambda *args, **kwargs: handle)
    monkeypatch.setattr(chat, "_stop_local_server", lambda *args: None)
    monkeypatch.setattr(chat, "_wait_for_server", lambda *args: None)
    monkeypatch.setattr(module, "_resolve_session_id_for_resume", lambda **kwargs: "parent")
    monkeypatch.setattr(
        module, "_align_working_directory_with_session", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(module, "_wrapper_spec_raw_instructions", lambda *args: None)
    monkeypatch.setattr(module, "open_conversation_link_if_enabled", lambda **kwargs: None)
    captured: dict[str, Any] = {}

    async def attach(**kwargs: Any) -> None:
        captured.update(kwargs)

    if harness == "claude":

        async def prepare(**kwargs: Any):
            # The privileged binding is not passed into vendor hook config.
            assert RUNNER_TUNNEL_TOKEN_HEADER not in kwargs["headers"]
            return claude_native.PreparedClaudeTerminal(
                session_id="parent",
                terminal_id="terminal_claude_main",
                bridge_dir=tmp_path,
                reattached=True,
            )

        monkeypatch.setattr(module, "_prepare_claude_terminal", prepare)
        monkeypatch.setattr(module, "_attach_with_transcript_forwarder", attach)
        claude_native._run_with_local_server(
            tmp_path / "test.yaml",
            session_id="parent",
            resume_picker=False,
            claude_args=(),
            command="claude",
        )
    else:

        async def prepare(**kwargs: Any):
            assert RUNNER_TUNNEL_TOKEN_HEADER not in kwargs["headers"]
            return codex_native.PreparedCodexTerminal(
                session_id="parent",
                terminal_id="terminal_codex_main",
                bridge_dir=tmp_path,
                tmux_socket=None,
                tmux_target=None,
                thread_id="thread",
                app_server_url=None,
                app_server=None,
                event_client=None,
                reattached=True,
            )

        monkeypatch.setattr(module, "_prepare_codex_terminal", prepare)
        monkeypatch.setattr(module, "_attach_with_forwarder", attach)
        codex_native._run_with_local_server(
            tmp_path / "test.yaml",
            session_id="parent",
            resume_picker=False,
            codex_args=(),
            command="codex",
            model=None,
            prompt=None,
        )
    assert captured["headers"].get(RUNNER_TUNNEL_TOKEN_HEADER) == binding_token
    if binding_token is not None:
        assert binding_token not in repr(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_token", [None, "native-known-thread-binding"])
async def test_runner_known_codex_launch_passes_binding_to_supervisor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    binding_token: str | None,
) -> None:
    from omnigent import codex_native_forwarder
    from omnigent.runner.native.orchestration import _codex_forward_known_thread

    if binding_token is None:
        monkeypatch.delenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, binding_token)
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://test")
    monkeypatch.setattr(
        "omnigent.runner._entry._make_auth_token_factory", lambda: lambda: "test-bearer"
    )
    captured: dict[str, Any] = {}

    async def supervise(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(codex_native_forwarder, "supervise_forwarder", supervise)
    await _codex_forward_known_thread(
        session_id="native-binding-test-parent",
        bridge_dir=tmp_path,
        codex_ws_url="ws://127.0.0.1:1",
        thread_id="thread",
    )
    assert captured["headers"].get(RUNNER_TUNNEL_TOKEN_HEADER) == binding_token
    assert captured["headers"]["Authorization"] == "Bearer test-bearer"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_remote_cli_never_acquires_a_daemon_runner_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    harness: str,
) -> None:
    import omnigent.chat as chat
    from omnigent import claude_native, codex_native

    module = claude_native if harness == "claude" else codex_native
    expected_headers = {"Authorization": "Bearer remote-test"}
    monkeypatch.setattr(chat, "_remote_headers", lambda **kwargs: dict(expected_headers))
    monkeypatch.setattr(chat, "_server_auth", lambda **kwargs: None)
    monkeypatch.setattr("omnigent.cli._ensure_host_daemon", lambda *args: None)
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: SimpleNamespace(host_id="host-test"),
    )
    monkeypatch.setattr(module, "_resolve_session_id_for_resume", lambda **kwargs: "parent")
    monkeypatch.setattr(
        module, "_align_working_directory_with_session", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(module, "open_conversation_link_if_enabled", lambda **kwargs: None)

    def forbidden_binding_read():
        pytest.fail("remote CLI must leave the binding token with its daemon runner")

    monkeypatch.setattr(
        "omnigent.runner._entry._runner_tunnel_binding_token_from_env", forbidden_binding_read
    )
    captured: dict[str, Any] = {}

    async def attach(**kwargs: Any):
        captured.update(kwargs)
        return claude_native._AttachOutcome.EXITED

    if harness == "claude":

        async def prepare(**kwargs: Any):
            assert kwargs["headers"] == expected_headers
            return claude_native.PreparedClaudeTerminal(
                session_id="parent",
                terminal_id="terminal_claude_main",
                bridge_dir=tmp_path,
                reattached=True,
            )

        monkeypatch.setattr(module, "_prepare_claude_terminal_via_daemon", prepare)
        monkeypatch.setattr(module, "_attach_with_transcript_forwarder", attach)
        claude_native._run_with_remote_server(
            "http://test",
            tmp_path / "test.yaml",
            session_id="parent",
            resume_picker=False,
            claude_args=(),
        )
        assert captured["run_transcript_forwarder"] is False
    else:

        async def prepare(**kwargs: Any):
            assert kwargs["headers"] == expected_headers
            return codex_native.PreparedCodexTerminal(
                session_id="parent",
                terminal_id="terminal_codex_main",
                bridge_dir=tmp_path,
                tmux_socket=None,
                tmux_target=None,
                thread_id="thread",
                app_server_url=None,
                app_server=None,
                event_client=None,
                reattached=True,
            )

        monkeypatch.setattr(module, "_prepare_codex_terminal_via_daemon", prepare)
        monkeypatch.setattr(module, "_attach_terminal_resource", attach)
        monkeypatch.setattr(
            module,
            "_start_codex_forwarder",
            lambda **kwargs: pytest.fail("daemon owns forwarding"),
        )
        codex_native._run_with_remote_server(
            "http://test",
            tmp_path / "test.yaml",
            session_id="parent",
            resume_picker=False,
            codex_args=(),
            model=None,
            prompt=None,
        )
    assert captured["headers"] == expected_headers
