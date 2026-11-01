"""Volatile child-retry scheduling backed by the server's fenced journal.

Construction is explicit: ordinary runner startup does not enable scheduling.
Final outcomes are journal/outbox intents; delivery recovery owns their effects.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.entities.subagent_retry import InboxOwnerFence, RetryChain, RetryFinal, RetryMutation
from omnigent.entities.subagent_terminal import TerminalEnvelope
from omnigent.runner.policy_proxy import _ASK_GATE_DELIVERY_TIMEOUT
from omnigent.runner.subagent_work import (
    _format_subagent_wake_notice,
    _session_inboxes_ref,
    _SubagentDeliveryAck,
    entry_owes_parent_wake,
    get_subagent_work,
    in_flight_send_lock,
)

_logger = logging.getLogger(__name__)
_SETTING = "OMNIGENT_SUBAGENT_AUTO_RETRY_ATTEMPTS"


def retry_attempt_limit() -> int:
    """The deployment setting controls new chains; existing chains retain N."""
    raw = os.environ.get(_SETTING)
    if raw is None:
        return 3
    try:
        value = int(raw)
        if value >= 0:
            return value
    except ValueError:
        pass
    _logger.warning("Invalid %s; using 3", _SETTING)
    return 3


@dataclass
class _ScheduledChain:
    chain: RetryChain
    output: str | None
    work_id: str | None = None
    timer: asyncio.TimerHandle | None = None
    accepted: asyncio.Event | None = None
    awaiting_input_id: str | None = None


class SubagentRetryRuntime:
    """One scheduler with no database access or network-held state transaction."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.time,
        limit: int | None = None,
    ) -> None:
        self.client = client
        self.clock = clock
        self.limit = retry_attempt_limit() if limit is None else limit
        if type(self.limit) is not int or self.limit < 0:
            raise ValueError("Retry limit must be a nonnegative integer")
        self.chains: dict[str, _ScheduledChain] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.failures: dict[str, str] = {}
        self.closed = False

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        response = await self.client.post(path, json=body, timeout=5.0)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _path(chain: RetryChain, action: str) -> str:
        return f"/v1/sessions/{chain.parent_id}/retry-chains/{chain.chain_id}/{action}"

    async def _control(self, chain: RetryChain, action: str, **values: Any) -> Any:
        return await self._post(
            self._path(chain, action),
            {
                "owner": chain.owner.model_dump(),
                "expected_version": chain.version,
                **values,
            },
        )

    def _remember(self, chain: RetryChain, output: str | None = None) -> _ScheduledChain:
        state = self.chains.get(chain.chain_id)
        if state is None:
            state = _ScheduledChain(chain, output)
            self.chains[chain.chain_id] = state
        elif chain.version >= state.chain.version:
            state.chain = chain
            if output is not None:
                state.output = output
        return state

    def _arm(self, state: _ScheduledChain) -> None:
        chain = state.chain
        if self.closed or chain.phase != "waiting" or chain.retry_closed or chain.due_at is None:
            if state.timer is not None:
                state.timer.cancel()
                state.timer = None
            return
        if state.timer is not None and not state.timer.cancelled():
            return

        def fire() -> None:
            state.timer = None
            task = asyncio.create_task(self._run(chain.chain_id))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        state.timer = asyncio.get_running_loop().call_later(
            max(0, chain.due_at - self.clock()), fire
        )

    async def accept_terminal(
        self,
        terminal: TerminalEnvelope,
        output: str | None,
        *,
        parent_harness: str | None = None,
    ) -> _SubagentDeliveryAck | None:
        """Return held only after a durable receipt; None keeps legacy delivery."""
        entry = get_subagent_work(terminal.child_id)
        if (
            self.closed
            or entry is None
            or terminal.parent_id != entry.parent_session_id
            or entry.parent_session_id not in _session_inboxes_ref
            or not entry_owes_parent_wake(entry, parent_harness)
            or terminal.identity_diagnostic
            or terminal.input_generation is None
            or terminal.event_kind != "turn_terminal"
        ):
            return None
        try:
            owner = InboxOwnerFence.model_validate(
                await self._post(
                    f"/v1/sessions/{entry.parent_session_id}/retry-owner",
                )
            )
            raw = await self._post(
                f"/v1/sessions/{entry.parent_session_id}/retry-terminals",
                {
                    "owner": owner.model_dump(),
                    "terminal": terminal.model_dump(mode="json"),
                    "limit": self.limit,
                    "observed_at": self.clock(),
                },
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {403, 409}:
                raise
            return _SubagentDeliveryAck(entry, False, False, "missing_parent_inbox")
        if raw is None:
            return None
        mutation = RetryMutation.model_validate(raw)
        state = self._remember(mutation.chain, output if not mutation.duplicate else None)
        if state.work_id is None and not mutation.duplicate:
            state.work_id = entry.work_id
        if (
            state.work_id == entry.work_id
            and state.chain.phase not in {"finalized", "superseded"}
            and get_subagent_work(terminal.child_id) is entry
            and not entry.delivered
        ):
            # An admitted failed turn is past launch, even while its retry waits.
            entry.status = "running" if state.chain.phase == "running" else "waiting"
            entry.terminal = state.chain.latest_terminal
            entry.output = state.output
        if state.accepted is not None and terminal.input_id == state.awaiting_input_id:
            state.accepted.set()
        self._arm(state)
        if state.chain.phase == "terminal":
            await self.finalize(
                state,
                "exhausted"
                if state.chain.latest_terminal.status == "failed"
                else state.chain.latest_terminal.status,
            )
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=False,
            delivered_now=False,
            reason="superseded" if state.chain.phase == "superseded" else "held",
            chain_id=state.chain.chain_id,
        )

    async def _read(self, state: _ScheduledChain) -> RetryChain:
        chain = RetryChain.model_validate(await self._control(state.chain, "read"))
        self._remember(chain)
        return state.chain

    async def _run(self, chain_id: str) -> None:
        state = self.chains[chain_id]
        lock = in_flight_send_lock(state.chain.child_id)
        try:
            async with lock:
                chain = await self._read(state)
                if self.closed or chain.phase != "waiting" or chain.retry_closed:
                    return
                if chain.due_at is None or self.clock() < chain.due_at:
                    self._arm(state)
                    return
                checked = await self._control(chain, "check")
                if not checked["ready"]:
                    await self.finalize(state, checked["reason"])
                    return
                prepared = await self._control(chain, "prepare")
                if prepared is None:
                    await self._read(state)
                    return
                state.accepted = asyncio.Event()
                receipt = prepared
                input_id = receipt["input_id"]
                state.awaiting_input_id = input_id
                path = f"/v1/sessions/{chain.child_id}/retry-inputs/{input_id}"
                body = {"owner": chain.owner.model_dump()}
                try:
                    response = await self.client.post(
                        path + "/dispatch", json=body, timeout=_ASK_GATE_DELIVERY_TIMEOUT
                    )
                    response.raise_for_status()
                    resolution = response.json()
                except httpx.HTTPError:
                    resolution = await self._post(path + "/lookup", body)
            if resolution["state"] == "pending":
                # A terminal can prove acceptance before the POST response.
                # Otherwise the existing policy/launch budget bounds this ID.
                deadline = resolution.get("admission_deadline")
                if not isinstance(deadline, (float, int)):
                    self.failures[chain_id] = "retry_state_unavailable"
                    return
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        state.accepted.wait(),
                        timeout=max(0, deadline - self.clock()),
                    )
                resolution = await self._post(path + "/lookup", body)
                if resolution["state"] == "pending" and resolution.get("receipt"):
                    resolution = await self._post(
                        path + "/lookup",
                        {
                            **body,
                            "expected_version": resolution["receipt"]["version"],
                            "reason": {"code": "acceptance_unknown"},
                        },
                    )
            current = await self._read(state)
            if current.phase in {"finalized", "superseded"}:
                return
            if resolution["state"] == "accepted":
                self._arm(state)
                return
            detail = resolution.get("receipt") or {}
            reason = (
                (detail.get("reason") or {}).get("code")
                or resolution.get("reason")
                or "acceptance_unknown"
            )
            await self.finalize(state, reason)
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            # Recovery reconciles this journal input; never resend another ID.
            self.failures[chain_id] = type(exc).__name__
            _logger.warning(
                "Child retry suspended; chain=%s error=%s", chain_id, type(exc).__name__
            )

    async def finalize(self, state: _ScheduledChain, reason: str) -> RetryFinal | None:
        chain = await self._read(state)
        if chain.phase == "superseded":
            self._arm(state)
            return None
        terminal = chain.latest_terminal
        status = (
            "completed"
            if terminal.status == "completed"
            else (
                "cancelled" if terminal.status in {"cancelled", "stopped", "killed"} else "failed"
            )
        )
        entry = get_subagent_work(chain.child_id)
        if entry is None:
            raise RuntimeError("Retry work entry is unavailable")
        output = state.output or (chain.latest_error or {}).get("message", "")
        if status == "failed":
            output = (
                f"Automatic retry stopped ({reason}) after "
                f"{chain.accepted_attempts}/{chain.limit} attempts.\n{output}"
            )
        notice = _format_subagent_wake_notice(
            agent=entry.agent,
            title=entry.title,
            status=status,
            pending=1,
            placement_label=entry.placement_label,
        )
        raw = await self._control(
            chain,
            "finalize",
            outcome={
                "status": status,
                "reason": reason,
                "output": output,
                "error": chain.latest_error,
            },
            wake_payload={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": notice}],
                },
            },
        )
        await self._read(state)
        self._arm(state)
        return RetryFinal.model_validate(raw) if raw is not None else None

    async def close(self) -> None:
        self.closed = True
        for state in self.chains.values():
            if state.timer is not None:
                state.timer.cancel()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
