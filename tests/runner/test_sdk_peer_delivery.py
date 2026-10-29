"""Runner transport keeps SDK peer attempts separate from ordinary turn buffering."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.sdk_peer_delivery import SDK_PEER_RECEIPT_EVENT
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _BlockingHarnessClient,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

SESSION = "49ed0bd1f0cae058f05f48057e9f98cf"
AGENT = "880b5afda28ad55ff74cbeb9b5fc67fb"
PEER = "a" * 32
ATTEMPT = "b" * 32


class _RecordingServer(NullServerClient):
    def __init__(self) -> None:
        self.receipts: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        body = kwargs.get("json")
        if url.endswith("/events") and isinstance(body, dict):
            if body.get("type") == SDK_PEER_RECEIPT_EVENT:
                self.receipts.append(body["data"])
        return await super().post(url, **kwargs)


def _peer_body() -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "model": "agent",
        "content": [{"type": "input_text", "text": "peer envelope"}],
        "peer_delivery": {"peer_id": PEER, "attempt_id": ATTEMPT},
        "peer_message_id": PEER,
        "stable_id": ATTEMPT,
    }


def _app(
    harness: _ScriptedHarnessClient, server: _RecordingServer, harness_name: str = "codex"
) -> Any:
    spec = AgentSpec(
        spec_version=1,
        name="agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness_name}),
    )

    async def resolve(_agent_id: str, _session_id: str | None = None) -> AgentSpec:
        return spec

    return create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=resolve,
        server_client=server,  # type: ignore[arg-type]
    )


async def _wait_for(predicate: Any) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=3.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("harness_name", ["codex", "pi"])
async def test_midturn_peer_requires_stream_receipt_and_never_enters_continuation(
    harness_name: str,
) -> None:
    gate = asyncio.Event()
    harness = _BlockingHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse(
                {
                    "type": "sdk.peer.receipt",
                    "peer_id": PEER,
                    "attempt_id": ATTEMPT,
                    "outcome": "accepted",
                    "proof_source": "codex_turn_steer",
                }
            ),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ],
        gate,
    )
    server = _RecordingServer()
    app = _app(harness, server, harness_name)
    async with _runner_client(app, permission_harness=harness_name) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT}
        )
        assert created.status_code == 201
        started = await client.post(
            f"/v1/sessions/{SESSION}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "agent",
                "content": [{"type": "input_text", "text": "first"}],
            },
        )
        assert started.status_code == 202
        await _wait_for(lambda: app.state.live_response_id.get(SESSION) == "resp_1")
        peer = await client.post(f"/v1/sessions/{SESSION}/events", json=_peer_body())
        assert peer.status_code == 202
        assert peer.json()["status"] == "pending"
        assert not server.receipts  # 2xx injection is only a pending transport result.
        assert app.state.session_message_buffers.get(SESSION) is None
        assert len(harness.patched_events) == 1
        assert harness.patched_events[0]["previous_response_id"] == "resp_1"
        assert harness.patched_events[0]["peer_delivery"] == {
            "peer_id": PEER,
            "attempt_id": ATTEMPT,
        }
        gate.set()
        await _wait_for(lambda: bool(server.receipts))
        await _wait_for(lambda: SESSION not in app.state.active_turns)

    assert server.receipts == [
        {
            "peer_id": PEER,
            "attempt_id": ATTEMPT,
            "outcome": "accepted",
            "proof_source": "codex_turn_steer",
        }
    ]
    queue = app.state.session_event_queues[SESSION]
    assert all(event.get("type") != "sdk.peer.receipt" for event in list(queue._queue))


@pytest.mark.asyncio
@pytest.mark.parametrize("harness_name", ["codex", "pi"])
async def test_idle_peer_starts_real_sdk_turn_with_receipt_metadata(harness_name: str) -> None:
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_2"}}),
            _sse(
                {
                    "type": "sdk.peer.receipt",
                    "peer_id": PEER,
                    "attempt_id": ATTEMPT,
                    "outcome": "accepted",
                    "proof_source": "codex_turn_started",
                }
            ),
            _sse({"type": "response.completed", "response": {"id": "resp_2"}}),
        ]
    )
    server = _RecordingServer()
    app = _app(harness, server, harness_name)
    async with _runner_client(app, permission_harness=harness_name) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT}
        )
        assert created.status_code == 201
        peer = await client.post(f"/v1/sessions/{SESSION}/events", json=_peer_body())
        assert peer.status_code == 202
        await _wait_for(lambda: bool(server.receipts))
        await _wait_for(lambda: SESSION not in app.state.active_turns)

    assert len(harness.posted_bodies) == 1
    assert harness.posted_bodies[0]["peer_delivery"] == {
        "peer_id": PEER,
        "attempt_id": ATTEMPT,
    }
    assert harness.posted_bodies[0]["peer_delivery_start"] is True
    assert server.receipts[0]["outcome"] == "accepted"


@pytest.mark.asyncio
async def test_refused_idle_peer_is_removed_from_next_turn_history() -> None:
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_2"}}),
            _sse(
                {
                    "type": "sdk.peer.receipt",
                    "peer_id": PEER,
                    "attempt_id": ATTEMPT,
                    "outcome": "refused",
                    "proof_source": "sdk_initial_refused",
                }
            ),
            _sse({"type": "response.completed", "response": {"id": "resp_2"}}),
        ]
    )
    server = _RecordingServer()
    app = _app(harness, server)
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT}
        )
        assert created.status_code == 201
        assert (
            await client.post(f"/v1/sessions/{SESSION}/events", json=_peer_body())
        ).status_code == 202
        await _wait_for(lambda: bool(server.receipts))
        await _wait_for(lambda: SESSION not in app.state.active_turns)
        ordinary = await client.post(
            f"/v1/sessions/{SESSION}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "agent",
                "content": [{"type": "input_text", "text": "ordinary follow-up"}],
            },
        )
        assert ordinary.status_code == 202
        await _wait_for(lambda: len(harness.posted_bodies) == 2)

    second = harness.posted_bodies[1]["content"]
    assert all(item.get("peer_message_id") != PEER for item in second)
    assert "peer envelope" not in str(second)


@pytest.mark.asyncio
async def test_peer_with_active_slot_but_no_sdk_client_is_refused_without_buffering() -> None:
    harness = _ScriptedHarnessClient([])
    server = _RecordingServer()
    app = _app(harness, server)
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT}
        )
        assert created.status_code == 201
        app.state.active_turns[SESSION] = None
        peer = await client.post(f"/v1/sessions/{SESSION}/events", json=_peer_body())
        assert peer.status_code == 202
        await _wait_for(lambda: bool(server.receipts))

    assert server.receipts[0]["outcome"] == "refused"
    assert app.state.session_message_buffers.get(SESSION) is None
    assert not harness.patched_events


@pytest.mark.asyncio
@pytest.mark.parametrize("harness_name", ["codex", "pi"])
async def test_persisted_peer_envelope_does_not_start_sdk_recovery_turn(harness_name: str) -> None:
    """A durable peer transcript item is context, not executable replay authority."""

    class _HistoryServer(_RecordingServer):
        async def get(self, url: str, **kwargs: Any) -> Any:
            if url.endswith("/items"):

                class _ItemsResponse:
                    status_code = 200

                    def json(self) -> dict[str, Any]:
                        return {
                            "data": [
                                {
                                    "id": "item_peer",
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "peer envelope"}],
                                    "peer_message_id": PEER,
                                    "stable_id": ATTEMPT,
                                }
                            ],
                            "has_more": False,
                        }

                return _ItemsResponse()
            return await super().get(url, **kwargs)

    harness = _ScriptedHarnessClient([])
    app = _app(harness, _HistoryServer(), harness_name)
    async with _runner_client(app, permission_harness=harness_name) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT}
        )

    assert created.status_code == 201
    assert SESSION not in app.state.active_turns
    assert not harness.posted_bodies


async def test_pi_peer_child_reports_its_attempt_for_server_attribution() -> None:
    class ChildServer(_RecordingServer):
        def __init__(self):
            super().__init__()
            self.terminal_posts = []

        async def get(self, url, **kwargs):
            response = await super().get(url, **kwargs)
            if url == f"/v1/sessions/{SESSION}":
                snapshot = {
                    **response.json(),
                    "id": SESSION,
                    "agent_id": AGENT,
                    "parent_session_id": "parent",
                    "kind": "sub_agent",
                    "harness_override": "pi",
                }

                class Snapshot(self._Response):
                    def json(self):
                        return snapshot

                return Snapshot()
            return response

        async def post(self, url, **kwargs):
            body = kwargs.get("json", {})
            if body.get("type") == "external_session_status":
                self.terminal_posts.append(body["data"])
            return await super().post(url, **kwargs)

    server = ChildServer()
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "peer_response"}}),
            _sse(
                {
                    "type": "sdk.peer.receipt",
                    "peer_id": PEER,
                    "attempt_id": ATTEMPT,
                    "outcome": "accepted",
                    "proof_source": "sdk_turn_response",
                }
            ),
            _sse({"type": "response.output_text.delta", "delta": "peer answer"}),
            _sse({"type": "response.completed", "response": {"id": "peer_response"}}),
        ]
    )
    app = _app(harness, server, "pi")
    async with _runner_client(
        app, permission_harness="pi", permission_snapshot={"parent_session_id": "parent"}
    ) as client:
        assert (
            await client.post("/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT})
        ).status_code == 201
        assert (
            await client.post(f"/v1/sessions/{SESSION}/events", json=_peer_body())
        ).status_code == 202
        await _wait_for(lambda: bool(server.terminal_posts))
    assert server.receipts[0]["outcome"] == "accepted"
    assert server.terminal_posts[-1]["sdk_peer_delivery"] == {
        "peer_id": PEER,
        "attempt_id": ATTEMPT,
    }
    assert server.terminal_posts[-1]["response_id"] == "peer_response"
