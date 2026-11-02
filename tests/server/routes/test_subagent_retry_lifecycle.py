"""Automatic retry dispatch obeys the ordinary input policy boundary."""

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from omnigent.entities.subagent_terminal import TerminalEnvelope
from omnigent.policies.types import PolicyAction, PolicyResult
from omnigent.runner import subagent_work
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runner.subagent_retry import SubagentRetryRuntime
from tests.server.helpers import create_test_agent


@pytest.mark.asyncio
async def test_final_wake_refuses_legacy_receiver_before_dispatch(client, app, monkeypatch):
    from omnigent.entities.subagent_retry import RetryOutcome
    from omnigent.server.routes import sessions

    agent = await create_test_agent(client, guardrails={"policies": {}})
    service = app.state.subagent_retry_admission
    parent = agent["_session_id"]
    child = service.conversations.create_conversation(
        parent_conversation_id=parent, agent_id=agent["id"]
    )
    token = "test-final-legacy-receiver"
    service.conversations.clear_runner_id(parent)
    assert service.conversations.set_runner_id(parent, token_bound_runner_id(token))
    client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = token
    owner = service.journal.claim_owner(parent, token_bound_runner_id(token), expected_epoch=0)
    source = uuid4().hex
    control = service.journal.record_input(
        child.id,
        input_id=source,
        turn_id="failed-turn",
        expected_generation=0,
        payload={"text": "initial"},
    )
    terminal = TerminalEnvelope(
        child_id=child.id,
        parent_id=parent,
        input_id=source,
        turn_id=control.turn_id,
        input_generation=control.generation,
        status="failed",
        error={"message": "provider failed"},
    )
    chain = service.journal.open_chain(terminal, owner=owner, limit=3, observed_at=100).chain
    final = service.journal.finalize(
        chain.chain_id,
        owner=owner,
        expected_version=chain.version,
        outcome=RetryOutcome(status="failed", reason="runner_restarted", output="final error"),
        wake_payload={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "final wake"}]},
        },
    )
    assert service.journal.record_inbox(chain.chain_id, owner=owner, result_id=final.result_id)
    forwarded = []

    def receiver(request):
        if request.url.path.endswith("/events"):
            forwarded.append(request.url.path)
        return httpx.Response(202, json={"status": "accepted"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver), base_url="http://legacy-runner"
    ) as runner:
        monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=runner))
        response = await client.post(
            f"/v1/sessions/{parent}/retry-chains/{chain.chain_id}/wake",
            json={"owner": owner.model_dump()},
        )
    assert response.status_code == 200, response.text
    assert response.json()["reason"]["code"] == "runner_admission_unsupported"
    assert not forwarded and service.journal.get_final(chain.chain_id).wake_receipt is None
    assert service.journal.get_input(final.notice_id).state == "dispatching"


@pytest.mark.asyncio
async def test_real_retry_dispatch_denial_preserves_reason_without_attempt(
    client, app, monkeypatch
):
    agent = await create_test_agent(client, guardrails={"policies": {}})
    service = app.state.subagent_retry_admission
    conversations = service.conversations
    parent = agent["_session_id"]
    child = conversations.create_conversation(parent_conversation_id=parent, agent_id=agent["id"])
    token = "test-policy-retry-owner"
    conversations.clear_runner_id(parent)
    assert conversations.set_runner_id(parent, token_bound_runner_id(token))
    client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = token
    source = uuid4().hex
    original = service.journal.record_input(
        child.id,
        input_id=source,
        turn_id="root-turn",
        expected_generation=0,
        payload={"text": "initial input"},
    )
    terminal = TerminalEnvelope(
        child_id=child.id,
        parent_id=parent,
        turn_id="root-turn",
        input_id=source,
        input_generation=original.generation,
        status="failed",
        error={"message": "provider failed"},
    )
    inbox = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent] = inbox
    entry = subagent_work.register_subagent_work(
        parent_session_id=parent,
        child_session_id=child.id,
        agent="worker",
        title="policy denial",
        registered_by="sys_session_send",
    )
    clock = [100.0]

    def now():
        return clock[0]

    monkeypatch.setattr(service, "clock", now)
    runtime = SubagentRetryRuntime(client, clock=now)
    policy = AsyncMock(
        return_value=PolicyResult(action=PolicyAction.DENY, reason="User decision required")
    )
    try:
        ack = await runtime.accept_terminal(terminal, "provider failed")
        assert ack.reason == "held"
        with patch("omnigent.server.routes.sessions.build_policy_engine") as build:
            engine = build.return_value
            engine.evaluate = policy
            engine.apply_label_writes = lambda _writes: None
            clock[0] = 160
            await runtime._run(ack.chain_id)
        assert policy.await_count > 0
        assert "admission_denied" in runtime.failures[ack.chain_id]
        final = service.journal.get_final(ack.chain_id)
        assert final is not None and final.outcome.reason == "admission_denied"
        assert final.attempts == 0 and final.outcome.error == terminal.error
        chain = service.journal.get_chain(ack.chain_id)
        assert chain.phase == "finalized" and chain.accepted_attempts == 0
        assert entry.delivered and inbox.qsize() == 1
        assert service.journal.get_final(ack.chain_id).wake_receipt is None
    finally:
        await runtime.close()
        subagent_work.unregister_subagent_work_for_session(child.id)
        subagent_work._session_inboxes_ref.pop(parent, None)
