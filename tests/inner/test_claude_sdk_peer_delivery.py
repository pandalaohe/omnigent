"""Receipt-bound peer delivery through Claude SDK hooks."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from omnigent.inner.claude_sdk_executor import (
    ClaudeSDKExecutor,
    _ClaudePeerReceiptStore,
    claude_peer_receipts_supported,
)
from omnigent.sdk_peer_delivery import SDKPeerInput


def _delivery(number: int) -> SDKPeerInput:
    return SDKPeerInput(f"{number:032x}", f"{number + 100:032x}", f"peer envelope {number}")


def _hooks(executor: ClaudeSDKExecutor, session: str):
    class Matcher:
        def __init__(self, *, hooks):
            self.hooks = hooks

    options = SimpleNamespace(hooks={})
    executor._install_peer_hooks(cast(Any, SimpleNamespace(HookMatcher=Matcher)), options, session)
    return {event: matchers[0].hooks[0] for event, matchers in options.hooks.items()}


def _payload(event: str, *, session: str = "sdk-session", tool: str = "tool-1", **extra):
    return {
        "hook_event_name": event,
        "session_id": session,
        "tool_use_id": tool,
        **extra,
    }


def _receipt(event: str, text: str, *, session: str = "sdk-session", tool: str = "tool-1"):
    return {
        "type": "attachment",
        "sessionId": session,
        "attachment": {
            "type": "hook_additional_context",
            "content": [text],
            "hookEvent": event,
            "hookName": f"{event}:*",
            "toolUseID": tool,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure"])
async def test_hook_accepts_only_matching_sdk_mirror_attachment(event: str) -> None:
    executor = ClaudeSDKExecutor()
    executor._query_slot("run").busy = 1
    outcomes = []

    async def report(outcome):
        outcomes.append(outcome)

    hooks = _hooks(executor, "run")
    with patch(
        "omnigent.inner.claude_sdk_executor.claude_peer_receipts_supported", return_value=True
    ):
        await executor.submit_peer_message("run", _delivery(1), report)
    assert await hooks[event](_payload(event), "tool-1", {}) == {
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": "peer envelope 1"}
    }
    assert outcomes == []

    store = _ClaudePeerReceiptStore(executor, "run")
    key = {"project_key": "-opt-work-omnigent", "session_id": "sdk-session"}
    assert await store.load(key) is None
    proof = _receipt(event, "peer envelope 1")
    for bad_key, bad_proof in (
        ({**key, "subpath": "agent-1"}, proof),
        (key, _receipt(event, "peer envelope 1", session="other")),
        (key, _receipt(event, "peer envelope 1", tool="other")),
        (key, _receipt(event, "other envelope")),
        (key, {**proof, "type": "user"}),
    ):
        await store.append(cast(Any, bad_key), cast(Any, [bad_proof]))
    assert outcomes == []
    await store.append(cast(Any, key), cast(Any, [proof, proof]))
    assert [(value.outcome, value.proof_source) for value in outcomes] == [
        ("accepted", "claude_session_store_hook")
    ]
    await executor.finish_peer_messages("run")
    assert len(outcomes) == 1


@pytest.mark.asyncio
async def test_parallel_hooks_reserve_distinct_attempts_and_end_turn_settles() -> None:
    executor = ClaudeSDKExecutor()
    executor._query_slot("run").busy = 1
    outcomes = []

    async def report(outcome):
        outcomes.append(outcome)

    hooks = _hooks(executor, "run")
    with patch(
        "omnigent.inner.claude_sdk_executor.claude_peer_receipts_supported", return_value=True
    ):
        for number in (1, 2, 3):
            await executor.submit_peer_message("run", _delivery(number), report)
    responses = await asyncio.gather(
        hooks["PostToolUse"](_payload("PostToolUse", tool="tool-1"), "tool-1", {}),
        hooks["PostToolUseFailure"](_payload("PostToolUseFailure", tool="tool-2"), "tool-2", {}),
    )
    assert [response["hookSpecificOutput"]["additionalContext"] for response in responses] == [
        "peer envelope 1",
        "peer envelope 2",
    ]
    assert await hooks["PostToolUse"](_payload("PostToolUse", tool="tool-1"), "tool-1", {}) == {}
    await executor.finish_peer_messages("run")
    assert [(value.peer_id, value.outcome) for value in outcomes] == [
        (_delivery(1).peer_id, "unknown"),
        (_delivery(2).peer_id, "unknown"),
        (_delivery(3).peer_id, "refused"),
    ]


@pytest.mark.asyncio
async def test_late_mirror_receipt_upgrades_unknown_once() -> None:
    executor = ClaudeSDKExecutor()
    executor._query_slot("run").busy = 1
    outcomes = []

    async def report(outcome):
        outcomes.append(outcome)

    hook = _hooks(executor, "run")["PostToolUse"]
    with patch(
        "omnigent.inner.claude_sdk_executor.claude_peer_receipts_supported", return_value=True
    ):
        await executor.submit_peer_message("run", _delivery(1), report)
    await hook(_payload("PostToolUse"), "tool-1", {})
    await executor.finish_peer_messages("run")
    assert [value.outcome for value in outcomes] == ["unknown"]
    assert "run" not in executor._peer_pending
    assert executor._peer_late["run"][0].delivery.text == ""
    store = _ClaudePeerReceiptStore(executor, "run")
    proof = _receipt("PostToolUse", "peer envelope 1")
    key = {"project_key": "-opt-work-omnigent", "session_id": "sdk-session"}
    await store.append(cast(Any, key), cast(Any, [proof]))
    await store.append(cast(Any, key), cast(Any, [proof]))
    assert [value.outcome for value in outcomes] == ["unknown", "accepted"]


@pytest.mark.asyncio
async def test_actual_can_use_tool_await_blocks_peer_reservation() -> None:
    executor = ClaudeSDKExecutor(permission_mode="default")
    executor._query_slot("run").busy = 1
    entered = asyncio.Event()
    release = asyncio.Event()

    async def permission(_name, _args):
        entered.set()
        await release.wait()
        return True

    executor._elicitation_handler = permission
    outcomes = []

    async def report(outcome):
        outcomes.append(outcome)

    hook = _hooks(executor, "run")["PostToolUse"]
    task = asyncio.create_task(
        executor._can_use_tool_gate("Bash", {}, None, session_key="run", permission_mode="default")
    )
    await entered.wait()
    with patch(
        "omnigent.inner.claude_sdk_executor.claude_peer_receipts_supported", return_value=True
    ):
        await executor.submit_peer_message("run", _delivery(1), report)
    assert [value.outcome for value in outcomes] == ["refused"]
    assert await hook(_payload("PostToolUse"), "tool-1", {}) == {}
    release.set()
    await task
    assert executor._query_slot("run").permission_waits == 0


@pytest.mark.asyncio
async def test_hook_excludes_subagent_maintenance_and_pending_permission() -> None:
    executor = ClaudeSDKExecutor()
    slot = executor._query_slot("run")
    slot.busy = 1
    outcomes = []

    async def report(outcome):
        outcomes.append(outcome)

    hook = _hooks(executor, "run")["PostToolUse"]
    with patch(
        "omnigent.inner.claude_sdk_executor.claude_peer_receipts_supported", return_value=True
    ):
        await executor.submit_peer_message("run", _delivery(1), report)
    payload = _payload("PostToolUse")
    assert await hook(_payload("PostToolUse", agent_id="agent-1"), "tool-1", {}) == {}
    slot.maintenance = True
    assert await hook(payload, "tool-1", {}) == {}
    slot.maintenance = False
    slot.permission_waits = 1
    assert await hook(payload, "tool-1", {}) == {}
    slot.permission_waits = 0
    result = await hook(payload, "tool-1", {})
    assert result["hookSpecificOutput"]["additionalContext"] == "peer envelope 1"
    await executor.finish_peer_messages("run")
    assert [value.outcome for value in outcomes] == ["unknown"]


@pytest.mark.asyncio
async def test_absent_sdk_mirror_capability_refuses_and_never_queues() -> None:
    executor = ClaudeSDKExecutor()
    executor._query_slot("run").busy = 1
    outcomes = []

    async def report(outcome):
        outcomes.append(outcome)

    with patch(
        "omnigent.inner.claude_sdk_executor.claude_peer_receipts_supported", return_value=False
    ):
        await executor.submit_peer_message("run", _delivery(1), report)
    assert [value.outcome for value in outcomes] == ["refused"]
    assert "run" not in executor._peer_pending


def test_installed_sdk_declares_required_public_mirror_options() -> None:
    with patch("omnigent.inner.claude_sdk_executor._find_system_claude", return_value=None):
        assert claude_peer_receipts_supported()
    with patch(
        "omnigent.inner.claude_sdk_executor._find_system_claude",
        return_value="/opt/work/omnigent/system-claude",
    ):
        assert not claude_peer_receipts_supported()


@pytest.mark.asyncio
async def test_store_load_none_keeps_sdk_local_resume_path() -> None:
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.session_resume import materialize_resume_session

    executor = ClaudeSDKExecutor()
    store = _ClaudePeerReceiptStore(executor, "run")
    options = ClaudeAgentOptions(
        cwd="/opt/work/omnigent",
        resume="12345678-1234-1234-1234-123456789012",
        session_store=cast(Any, store),
    )
    assert await materialize_resume_session(options) is None
