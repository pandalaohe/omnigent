"""Peer injection uses SDK outcomes without ordinary continuation fallback."""

import asyncio
import contextlib
import json

import pytest

from omnigent.inner.executor import Executor, ExecutorError, TurnComplete
from omnigent.inner.pi_executor import _PiSessionState
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.sdk_peer_delivery import SDKPeerOutcome
from omnigent.server.schemas import CreateResponseRequest, SDKPeerReceiptEvent
from tests.inner.test_pi_executor import _executor_with_scripted_rpc
from tests.runtime.harnesses.test_executor_adapter import _OneInjectionCtx

_METADATA = {"peer_id": "a" * 32, "attempt_id": "b" * 32}


class PeerExecutor(Executor):
    def __init__(self, outcome="accepted", events=()):
        self.outcome = outcome
        self.events = events
        self.peer_calls = []
        self.ordinary_calls = []

    async def submit_peer_message(self, session_key, delivery, report):
        self.peer_calls.append(delivery)
        await report(SDKPeerOutcome.for_input(delivery, self.outcome, "sdk_protocol"))

    async def enqueue_session_message(self, session_key, content):
        self.ordinary_calls.append(content)
        return True

    async def run_turn(self, **kwargs):
        for event in self.events:
            yield event


@pytest.mark.parametrize("outcome", ["accepted", "refused", "unknown"])
@pytest.mark.parametrize("approval", [False, True])
async def test_peer_injection_uses_receipts_and_preserves_approval_gate(outcome, approval):
    executor = PeerExecutor(outcome)
    adapter = ExecutorAdapter(lambda: executor)
    ctx = _OneInjectionCtx(
        CreateResponseRequest(model="test", input="peer", peer_delivery=_METADATA)
    )
    ctx._pending_human_waits = int(approval)
    task = asyncio.create_task(adapter._watch_injections(ctx, executor))
    try:
        async with asyncio.timeout(1):
            while not ctx.emitted:
                await asyncio.sleep(0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert executor.ordinary_calls == []
    assert len(executor.peer_calls) == int(not approval)
    assert len(ctx.emitted) == 1
    assert isinstance(ctx.emitted[0], SDKPeerReceiptEvent)
    assert ctx.emitted[0].outcome == ("refused" if approval else outcome)


@pytest.mark.parametrize(
    "events,outcome",
    [
        ([TurnComplete(response="received")], "accepted"),
        (
            [ExecutorError(message="not started", undelivered=True, preserve_session=True)],
            "refused",
        ),
        ([], "unknown"),
    ],
)
async def test_idle_peer_needs_actual_executor_evidence(events, outcome):
    executor = PeerExecutor(events=events)
    adapter = ExecutorAdapter(lambda: executor)
    queue = asyncio.Queue()
    ctx = TurnContext(response_id="response", event_queue=queue, cancelled=asyncio.Event())
    request = CreateResponseRequest(
        model="test", input="peer", peer_delivery=_METADATA, peer_delivery_start=True
    )
    with contextlib.suppress(RuntimeError):
        await adapter.run_turn(request, ctx)
    receipts = []
    while not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, SDKPeerReceiptEvent):
            receipts.append(event)
    assert len(receipts) == 1
    assert receipts[0].outcome == outcome


@pytest.mark.parametrize("consumed", [False, True])
async def test_idle_pi_peer_requires_final_user_consumption(consumed):
    wire = [
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "reply"},
        },
        *(
            [{"type": "message_end", "message": {"role": "user", "content": "peer"}}]
            if consumed
            else []
        ),
        {"type": "agent_end", "messages": []},
    ]
    executor = _executor_with_scripted_rpc([json.dumps(event) for event in wire])
    adapter = ExecutorAdapter(lambda: executor)
    rpc = await executor._ensure_rpc(adapter._session_key, "", None, [])
    executor._session_states[adapter._session_key] = _PiSessionState(rpc=rpc)
    queue = asyncio.Queue()
    ctx = TurnContext(response_id="response", event_queue=queue, cancelled=asyncio.Event())
    request = CreateResponseRequest(
        model="test", input="peer", peer_delivery=_METADATA, peer_delivery_start=True
    )
    try:
        await adapter.run_turn(request, ctx)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        receipts = [event for event in events if isinstance(event, SDKPeerReceiptEvent)]
        assert len(receipts) == 1
        assert receipts[0].outcome == ("accepted" if consumed else "unknown")
        if consumed:
            assert receipts[0].proof_source == "pi_user_message_end"
    finally:
        await executor.close()
