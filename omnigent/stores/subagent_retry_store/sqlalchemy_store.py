"""Database-owned fencing and phase receipts; no timers or network effects."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationLabel,
    SqlConversationMetadata,
    current_workspace_id,
)
from omnigent.db.subagent_retry import (
    SqlRetryChain,
    SqlRetryControl,
    SqlRetryFinal,
    SqlRetryInput,
    SqlRetryOwner,
    SqlRetryTerminal,
)
from omnigent.db.utils import (
    get_or_create_conversation_engine,
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.entities.subagent_retry import (
    InboxOwnerFence,
    InputExecutionClaim,
    InputExecutionReceipt,
    RetryChain,
    RetryControl,
    RetryFinal,
    RetryInput,
    RetryInputClaim,
    RetryMutation,
    RetryOutcome,
    retry_identity,
)
from omnigent.entities.subagent_terminal import TerminalEnvelope
from omnigent.stores.subagent_retry_store import SubagentRetryStore
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, is_session_closed

_JournalRow = TypeVar("_JournalRow")


def _current(db: Session, model: type[_JournalRow], identity: tuple) -> _JournalRow | None:
    # Locking reads see current rows even under MySQL REPEATABLE READ.
    return db.get(model, identity, with_for_update=True, populate_existing=True)


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False, sort_keys=True))


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not 0 < len(value) <= 256:
        raise ValueError("Source identity must contain 1 to 256 characters")


def _clock(value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError("Observation time must be finite and nonnegative")


def _chain(row: SqlRetryChain) -> RetryChain:
    return RetryChain.model_validate(
        {
            **{
                field: getattr(row, field)
                for field in RetryChain.model_fields
                if field not in {"owner", "limit"}
            },
            "limit": row.retry_limit,
            "owner": InboxOwnerFence(
                parent_id=row.parent_id, runner_id=row.owner_runner_id, epoch=row.inbox_epoch
            ),
        }
    )


def _input(row: SqlRetryInput) -> RetryInput:
    return RetryInput.model_validate(
        {field: getattr(row, field) for field in RetryInput.model_fields}
    )


def _final(row: SqlRetryFinal) -> RetryFinal:
    return RetryFinal.model_validate(
        {
            **{
                field: getattr(row, field) for field in RetryFinal.model_fields if field != "limit"
            },
            "limit": row.retry_limit,
        }
    )


def _control(row: SqlRetryControl) -> RetryControl:
    return RetryControl.model_validate(
        {field: getattr(row, field) for field in RetryControl.model_fields}
    )


class SqlAlchemySubagentRetryStore(SubagentRetryStore):
    """Serialize on persisted session bindings, never a process-local mutex.

    All mutations lock metadata session IDs in sorted order, then journal rows.
    SQLite uses BEGIN IMMEDIATE; other backends use row locks and the repository's
    whole-transaction retry for serialization/deadlock victims.
    """

    def __init__(
        self, storage_location: str, conversation_storage_location: str | None = None
    ) -> None:
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.subagent_retry_store", immediate=True
        )
        self._read = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.subagent_retry_store"
        )
        conv_engine = (
            get_or_create_conversation_engine(conversation_storage_location)
            if conversation_storage_location and conversation_storage_location != storage_location
            else engine
        )
        self._conversation_session = make_named_managed_session_maker(
            conv_engine, query_name_prefix="omnigent.subagent_retry_store", immediate=True
        )

    def _execution_write(
        self,
        child_id: str,
        write: Callable[[Session], InputExecutionReceipt],
        *,
        parent_id: str | None = None,
    ) -> InputExecutionReceipt:
        """Hold AP ancestor locks through the metadata admission commit.

        A split database does not share a transaction. Holding its AP locks
        until the journal commits still orders admission before retirement;
        same-engine mode uses one transaction to avoid a nested SQLite writer.
        """

        def locked(ap: Session) -> InputExecutionReceipt:
            lineage: list[tuple[str, str | None]] = []
            target: str | None = child_id
            seen: set[str] = set()
            while target is not None:
                if target in seen or len(seen) >= 256:
                    return InputExecutionReceipt(reason="invalid_ancestry")
                seen.add(target)
                row = ap.get(SqlConversation, (current_workspace_id(), target))
                if row is None:
                    return InputExecutionReceipt(reason="invalid_ancestry")
                lineage.append((row.id, row.parent_conversation_id))
                target = row.parent_conversation_id
            if parent_id is not None and lineage[0][1] != parent_id:
                return InputExecutionReceipt(reason="invalid_ancestry")
            for identity, expected_parent in reversed(lineage):
                row = _current(ap, SqlConversation, (current_workspace_id(), identity))
                if row is None or row.parent_conversation_id != expected_parent:
                    return InputExecutionReceipt(reason="invalid_ancestry")
                closed_row = _current(
                    ap, SqlConversationLabel, (current_workspace_id(), identity, CLOSED_LABEL_KEY)
                )
                closed = closed_row.value if closed_row else None
                if (
                    row.archived
                    or row.archive_close_requested_revision is not None
                    or row.archive_close_claim_token is not None
                    or is_session_closed({CLOSED_LABEL_KEY: closed} if closed else {}, row.title)
                ):
                    return InputExecutionReceipt(reason="child_retired")
            if self._conversation_session.engine is self._session.engine:
                return write(ap)
            return run_write_transaction(self._session, "admit_execution", write)

        return run_write_transaction(self._conversation_session, "lock_execution_lineage", locked)

    def admit_execution(
        self, child_id: str, claim: InputExecutionClaim, *, runner_id: str | None
    ) -> InputExecutionReceipt:
        """Called only by the authenticated receiver at its final input boundary."""

        def write(db: Session) -> InputExecutionReceipt:
            guard = claim.retry
            ids = {child_id} | ({guard.owner.parent_id} if guard else set())
            binding = self._bindings(db, ids).get(child_id)
            if binding is None or binding.runner_id != runner_id:
                return InputExecutionReceipt(reason="stale_runner")
            pending_control = db.scalar(
                select(SqlRetryInput.input_id)
                .where(
                    SqlRetryInput.workspace_id == current_workspace_id(),
                    SqlRetryInput.target_id == child_id,
                    SqlRetryInput.kind == "control",
                    SqlRetryInput.state == "prepared",
                )
                .with_for_update()
            )
            prior_receipt = _current(db, SqlRetryInput, (current_workspace_id(), claim.input_id))
            if pending_control is not None and (
                (prior_receipt is None or prior_receipt.state != "accepted")
                and claim.boundary not in {"native_accept", "native_refuse"}
            ):
                return InputExecutionReceipt(state="pending", reason="control_pending")
            if guard is not None:
                if guard.input_id != claim.input_id or claim.turn_id is None:
                    return InputExecutionReceipt(reason="invalid_retry_input")
                locked = self._locked_input(db, claim.input_id, guard.owner)
                if locked is None:
                    return InputExecutionReceipt(reason="stale_owner")
                receipt, chain = locked
                if (
                    receipt.target_id != child_id
                    or receipt.kind != "retry"
                    or receipt.chain_id != guard.chain_id
                    or receipt.attempt != guard.attempt
                    or chain.retry_limit != guard.of
                    or receipt.expected_input_generation != guard.expected_input_generation
                ):
                    return InputExecutionReceipt(reason="invalid_retry_input")
                if receipt.state == "accepted":
                    original_turn = (receipt.reason or {}).get(
                        "execution_turn_id", receipt.turn_id
                    )
                    if original_turn != claim.turn_id:
                        return InputExecutionReceipt(reason="input_identity_conflict")
                    return InputExecutionReceipt(
                        state="accepted", reason="accepted", receipt=_input(receipt)
                    )
                if receipt.state == "reserved":
                    if receipt.turn_id != claim.turn_id:
                        return InputExecutionReceipt(reason="input_identity_conflict")
                    if claim.boundary == "native_refuse":
                        return self._refuse_native_input(db, receipt, chain, claim)
                    if claim.boundary == "native_reserve":
                        return InputExecutionReceipt(
                            state="reserved", reason="reserved", receipt=_input(receipt)
                        )
                    if claim.boundary != "native_accept" or not claim.native_turn_id:
                        return InputExecutionReceipt(reason="native_acceptance_required")
                    original = receipt.turn_id
                    if not self._accept(db, receipt, chain, claim.native_turn_id):
                        return InputExecutionReceipt(reason="superseded")
                    receipt.reason = {
                        "execution_turn_id": original,
                        "native_turn_id": claim.native_turn_id,
                    }
                    return InputExecutionReceipt(
                        state="accepted", reason="accepted", receipt=_input(receipt)
                    )
                if receipt.state == "closed" and claim.boundary == "native_refuse":
                    return InputExecutionReceipt(
                        reason=(receipt.reason or {}).get("code", "input_closed"),
                        receipt=_input(receipt),
                    )
                if receipt.state == "dispatching" and claim.boundary == "native_refuse":
                    control = self._child_control(db, child_id)
                    predecessor = (guard.expected_turn_id, guard.expected_input_generation)
                    if (
                        claim.native_reason is None
                        or claim.native_turn_id is not None
                        or receipt.turn_id is not None
                        or chain.retry_closed
                        or chain.phase != "submitting"
                        or chain.pending_input_id != receipt.input_id
                        or (chain.expected_turn_id, chain.expected_input_generation) != predecessor
                        or (control.turn_id, control.generation) != predecessor
                    ):
                        return InputExecutionReceipt(reason="superseded")
                    return self._refuse_native_input(db, receipt, chain, claim)
                if claim.boundary in {"native_accept", "native_refuse"}:
                    return InputExecutionReceipt(
                        reason="execution_not_reserved", receipt=_input(receipt)
                    )
                if (
                    claim.admission_deadline is not None
                    and time.time() >= claim.admission_deadline
                ):
                    return InputExecutionReceipt(reason="input_expired")
                if (
                    receipt.state != "dispatching"
                    or chain.retry_closed
                    or (chain.expected_turn_id, chain.expected_input_generation)
                    != (guard.expected_turn_id, guard.expected_input_generation)
                ):
                    return InputExecutionReceipt(reason="superseded")
                if claim.boundary == "native_reserve":
                    control = self._child_control(db, child_id)
                    if (control.turn_id, control.generation) != (
                        guard.expected_turn_id,
                        guard.expected_input_generation,
                    ):
                        return InputExecutionReceipt(reason="superseded")
                    receipt.state, receipt.turn_id = "reserved", claim.turn_id
                    receipt.version += 1
                    return InputExecutionReceipt(
                        state="reserved", reason="reserved", receipt=_input(receipt)
                    )
                if not self._accept(db, receipt, chain, claim.turn_id):
                    return InputExecutionReceipt(reason="superseded")
                return InputExecutionReceipt(
                    state="accepted", reason="accepted", receipt=_input(receipt)
                )
            if claim.boundary != "receiver":
                return InputExecutionReceipt(reason="invalid_native_admission")
            receipt = _current(db, SqlRetryInput, (current_workspace_id(), claim.input_id))
            if receipt is not None:
                original_turn = (receipt.reason or {}).get("execution_turn_id", receipt.turn_id)
                if (
                    receipt.target_id != child_id
                    or receipt.kind not in {"human", "control"}
                    or original_turn != claim.turn_id
                ):
                    return InputExecutionReceipt(reason="input_identity_conflict")
                if receipt.state != "accepted":
                    return InputExecutionReceipt(reason="input_closed", receipt=_input(receipt))
                return InputExecutionReceipt(
                    state="accepted", reason="accepted", receipt=_input(receipt)
                )
            control = self._child_control(db, child_id)
            if claim.admission_deadline is not None and time.time() >= claim.admission_deadline:
                return InputExecutionReceipt(reason="input_expired")
            previous = (
                _current(db, SqlRetryInput, (current_workspace_id(), control.input_id))
                if control.input_id
                else None
            )
            native_floor = (previous.payload if previous else {}).get("native_floor")
            if claim.native_generation is not None:
                if (
                    native_floor
                    and native_floor.get("runner_id") == runner_id
                    and native_floor["generation"] >= claim.native_generation
                ):
                    return InputExecutionReceipt(reason="superseded")
                native_floor = {"runner_id": runner_id, "generation": claim.native_generation}
            previous_generation = control.generation
            control.generation += 1
            control.turn_id, control.input_id = claim.turn_id, claim.input_id
            receipt = SqlRetryInput(
                input_id=claim.input_id,
                chain_id=None,
                target_id=child_id,
                kind="human" if claim.turn_id is not None else "control",
                attempt=0,
                state="accepted",
                payload={"native_floor": native_floor} if native_floor else {},
                version=0,
                expected_input_generation=previous_generation,
                turn_id=claim.turn_id,
                input_generation=control.generation,
                reason=None,
            )
            db.add(receipt)
            return InputExecutionReceipt(
                state="accepted", reason="accepted", receipt=_input(receipt)
            )

        return self._execution_write(
            child_id, write, parent_id=claim.retry.owner.parent_id if claim.retry else None
        )

    def _refuse_native_input(
        self,
        db: Session,
        receipt: SqlRetryInput,
        chain: SqlRetryChain,
        claim: InputExecutionClaim,
    ) -> InputExecutionReceipt:
        reason = claim.native_reason or "launch_refused"
        detail = claim.native_detail or reason
        receipt.state, receipt.reason = "closed", {"code": reason, "detail": detail}
        receipt.turn_id = claim.turn_id
        receipt.version += 1
        chain.retry_closed, chain.phase, chain.due_at = True, "resolving", None
        chain.version += 1
        self._finalize(
            db,
            chain,
            chain.version,
            RetryOutcome(status="failed", reason=reason, output=detail),
            {
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": detail}]},
            },
        )
        return InputExecutionReceipt(reason=reason, receipt=_input(receipt))

    def resolve_execution(
        self, child_id: str, claim: InputExecutionClaim, *, runner_id: str, close: bool
    ) -> InputExecutionReceipt:
        """Close the same ID before a timed-out receiver can refuse consumption."""

        def write(db: Session) -> InputExecutionReceipt:
            binding = self._bindings(db, {child_id}).get(child_id)
            if binding is None or binding.runner_id != runner_id:
                return InputExecutionReceipt(reason="stale_runner")
            receipt = _current(db, SqlRetryInput, (current_workspace_id(), claim.input_id))
            if receipt is not None:
                if receipt.target_id != child_id:
                    return InputExecutionReceipt(reason="input_identity_conflict")
                if receipt.state in {"closed", "rejected"}:
                    return InputExecutionReceipt(
                        reason=(receipt.reason or {}).get("code", "input_closed"),
                        receipt=_input(receipt),
                    )
                if receipt.state == "reserved" and receipt.turn_id != claim.turn_id:
                    return InputExecutionReceipt(reason="input_identity_conflict")
                if receipt.state == "accepted":
                    original = (receipt.reason or {}).get("execution_turn_id", receipt.turn_id)
                    if original != claim.turn_id:
                        return InputExecutionReceipt(reason="input_identity_conflict")
                    return InputExecutionReceipt(
                        state="accepted", reason="accepted", receipt=_input(receipt)
                    )
                if not close:
                    return InputExecutionReceipt(
                        state="pending", reason="acceptance_unknown", receipt=_input(receipt)
                    )
                receipt.state, receipt.reason = "closed", {"code": "acceptance_unknown"}
                receipt.version += 1
                if receipt.chain_id:
                    chain = _current(db, SqlRetryChain, (current_workspace_id(), receipt.chain_id))
                    if chain is not None and chain.pending_input_id == receipt.input_id:
                        chain.retry_closed, chain.phase, chain.due_at = True, "resolving", None
                        chain.version += 1
            elif close:
                receipt = SqlRetryInput(
                    input_id=claim.input_id,
                    chain_id=None,
                    target_id=child_id,
                    kind="human" if claim.turn_id is not None else "control",
                    attempt=0,
                    state="closed",
                    payload={},
                    version=0,
                    expected_input_generation=None,
                    turn_id=claim.turn_id,
                    input_generation=None,
                    reason={"code": "acceptance_unknown"},
                )
                db.add(receipt)
            else:
                return InputExecutionReceipt(state="pending", reason="acceptance_unknown")
            return InputExecutionReceipt(reason="input_closed", receipt=_input(receipt))

        return run_write_transaction(self._session, "resolve_receiver_execution", write)

    def begin_control(self, child_id: str, input_id: str, *, runner_id: str | None) -> bool:
        """Reserve control ordering without claiming that the control succeeded."""

        def write(db: Session) -> bool:
            binding = self._bindings(db, {child_id}).get(child_id)
            if binding is None or binding.runner_id != runner_id:
                return False
            receipt = _current(db, SqlRetryInput, (current_workspace_id(), input_id))
            if receipt is not None:
                return (
                    receipt.target_id == child_id
                    and receipt.kind == "control"
                    and receipt.state == "prepared"
                )
            control = self._child_control(db, child_id)
            db.add(
                SqlRetryInput(
                    input_id=input_id,
                    chain_id=None,
                    target_id=child_id,
                    kind="control",
                    attempt=0,
                    state="prepared",
                    payload={},
                    version=0,
                    expected_input_generation=control.generation,
                    turn_id=None,
                    input_generation=None,
                    reason=None,
                )
            )
            return True

        return run_write_transaction(self._session, "reserve_control_order", write)

    def finish_control(self, child_id: str, input_id: str, *, accepted: bool) -> bool:
        def write(db: Session) -> bool:
            binding = self._bindings(db, {child_id}).get(child_id)
            receipt = _current(db, SqlRetryInput, (current_workspace_id(), input_id))
            if receipt is None or receipt.target_id != child_id or receipt.kind != "control":
                return False
            if receipt.state != "prepared":
                return receipt.state == "accepted"
            if not accepted or binding is None:
                receipt.state, receipt.reason = "rejected", {"code": "control_refused"}
                receipt.version += 1
                return False
            # The reservation orders a control on the session, including a
            # receiver rebind while its transport is in flight.
            control = self._child_control(db, child_id)
            previous = (
                _current(db, SqlRetryInput, (current_workspace_id(), control.input_id))
                if control.input_id
                else None
            )
            floor = (previous.payload if previous else {}).get("native_floor")
            control.generation += 1
            control.turn_id, control.input_id = None, input_id
            receipt.state, receipt.input_generation = "accepted", control.generation
            receipt.payload = {"native_floor": floor} if floor else {}
            receipt.version += 1
            return True

        return run_write_transaction(self._session, "settle_control_order", write)

    def list_controls(self, child_id: str) -> list[RetryInput]:
        with self._read("list_unresolved_controls") as db:
            return [
                _input(row)
                for row in db.scalars(
                    select(SqlRetryInput).where(
                        SqlRetryInput.workspace_id == current_workspace_id(),
                        SqlRetryInput.target_id == child_id,
                        SqlRetryInput.kind == "control",
                        SqlRetryInput.state == "prepared",
                    )
                )
            ]

    def retire_control(self, child_id: str, input_id: str, *, expected_version: int) -> bool:
        """Unknown delivery stays unknown; recovery first fences every old retry."""

        def write(db: Session) -> bool:
            if child_id not in self._bindings(db, {child_id}):
                return False
            receipt = _current(db, SqlRetryInput, (current_workspace_id(), input_id))
            if receipt is None or receipt.target_id != child_id or receipt.kind != "control":
                return False
            if receipt.state != "prepared":
                return receipt.state == "closed"
            active = db.scalar(
                select(SqlRetryChain.chain_id)
                .where(
                    SqlRetryChain.workspace_id == current_workspace_id(),
                    SqlRetryChain.child_id == child_id,
                    SqlRetryChain.phase.not_in(("terminal", "finalized", "superseded")),
                )
                .with_for_update()
            )
            if active is not None or receipt.version != expected_version:
                return False
            receipt.state = "closed"
            receipt.reason = {"code": "control_outcome_unknown"}
            receipt.version += 1
            control = self._child_control(db, child_id)
            if control.generation == receipt.expected_input_generation:
                previous = (
                    _current(db, SqlRetryInput, (current_workspace_id(), control.input_id))
                    if control.input_id
                    else None
                )
                floor = (previous.payload if previous else {}).get("native_floor")
                control.generation += 1
                control.turn_id, control.input_id = None, input_id
                receipt.input_generation = control.generation
                receipt.payload = {"native_floor": floor} if floor else {}
            return True

        return run_write_transaction(self._session, "retire_reconciled_control", write)

    def bind_native_source(
        self, child_id: str, input_id: str, turn_id: str, source_id: str
    ) -> bool:
        """An accepted input has one durable transcript source across retries."""
        _identifier(source_id)

        def write(db: Session) -> bool:
            receipt = _current(db, SqlRetryInput, (current_workspace_id(), input_id))
            if (
                receipt is None
                or receipt.target_id != child_id
                or receipt.kind != "retry"
                or receipt.state != "accepted"
                or receipt.turn_id != turn_id
                or (receipt.reason or {}).get("native_turn_id") != turn_id
            ):
                return False
            bound = receipt.payload.get("native_source_id")
            if bound is not None:
                return bound == source_id
            receipt.payload = {**receipt.payload, "native_source_id": source_id}
            receipt.version += 1
            return True

        return run_write_transaction(self._session, "bind_native_source", write)

    def bind_native_turn(
        self,
        child_id: str,
        *,
        input_id: str,
        turn_id: str,
        runner_id: str,
        native_generation: int | None = None,
    ) -> InputExecutionReceipt:
        """Join an existing receiver admission to its real native turn once."""
        _identifier(turn_id)

        def write(db: Session) -> InputExecutionReceipt:
            binding = self._bindings(db, {child_id}).get(child_id)
            if binding is None or binding.runner_id != runner_id:
                return InputExecutionReceipt(reason="stale_runner")
            receipt = _current(db, SqlRetryInput, (current_workspace_id(), input_id))
            if (
                receipt is None
                or receipt.target_id != child_id
                or receipt.state not in {"reserved", "accepted"}
            ):
                return InputExecutionReceipt(reason="execution_not_admitted")
            original = receipt.turn_id
            if receipt.state == "reserved":
                chain = _current(db, SqlRetryChain, (current_workspace_id(), receipt.chain_id))
                original = receipt.turn_id
                if chain is None or not self._accept(db, receipt, chain, turn_id):
                    return InputExecutionReceipt(reason="superseded")
                receipt.reason = {"execution_turn_id": original, "native_turn_id": turn_id}
            if receipt.turn_id != turn_id and (
                receipt.kind == "control" or (receipt.reason or {}).get("native_turn_id")
            ):
                return InputExecutionReceipt(reason="input_identity_conflict")
            if receipt.turn_id != turn_id:
                receipt.reason = {"execution_turn_id": original, "native_turn_id": turn_id}
                receipt.turn_id = turn_id
                receipt.version += 1
            control = self._child_control(db, child_id)
            current_input = (
                _current(db, SqlRetryInput, (current_workspace_id(), control.input_id))
                if control.input_id
                else None
            )
            floor = (current_input.payload if current_input else {}).get("native_floor")
            if (
                native_generation is not None
                and current_input is not None
                and (
                    not floor
                    or floor.get("runner_id") != runner_id
                    or floor["generation"] < native_generation
                )
            ):
                current_input.payload = {
                    **current_input.payload,
                    "native_floor": {"runner_id": runner_id, "generation": native_generation},
                }
            if (control.input_id, control.turn_id) == (input_id, original):
                control.turn_id = turn_id
            if receipt.chain_id:
                chain = _current(db, SqlRetryChain, (current_workspace_id(), receipt.chain_id))
                if (
                    chain
                    and chain.pending_input_id == input_id
                    and chain.expected_turn_id == original
                ):
                    chain.expected_turn_id = turn_id
                    chain.version += 1
            return InputExecutionReceipt(
                state="accepted", reason="accepted", receipt=_input(receipt)
            )

        # Binding is historical evidence, including a terminal after retirement.
        return run_write_transaction(self._session, "bind_native_execution_turn", write)

    @staticmethod
    def _bindings(db: Session, ids: set[str]) -> dict[str, SqlConversationMetadata]:
        rows = db.scalars(
            select(SqlConversationMetadata)
            .where(
                SqlConversationMetadata.workspace_id == current_workspace_id(),
                SqlConversationMetadata.id.in_(sorted(ids)),
            )
            .order_by(SqlConversationMetadata.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        return {row.id: row for row in rows}

    @staticmethod
    def _owner(
        db: Session, owner: InboxOwnerFence, bindings: dict[str, SqlConversationMetadata]
    ) -> bool:
        binding = bindings.get(owner.parent_id)
        row = _current(db, SqlRetryOwner, (current_workspace_id(), owner.parent_id))
        return bool(
            binding is not None
            and binding.runner_id == owner.runner_id
            and row is not None
            and (row.runner_id, row.epoch) == (owner.runner_id, owner.epoch)
        )

    def _locked_chain(
        self,
        db: Session,
        chain_id: str,
        owner: InboxOwnerFence,
        *,
        extra_ids: set[str] | None = None,
        take_over: bool = False,
    ) -> SqlRetryChain | None:
        row = db.get(SqlRetryChain, (current_workspace_id(), chain_id))
        if row is None:
            return None
        bindings = self._bindings(
            db, {row.child_id, row.parent_id, owner.parent_id} | (extra_ids or set())
        )
        row = db.scalar(
            select(SqlRetryChain)
            .where(
                SqlRetryChain.workspace_id == current_workspace_id(),
                SqlRetryChain.chain_id == chain_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.parent_id != owner.parent_id or not self._owner(db, owner, bindings):
            return None
        if row.child_id not in bindings:
            return None
        if not take_over and (row.owner_runner_id, row.inbox_epoch) != (
            owner.runner_id,
            owner.epoch,
        ):
            return None
        return row

    def _locked_input(
        self,
        db: Session,
        input_id: str,
        owner: InboxOwnerFence,
    ) -> tuple[SqlRetryInput, SqlRetryChain] | None:
        receipt = db.get(SqlRetryInput, (current_workspace_id(), input_id))
        if receipt is None or receipt.chain_id is None:
            return None
        chain = self._locked_chain(db, receipt.chain_id, owner, extra_ids={receipt.target_id})
        if chain is None:
            return None
        receipt = _current(db, SqlRetryInput, (current_workspace_id(), input_id))
        return (receipt, chain) if receipt is not None else None

    @staticmethod
    def _child_control(db: Session, child_id: str) -> SqlRetryControl:
        row = _current(db, SqlRetryControl, (current_workspace_id(), child_id))
        if row is None:
            row = SqlRetryControl(child_id=child_id, generation=0, turn_id=None, input_id=None)
            db.add(row)
        return row

    @staticmethod
    def _terminal_source(
        db: Session,
        chain: SqlRetryChain,
        receipt: SqlRetryInput | None,
        terminal: TerminalEnvelope,
    ) -> bool:
        if (
            receipt is None
            or receipt.state != "accepted"
            or receipt.target_id != chain.child_id
            or (receipt.turn_id, receipt.input_generation)
            != (terminal.turn_id, terminal.input_generation)
        ):
            return False
        if terminal.turn_id != chain.root_turn_id:
            return receipt.kind == "retry" and receipt.chain_id == chain.chain_id
        root = _current(
            db,
            SqlRetryTerminal,
            (current_workspace_id(), chain.child_id, chain.root_turn_id, "failed"),
        )
        return bool(
            root is not None
            and root.chain_id == chain.chain_id
            and root.envelope.get("input_id") == terminal.input_id
            and root.envelope.get("input_generation") == terminal.input_generation
        )

    def claim_owner(
        self,
        parent_id: str,
        runner_id: str,
        *,
        expected_epoch: int,
    ) -> InboxOwnerFence | None:
        if type(expected_epoch) is not int or expected_epoch < 0 or not 0 < len(runner_id) <= 64:
            raise ValueError("Invalid inbox owner claim")

        def write(db: Session) -> InboxOwnerFence | None:
            binding = self._bindings(db, {parent_id}).get(parent_id)
            if binding is None or binding.runner_id != runner_id:
                return None
            row = _current(db, SqlRetryOwner, (current_workspace_id(), parent_id))
            if (row.epoch if row else 0) != expected_epoch:
                return None
            if row is None:
                row = SqlRetryOwner(parent_id=parent_id, runner_id=runner_id, epoch=1)
                db.add(row)
            else:
                row.runner_id = runner_id
                row.epoch += 1
            return InboxOwnerFence(parent_id=parent_id, runner_id=runner_id, epoch=row.epoch)

        return run_write_transaction(self._session, "claim_inbox_owner", write)

    def get_owner(self, parent_id: str) -> InboxOwnerFence | None:
        with self._read("read_inbox_owner") as db:
            row = db.get(SqlRetryOwner, (current_workspace_id(), parent_id))
            return (
                InboxOwnerFence(parent_id=parent_id, runner_id=row.runner_id, epoch=row.epoch)
                if row
                else None
            )

    def get_control(self, child_id: str) -> RetryControl | None:
        with self._read("read_input_generation") as db:
            row = db.get(SqlRetryControl, (current_workspace_id(), child_id))
            return _control(row) if row else None

    def record_input(
        self,
        child_id: str,
        *,
        input_id: str,
        turn_id: str | None,
        expected_generation: int,
        payload: dict[str, Any],
    ) -> RetryControl | None:
        """Record confirmed ordinary input/control admission, idempotently."""
        _identifier(input_id)
        if turn_id is not None:
            _identifier(turn_id)
        if type(expected_generation) is not int or expected_generation < 0:
            raise ValueError("Invalid expected input generation")
        body = _json(payload)

        def write(db: Session) -> RetryControl | None:
            if child_id not in self._bindings(db, {child_id}):
                return None
            existing = _current(db, SqlRetryInput, (current_workspace_id(), input_id))
            if existing is not None:
                if (
                    existing.chain_id is not None
                    or existing.target_id != child_id
                    or existing.turn_id != turn_id
                    or existing.payload != body
                ):
                    raise ValueError("Input identity reused for another admission")
                if existing.input_generation is None:
                    raise ValueError("Confirmed input receipt has no admitted generation")
                return RetryControl(
                    child_id=child_id,
                    generation=existing.input_generation,
                    turn_id=turn_id,
                    input_id=input_id,
                )
            row = self._child_control(db, child_id)
            if row.generation != expected_generation:
                return None
            row.generation += 1
            row.turn_id, row.input_id = turn_id, input_id
            db.add(
                SqlRetryInput(
                    input_id=input_id,
                    chain_id=None,
                    target_id=child_id,
                    kind="human" if turn_id is not None else "control",
                    attempt=0,
                    state="accepted",
                    payload=body,
                    version=0,
                    expected_input_generation=expected_generation,
                    turn_id=turn_id,
                    input_generation=row.generation,
                    reason=None,
                )
            )
            return _control(row)

        return run_write_transaction(self._session, "record_confirmed_input", write)

    def open_chain(
        self,
        terminal: TerminalEnvelope,
        *,
        owner: InboxOwnerFence,
        limit: int,
        observed_at: float,
    ) -> RetryMutation | None:
        _clock(observed_at)
        if type(limit) is not int or limit < 1:
            raise ValueError("A retry chain requires a positive limit")
        if (
            terminal.event_kind != "turn_terminal"
            or terminal.status != "failed"
            or terminal.identity_diagnostic
            or terminal.input_generation is None
        ):
            raise ValueError("A retry chain requires an identified admitted failed turn")
        if terminal.recovered or terminal.parent_id != owner.parent_id:
            return None
        assert terminal.turn_id is not None
        assert terminal.input_id is not None
        _identifier(terminal.turn_id)
        _identifier(terminal.input_id)
        chain_id = retry_identity("subagent-chain", terminal.child_id, terminal.turn_id)

        def write(db: Session) -> RetryMutation | None:
            bindings = self._bindings(db, {owner.parent_id, terminal.child_id})
            if terminal.child_id not in bindings or not self._owner(db, owner, bindings):
                return None
            seen = _current(
                db,
                SqlRetryTerminal,
                (current_workspace_id(), terminal.child_id, terminal.turn_id, terminal.status),
            )
            if seen is not None:
                existing_chain = _current(
                    db, SqlRetryChain, (current_workspace_id(), seen.chain_id)
                )
                if existing_chain is None:
                    raise ValueError("Terminal receipt has no chain")
                return RetryMutation(
                    chain=_chain(existing_chain),
                    duplicate=True,
                )
            control = self._child_control(db, terminal.child_id)
            if (control.turn_id, control.input_id, control.generation) != (
                terminal.turn_id,
                terminal.input_id,
                terminal.input_generation,
            ):
                return None
            row = SqlRetryChain(
                chain_id=chain_id,
                child_id=terminal.child_id,
                parent_id=owner.parent_id,
                owner_runner_id=owner.runner_id,
                inbox_epoch=owner.epoch,
                root_turn_id=terminal.turn_id,
                expected_turn_id=terminal.turn_id,
                expected_input_generation=terminal.input_generation,
                version=0,
                retry_limit=limit,
                accepted_attempts=0,
                phase="waiting",
                latest_error=_json(terminal.error),
                latest_terminal=terminal.model_dump(mode="json"),
                observed_failure_at=observed_at,
                due_at=observed_at + 60,
                pending_input_id=None,
                retry_closed=False,
            )
            db.add(row)
            db.add(
                SqlRetryTerminal(
                    child_id=terminal.child_id,
                    turn_id=terminal.turn_id,
                    status=terminal.status,
                    chain_id=chain_id,
                    envelope=terminal.model_dump(mode="json"),
                )
            )
            return RetryMutation(chain=_chain(row), duplicate=False)

        return run_write_transaction(self._session, "open_failed_turn_chain", write)

    def get_chain(self, chain_id: str) -> RetryChain | None:
        with self._read("read_retry_chain") as db:
            row = db.get(SqlRetryChain, (current_workspace_id(), chain_id))
            return _chain(row) if row else None

    def get_input(self, input_id: str) -> RetryInput | None:
        with self._read("read_input_receipt") as db:
            row = db.get(SqlRetryInput, (current_workspace_id(), input_id))
            return _input(row) if row else None

    def take_over(
        self,
        chain_id: str,
        *,
        owner: InboxOwnerFence,
        expected_version: int,
    ) -> RetryChain | None:
        """Fence recovery into delivery/reconciliation; never re-arm a retry."""

        def write(db: Session) -> RetryChain | None:
            row = self._locked_chain(db, chain_id, owner, take_over=True)
            if row is None or row.version != expected_version:
                return None
            row.owner_runner_id, row.inbox_epoch = owner.runner_id, owner.epoch
            row.retry_closed, row.due_at = True, None
            if row.phase not in {"finalized", "superseded"}:
                row.phase = "resolving"
            row.version += 1
            return _chain(row)

        return run_write_transaction(self._session, "recover_chain_owner", write)

    def supersede(
        self,
        chain_id: str,
        *,
        owner: InboxOwnerFence,
        expected_version: int,
        replacement_input_id: str,
    ) -> RetryChain | None:
        """Transfer the obligation only to a confirmed newer input admission."""

        def write(db: Session) -> RetryChain | None:
            chain = self._locked_chain(db, chain_id, owner)
            if chain is None or chain.version != expected_version or chain.phase == "finalized":
                return None
            receipt = _current(db, SqlRetryInput, (current_workspace_id(), replacement_input_id))
            if (
                receipt is None
                or receipt.state != "accepted"
                or receipt.target_id != chain.child_id
                or receipt.input_generation is None
                or receipt.input_generation <= chain.expected_input_generation
            ):
                return None
            chain.phase, chain.retry_closed, chain.due_at = "superseded", True, None
            chain.version += 1
            if chain.pending_input_id:
                pending = _current(
                    db, SqlRetryInput, (current_workspace_id(), chain.pending_input_id)
                )
                if pending is not None and pending.state in {
                    "prepared",
                    "dispatching",
                    "reserved",
                }:
                    pending.state, pending.reason = "closed", {"code": "superseded"}
                    pending.version += 1
            return _chain(chain)

        return run_write_transaction(self._session, "transfer_obligation_to_replacement", write)

    def prepare_attempt(
        self,
        chain_id: str,
        *,
        owner: InboxOwnerFence,
        expected_version: int,
        now: float,
    ) -> RetryInput | None:
        _clock(now)

        def write(db: Session) -> RetryInput | None:
            row = self._locked_chain(db, chain_id, owner)
            if (
                row is None
                or row.version != expected_version
                or row.retry_closed
                or row.phase != "waiting"
                or row.pending_input_id is not None
                or row.due_at is None
                or now < row.due_at
                or row.accepted_attempts >= row.retry_limit
            ):
                return None
            control = self._child_control(db, row.child_id)
            if (control.turn_id, control.generation) != (
                row.expected_turn_id,
                row.expected_input_generation,
            ):
                return None
            attempt = row.accepted_attempts + 1
            input_id = retry_identity("subagent-retry-input", chain_id, str(attempt))
            receipt = SqlRetryInput(
                input_id=input_id,
                chain_id=chain_id,
                target_id=row.child_id,
                kind="retry",
                attempt=attempt,
                state="prepared",
                version=0,
                expected_input_generation=row.expected_input_generation,
                turn_id=None,
                input_generation=None,
                reason=None,
                payload={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "retry"}],
                        "stable_id": input_id,
                        "auto_retry": {
                            "chain_id": chain_id,
                            "input_id": input_id,
                            "attempt": attempt,
                            "of": row.retry_limit,
                        },
                    },
                },
            )
            db.add(receipt)
            row.pending_input_id, row.phase, row.due_at = input_id, "submitting", None
            row.version += 1
            return _input(receipt)

        return run_write_transaction(self._session, "prepare_retry_input", write)

    def claim_input(
        self, input_id: str, *, owner: InboxOwnerFence, admission_deadline: float | None = None
    ) -> RetryInputClaim | None:
        """A unique logical dispatch claim is not proof the receiver accepted it."""
        if admission_deadline is not None:
            _clock(admission_deadline)

        def write(db: Session) -> RetryInputClaim | None:
            locked = self._locked_input(db, input_id, owner)
            if locked is None:
                return None
            receipt, chain = locked
            if receipt.state != "prepared":
                return RetryInputClaim(receipt=_input(receipt), claimed=False)
            if receipt.kind == "retry":
                control = self._child_control(db, chain.child_id)
                if (
                    chain.retry_closed
                    or chain.pending_input_id != input_id
                    or receipt.expected_input_generation != control.generation
                    or (control.turn_id, control.generation)
                    != (chain.expected_turn_id, chain.expected_input_generation)
                ):
                    return None
            else:
                final = _current(db, SqlRetryFinal, (current_workspace_id(), chain.chain_id))
                if (
                    final is None
                    or final.inbox_parent_id != chain.parent_id
                    or final.drain_receipt
                ):
                    return None
                receipt.expected_input_generation = self._child_control(
                    db, receipt.target_id
                ).generation
            receipt.state = "dispatching"
            if admission_deadline is not None:
                receipt.payload = {**receipt.payload, "admission_deadline": admission_deadline}
            receipt.version += 1
            return RetryInputClaim(receipt=_input(receipt), claimed=True)

        return run_write_transaction(self._session, "claim_logical_input_dispatch", write)

    def set_admission_deadline(
        self, input_id: str, *, owner: InboxOwnerFence, deadline: float
    ) -> RetryInput | None:
        """Persist the receiver's narrower execution budget after policy completes."""
        _clock(deadline)

        def write(db: Session) -> RetryInput | None:
            locked = self._locked_input(db, input_id, owner)
            if locked is None:
                return None
            receipt, _chain = locked
            if receipt.state in {"dispatching", "reserved"}:
                current = receipt.payload.get("admission_deadline")
                if current is None or deadline < current:
                    receipt.payload = {**receipt.payload, "admission_deadline": deadline}
                    receipt.version += 1
            return _input(receipt)

        return run_write_transaction(self._session, "record_input_admission_deadline", write)

    def _accept(
        self,
        db: Session,
        receipt: SqlRetryInput,
        chain: SqlRetryChain,
        turn_id: str,
    ) -> bool:
        if receipt.state == "accepted":
            if receipt.turn_id != turn_id:
                raise ValueError("Input receipt already belongs to another turn")
            return True
        if receipt.state not in {"prepared", "dispatching", "reserved"}:
            return False
        if receipt.kind == "retry":
            if (
                chain.phase not in {"submitting", "resolving"}
                or chain.pending_input_id != receipt.input_id
            ):
                return False
            control = self._child_control(db, chain.child_id)
            if (control.turn_id, control.generation) != (
                chain.expected_turn_id,
                chain.expected_input_generation,
            ) or receipt.expected_input_generation != control.generation:
                return False
            previous = (
                _current(db, SqlRetryInput, (current_workspace_id(), control.input_id))
                if control.input_id
                else None
            )
            floor = (previous.payload if previous else {}).get("native_floor")
            if floor:
                receipt.payload = {**receipt.payload, "native_floor": floor}
            control.generation += 1
            control.turn_id, control.input_id = turn_id, receipt.input_id
            receipt.input_generation = control.generation
            chain.expected_turn_id, chain.expected_input_generation = turn_id, control.generation
            chain.accepted_attempts += 1
            chain.phase = "running"
            chain.version += 1
        elif receipt.kind == "wake":
            final = _current(db, SqlRetryFinal, (current_workspace_id(), chain.chain_id))
            target = _current(
                db, SqlConversationMetadata, (current_workspace_id(), receipt.target_id)
            )
            if final is None or final.inbox_parent_id is None or target is None:
                return False
            control = self._child_control(db, receipt.target_id)
            if receipt.expected_input_generation != control.generation:
                return False
            control.generation += 1
            control.turn_id, control.input_id = turn_id, receipt.input_id
            receipt.input_generation = control.generation
            final.wake_receipt = receipt.input_id
        else:
            return False
        receipt.turn_id, receipt.state = turn_id, "accepted"
        receipt.version += 1
        return True

    def record_acceptance(
        self,
        input_id: str,
        *,
        owner: InboxOwnerFence,
        turn_id: str,
    ) -> RetryInput | None:
        _identifier(turn_id)

        def write(db: Session) -> RetryInput | None:
            locked = self._locked_input(db, input_id, owner)
            if locked is None:
                return None
            receipt, chain = locked
            return _input(receipt) if self._accept(db, receipt, chain, turn_id) else None

        return run_write_transaction(self._session, "record_execution_acceptance", write)

    def close_input(
        self,
        input_id: str,
        *,
        owner: InboxOwnerFence,
        expected_version: int,
        reason: dict[str, Any],
    ) -> RetryInput | None:
        detail = _json(reason)

        def write(db: Session) -> RetryInput | None:
            locked = self._locked_input(db, input_id, owner)
            if locked is None:
                return None
            receipt, chain = locked
            if receipt.state in {"accepted", "closed", "rejected"}:
                return _input(receipt)
            if receipt.version != expected_version:
                return None
            receipt.state, receipt.reason = "closed", detail
            receipt.version += 1
            if receipt.kind == "retry":
                chain.retry_closed, chain.phase, chain.due_at = True, "resolving", None
                chain.version += 1
            return _input(receipt)

        return run_write_transaction(self._session, "close_uncertain_input", write)

    def record_terminal(
        self,
        chain_id: str,
        terminal: TerminalEnvelope,
        *,
        owner: InboxOwnerFence,
        observed_at: float,
    ) -> RetryMutation | None:
        _clock(observed_at)
        if (
            terminal.event_kind != "turn_terminal"
            or terminal.identity_diagnostic
            or terminal.input_generation is None
        ):
            raise ValueError("An admitted terminal requires its source identity and generation")
        assert terminal.turn_id is not None
        assert terminal.input_id is not None
        _identifier(terminal.turn_id)
        _identifier(terminal.input_id)

        def write(db: Session) -> RetryMutation | None:
            chain = self._locked_chain(db, chain_id, owner)
            if chain is None or terminal.child_id != chain.child_id:
                return None
            seen = _current(
                db,
                SqlRetryTerminal,
                (current_workspace_id(), terminal.child_id, terminal.turn_id, terminal.status),
            )
            if seen is not None:
                if seen.chain_id != chain_id:
                    return None
                return RetryMutation(chain=_chain(chain), duplicate=True)
            receipt = _current(db, SqlRetryInput, (current_workspace_id(), terminal.input_id))
            if chain.phase in {"finalized", "superseded"}:
                if not self._terminal_source(db, chain, receipt, terminal):
                    return None
                db.add(
                    SqlRetryTerminal(
                        child_id=terminal.child_id,
                        turn_id=terminal.turn_id,
                        status=terminal.status,
                        chain_id=chain_id,
                        envelope=terminal.model_dump(mode="json"),
                    )
                )
                return RetryMutation(chain=_chain(chain), duplicate=False)
            if receipt is not None and receipt.chain_id == chain_id and receipt.kind == "retry":
                if receipt.expected_input_generation is None:
                    return None
                expected_generation = (
                    receipt.input_generation
                    if receipt.state == "accepted"
                    else receipt.expected_input_generation + 1
                )
                if expected_generation != terminal.input_generation or not self._accept(
                    db, receipt, chain, terminal.turn_id
                ):
                    return None
            if not self._terminal_source(db, chain, receipt, terminal) or (
                chain.expected_turn_id,
                chain.expected_input_generation,
            ) != (terminal.turn_id, terminal.input_generation):
                return None
            control = self._child_control(db, chain.child_id)
            if (control.turn_id, control.input_id, control.generation) != (
                terminal.turn_id,
                terminal.input_id,
                terminal.input_generation,
            ):
                return None
            db.add(
                SqlRetryTerminal(
                    child_id=terminal.child_id,
                    turn_id=terminal.turn_id,
                    status=terminal.status,
                    chain_id=chain_id,
                    envelope=terminal.model_dump(mode="json"),
                )
            )
            if chain.phase not in {"finalized", "superseded"}:
                chain.latest_terminal = terminal.model_dump(mode="json")
                chain.pending_input_id = None
                chain.due_at = None
                if terminal.status == "failed":
                    chain.latest_error = _json(terminal.error)
                    chain.observed_failure_at = observed_at
                if (
                    terminal.status == "failed"
                    and not terminal.recovered
                    and not chain.retry_closed
                    and chain.accepted_attempts < chain.retry_limit
                ):
                    chain.phase, chain.due_at = "waiting", observed_at + 60
                else:
                    chain.phase = "terminal"
                    chain.retry_closed = True
                chain.version += 1
            return RetryMutation(chain=_chain(chain), duplicate=False)

        return run_write_transaction(self._session, "record_identified_terminal", write)

    def finalize(
        self,
        chain_id: str,
        *,
        owner: InboxOwnerFence,
        expected_version: int,
        outcome: RetryOutcome,
        wake_payload: dict[str, Any],
    ) -> RetryFinal | None:
        body = _json(wake_payload)

        def write(db: Session) -> RetryFinal | None:
            chain = self._locked_chain(db, chain_id, owner)
            if chain is None:
                return None
            return self._finalize(db, chain, expected_version, outcome, body)

        return run_write_transaction(self._session, "finalize_chain_and_outbox", write)

    def _finalize(
        self,
        db: Session,
        chain: SqlRetryChain,
        expected_version: int,
        outcome: RetryOutcome,
        body: dict[str, Any],
    ) -> RetryFinal | None:
        chain_id = chain.chain_id
        existing = _current(db, SqlRetryFinal, (current_workspace_id(), chain_id))
        if existing is not None:
            return _final(existing)
        if chain.version != expected_version or chain.phase == "superseded":
            return None
        control = self._child_control(db, chain.child_id)
        if (control.turn_id, control.generation) != (
            chain.expected_turn_id,
            chain.expected_input_generation,
        ) and not (
            outcome.status == "cancelled"
            and control.turn_id is None
            and control.generation > chain.expected_input_generation
        ):
            return None
        pending = (
            _current(db, SqlRetryInput, (current_workspace_id(), chain.pending_input_id))
            if chain.pending_input_id
            else None
        )
        if pending is not None and pending.state in {"prepared", "dispatching", "reserved"}:
            raise ValueError("Resolve or close pending admission before finalization")
        terminal = TerminalEnvelope.model_validate(chain.latest_terminal)
        known_success = (
            terminal.status == "completed" and terminal.turn_id == chain.expected_turn_id
        )
        if known_success != (outcome.status == "completed"):
            raise ValueError("Final outcome must preserve the recorded successful turn")
        if terminal.status in {"cancelled", "stopped", "killed"} and outcome.status != "cancelled":
            raise ValueError("Final outcome must preserve confirmed cancellation")
        result_id = retry_identity("subagent-result", chain_id)
        notice_id = retry_identity("subagent-final", chain_id)
        if body.get("type") != "message" or not isinstance(body.get("data"), dict):
            raise ValueError("Final wake must be a visible message payload")
        data = body["data"]
        if data.get("stable_id", notice_id) != notice_id:
            raise ValueError("Final wake payload has another input identity")
        data["stable_id"] = notice_id
        final_outcome = outcome.model_copy(
            update={
                "error": outcome.error if outcome.error is not None else _json(chain.latest_error)
            }
        )
        final = SqlRetryFinal(
            chain_id=chain_id,
            child_id=chain.child_id,
            parent_id=chain.parent_id,
            result_id=result_id,
            notice_id=notice_id,
            outcome=final_outcome.model_dump(mode="json"),
            attempts=chain.accepted_attempts,
            retry_limit=chain.retry_limit,
            inbox_parent_id=None,
            wake_receipt=None,
            drain_receipt=None,
        )
        db.add(final)
        db.add(
            SqlRetryInput(
                input_id=notice_id,
                chain_id=chain_id,
                target_id=chain.parent_id,
                kind="wake",
                attempt=0,
                state="prepared",
                payload=body,
                version=0,
                expected_input_generation=self._child_control(db, chain.parent_id).generation,
                turn_id=None,
                input_generation=None,
                reason=None,
            )
        )
        chain.phase, chain.retry_closed, chain.due_at = "finalized", True, None
        chain.version += 1
        return _final(final)

    def get_final(self, chain_id: str) -> RetryFinal | None:
        with self._read("read_final_delivery") as db:
            row = db.get(SqlRetryFinal, (current_workspace_id(), chain_id))
            return _final(row) if row else None

    def list_outbox(self, *, owner: InboxOwnerFence, limit: int = 100) -> list[RetryFinal]:
        if not 1 <= limit <= 1000:
            raise ValueError("Outbox limit must be 1 to 1000")
        with self._read("read_pending_final_deliveries") as db:
            binding = db.get(SqlConversationMetadata, (current_workspace_id(), owner.parent_id))
            if not self._owner(db, owner, {owner.parent_id: binding} if binding else {}):
                return []
            rows = db.scalars(
                select(SqlRetryFinal)
                .where(
                    SqlRetryFinal.workspace_id == current_workspace_id(),
                    SqlRetryFinal.parent_id == owner.parent_id,
                    SqlRetryFinal.drain_receipt.is_(None),
                )
                .order_by(SqlRetryFinal.chain_id)
                .limit(limit)
            ).all()
            return [_final(row) for row in rows]

    def record_inbox(self, chain_id: str, *, owner: InboxOwnerFence, result_id: str) -> bool:
        def write(db: Session) -> bool:
            chain = self._locked_chain(db, chain_id, owner)
            final = (
                _current(db, SqlRetryFinal, (current_workspace_id(), chain_id)) if chain else None
            )
            if final is None or final.result_id != result_id:
                return False
            if final.drain_receipt is None:
                final.inbox_parent_id = owner.parent_id
            return True

        return run_write_transaction(self._session, "record_final_inbox_delivery", write)

    def record_drain(self, chain_id: str, *, owner: InboxOwnerFence, result_id: str) -> bool:
        def write(db: Session) -> bool:
            chain = self._locked_chain(db, chain_id, owner)
            final = (
                _current(db, SqlRetryFinal, (current_workspace_id(), chain_id)) if chain else None
            )
            if (
                final is None
                or final.result_id != result_id
                or final.inbox_parent_id != owner.parent_id
            ):
                return False
            final.drain_receipt = result_id
            wake = _current(db, SqlRetryInput, (current_workspace_id(), final.notice_id))
            if wake is not None and wake.state == "prepared":
                wake.state, wake.reason = "closed", {"code": "inbox_already_drained"}
                wake.version += 1
            return True

        return run_write_transaction(self._session, "acknowledge_final_inbox_drain", write)

    def transfer_parent(
        self,
        chain_id: str,
        *,
        owner: InboxOwnerFence,
        successor: InboxOwnerFence,
        expected_version: int,
    ) -> RetryChain | None:
        def write(db: Session) -> RetryChain | None:
            chain = self._locked_chain(db, chain_id, owner, extra_ids={successor.parent_id})
            if chain is None or chain.version != expected_version:
                return None
            bindings = self._bindings(db, {successor.parent_id})
            if not self._owner(db, successor, bindings):
                return None
            chain.parent_id = successor.parent_id
            chain.owner_runner_id, chain.inbox_epoch = successor.runner_id, successor.epoch
            chain.retry_closed, chain.due_at = True, None
            if chain.phase not in {"finalized", "superseded"}:
                chain.phase = "resolving"
            chain.version += 1
            final = _current(db, SqlRetryFinal, (current_workspace_id(), chain_id))
            if final is not None:
                final.parent_id = successor.parent_id
                wake = _current(db, SqlRetryInput, (current_workspace_id(), final.notice_id))
                if wake is not None and wake.state == "prepared":
                    wake.target_id = successor.parent_id
                    wake.expected_input_generation = self._child_control(
                        db, successor.parent_id
                    ).generation
            return _chain(chain)

        return run_write_transaction(self._session, "transfer_final_delivery_destination", write)
