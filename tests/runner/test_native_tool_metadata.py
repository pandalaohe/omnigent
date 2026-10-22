"""Tool metadata remains available after the process working directory is removed."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.harnesses.pi_native.bridge import write_extension_files
from omnigent.inner.codex_executor import _dynamic_tool_specs
from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
from omnigent.runner import create_runner_app, tool_dispatch
from omnigent.runner.app import ResolvedSpec
from omnigent.spec.types import AgentSpec, LocalToolInfo, ToolRuntime
from omnigent.tools import ToolManager
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient


def test_history_and_peer_send_reach_codex_sdk_and_pi_registration(tmp_path: Path) -> None:
    """A peer child receives read/history and by-id send at both tool entrypoints."""
    spec = AgentSpec(spec_version=1)
    manager = ToolManager(spec, peer_messaging_enabled=True)
    try:
        codex_tools = _dynamic_tool_specs(
            [schema["function"] for schema in manager.get_tool_schemas()]
        )
    finally:
        manager.shutdown()
    codex_names = {tool["name"] for tool in codex_tools}
    assert {"sys_session_get_history", "sys_session_send"} <= codex_names
    codex_send = next(tool for tool in codex_tools if tool["name"] == "sys_session_send")
    assert "session_id" in codex_send["inputSchema"]["properties"]
    assert "agent" not in codex_send["inputSchema"]["properties"]

    native_tools = tool_dispatch.build_native_relay_tool_schemas(spec, peer_messaging_enabled=True)
    _extension, config = write_extension_files(
        tmp_path / "pi-bridge",
        session_id="conv_child",
        server_url="http://omnigent.example/",
        conversation_url="http://omnigent.example/c/conv_child",
        auth_headers={},
        tools=native_tools,
    )
    pi_tools = json.loads(config.read_text())["tools"]
    pi_names = {tool["name"] for tool in pi_tools}
    assert {"sys_session_get_history", "sys_session_send"} <= pi_names
    pi_send = next(tool for tool in pi_tools if tool["name"] == "sys_session_send")
    assert "session_id" in pi_send["parameters"]["properties"]
    assert "agent" not in pi_send["parameters"]["properties"]
    assert "sys_session_create" not in codex_names | pi_names


@pytest.mark.posix_only
@pytest.mark.parametrize("os_env", [None, OSEnvSpec(), OSEnvSpec(cwd=".")])
@pytest.mark.parametrize("enabled", [False, True])
def test_metadata_survives_deleted_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, os_env: OSEnvSpec | None, enabled: bool
) -> None:
    """Unlinked cwd preserves request/relay schemas, all grants, and optional gates."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("omnigent.runtime.get_terminal_registry", Mock)
    spec = AgentSpec(
        spec_version=1,
        os_env=os_env,
        async_enabled=enabled,
        timers=enabled,
        spawn=enabled,
        terminals={"shell": TerminalEnvSpec()} if enabled else None,
        local_tools=[LocalToolInfo(name="local_probe", path="tools/probe.py", language="python")],
    )
    schemas_without_env = tool_dispatch.build_native_relay_tool_schemas(
        dataclasses.replace(spec, os_env=None)
    )
    fallback_schemas = tool_dispatch.build_native_relay_tool_schemas(None)
    manager = ToolManager(spec)
    try:
        registered = frozenset(manager.get_tool_names()) | {"local_probe"}
        request_schemas = manager.get_tool_schemas()
    finally:
        manager.shutdown()

    deleted_cwd = tmp_path / "deleted-cwd"
    deleted_cwd.mkdir()
    original_cwd = os.open(".", os.O_RDONLY)
    try:
        os.chdir(deleted_cwd)
        deleted_cwd.rmdir()
        with pytest.raises(FileNotFoundError):
            Path.cwd()

        assert ToolManager(spec, os_env_schema_only=True).get_tool_schemas() == request_schemas
        schemas = tool_dispatch.build_native_relay_tool_schemas(spec)
        assert schemas == schemas_without_env
        assert tool_dispatch.build_native_relay_tool_schemas(None) == fallback_schemas
        schema_names = [schema["name"] for schema in schemas]
        assert len(schema_names) == len(set(schema_names))
        assert set(schema_names) >= tool_dispatch._OS_ENV_TOOLS
        for tool_name in ("sys_call_async", "sys_session_create", "sys_terminal_launch"):
            assert (tool_name in schema_names) is enabled

        for harness in ("claude-sdk", "claude-native", "codex-native", "pi-native"):
            native = harness.endswith("-native")
            granted = tool_dispatch._granted_tool_names(spec, harness)
            # read_skill_file is granted with load_skill, whatever discovery finds.
            assert granted == registered | {"read_skill_file"} | (
                tool_dispatch._OS_ENV_TOOLS if native else set()
            )
            for tool_name in ("sys_call_async", "sys_timer_set", "sys_session_create"):
                assert (tool_name in granted) is enabled
            for tool_name in tool_dispatch._OS_ENV_TOOLS:
                reason = tool_dispatch._ungranted_tool_reason(tool_name, spec, harness)
                assert (reason is None) is (os_env is not None or native)
            assert tool_dispatch._ungranted_tool_reason("unknown_tool", spec, harness) is not None
    finally:
        os.fchdir(original_cwd)
        os.close(original_cwd)


@pytest.mark.parametrize("harness", ["claude-sdk", "claude-native"])
def test_metadata_probe_errors_still_fail_closed(harness: str) -> None:
    """OS schema registration still rejects local-tool collisions before granting access."""
    spec = AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(),
        local_tools=[
            LocalToolInfo(
                name="sys_os_shell",
                path=None,
                language="python",
                runtime=ToolRuntime.CLIENT,
                parameters={"type": "object", "properties": {}},
            )
        ],
    )
    reason = tool_dispatch._ungranted_tool_reason("sys_os_shell", spec, harness)

    assert reason is not None
    assert "granted tool surface could not be resolved" in reason
    assert "collides with an already-registered tool" in reason


async def test_cold_turn_schemas_preserve_local_tools_without_os_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first web turn keeps its full request schema surface without creating an env."""
    (tmp_path / "local_probe.py").write_text(
        "from omnigent_client.tools import tool\n"
        "@tool\n"
        "def local_probe(text: str) -> str:\n"
        "    return text\n"
    )
    spec = AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(),
        timers=True,
        local_tools=[LocalToolInfo(name="local_probe", path="local_probe.py", language="python")],
    )

    async def resolve(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        return ResolvedSpec(spec=spec, workdir=tmp_path)

    create_env = Mock(side_effect=FileNotFoundError("metadata must not resolve the process cwd"))
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", create_env)
    harness = _ScriptedHarnessClient(
        [_sse({"type": "response.completed", "response": {"id": "resp_metadata"}})]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=resolve,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=tmp_path,
    )
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions/conv_metadata/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_metadata",
                "harness": "openai-agents",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        )
        assert response.status_code == 202
        tasks = [t for t in asyncio.all_tasks() if t.get_name() == "turn-conv_metadata"]
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

    assert len(harness.posted_bodies) == 1
    names = {schema["function"]["name"] for schema in harness.posted_bodies[0]["tools"]}
    assert names >= tool_dispatch._OS_ENV_TOOLS | {"local_probe", "sys_timer_set"}
    create_env.assert_not_called()
