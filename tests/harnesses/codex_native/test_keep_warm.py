"""Unit tests for the shared Codex keep-warm ping driver.

Covers the refusal payload shapes (mirroring the server-side elicitation
adapters), the legacy ``conversationId`` fork identity, the whole-ping budget
and the best-effort interrupt cleanup on every exceptional exit.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from omnigent.harnesses.codex_native import keep_warm as keep_warm_module
from omnigent.harnesses.codex_native.keep_warm import (
    drive_keep_warm_ping,
    refusal_payload,
)

_JsonObject = dict[str, Any]
_FORK_ID = "thread_fork"
_TURN_ID = "turn_fork"
_PARENT_ID = "thread_parent"


def _fork_response() -> _JsonObject:
    """The ``thread/fork`` result for the ping's ephemeral fork."""
    return {"result": {"thread": {"id": _FORK_ID, "model": "gpt-test"}}}


def _turn_response() -> _JsonObject:
    """The ``turn/start`` result for the ping turn."""
    return {"result": {"turn": {"id": _TURN_ID}}}


class _PingSide:
    """Recording app-server side of one ping: RPCs, responses, fork stream."""

    def __init__(self, events: Any) -> None:
        """
        Initialize with a scripted fork-stream factory.

        :param events: Callable/factory mirroring ``events_for``.
        """
        self.requests: list[tuple[str, _JsonObject]] = []
        self.responses: list[tuple[int | str, _JsonObject]] = []
        self._events = events

    async def request(self, method: str, params: _JsonObject) -> _JsonObject:
        """Record one RPC; answer the fork and turn calls."""
        self.requests.append((method, params))
        if method == "thread/fork":
            return _fork_response()
        if method == "turn/start":
            return _turn_response()
        return {"result": {}}

    async def respond(self, request_id: int | str, result: _JsonObject) -> None:
        """Record one response to a server request."""
        self.responses.append((request_id, result))

    def events_for(self, fork_id: str) -> AsyncIterator[_JsonObject]:
        """Build the scripted fork stream for *fork_id*."""
        return self._events(fork_id)

    @property
    def interrupt_calls(self) -> list[tuple[str, _JsonObject]]:
        """The recorded ``turn/interrupt`` cleanup requests."""
        return [call for call in self.requests if call[0] == "turn/interrupt"]


async def _blocking_events(_fork_id: str) -> AsyncIterator[_JsonObject]:
    """A fork stream that yields nothing and never ends."""
    await asyncio.Event().wait()
    yield {}  # pragma: no cover — the wait never returns


# --------------------------------------------------------------------------- #
# refusal_payload — method-specific shapes mirroring the server adapters
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("execCommandApproval", {"decision": "denied"}),
        ("applyPatchApproval", {"decision": "denied"}),
        ("item/commandExecution/requestApproval", {"decision": "decline"}),
        ("item/fileChange/requestApproval", {"decision": "decline"}),
        ("item/permissions/requestApproval", {"permissions": {}, "scope": "turn"}),
        (
            "mcpServer/elicitation/request",
            {"action": "decline", "content": None, "_meta": None},
        ),
        ("item/tool/requestUserInput", {"answers": {}}),
        (
            "item/tool/call",
            {
                "success": False,
                "contentItems": [{"type": "inputText", "text": "keep-warm ping declined"}],
            },
        ),
        ("unknown/method", {"decision": "decline"}),
    ],
)
def test_refusal_payload_mirrors_the_server_response_shapes(
    method: str, expected: _JsonObject
) -> None:
    """Each refusal payload equals the decline shape the server adapter builds."""
    assert refusal_payload({"id": 1, "method": method, "params": {}}) == expected


# --------------------------------------------------------------------------- #
# Legacy conversationId identity
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_legacy_conversation_id_request_is_answered_by_the_driver() -> None:
    """A fork request keyed only by ``conversationId`` is refused, not ignored."""
    side = _PingSide(_legacy_request_events)

    result = await drive_keep_warm_ping(
        request=side.request,
        respond=side.respond,
        events_for=side.events_for,
        parent_thread_id=_PARENT_ID,
        budget_s=1.0,
    )

    assert result.outcome == "failed" and result.reason == "tool_attempt"
    assert side.responses == [(9, {"decision": "denied"})]
    assert side.interrupt_calls[-1] == (
        "turn/interrupt",
        {"threadId": _FORK_ID, "turnId": _TURN_ID},
    )


async def _legacy_request_events(fork_id: str) -> AsyncIterator[_JsonObject]:
    """The fork's legacy ``applyPatchApproval`` keyed only by ``conversationId``."""
    yield {
        "id": 9,
        "method": "applyPatchApproval",
        "params": {"conversationId": fork_id, "callId": "call_1"},
    }
    await asyncio.Event().wait()


# --------------------------------------------------------------------------- #
# Exceptional exits — interrupt the unfinished fork turn
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cancelled_ping_interrupts_the_unfinished_fork_turn() -> None:
    """Cancellation still sends ``turn/interrupt``, then re-raises."""
    side = _PingSide(_blocking_events)
    task = asyncio.create_task(
        drive_keep_warm_ping(
            request=side.request,
            respond=side.respond,
            events_for=side.events_for,
            parent_thread_id=_PARENT_ID,
            budget_s=60.0,
        )
    )
    while not any(method == "turn/start" for method, _ in side.requests):
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert side.interrupt_calls == [("turn/interrupt", {"threadId": _FORK_ID, "turnId": _TURN_ID})]


@pytest.mark.asyncio
async def test_ended_event_stream_interrupts_the_unfinished_fork_turn() -> None:
    """A stream that ends before ``turn/completed`` interrupts before raising."""
    side = _PingSide(_empty_events)

    with pytest.raises(RuntimeError, match="ended before the ping turn completed"):
        await drive_keep_warm_ping(
            request=side.request,
            respond=side.respond,
            events_for=side.events_for,
            parent_thread_id=_PARENT_ID,
            budget_s=60.0,
        )

    assert side.interrupt_calls == [("turn/interrupt", {"threadId": _FORK_ID, "turnId": _TURN_ID})]


async def _empty_events(_fork_id: str) -> AsyncIterator[_JsonObject]:
    """A fork stream that ends immediately."""
    return
    yield {}  # pragma: no cover — async generator shape


@pytest.mark.asyncio
async def test_failed_respond_interrupts_the_unfinished_fork_turn() -> None:
    """A ``respond`` failure propagates but never skips the interrupt."""
    side = _PingSide(_approval_request_events)

    async def failing_respond(request_id: int | str, result: _JsonObject) -> None:
        raise RuntimeError("respond failed")

    with pytest.raises(RuntimeError, match="respond failed"):
        await drive_keep_warm_ping(
            request=side.request,
            respond=failing_respond,
            events_for=side.events_for,
            parent_thread_id=_PARENT_ID,
            budget_s=60.0,
        )

    assert side.interrupt_calls == [("turn/interrupt", {"threadId": _FORK_ID, "turnId": _TURN_ID})]


async def _approval_request_events(fork_id: str) -> AsyncIterator[_JsonObject]:
    """One fork approval request, then a stream that never ends."""
    yield {
        "id": 7,
        "method": "item/commandExecution/requestApproval",
        "params": {"threadId": fork_id, "turnId": _TURN_ID, "itemId": "item_1"},
    }
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_event_stream_exception_interrupts_the_unfinished_fork_turn() -> None:
    """Any exception after ``turn/start`` still interrupts before propagating."""
    side = _PingSide(_exploding_events)

    with pytest.raises(ValueError, match="stream exploded"):
        await drive_keep_warm_ping(
            request=side.request,
            respond=side.respond,
            events_for=side.events_for,
            parent_thread_id=_PARENT_ID,
            budget_s=60.0,
        )

    assert side.interrupt_calls == [("turn/interrupt", {"threadId": _FORK_ID, "turnId": _TURN_ID})]


async def _exploding_events(_fork_id: str) -> AsyncIterator[_JsonObject]:
    """A fork stream that raises on its first read."""
    raise ValueError("stream exploded")
    yield {}  # pragma: no cover — async generator shape


@pytest.mark.asyncio
async def test_interrupt_cleanup_is_bounded_and_never_replaces_the_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled ``turn/interrupt`` is cut at its own bound, outcome preserved."""
    monkeypatch.setattr(keep_warm_module, "_KEEP_WARM_INTERRUPT_TIMEOUT_S", 0.05)
    side = _InterruptStallingSide(_exploding_events)
    started = time.monotonic()

    with pytest.raises(ValueError, match="stream exploded"):
        await drive_keep_warm_ping(
            request=side.request,
            respond=side.respond,
            events_for=side.events_for,
            parent_thread_id=_PARENT_ID,
            budget_s=60.0,
        )

    assert time.monotonic() - started < 1.0
    assert side.stalled_interrupts == 1


class _InterruptStallingSide(_PingSide):
    """A ping side whose ``turn/interrupt`` never answers."""

    def __init__(self, events: Any) -> None:
        super().__init__(events)
        self.stalled_interrupts = 0

    async def request(self, method: str, params: _JsonObject) -> _JsonObject:
        """Stall the interrupt RPC; answer the fork/turn RPCs normally."""
        if method == "turn/interrupt":
            self.stalled_interrupts += 1
            await asyncio.Event().wait()
        return await super().request(method, params)


# --------------------------------------------------------------------------- #
# Whole-ping budget
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_stalled_fork_rpc_times_out_within_the_budget() -> None:
    """A ``thread/fork`` that never answers ends ``failed``/``timeout`` in time."""

    async def stalling_fork_request(method: str, params: _JsonObject) -> _JsonObject:
        await asyncio.Event().wait()
        return {}  # pragma: no cover — the wait never returns

    side = _PingSide(_blocking_events)
    started = time.monotonic()

    result = await drive_keep_warm_ping(
        request=stalling_fork_request,
        respond=side.respond,
        events_for=side.events_for,
        parent_thread_id=_PARENT_ID,
        budget_s=0.05,
    )

    assert result.outcome == "failed" and result.reason == "timeout"
    assert time.monotonic() - started < 1.0
    assert side.requests == []


@pytest.mark.asyncio
async def test_stalled_turn_start_rpc_still_interrupts_the_started_fork_turn() -> None:
    """A stalled ``turn/start`` reply is interrupted via its ``turn/started`` id."""
    side = _TurnStartStallingSide(_turn_started_events)
    started = time.monotonic()

    result = await drive_keep_warm_ping(
        request=side.request,
        respond=side.respond,
        events_for=side.events_for,
        parent_thread_id=_PARENT_ID,
        budget_s=0.05,
    )

    assert result.outcome == "failed" and result.reason == "timeout"
    assert time.monotonic() - started < 1.0
    assert [method for method, _ in side.requests] == [
        "thread/fork",
        "turn/start",
        "turn/interrupt",
    ]
    assert side.interrupt_calls == [("turn/interrupt", {"threadId": _FORK_ID, "turnId": _TURN_ID})]


class _TurnStartStallingSide(_PingSide):
    """A ping side whose ``turn/start`` reply never lands."""

    async def request(self, method: str, params: _JsonObject) -> _JsonObject:
        """Stall the turn RPC; answer the fork and interrupt RPCs normally."""
        if method == "turn/start":
            self.requests.append((method, params))
            await asyncio.Event().wait()
        return await super().request(method, params)


async def _turn_started_events(fork_id: str) -> AsyncIterator[_JsonObject]:
    """The fork's ``turn/started``, then a stream that never ends."""
    yield {
        "method": "turn/started",
        "params": {"threadId": fork_id, "turn": {"id": _TURN_ID}},
    }
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_cancelled_ping_awaiting_the_turn_start_reply_interrupts_the_started_turn() -> None:
    """Cancellation while the ``turn/start`` reply stalls still interrupts."""
    processed = asyncio.Event()

    async def events(fork_id: str) -> AsyncIterator[_JsonObject]:
        yield {
            "method": "turn/started",
            "params": {"threadId": fork_id, "turn": {"id": _TURN_ID}},
        }
        # Resumed only after the driver consumed the notification above, so
        # the learned turn id is in place before the test cancels the ping.
        processed.set()
        await asyncio.Event().wait()

    side = _TurnStartStallingSide(events)
    task = asyncio.create_task(
        drive_keep_warm_ping(
            request=side.request,
            respond=side.respond,
            events_for=side.events_for,
            parent_thread_id=_PARENT_ID,
            budget_s=60.0,
        )
    )
    await asyncio.wait_for(processed.wait(), timeout=1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert side.interrupt_calls == [("turn/interrupt", {"threadId": _FORK_ID, "turnId": _TURN_ID})]


@pytest.mark.asyncio
async def test_stalled_refusal_write_times_out_through_the_interrupt_cleanup() -> None:
    """A ``respond`` that never answers is cut by the budget and interrupted."""
    side = _PingSide(_approval_request_events)

    async def stalling_respond(request_id: int | str, result: _JsonObject) -> None:
        await asyncio.Event().wait()

    result = await asyncio.wait_for(
        drive_keep_warm_ping(
            request=side.request,
            respond=stalling_respond,
            events_for=side.events_for,
            parent_thread_id=_PARENT_ID,
            budget_s=0.05,
        ),
        timeout=1.0,
    )

    assert result.outcome == "failed" and result.reason == "timeout"
    assert side.responses == []
    assert side.interrupt_calls == [("turn/interrupt", {"threadId": _FORK_ID, "turnId": _TURN_ID})]
