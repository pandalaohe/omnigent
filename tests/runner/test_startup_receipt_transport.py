"""The runner consumes trusted SDK startup frames before publishing turn output."""

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
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
async def test_codex_internal_claim_and_proof_reach_server_without_public_frames():
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
        source="codex",
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
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex"}),
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
async def test_failed_receipt_can_be_requeried_only_for_current_attempt():
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
        source="codex",
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
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex"}),
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
        next_ref = ref.model_copy(update={"attempt_id": "d" * 32})
        app.state.startup_admission.register(SESSION, next_ref)
        assert (
            await client.post(route, json=ref.model_dump(), headers=headers)
        ).status_code == 404
        assert (await client.post(route, json=next_ref.model_dump(), headers=headers)).json() == {
            "observations": []
        }
