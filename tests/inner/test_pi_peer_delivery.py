"""RPC Pi peer input settles from final consumed user messages."""

import json
from unittest.mock import AsyncMock

import pytest

from omnigent.inner.executor import ExecutorConfig, ExecutorError, TextChunk, TurnComplete
from omnigent.inner.pi_executor import _PiSessionState
from omnigent.sdk_peer_delivery import SDKPeerInput
from tests.inner.test_pi_executor import _executor_with_scripted_rpc
from tests.inner.test_pi_executor import context_v2_pi as context_v2_pi


@pytest.mark.parametrize("boundary", ["agent_end", "agent_settled"])
@pytest.mark.parametrize("result", ["consumed", "rejected", "unconfirmed"])
async def test_pi_peer_receipt_requires_consumption_without_failing_user_turn(boundary, result):
    delivery = SDKPeerInput("a" * 32, "b" * 32, "peer envelope")
    final_user = {
        "type": "message_end",
        "message": {"role": "user", "content": [{"type": "text", "text": delivery.text}]},
    }
    events = [
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "before"},
        },
        {"type": "response", "id": delivery.attempt_id, "success": result != "rejected"},
        {"type": "message_start", "message": final_user["message"]},
        {"type": "message_end", "message": {"role": "assistant", "content": delivery.text}},
        {"type": "message_end", "message": {"role": "user", "content": "unrelated"}},
        *([final_user, final_user] if result == "consumed" else []),
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "after"},
        },
        {"type": "agent_end", "messages": []},
        *([{"type": "agent_settled"}] if boundary == "agent_settled" else []),
    ]
    executor = _executor_with_scripted_rpc([json.dumps(event) for event in events])
    executor._settled_turn_boundary = boundary == "agent_settled"
    rpc = await executor._ensure_rpc("session-1", "system", None, [])
    executor._session_states["session-1"] = _PiSessionState(rpc=rpc)
    report = AsyncMock()
    turn = executor.run_turn(
        [{"role": "user", "content": "original", "session_id": "session-1"}], [], "system"
    )
    try:
        assert isinstance(await anext(turn), TextChunk)
        await executor.submit_peer_message("session-1", delivery, report)
        await executor.submit_peer_message("session-1", delivery, report)
        assert report.await_count == 0, "stdin write/queued acknowledgment is not consumption"
        remaining = [event async for event in turn]
        assert not any(isinstance(event, ExecutorError) for event in remaining)
        assert any(isinstance(event, TurnComplete) for event in remaining)
        assert [call.args[0].outcome for call in report.await_args_list] == [
            {"consumed": "accepted", "rejected": "refused", "unconfirmed": "unknown"}[result]
        ]
        commands = [json.loads(line) for line in rpc.process.stdin.data]
        steers = [command for command in commands if command["type"] == "steer"]
        assert steers == [{"type": "steer", "id": delivery.attempt_id, "message": delivery.text}]
    finally:
        await turn.aclose()
        await executor.close()


async def test_pi_peer_without_active_turn_is_refused_without_starting_one():
    executor = _executor_with_scripted_rpc([])
    report = AsyncMock()
    await executor.submit_peer_message(
        "session-1", SDKPeerInput("a" * 32, "b" * 32, "peer"), report
    )
    assert report.await_args.args[0].outcome == "refused"
    assert executor._session_states == {}


async def test_initial_peer_survives_replacing_the_previous_model(context_v2_pi):
    executor, _, _, processes = context_v2_pi
    await executor._ensure_rpc("session-1", "system", "old-model", [])
    delivery = SDKPeerInput("a" * 32, "b" * 32, "peer")
    report = AsyncMock()
    assert await executor.prepare_peer_turn("session-1", delivery, report)
    await executor._ensure_rpc("session-1", "system", "new-model", [])
    assert report.await_count == 0
    processes[-1].stdout.feed_data(
        b'{"type":"message_end","message":{"role":"user","content":"peer"}}\n'
        b'{"type":"agent_end","messages":[]}\n'
    )
    events = [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": "peer", "session_id": "session-1"}],
            [],
            "system",
            ExecutorConfig(model="new-model"),
        )
    ]
    assert any(isinstance(event, TurnComplete) for event in events)
    assert [call.args[0].outcome for call in report.await_args_list] == ["accepted"]
