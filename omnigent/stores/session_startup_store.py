"""Atomic host startup admission shared by application replicas."""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import AccountAuthority, require_active_account
from omnigent.db.db_models import SqlConversationMetadata, current_workspace_id
from omnigent.db.session_startup import SqlSessionStartup, SqlStartupBudget
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.host_recovery import HostRecoveryPlan
from omnigent.entities.session_startup import SessionStartupStatus, StartupReceipt
from omnigent.errors import ErrorCode, OmnigentError


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
    runtime_generation: int
    parent_startup_id: str | None
    preparation: dict | None
    owner_user_id: str
    occupied: bool
    resident: bool
    host_generation: str | None
    input_accepted: bool
    proof_source: str | None
    admitted_at: int | None
    first_dispatch_at: int | None


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

    def register_host_generation(self, host_id: str, generation: str) -> None:
        if not 0 < len(generation) <= 128:
            raise ValueError("Invalid host startup generation")

        def write(db: Session) -> None:
            _, host = self._budgets(db, host_id)
            host.current_generation = generation
            host.work_revision = (host.work_revision or 0) + 1

        run_write_transaction(self._session, "register_host_startup_generation", write)

    def recovery(self, host_id: str) -> HostRecoveryPlan | None:
        with self._session("read_host_recovery") as db:
            host = db.get(SqlStartupBudget, f"{current_workspace_id()}:{host_id}")
            return (
                HostRecoveryPlan.model_validate(host.recovery) if host and host.recovery else None
            )

    def begin_recovery(
        self,
        host_id: str,
        *,
        expected_epoch: int,
        account_generation: str,
        host_generation: str,
        process_identity: str,
        supervisor: str,
        members: frozenset[tuple[str, str]],
        unresolved_runtimes: frozenset[tuple[str, str]],
        startup_attempts: tuple[tuple[str, str], ...],
        now: float,
    ) -> HostRecoveryPlan:
        def write(db: Session) -> HostRecoveryPlan:
            _, host = self._budgets(db, host_id)
            old = HostRecoveryPlan.model_validate(host.recovery) if host.recovery else None
            if old and old.phase not in ("complete", "cancelled"):
                if (
                    old.startup_attempts == tuple(sorted(startup_attempts))
                    and old.account_generation == account_generation
                    and old.host_generation == host_generation
                    and old.process_identity == process_identity
                    and old.supervisor == supervisor
                    and frozenset(old.members) == members
                ):
                    return old
                raise ValueError("Another recovery owns the host fence")
            if old and old.committed_at is not None and now - old.committed_at < 600:
                raise ValueError("Host recovery cooldown is active")
            if host.fenced or host.work_fence_epoch != expected_epoch:
                raise ValueError("Host recovery fence changed")
            targets = db.scalars(
                select(SqlSessionStartup)
                .where(
                    SqlSessionStartup.workspace_id == current_workspace_id(),
                    SqlSessionStartup.host_id == host_id,
                    SqlSessionStartup.occupied.is_(True),
                )
                .with_for_update()
            ).all()
            if (
                len(targets) != 2
                or {(row.id, row.attempt_id) for row in targets} != set(startup_attempts)
                or any(row.state not in ("uncertain", "blocked") for row in targets)
                or {(row.runtime_id, row.host_generation) for row in targets}
                != unresolved_runtimes
            ):
                raise ValueError("Startup incident changed")
            host.work_fence_epoch += 1
            plan = HostRecoveryPlan(
                recovery_id=uuid4().hex,
                host_id=host_id,
                account_generation=account_generation,
                epoch=host.work_fence_epoch,
                work_revision=host.work_revision,
                host_generation=host_generation,
                process_identity=process_identity,
                supervisor=supervisor,
                members=tuple(sorted(members)),
                startup_attempts=tuple(sorted(startup_attempts)),
                started_at=now,
            )
            host.fenced, host.recovery = True, plan.model_dump(mode="json")
            return plan

        return run_write_transaction(self._session, "fence_host_recovery", write)

    def note_host_work(self, host_id: str) -> None:
        def write(db: Session) -> None:
            _, host = self._budgets(db, host_id)
            host.work_revision += 1

        run_write_transaction(self._session, "observe_host_work_revision", write)

    def commit_recovery(self, plan: HostRecoveryPlan, *, now: float) -> bool:
        def write(db: Session) -> bool:
            _, host = self._budgets(db, plan.host_id)
            current = HostRecoveryPlan.model_validate(host.recovery) if host.recovery else None
            if (
                current is None
                or current != plan
                or current.phase != "quiescing"
                or not host.fenced
                or host.work_fence_epoch != plan.epoch
                or host.work_revision != plan.work_revision
            ):
                return False
            host.recovery = plan.model_copy(
                update={"phase": "restart_committed", "committed_at": now}
            ).model_dump(mode="json")
            return True

        return run_write_transaction(self._session, "commit_exact_host_recovery", write)

    def cancel_recovery(self, plan: HostRecoveryPlan) -> bool:
        def write(db: Session) -> bool:
            _, host = self._budgets(db, plan.host_id)
            current = HostRecoveryPlan.model_validate(host.recovery) if host.recovery else None
            if current is None or current != plan or current.phase != "quiescing":
                return False
            host.fenced = False
            host.recovery = current.model_copy(update={"phase": "cancelled"}).model_dump(
                mode="json"
            )
            return True

        return run_write_transaction(self._session, "cancel_uncommitted_host_recovery", write)

    def mark_recovery_unconfirmed(self, plan: HostRecoveryPlan) -> None:
        def write(db: Session) -> None:
            _, host = self._budgets(db, plan.host_id)
            current = HostRecoveryPlan.model_validate(host.recovery) if host.recovery else None
            if current and current.same_operation(plan) and current.phase == "restart_committed":
                host.recovery = current.model_copy(
                    update={"phase": "recovery_unconfirmed"}
                ).model_dump(mode="json")

        run_write_transaction(self._session, "mark_host_recovery_unconfirmed", write)

    def reconcile_recovery(
        self,
        plan: HostRecoveryPlan,
        *,
        new_generation: str,
        account_generation: str,
        stopped: set[tuple[str, str]],
        adopted: set[tuple[str, str]],
        inputs_reconciled: bool,
    ) -> bool:
        if (
            not new_generation
            or new_generation == plan.host_generation
            or account_generation != plan.account_generation
            or not inputs_reconciled
            or stopped & adopted
            or stopped | adopted != set(plan.members)
        ):
            return False

        def write(db: Session) -> bool:
            _, host = self._budgets(db, plan.host_id)
            current = HostRecoveryPlan.model_validate(host.recovery) if host.recovery else None
            if (
                current is None
                or not current.same_operation(plan)
                or current.phase not in ("restart_committed", "recovery_unconfirmed")
                or host.work_fence_epoch != plan.epoch
                or not host.fenced
            ):
                return False
            rows = db.scalars(
                select(SqlSessionStartup)
                .where(
                    SqlSessionStartup.workspace_id == current_workspace_id(),
                    SqlSessionStartup.host_id == plan.host_id,
                    SqlSessionStartup.resident.is_(True),
                )
                .with_for_update()
            ).all()
            if any(
                row.host_generation != plan.host_generation
                or (row.runtime_id, row.host_generation) not in plan.members
                or (row.occupied and (row.runtime_id, row.host_generation) not in stopped)
                for row in rows
            ):
                return False
            for row in rows:
                if (row.runtime_id, row.host_generation) in stopped:
                    row.occupied = row.resident = False
                    row.state = "cancelled" if row.cancel_requested else "stopped"
                    row.updated_at = now_epoch()
            host.fenced = False
            host.recovery = current.model_copy(update={"phase": "complete"}).model_dump(
                mode="json"
            )
            return True

        return run_write_transaction(self._session, "reconcile_recovered_host_generations", write)

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
            now = now_epoch()
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
                    "elapsed_seconds": max(0, now - row.created_at),
                    "admitted_elapsed_seconds": (
                        max(0, now - row.admitted_at) if row.admitted_at is not None else None
                    ),
                    "input_elapsed_seconds": (
                        max(0, now - row.first_dispatch_at)
                        if row.first_dispatch_at is not None
                        else None
                    ),
                    "unconfirmed_startups": self._unconfirmed_startups(db, row.host_id),
                    "queue_position_estimate": position,
                    "can_cancel": row.state
                    in (
                        "queued",
                        "starting",
                        "verifying",
                        "waiting_input",
                        "uncertain",
                        "blocked",
                    ),
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

    def unconfirmed_startups(self, host_id: str) -> int:
        """Count current uncertain permits on a host for its pause indicator."""
        with self._session("read_host_unconfirmed_startups") as db:
            return self._unconfirmed_startups(db, host_id)

    def _unconfirmed_startups(self, db: Session, host_id: str) -> int:
        candidates = db.scalars(
            select(SqlSessionStartup).where(
                SqlSessionStartup.workspace_id == current_workspace_id(),
                SqlSessionStartup.host_id == host_id,
                SqlSessionStartup.state == "uncertain",
                SqlSessionStartup.occupied.is_(True),
            )
        ).all()
        return sum(
            1
            for row in candidates
            if (current := self._current_session_row(db, row.session_id)) is not None
            and current.id == row.id
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
        reuse_startup_id: str | None = None,
        preparation: dict | None = None,
    ) -> StartupSnapshot:
        if not 0 < len(operation_key) <= 128 or not 0 < len(owner_user_id) <= 128:
            raise ValueError("Invalid owner or operation key")

        def write(db: Session) -> StartupSnapshot:
            deployment, host = self._budgets(db, host_id)
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
                ) != (host_id, session_id, input_id, runtime_generation):
                    raise ValueError("Conflicting startup operation key")
                if old.preparation != preparation:
                    raise ValueError("Conflicting startup preparation")
                return _snapshot(old)
            reuse = (
                db.get(SqlSessionStartup, (current_workspace_id(), reuse_startup_id))
                if reuse_startup_id
                else None
            )
            latest = self._current_session_row(db, session_id) if reuse_startup_id else None
            if reuse_startup_id and (
                reuse is None
                or reuse.owner_user_id != owner_user_id
                or reuse.host_id != host_id
                or reuse.session_id != session_id
                or reuse.state != "ready_empty"
                or reuse.input_id is not None
                or not reuse.resident
                or reuse.cancel_requested
                or not reuse.runtime_id
                or not reuse.host_generation
                or host.current_generation not in (None, reuse.host_generation)
                or runtime_generation != reuse.runtime_generation + 1
                or input_id is None
                or latest is None
                or latest.id != reuse.id
            ):
                raise ValueError("Cannot reuse this runtime for first input")
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
                runtime_ready=False,
                tools_ready=False,
                input_accepted=False,
                work_started=False,
                cancel_requested=False,
                created_at=now_epoch(),
                updated_at=now_epoch(),
                runtime_id=reuse.runtime_id if reuse else None,
                host_generation=reuse.host_generation if reuse else None,
                cold_claims={"resident_reuse": reuse.id} if reuse else {},
                preparation=preparation,
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

    def attach_queued_input(self, startup_id: str, input_id: str) -> StartupSnapshot | None:
        """Attach the canonical first input only before any permit is issued."""

        def write(db: Session) -> StartupSnapshot | None:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return None
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if row.state == "queued" and not row.cancel_requested and row.input_id is None:
                row.input_id, row.updated_at = input_id, now_epoch()
            return _snapshot(row)

        return run_write_transaction(self._session, "attach_queued_startup_input", write)

    def attach_preparation(self, startup_id: str, preparation: dict) -> StartupSnapshot:
        def write(db: Session) -> StartupSnapshot:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                raise ValueError("Startup disappeared")
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if row.state != "queued" or row.cancel_requested or row.occupied:
                raise ValueError("Startup preparation already issued")
            if row.preparation is not None and row.preparation != preparation:
                raise ValueError("Conflicting startup preparation")
            row.preparation, row.updated_at = preparation, now_epoch()
            return _snapshot(row)

        return run_write_transaction(self._session, "attach_startup_preparation", write)

    def attach_parent_dependency(self, startup_id: str, parent_startup_id: str) -> bool:
        """Fence a queued child on its parent's current startup generation."""

        def write(db: Session) -> bool:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return False
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            parent = db.get(SqlSessionStartup, (current_workspace_id(), parent_startup_id))
            child_current = self._current_session_row(db, row.session_id, locked=True)
            parent_current = (
                self._current_session_row(db, parent.session_id, locked=True)
                if parent is not None
                else None
            )
            if (
                parent is None
                or row.id == parent.id
                or row.state != "queued"
                or row.occupied
                or row.cancel_requested
                or row.attempt_id is not None
                or child_current is None
                or child_current.id != row.id
                or parent_current is None
                or parent_current.id != parent.id
                or parent.parent_startup_id == row.id
            ):
                return False
            if (row.parent_startup_id, row.family_id) != (parent.id, parent.family_id):
                row.parent_startup_id = parent.id
                row.family_id = parent.family_id
                row.updated_at = now_epoch()
            return True

        return run_write_transaction(self._session, "attach_parent_startup_dependency", write)

    def expire_observations(
        self, host_id: str, *, now: int | None = None
    ) -> list[StartupSnapshot]:
        """Advance fixed observation budgets without releasing uncertain work."""
        current_time = now_epoch() if now is None else now

        def write(db: Session) -> list[StartupSnapshot]:
            self._budgets(db, host_id)
            changed: list[StartupSnapshot] = []
            rows = db.scalars(
                select(SqlSessionStartup)
                .where(
                    SqlSessionStartup.workspace_id == current_workspace_id(),
                    SqlSessionStartup.host_id == host_id,
                    SqlSessionStartup.state.in_(("queued", "starting", "verifying", "uncertain")),
                )
                .with_for_update()
            ).all()
            for row in rows:
                if row.state == "queued":
                    if current_time - row.created_at >= 86400:
                        row.state, row.reason, row.updated_at = (
                            "blocked",
                            "needs_attention",
                            current_time,
                        )
                        changed.append(_snapshot(row))
                    continue
                if not row.occupied or row.cancel_requested:
                    continue
                if row.admitted_at is None:
                    if row.state != "uncertain":
                        row.state, row.reason, row.updated_at = (
                            "uncertain",
                            "admission_origin_unknown",
                            current_time,
                        )
                        changed.append(_snapshot(row))
                    continue
                deadlines: list[tuple[int, str, str]] = []
                if not (row.runtime_ready and row.tools_ready):
                    deadlines.append(
                        (
                            row.admitted_at + 180,
                            "runtime_observation_pending",
                            "runtime_readiness_unconfirmed",
                        )
                    )
                if (
                    row.input_id is not None
                    and row.first_dispatch_at is not None
                    and not (row.input_accepted and row.work_started)
                ):
                    deadlines.append(
                        (
                            row.first_dispatch_at + 120,
                            "input_observation_pending",
                            "input_start_unconfirmed",
                        )
                    )
                if not deadlines:
                    continue
                deadline, pending_reason, final_reason = min(deadlines)
                if current_time < deadline:
                    continue
                if row.state == "uncertain" and row.reason not in (
                    "runtime_observation_pending",
                    "input_observation_pending",
                ):
                    continue
                if current_time < deadline + 30:
                    if row.reason != pending_reason:
                        row.reason, row.updated_at = pending_reason, current_time
                        changed.append(_snapshot(row))
                elif (row.state, row.reason) != ("uncertain", final_reason):
                    row.state, row.reason, row.updated_at = "uncertain", final_reason, current_time
                    changed.append(_snapshot(row))
            return changed

        return run_write_transaction(self._session, "expire_startup_observation_budget", write)

    def reserve(
        self, host_id: str, *, eligible_ids: set[str] | None = None
    ) -> StartupSnapshot | None:
        def write(db: Session) -> StartupSnapshot | None:
            deployment, host = self._budgets(db, host_id)
            now = now_epoch()
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

            def residents(*predicates) -> int:
                contexts = (
                    select(
                        SqlSessionStartup.workspace_id,
                        SqlSessionStartup.host_id,
                        SqlSessionStartup.session_id,
                        SqlSessionStartup.runtime_id,
                        case((SqlSessionStartup.runtime_id.is_(None), SqlSessionStartup.id)),
                    )
                    .where(SqlSessionStartup.resident.is_(True), *predicates)
                    .distinct()
                    .subquery()
                )
                return db.scalar(select(func.count()).select_from(contexts)) or 0

            stale = db.scalars(
                select(SqlSessionStartup)
                .where(
                    *scope,
                    SqlSessionStartup.state == "queued",
                    SqlSessionStartup.created_at <= now - 86400,
                )
                .with_for_update()
            ).all()
            for row in stale:
                row.state, row.reason, row.updated_at = "blocked", "needs_attention", now

            if (
                host.fenced
                or deployment.resident_limit == 0
                or host.resident_limit == 0
                or count(*scope, SqlSessionStartup.occupied.is_(True)) >= host.startup_limit
            ):
                return None
            full = (
                residents(*scope) >= host.resident_limit
                or residents() >= deployment.resident_limit
            )
            query = select(SqlSessionStartup).where(*scope, SqlSessionStartup.state == "queued")
            if eligible_ids is not None:
                query = query.where(SqlSessionStartup.id.in_(eligible_ids))
            rows = db.scalars(query.order_by(SqlSessionStartup.sequence)).all()
            eligible = []
            for row in rows:
                reuses_resident = (
                    row.runtime_id is not None
                    and db.scalar(
                        select(SqlSessionStartup.id)
                        .where(
                            *scope,
                            SqlSessionStartup.session_id == row.session_id,
                            SqlSessionStartup.runtime_id == row.runtime_id,
                            SqlSessionStartup.host_generation == row.host_generation,
                            SqlSessionStartup.resident.is_(True),
                        )
                        .limit(1)
                    )
                    is not None
                )
                if full and not reuses_resident:
                    continue
                if row.parent_startup_id:
                    parent = db.get(
                        SqlSessionStartup, (current_workspace_id(), row.parent_startup_id)
                    )
                    parent_current = (
                        self._current_session_row(db, parent.session_id, locked=True)
                        if parent is not None
                        else None
                    )
                    if (
                        parent is None
                        or parent_current is None
                        or parent_current.id != parent.id
                        or parent.state not in ("working", "ready_empty", "command_completed")
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
            row.attempt_id, row.updated_at, row.admitted_at = uuid4().hex, now, now
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
        account: AccountAuthority | None = None,
    ) -> bool:
        """A lost grant response is ambiguous; never grant the same physical start twice."""
        if boundary not in (
            "workspace_prepare",
            "input_dispatch",
            "runner_prepare",
            "runner_spawn",
            "harness_prepare",
            "native_prepare",
            "executor_construct",
            "executor_prepare",
        ):
            raise ValueError("Unknown cold startup boundary")
        if not all(0 < len(value) <= 128 for value in (runtime_id, host_generation, claim_id)):
            raise ValueError("Invalid cold startup identity")

        def write(db: Session) -> bool:
            if account is not None:
                if account.workspace_id != current_workspace_id():
                    raise OmnigentError(
                        "account authority has been revoked", code=ErrorCode.UNAUTHORIZED
                    )
                require_active_account(db, account.user_id, generation=account.generation)
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
                or host.current_generation not in (None, host_generation)
                or (expected_runner_id is not None and host.current_generation != host_generation)
                or ("resident_reuse" in row.cold_claims and boundary != "input_dispatch")
                or boundary in row.cold_claims
                or (boundary == "runner_spawn" and "runner_prepare" not in row.cold_claims)
            ):
                return False
            if expected_runner_id is not None and not self._bound_runner_matches(
                db, row, expected_runner_id
            ):
                return False
            if boundary == "workspace_prepare":
                binding = db.get(
                    SqlConversationMetadata,
                    (current_workspace_id(), row.session_id),
                    with_for_update=True,
                )
                if (
                    row.preparation is None
                    or binding is None
                    or (binding.host_id, binding.runner_id)
                    != (row.preparation["previous_host_id"], row.preparation["previous_runner_id"])
                ):
                    return False
            row.runtime_id, row.host_generation = runtime_id, host_generation
            row.cold_claims = {**row.cold_claims, boundary: claim_id}
            row.state, row.updated_at = "verifying", now_epoch()
            return True

        return run_write_transaction(self._session, "grant_cold_start_boundary", write)

    def _bound_runner_matches(self, db: Session, row: SqlSessionStartup, runner_id: str) -> bool:
        binding = db.get(
            SqlConversationMetadata,
            (current_workspace_id(), row.session_id),
            with_for_update=True,
        )
        return (
            binding is not None
            and binding.host_id == row.host_id
            and binding.runner_id == runner_id
        )

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
                        "waiting_input",
                        "uncertain",
                        "blocked",
                        "cancelling",
                    )
                ):
                    return None
            row.cancel_requested = True
            physical_runner_claimed = row.state not in ("cancelled", "stopped") and bool(
                {"runner_prepare", "runner_spawn"} & row.cold_claims.keys()
            )
            row.state = "cancelling" if row.occupied or physical_runner_claimed else "cancelled"
            row.updated_at = now_epoch()
            return _snapshot(row)

        return run_write_transaction(self._session, "cancel_startup", write)

    def cancel_session_current(self, session_id: str) -> StartupSnapshot | None:
        """Session Stop fences the latest attempt atomically with reservation."""

        def write(db: Session) -> StartupSnapshot | None:
            row = self._current_session_row(db, session_id)
            if row is None:
                return None
            self._budgets(db, row.host_id)
            row = self._current_session_row(db, session_id, locked=True)
            assert row is not None
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if row.state not in (
                "queued",
                "starting",
                "verifying",
                "waiting_input",
                "blocked",
                "uncertain",
                "cancelling",
            ):
                return None
            row.cancel_requested = True
            if not (set(row.cold_claims) - {"resident_reuse"}):
                row.occupied = row.resident = False
                row.state = "cancelled"
            else:
                row.state = "cancelling"
            row.updated_at = now_epoch()
            return _snapshot(row)

        return run_write_transaction(self._session, "cancel_current_session_startup", write)

    def reject_preparation(self, startup_id: str, attempt_id: str) -> bool:
        """A definite preparation refusal proves no runtime command was issued."""

        def write(db: Session) -> bool:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return False
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if (
                row.attempt_id != attempt_id
                or set(row.cold_claims) - {"workspace_prepare"}
                or row.runtime_ready
                or row.tools_ready
                or row.proof_source is not None
            ):
                return False
            if not row.occupied:
                return row.state in ("blocked", "cancelled")
            cancelled = row.cancel_requested
            row.occupied = row.resident = False
            row.cancel_requested = True
            row.state = "cancelled" if cancelled else "blocked"
            row.reason = None if cancelled else "preparation_refused"
            row.updated_at = now_epoch()
            return True

        return run_write_transaction(self._session, "reject_startup_preparation", write)

    @staticmethod
    def _settle_runtime_stopped(db: Session, row: SqlSessionStartup) -> None:
        if row.runtime_id is not None:
            for previous in db.scalars(
                select(SqlSessionStartup)
                .where(
                    SqlSessionStartup.workspace_id == current_workspace_id(),
                    SqlSessionStartup.host_id == row.host_id,
                    SqlSessionStartup.session_id == row.session_id,
                    SqlSessionStartup.runtime_id == row.runtime_id,
                    SqlSessionStartup.host_generation == row.host_generation,
                )
                .with_for_update()
            ):
                previous.occupied = previous.resident = False
                previous.state = "cancelled" if previous.cancel_requested else "stopped"
                previous.updated_at = now_epoch()
        row.occupied = row.resident = False
        row.state = "cancelled" if row.cancel_requested else "stopped"
        row.updated_at = now_epoch()

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
            self._settle_runtime_stopped(db, row)
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

    def discard_ungranted_attempt(
        self, startup_id: str, attempt_id: str, *, runtime_id: str
    ) -> bool:
        """Release a preflight failure only when no physical boundary was granted."""

        def write(db: Session) -> bool:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return False
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if (
                row.attempt_id != attempt_id
                or row.runtime_id != runtime_id
                or set(row.cold_claims) - {"resident_reuse"}
                or row.proof_source is not None
                or row.runtime_ready
                or row.tools_ready
            ):
                return False
            if not row.occupied:
                return row.state in ("blocked", "cancelled")
            cancelled = row.cancel_requested
            row.cancel_requested = True
            row.occupied = row.resident = False
            row.state = "cancelled" if cancelled else "blocked"
            row.reason = None if cancelled else "input_dispatch_unavailable"
            row.updated_at = now_epoch()
            return True

        return run_write_transaction(self._session, "discard_ungranted_startup_attempt", write)

    def bind_dispatch(
        self,
        startup_id: str,
        attempt_id: str,
        *,
        runtime_id: str,
        source: str,
        vendor_session_id: str,
        vendor_turn_id: str,
        expected_runner_id: str | None = None,
        host_generation: str | None = None,
        account_authority: AccountAuthority | None = None,
    ) -> bool:
        """Bind vendor identities from the owning adapter before accepting its proof."""
        if not (
            0 < len(source) <= 64
            and 0 < len(vendor_session_id) <= 256
            and 0 < len(vendor_turn_id) <= 256
        ):
            raise ValueError("Invalid startup dispatch identity")

        def write(db: Session) -> bool:
            if account_authority is not None:
                if account_authority.workspace_id != current_workspace_id():
                    raise OmnigentError(
                        "account authority has been revoked", code=ErrorCode.UNAUTHORIZED
                    )
                require_active_account(
                    db, account_authority.user_id, generation=account_authority.generation
                )
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return False
            _, host = self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if expected_runner_id is not None and (
                row.host_generation != host_generation
                or host.current_generation != host_generation
                or not self._bound_runner_matches(db, row, expected_runner_id)
            ):
                return False
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

    def defer_empty_sdk(
        self,
        startup_id: str,
        attempt_id: str,
        *,
        runtime_id: str,
        host_generation: str,
        expected_runner_id: str,
    ) -> bool:
        """An owned empty runner may wait without claiming an SDK context."""

        def write(db: Session) -> bool:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return False
            _, host = self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            current = self._current_session_row(db, row.session_id, locked=True)
            if (
                current is None
                or current.id != row.id
                or row.attempt_id != attempt_id
                or row.runtime_id != runtime_id
                or row.host_generation != host_generation
                or host.current_generation != host_generation
                or row.input_id is not None
                or row.cancel_requested
                or row.proof_source is not None
                or row.vendor_session_id is not None
                or row.vendor_turn_id is not None
                or row.runtime_ready
                or row.tools_ready
                or row.input_accepted
                or row.work_started
                or any(
                    boundary in row.cold_claims
                    for boundary in (
                        "harness_prepare",
                        "executor_construct",
                        "executor_prepare",
                        "native_prepare",
                        "input_dispatch",
                        "resident_reuse",
                    )
                )
                or not self._bound_runner_matches(db, row, expected_runner_id)
            ):
                return False
            if row.state == "waiting_input":
                return not row.occupied and not row.resident
            if row.state not in ("starting", "verifying") or not row.occupied:
                return False
            row.state, row.occupied, row.resident = "waiting_input", False, False
            row.reason, row.updated_at = None, now_epoch()
            return True

        return run_write_transaction(self._session, "defer_empty_sdk_startup", write)

    def observe(
        self,
        receipt: StartupReceipt,
        *,
        expected_runner_id: str | None = None,
        host_generation: str | None = None,
        account_authority: AccountAuthority | None = None,
    ) -> bool:
        """The caller must authenticate the runner/session before supplying a receipt."""

        def write(db: Session) -> bool:
            if account_authority is not None:
                if account_authority.workspace_id != current_workspace_id():
                    raise OmnigentError(
                        "account authority has been revoked", code=ErrorCode.UNAUTHORIZED
                    )
                require_active_account(
                    db, account_authority.user_id, generation=account_authority.generation
                )
            row = db.get(SqlSessionStartup, (current_workspace_id(), receipt.startup_id))
            if row is None:
                return False
            _, host = self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
            if expected_runner_id is not None and (
                row.host_generation != host_generation
                or host.current_generation != host_generation
                or not self._bound_runner_matches(db, row, expected_runner_id)
            ):
                return False
            if (
                row.attempt_id != receipt.attempt_id
                or row.runtime_id != receipt.runtime_id
                or row.input_id != receipt.input_id
            ):
                return False
            if receipt.phase == "stopped":
                self._settle_runtime_stopped(db, row)
                return True
            if row.cancel_requested:
                return False
            if receipt.phase in ("input_dispatched", "input_accepted", "work_started"):
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
                    "input_dispatched",
                    "input_accepted",
                    "work_started",
                )
            if receipt.phase in ("blocked", "uncertain") and row.state != "blocked":
                row.state, row.reason = receipt.phase, receipt.reason
            elif receipt.phase == "input_dispatched":
                if row.first_dispatch_at is None:
                    row.first_dispatch_at = now_epoch()
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
