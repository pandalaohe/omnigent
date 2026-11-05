"""Scoped relay -> production proxy/server/runner routes -> real stdio MCP."""

import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.policies.types import PolicyResult
from omnigent.runner.mcp_execution_registry import MCP_OPERATION_ID_PARAM
from omnigent.runner.mcp_manager import RunnerMcpManager
from omnigent.runner.pi_scope_relay import PiScopeRelay
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.server.permission_tiers import (
    PERMISSION_CAPABILITY_LABEL_KEY,
    PERMISSION_CAPABILITY_VALUE,
    ConfirmedPermissionState,
    read_permission_state,
)
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import orchestration
from omnigent.server.schemas import ElicitationResult
from omnigent.spec.types import AgentSpec, MCPServerConfig, Phase, PolicyAction
from omnigent.tools.mcp import McpElicitationRequired
from tests.runner.test_mcp_routes import Harness, _FakeConnection
from tests.runner.test_permission_tier_runtime import _publish_scope, _scoped_runtime
from tests.server.routes.test_sessions_mcp_proxy_policy_retry import (
    _FixedPolicyEngine,
    _StubConversationStore,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "allow",
        "replay",
        "policy-deny",
        "transformed",
        "revoked",
        "wrong-name",
        "wrong-arguments",
        "missing-operation",
        "wrong-operation",
        "server-replay",
        "registry-replay",
        "advisor-replay",
        "elicitation-deny",
        "elicitation-allow",
        "elicitation-ask",
        "elicitation-transformed",
    ],
)
async def test_scoped_call_reaches_real_mcp_without_replacing_its_lease(
    tmp_path, monkeypatch, fault
):
    runtime, _parent, _child, _source, _registry = _scoped_runtime(tmp_path, "ask")
    child = runtime.open_pi_child_scope("session-a")
    tool_name = (
        "mcp__omnigent__sys_advise_models" if fault == "advisor-replay" else "fixture__echo"
    )
    expected = runtime.pi_scopes.record(child.principal).expected
    _publish_scope(
        runtime,
        child,
        [{"name": tool_name, "sourceInfo": {"path": expected["extension_path"]}}],
    )
    arguments = {"text": "SCOPED_MCP_EXECUTED"}
    receipt = runtime.pi_origin_receipt(
        "session-a",
        child.credential,
        call_id="echo",
        name=tool_name,
        arguments=arguments,
        local_execution=False,
    )
    binding, decision = runtime.inspect(
        "session-a",
        call_id=receipt.binding.call_id,
        name=tool_name,
        arguments=arguments,
        expected_revision=3,
        origin_receipt=receipt.token,
        local_execution=False,
    )
    assert decision.action.value == "ASK"
    assert runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
        origin_receipt=receipt.token,
    )
    operation = "sdk-tool-operation"
    assert runtime.verify_execute(
        "session-a",
        call_id=binding.call_id,
        revision=3,
        arguments_digest=binding.arguments_digest,
        name=binding.name,
        arguments=arguments,
        operation_id=operation,
        step="initial",
        origin_receipt=receipt.token,
    )
    metadata = {
        "call_id": binding.call_id,
        "expected_revision": 3,
        "arguments_digest": binding.arguments_digest,
        "name": binding.name,
        "arguments": arguments,
        "operation_id": operation,
        "step": "initial",
        "pi_origin_receipt": receipt.token,
    }
    spec = AgentSpec(
        spec_version=1,
        name="scoped-mcp",
        mcp_servers=[
            MCPServerConfig(
                name="fixture",
                transport="stdio",
                command=sys.executable,
                args=[str(Path(__file__).parents[1] / "tools/fixtures/echo_stdio_mcp_server.py")],
            )
        ],
    )
    if fault == "registry-replay":
        spec.mcp_servers = [MCPServerConfig(name="fixture", transport="registry")]
    state = runtime.state("session-a")
    conversation = Conversation(
        id="session-a",
        created_at=1,
        updated_at=1,
        root_conversation_id="session-a",
        host_id=state.host_id,
        agent_id="fixture-agent",
        workspace=state.workspace,
        labels={
            **ConfirmedPermissionState("ask", 3, "ready").labels("pi-native"),
            PERMISSION_CAPABILITY_LABEL_KEY: PERMISSION_CAPABILITY_VALUE,
        },
    )
    assert read_permission_state(conversation.labels, harness="pi-native").status == "ready"
    store = _StubConversationStore(conversation)
    engine = _FixedPolicyEngine(
        PolicyResult(
            action=PolicyAction.DENY if fault == "policy-deny" else PolicyAction.ALLOW,
            reason="fixture policy denied" if fault == "policy-deny" else "",
            data={"text": "changed"} if fault == "transformed" else None,
        )
    )
    if fault == "revoked":
        evaluate = engine.evaluate

        async def revoke_during_policy(ctx):
            if ctx.phase == Phase.TOOL_CALL:
                runtime.revoke_pi_scope(child.principal)
            return await evaluate(ctx)

        engine.evaluate = revoke_during_policy
    monkeypatch.setattr(sessions, "_load_agent_spec_for_session", lambda *_: spec)
    monkeypatch.setattr(sessions, "_resolve_harness", lambda *_args, **_kwargs: "pi-native")
    monkeypatch.setattr(
        sessions, "_build_policy_engine_from_spec", lambda *_args, **_kwargs: engine
    )
    manager = RunnerMcpManager(stdio_cwd=Path(state.workspace))
    mcp_calls = []
    server_calls = []
    elicitation = fault.startswith("elicitation-")
    connection = _FakeConnection(output="echo: SCOPED_MCP_EXECUTED")
    call_mcp = manager.call_tool

    async def observed_mcp_call(*args, **kwargs):
        mcp_calls.append(args[1])
        if elicitation:
            raise McpElicitationRequired(
                input_requests={"input-1": {"params": {"message": "Continue?"}}},
                request_state="mcp-state",
                tool_name=tool_name,
                arguments=arguments,
            )
        return await call_mcp(*args, **kwargs)

    monkeypatch.setattr(manager, "call_tool", observed_mcp_call)
    if elicitation:
        monkeypatch.setattr(
            manager,
            "_resolve_tool_route",
            lambda *_: (SimpleNamespace(connection=connection), "echo"),
        )

        async def answer_elicitation(*_args, **_kwargs):
            engine.result = PolicyResult(
                action={
                    "elicitation-deny": PolicyAction.DENY,
                    "elicitation-ask": PolicyAction.ASK,
                }.get(fault, PolicyAction.ALLOW),
                reason="policy changed during MCP input",
                data={"text": "changed"} if fault == "elicitation-transformed" else None,
            )
            return ElicitationResult(action="accept")

        monkeypatch.setattr(
            orchestration, "_publish_and_wait_for_harness_elicitation", answer_elicitation
        )

    async def execute_server(values):
        server_calls.append(values)
        return {"result": {"output": "echo: SCOPED_MCP_EXECUTED"}}

    if fault == "registry-replay":
        from omnigent.server import registry_gateway

        async def execute_registry(_request, _conv, _config, _name, values, _user):
            return await execute_server(values)

        monkeypatch.setattr(registry_gateway, "execute_registry_tool", execute_registry)
    if fault == "advisor-replay":

        async def advise(rpc_id, _conv, values, *_args, **_kwargs):
            await execute_server(values)
            return sessions._mcp_ok_response(
                rpc_id, {"content": [{"type": "text", "text": "echo: SCOPED_MCP_EXECUTED"}]}
            )

        monkeypatch.setattr(orchestration, "_handle_advise_models_mcp", advise)
    harness = Harness(mcp_manager=manager, permission_tiers=runtime, harness="pi-native")
    harness.spec_cache["session-a"] = spec
    server_requests, execution_requests = [], []
    async with harness.client() as runner_client:

        async def runner_transport(request):
            body = json.loads(request.content)
            if request.url.path.endswith("/mcp/execute"):
                execution_requests.append(body)
                return await runner_client.post(request.url.path, json=body)
            try:
                if body["type"] == "permission_tier_inspect":
                    extra = (
                        {"execution_operation_id": body["execution_operation_id"]}
                        if "execution_operation_id" in body
                        else {}
                    )
                    checked, verdict = runtime.inspect(
                        "session-a",
                        call_id=body["call_id"],
                        name=body["name"],
                        arguments=body["arguments"],
                        expected_revision=body["expected_revision"],
                        local_execution=runtime.pi_execution_kind(
                            "session-a",
                            body.get("pi_origin_receipt"),
                            legacy_local_execution=body["local_execution"],
                        ),
                        origin_receipt=body.get("pi_origin_receipt"),
                        **extra,
                    )
                    response = {
                        **asdict(checked),
                        "verdict": verdict.action.value,
                        "reason": verdict.reason,
                    }
                elif body["type"] == "permission_tier_authorize":
                    response = {
                        "authorized": runtime.authorize(
                            "session-a",
                            call_id=body["call_id"],
                            name=body["name"],
                            arguments_digest=body["arguments_digest"],
                            expected_revision=3,
                            origin_receipt=body.get("pi_origin_receipt"),
                        )
                    }
                elif body["type"] == "permission_tier_execute":
                    response = {
                        "executed": runtime.verify_execute(
                            "session-a",
                            call_id=body["call_id"],
                            revision=body["expected_revision"],
                            arguments_digest=body["arguments_digest"],
                            name=body["name"],
                            arguments=body["arguments"],
                            operation_id=body["operation_id"],
                            step=body["step"],
                            origin_receipt=body.get("pi_origin_receipt"),
                        )
                    }
                else:
                    response = {
                        "finished": runtime.finish(
                            "session-a",
                            call_id=body["call_id"],
                            revision=3,
                            origin_receipt=body.get("pi_origin_receipt"),
                        )
                    }
                return httpx.Response(
                    200,
                    json={
                        "session_id": "session-a",
                        "host_id": state.host_id,
                        "call_id": body["call_id"],
                        "tier_revision": 3,
                        "permission_tiers_capability": "permission_tiers_v1",
                        **response,
                    },
                )
            except ValueError:
                return httpx.Response(409, json={"error": "permission_tier_stale"})

        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(runner_transport)
        ) as tunnel:

            async def get_runner(*_args, **_kwargs):
                return tunnel

            monkeypatch.setattr(sessions, "_get_runner_client", get_runner)

            async def server_transport(request):
                body = json.loads(request.content)
                if fault == "registry-replay":
                    body["params"]["name"] = "fixture__" + body["params"]["name"]
                if fault == "missing-operation":
                    body["params"].pop(MCP_OPERATION_ID_PARAM)
                elif fault == "wrong-operation":
                    body["params"][MCP_OPERATION_ID_PARAM] = "different-operation"
                server_requests.append(body)
                server_options = {}
                if fault == "server-replay":
                    server_options["execute_tool"] = execute_server
                if fault == "registry-replay" or elicitation:
                    server_options["request"] = SimpleNamespace(
                        app=SimpleNamespace(state=SimpleNamespace(mcp_registry=object()))
                    )
                result = await sessions._handle_mcp_tools_call(
                    body["id"],
                    "session-a",
                    body["params"],
                    store,
                    object(),
                    None,
                    **server_options,
                )
                if fault in ("replay", "server-replay", "registry-replay", "advisor-replay"):
                    repeated = await sessions._handle_mcp_tools_call(
                        body["id"],
                        "session-a",
                        body["params"],
                        store,
                        object(),
                        None,
                        **server_options,
                    )
                    if fault == "replay":
                        assert repeated.body == result.body
                    else:
                        assert len(server_calls) == 1, server_calls
                        assert "error" not in json.loads(result.body)
                        assert "error" in json.loads(repeated.body)
                return httpx.Response(200, content=result.body)

            async with httpx.AsyncClient(
                base_url="http://server", transport=httpx.MockTransport(server_transport)
            ) as client:
                proxy = ProxyMcpManager("session-a", client, execution_registry=harness.registry)

                async def dispatch(name, values):
                    if fault == "wrong-name":
                        name = "different__echo"
                    if fault == "wrong-arguments":
                        values = {"text": "changed"}
                    return await proxy.call_tool(spec, name, values)

                side = PiScopeRelay(runtime, "session-a")
                try:
                    try:
                        result = await side.execute_rpc(
                            "Bearer " + child.credential,
                            json.dumps(metadata),
                            dispatch,
                            {binding.name},
                        )
                    except ValueError:
                        assert fault in ("wrong-name", "wrong-arguments")
                        assert server_requests == []
                    else:
                        if fault in ("server-replay", "registry-replay", "advisor-replay"):
                            assert "echo: SCOPED_MCP_EXECUTED" in result["result"], result
                            assert server_calls == [arguments]
                            assert execution_requests == []
                            assert mcp_calls == []
                        elif elicitation:
                            assert mcp_calls == [tool_name]
                            if fault == "elicitation-allow":
                                assert "echo: SCOPED_MCP_EXECUTED" in result["result"], result
                                assert len(execution_requests) == 2
                                assert len(connection.calls) == 1
                                assert connection.calls[0]["input_responses"] == {
                                    "input-1": {"action": "accept"}
                                }
                            else:
                                assert connection.calls == [], connection.calls
                                assert len(execution_requests) == 1
                                assert "echo: SCOPED_MCP_EXECUTED" not in str(result)
                        elif fault in ("allow", "replay"):
                            assert "echo: SCOPED_MCP_EXECUTED" in result["result"], result
                            assert len(execution_requests) == (2 if fault == "replay" else 1)
                            assert mcp_calls == ["fixture__echo"]
                        else:
                            assert "echo: SCOPED_MCP_EXECUTED" not in str(result)
                            assert execution_requests == []
                            assert mcp_calls == []
                    assert runtime.finish(
                        "session-a",
                        call_id=binding.call_id,
                        revision=3,
                        origin_receipt=receipt.token,
                    ), "the SDK owner settles its original call"
                finally:
                    side.bootstrap.close()
                    await manager.shutdown()
