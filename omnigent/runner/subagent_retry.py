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

from omnigent.entities.subagent_retry import (
    InboxOwnerFence,
    RetryChain,
    RetryControl,
    RetryFinal,
    RetryMutation,
    retry_identity,
)
from omnigent.entities.subagent_terminal import TerminalEnvelope
from omnigent.process_logging import data_dir
from omnigent.runner.policy_proxy import _ASK_GATE_DELIVERY_TIMEOUT
from omnigent.runner.subagent_retry_spool import RetryFinalIntent, RetryFinalSpool
from omnigent.runner.subagent_work import (
    _deliver_subagent_completion,
    _fetch_latest_assistant_item,
    _format_subagent_wake_notice,
    _held_successions,
    _session_inboxes_ref,
    _SubagentDeliveryAck,
    _SubagentRecoveryReadError,
    _SubagentWorkEntry,
    entry_owes_parent_wake,
    get_subagent_work,
    in_flight_send_lock,
    register_subagent_work,
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
    stopped_reason: str | None = None
    agent: str = "sub-agent"
    title: str = ""


class SubagentRetryRuntime:
    """One scheduler with no database access or network-held state transaction."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.time,
        limit: int | None = None,
        spool: RetryFinalSpool | None = None,
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
        self.spool = spool or RetryFinalSpool(
            data_dir()
            / "run"
            / "subagent-retry"
            / (retry_identity("retry-server", str(client.base_url)) + ".sqlite3")
        )
        self.queued_results: set[str] = set()
        self.drained_chains: set[str] = set()
        self.wake_tasks: dict[str, asyncio.Task[None]] = {}
        self.recovery_locks: dict[str, asyncio.Lock] = {}
        self.on_delivered: Callable[[_SubagentWorkEntry], None] | None = None

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
        if (
            self.closed
            or state.stopped_reason
            or chain.phase != "waiting"
            or chain.retry_closed
            or chain.due_at is None
        ):
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
        if terminal.recovered or entry.recovered:
            terminal = terminal.model_copy(update={"recovered": True})
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
                await self._spool_unacknowledged(terminal, output, entry)
                raise
            return _SubagentDeliveryAck(entry, False, False, "missing_parent_inbox")
        except httpx.HTTPError:
            await self._spool_unacknowledged(terminal, output, entry)
            raise
        if raw is None:
            return None
        mutation = RetryMutation.model_validate(raw)
        known = mutation.chain.chain_id in self.chains
        if mutation.duplicate and not known and not terminal.recovered:
            terminal = terminal.model_copy(update={"recovered": True})
            raw = await self._post(
                f"/v1/sessions/{entry.parent_session_id}/retry-terminals",
                {
                    "owner": owner.model_dump(mode="json"),
                    "terminal": terminal.model_dump(mode="json"),
                    "limit": self.limit,
                    "observed_at": self.clock(),
                },
            )
            if raw is None:
                return None
            mutation = RetryMutation.model_validate(raw)
        matching = terminal.identity_key == mutation.chain.latest_terminal.identity_key
        state = self._remember(
            mutation.chain, output if matching and (not mutation.duplicate or not known) else None
        )
        if state.work_id is None and (
            not mutation.duplicate
            or entry.recovered
            or entry.retry_chain_id == state.chain.chain_id
        ):
            state.work_id = entry.work_id
            state.agent, state.title = entry.agent, entry.title
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
            entry.retry_chain_id = state.chain.chain_id
        if state.accepted is not None and terminal.input_id == state.awaiting_input_id:
            state.accepted.set()
        if terminal.recovered:
            state.stopped_reason = "runner_restarted"
            self._arm(state)
            await self._spool(state, "runner_restarted")
            await self.recover_parent(state.chain.parent_id)
        if state.chain.phase == "waiting" and not state.stopped_reason:
            try:
                await asyncio.to_thread(self.spool.check)
            except OSError:
                await self.finalize(state, "retry_state_unavailable")
        self._arm(state)
        final = None
        if state.chain.phase == "terminal":
            final = await self.finalize(
                state,
                "exhausted"
                if state.chain.latest_terminal.status == "failed"
                else state.chain.latest_terminal.status,
            )
        elif state.chain.phase == "finalized":
            final = await self.deliver_final(state)
        confirmed = final is not None and (
            final.inbox_parent_id == state.chain.parent_id or final.drain_receipt is not None
        )
        return _SubagentDeliveryAck(
            entry=self._delivery_entry(state) if confirmed else entry,
            delivered=confirmed,
            delivered_now=False,
            reason="delivered"
            if confirmed
            else ("superseded" if state.chain.phase == "superseded" else "held"),
            chain_id=state.chain.chain_id,
        )

    async def _read(self, state: _ScheduledChain) -> RetryChain:
        chain = RetryChain.model_validate(await self._control(state.chain, "read"))
        self._remember(chain)
        return state.chain

    async def _run(self, chain_id: str) -> None:
        state = self.chains[chain_id]
        if state.stopped_reason:
            return
        lock = in_flight_send_lock(state.chain.child_id)
        try:
            async with lock:
                chain = await self._read(state)
                if await self.reconcile_control(state):
                    return
                if self.closed or chain.phase != "waiting" or chain.retry_closed:
                    return
                if chain.due_at is None or self.clock() < chain.due_at:
                    self._arm(state)
                    return
                try:
                    await asyncio.to_thread(self.spool.check)
                except OSError:
                    await self.finalize(state, "retry_state_unavailable")
                    return
                checked = await self._control(chain, "check")
                if not checked["ready"]:
                    await self.finalize(state, checked["reason"])
                    return
                prepared = await self._control(chain, "prepare")
                if prepared is None:
                    await self._read(state)
                    await self.reconcile_control(state)
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
            state.stopped_reason = (
                "server_unreachable"
                if isinstance(exc, httpx.HTTPError)
                else "retry_state_unavailable"
            )
            self._arm(state)
            await self._spool(state, state.stopped_reason)
            _logger.warning(
                "Child retry suspended; chain=%s error=%s", chain_id, type(exc).__name__
            )

    async def finalize(self, state: _ScheduledChain, reason: str) -> RetryFinal | None:
        if reason == "superseded" and await self.reconcile_control(state):
            return None
        state.stopped_reason = reason
        self._arm(state)
        spooled = await self._spool(state, reason)
        chain = await self._read(state)
        if chain.phase == "superseded":
            self._arm(state)
            return None
        if chain.phase == "finalized":
            return await self.deliver_final(state)
        if chain.pending_input_id:
            path = f"/v1/sessions/{chain.child_id}/retry-inputs/{chain.pending_input_id}/lookup"
            body = {"owner": chain.owner.model_dump(mode="json")}
            resolution = await self._post(path, body)
            if resolution["state"] == "pending" and resolution.get("receipt"):
                resolution = await self._post(
                    path,
                    {
                        **body,
                        "expected_version": resolution["receipt"]["version"],
                        "reason": {
                            "code": "cancelled" if reason == "cancelled" else "acceptance_unknown"
                        },
                    },
                )
            if resolution["state"] in {"pending", "unavailable"}:
                return None
            chain = await self._read(state)
        terminal = chain.latest_terminal
        status = (
            "completed"
            if terminal.status == "completed"
            else (
                "cancelled"
                if reason == "cancelled" or terminal.status in {"cancelled", "stopped", "killed"}
                else "failed"
            )
        )
        entry = self._delivery_entry(state)
        if status == "completed":
            reason = "completed"
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
                "error": chain.latest_error
                if spooled
                else {
                    **(chain.latest_error or {}),
                    "retry_diagnostics": [{"code": "spool_unavailable"}],
                },
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
        if raw is None:
            return None
        return await self.deliver_final(state)

    def _delivery_entry(self, state: _ScheduledChain) -> _SubagentWorkEntry:
        chain = state.chain
        entry = get_subagent_work(chain.child_id)
        if entry is not None and (
            entry.work_id == state.work_id or entry.retry_chain_id == chain.chain_id
        ):
            return entry
        create_entry = register_subagent_work if entry is None else _SubagentWorkEntry
        return create_entry(
            parent_session_id=chain.parent_id,
            child_session_id=chain.child_id,
            agent=state.agent,
            title=state.title,
            work_id=state.work_id or retry_identity("subagent-result", chain.chain_id),
            registered_by="recovery",
            recovered=True,
        )

    async def _spool(
        self,
        state: _ScheduledChain,
        reason: str,
        *,
        terminal: TerminalEnvelope | None = None,
        output: str | None = None,
    ) -> bool:
        chain = state.chain
        intent = RetryFinalIntent(
            chain_id=chain.chain_id,
            parent_id=chain.parent_id,
            terminal=terminal or chain.latest_terminal,
            chain=chain,
            reason=reason,
            output=state.output if terminal is None else output,
            agent=state.agent,
            title=state.title,
            work_id=state.work_id or retry_identity("subagent-result", chain.chain_id),
            limit=chain.limit,
        )
        try:
            await asyncio.to_thread(self.spool.put, intent)
            return True
        except (OSError, ValueError):
            self.failures[chain.chain_id] = "spool_unavailable"
            _logger.error("Child retry spool unavailable; chain=%s", chain.chain_id)
            return False

    async def _acknowledge_final(self, chain_id: str, notice_id: str) -> None:
        try:
            await asyncio.to_thread(self.spool.acknowledge, chain_id, notice_id)
        except OSError:
            self.failures[chain_id] = "spool_unavailable"
            _logger.error(
                "Final is journaled; local retry spool receipt pending; chain=%s", chain_id
            )

    async def _spool_unacknowledged(
        self, terminal: TerminalEnvelope, output: str | None, entry: _SubagentWorkEntry
    ) -> None:
        state = next(
            (
                state
                for state in self.chains.values()
                if state.chain.child_id == terminal.child_id
                and (
                    terminal.input_id == state.chain.pending_input_id
                    or terminal.turn_id == state.chain.root_turn_id
                    or terminal.turn_id == state.chain.expected_turn_id
                )
            ),
            None,
        )
        if state is not None:
            if terminal.identity_key == state.chain.latest_terminal.identity_key or (
                terminal.turn_id == state.chain.root_turn_id and terminal.status == "failed"
            ):
                return
            state.stopped_reason = "server_unreachable"
            self._arm(state)
            await self._spool(state, "server_unreachable", terminal=terminal, output=output)
            return
        if self.limit == 0 or terminal.status != "failed":
            return
        intent = RetryFinalIntent(
            chain_id=retry_identity("subagent-chain", terminal.child_id, terminal.turn_id or ""),
            parent_id=entry.parent_session_id,
            terminal=terminal,
            reason="server_unreachable",
            output=output,
            agent=entry.agent,
            title=entry.title,
            work_id=entry.work_id,
            limit=self.limit,
        )
        await asyncio.to_thread(self.spool.put, intent)

    async def reconcile_control(self, state: _ScheduledChain) -> bool:
        chain = state.chain
        if chain.phase in {"finalized", "superseded"}:
            return True
        raw = await self._control(chain, "control")
        control = RetryControl.model_validate(raw) if raw else None
        if control is None or control.generation <= chain.expected_input_generation:
            return False
        state.stopped_reason = "cancelled" if control.turn_id is None else "superseded"
        self._arm(state)
        if state.accepted is not None:
            state.accepted.set()
        if control.turn_id is None:
            await self.finalize(state, "cancelled")
        elif control.input_id:
            raw = await self._control(chain, "supersede", input_id=control.input_id)
            if raw is not None:
                self._remember(RetryChain.model_validate(raw))
                entry = get_subagent_work(chain.child_id)
                if (
                    entry is not None
                    and entry.work_id == state.work_id
                    and entry.retry_chain_id == chain.chain_id
                ):
                    entry.retry_chain_id, entry.retry_owner, entry.retry_final = None, None, None
                    entry.status, entry.terminal, entry.output = "running", None, None
        return True

    async def deliver_final(self, state: _ScheduledChain) -> RetryFinal | None:
        chain = state.chain
        raw = await self._control(chain, "final")
        if raw is None:
            return None
        final = RetryFinal.model_validate(raw)
        await self._acknowledge_final(chain.chain_id, final.notice_id)
        if final.drain_receipt:
            self.drained_chains.add(chain.chain_id)
            return final
        if chain.parent_id not in _session_inboxes_ref:
            return final
        inbox = _session_inboxes_ref[chain.parent_id]
        for _ in range(inbox.qsize()):
            payload = inbox.get_nowait()
            reference = payload.get("retry_final")
            if isinstance(reference, dict) and reference.get("result_id") == final.result_id:
                reference["owner"] = chain.owner.model_dump(mode="json")
                self.queued_results.add(final.result_id)
            inbox.put_nowait(payload)
        for payload in _held_successions.get(chain.parent_id, []):
            reference = payload.get("retry_final")
            if isinstance(reference, dict) and reference.get("result_id") == final.result_id:
                reference["owner"] = chain.owner.model_dump(mode="json")
                self.queued_results.add(final.result_id)
        if chain.parent_id in _held_successions:
            return final
        entry = self._delivery_entry(state)
        entry.parent_session_id = chain.parent_id
        entry.retry_chain_id, entry.retry_owner, entry.retry_final = (
            chain.chain_id,
            chain.owner,
            final,
        )
        entry.status, entry.output, entry.completed_at = (
            final.outcome.status,
            final.outcome.output,
            self.clock(),
        )
        entry.terminal = chain.latest_terminal
        if final.result_id not in self.queued_results:
            ack = _deliver_subagent_completion(entry, result_key=final.result_id)
            if not ack.delivered:
                return final
            self.queued_results.add(final.result_id)
        confirmed = await self._control(chain, "inbox", result_id=final.result_id)
        if confirmed and self.on_delivered is not None:
            self.on_delivered(entry)
        if confirmed and not final.wake_receipt:
            task = self.wake_tasks.get(chain.chain_id)
            if task is None or task.done():
                task = asyncio.create_task(self._dispatch_final_wake(chain))
                self.wake_tasks[chain.chain_id] = task
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            recovery = self.recovery_locks.get(chain.parent_id)
            if recovery is None or not recovery.locked():
                await asyncio.shield(task)
        raw = await self._control(chain, "final")
        return RetryFinal.model_validate(raw) if raw is not None else final

    async def _dispatch_final_wake(self, chain: RetryChain) -> None:
        recovery = self.recovery_locks.get(chain.parent_id)
        if recovery is not None:
            # Session initialization can re-enter recovery during this POST.
            async with recovery:
                pass
        if self.closed:
            return
        try:
            raw = await self._control(chain, "final")
            if raw is None:
                return
            final = RetryFinal.model_validate(raw)
            if final.wake_receipt or final.drain_receipt:
                return
            wake = await self._control(chain, "wake")
            if wake.get("state") != "accepted":
                self.failures[chain.chain_id] = str(wake.get("reason") or "wake_admission_pending")
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            self.failures[chain.chain_id] = type(exc).__name__
            _logger.warning(
                "Child final wake remains pending; chain=%s error=%s",
                chain.chain_id,
                type(exc).__name__,
            )

    async def observe_control(self, child_id: str) -> None:
        for state in tuple(self.chains.values()):
            if state.chain.child_id != child_id or state.chain.phase in {
                "finalized",
                "superseded",
            }:
                continue
            await self._read(state)
            await self.reconcile_control(state)

    async def recover_parent(self, parent_id: str) -> bool:
        return await self._recover_parent_under_lock(parent_id)

    async def _recover_parent_under_lock(self, parent_id: str) -> bool:
        """Reconcile unfinished phases without reconstructing a retry timer."""
        if self.closed or parent_id not in _session_inboxes_ref:
            return False
        lock = self.recovery_locks.setdefault(parent_id, asyncio.Lock())
        async with lock:
            try:
                owner = InboxOwnerFence.model_validate(
                    await self._post(f"/v1/sessions/{parent_id}/retry-owner")
                )
                recovery_reason = "runner_restarted"
                try:
                    intents = {
                        item.chain_id: item
                        for item in await asyncio.to_thread(self.spool.pending, parent_id)
                    }
                except (OSError, ValueError):
                    self.failures[parent_id] = "spool_unavailable"
                    _logger.error(
                        "Local retry candidates unavailable; shared journal recovery; parent=%s",
                        parent_id,
                    )
                    intents = {}
                    recovery_reason = "retry_state_unavailable"
                for intent in intents.values():
                    if intent.chain is None:
                        await self._post(
                            f"/v1/sessions/{parent_id}/retry-terminals",
                            {
                                "owner": owner.model_dump(mode="json"),
                                "terminal": intent.terminal.model_dump(mode="json"),
                                "limit": intent.limit,
                                "observed_at": self.clock(),
                            },
                        )
                after = None
                while True:
                    page = await self._post(
                        f"/v1/sessions/{parent_id}/retry-chains",
                        {"owner": owner.model_dump(mode="json"), "after": after},
                    )
                    for raw in page:
                        chain = RetryChain.model_validate(raw)
                        intent = intents.get(chain.chain_id)
                        state = self.chains.get(chain.chain_id)
                        if (
                            state is not None
                            and state.chain.owner == owner
                            and state.stopped_reason is None
                        ):
                            self._remember(chain)
                            if chain.phase == "finalized":
                                await self.deliver_final(state)
                            else:
                                await self.reconcile_control(state)
                            continue
                        adopted = chain.model_dump(mode="json")
                        if (
                            chain.parent_id != parent_id
                            or chain.owner != owner
                            or not chain.retry_closed
                        ):
                            action = "recover" if chain.parent_id == parent_id else "adopt"
                            adopted = await self._post(
                                f"/v1/sessions/{parent_id}/retry-chains/{chain.chain_id}/{action}",
                                {
                                    "owner": owner.model_dump(mode="json"),
                                    "expected_version": chain.version,
                                },
                            )
                        state = self._remember(
                            RetryChain.model_validate(adopted), intent.output if intent else None
                        )
                        state.stopped_reason = intent.reason if intent else recovery_reason
                        self._arm(state)
                        if intent:
                            state.work_id, state.agent, state.title = (
                                intent.work_id,
                                intent.agent,
                                intent.title,
                            )
                        entry = get_subagent_work(chain.child_id)
                        if entry is not None and entry.retry_chain_id in {None, chain.chain_id}:
                            if state.work_id is None:
                                state.work_id, state.agent, state.title = (
                                    entry.work_id,
                                    entry.agent,
                                    entry.title,
                                )
                            if entry.work_id == state.work_id:
                                entry.retry_chain_id = chain.chain_id
                        if state.chain.phase == "finalized":
                            await self.deliver_final(state)
                            continue
                        if state.chain.phase == "superseded":
                            if intent:
                                await self._acknowledge_final(chain.chain_id, intent.notice_id)
                            continue
                        await self._recover_observed_terminal(state)
                        if (
                            intent
                            and intent.terminal.identity_key
                            != state.chain.latest_terminal.identity_key
                        ):
                            mutation = await self._post(
                                f"/v1/sessions/{parent_id}/retry-terminals",
                                {
                                    "owner": owner.model_dump(mode="json"),
                                    "terminal": intent.terminal.model_copy(
                                        update={"parent_id": parent_id, "recovered": True}
                                    ).model_dump(mode="json"),
                                    "limit": state.chain.limit,
                                    "observed_at": self.clock(),
                                },
                            )
                            if mutation:
                                self._remember(RetryMutation.model_validate(mutation).chain)
                        if await self.reconcile_control(state):
                            continue
                        if state.chain.latest_terminal.status == "completed" and (
                            state.output is None
                            or intent is None
                            or intent.terminal.status != "completed"
                        ):
                            result = await _fetch_latest_assistant_item(
                                self.client,
                                chain.child_id,
                                response_id=state.chain.expected_turn_id,
                            )
                            state.output = (
                                result[1]
                                if result
                                else "[System: sub-agent completed with no output]"
                            )
                        await self.finalize(state, state.stopped_reason)
                    if not page:
                        return True
                    after = page[-1]["chain_id"]
            except (
                httpx.HTTPError,
                ValueError,
                RuntimeError,
                OSError,
                _SubagentRecoveryReadError,
            ) as exc:
                self.failures[parent_id] = type(exc).__name__
                _logger.warning(
                    "Child final recovery pending; parent=%s error=%s",
                    parent_id,
                    type(exc).__name__,
                )
                return False

    async def _recover_observed_terminal(self, state: _ScheduledChain) -> None:
        response = await self.client.get(
            f"/v1/sessions/{state.chain.child_id}",
            params={
                "include_items": "false",
                "include_liveness": "false",
                "include_live_status": "false",
                "include_usage": "false",
            },
            timeout=5,
        )
        response.raise_for_status()
        child = response.json()
        if state.work_id is None:
            state.agent = str(
                child.get("sub_agent_name") or child.get("agent_name") or "sub-agent"
            )
            state.title = str(child.get("title") or "")
        status = child.get("current_task_status")
        labels = child.get("labels")
        if status not in {
            "completed",
            "failed",
            "cancelled",
            "stopped",
            "killed",
        } or not isinstance(labels, dict):
            return
        terminal = TerminalEnvelope.from_labels(
            state.chain.child_id,
            parent_id=state.chain.parent_id,
            status=status,
            labels=labels,
            error=child.get("last_task_error"),
        )
        if (
            terminal.identity_diagnostic
            or terminal.input_generation is None
            or terminal.identity_key == state.chain.latest_terminal.identity_key
        ):
            return
        raw = await self._post(
            f"/v1/sessions/{state.chain.parent_id}/retry-terminals",
            {
                "owner": state.chain.owner.model_dump(mode="json"),
                "terminal": terminal.model_dump(mode="json"),
                "limit": state.chain.limit,
                "observed_at": self.clock(),
            },
        )
        if raw:
            self._remember(RetryMutation.model_validate(raw).chain)

    async def succeed_parent(self, old_id: str, new_id: str) -> None:
        await asyncio.to_thread(self.spool.transfer_parent, old_id, new_id)
        for state in self.chains.values():
            if state.chain.parent_id == old_id:
                state.stopped_reason = "parent_succeeded"
                self._arm(state)
        await self.recover_parent(new_id)

    async def pending_parents(self) -> set[str]:
        parents = {
            state.chain.parent_id
            for state in self.chains.values()
            if state.chain.phase != "superseded"
            and state.chain.chain_id not in self.drained_chains
        }
        try:
            parents.update(await asyncio.to_thread(self.spool.parents))
        except OSError:
            _logger.error("Retry spool unavailable; shared journal delivery remains active")
        return parents

    async def close(self) -> None:
        self.closed = True
        for state in self.chains.values():
            if state.timer is not None:
                state.timer.cancel()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
