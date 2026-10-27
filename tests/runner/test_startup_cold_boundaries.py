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
@pytest.mark.parametrize("change", ["new", "same", "forget", "refresh"])
async def test_late_exemption_cannot_cross_a_new_control_registration(change):
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(_request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"exempt": True})

    async with httpx.AsyncClient(
        base_url="http://server.test", transport=httpx.MockTransport(respond)
    ) as client:
        ref = _attempt().model_copy(update={"mode": "startup_only"})
        admission = StartupAdmission(client, root_session_id="root", root_attempt=ref)
        pending = asyncio.create_task(admission.native_admitter("root")())
        await entered.wait()
        if change == "forget":
            admission.forget("root")
        elif change == "refresh":
            newer = asyncio.create_task(admission.refresh_exemption("root"))
            await asyncio.sleep(0)
        else:
            admission.register(
                "root",
                ref if change == "same" else ref.model_copy(update={"attempt_id": "d" * 32}),
            )
        release.set()
        with pytest.raises(RuntimeError, match="control changed"):
            await pending
        if change == "refresh":
            assert await newer
        else:
            assert not admission.recreate_is_exempt("root")


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
@pytest.mark.parametrize("harness", ["native", "codex", "claude-sdk", "acp"])
async def test_settled_recreate_exemption_precedes_cold_claim_and_is_session_local(harness):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(
            200, json={"exempt": request.url.path.startswith("/v1/sessions/old/")}
        )

    async with httpx.AsyncClient(
        base_url="http://server.test", transport=httpx.MockTransport(respond)
    ) as client:
        ref = _attempt().model_copy(update={"mode": "startup_only"})
        admission = StartupAdmission(client, root_session_id="old", root_attempt=ref)
        if harness == "native":
            await admission.native_admitter("old")()
        else:
            await admission.admit_harness("old", harness)
        assert not admission.required("old")
        assert not await admission.opening_settled("old", ref)
        with pytest.raises(RuntimeError):
            await admission.admit_harness("new", "codex")
    assert requests[0].endswith("/startup/recreate-exemption")
    assert not any(path == "/v1/sessions/old/startup/claims" for path in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["native", "acp", "codex", "claude-sdk"])
async def test_queue_mode_claims_real_cold_boundary_and_keeps_missing_child_closed(harness):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"granted": True})

    ref = _attempt().model_copy(update={"mode": "startup_only", "policy_revision": 3})
    async with httpx.AsyncClient(
        base_url="http://server.test", transport=httpx.MockTransport(respond)
    ) as client:
        admission = StartupAdmission(client, root_session_id="root", root_attempt=ref)
        if harness == "native":
            await admission.native_admitter("root")()
            with pytest.raises(RuntimeError):
                await admission.native_admitter("child")()
        else:
            await admission.admit_harness("root", harness)
            with pytest.raises(RuntimeError):
                await admission.admit_harness("child", harness)
    assert len(requests) == 1
    assert requests[0]["boundary"] == (
        "native_prepare" if harness == "native" else "harness_prepare"
    )
    assert requests[0]["attempt"] == ref.model_dump()


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
@pytest.mark.parametrize("recreated", [False, True])
async def test_enabling_cold_protection_keeps_warm_non_codex_messages_working(recreated):
    class Server(NullServerClient):
        async def post(self, url, **kwargs):
            if url.endswith("/startup/recreate-exemption"):
                return httpx.Response(200, json={"exempt": recreated})
            return await super().post(url, **kwargs)

    session_id = "warm-recreated" if recreated else "warm-existing"
    executor = MockExecutor()
    executor.enqueue_response("first")
    executor.enqueue_response("followup")
    adapter = ExecutorAdapter(lambda: executor)
    harness = adapter.build()
    harness.state.conversation_id = session_id
    spec = AgentSpec(
        spec_version=1,
        name="agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    async with _runner_client(harness) as harness_client:
        app = create_runner_app(
            process_manager=_FakeProcessManager(harness_client),
            spec_resolver=AsyncMock(return_value=spec),
            server_client=Server(),
        )
        async with _runner_client(app) as client:
            assert (
                await client.post(
                    "/v1/sessions", json={"session_id": session_id, "agent_id": "agent"}
                )
            ).status_code == 201
            for turn in range(2):
                if turn:
                    ref = (
                        _attempt().model_copy(update={"mode": "startup_only"})
                        if recreated
                        else _attempt()
                    )
                    app.state.startup_admission.register("root", ref)
                    if recreated:
                        adapter._executor = None  # The old local executor was reaped.
                        app.state.startup_admission.register(session_id, None)
                response = await client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "model": "agent",
                        "content": [],
                        **({"startup_required": True} if recreated and turn else {}),
                    },
                )
                assert response.status_code == 202

                async def settled():
                    while session_id in app.state.active_turns:
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(settled(), 3)
            events = list(app.state.session_event_queues[session_id]._queue)
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
async def test_native_resource_ensure_reports_empty_ready_after_local_adapter(
    tmp_path, monkeypatch
):
    server = []
    order = []

    def respond(request):
        body = json.loads(request.content)
        server.append((request.url.path, body))
        return httpx.Response(
            200,
            json={"granted": True} if request.url.path.endswith("claims") else {"accepted": True},
        )

    async def launch(_ctx):
        order.append("ready")
        return _terminal_view("native")

    monkeypatch.setattr("omnigent.runner.native._launch_pi", launch)
    ref = _attempt().model_copy(update={"mode": "startup_only", "input_id": None})
    async with httpx.AsyncClient(
        base_url="http://server.test", transport=httpx.MockTransport(respond)
    ) as client:
        app = create_runner_app(
            server_client=client,
            process_manager=HarnessProcessManager(tmp_parent=tmp_path),
            root_startup_session_id="root",
            root_startup_attempt=ref,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner.test"
        ) as runner:
            response = await runner.post(
                "/v1/sessions/root/resources/terminals",
                json={
                    "terminal": "pi",
                    "session_key": "main",
                    "ensure_native_terminal": True,
                },
            )
    assert response.status_code == 200, response.text
    assert order == ["ready"]
    assert [path.rsplit("/", 1)[-1] for path, _ in server] == ["claims", "receipts"]
    assert server[-1][1]["phase"] == "opening_settled"


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
