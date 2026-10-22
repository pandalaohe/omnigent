"""Startup proof is accepted only from the bound runtime, with exact vendor identity."""

from unittest.mock import Mock
from uuid import uuid4

import pytest

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostHelloFrame
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.server_config import StartupAdmissionPolicy
from omnigent.server.startup_admission import StartupAdmission
from omnigent.server.startup_inputs import ManagedStartupInputs
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore
from tests.server.test_host_registry import FakeWebSocket


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["runner", "host"])
@pytest.mark.parametrize("source", ["codex", "claude-sdk", "codex-native"])
async def test_bound_runner_exact_receipts_join_both_orders_and_reject_owner_forgery(
    app, client, db_uri, monkeypatch, race, source
):
    conversations, registry = SqlAlchemyConversationStore(db_uri), app.state.host_registry
    store, host = SessionStartupStore(db_uri), uuid4().hex
    app.state.startup_store = store
    conn = registry.register(
        host,
        FakeWebSocket(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="sample-host",
            connection_id="generation",
            capabilities=["startup_admission_v1", f"startup_receipts:{source}"],
        ),
        owner=None,
    )
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, host, "/opt/work/project")
    conversations.set_runner_id(conv.id, token_bound_runner_id("sample-binding"))
    [item] = conversations.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id=PENDING_INPUT_RESPONSE_ID,
                status="in_progress",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "work"}]),
            )
        ],
    )
    store.configure(host, host_resident_limit=8, deployment_resident_limit=8)
    store.register_host_generation(host, conn.startup_generation)
    startup = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=conv.id,
        operation_key="create",
        input_id=item.id,
    )
    attempt = store.reserve(host)
    assert store.claim_boundary(
        startup.id,
        attempt.attempt_id,
        runtime_id="context",
        host_generation=conn.startup_generation,
        boundary="runner_prepare",
        claim_id="claim",
    )
    url = f"/v1/sessions/{conv.id}/startup/receipts"
    payload = {
        "attempt": {
            "startup_id": startup.id,
            "attempt_id": attempt.attempt_id,
            "input_id": item.id,
            "runtime_id": "context",
            "host_generation": conn.startup_generation,
        },
        "phase": "work_started",
        "source": source,
        "vendor_session_id": "thread",
        "vendor_turn_id": "turn",
    }
    assert (await client.post(url, json=payload)).status_code == 403
    headers = {RUNNER_TUNNEL_TOKEN_HEADER: "sample-binding"}
    claim_url = f"/v1/sessions/{conv.id}/startup/claims"
    claim = {"attempt": payload["attempt"], "boundary": "executor_prepare", "claim_id": "cold"}
    assert (await client.post(claim_url, json=claim)).status_code == 403
    stale = {**claim, "attempt": {**claim["attempt"], "host_generation": "old"}}
    assert (await client.post(claim_url, json=stale, headers=headers)).json() == {"granted": False}
    registry.launch_authorizer = Mock(
        side_effect=OmnigentError("Owner changed", code=ErrorCode.FORBIDDEN)
    )
    assert (await client.post(claim_url, json=claim, headers=headers)).json() == {"granted": False}
    registry.launch_authorizer = Mock()
    assert (await client.post(claim_url, json=claim, headers=headers)).json() == {"granted": True}
    assert (await client.post(claim_url, json=claim, headers=headers)).json() == {"granted": False}
    assert (await client.post(url, json=payload, headers=headers)).json() == {"accepted": True}
    assert len(conversations.list_pending_inputs(conv.id)) == 1
    wrong = {**payload, "phase": "input_accepted", "vendor_turn_id": "different"}
    assert (await client.post(url, json=wrong, headers=headers)).json() == {"accepted": False}
    observe = store.observe

    def rebind_before_receipt(*args, **kwargs):
        if race == "runner":
            conversations.replace_runner_id(conv.id, token_bound_runner_id("replacement-binding"))
        else:
            replacement = registry.register(host, FakeWebSocket(), conn.hello, owner=None)
            store.register_host_generation(host, replacement.startup_generation)
        return observe(*args, **kwargs)

    monkeypatch.setattr(store, "observe", rebind_before_receipt)
    readiness = {"attempt": payload["attempt"], "phase": "runtime_ready", "source": "runner"}
    assert (await client.post(url, json=readiness, headers=headers)).json() == {"accepted": False}
    monkeypatch.setattr(store, "observe", observe)
    if race == "host":
        assert store.get(startup.id).state == "verifying"
        return
    conversations.replace_runner_id(conv.id, token_bound_runner_id("sample-binding"))
    for phase in ("input_accepted", "runtime_ready", "tools_ready"):
        message = {**payload, "phase": phase}
        if phase.endswith("ready"):
            message = {"attempt": payload["attempt"], "phase": phase, "source": "runner"}
        if phase == "input_accepted" and source in ("codex", "claude-sdk"):
            append = SqlAlchemyConversationStore.append

            def fail_handoff(*args, **kwargs):
                raise RuntimeError("Receipt persisted; canonical handoff interrupted")

            monkeypatch.setattr(SqlAlchemyConversationStore, "append", fail_handoff)
            with pytest.raises(RuntimeError, match="handoff interrupted"):
                await client.post(url, json=message, headers=headers)
            monkeypatch.setattr(SqlAlchemyConversationStore, "append", append)
            assert conversations.list_pending_inputs(conv.id)
            inputs = ManagedStartupInputs(conversations, registry)
            app.state.startup_inputs = inputs
            admission = StartupAdmission(
                store, conversations, registry, StartupAdmissionPolicy(8, 8, 2)
            )
            admission.bind(app)
            await inputs._recover_session(app, conv.id, conn)
            assert conversations.list_pending_inputs(conv.id) == []
            await admission.shutdown()
            await inputs.shutdown()
        else:
            assert (await client.post(url, json=message, headers=headers)).json() == {
                "accepted": True
            }
    assert store.get(startup.id).state == "working"
    if source in ("codex", "claude-sdk"):
        assert conversations.list_pending_inputs(conv.id) == []
        consumed = conversations.get_item(conv.id, item.id)
        assert consumed.response_id != PENDING_INPUT_RESPONSE_ID
        assert (
            await client.post(url, json={**payload, "phase": "input_accepted"}, headers=headers)
        ).json() == {"accepted": True}
        assert conversations.get_item(conv.id, item.id).response_id == consumed.response_id
