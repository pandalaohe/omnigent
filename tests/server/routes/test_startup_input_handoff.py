"""Transport acceptance does not consume the canonical startup input."""

import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.server.routes._sessions import orchestration
from omnigent.server.schemas import SessionEventInput
from omnigent.startup_protocol import StartupAttemptRef
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_reply", [False, True])
async def test_forward_keeps_one_pending_owner_until_vendor_accepts(
    db_uri, monkeypatch, lost_reply
):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Session")
    store.update_conversation(conv.id, harness_override="codex")
    conv = store.get_conversation(conv.id)
    [saved] = store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id=PENDING_INPUT_RESPONSE_ID,
                status="incomplete",
                stable_id=uuid4().hex,
                data=MessageData(role="user", content=[{"type": "input_text", "text": "work"}]),
            )
        ],
    )
    attempt = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id=saved.id,
        runtime_id="runtime",
        host_generation="generation",
    )
    forwarded = []
    consumed = Mock()
    monkeypatch.setattr(orchestration, "_publish_input_consumed", consumed)
    monkeypatch.setattr(
        orchestration, "runner_archive_states_for_conversation", AsyncMock(return_value=[])
    )

    def receive(request):
        forwarded.append(json.loads(request.content))
        if lost_reply:
            raise httpx.ReadError("reply lost", request=request)
        return httpx.Response(202)

    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "work"}],
            "stable_id": saved.id,
            "startup_attempt": {"forged": True},
        },
    )
    async with httpx.AsyncClient(
        base_url="http://runner", transport=httpx.MockTransport(receive)
    ) as runner:
        if lost_reply:
            with pytest.raises(HTTPException, match="no receipt"):
                await orchestration._forward_event_to_runner(
                    conv.id,
                    conv,
                    body,
                    store,
                    runner,
                    startup_input_id=saved.id,
                    startup_attempt=attempt,
                )
        else:
            assert (
                await orchestration._forward_event_to_runner(
                    conv.id,
                    conv,
                    body,
                    store,
                    runner,
                    startup_input_id=saved.id,
                    startup_attempt=attempt,
                )
                == saved.id
            )
    [pending] = store.list_pending_inputs(conv.id)
    assert pending.id == saved.id and pending.status == "incomplete"
    assert forwarded[0]["startup_attempt"] == attempt.model_dump(mode="json")
    assert forwarded[0]["startup_required"] is True
    consumed.assert_not_called()
