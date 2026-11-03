"""A native transcript joins its accepted retry input without text matching."""

import json
import time
from uuid import uuid4

import pytest

from omnigent.harnesses.claude_native import bridge, forwarder
from omnigent.native.auto_retry import (
    AutoRetryGuard,
    admit_prompt,
    auto_retry_scope,
    observe_turn,
    prepare_submission,
)
from omnigent.runtime import pending_inputs
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.stores.test_subagent_retry_execution import _prepared
from tests.stores.test_subagent_retry_store import _journal


def prepared_native(db_uri, *, harness="claude-native"):
    journal = _journal(db_uri, SqlAlchemyConversationStore(db_uri))
    journal.conversations.update_conversation(
        journal.child,
        harness_override=harness,
    )
    journal.conversations.set_labels(
        journal.child, {"omnigent.wrapper": "claude-code-native-ui-subagent"}
    )
    chain, claim = _prepared(journal)
    assert journal.conversations.set_runner_id(journal.child, "child-runner")
    claim = claim.model_copy(update={"boundary": "native_reserve", "native_generation": 1})
    reserved = journal.store.admit_execution(journal.child, claim, runner_id="child-runner")
    assert reserved.state == "reserved"
    return journal, chain, claim


@pytest.mark.asyncio
async def test_codex_echo_waits_for_acceptance_and_replays_one_canonical_item(
    db_uri, tmp_path, client
):
    from omnigent.harnesses.codex_native import bridge as codex_bridge
    from omnigent.harnesses.codex_native import forwarder as codex_forwarder

    journal, _chain, claim = prepared_native(db_uri, harness="codex-native")
    codex_bridge.write_bridge_state(
        tmp_path,
        codex_bridge.CodexNativeBridgeState(
            session_id=journal.child,
            socket_path="/opt/work/app.sock",
            thread_id="thread",
            codex_home=str(tmp_path / "home"),
        ),
    )
    codex_bridge.record_retry_input(
        tmp_path,
        session_id=journal.child,
        thread_id="thread",
        input_id=claim.input_id,
    )
    pending_inputs.record(
        journal.child,
        [{"type": "input_text", "text": "retry"}],
        stable_id=claim.input_id,
        auto_retry=claim.retry.metadata,
    )
    statuses = []

    async def acknowledge_after_pending(response):
        if response.request.url.path.endswith("/events"):
            statuses.append(response.status_code)
            if response.status_code == 503:
                assert journal.conversations.list_items(journal.child).data == []
                codex_bridge.record_retry_input(
                    tmp_path,
                    session_id=journal.child,
                    thread_id="thread",
                    input_id=claim.input_id,
                    turn_id="turn",
                )
                accept_native(journal, claim, "codex_turn")

    client.event_hooks["response"].append(acknowledge_after_pending)
    params = {
        "threadId": "thread",
        "turnId": "turn",
        "item": {
            "id": "vendor-automatic-item",
            "clientId": claim.input_id,
            "type": "userMessage",
            "content": [{"type": "text", "text": "retry"}],
        },
    }
    try:
        for _ in range(2):
            await codex_forwarder._handle_completed_item(
                client,
                journal.child,
                params,
                forwarder_state=codex_forwarder._CodexForwarderState(
                    parent_session_id=journal.child
                ),
                bridge_dir=tmp_path,
            )
    finally:
        client.event_hooks["response"].remove(acknowledge_after_pending)
    assert statuses[0] == 503 and all(code < 400 for code in statuses[1:])
    [saved] = journal.conversations.list_items(journal.child).data
    assert saved.data.auto_retry == claim.retry.metadata
    assert pending_inputs.pending_id_for_stable_id(journal.child, claim.input_id) is None
    response = await client.get(f"/v1/sessions/{journal.child}/items")
    [reloaded] = response.json()["data"]
    assert reloaded["id"] == saved.id
    assert reloaded["auto_retry"] == claim.retry.metadata.model_dump()


def accept_native(journal, claim, turn_id):
    result = journal.store.admit_execution(
        journal.child,
        claim.model_copy(update={"boundary": "native_accept", "native_turn_id": turn_id}),
        runner_id="child-runner",
    )
    assert result.accepted


def native_item(tmp_path, monkeypatch, journal, chain, claim):
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    directory = bridge.prepare_bridge_dir(journal.child, workspace=tmp_path)
    vendor_session, prompt_id, user_uuid = str(uuid4()), str(uuid4()), str(uuid4())
    bridge.record_hook_event(
        directory, {"hook_event_name": "SessionStart", "session_id": vendor_session}
    )
    snapshot = observe_turn(directory, journal.child, "root-turn", idle=True)
    guard = AutoRetryGuard(
        session_id=journal.child,
        chain_id=chain.chain_id,
        input_id=claim.input_id,
        expected_turn_id="root-turn",
        expected_input_generation=1,
        expected_native_generation=snapshot.generation,
        admission_deadline=time.time() + 60,
        attempt=1,
        of=chain.limit,
    )
    with auto_retry_scope(guard):
        prepare_submission(directory, native_idle=True)
    admission = admit_prompt(directory, journal.child, "retry", turn_id="claude_turn_retry")
    assert admission.auto_retry is not None
    bridge.record_hook_event(
        directory,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": vendor_session,
            "prompt_id": prompt_id,
            "prompt": "retry",
        },
        input_admission=admission,
    )
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": user_uuid,
                "sessionId": vendor_session,
                "promptId": prompt_id,
                "message": {"role": "user", "content": "retry"},
            }
        )
        + "\n"
    )
    [item] = bridge.read_transcript_items_from_offset(
        path, 0, start_line=0, agent_name="claude"
    ).items
    correlated = bridge.correlate_retry_input(directory, journal.child, item)
    assert correlated.retry_input_id == claim.input_id
    assert correlated.source_id == f"{user_uuid}:0:message"
    assert bridge.correlate_retry_input(directory, "other-child", item).retry_input_id is None
    return directory, item, correlated


@pytest.mark.asyncio
@pytest.mark.parametrize("append_failure", [False, True])
async def test_native_echo_has_one_verified_message_across_replay_and_reload(
    db_uri,
    tmp_path,
    monkeypatch,
    client,
    append_failure,
):
    journal, chain, claim = prepared_native(db_uri)
    _directory, item, correlated = native_item(tmp_path, monkeypatch, journal, chain, claim)
    accept_native(journal, claim, correlated.retry_turn_id)
    human_id = uuid4().hex
    pending_inputs.record(
        journal.child, [{"type": "input_text", "text": "retry"}], stable_id=human_id
    )
    automatic = pending_inputs.record(
        journal.child,
        [{"type": "input_text", "text": "retry"}],
        stable_id=claim.input_id,
        auto_retry=claim.retry.metadata,
    )
    if append_failure:
        original = SqlAlchemyConversationStore.append
        with monkeypatch.context() as failure:

            def unavailable(self, conversation_id, items, **kwargs):
                if conversation_id == journal.child:
                    raise RuntimeError("append unavailable")
                return original(self, conversation_id, items, **kwargs)

            failure.setattr(SqlAlchemyConversationStore, "append", unavailable)
            with pytest.raises(RuntimeError, match="append unavailable"):
                await forwarder._post_external_conversation_item(
                    client, session_id=journal.child, item=correlated
                )
        assert pending_inputs.pending_id_for_stable_id(journal.child, claim.input_id) == automatic
    assert await forwarder._post_external_conversation_item(
        client, session_id=journal.child, item=correlated
    )
    [saved] = journal.conversations.list_items(journal.child).data
    assert saved.data.auto_retry == claim.retry.metadata
    altered = {
        "type": "external_conversation_item",
        "data": {
            "source_id": "another-source:0:message",
            "item_type": "message",
            "item_data": item.data,
            "response_id": item.response_id,
            "input_id": correlated.retry_input_id,
            "input_turn_id": correlated.retry_turn_id,
        },
    }
    refused = await client.post(f"/v1/sessions/{journal.child}/events", json=altered)
    assert refused.status_code == 409, refused.text
    assert [row["pending_id"] for row in pending_inputs.snapshot_for(journal.child)] == [
        pending_inputs.make_pending_id(journal.child, human_id)
    ]
    # A cold transcript replay carries its vendor source, without arbitrary metadata.
    assert not await forwarder._post_external_conversation_item(
        client, session_id=journal.child, item=item
    )
    historical = {
        "type": "external_conversation_item",
        "data": {
            "source_id": item.source_id,
            "item_type": "message",
            "item_data": item.data,
            "response_id": item.response_id,
            "recovery_after": None,
        },
    }
    response = await client.post(f"/v1/sessions/{journal.child}/events", json=historical)
    assert response.status_code == 202, response.text
    reloaded = await client.get(f"/v1/sessions/{journal.child}/items")
    [message] = reloaded.json()["data"]
    assert message["id"] == saved.id and message["auto_retry"] == claim.retry.metadata.model_dump()
    pending_inputs.resolve(journal.child, pending_inputs.make_pending_id(journal.child, human_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["pending", "foreign", "turn", "marker"])
async def test_native_echo_requires_its_accepted_session_and_turn_receipt(db_uri, client, fault):
    journal, _chain, claim = prepared_native(db_uri)
    if fault != "pending":
        accept_native(journal, claim, "claude_turn_retry")
    event = {
        "type": "external_conversation_item",
        "data": {
            "item_type": "message",
            "source_id": "vendor-source:0:message",
            "response_id": "native-turn",
            "input_id": uuid4().hex if fault == "foreign" else claim.input_id,
            "input_turn_id": "other-turn" if fault == "turn" else "claude_turn_retry",
            "item_data": {"role": "user", "content": [{"type": "input_text", "text": "retry"}]},
        },
    }
    if fault == "marker":
        event["data"]["item_data"]["auto_retry"] = {
            **claim.retry.metadata.model_dump(),
            "attempt": 2,
        }
    response = await client.post(f"/v1/sessions/{journal.child}/events", json=event)
    assert response.status_code == (503 if fault == "pending" else 400), response.text
    assert journal.conversations.list_items(journal.child).data == []
    assert journal.store.get_input(claim.input_id).payload.get("native_source_id") is None
