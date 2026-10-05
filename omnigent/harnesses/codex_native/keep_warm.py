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
from typing import Any, NamedTuple

from omnigent.harnesses.codex_native.side_chat import (
    KeepWarmRequestFn,
    fork_keep_warm_thread,
    submit_keep_warm_turn,
)

_logger = logging.getLogger(__name__)

_JsonObject = dict[str, Any]

#: Hard budget for one fork ping; the turn normally completes in seconds.
KEEP_WARM_PING_BUDGET_S = 90.0

#: Item types a legitimate ping turn may produce; anything else (command
#: execution, file change, MCP tool, ...) means the model tried to work.
_PASSIVE_ITEM_TYPES = frozenset({"userMessage", "agentMessage", "reasoning"})

#: Server-request methods Codex answers with a decision verdict.
_APPROVAL_REQUEST_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "applyPatchApproval",
        "execCommandApproval",
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

    Approval methods take a decision verdict; the other shapes mirror the
    decline answers the SDK executor and the native forwarder already
    produce. The turn is interrupted right after, so the answer only needs
    to unblock the app-server.

    :param message: The app-server's request envelope.
    :returns: The JSON-RPC result payload to answer with.
    """
    method = message.get("method")
    if method in _APPROVAL_REQUEST_METHODS:
        return {"decision": "decline"}
    if method == "mcpServer/elicitation/request":
        return {"action": "decline", "content": None, "_meta": None}
    if method == "item/tool/requestUserInput":
        return {"answers": {}}
    if method == "item/tool/call":
        # Dynamic-tool refusal, mirroring ``_dynamic_tool_result_payload``.
        return {
            "success": False,
            "contentItems": [{"type": "inputText", "text": "keep-warm ping declined"}],
        }
    # Anything unrecognized: the approval verdict is the only refusal shape
    # both Codex clients already produce.
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
        :data:`KEEP_WARM_PING_BUDGET_S`.
    :returns: The ping outcome and raw usage.
    :raises RuntimeError: When the fork or the event stream fails outright —
        the caller reports ``harness_error``.
    """
    budget = KEEP_WARM_PING_BUDGET_S if budget_s is None else budget_s
    forked = await fork_keep_warm_thread(request, parent_thread_id)
    if forked is None:
        raise RuntimeError("Codex keep-warm fork returned no thread id")
    fork_id, model = forked
    events = events_for(fork_id)
    turn_id = await submit_keep_warm_turn(request, fork_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    last_usage: _JsonObject | None = None
    iterator = events.__aiter__()
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await _interrupt_keep_warm_turn(request, fork_id, turn_id)
                return KeepWarmPingResult("failed", "timeout")
            try:
                message = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
            except TimeoutError:
                await _interrupt_keep_warm_turn(request, fork_id, turn_id)
                return KeepWarmPingResult("failed", "timeout")
            except StopAsyncIteration:
                raise RuntimeError(
                    "Codex keep-warm event stream ended before the ping turn completed"
                ) from None
            if _fork_thread_id(message) != fork_id:
                # Another thread's traffic on a shared stream (e.g. a real
                # turn that started on the parent mid-ping) is not ours.
                continue
            request_id = message.get("id")
            if request_id is not None and message.get("method") is not None:
                # The turn asked for something (approval, input, a tool
                # call): refuse so the app-server never hangs, then tear the
                # turn down.
                await respond(request_id, refusal_payload(message))
                await _interrupt_keep_warm_turn(request, fork_id, turn_id)
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
                turn = params.get("turn")
                if isinstance(turn, dict) and isinstance(turn.get("id"), str) and turn["id"]:
                    turn_id = turn["id"]
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
                    await _interrupt_keep_warm_turn(request, fork_id, turn_id)
                    return KeepWarmPingResult("failed", "tool_attempt")
            elif method == "turn/completed":
                return _ok_result(last_usage, model)
            elif method == "turn/failed":
                return KeepWarmPingResult("failed", "harness_error", model=model)
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


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


async def _interrupt_keep_warm_turn(
    request: KeepWarmRequestFn, fork_thread_id: str, turn_id: str | None
) -> None:
    """Best-effort ``turn/interrupt`` of the ping turn; never raises."""
    if not turn_id:
        return
    with contextlib.suppress(Exception):
        await request("turn/interrupt", {"threadId": fork_thread_id, "turnId": turn_id})


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
    thread_id = params.get("threadId")
    if isinstance(thread_id, str) and thread_id:
        return thread_id
    thread = params.get("thread")
    if isinstance(thread, dict):
        nested = thread.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


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
