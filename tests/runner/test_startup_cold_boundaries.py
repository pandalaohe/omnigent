"""Cold admission precedes configuration, teardown and physical adapter work."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.inner.executor import MockExecutor
from omnigent.runner import create_runner_app
from omnigent.runner.native import _ensure_native_terminal, _launch_native_terminal
from omnigent.runner.startup_admission import StartupAdmission
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager, _SubprocessEntry
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.startup_protocol import StartupAttemptRef
from tests.runner.conftest import _FakeProcessManager, _runner_client
from tests.runner.helpers import NullServerClient
from tests.runner.test_app_sessions_native_workflow_init import (
    _ensure_ctx,
    _FakeEnsureRegistry,
    _launch_ctx,
    _terminal_view,
)


@pytest.mark.asyncio
async def test_native_cold_refusal_precedes_context_probes(monkeypatch):
    denied, build = AsyncMock(side_effect=RuntimeError("startup not admitted")), AsyncMock()
    ctx = _launch_ctx(startup_required=True, admit_cold=denied)
    with pytest.raises(RuntimeError, match="startup not admitted"):
        await _launch_native_terminal(
            "pi-native", ctx, ensure_locks={}, build_context=build, reraise=True
        )
    build.assert_not_awaited()
    denied.assert_awaited_once()


@pytest.mark.asyncio
async def test_harness_shutdown_during_admission_prevents_spawn(tmp_path, monkeypatch):
    manager = HarnessProcessManager(tmp_parent=tmp_path)
    manager._started = True

    async def shutdown_while_admitting(*_args):
        manager._shutting_down = True

    manager.set_startup_admitter(shutdown_while_admitting)
    spawn = AsyncMock()
    monkeypatch.setattr(manager, "_spawn_entry", spawn)
    with pytest.raises(RuntimeError, match="stopped during startup admission"):
        await manager.get_client("session", "pi")
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_replacement_refusal_preserves_existing_terminal():
    registry = _FakeEnsureRegistry(existing=_terminal_view("existing"))
    denied, build = AsyncMock(side_effect=RuntimeError("startup not admitted")), AsyncMock()
    ctx = replace(_ensure_ctx(registry), startup_required=True, admit_cold=denied)
    response = await _ensure_native_terminal(
        "goose",
        ctx,
        ensure_locks={},
        build_context=build,
        is_owned=lambda *_: False,
    )
    assert response.status_code >= 400
    assert registry.closed == []
    build.assert_not_awaited()
    denied.assert_awaited_once()


@pytest.mark.asyncio
async def test_warm_native_terminal_needs_no_new_start_permission():
    registry = _FakeEnsureRegistry(existing=_terminal_view("existing"))
    denied = AsyncMock(side_effect=RuntimeError("startup not admitted"))
    ctx = replace(_ensure_ctx(registry), startup_required=True, admit_cold=denied)
    assert (await _ensure_native_terminal("goose", ctx, ensure_locks={})).status_code == 200
    denied.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["cold", "exited", "agent", "model", "acp"])
async def test_harness_refusal_precedes_spawn_and_replacement_teardown(
    tmp_path, monkeypatch, reason
):
    manager = HarnessProcessManager(tmp_parent=tmp_path)
    manager._started = True
    harness, env = "pi", None
    if reason != "cold":
        entry = _SubprocessEntry(
            process=SimpleNamespace(returncode=0 if reason == "exited" else None),
            client=object(),
            endpoint=SimpleNamespace(socket_path=None),
            harness="claude-sdk" if reason == "agent" else "pi",
            model="previous",
        )
        manager._entries["session"] = entry
    if reason == "model":
        env = {"HARNESS_PI_MODEL": "next"}
    elif reason == "acp":
        manager._entries["session"].harness = "acp"
        harness, env = "acp", {"HARNESS_ACP_COMMAND": "next"}
    close, spawn = AsyncMock(), AsyncMock()
    monkeypatch.setattr(manager, "_close_entry", close)
    monkeypatch.setattr(manager, "_spawn_entry", spawn)
    denied = AsyncMock(side_effect=RuntimeError("startup not admitted"))
    manager.set_startup_admitter(denied)
    with pytest.raises(RuntimeError, match="startup not admitted"):
        await manager.get_client("session", harness, env=env)
    close.assert_not_awaited()
    spawn.assert_not_awaited()
    denied.assert_awaited_once_with("session", harness)


def _attempt() -> StartupAttemptRef:
    return StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="runtime",
        host_generation="generation",
    )


@pytest.mark.asyncio
async def test_bound_harness_claim_requires_explicit_server_grant():
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"granted": True})

    async with httpx.AsyncClient(
        base_url="http://server.test", transport=httpx.MockTransport(respond)
    ) as client:
        admission = StartupAdmission(client, root_session_id="root", root_attempt=_attempt())
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            await admission.admit_harness("child", "codex")
        assert requests == []
        await admission.admit_harness("root", "codex")

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/sessions/root/startup/claims"
    body = json.loads(requests[0].content)
    assert body["attempt"] == _attempt().model_dump()
    assert body["boundary"] == "harness_prepare"
    assert len(body["claim_id"]) == 32


@pytest.mark.asyncio
async def test_protected_runner_refuses_unregistered_side_contexts():
    async with httpx.AsyncClient() as client:
        admission = StartupAdmission(client)
        await admission.admit_harness("legacy", "codex")
        admission.register("root", _attempt())
        assert not admission.required("side-context")
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            await admission.admit_harness("side-context", "codex")
        with pytest.raises(RuntimeError, match="startup_proof_unavailable"):
            await admission.native_admitter("side-context")()
        admission.forget("root")
        assert not admission.required("replacement")
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            await admission.claim("replacement", "harness_prepare")


@pytest.mark.asyncio
async def test_enabling_cold_protection_keeps_warm_non_codex_messages_working():
    executor = MockExecutor()
    executor.enqueue_response("first")
    executor.enqueue_response("followup")
    harness = ExecutorAdapter(lambda: executor).build()
    harness.state.conversation_id = "warm"
    spec = AgentSpec(
        spec_version=1,
        name="agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    async with _runner_client(harness) as harness_client:
        app = create_runner_app(
            process_manager=_FakeProcessManager(harness_client),
            spec_resolver=AsyncMock(return_value=spec),
            server_client=NullServerClient(),
        )
        async with _runner_client(app) as client:
            assert (
                await client.post("/v1/sessions", json={"session_id": "warm", "agent_id": "agent"})
            ).status_code == 201
            for turn in range(2):
                if turn:
                    app.state.startup_admission.register("root", _attempt())
                response = await client.post(
                    "/v1/sessions/warm/events",
                    json={"type": "message", "role": "user", "model": "agent", "content": []},
                )
                assert response.status_code == 202

                async def settled():
                    while "warm" in app.state.active_turns:
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(settled(), 3)
            events = list(app.state.session_event_queues["warm"]._queue)
            assert sum(event.get("type") == "response.completed" for event in events) == 2
            assert all(event.get("type") != "response.failed" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload"),
    [(403, {"granted": True}), (200, {"granted": False}), (200, {"accepted": True})],
)
async def test_bound_harness_claim_refuses_non_grant(status, payload):
    async with httpx.AsyncClient(
        base_url="http://server.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=payload)),
    ) as client:
        admission = StartupAdmission(client)
        admission.register("child", _attempt())
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            await admission.admit_harness("child", "codex")


@pytest.mark.asyncio
async def test_missing_child_ref_and_native_proof_refuse_before_claim():
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"granted": True})

    async with httpx.AsyncClient(
        base_url="http://server.test", transport=httpx.MockTransport(respond)
    ) as client:
        admission = StartupAdmission(client, root_session_id="root", root_attempt=_attempt())
        admission.register("child", None)
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            await admission.admit_harness("child", "codex")
        with pytest.raises(RuntimeError, match="startup_proof_unavailable"):
            await admission.native_admitter("root")()
        with pytest.raises(RuntimeError, match="startup_proof_unavailable"):
            await admission.native_admitter("ordinary")()
        await StartupAdmission(client).native_admitter("ordinary")()
    assert requests == []


@pytest.mark.asyncio
async def test_runner_app_binds_root_and_child_claims_independently(tmp_path):
    paths = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"granted": True})

    async with httpx.AsyncClient(
        base_url="http://server.test", transport=httpx.MockTransport(respond)
    ) as client:
        manager = HarnessProcessManager(tmp_parent=tmp_path)
        app = create_runner_app(
            server_client=client,
            process_manager=manager,
            root_startup_session_id="root",
            root_startup_attempt=_attempt(),
        )
        app.state.register_startup_attempt("child", None)
        assert manager._startup_admitter is not None
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            await manager._startup_admitter("child", "codex")
        await manager._startup_admitter("root", "codex")
    assert paths == ["/v1/sessions/root/startup/claims"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ensure_native", [False, True])
async def test_native_primary_terminal_generic_route_refuses_before_launch(
    ensure_native, tmp_path
):
    async with httpx.AsyncClient(base_url="http://server.test") as server_client:
        app = create_runner_app(
            server_client=server_client,
            process_manager=HarnessProcessManager(tmp_parent=tmp_path),
            root_startup_session_id="root",
            root_startup_attempt=_attempt(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner.test"
        ) as runner_client:
            response = await runner_client.post(
                "/v1/sessions/root/resources/terminals",
                json={
                    "terminal": "antigravity",
                    "session_key": "main",
                    "ensure_native_terminal": ensure_native,
                    "spec": {"command": "unused"},
                },
            )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "startup_proof_unavailable"


@pytest.mark.asyncio
async def test_required_unsupported_harness_refuses_before_claim():
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"granted": True})

    async with httpx.AsyncClient(
        base_url="http://server.test", transport=httpx.MockTransport(respond)
    ) as client:
        admission = StartupAdmission(client, root_session_id="root", root_attempt=_attempt())
        with pytest.raises(RuntimeError, match="startup_proof_unavailable"):
            await admission.admit_harness("root", "acp")
    assert requests == []


@pytest.mark.asyncio
async def test_session_init_registers_missing_required_ref_before_initialization():
    async with httpx.AsyncClient(base_url="http://server.test") as server_client:
        app = create_runner_app(server_client=server_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner.test"
        ) as runner_client:
            response = await runner_client.post(
                "/v1/sessions",
                json={"session_id": "child", "startup_required": True},
            )
        admission = app.state.startup_admission
        assert admission.required("child")
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            await admission.admit_harness("child", "codex")
    assert response.status_code == 501


@pytest.mark.asyncio
async def test_message_registers_missing_required_ref_before_harness_spawn(tmp_path, monkeypatch):
    manager = HarnessProcessManager(tmp_parent=tmp_path)
    manager._started = True
    spawn = AsyncMock()
    monkeypatch.setattr(manager, "_spawn_entry", spawn)
    async with httpx.AsyncClient(base_url="http://server.test") as server_client:
        app = create_runner_app(server_client=server_client, process_manager=manager)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner.test"
        ) as runner_client:
            response = await runner_client.post(
                "/v1/sessions/child/events?stream=true",
                json={
                    "type": "message",
                    "content": [{"type": "input_text", "text": "work"}],
                    "startup_required": True,
                },
            )
        assert app.state.startup_admission.required("child")
    assert response.status_code == 503
    spawn.assert_not_awaited()
