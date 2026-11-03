"""Admin terminal consent, incidental readers and launcher lifecycle."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.runner import create_runner_app
from omnigent.runner.direct_attach import create_direct_attach_app
from omnigent.runner.native.orchestration import (
    _codex_pane_screen,
    _publish_tmux_target_for_bridge,
    _terminal_tmux_pane,
)
from omnigent.runner.resource_registry import SessionResourceRegistry, _terminal_exit_diagnostics
from omnigent.runner.sign_in_watch import build_sign_in_watch
from omnigent.runner.tool_dispatch import (
    _execute_terminal_tool,
    _spawn_async_tool,
    build_native_relay_tool_schemas,
)
from omnigent.spec.types import AgentSpec
from omnigent.terminals import TerminalRegistry
from omnigent.terminals.elevation import ELEVATED_TERMINALS_ENV
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.sys_terminal import SysTerminalRequestAccessTool
from omnigent.tools.manager import ToolManager
from tests._helpers.polling import poll_until, poll_until_async
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient, make_test_terminal_instance
from tests.runner.test_session_resources import _make_capturing_instance, _WatcherCapture


@pytest.fixture
def admin(tmp_path: Path):
    registry = TerminalRegistry()
    instance = make_test_terminal_instance("bash", "admin", tmp_path)
    instance.elevated = True
    instance.origin = "user"
    registry._by_conversation["session"] = {("bash", "admin"): instance}
    registry._instance_locks[("session", "bash", "admin")] = threading.Lock()
    return registry, instance


def test_terminal_access_request_is_discovered_only_with_approval_transport(admin, monkeypatch):
    registry, instance = admin
    monkeypatch.setattr("omnigent.runtime.get_terminal_registry", lambda: registry)
    spec = AgentSpec(spec_version=1, terminals={"bash": TerminalEnvSpec()})
    name = "sys_terminal_request_access"
    for available in (False, True):
        schemas = ToolManager(spec, terminal_access_requests_enabled=available).get_tool_schemas()
        assert (name in {item["function"]["name"] for item in schemas}) is available
        relayed = build_native_relay_tool_schemas(spec, terminal_access_requests_enabled=available)
        assert (name in {item["name"] for item in relayed}) is available
    tool = SysTerminalRequestAccessTool(registry)
    args = json.dumps(
        {
            "terminal": "bash",
            "session": "admin",
            "instance_id": instance.instance_id,
            "reason": "Requested work",
        }
    )
    context = ToolContext(task_id="test", agent_id="test", conversation_id="session")
    assert json.loads(tool.invoke(args, context)) == {"status": "rejected"}
    assert not instance.agent_access
    registry.set_agent_access("session", "bash", "admin", instance.instance_id, True)
    assert json.loads(tool.invoke(args, context)) == {"status": "granted"}
    registry.set_agent_access("session", "bash", "admin", instance.instance_id, False)
    assert json.loads(tool.invoke(args, context)) == {"status": "rejected"}
    refusal = _spawn_async_tool(
        {"tool": name, "args": args},
        session_inbox=None,
        session_async_tasks=None,
        server_client=None,
        terminal_registry=registry,
        resource_registry=None,
        agent_spec=spec,
        conversation_id="session",
        task_id="test",
        agent_id="test",
        agent_name=None,
        runner_workspace=None,
        mcp_manager=None,
    )
    assert "interactive application approval" in refusal
    assert not instance.agent_access


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["launch", "send", "read", "close", "list"])
async def test_agent_terminal_tools_require_instance_consent(admin, tool: str) -> None:
    registry, instance = admin
    instance.send = AsyncMock(return_value={"status": "sent"})
    instance.read = AsyncMock(return_value={"screen": "private admin output"})
    spec = AgentSpec(spec_version=1, name="test", terminals={"bash": TerminalEnvSpec()})

    async def invoke():
        return json.loads(
            await _execute_terminal_tool(
                f"sys_terminal_{tool}",
                {"terminal": "bash", "session": "admin", "text": "hello"},
                terminal_registry=registry,
                agent_spec=spec,
                conversation_id="session",
                task_id="test",
                agent_id="test",
            )
        )

    result = await invoke()
    if tool == "list":
        assert result[0]["instance_id"] == instance.instance_id
        assert result[0]["elevated"] is True and result[0]["agent_access"] is False
        assert "tmux_socket" not in result[0]
        return
    assert "agent access not granted" in result["error"]
    instance.read.assert_not_called()
    instance.send.assert_not_called()
    assert registry.get("session", "bash", "admin") is instance
    registry.set_agent_access("session", "bash", "admin", instance.instance_id, True)
    granted = await invoke()
    assert "error" not in granted
    if tool in ("send", "read", "launch"):
        registry.set_agent_access("session", "bash", "admin", instance.instance_id, False)
        assert "agent access not granted" in (await invoke())["error"]
    else:
        assert registry.get("session", "bash", "admin") is None
        assert instance.agent_access is False


@pytest.mark.asyncio
async def test_resource_projection_switch_and_transfer_are_instance_bound(admin) -> None:
    registry, instance = admin
    app = create_runner_app(terminal_registry=registry, server_client=NullServerClient())
    path = "/v1/sessions/session/resources/terminals/terminal_bash_admin"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://runner"
    ) as client:
        for url in [path, "/v1/sessions/session/resources/terminal_bash_admin"]:
            response = await client.get(url)
            assert response.status_code == 200, response.text
            metadata = response.json()["metadata"]
            assert metadata["instance_id"] == instance.instance_id
            assert metadata["agent_access"] is False
            assert "tmux_socket" not in metadata and "tmux_target" not in metadata
        for suffix in ["resources", "resources/terminals"]:
            items = (await client.get(f"/v1/sessions/session/{suffix}")).json()["data"]
            item = next(item for item in items if item["id"] == "terminal_bash_admin")
            assert "tmux_socket" not in item["metadata"]
        for enabled in (True, False):
            response = await client.patch(
                path, json={"agent_access": enabled, "instance_id": instance.instance_id}
            )
            assert response.status_code == 200, response.text
            assert response.json()["metadata"]["agent_access"] is enabled
            transfer = await client.post(path + "/transfer", json={"target_session_id": "other"})
            assert transfer.status_code == 409
        replacement = make_test_terminal_instance("bash", "admin", instance.private_dir)
        replacement.elevated = True
        registry._by_conversation["session"][("bash", "admin")] = replacement
        stale = await client.patch(
            path, json={"agent_access": True, "instance_id": instance.instance_id}
        )
        assert stale.status_code == 409 and not replacement.agent_access
        replacement.elevated = False
        plain = await client.patch(
            path, json={"agent_access": True, "instance_id": replacement.instance_id}
        )
        assert plain.status_code == 409


@pytest.mark.parametrize("read_only", [False, True])
def test_direct_attach_rejects_admin_even_with_valid_token_and_instance(
    admin, read_only, monkeypatch
):
    registry, instance = admin
    runner = create_runner_app(terminal_registry=registry, server_client=NullServerClient())
    bridge = AsyncMock(side_effect=AssertionError("must not reach admin socket"))
    monkeypatch.setattr("omnigent.runner.resource_routes.bridge_tmux_control_to_websocket", bridge)
    direct = create_direct_attach_app(
        runner.state.terminal_attach_handler,
        token="test-token",
        allowed_origins={"http://localhost"},
    )
    path = (
        "/v1/sessions/session/resources/terminals/terminal_bash_admin/attach"
        f"?token=test-token&read_only={str(read_only).lower()}"
        f"&elevated_instance_id={instance.instance_id}"
    )
    with TestClient(direct).websocket_connect(path, headers={"origin": "http://localhost"}) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_bytes()
    assert closed.value.code == 1008
    bridge.assert_not_called()


@pytest.mark.parametrize(
    "name",
    [
        "claude",
        "codex",
        "cursor",
        "goose",
        "hermes",
        "kiro",
        "devin",
        "qwen",
        "kimi",
        "antigravity",
        "opencode",
        "pi",
    ],
)
@pytest.mark.asyncio
async def test_native_name_collision_cannot_publish_or_read_admin(
    admin, name, monkeypatch, tmp_path
):
    registry, instance = admin
    registry._by_conversation["session"] = {(name, "main"): instance}
    instance.read = AsyncMock(side_effect=AssertionError("incidental admin read"))
    resources = SessionResourceRegistry(terminal_registry=registry)
    publish = Mock(side_effect=AssertionError("admin socket published"))
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge.write_tmux_target", publish)
    _publish_tmux_target_for_bridge(
        resource_registry=resources,
        session_id="session",
        bridge_id="test",
        terminal_name=name,
        session_key="main",
    )
    assert _terminal_tmux_pane(resources, "session", name, "main") == (None, None)
    assert registry.get_native("session", name, "main") is None
    assert not registry.native_panes()
    assert await _codex_pane_screen(instance) == ""
    instance._last_pane_snapshot = "private admin output"
    instance._last_exit_snapshot = "private admin output"
    assert instance.last_pane_text() is None and instance.last_exit_text() is None
    assert _terminal_exit_diagnostics(instance)[3] is None
    publish.assert_not_called()
    instance.read.assert_not_called()


@pytest.mark.asyncio
async def test_sign_in_readers_ignore_admin_native_name_collision(admin, monkeypatch):
    registry, instance = admin
    registry._by_conversation["session"] = {("codex", "main"): instance}
    instance.read = AsyncMock(side_effect=AssertionError("admin sign-in content leaked"))
    resources = SessionResourceRegistry(terminal_registry=registry)
    app = create_runner_app(
        terminal_registry=registry, resource_registry=resources, server_client=NullServerClient()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://runner"
    ) as client:
        result = await client.get("/v1/sessions/session/sign-in-link")
        assert result.status_code == 200 and result.json()["pending"] is False
    monkeypatch.setattr("omnigent.runner.sign_in_watch._SIGN_IN_WATCH_INTERVAL_S", 0)
    watchers = {}
    service = build_sign_in_watch(
        _background_tasks=set(),
        _session_harness_name=lambda _: "codex-native",
        _sign_in_watchers=watchers,
        resource_registry=resources,
        server_client=NullServerClient(),
    )
    service.start_sign_in_watch("session")
    await asyncio.wait_for(watchers["session"], 2)
    instance.read.assert_not_called()


@pytest.mark.asyncio
async def test_admin_off_rejects_create_without_launch(admin, monkeypatch):
    registry, _ = admin
    monkeypatch.delenv(ELEVATED_TERMINALS_ENV, raising=False)
    app = create_runner_app(terminal_registry=registry, server_client=NullServerClient())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://runner"
    ) as client:
        result = await client.post(
            "/v1/sessions/session/resources/terminals",
            json={"terminal": "bash", "session_key": "new", "elevated": True},
        )
    assert result.status_code == 409
    assert registry.get("session", "bash", "new") is None


def test_stand_in_prompt_attach_interrupt_and_registry_exit(monkeypatch, tmp_path):
    from omnigent.terminals.elevation import elevated_terminal_spec

    monkeypatch.setenv(ELEVATED_TERMINALS_ENV, "sudo")
    selected = []

    def launcher(spec, *, method):
        elevated = elevated_terminal_spec(spec, method=method)
        selected.append((elevated.command, elevated.args))
        return replace(
            elevated,
            command="/bin/sh",
            args=["-c", "trap 'exit 130' INT; printf 'Password:'; read -r reply; exit 1"],
        )

    monkeypatch.setattr("omnigent.terminals.registry.elevated_terminal_spec", launcher)
    registry = TerminalRegistry()
    spec = TerminalEnvSpec(
        command="/bin/sh",
        os_env=OSEnvSpec(
            type="caller_process", cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none")
        ),
    )

    async def resolve(*_):
        return AgentSpec(spec_version=1, name="test", terminals={"shell": spec})

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),
        terminal_registry=registry,
        spec_resolver=resolve,
        server_client=NullServerClient(),
        runner_workspace=tmp_path,
        per_session_workspace=False,
    )
    path = "/v1/sessions/session/resources/terminals"
    with TestClient(app) as client:
        initialized = client.post(
            "/v1/sessions", json={"session_id": "session", "agent_id": "agent"}
        )
        assert initialized.status_code == 201, initialized.text
        created = client.post(
            path, json={"terminal": "shell", "session_key": "admin", "elevated": True}
        )
        assert created.status_code == 200, created.text
        instance = registry.get("session", "shell", "admin")
        assert instance is not None
        assert selected == [("sudo", ["-i"])]
        output = b""
        with client.websocket_connect(
            path + f"/terminal_shell_admin/attach?elevated_instance_id={instance.instance_id}"
        ) as ws:
            while b"Password:" not in output:
                output += ws.receive_bytes()
            before = instance.last_input_at
            ws.send_bytes(b"\x03")
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    ws.receive_bytes()
        poll_until(lambda: registry.get("session", "shell", "admin") is None, timeout_s=5)
        assert instance.last_input_at >= before
        assert closed.value.code in (4404, 4405)
        assert not instance.running and not instance.agent_access
        assert client.get(path + "/terminal_shell_admin").status_code == 404


@pytest.mark.asyncio
async def test_admin_idle_closes_and_revokes_despite_output_and_resize(admin, monkeypatch):
    registry, instance = admin
    monkeypatch.setattr("omnigent.terminals.registry.ELEVATED_INPUT_IDLE_S", 0.05)
    instance.last_input_at = time.monotonic() - 1
    instance.agent_access = True
    instance.note_client_interaction()
    instance._remember_pane_snapshot("new output does not renew admin input idle")
    registry._watch_elevated("session", "bash", "admin", instance)
    await poll_until_async(
        lambda: registry.get("session", "bash", "admin") is None and not instance.running,
        timeout_s=3,
    )
    assert not instance.agent_access and not instance.running


@pytest.mark.asyncio
async def test_admin_idle_closes_through_resource_lifecycle(tmp_path, monkeypatch):
    registry = TerminalRegistry()
    capture = _WatcherCapture()
    instance = _make_capturing_instance(tmp_path, capture, name="bash", session_key="admin")
    instance.elevated = True
    registry._by_conversation["session"] = {("bash", "admin"): instance}
    registry._instance_locks[("session", "bash", "admin")] = threading.Lock()
    resources = SessionResourceRegistry(terminal_registry=registry)
    events = []
    resources.set_terminal_exit_publisher(events.append)
    await resources.observe_auxiliary_terminal("session", "bash", "admin", instance)
    assert capture.started
    monkeypatch.setattr("omnigent.terminals.registry.ELEVATED_INPUT_IDLE_S", 0.05)
    instance.last_input_at = time.monotonic() - 1
    registry._watch_elevated("session", "bash", "admin", instance)
    await poll_until_async(lambda: len(events) == 1, timeout_s=3)
    assert registry.get("session", "bash", "admin") is None
    assert ("session", "terminal_bash_admin") not in resources._terminal_lifecycles
    assert ("session", "terminal_bash_admin") not in resources._terminal_roles
    assert events[0].terminal_instance_id == instance.instance_id


@pytest.mark.asyncio
async def test_agent_input_renews_admin_clock_but_empty_send_does_not(admin, monkeypatch):
    registry, instance = admin
    instance.agent_access = True
    instance.last_input_at = 0
    monkeypatch.setattr(instance, "_tmux", AsyncMock(return_value=""))
    await instance.send("hello", keys="")
    assert instance.last_input_at > 0
    before = instance.last_input_at
    await instance.send("", keys="")
    assert instance.last_input_at == before
    # Closing a stopped ordinary terminal retains the existing cleanup behavior.
    instance.elevated = False
    instance.running = False
    registry.require_agent_access("session", "bash", "admin", instance)
    assert await registry.close("session", "bash", "admin", expected=instance)


@pytest.mark.asyncio
@pytest.mark.parametrize("granted", [False, True])
async def test_native_controls_never_drive_an_admin_name_collision(admin, monkeypatch, granted):
    from omnigent.runner import app as runner_app
    from omnigent.runner.native_controls import build_native_controls

    registry, instance = admin
    instance.agent_access = granted
    registry._by_conversation["session"] = {
        ("codex", "main"): instance,
        ("opencode", "main"): instance,
    }
    controls = []

    def capture(**kwargs):
        result = build_native_controls(**kwargs)
        controls.append(result)
        return result

    monkeypatch.setattr(runner_app, "build_native_controls", capture)
    injection = Mock(side_effect=AssertionError("native control touched admin pane"))
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._run_tmux", injection)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._capture_pane", injection)
    monkeypatch.setattr("omnigent.native.native_cost_popup.launch_cost_popup", injection)
    monkeypatch.setattr("omnigent.native.native_cost_popup.launch_blocked_notice", injection)
    create_runner_app(terminal_registry=registry, server_client=NullServerClient())
    control = controls[0]
    assert (
        await control.handle_codex_native_slash_command(
            "session", "/help", error_code="unavailable", context="test"
        )
    ).status_code == 503
    assert (
        await control.handle_codex_native_approval_mode_change("session", "ask-for-approval")
    ).status_code == 503
    assert (
        await control.handle_codex_native_cost_popup("session", "card", "test")
    ).status_code == 204
    assert (
        await control.handle_opencode_native_cost_popup("session", "card", "test")
    ).status_code == 204
    assert (
        await control.handle_opencode_native_blocked_notice("session", "test")
    ).status_code == 204
    injection.assert_not_called()
