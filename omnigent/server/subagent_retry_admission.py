"""Read retry preconditions and reconcile stable execution receipts.

Preflight is advisory: dispatch must still claim the journal input and perform
the runner's final admission guard. A healthy snapshot never proves execution.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from omnigent.entities.conversation import AutoRetryMetadata
from omnigent.entities.subagent_retry import (
    ControlInputReference,
    InboxOwnerFence,
    InputExecutionClaim,
    InputExecutionReceipt,
    RetryChain,
    RetryExecutionGuard,
    RetryInput,
    RetryOutcome,
)
from omnigent.entities.subagent_terminal import TerminalEnvelope
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.host_registry import HostRegistry
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.host_store import HostStore, host_is_live
from omnigent.stores.subagent_retry_store import SubagentRetryStore

_RECONCILIATION_TIMEOUT_S = 5


@dataclass
class _RetryDispatch:
    admission: SubagentRetryAdmission
    guard: RetryExecutionGuard
    claimed_version: int
    forwarded: bool = False


_dispatch: ContextVar[_RetryDispatch | None] = ContextVar("subagent_retry_dispatch", default=None)


@dataclass
class _FinalDispatch:
    admission: SubagentRetryAdmission
    owner: InboxOwnerFence
    receipt: RetryInput
    forwarded: bool = False


_final_dispatch: ContextVar[_FinalDispatch | None] = ContextVar(
    "subagent_final_dispatch", default=None
)


@contextmanager
def final_dispatch_scope(
    admission: SubagentRetryAdmission, owner: InboxOwnerFence, receipt: RetryInput
) -> Iterator[None]:
    token = _final_dispatch.set(_FinalDispatch(admission, owner, receipt))
    try:
        yield
    finally:
        _final_dispatch.reset(token)


def active_final_dispatch() -> RetryInput | None:
    current = _final_dispatch.get()
    return current.receipt if current else None


class RetryDispatchRefused(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ControlDeliveryRefused(OmnigentError):
    """A definite receiver refusal, distinct from an ambiguous transport loss."""


@dataclass
class ControlDispatch:
    reference: ControlInputReference
    not_sent: bool = False


_control_dispatch: ContextVar[ControlDispatch | None] = ContextVar(
    "control_dispatch", default=None
)


@contextmanager
def control_dispatch_scope(dispatch: ControlDispatch | None) -> Iterator[None]:
    token = _control_dispatch.set(dispatch)
    try:
        yield
    finally:
        _control_dispatch.reset(token)


def control_transport_event(event: dict[str, Any]) -> dict[str, Any]:
    current = _control_dispatch.get()
    return (
        {**event, "input_control": current.reference.model_dump()}
        if current and event.get("type") in {"interrupt", "stop_session"}
        else event
    )


def control_not_sent() -> None:
    current = _control_dispatch.get()
    if current is not None:
        current.not_sent = True


def control_was_not_sent() -> bool:
    current = _control_dispatch.get()
    return bool(current and current.not_sent)


@contextmanager
def retry_dispatch_scope(
    admission: SubagentRetryAdmission, guard: RetryExecutionGuard, *, claimed_version: int
) -> Iterator[None]:
    token = _dispatch.set(_RetryDispatch(admission, guard, claimed_version))
    try:
        yield
    finally:
        _dispatch.reset(token)


def active_retry_dispatch() -> RetryExecutionGuard | None:
    current = _dispatch.get()
    return current.guard if current else None


def native_retry_marker(
    conversations: ConversationStore,
    session_id: str,
    input_id: object,
    turn_id: object,
    source_id: str,
    supplied_marker: object,
) -> AutoRetryMetadata:
    """Derive a native echo label from the accepted, identity-bound journal input."""
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.subagent_retry_store.sqlalchemy_store import SqlAlchemySubagentRetryStore

    if not isinstance(conversations, SqlAlchemyConversationStore) or not isinstance(input_id, str):
        raise OmnigentError("Native input receipt is unavailable", code=ErrorCode.INVALID_INPUT)
    journal = SqlAlchemySubagentRetryStore(
        conversations.storage_location, conversations.conversation_storage_location
    )
    receipt = journal.get_input(input_id)
    if receipt is None or receipt.target_id != session_id or receipt.kind != "retry":
        raise OmnigentError("Native input receipt does not match", code=ErrorCode.INVALID_INPUT)
    if receipt.state in {"prepared", "dispatching", "reserved"}:
        raise OmnigentError(
            "Native input acceptance is pending", code=ErrorCode.RUNNER_UNAVAILABLE
        )
    chain = journal.get_chain(receipt.chain_id) if receipt.chain_id else None
    if (
        receipt.state != "accepted"
        or not isinstance(turn_id, str)
        or receipt.turn_id != turn_id
        or (receipt.reason or {}).get("native_turn_id") != turn_id
        or chain is None
        or chain.child_id != session_id
        or receipt.attempt > chain.accepted_attempts
    ):
        raise OmnigentError(
            "Native input was not accepted on this turn", code=ErrorCode.INVALID_INPUT
        )
    marker = AutoRetryMetadata(
        chain_id=chain.chain_id, input_id=receipt.input_id, attempt=receipt.attempt, of=chain.limit
    )
    if supplied_marker is not None and supplied_marker != marker.model_dump(mode="json"):
        raise OmnigentError("Native retry marker changed", code=ErrorCode.INVALID_INPUT)
    if not journal.bind_native_source(session_id, input_id, turn_id, source_id):
        raise OmnigentError("Native input already has another source", code=ErrorCode.CONFLICT)
    return marker


async def claim_retry_forward(session_id: str, event: dict[str, Any], *, deadline: float) -> None:
    """Run after normal policy/routing, immediately before the sole forward."""
    final = _final_dispatch.get()
    if final is not None:
        if final.receipt.chain_id is None:
            raise RetryDispatchRefused("final_delivery_changed")
        receipt = await asyncio.to_thread(
            final.admission.journal.get_input, final.receipt.input_id
        )
        chain = await asyncio.to_thread(final.admission.journal.get_chain, final.receipt.chain_id)
        if (
            receipt is None
            or chain is None
            or receipt.target_id != session_id
            or receipt.state != "dispatching"
            or final.forwarded
            or not await asyncio.to_thread(final.admission._owns, chain, final.owner)
        ):
            raise RetryDispatchRefused("final_delivery_changed")
        final.forwarded = True
        event["input_admission_required"] = True
        event["input_id"] = receipt.input_id
        event["input_admission_deadline"] = deadline
        event["_final_wake"] = True
        return
    current = _dispatch.get()
    if current is None:
        return
    admission, guard = current.admission, current.guard
    receipt = await asyncio.to_thread(admission.journal.get_input, guard.input_id)
    chain = await asyncio.to_thread(admission.journal.get_chain, guard.chain_id)
    if receipt is None or chain is None or receipt.target_id != session_id:
        raise RetryDispatchRefused("retry_state_unavailable")
    checked = await admission.check_before_retry(chain)
    if not checked.ready:
        raise RetryDispatchRefused(checked.reason)
    if (
        receipt.state != "dispatching"
        or receipt.version != current.claimed_version
        or current.forwarded
    ):
        raise RetryDispatchRefused("input_already_claimed")
    current.forwarded = True
    deadline_receipt = await asyncio.to_thread(
        admission.journal.set_admission_deadline,
        guard.input_id,
        owner=guard.owner,
        deadline=deadline,
    )
    if deadline_receipt is None or deadline_receipt.state != "dispatching":
        raise RetryDispatchRefused("input_already_claimed")
    event["retry_guard"] = guard.model_dump(mode="json")
    event["auto_retry"] = guard.metadata.model_dump(mode="json")
    event["input_admission_required"] = True
    event["input_id"] = guard.input_id
    event["input_admission_deadline"] = deadline


class RetryCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    ready: bool = False
    reason: Literal[
        "ready",
        "host_offline",
        "child_retired",
        "superseded",
        "stale_owner",
        "chain_changed",
        "invalid_ancestry",
        "retry_state_unavailable",
        "server_unreachable",
    ]
    effective_host_id: str | None = None
    host_online: bool | None = None


class RetryInputResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: Literal[
        "accepted", "pending", "closed", "rejected", "stale_owner", "not_found", "unavailable"
    ]
    receipt: RetryInput | None = None
    reason: str | None = None
    admission_deadline: float | None = None


class RetryInputControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: InboxOwnerFence
    expected_version: int | None = None
    reason: dict[str, Any] | None = None


class RetryTerminalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: InboxOwnerFence
    terminal: TerminalEnvelope
    limit: int = Field(strict=True, ge=0)
    observed_at: float = Field(allow_inf_nan=False)


class RetryFinalizeRequest(RetryInputControl):
    outcome: RetryOutcome
    wake_payload: dict[str, Any]


class RetryChainPage(RetryInputControl):
    after: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class RetryResultReceipt(RetryInputControl):
    result_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class RetryReplacement(RetryInputControl):
    input_id: str = Field(min_length=1, max_length=256)


class SubagentRetryAdmission:
    """Server-side reads share the journal's workspace and owner fences."""

    def __init__(
        self,
        journal: SubagentRetryStore,
        conversations: ConversationStore,
        *,
        host_store: HostStore | None,
        host_registry: HostRegistry | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.journal = journal
        self.conversations = conversations
        self.host_store = host_store
        self.host_registry = host_registry
        self.clock = clock
        self.control_tasks: set[asyncio.Task[Any]] = set()

    async def admit_execution(
        self, child_id: str, claim: InputExecutionClaim, *, runner_id: str | None
    ) -> InputExecutionReceipt:
        try:
            return await asyncio.to_thread(
                self.journal.admit_execution, child_id, claim, runner_id=runner_id
            )
        except (SQLAlchemyError, OSError, ValueError, RuntimeError):
            return InputExecutionReceipt(state="pending", reason="input_admission_unavailable")

    def _owns(self, chain: RetryChain, owner: InboxOwnerFence) -> bool:
        parent = self.conversations.get_conversation(owner.parent_id)
        return bool(
            chain.owner == owner
            and chain.parent_id == owner.parent_id
            and parent is not None
            and parent.runner_id == owner.runner_id
            and self.journal.get_owner(owner.parent_id) == owner
        )

    def _check(self, expected: RetryChain) -> RetryCheck:
        chain = self.journal.get_chain(expected.chain_id)
        if chain is None:
            return RetryCheck(reason="retry_state_unavailable")
        if not self._owns(chain, expected.owner):
            return RetryCheck(reason="stale_owner")
        control = self.journal.get_control(chain.child_id)
        if control is None:
            return RetryCheck(reason="retry_state_unavailable")
        if (control.turn_id, control.generation) != (
            expected.expected_turn_id,
            expected.expected_input_generation,
        ):
            return RetryCheck(reason="superseded")
        if (
            chain != expected
            or chain.retry_closed
            or chain.phase not in {"waiting", "submitting", "resolving"}
            or chain.latest_terminal.status != "failed"
            or chain.latest_terminal.turn_id != chain.expected_turn_id
        ):
            return RetryCheck(reason="chain_changed")

        seen: set[str] = set()
        session_id: str | None = chain.child_id
        effective_host_id: str | None = None
        while session_id is not None:
            if session_id in seen or len(seen) >= 256:
                return RetryCheck(reason="invalid_ancestry")
            seen.add(session_id)
            conv = self.conversations.get_conversation(session_id)
            if conv is None:
                return RetryCheck(reason="invalid_ancestry")
            if session_id == chain.child_id and conv.parent_conversation_id != chain.parent_id:
                return RetryCheck(reason="invalid_ancestry")
            if (
                conv.archived
                or conv.archive_close_requested_revision is not None
                or conv.archive_close_claimed
            ):
                return RetryCheck(reason="child_retired")
            if effective_host_id is None and conv.host_id is not None:
                effective_host_id = conv.host_id
            session_id = conv.parent_conversation_id

        if effective_host_id is None:
            return RetryCheck(ready=True, reason="ready", host_online=True)
        if self.host_store is not None:
            host = self.host_store.get_host(effective_host_id)
            online = host is not None and host.deleted_at is None and host_is_live(host)
        elif self.host_registry is not None:
            online = self.host_registry.get(effective_host_id) is not None
        else:
            return RetryCheck(
                reason="retry_state_unavailable", effective_host_id=effective_host_id
            )
        return RetryCheck(
            ready=online,
            reason="ready" if online else "host_offline",
            effective_host_id=effective_host_id,
            host_online=online,
        )

    async def check_before_retry(self, chain: RetryChain) -> RetryCheck:
        """Bound the entire ancestry/health read, including cross-replica storage."""
        try:
            async with asyncio.timeout(_RECONCILIATION_TIMEOUT_S):
                return await asyncio.to_thread(self._check, chain)
        except TimeoutError:
            return RetryCheck(reason="server_unreachable")
        except (SQLAlchemyError, OSError, ValueError, RuntimeError):
            return RetryCheck(reason="retry_state_unavailable")

    def _resolve(
        self,
        input_id: str,
        owner: InboxOwnerFence,
        expected_version: int | None,
        reason: dict[str, Any],
    ) -> RetryInputResolution:
        receipt = self.journal.get_input(input_id)
        chain = self.journal.get_chain(receipt.chain_id) if receipt and receipt.chain_id else None
        if chain is None or receipt is None:
            return RetryInputResolution(state="not_found")
        if not self._owns(chain, owner):
            return RetryInputResolution(state="stale_owner", reason="stale_owner")
        if (
            expected_version is not None
            and reason.get("code") == "acceptance_unknown"
            and receipt.payload.get("admission_deadline", 0) > self.clock()
        ):
            expected_version = None
        if expected_version is not None and receipt.state in {
            "prepared",
            "dispatching",
            "reserved",
        }:
            closed = self.journal.close_input(
                input_id, owner=owner, expected_version=expected_version, reason=reason
            )
            if closed is None:
                # Acceptance, closure, or an owner change may have won the CAS.
                current = self.journal.get_chain(chain.chain_id)
                if current is None or not self._owns(current, owner):
                    return RetryInputResolution(state="stale_owner", reason="stale_owner")
                receipt = self.journal.get_input(input_id)
                if receipt is None:
                    return RetryInputResolution(state="not_found")
            else:
                receipt = closed
        return RetryInputResolution(
            state="pending"
            if receipt.state in {"prepared", "dispatching", "reserved"}
            else receipt.state,
            receipt=receipt,
            admission_deadline=receipt.payload.get("admission_deadline"),
        )

    async def lookup_or_close_input(
        self,
        input_id: str,
        *,
        owner: InboxOwnerFence,
        expected_version: int | None = None,
        reason: dict[str, Any] | None = None,
    ) -> RetryInputResolution:
        """Reconcile one ID; a stale version never authorizes another dispatch."""
        try:
            async with asyncio.timeout(_RECONCILIATION_TIMEOUT_S):
                return await asyncio.to_thread(
                    self._resolve,
                    input_id,
                    owner,
                    expected_version,
                    reason if reason is not None else {"code": "acceptance_unknown"},
                )
        except TimeoutError:
            return RetryInputResolution(state="unavailable", reason="server_unreachable")
        except (SQLAlchemyError, OSError, ValueError, RuntimeError):
            return RetryInputResolution(state="unavailable", reason="retry_state_unavailable")
