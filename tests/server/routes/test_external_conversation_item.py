"""Tests for externally-forwarded conversation-item parsing."""

import pytest

from omnigent.entities import ConversationItem, MessageData
from omnigent.server.routes.sessions import _parse_external_conversation_item
from omnigent.server.schemas import SessionEventInput


@pytest.mark.parametrize(
    "marker", [{"chain_id": "a" * 32, "input_id": "b" * 32, "attempt": 1, "of": 3}, "forged", True]
)
def test_external_parser_does_not_admit_caller_retry_markers(marker) -> None:
    body = SessionEventInput(
        type="external_conversation_item",
        data={
            "item_type": "message",
            "item_data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "retry"}],
                "auto_retry": marker,
            },
            "source_id": "native:user:0",
        },
    )
    parsed = _parse_external_conversation_item(body)
    assert parsed.data.auto_retry is None
    assert "auto_retry" not in parsed.data.model_dump()
    assert body.data["item_data"]["auto_retry"] == marker


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("role", ["user", "assistant"])
async def test_external_marker_is_untrusted_through_http_and_reload(client, batch, role):
    from tests.server.helpers import create_test_agent

    agent = await create_test_agent(client)
    session_id = agent["_session_id"]
    event = {
        "type": "external_conversation_item",
        "data": {
            "item_type": "message",
            "source_id": "native:spoof:0",
            "item_data": {
                "role": role,
                **({"agent": "test-agent"} if role == "assistant" else {}),
                "content": [
                    {"type": "input_text" if role == "user" else "output_text", "text": "retry"}
                ],
                "auto_retry": {"chain_id": "a" * 32, "input_id": "b" * 32, "attempt": 1, "of": 3},
            },
        },
    }
    response = await client.post(
        f"/v1/sessions/{session_id}/events", json=[event] if batch else event
    )
    assert response.status_code == 202, response.text
    reloaded = await client.get(f"/v1/sessions/{session_id}/items")
    assert reloaded.status_code == 200
    [message] = [item for item in reloaded.json()["data"] if item["type"] == "message"]
    assert message["role"] == role
    assert "auto_retry" not in message


def test_native_message_stream_id_survives_snapshot_serialization() -> None:
    """The completed item durably identifies the preview stream it replaces."""
    parsed = _parse_external_conversation_item(
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex",
                    "content": [{"type": "output_text", "text": "done"}],
                },
                "response_id": "turn_1",
                "message_id": "codex:thread_1:turn_1:agentMessage:item_1",
            },
        )
    )

    assert isinstance(parsed.data, MessageData)
    assert parsed.data.stream_message_id == "codex:thread_1:turn_1:agentMessage:item_1"
    persisted = ConversationItem(
        id="item_1",
        type=parsed.type,
        status="completed",
        response_id=parsed.response_id,
        created_at=1,
        data=parsed.data,
    )
    assert persisted.to_api_dict()["stream_message_id"] == (
        "codex:thread_1:turn_1:agentMessage:item_1"
    )


def test_native_message_phase_survives_snapshot_serialization() -> None:
    """Codex's commentary/answer label reaches the web UI on reload and live broadcast."""
    parsed = _parse_external_conversation_item(
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "content": [{"type": "output_text", "text": "Checking the tests."}],
                    "phase": "commentary",
                },
                "response_id": "turn_1",
            },
        )
    )

    persisted = ConversationItem(
        id="item_1",
        type=parsed.type,
        status="completed",
        response_id=parsed.response_id,
        created_at=1,
        data=parsed.data,
    )
    assert persisted.to_api_dict()["phase"] == "commentary"
