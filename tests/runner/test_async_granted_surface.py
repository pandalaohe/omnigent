"""``sys_call_async`` and ``execute_tool`` honour the agent spec's granted tool surface."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.runner import tool_dispatch
from omnigent.spec.types import AgentSpec, ExecutorSpec, SharePolicy, ToolsConfig

pytestmark = pytest.mark.asyncio


def _spawn_kwargs(spec: AgentSpec | None, harness: str | None = None) -> dict[str, Any]:
    return {
        "effective_harness": harness,
        "server_client": None,
        "terminal_registry": None,
        "resource_registry": None,
        "agent_spec": spec,
        "conversation_id": "conv_granted",
        "task_id": None,
        "agent_id": None,
        "agent_name": None,
        "runner_workspace": None,
        "mcp_manager": None,
        "filesystem_registry": None,
    }


async def _call_sys_call_async(
    spec: AgentSpec | None,
    target: str,
    *,
    inbox: asyncio.Queue[dict[str, Any]],
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]],
    harness: str | None = None,
) -> str:
    return await tool_dispatch.execute_tool(
        tool_name="sys_call_async",
        arguments=json.dumps({"tool": target, "args": "{}"}),
        agent_spec=spec,
        conversation_id="conv_granted",
        session_inbox=inbox,
        session_async_tasks=tasks,
        effective_harness=harness,
    )


async def test_sys_call_async_refuses_target_outside_granted_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only agent cannot reach ``sys_os_shell`` by naming it as an async target."""
    executed: list[str] = []

    async def _record_os_env(tool_name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(tool_name)
        return "should not run"

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _record_os_env)
    spec = AgentSpec(spec_version=1, async_enabled=True)  # no os_env → no sys_os_*
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}

    out = await _call_sys_call_async(spec, "sys_os_shell", inbox=inbox, tasks=tasks)

    assert out.startswith("Error: sys_call_async refused:")
    assert "sys_os_shell" in out
    assert "not enabled" in out
    assert tasks == {}
    assert inbox.empty()
    await asyncio.sleep(0)
    assert executed == []


async def test_sys_call_async_granted_target_reports_through_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target the spec grants still runs in the background and lands in the inbox."""

    async def _fake_session_query(tool_name: str, *_a: Any, **_kw: Any) -> str:
        return json.dumps({"ran": tool_name})

    monkeypatch.setattr(tool_dispatch, "_execute_session_query_tool", _fake_session_query)
    spec = AgentSpec(spec_version=1, async_enabled=True)  # sys_session_list is always registered
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}

    handle = json.loads(
        await _call_sys_call_async(spec, "sys_session_list", inbox=inbox, tasks=tasks)
    )
    assert handle["status"] == "in_progress"
    bg_task, _evt = tasks[handle["handle_id"]]
    await bg_task

    item = inbox.get_nowait()
    assert item["handle_id"] == handle["handle_id"]
    assert item["tool_name"] == "sys_session_list"
    assert item["status"] == "completed"
    assert json.loads(item["output"]) == {"ran": "sys_session_list"}

    drained = await tool_dispatch.execute_tool(
        tool_name="sys_read_inbox",
        arguments="{}",
        agent_spec=spec,
        conversation_id="conv_granted",
        session_inbox=inbox,
        session_async_tasks=tasks,
    )
    assert drained == "Inbox is empty — no completed tasks."


async def test_sys_call_async_without_spec_is_not_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec-less dispatch has no granted surface to check and keeps working."""

    async def _fake(**_kw: Any) -> str:
        return "ok"

    monkeypatch.setattr(tool_dispatch, "execute_tool", _fake)
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}

    handle = json.loads(
        tool_dispatch._spawn_async_tool(
            {"tool": "sys_os_shell", "args": "{}"},
            session_inbox=inbox,
            session_async_tasks=tasks,
            **_spawn_kwargs(None),
        )
    )
    await tasks[handle["handle_id"]][0]
    assert inbox.get_nowait()["status"] == "completed"


async def test_sys_call_async_failure_result_is_failed_and_wakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``Error:`` target result lands as ``failed`` and wakes the dispatcher."""
    from omnigent.runner import app as runner_app

    async def _fake_failure(**_kw: Any) -> str:
        return "Error: boom"

    monkeypatch.setattr(tool_dispatch, "execute_tool", _fake_failure)
    waker_calls: list[tuple[str, str, str, str]] = []

    def _record_wake(*args: str) -> None:
        waker_calls.append(args)

    monkeypatch.setattr(runner_app, "_async_result_waker", _record_wake)
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}

    handle = json.loads(
        tool_dispatch._spawn_async_tool(
            {"tool": "sys_os_shell", "args": "{}"},
            session_inbox=inbox,
            session_async_tasks=tasks,
            **_spawn_kwargs(None),
        )
    )
    await tasks[handle["handle_id"]][0]

    item = inbox.get_nowait()
    assert item["status"] == "failed"
    assert item["output"] == "Error: boom"
    assert waker_calls == [("conv_granted", handle["handle_id"], "sys_os_shell", "failed")]


async def test_sys_call_async_success_result_wakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful target result lands as ``completed`` and wakes the dispatcher."""
    from omnigent.runner import app as runner_app

    async def _fake_success(**_kw: Any) -> str:
        return json.dumps({"ran": True})

    monkeypatch.setattr(tool_dispatch, "execute_tool", _fake_success)
    waker_calls: list[tuple[str, str, str, str]] = []

    def _record_wake(*args: str) -> None:
        waker_calls.append(args)

    monkeypatch.setattr(runner_app, "_async_result_waker", _record_wake)
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}

    handle = json.loads(
        tool_dispatch._spawn_async_tool(
            {"tool": "sys_os_shell", "args": "{}"},
            session_inbox=inbox,
            session_async_tasks=tasks,
            **_spawn_kwargs(None),
        )
    )
    await tasks[handle["handle_id"]][0]

    item = inbox.get_nowait()
    assert item["status"] == "completed"
    assert waker_calls == [("conv_granted", handle["handle_id"], "sys_os_shell", "completed")]


async def test_execute_tool_refuses_ungranted_tool_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The membership check also guards direct ``execute_tool`` calls."""
    executed: list[str] = []

    async def _record_os_env(tool_name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(tool_name)
        return "should not run"

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _record_os_env)
    spec = AgentSpec(spec_version=1)

    out = json.loads(
        await tool_dispatch.execute_tool(
            tool_name="sys_os_shell",
            arguments=json.dumps({"command": "id"}),
            agent_spec=spec,
        )
    )
    assert "sys_os_shell" in out["error"]
    assert "not enabled" in out["error"]
    assert executed == []


@pytest.mark.parametrize("with_spec", [False, True])
async def test_session_create_unknown_arguments_have_no_server_side_effects(
    with_spec: bool,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "conv_parent"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await tool_dispatch.execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps({"args": "the child brief", "mesage": "typo"}),
                agent_spec=AgentSpec(spec_version=1, spawn=True) if with_spec else None,
                conversation_id="conv_parent",
                server_client=client,
            )
        )

    assert requests == []
    assert "args" in result["error"] and "mesage" in result["error"]
    assert "accepted" in result["error"].lower() and "message" in result["error"]


@pytest.mark.parametrize("spawn", [False, True])
@pytest.mark.parametrize("peer_enabled", [False, True])
@pytest.mark.parametrize("harness", ["claude-sdk", "codex-native"])
async def test_system_tool_argument_keys_match_each_advertised_schema(
    monkeypatch: pytest.MonkeyPatch, spawn: bool, peer_enabled: bool, harness: str
) -> None:
    monkeypatch.setattr("omnigent.runtime.get_terminal_registry", Mock)
    spec = AgentSpec(
        spec_version=1,
        spawn=spawn,
        timers=True,
        agent_session_sharing=SharePolicy.PUBLIC,
        terminals={"shell": TerminalEnvSpec()},
        tools=ToolsConfig(agents=["worker"] if spawn else []),
        sub_agents=[AgentSpec(spec_version=1, name="worker")] if spawn else [],
    )
    monkeypatch.setattr(tool_dispatch, "_peer_messaging_enabled_for", lambda _cid: peer_enabled)
    monkeypatch.setattr(tool_dispatch, "_session_open_enabled_for", lambda _cid: peer_enabled)
    manager = tool_dispatch.ToolManager(
        spec,
        peer_messaging_enabled=peer_enabled,
        session_open_enabled=peer_enabled,
        os_env_schema_only=True,
        skill_registry=[],
    )
    try:
        schemas = {s["function"]["name"]: s["function"] for s in manager.get_tool_schemas()}
    finally:
        manager.shutdown()
    if harness == "codex-native":
        schemas.update(
            {
                s["name"]: s
                for s in tool_dispatch.build_native_relay_tool_schemas(
                    spec,
                    peer_messaging_enabled=peer_enabled,
                    session_open_enabled=peer_enabled,
                )
            }
        )

    for name, schema in schemas.items():
        if not name.startswith("sys_"):
            continue
        parameters = schema["parameters"]
        # The key gate must leave value validation to the existing handlers.
        for key in parameters.get("properties", {}):
            assert (
                tool_dispatch._ungranted_tool_reason(
                    name,
                    spec,
                    harness,
                    peer_messaging_enabled=peer_enabled,
                    session_open_enabled=peer_enabled,
                    arguments={key: None},
                )
                is None
            ), (name, key)
        if parameters.get("additionalProperties") is False:
            result = json.loads(
                await tool_dispatch.execute_tool(
                    tool_name=name,
                    arguments=json.dumps({"unexpected_field": "value must not be echoed"}),
                    agent_spec=spec,
                    effective_harness=harness,
                )
            )
            assert "unexpected_field" in result["error"], name
            assert "value must not be echoed" not in result["error"], name
            for key in parameters.get("properties", {}):
                assert key in result["error"], (name, key)


@pytest.mark.parametrize(
    "extra_schema",
    [
        {},
        {"additionalProperties": True},
        {"additionalProperties": {"type": "string"}},
        {"additionalProperties": False, "patternProperties": {"^extra_": {}}},
    ],
)
async def test_schema_allowed_additional_keys_are_preserved(extra_schema: dict[str, Any]) -> None:
    assert (
        tool_dispatch._unknown_tool_argument_reason(
            "sys_example",
            {"extra_key": "allowed"},
            {"properties": {"message": {}}, **extra_schema},
        )
        is None
    )


@pytest.mark.parametrize("id_key", ["handle_id", "task_id"])
async def test_cancel_async_accepts_canonical_and_legacy_handle_keys(id_key: str) -> None:
    task = asyncio.create_task(asyncio.sleep(0, result="done"))
    cancel_event = asyncio.Event()
    try:
        output = await tool_dispatch.execute_tool(
            tool_name="sys_cancel_async",
            arguments=json.dumps({id_key: "handle_example"}),
            agent_spec=AgentSpec(spec_version=1),
            session_async_tasks={"handle_example": (task, cancel_event)},
        )
        assert json.loads(output) == {"cancelled": True, "handle_id": "handle_example"}
        assert cancel_event.is_set()
    finally:
        await task


async def test_native_harness_spec_keeps_relayed_sys_os_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native harnesses get ``sys_os_*`` from the relay regardless of ``os_env``."""

    async def _fake_os_env(tool_name: str, *_a: Any, **_kw: Any) -> str:
        return f"ran {tool_name}"

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)
    spec = AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    out = await tool_dispatch.execute_tool(
        tool_name="sys_os_read",
        arguments=json.dumps({"path": "x"}),
        agent_spec=spec,
    )
    assert out == "ran sys_os_read"


async def test_granted_tool_names_cached_per_spec_instance() -> None:
    """The surface is computed once per live spec object and harness."""
    spec = AgentSpec(spec_version=1, async_enabled=True)
    first = tool_dispatch._granted_tool_names(spec)
    assert first is tool_dispatch._granted_tool_names(spec)
    assert {"sys_call_async", "sys_read_inbox", "sys_cancel_async"} <= first
    assert "sys_os_shell" not in first
    # A different effective harness is a different surface, so a different entry.
    native = tool_dispatch._granted_tool_names(spec, "claude-native")
    assert native is not first
    assert "sys_os_shell" in native


async def test_native_override_on_non_native_spec_keeps_relayed_sys_os_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session pinned to a native harness gets the relay's ``sys_os_*``.

    The spec declares ``claude-sdk`` and no ``os_env``, but the session runs
    ``claude-native``, whose relay advertises ``sys_os_*`` unconditionally.
    Gating on the spec's declaration instead of the session's harness would
    refuse tools the model was actually shown.
    """

    async def _fake_os_env(tool_name: str, *_a: Any, **_kw: Any) -> str:
        return f"ran {tool_name}"

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)
    spec = AgentSpec(
        spec_version=1,
        async_enabled=True,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    direct = await tool_dispatch.execute_tool(
        tool_name="sys_os_read",
        arguments=json.dumps({"path": "x"}),
        agent_spec=spec,
        effective_harness="claude-native",
    )
    assert direct == "ran sys_os_read"

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    handle = json.loads(
        await _call_sys_call_async(
            spec, "sys_os_read", inbox=inbox, tasks=tasks, harness="claude-native"
        )
    )
    assert handle["status"] == "in_progress"
    await tasks[handle["handle_id"]][0]
    item = inbox.get_nowait()
    assert item["status"] == "completed"
    assert item["output"] == "ran sys_os_read"


async def test_sdk_override_on_native_spec_refuses_sys_os_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native-declared spec running an SDK harness loses the relay allowance.

    Without ``os_env`` the SDK session never advertises ``sys_os_*``, so the
    spec's ``claude-native`` declaration must not keep granting them.
    """
    executed: list[str] = []

    async def _record_os_env(tool_name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(tool_name)
        return "should not run"

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _record_os_env)
    spec = AgentSpec(
        spec_version=1,
        async_enabled=True,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    direct = json.loads(
        await tool_dispatch.execute_tool(
            tool_name="sys_os_shell",
            arguments=json.dumps({"command": "id"}),
            agent_spec=spec,
            effective_harness="claude-sdk",
        )
    )
    assert "sys_os_shell" in direct["error"]
    assert "not enabled" in direct["error"]

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    spawned = await _call_sys_call_async(
        spec, "sys_os_shell", inbox=inbox, tasks=tasks, harness="claude-sdk"
    )
    assert spawned.startswith("Error: sys_call_async refused:")
    assert tasks == {}
    assert inbox.empty()
    await asyncio.sleep(0)
    assert executed == []


@pytest.mark.parametrize("os_env_option", ["fork", "start_in_scratch"])
async def test_surface_probe_does_not_fork_the_working_tree(
    monkeypatch: pytest.MonkeyPatch,
    os_env_option: str,
) -> None:
    """Metadata probes never construct an environment, even for fork/scratch specs."""
    from omnigent.inner import os_env as os_env_mod
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    create_env = Mock(side_effect=AssertionError("metadata must not construct an OS environment"))
    monkeypatch.setattr(os_env_mod, "create_os_environment", create_env)
    spec = AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=".",
            sandbox=OSEnvSandboxSpec(type="none"),
            fork=os_env_option == "fork",
            start_in_scratch=os_env_option == "start_in_scratch",
        ),
    )

    granted = tool_dispatch._granted_tool_names(spec)
    schemas = tool_dispatch.build_native_relay_tool_schemas(spec)
    request_schemas = tool_dispatch.ToolManager(spec, os_env_schema_only=True).get_tool_schemas()

    assert granted >= tool_dispatch._OS_ENV_TOOLS
    assert {schema["name"] for schema in schemas} >= tool_dispatch._OS_ENV_TOOLS
    assert {
        schema["function"]["name"] for schema in request_schemas
    } >= tool_dispatch._OS_ENV_TOOLS
    create_env.assert_not_called()
