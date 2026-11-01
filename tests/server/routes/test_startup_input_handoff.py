"""Transport acceptance does not consume the canonical startup input."""

import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from omnigent.entities.conversation import AutoRetryMetadata, MessageData, NewConversationItem
from omnigent.runtime import pending_inputs
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.helpers import _dispatch_skill_slash_command_to_runner
from omnigent.server.schemas import SessionEventInput
from omnigent.server.startup_inputs import StartupInputContext, restore_pending_inputs
from omnigent.startup_protocol import StartupAttemptRef
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.asyncio
@pytest.mark.parametrize("with_attempt", [False, True])
async def test_native_forward_transports_only_its_owned_startup_reference(db_uri, with_attempt):
    from omnigent.runtime.harnesses._scaffold import MessageEvent

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    store.update_conversation(conv.id, harness_override="codex-native")
    conv = store.get_conversation(conv.id)
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="runtime",
        host_generation="generation",
        mode="startup_only",
    )
    context = StartupInputContext(
        app=None,
        headers=Headers(),
        user_id=None,
        input_id=ref.input_id,
        startup=ref if with_attempt else None,
    )
    received = []

    def receive(request):
        event = MessageEvent.model_validate(json.loads(request.content)).to_create_request()
        received.append(event)
        return httpx.Response(202)

    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "new input"}],
            "startup_attempt": {"forged": True},
            "startup_required": True,
        },
    )
    async with httpx.AsyncClient(
        base_url="http://runner.test", transport=httpx.MockTransport(receive)
    ) as runner:
        await orchestration._forward_native_terminal_message(
            runner, conv.id, conv, body, startup_context=context
        )
    assert received[0].startup_attempt == (ref if with_attempt else None)
    assert received[0].startup_required is with_attempt


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


@pytest.mark.asyncio
async def test_restored_pending_input_retains_retry_marker_after_failed_delivery(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    marker = AutoRetryMetadata(chain_id="a" * 32, input_id="b" * 32, attempt=2, of=5)
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id=PENDING_INPUT_RESPONSE_ID,
                status="incomplete",
                stable_id=marker.input_id,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "retry"}],
                    auto_retry=marker,
                ),
            )
        ],
    )
    try:
        for _ in range(2):
            await restore_pending_inputs(store, conv.id)
            [snapshot] = pending_inputs.snapshot_for(conv.id)
            assert snapshot["auto_retry"] == marker.model_dump(mode="json")
            assert snapshot["content"] == [{"type": "input_text", "text": "retry"}]
            drained = pending_inputs.resolve(conv.id, snapshot["pending_id"])
            assert drained is not None and drained.auto_retry == marker
        assert store.list_pending_inputs(conv.id)[0].id == marker.input_id
        await orchestration._settle_undelivered_native_input(
            store, conv.id, "failed-turn", marker.input_id
        )
        [saved] = store.list_items(conv.id).data
        assert saved.id == marker.input_id and saved.status == "completed"
        assert saved.data.auto_retry == marker
        assert store.list_pending_inputs(conv.id) == []
    finally:
        for pending_id in pending_inputs.pending_ids(conv.id):
            pending_inputs.resolve(conv.id, pending_id)
