"""Retry correlation survives typed message and harness request serialization."""

import pytest
from pydantic import ValidationError

from omnigent.entities.conversation import AutoRetryMetadata, ConversationItem, MessageData
from omnigent.runtime.harnesses._scaffold import MessageEvent
from omnigent.server.schemas import CreateResponseRequest

_MARKER = {"chain_id": "a" * 32, "input_id": "b" * 32, "attempt": 5, "of": 7}
_GUARD = {
    **_MARKER,
    "owner": {"parent_id": "parent", "runner_id": "runner", "epoch": 1},
    "expected_turn_id": "failed-turn",
    "expected_input_generation": 4,
}


def test_message_marker_round_trip_preserves_literal_input_and_api_data() -> None:
    message = MessageData.model_validate(
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "retry"}],
            "auto_retry": _MARKER,
        }
    )
    restored = MessageData.model_validate_json(message.model_dump_json())
    item = ConversationItem(
        id="b" * 32,
        type="message",
        status="completed",
        response_id="retry-turn",
        created_at=0,
        data=restored,
    )
    assert restored.content == [{"type": "input_text", "text": "retry"}]
    assert restored.auto_retry == AutoRetryMetadata.model_validate(_MARKER)
    assert item.to_api_dict()["auto_retry"] == _MARKER
    assert not restored.is_meta


def test_message_event_retains_typed_marker_in_harness_request() -> None:
    event = MessageEvent.model_validate(
        {
            "type": "message",
            "role": "user",
            "model": "agent",
            "content": "retry",
            "auto_retry": _MARKER,
            "input_id": "b" * 32,
            "retry_guard": _GUARD,
        }
    )
    request = event.to_create_request()
    restored = CreateResponseRequest.model_validate_json(request.model_dump_json())
    assert isinstance(event.auto_retry, AutoRetryMetadata)
    assert restored.auto_retry == event.auto_retry
    assert restored.input == "retry"
    assert restored.retry_guard is not None
    assert restored.retry_guard.metadata == event.auto_retry


@pytest.mark.parametrize(
    "changed",
    [
        {"retry_guard": None},
        {"input_id": "c" * 32},
        {"auto_retry": {**_MARKER, "chain_id": "c" * 32}},
        {"auto_retry": {**_MARKER, "attempt": 4}},
        {"auto_retry": {**_MARKER, "of": 8}},
    ],
)
def test_request_marker_requires_matching_guard(changed: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="matching execution guard"):
        CreateResponseRequest.model_validate(
            {
                "model": "agent",
                "input": "retry",
                "input_id": "b" * 32,
                "retry_guard": _GUARD,
                "auto_retry": _MARKER,
                **changed,
            }
        )


def test_plain_human_retry_has_no_automatic_marker() -> None:
    message = MessageData(role="user", content=[{"type": "input_text", "text": "retry"}])
    event = MessageEvent(type="message", role="user", model="agent", content="retry")
    assert "auto_retry" not in message.model_dump()
    assert "auto_retry" not in event.model_dump()
    assert "auto_retry" not in event.to_create_request().model_dump()


@pytest.mark.parametrize(
    "invalid",
    [
        {"chain_id": "invalid"},
        {"input_id": "B" * 32},
        {"attempt": 0},
        {"attempt": True},
        {"attempt": "5"},
        {"of": 0},
        {"of": False},
        {"of": "7"},
        {"attempt": 8},
        {"unexpected": "control"},
    ],
)
def test_invalid_marker_is_rejected(invalid: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AutoRetryMetadata.model_validate({**_MARKER, **invalid})
