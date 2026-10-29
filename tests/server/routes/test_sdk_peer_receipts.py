"""Authenticated, attempt-specific SDK peer receipts never replay inputs."""

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException

from omnigent.entities import SessionPeerMessage
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.sdk_peer_delivery import sdk_attempt_reason
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.helpers import _build_new_item
from omnigent.server.schemas import SessionEventInput
from tests.server.routes.test_session_event_batches import (
    _SESSION_ID,
    _make_client,
    _make_conv,
    _RecordingStore,
)


@pytest.mark.parametrize(
    "outcome,state", [("accepted", "delivered"), ("refused", "queued"), ("unknown", "delivering")]
)
def test_sdk_receipt_requires_bound_runner_and_current_attempt(outcome, state):
    token = "test-sdk-runner"
    conv = _make_conv()
    conv.runner_id = token_bound_runner_id(token)
    store = _RecordingStore(conv)
    record = SessionPeerMessage(
        id="a" * 32,
        sender_session_id="sender",
        receiver_session_id=_SESSION_ID,
        ref="ref",
        text="peer input",
        state="delivering",
        reason=sdk_attempt_reason("b" * 32, "wait"),
        created_at=1,
        expires_at=100,
    )
    peers = MagicMock()
    peers.get.return_value = record
    client = _make_client(store, peer_message_store=peers)
    body = {
        "type": "sdk_peer_delivery_receipt",
        "data": {
            "peer_id": record.id,
            "attempt_id": "b" * 32,
            "outcome": outcome,
            "proof_source": "sdk_protocol",
        },
    }
    url = f"/sessions/{_SESSION_ID}/events"
    assert client.post(url, json=body).status_code == 403
    assert (
        client.post(url, json=body, headers={RUNNER_TUNNEL_TOKEN_HEADER: "old-runner"}).status_code
        == 403
    )
    headers = {RUNNER_TUNNEL_TOKEN_HEADER: token}
    body["data"]["attempt_id"] = "c" * 32
    assert client.post(url, json=body, headers=headers).status_code == 202
    peers.transition.assert_not_called()
    body["data"]["attempt_id"] = "b" * 32
    assert client.post(url, json=body, headers=headers).json() == {"queued": False}
    peers.transition.assert_called_once_with(
        record.id,
        state,
        sdk_attempt_reason("b" * 32, outcome),
        ("delivering",),
        expected_reason=record.reason,
    )
    assert len(store.append_calls) == int(outcome == "accepted")
    if outcome == "accepted":
        assert store.append_calls[0][0].stable_id == "b" * 32
        assert store.append_calls[0][0].data.peer_message_id == record.id
    # A duplicate or late acceptance cannot overwrite a definitive refusal.
    peers.transition.reset_mock()
    record.state = "queued"
    assert client.post(url, json=body, headers=headers).status_code == 202
    peers.transition.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "message", "data": {"role": "user", "content": [], "startup_attempt": {}}},
        {"type": "message", "data": {"role": "user", "content": [], "startup_required": False}},
        {"type": "message", "data": {"role": "user", "content": [], "peer_delivery": {}}},
        {"type": "message", "data": {"role": "user", "content": [], "peer_message_id": "a" * 32}},
        {
            "type": "external_session_status",
            "data": {
                "status": "completed",
                "sdk_peer_delivery": {"peer_id": "a" * 32, "attempt_id": "b" * 32},
            },
        },
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "model": "test",
                    "content": [],
                    "peer_message_id": "a" * 32,
                },
            },
        },
    ],
)
def test_public_caller_cannot_supply_peer_control_metadata(payload):
    store = _RecordingStore(_make_conv())
    client = _make_client(store)
    for body in (payload, [payload]):
        assert client.post(f"/sessions/{_SESSION_ID}/events", json=body).status_code == 403
    assert store.append_calls == []


@pytest.mark.parametrize("result", ["accepted_transport", "server_error", "lost_response"])
async def test_forwarded_sdk_peer_is_not_recoverable_conversation_input(result, monkeypatch):
    conv = _make_conv()
    conv.harness_override = "codex"
    store = _RecordingStore(conv)
    forwarded = []

    async def archive_states(*args):
        return []

    monkeypatch.setattr(orchestration, "runner_archive_states_for_conversation", archive_states)

    def handle(request):
        import json

        forwarded.append(json.loads(request.content))
        if result == "lost_response":
            raise httpx.ReadError("response lost", request=request)
        return httpx.Response(202 if result == "accepted_transport" else 500)

    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "peer envelope"}],
            "peer_message_id": "a" * 32,
            "stable_id": "b" * 32,
            "peer_delivery": {"peer_id": "a" * 32, "attempt_id": "b" * 32},
        },
    )
    async with httpx.AsyncClient(
        base_url="http://runner", transport=httpx.MockTransport(handle)
    ) as runner:
        if result == "accepted_transport":
            await orchestration._forward_event_to_runner(_SESSION_ID, conv, body, store, runner)
        else:
            with pytest.raises(HTTPException, match="no receipt"):
                await orchestration._forward_event_to_runner(
                    _SESSION_ID, conv, body, store, runner
                )
    assert store.append_calls == []
    assert forwarded[0]["peer_delivery"] == body.data["peer_delivery"]
    assert "persisted_item_id" not in forwarded[0]


def test_legacy_peer_input_keeps_normal_restart_recovery():
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "legacy peer"}],
            "peer_message_id": "a" * 32,
        },
    )
    item = _build_new_item(body, "turn", adopt_stable_id=True)
    # Only a receipt-confirmed SDK input may suppress automatic recovery.
    assert item.data.peer_message_id is None
