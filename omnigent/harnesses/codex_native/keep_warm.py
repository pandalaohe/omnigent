"""Guarded keep-warm fork ping driver shared by the Codex harnesses.

One keep-warm ping forks the live Codex thread into an ephemeral thread
(stamped ``KEEP_WARM_THREAD_SOURCE``), runs a fixed one-line turn there, and
reads the fork's token usage: the model round-trip re-reads the parent's
inherited context, which is what re-warms the provider prompt cache. The fork
never persists and never touches the parent thread's state.

The turn is guarded end to end: it starts with ``untrusted`` approval and a
read-only sandbox, and the driver refuses and interrupts on any sign of real
work — a non-passive item (command execution, file change, ...) or any
server-to-client request (approval, user input, tool call). A turn that
outlives the budget is interrupted and reported as a timeout.

Both Codex keep-warm channels drive it: codex-native over a throwaway
:class:`~omnigent.harnesses.codex_native.app_server.CodexAppServerClient`
(``client.request`` / ``client.respond`` / ``client.iter_events()``), the
codex SDK harness over its live session's ``_request`` / ``_send_response``
and the per-fork queue its reader loop fills.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, NamedTuple, TypeVar

from omnigent.harnesses.codex_native.side_chat import (
    KeepWarmRequestFn,
    fork_keep_warm_thread,
    submit_keep_warm_turn,
)

_logger = logging.getLogger(__name__)

_JsonObject = dict[str, Any]

#: Hard budget for one fork ping; the turn normally completes in seconds.
KEEP_WARM_PING_BUDGET_S = 90.0

#: Bound on the best-effort ``turn/interrupt`` cleanup after a failed ping.
_KEEP_WARM_INTERRUPT_TIMEOUT_S = 5.0

#: Item types a legitimate ping turn may produce; anything else (command
#: execution, file change, MCP tool, ...) means the model tried to work.
_PASSIVE_ITEM_TYPES = frozenset({"userMessage", "agentMessage", "reasoning"})

#: v2 approval methods whose decline verdict is ``"decline"``.
_V2_APPROVAL_REQUEST_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }
)

#: Legacy approval methods whose deny verdict is ``"denied"``.
_LEGACY_APPROVAL_REQUEST_METHODS = frozenset(
    {
        "execCommandApproval",
        "applyPatchApproval",
    }
)


class KeepWarmPingResult(NamedTuple):
    """
    One fork ping's outcome and raw usage, receipt-ready.

    :param outcome: ``"ok"`` or ``"failed"``.
    :param reason: Failure reason (``tool_attempt`` / ``timeout`` /
        ``harness_error``); ``None`` on ``"ok"``.
    :param input_total: Fork turn's ``inputTokens`` (inclusive of cached).
    :param cache_read: Fork turn's ``cachedInputTokens``.
    :param output_tokens: Fork turn's ``outputTokens``.
    :param model: The fork thread's model, when observed.
    """

    outcome: str
    reason: str | None = None
    input_total: int | None = None
    cache_read: int | None = None
    output_tokens: int | None = None
    model: str | None = None


def refusal_payload(message: _JsonObject) -> _JsonObject:
    """
    Build the refusal answering a server request from the ping turn.

    Each shape mirrors the decline verdict the server-side adapters in
    ``omnigent/server/routes/_codex_elicitation.py`` produce for that
    method. The turn is interrupted right after, so the answer only needs
    to unblock the app-server.

    :param message: The app-server's request envelope.
    :returns: The JSON-RPC result payload to answer with.
    """
    method = message.get("method")
    if method in _LEGACY_APPROVAL_REQUEST_METHODS:
        # Mirrors _codex_command_approval_response / _codex_apply_patch_approval_response.
        return {"decision": "denied"}
    if method in _V2_APPROVAL_REQUEST_METHODS:
        # Mirrors _codex_command_approval_response / _codex_file_change_approval_response.
        return {"decision": "decline"}
    if method == "item/permissions/requestApproval":
        # Mirrors _codex_permissions_approval_response.
        return {"permissions": {}, "scope": "turn"}
    if method == "mcpServer/elicitation/request":
        # Mirrors _codex_mcp_elicitation_response.
        return {"action": "decline", "content": None, "_meta": None}
    if method == "item/tool/requestUserInput":
        # Mirrors _codex_request_user_input_response.
        return {"answers": {}}
    if method == "item/tool/call":
        # Dynamic-tool refusal, mirroring ``_dynamic_tool_result_payload``.
        return {
            "success": False,
            "contentItems": [{"type": "inputText", "text": "keep-warm ping declined"}],
        }
    # Anything unrecognized: the v2 approval verdict is the only other
    # refusal shape a Codex client already produces.
    return {"decision": "decline"}


async def drive_keep_warm_ping(
    *,
    request: KeepWarmRequestFn,
    respond: Callable[[int | str, _JsonObject], Awaitable[None]],
    events_for: Callable[[str], AsyncIterator[_JsonObject]],
    parent_thread_id: str,
    budget_s: float | None = None,
) -> KeepWarmPingResult:
    """
    Run one guarded keep-warm ping against a live app-server connection.

    :param request: JSON-RPC request callable.
    :param respond: JSON-RPC response callable for server requests.
    :param events_for: Builds the fork thread's message stream once its id is
        known; any registration the stream needs must happen inside, before
        the ping turn starts.
    :param parent_thread_id: Live Codex thread id to fork from.
    :param budget_s: Ping budget in seconds; ``None`` takes
        :data:`KEEP_WARM_PING_BUDGET_S`. Bounds the WHOLE ping — the fork
        RPC, the turn RPC and the event consumption; the teardown interrupt
        has its own bound.
    :returns: The ping outcome and raw usage.
    :raises RuntimeError: When the fork or the event stream fails outright —
        the caller reports ``harness_error``.
    """
    budget = KEEP_WARM_PING_BUDGET_S if budget_s is None else budget_s
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    try:
        forked = await _await_within_budget(
            fork_keep_warm_thread(request, parent_thread_id), deadline, loop
        )
    except TimeoutError:
        return KeepWarmPingResult("failed", "timeout")
    if forked is None:
        raise RuntimeError("Codex keep-warm fork returned no thread id")
    fork_id, model = forked
    events = events_for(fork_id)
    iterator = events.__aiter__()
    turn_start: asyncio.Task[str | None] | None = None
    pending_event: asyncio.Task[_JsonObject] | None = None
    last_usage: _JsonObject | None = None
    turn_finished = False
    turn_id: str | None = None
    try:
        # Submit the turn as a task so a stalled ``turn/start`` reply cannot
        # hide the turn: the fork's ``turn/started`` notification still
        # teaches us the turn id the bounded interrupt cleanup needs.
        turn_start = asyncio.ensure_future(submit_keep_warm_turn(request, fork_id))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return KeepWarmPingResult("failed", "timeout")
            if pending_event is None:
                pending_event = asyncio.ensure_future(iterator.__anext__())
            pending: set[asyncio.Task[Any]] = {pending_event}
            if turn_start is not None:
                pending.add(turn_start)
            done, _ = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                return KeepWarmPingResult("failed", "timeout")
            if turn_start is not None and turn_start in done:
                started_turn_id = turn_start.result()
                if started_turn_id:
                    turn_id = started_turn_id
                turn_start = None
            if pending_event is None or pending_event not in done:
                continue
            try:
                message = pending_event.result()
            except StopAsyncIteration:
                raise RuntimeError(
                    "Codex keep-warm event stream ended before the ping turn completed"
                ) from None
            pending_event = None
            if _fork_thread_id(message) != fork_id:
                # Another thread's traffic on a shared stream (e.g. a real
                # turn that started on the parent mid-ping) is not ours.
                continue
            request_id = message.get("id")
            if request_id is not None and message.get("method") is not None:
                # The turn asked for something (approval, input, a tool
                # call): refuse so the app-server never hangs; the cleanup
                # below tears the turn down. A stalled write is itself a
                # timeout, not a tool attempt.
                try:
                    await _await_within_budget(
                        respond(request_id, refusal_payload(message)), deadline, loop
                    )
                except TimeoutError:
                    return KeepWarmPingResult("failed", "timeout")
                return KeepWarmPingResult("failed", "tool_attempt")
            method = message.get("method")
            params = message.get("params")
            params = params if isinstance(params, dict) else {}
            if method == "thread/started":
                thread = params.get("thread")
                if isinstance(thread, dict):
                    started_model = thread.get("model")
                    if isinstance(started_model, str) and started_model:
                        model = started_model
            elif method == "turn/started":
                started_turn_id = _turn_id_from_message(message)
                if started_turn_id:
                    turn_id = started_turn_id
            elif method == "thread/tokenUsage/updated":
                token_usage = params.get("tokenUsage")
                if isinstance(token_usage, dict):
                    last = token_usage.get("last")
                    if isinstance(last, dict):
                        last_usage = last
            elif method == "item/started":
                item = params.get("item")
                item_type = item.get("type") if isinstance(item, dict) else None
                if item_type not in _PASSIVE_ITEM_TYPES:
                    return KeepWarmPingResult("failed", "tool_attempt")
            elif method == "turn/completed":
                turn_finished = True
                return _ok_result(last_usage, model)
            elif method == "turn/failed":
                turn_finished = True
                return KeepWarmPingResult("failed", "harness_error", model=model)
    finally:
        for task in (pending_event, turn_start):
            if task is None or task.cancelled():
                continue
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                value = await task
                # A reply or notification already in hand still names the
                # turn the interrupt below must stop.
                if isinstance(value, str) and value:
                    turn_id = value
                elif isinstance(value, dict) and _fork_thread_id(value) == fork_id:
                    learned = _turn_id_from_message(value)
                    if learned:
                        turn_id = learned
        try:
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
        finally:
            if not turn_finished:
                # Every exit that leaves the fork turn running — timeout,
                # refusal, stream end, a failed respond, cancellation, a
                # stalled turn start — tears it down. The cleanup is bounded
                # and never masks the ping's own outcome.
                await _interrupt_keep_warm_turn(request, fork_id, turn_id)


def keep_warm_receipt(
    attempt_id: str,
    *,
    outcome: str,
    reason: str | None,
    input_total: int | None = None,
    cache_read: int | None = None,
    cost_usd: float | None = None,
) -> dict[str, Any]:
    """
    Build one normalized keep-warm receipt (Codex channel).

    :param attempt_id: Ping attempt id to echo.
    :param outcome: ``"ok"`` / ``"skipped"`` / ``"failed"``.
    :param reason: Machine reason; ``None`` on ``"ok"``.
    :param input_total: Ping turn's ``inputTokens`` (inclusive of cached).
    :param cache_read: Ping turn's ``cachedInputTokens``.
    :param cost_usd: Computed cost, when the fork's model is priced.
    :returns: The receipt dict the runner relays to the server.
    """
    return {
        "attempt_id": attempt_id,
        "outcome": outcome,
        "reason": reason,
        "input_total": input_total,
        "cache_read": cache_read,
        # Codex reports no cache creation on this channel.
        "cache_write": None,
        "cost_usd": cost_usd,
        "estimated": False,
    }


def keep_warm_result_receipt(attempt_id: str, result: KeepWarmPingResult) -> dict[str, Any]:
    """
    Normalize a ping outcome into the receipt dict.

    The ok path prices the split usage (non-cached input, cache read, output)
    against the fork thread's model; an unpriced model leaves ``cost_usd``
    ``None``.

    :param attempt_id: Ping attempt id to echo.
    :param result: The driver's outcome.
    :returns: The receipt dict the runner relays to the server.
    """
    if result.outcome != "ok":
        return keep_warm_receipt(attempt_id, outcome=result.outcome, reason=result.reason)
    return keep_warm_receipt(
        attempt_id,
        outcome="ok",
        reason=None,
        input_total=result.input_total,
        cache_read=result.cache_read,
        cost_usd=_keep_warm_cost_usd(result),
    )


_T = TypeVar("_T")


async def _await_within_budget(
    awaitable: Awaitable[_T],
    deadline: float,
    loop: asyncio.AbstractEventLoop,
) -> _T:
    """Await *awaitable* under the ping deadline, cancelling it on expiry."""
    remaining = deadline - loop.time()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(awaitable, timeout=remaining)


async def _interrupt_keep_warm_turn(
    request: KeepWarmRequestFn, fork_thread_id: str, turn_id: str | None
) -> None:
    """Best-effort bounded ``turn/interrupt`` of the ping turn; never raises."""
    if not turn_id:
        return
    try:
        await asyncio.wait_for(
            request("turn/interrupt", {"threadId": fork_thread_id, "turnId": turn_id}),
            timeout=_KEEP_WARM_INTERRUPT_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — teardown is best-effort, never masks the outcome
        _logger.debug("Codex keep-warm turn interrupt failed", exc_info=True)


def _ok_result(last_usage: _JsonObject | None, model: str | None) -> KeepWarmPingResult:
    """Build the ok outcome from the fork's last token-usage breakdown."""
    if last_usage is None:
        return KeepWarmPingResult("ok", model=model)
    return KeepWarmPingResult(
        "ok",
        input_total=_usage_int(last_usage.get("inputTokens")),
        cache_read=_usage_int(last_usage.get("cachedInputTokens")),
        output_tokens=_usage_int(last_usage.get("outputTokens")),
        model=model,
    )


def _usage_int(value: object) -> int | None:
    """Return *value* when it is a genuine int (never a bool), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _fork_thread_id(message: _JsonObject) -> str | None:
    """Thread id a fork message belongs to (notification or server request)."""
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    # Legacy server requests identify the thread by ``conversationId``.
    thread_id = params.get("threadId") or params.get("conversationId")
    if isinstance(thread_id, str) and thread_id:
        return thread_id
    thread = params.get("thread")
    if isinstance(thread, dict):
        nested = thread.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _turn_id_from_message(message: _JsonObject) -> str | None:
    """The turn id a ``turn/started`` notification carries, else ``None``."""
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    turn = params.get("turn")
    if not isinstance(turn, dict):
        return None
    turn_id = turn.get("id")
    return turn_id if isinstance(turn_id, str) and turn_id else None


def _keep_warm_cost_usd(result: KeepWarmPingResult) -> float | None:
    """
    Price the ping turn's split usage against the fork model's catalog rates.

    :param result: An ok ping outcome.
    :returns: The USD cost, or ``None`` when usage or pricing is missing.
    """
    if result.model is None or result.input_total is None or result.output_tokens is None:
        return None
    from omnigent.llms.context_window import compute_llm_cost, fetch_model_pricing

    pricing = fetch_model_pricing(result.model)
    if pricing is None:
        return None
    cache_read = result.cache_read or 0
    usage: dict[str, Any] = {
        # Codex inputTokens is inclusive of cached tokens; compute_llm_cost
        # wants the non-cached portion with cache reads priced separately.
        "input_tokens": max(0, result.input_total - cache_read),
        "output_tokens": result.output_tokens,
    }
    if cache_read:
        usage["cache_read_input_tokens"] = cache_read
    return compute_llm_cost(usage, pricing)
