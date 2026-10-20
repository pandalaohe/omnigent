"""Atomic host startup admission shared by application replicas."""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import current_workspace_id
from omnigent.db.session_startup import SqlSessionStartup, SqlStartupBudget
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)


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
        self, host_id: str, *, host_resident_limit: int, deployment_resident_limit: int
    ) -> None:
        if min(host_resident_limit, deployment_resident_limit) < 1:
            raise ValueError("Resident limits must be positive")

        def write(db: Session) -> None:
            deployment, host = self._budgets(db, host_id)
            deployment.resident_limit = deployment_resident_limit
            host.resident_limit = host_resident_limit

        run_write_transaction(self._session, "configure_capacity", write)

    def get(self, startup_id: str) -> StartupSnapshot | None:
        with self._session("read_startup") as db:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            return _snapshot(row) if row else None

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
                            ("queued", "starting", "verifying", "uncertain", "cancelling")
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

    def reserve(self, host_id: str) -> StartupSnapshot | None:
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
                or count(*scope, SqlSessionStartup.occupied.is_(True)) >= 2
                or count(*scope, SqlSessionStartup.resident.is_(True)) >= host.resident_limit
                or count(SqlSessionStartup.resident.is_(True)) >= deployment.resident_limit
            ):
                return None
            rows = db.scalars(
                select(SqlSessionStartup)
                .where(*scope, SqlSessionStartup.state == "queued")
                .order_by(SqlSessionStartup.sequence)
            ).all()
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
                or row.attempt_id != attempt_id
                or not row.occupied
                or row.runtime_id not in (None, runtime_id)
            ):
                return False
            row.runtime_id, row.state, row.updated_at = runtime_id, "verifying", now_epoch()
            return True

        return run_write_transaction(self._session, "claim_startup", write)

    def cancel(self, startup_id: str) -> StartupSnapshot | None:
        def write(db: Session) -> StartupSnapshot | None:
            row = db.get(SqlSessionStartup, (current_workspace_id(), startup_id))
            if row is None:
                return None
            self._budgets(db, row.host_id)
            db.refresh(row, with_for_update=True)
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
