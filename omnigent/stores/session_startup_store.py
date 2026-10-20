"""Atomic host startup admission shared by application replicas."""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlConversationMetadata, current_workspace_id
from omnigent.db.session_startup import SqlSessionStartup, SqlStartupBudget
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.session_startup import SessionStartupStatus, StartupReceipt


@dataclass(frozen=True)
class StartupSnapshot:
    id: str
    session_id: str
    host_id: str
    input_id: str | None
    state: str
    reason: str | None
    attempt_id: str | None
    runtime_id: str | None


def _snapshot(row: SqlSessionStartup) -> StartupSnapshot:
    return StartupSnapshot(
        **{name: getattr(row, name) for name in StartupSnapshot.__dataclass_fields__}
    )


class SessionStartupStore:
    """No network effects occur while deployment then host locks are held."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_startup_store", immediate=True
        )

    def _budgets(self, db: Session, host_id: str) -> tuple[SqlStartupBudget, SqlStartupBudget]:
        deployment = db.scalar(
            select(SqlStartupBudget)
            .where(SqlStartupBudget.scope == "deployment")
            .with_for_update()
        )
        if deployment is None:
            try:
                with db.begin_nested():
                    db.add(
                        SqlStartupBudget(
                            scope="deployment", resident_limit=0, next_sequence=0, fenced=False
                        )
                    )
                    db.flush()
            except IntegrityError:
                pass  # Another replica initialized the singleton.
            deployment = db.scalar(
                select(SqlStartupBudget)
                .where(SqlStartupBudget.scope == "deployment")
                .with_for_update()
            )
            if deployment is None:
                raise RuntimeError("Startup admission budget could not be initialized")
            deployment.next_sequence = max(
                deployment.next_sequence,
                db.scalar(select(func.max(SqlSessionStartup.sequence))) or 0,
            )
        scope = f"{current_workspace_id()}:{host_id}"
        host = db.scalar(
            select(SqlStartupBudget).where(SqlStartupBudget.scope == scope).with_for_update()
        )
        if host is None:
            host = SqlStartupBudget(scope=scope, resident_limit=0, next_sequence=0, fenced=False)
            db.add(host)
        return deployment, host

    def configure(
        self,
        host_id: str,
        *,
        host_resident_limit: int,
        deployment_resident_limit: int,
        startup_limit: int = 2,
    ) -> None:
        if (
            any(
                type(value) is not int
                for value in (host_resident_limit, deployment_resident_limit, startup_limit)
            )
            or min(host_resident_limit, deployment_resident_limit) < 1
            or startup_limit not in (1, 2)
        ):
            raise ValueError("Resident limits must be positive and startup limit must be 1 or 2")

        def write(db: Session) -> None:
            deployment, host = self._budgets(db, host_id)
            deployment.resident_limit = deployment_resident_limit
            host.resident_limit = host_resident_limit
            host.startup_limit = startup_limit

        run_write_transaction(self._session, "configure_capacity", write)

    def get(self, startup_id: str) -> StartupSnapshot | None:
        with self._session("read_startup") as db:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            return _snapshot(row) if row else None

    def for_session(self, session_id: str) -> StartupSnapshot | None:
        with self._session("read_current_session_startup") as db:
            row = self._current_session_row(db, session_id)
            return _snapshot(row) if row else None

    @staticmethod
    def _current_session_row(
        db: Session, session_id: str, *, locked: bool = False
    ) -> SqlSessionStartup | None:
        query = (
            select(SqlSessionStartup)
            .where(
                SqlSessionStartup.workspace_id == current_workspace_id(),
                SqlSessionStartup.session_id == session_id,
            )
            .order_by(SqlSessionStartup.runtime_generation.desc())
            .limit(1)
        )
        return db.scalar(query.with_for_update() if locked else query)

    def presentation(self, session_id: str) -> SessionStartupStatus | None:
        with self._session("read_startup_presentation") as db:
            row = self._current_session_row(db, session_id)
            if row is None:
                return None
            position = None
            if row.state == "queued":
                position = db.scalar(
                    select(func.count())
                    .select_from(SqlSessionStartup)
                    .where(
                        SqlSessionStartup.workspace_id == current_workspace_id(),
                        SqlSessionStartup.host_id == row.host_id,
                        SqlSessionStartup.state == "queued",
                        SqlSessionStartup.sequence <= row.sequence,
                    )
                )
            return SessionStartupStatus.model_validate(
                {
                    "id": row.id,
                    "attempt_id": row.attempt_id,
                    "input_id": row.input_id,
                    "state": row.state,
                    "reason": row.reason,
                    "elapsed_seconds": max(0, now_epoch() - row.created_at),
                    "queue_position_estimate": position,
                    "can_cancel": row.state
                    in ("queued", "starting", "verifying", "uncertain", "blocked"),
                    "evidence": {
                        name: getattr(row, name)
                        for name in (
                            "runtime_ready",
                            "tools_ready",
                            "input_accepted",
                            "work_started",
                        )
                    },
                }
            )

    def enqueue(
        self,
        *,
        owner_user_id: str,
        host_id: str,
        session_id: str,
        operation_key: str,
        input_id: str | None,
        runtime_generation: int = 1,
        parent_startup_id: str | None = None,
    ) -> StartupSnapshot:
        if not 0 < len(operation_key) <= 128 or not 0 < len(owner_user_id) <= 128:
            raise ValueError("Invalid owner or operation key")

        def write(db: Session) -> StartupSnapshot:
            deployment, _ = self._budgets(db, host_id)
            scope = SqlSessionStartup.workspace_id == current_workspace_id()
            old = db.scalar(
                select(SqlSessionStartup).where(
                    scope,
                    SqlSessionStartup.owner_user_id == owner_user_id,
                    SqlSessionStartup.operation_key == operation_key,
                )
            )
            if old is not None:
                if (
                    old.host_id,
                    old.session_id,
                    old.input_id,
                    old.runtime_generation,
                    old.parent_startup_id,
                ) != (host_id, session_id, input_id, runtime_generation, parent_startup_id):
                    raise ValueError("Conflicting startup operation key")
                return _snapshot(old)
            for predicate, maximum in (
                (SqlSessionStartup.host_id == host_id, 100),
                (SqlSessionStartup.owner_user_id == owner_user_id, 500),
            ):
                count = db.scalar(
                    select(func.count())
                    .select_from(SqlSessionStartup)
                    .where(
                        scope,
                        predicate,
                        SqlSessionStartup.state.in_(
                            (
                                "queued",
                                "starting",
                                "verifying",
                                "uncertain",
                                "blocked",
                                "cancelling",
                            )
                        ),
                    )
                )
                if count is not None and count >= maximum:
                    raise ValueError("Startup queue is full")
            parent = (
                db.get(SqlSessionStartup, (current_workspace_id(), parent_startup_id))
                if parent_startup_id
                else None
            )
            if parent_startup_id and parent is None:
                raise ValueError("Startup prerequisite is missing")
            deployment.next_sequence += 1
            row = SqlSessionStartup(
                workspace_id=current_workspace_id(),
                id=uuid4().hex,
                owner_user_id=owner_user_id,
                host_id=host_id,
                session_id=session_id,
                operation_key=operation_key,
                input_id=input_id,
                runtime_generation=runtime_generation,
                parent_startup_id=parent_startup_id,
                family_id=parent.family_id if parent else session_id,
                sequence=deployment.next_sequence,
                state="queued",
                occupied=False,
                resident=False,
                cancel_requested=False,
                created_at=now_epoch(),
                updated_at=now_epoch(),
            )
            db.add(row)
            return _snapshot(row)

        return run_write_transaction(self._session, "enqueue_startup", write)

    def list_queued(self, host_id: str) -> list[StartupSnapshot]:
        with self._session("read_host_startup_queue") as db:
            rows = db.scalars(
                select(SqlSessionStartup)
                .where(
                    SqlSessionStartup.workspace_id == current_workspace_id(),
                    SqlSessionStartup.host_id == host_id,
                    SqlSessionStartup.state == "queued",
                )
                .order_by(SqlSessionStartup.sequence)
                .limit(100)
            )
            return [_snapshot(row) for row in rows]

    def reserve(
        self, host_id: str, *, eligible_ids: set[str] | None = None
    ) -> StartupSnapshot | None:
        def write(db: Session) -> StartupSnapshot | None:
            deployment, host = self._budgets(db, host_id)
            scope = (
                SqlSessionStartup.workspace_id == current_workspace_id(),
                SqlSessionStartup.host_id == host_id,
            )

            def count(*predicates) -> int:
                return (
                    db.scalar(
                        select(func.count()).select_from(SqlSessionStartup).where(*predicates)
                    )
                    or 0
                )

            if (
                host.fenced
                or deployment.resident_limit == 0
                or host.resident_limit == 0
                or count(*scope, SqlSessionStartup.occupied.is_(True)) >= host.startup_limit
                or count(*scope, SqlSessionStartup.resident.is_(True)) >= host.resident_limit
                or count(SqlSessionStartup.resident.is_(True)) >= deployment.resident_limit
            ):
                return None
            query = select(SqlSessionStartup).where(*scope, SqlSessionStartup.state == "queued")
            if eligible_ids is not None:
                query = query.where(SqlSessionStartup.id.in_(eligible_ids))
            rows = db.scalars(query.order_by(SqlSessionStartup.sequence)).all()
            eligible = []
            for row in rows:
                if row.parent_startup_id:
                    parent = db.get(
                        SqlSessionStartup, (current_workspace_id(), row.parent_startup_id)
                    )
                    if parent is None or parent.state not in (
                        "working",
                        "ready_empty",
                        "command_completed",
                    ):
                        continue
                eligible.append(row)
            if not eligible:
                return None
            family_heads = {}
            for row in eligible:
                family_heads.setdefault(row.family_id, row)
            family_ids = set(family_heads)
            if host.last_family_id:
                family_ids.add(host.last_family_id)
            family_order = dict(
                db.execute(
                    select(SqlSessionStartup.family_id, func.min(SqlSessionStartup.sequence))
                    .where(
                        SqlSessionStartup.workspace_id == current_workspace_id(),
                        SqlSessionStartup.family_id.in_(family_ids),
                    )
                    .group_by(SqlSessionStartup.family_id)
                )
                .tuples()
                .all()
            )
            ordered = sorted(family_heads, key=lambda family: family_order[family])
            last_sequence = family_order.get(host.last_family_id, 0) if host.last_family_id else 0
            next_family = next(
                (family for family in ordered if family_order[family] > last_sequence),
                ordered[0],
            )
            row = family_heads[next_family]
            row.state, row.occupied, row.resident = "starting", True, True
            row.attempt_id, row.updated_at = uuid4().hex, now_epoch()
            host.last_family_id = row.family_id
            return _snapshot(row)

        return run_write_transaction(self._session, "reserve_startup", write)

    def claim(self, startup_id: str, attempt_id: str, *, runtime_id: str) -> bool:
        def write(db: Session) -> bool:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return False
            _, host = self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if (
                host.fenced
                or row.cancel_requested
                or row.state == "blocked"
                or row.attempt_id != attempt_id
                or not row.occupied
                or row.runtime_id not in (None, runtime_id)
            ):
                return False
            if row.runtime_id is None:
                row.runtime_id, row.state, row.updated_at = runtime_id, "verifying", now_epoch()
            return True

        return run_write_transaction(self._session, "claim_startup", write)

    def claim_boundary(
        self,
        startup_id: str,
        attempt_id: str,
        *,
        runtime_id: str,
        host_generation: str,
        boundary: str,
        claim_id: str,
        expected_runner_id: str | None = None,
    ) -> bool:
        """A lost grant response is ambiguous; never grant the same physical start twice."""
        if boundary not in (
            "runner_prepare",
            "runner_spawn",
            "native_prepare",
            "executor_prepare",
        ):
            raise ValueError("Unknown cold startup boundary")
        if not all(0 < len(value) <= 128 for value in (runtime_id, host_generation, claim_id)):
            raise ValueError("Invalid cold startup identity")

        def write(db: Session) -> bool:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return False
            _, host = self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if (
                host.fenced
                or row.cancel_requested
                or row.state == "blocked"
                or row.attempt_id != attempt_id
                or not row.occupied
                or row.runtime_id not in (None, runtime_id)
                or row.host_generation not in (None, host_generation)
                or boundary in row.cold_claims
                or (boundary == "runner_spawn" and "runner_prepare" not in row.cold_claims)
            ):
                return False
            if expected_runner_id is not None:
                binding = db.get(
                    SqlConversationMetadata,
                    (current_workspace_id(), row.session_id),
                    with_for_update=True,
                )
                if (
                    binding is None
                    or binding.host_id != row.host_id
                    or binding.runner_id != expected_runner_id
                ):
                    return False
            row.runtime_id, row.host_generation = runtime_id, host_generation
            row.cold_claims = {**row.cold_claims, boundary: claim_id}
            row.state, row.updated_at = "verifying", now_epoch()
            return True

        return run_write_transaction(self._session, "grant_cold_start_boundary", write)

    def cancel(self, startup_id: str) -> StartupSnapshot | None:
        return self._cancel(startup_id, expected_attempt_id=None, compare=False)

    def cancel_current(
        self, startup_id: str, *, expected_attempt_id: str | None
    ) -> StartupSnapshot | None:
        return self._cancel(startup_id, expected_attempt_id=expected_attempt_id, compare=True)

    def _cancel(
        self, startup_id: str, *, expected_attempt_id: str | None, compare: bool
    ) -> StartupSnapshot | None:
        def write(db: Session) -> StartupSnapshot | None:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return None
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if compare and row.attempt_id != expected_attempt_id:
                return None
            if compare:
                current = self._current_session_row(db, row.session_id, locked=True)
                if (
                    current is None
                    or current.id != row.id
                    or row.state
                    not in (
                        "queued",
                        "starting",
                        "verifying",
                        "uncertain",
                        "blocked",
                        "cancelling",
                    )
                ):
                    return None
            row.cancel_requested = True
            row.state = "cancelling" if row.occupied else "cancelled"
            row.updated_at = now_epoch()
            return _snapshot(row)

        return run_write_transaction(self._session, "cancel_startup", write)

    def confirm_stopped(self, startup_id: str, attempt_id: str) -> bool:
        """The authenticated runtime owner supplies exact-attempt stop evidence."""

        def write(db: Session) -> bool:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return False
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if row.attempt_id != attempt_id:
                return False
            row.occupied = row.resident = False
            row.state = "cancelled" if row.cancel_requested else "stopped"
            row.updated_at = now_epoch()
            return True

        return run_write_transaction(self._session, "settle_stopped_startup", write)

    def cancel_unissued(
        self, startup_id: str, attempt_id: str | None, *, reason: str | None = None
    ) -> bool:
        """Fence a callback that has not issued; retain any already claimed runtime."""

        def write(db: Session) -> bool:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return False
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if row.attempt_id != attempt_id:
                return False
            row.cancel_requested = True
            if row.runtime_id is None:
                row.occupied = row.resident = False
                row.state = "blocked" if reason else "cancelled"
                row.reason = reason
            else:
                row.state = "cancelling"
            row.updated_at = now_epoch()
            return True

        return run_write_transaction(self._session, "cancel_unissued_startup", write)

    def bind_dispatch(
        self,
        startup_id: str,
        attempt_id: str,
        *,
        runtime_id: str,
        source: str,
        vendor_session_id: str,
        vendor_turn_id: str,
    ) -> bool:
        """Bind vendor identities from the owning adapter before accepting its proof."""
        if not (
            0 < len(source) <= 64
            and 0 < len(vendor_session_id) <= 256
            and 0 < len(vendor_turn_id) <= 256
        ):
            raise ValueError("Invalid startup dispatch identity")

        def write(db: Session) -> bool:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return False
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if (
                not row.occupied
                or row.cancel_requested
                or row.state == "blocked"
                or row.attempt_id != attempt_id
                or row.runtime_id != runtime_id
                or row.input_id is None
            ):
                return False
            identity = (source, vendor_session_id, vendor_turn_id)
            if row.proof_source is not None:
                return (row.proof_source, row.vendor_session_id, row.vendor_turn_id) == identity
            row.proof_source, row.vendor_session_id, row.vendor_turn_id = identity
            row.updated_at = now_epoch()
            return True

        return run_write_transaction(self._session, "bind_startup_dispatch", write)

    def observe(self, receipt: StartupReceipt) -> bool:
        """The caller must authenticate the runner/session before supplying a receipt."""

        def write(db: Session) -> bool:
            row = db.get(SqlSessionStartup, (current_workspace_id(), receipt.startup_id))
            if row is None:
                return False
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if (
                row.attempt_id != receipt.attempt_id
                or row.runtime_id != receipt.runtime_id
                or row.input_id != receipt.input_id
            ):
                return False
            if receipt.phase == "stopped":
                row.occupied = row.resident = False
                row.state = "cancelled" if row.cancel_requested else "stopped"
                row.updated_at = now_epoch()
                return True
            if row.cancel_requested:
                return False
            if receipt.phase in ("input_accepted", "work_started"):
                if row.proof_source is None or (
                    row.proof_source,
                    row.vendor_session_id,
                    row.vendor_turn_id,
                ) != (receipt.source, receipt.vendor_session_id, receipt.vendor_turn_id):
                    return False
            elif receipt.source not in ("runner", row.proof_source):
                return False
            if not row.occupied:
                return row.state in ("working", "ready_empty") and receipt.phase in (
                    "runtime_ready",
                    "tools_ready",
                    "input_accepted",
                    "work_started",
                )
            if receipt.phase in ("blocked", "uncertain") and row.state != "blocked":
                row.state, row.reason = receipt.phase, receipt.reason
            elif receipt.phase not in ("blocked", "uncertain"):
                setattr(row, receipt.phase, True)
                if row.state != "blocked" and row.runtime_ready and row.tools_ready:
                    if row.input_id is None:
                        row.state, row.occupied, row.reason = "ready_empty", False, None
                    elif row.input_accepted and row.work_started:
                        row.state, row.occupied, row.reason = "working", False, None
            row.updated_at = now_epoch()
            return True

        return run_write_transaction(self._session, "observe_startup_receipt", write)

    def mark_uncertain(self, startup_id: str, attempt_id: str | None, *, reason: str) -> bool:
        """Losing a control-call response never frees a possibly live startup."""

        def write(db: Session) -> bool:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return False
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if (
                row.attempt_id != attempt_id
                or not row.occupied
                or row.cancel_requested
                or row.state == "blocked"
            ):
                return False
            row.state, row.reason, row.updated_at = "uncertain", reason, now_epoch()
            return True

        return run_write_transaction(self._session, "retain_uncertain_startup", write)
