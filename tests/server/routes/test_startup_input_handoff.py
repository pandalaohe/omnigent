"""Transport acceptance does not consume the canonical startup input."""

import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.helpers import _dispatch_skill_slash_command_to_runner
from omnigent.server.schemas import SessionEventInput
from omnigent.server.startup_inputs import StartupInputContext
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


@pytest.mark.asyncio
async def test_unclaimed_skill_reports_not_forwarded(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Session")
    context = StartupInputContext(app=None, headers=Headers(), user_id=None, input_id=uuid4().hex)
    requests = []

    def resolve(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"meta_text": "skill content"})

    body = SessionEventInput(
        type="slash_command", data={"kind": "skill", "name": "review", "arguments": ""}
    )
    async with httpx.AsyncClient(
        base_url="http://runner", transport=httpx.MockTransport(resolve)
    ) as runner:
        result = await _dispatch_skill_slash_command_to_runner(
            conv.id,
            conv,
            body,
            store,
            runner,
            agent=Mock(),
            has_mcp_servers=False,
            created_by=None,
            startup_input=context,
        )
    assert result.forwarded is False and result.item_id == context.input_id
    assert requests == [f"/v1/sessions/{conv.id}/skills/resolve"]
    assert store.list_items(conv.id).data == []
