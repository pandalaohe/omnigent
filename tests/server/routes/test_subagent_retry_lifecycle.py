"""Automatic retry dispatch obeys the ordinary input policy boundary."""

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from omnigent.entities.subagent_terminal import TerminalEnvelope
from omnigent.policies.types import PolicyAction, PolicyResult
from omnigent.runner import subagent_work
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runner.subagent_retry import SubagentRetryRuntime
from tests.server.helpers import create_test_agent


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
        assert not runtime.failures
        final = service.journal.get_final(ack.chain_id)
        assert final is not None and final.outcome.reason == "admission_denied"
        assert final.attempts == 0 and final.outcome.error == terminal.error
        chain = service.journal.get_chain(ack.chain_id)
        assert chain.phase == "finalized" and chain.accepted_attempts == 0
        assert not entry.delivered and inbox.empty()
    finally:
        await runtime.close()
        subagent_work.unregister_subagent_work_for_session(child.id)
        subagent_work._session_inboxes_ref.pop(parent, None)
