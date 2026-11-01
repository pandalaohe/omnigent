"""The runner consumes trusted SDK startup frames before publishing turn output."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.runner import create_runner_app
from omnigent.runner.app import _relay_startup_proof
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.startup_protocol import StartupAttemptRef, StartupObservation
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

SESSION = "49ed0bd1f0cae058f05f48057e9f98cf"
AGENT = "880b5afda28ad55ff74cbeb9b5fc67fb"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["codex", "claude-sdk", "acp"])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    ("warm_harness", "registered"), [(False, True), (True, True), (True, False)]
)
async def test_shared_runner_initialization_only_suppresses_registered_recovery(
    source, resume, warm_harness, registered
):
    from uuid import uuid4

    from omnigent.runner.session_init_protocol import (
        SESSION_INIT_PROTOCOL_VERSION,
        RunnerSessionInitEnvelope,
        RunnerSessionInitSnapshot,
    )
    from tests.runner.test_app_sessions_native_workflow_messages import _FakeServerClient

    class Server(_FakeServerClient, NullServerClient):
        async def post(self, url, **kwargs):
            if url.endswith("/startup/recreate-exemption"):
                return _Accepted({"exempt": True})
            return await NullServerClient.post(self, url, **kwargs)

    class Manager(_FakeProcessManager):
        def set_startup_admitter(self, admitter):
            self.admitter = admitter

        async def get_client(self, session_id, harness, **kwargs):
            if not warm_harness:
                await self.admitter(session_id, harness)
            return await super().get_client(session_id, harness, **kwargs)

    session_id = uuid4().hex
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="old-runtime",
        host_generation="generation",
        mode="startup_only",
    )
    history = [
        {
            "id": ref.input_id,
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "old first input"}],
        }
    ]
    server = Server(history)
    stream_finished = asyncio.Event()
    harness = _ScriptedHarnessClient([], stream_finished=stream_finished)
    spec = AgentSpec(
        spec_version=1, name="agent", executor=ExecutorSpec(config={"harness": source})
    )
    app = create_runner_app(
        process_manager=Manager(harness),
        server_client=server,
        spec_resolver=AsyncMock(return_value=spec),
        root_startup_session_id=session_id if registered else uuid4().hex,
        root_startup_attempt=ref,
    )
    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="test",
        session_id=session_id,
        agent_id=AGENT,
        snapshot=RunnerSessionInitSnapshot(
            created_at=1, updated_at=1, workspace="/opt/work/project"
        ),
        resume_interrupted_turn=resume,
        recovery_id="old-recovery" if resume else None,
    )
    body = {
        "session_id": session_id,
        "agent_id": AGENT,
        "session_init": envelope.model_dump(),
        "startup_policy_enabled": True,
        **({"startup_required": True, "startup_attempt": ref.model_dump()} if registered else {}),
    }
    async with _runner_client(app, permission_harness=source) as client:
        result = await client.post("/v1/sessions", json=body)
        assert result.status_code == 201, result.text
        if registered:
            await asyncio.sleep(0)
            assert session_id not in app.state.active_turns
            assert not any(item.get("type") == "message" for item in harness.posted_bodies)
        else:
            assert result.json()["status"] == "running"
            await asyncio.wait_for(stream_finished.wait(), timeout=3)
            assert len(harness.posted_bodies) == 1
            assert harness.posted_bodies[0]["type"] == "message"


class _RecordingServer(NullServerClient):
    def __init__(self) -> None:
        self.claims: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []
        self.receipt_attempts = 0
        self.fail_receipts = False

    async def post(self, url: str, **kwargs: Any) -> Any:
        if url.endswith("/startup/claims"):
            self.claims.append(kwargs["json"])
            return _Accepted({"granted": True})
        if url.endswith("/startup/receipts"):
            self.receipt_attempts += 1
            if self.fail_receipts:
                raise httpx.ConnectError("server unavailable")
            self.receipts.append(kwargs["json"])
            return _Accepted({"accepted": True})
        return await super().post(url, **kwargs)


class _Accepted:
    status_code = 200

    def __init__(self, body: dict[str, bool]) -> None:
        self._body = body

    def json(self) -> dict[str, bool]:
        return self._body

    def raise_for_status(self) -> None:
        pass


@pytest.mark.asyncio
async def test_proof_cannot_change_the_owning_harness_or_mix_cached_sources():
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="context",
        host_generation="generation",
    )
    server = _RecordingServer()
    admission = create_runner_app(server_client=server).state.startup_admission
    admission.register(SESSION, ref)
    proof = StartupObservation(
        attempt=ref,
        phase="input_dispatched",
        source="claude-sdk",
        vendor_session_id="session",
        vendor_turn_id="query",
    )
    event = {"type": "startup.proof", "observation": proof.model_dump()}
    assert not await _relay_startup_proof(
        server, SESSION, event, ref, admission=admission, expected_source="codex"
    )
    assert server.receipts == []
    assert admission.observations_for(SESSION, ref) == []
    assert await _relay_startup_proof(
        server, SESSION, event, ref, admission=admission, expected_source="claude-sdk"
    )
    assert not admission.record_observation(
        SESSION, proof.model_copy(update={"phase": "input_accepted", "source": "codex"})
    )
    assert admission.observations_for(SESSION, ref) == [proof]


@pytest.mark.asyncio
async def test_stop_fences_late_observations_without_disabling_admission():
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="context",
        host_generation="generation",
    )
    interrupted, release = asyncio.Event(), asyncio.Event()
    harness = _ScriptedHarnessClient([])

    async def interrupt(_url, **kwargs):
        assert kwargs["json"] == {"type": "interrupt"}
        interrupted.set()
        await release.wait()
        return _Accepted({})

    harness.post = interrupt
    app = create_runner_app(
        server_client=_RecordingServer(),
        process_manager=_FakeProcessManager(harness),
    )
    admission = app.state.startup_admission
    admission.register(SESSION, ref)
    proof = StartupObservation(
        attempt=ref,
        phase="input_accepted",
        source="codex",
        vendor_session_id="thread",
        vendor_turn_id="turn",
    )
    assert admission.record_observation(SESSION, proof)
    async with _runner_client(app) as client:
        app.state.active_turns[SESSION] = None
        stopping = asyncio.create_task(
            client.post(f"/v1/sessions/{SESSION}/events", json={"type": "stop_session"})
        )
        await asyncio.wait_for(interrupted.wait(), 2.0)
        try:
            assert not admission.record_observation(SESSION, proof)
        finally:
            release.set()
            result = await stopping
        assert result.status_code == 204
        assert not admission.record_observation(SESSION, proof)
        assert admission.observations_for(SESSION, ref) is None
        assert admission.required(SESSION)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["codex", "claude-sdk"])
async def test_sdk_internal_claim_and_proof_reach_server_without_public_frames(source):
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="context",
        host_generation="generation",
    )
    observation = StartupObservation(
        attempt=ref,
        phase="input_accepted",
        source=source,
        vendor_session_id="thread",
        vendor_turn_id="turn",
    )
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "response"}}),
            _sse(
                {
                    "type": "startup.claim.requested",
                    "claim_id": "claim",
                    "attempt": ref.model_dump(),
                    "boundary": "executor_prepare",
                }
            ),
            _sse({"type": "startup.proof", "observation": observation.model_dump()}),
            _sse({"type": "response.completed", "response": {"id": "response"}}),
        ]
    )
    server = _RecordingServer()
    spec = AgentSpec(
        spec_version=1,
        name="agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": source}),
    )

    async def resolve(_agent_id: str, _session_id: str | None = None) -> AgentSpec:
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=resolve,
        server_client=server,  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT}
        )
        assert created.status_code == 201
        result = await client.post(
            f"/v1/sessions/{SESSION}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "agent",
                "content": [{"type": "input_text", "text": "work"}],
                "startup_attempt": ref.model_dump(),
            },
        )
        assert result.status_code == 202

        async def wait_for_receipt() -> None:
            while not server.receipts:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_receipt(), timeout=3.0)

    assert harness.posted_bodies[0]["startup_attempt"] == ref.model_dump()
    assert server.claims[0]["boundary"] == "executor_prepare"
    assert server.receipts == [observation.model_dump()]
    assert harness.patched_events == [
        {"type": "startup_claim_result", "claim_id": "claim", "granted": True}
    ]
    queue = app.state.session_event_queues[SESSION]
    assert all(
        event.get("type") not in {"startup.claim.requested", "startup.proof"}
        for event in list(queue._queue)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["codex", "claude-sdk"])
async def test_failed_receipt_can_be_requeried_only_for_current_attempt(source):
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="context",
        host_generation="generation",
    )
    observation = StartupObservation(
        attempt=ref,
        phase="input_dispatched",
        source=source,
        vendor_session_id="thread",
        vendor_turn_id="turn",
        reason="not cached",
    )
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "response"}}),
            _sse({"type": "startup.proof", "observation": observation.model_dump()}),
            _sse({"type": "response.completed", "response": {"id": "response"}}),
        ]
    )
    server = _RecordingServer()
    server.fail_receipts = True
    spec = AgentSpec(
        spec_version=1,
        name="agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": source}),
    )

    async def resolve(_agent_id: str, _session_id: str | None = None) -> AgentSpec:
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=resolve,
        server_client=server,  # type: ignore[arg-type]
        auth_token="private-test-token",
    )
    async with _runner_client(app) as client:
        headers = {"Authorization": "Bearer private-test-token"}
        created = await client.post(
            "/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT}, headers=headers
        )
        assert created.status_code == 201
        result = await client.post(
            f"/v1/sessions/{SESSION}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "agent",
                "content": [{"type": "input_text", "text": "work"}],
                "startup_attempt": ref.model_dump(),
            },
            headers=headers,
        )
        assert result.status_code == 202

        async def wait_for_failed_relay() -> None:
            while server.receipt_attempts < 3:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_failed_relay(), timeout=3.0)
        route = f"/v1/sessions/{SESSION}/startup/observations"
        assert (await client.post(route, json=ref.model_dump())).status_code == 401
        cached = await client.post(route, json=ref.model_dump(), headers=headers)
        assert cached.status_code == 200
        assert cached.json() == {
            "observations": [observation.model_copy(update={"reason": None}).model_dump()]
        }
        # A reconnect init has no dispatch ContextVar. Its verified saved ref
        # must preserve already observed proof instead of resetting the cache.
        client.headers.update(headers)
        initializer = RunnerSessionInitializer(
            TunnelRegistry(),
            server_version="0.6.0.dev0",
            startup_control_resolver=lambda _conversation: (True, None),
            startup_recovery_resolver=AsyncMock(return_value=(True, ref)),
        )
        initialized = await initializer.initialize(
            Conversation(
                id=SESSION,
                agent_id=AGENT,
                runner_id="runner",
                created_at=1,
                updated_at=1,
                root_conversation_id=SESSION,
                workspace="/Users/sample/project",
            ),
            client,
            timeout=2,
        )
        assert initialized.status_code == 201
        assert (
            await client.post(route, json=ref.model_dump(), headers=headers)
        ).json() == cached.json()
        next_ref = ref.model_copy(update={"attempt_id": "d" * 32})
        app.state.startup_admission.register(SESSION, next_ref)
        assert (
            await client.post(route, json=ref.model_dump(), headers=headers)
        ).status_code == 404
        assert (await client.post(route, json=next_ref.model_dump(), headers=headers)).json() == {
            "observations": []
        }
