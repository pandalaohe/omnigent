"""Immutable pool relations: the ``usage_account_relations`` table.

One row records that a destination usage account reports a stable source pool
under a parent account. The identity columns (parent, pool, channel, effective
boundary) never change: a renamed or reordered pool label only refreshes
``display_name``, and a real reparent needs a new source identity. An older
reporter that omits the relation never removes it.

``usage_hours`` keeps its own ``account_id`` and ``basis``; readers join this
table onto those rows, so nothing here rewrites hourly history.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from sqlalchemy import LargeBinary, select, type_coerce
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.compression import decode
from omnigent.db.db_models import (
    SqlPreference,
    SqlUsageAccountRelation,
    SqlUsageHour,
    current_workspace_id,
)
from omnigent.db.utils import (
    NamedManagedSessionMaker,
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore
from omnigent.usage.contract import POOL_CHANNELS, PoolRelationClaim

#: Hard bound on the relations one owner may hold, so a read stays bounded.
MAX_POOL_RELATIONS = 512

#: Cycles are impossible among N relations, so walking them is bounded.
_MAX_CYCLE_WALK = MAX_POOL_RELATIONS + 1


class UsagePoolRelationError(ValueError):
    """A relation claim that cannot be true: cycles, unknown parents, channels."""


class UsagePoolConflictError(UsagePoolRelationError):
    """A claim that would reassign an existing relation's immutable identity."""


@dataclass(frozen=True)
class UsageAccountRelation:
    """One stored ``usage_account_relations`` row, as read back."""

    workspace_id: int
    owner_id: str
    account_id: str
    parent_account_id: str
    pool_id: str
    channel: str
    display_name: str | None
    effective_from_hour: int
    created_at: int
    updated_at: int

    @property
    def identity(self) -> tuple[str, str, str, int]:
        """The immutable identity a repeat claim must match exactly."""
        return (
            self.parent_account_id,
            self.pool_id,
            self.channel,
            self.effective_from_hour,
        )

    @property
    def reports_usage(self) -> bool:
        """Whether rows of this destination are attributable pool usage."""
        return self.channel == "usage"


def _relation_from_orm(row: SqlUsageAccountRelation) -> UsageAccountRelation:
    return UsageAccountRelation(
        workspace_id=row.workspace_id,
        owner_id=row.owner_id,
        account_id=row.account_id,
        parent_account_id=row.parent_account_id,
        pool_id=row.pool_id,
        channel=row.channel,
        display_name=row.display_name,
        effective_from_hour=row.effective_from_hour,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class UsagePoolStore:
    """Lazy synchronous access to one owner's pool relations."""

    def __init__(self, storage_location: str) -> None:
        self._storage_location = storage_location
        self._read_sessions: NamedManagedSessionMaker | None = None
        self._write_sessions: NamedManagedSessionMaker | None = None

    def _read_session(self) -> NamedManagedSessionMaker:
        if self._read_sessions is None:
            self._read_sessions = make_named_managed_session_maker(
                get_or_create_engine(self._storage_location),
                query_name_prefix="omnigent.usage_pool_store",
            )
        return self._read_sessions

    def _write_session(self) -> NamedManagedSessionMaker:
        if self._write_sessions is None:
            self._write_sessions = make_named_managed_session_maker(
                get_or_create_engine(self._storage_location),
                query_name_prefix="omnigent.usage_pool_store",
                immediate=True,
            )
        return self._write_sessions

    # -- reads ----------------------------------------------------------

    def read(self, owner: str) -> list[UsageAccountRelation]:
        """Read one owner's relations in the current workspace.

        :param owner: The authenticated owner of every returned relation.
        :returns: Up to :data:`MAX_POOL_RELATIONS` rows, ordered by identity.
        """
        with self._read_session()("read_usage_pool_relations") as session:
            query = (
                select(SqlUsageAccountRelation)
                .where(
                    SqlUsageAccountRelation.workspace_id == current_workspace_id(),
                    SqlUsageAccountRelation.owner_id == owner,
                )
                .order_by(
                    SqlUsageAccountRelation.parent_account_id,
                    SqlUsageAccountRelation.pool_id,
                    SqlUsageAccountRelation.channel,
                    SqlUsageAccountRelation.account_id,
                )
                .limit(MAX_POOL_RELATIONS)
            )
            return [_relation_from_orm(row) for row in session.execute(query).scalars()]

    def read_map(self, owner: str) -> dict[str, UsageAccountRelation]:
        """Read one owner's relations keyed by destination account id."""
        return {relation.account_id: relation for relation in self.read(owner)}

    def get(self, owner: str, account_id: str) -> UsageAccountRelation | None:
        """Read one destination account's relation, scoped to its owner."""
        with self._read_session()("read_usage_pool_relation") as session:
            row = session.scalars(
                select(SqlUsageAccountRelation).where(
                    SqlUsageAccountRelation.workspace_id == current_workspace_id(),
                    SqlUsageAccountRelation.owner_id == owner,
                    SqlUsageAccountRelation.account_id == account_id,
                )
            ).one_or_none()
            return None if row is None else _relation_from_orm(row)

    # -- writes ---------------------------------------------------------

    def check(
        self,
        owner: str,
        account_id: str,
        claim: PoolRelationClaim,
        *,
        known_parents: Collection[str] = (),
    ) -> UsageAccountRelation | None:
        """Validate one claim without writing anything.

        The caller can therefore reject a bad claim before it applies the
        report's activity, so no relation or hourly row is persisted for a
        claim the user must fix. The stored row is returned when the claim is
        already present, else ``None``.

        :param owner: The authenticated owner of both accounts.
        :param account_id: The destination account the claim was reported on.
        :param claim: The validated report-v2 ``pool_relation`` metadata.
        :param known_parents: Owner accounts already known to the caller.
        :returns: The stored relation when the claim repeats it, else ``None``.
        :raises UsagePoolRelationError: A cycle, a missing parent, a bad channel.
        :raises UsagePoolConflictError: A reassignment of a stored relation.
        """
        with self._read_session()("check_usage_pool_relation") as session:
            return self._resolve(session, owner, account_id, claim, known_parents=known_parents)

    def write(
        self,
        owner: str,
        account_id: str,
        claim: PoolRelationClaim,
        *,
        display_name: str | None = None,
        known_parents: Collection[str] = (),
    ) -> UsageAccountRelation:
        """Store one immutable relation, idempotently.

        A repeated identical claim returns the stored row (refreshing only its
        label). A claim that changes the parent, pool, channel or effective
        boundary is a conflict: a real reparent needs a new source identity.

        :param owner: The authenticated owner of both accounts.
        :param account_id: The destination account the claim was reported on.
        :param claim: The validated report-v2 ``pool_relation`` metadata.
        :param display_name: The pool's current label, for display only.
        :param known_parents: Owner accounts the caller already knows about;
            a parent must still be proven by stored rows or a stored relation.
        :returns: The stored (or already present) relation.
        :raises UsagePoolRelationError: A cycle, a missing parent, a bad channel.
        :raises UsagePoolConflictError: A reassignment of a stored relation.
        """
        if claim.channel not in POOL_CHANNELS:
            raise UsagePoolRelationError(f"unknown pool channel {claim.channel!r}")
        if claim.parent_account_id == account_id:
            raise UsagePoolRelationError("a pool account cannot be its own parent")
        try:
            return run_write_transaction(
                self._write_session(),
                "write_usage_pool_relation",
                lambda session: self.write_in_session(
                    session,
                    owner,
                    account_id,
                    claim,
                    display_name=display_name,
                    known_parents=known_parents,
                ),
            )
        except IntegrityError:
            # A concurrent writer claimed the same logical (parent, pool,
            # channel), or this account's row appeared first. Re-read to tell
            # an idempotent repeat from a genuine conflict.
            existing = self.get(owner, account_id)
            if existing is not None and existing.identity == (
                claim.parent_account_id,
                claim.pool_id,
                claim.channel,
                claim.effective_from_hour,
            ):
                return existing
            raise UsagePoolConflictError(
                "pool relation already assigned to another destination account"
            ) from None

    def write_in_session(
        self,
        session: Session,
        owner: str,
        account_id: str,
        claim: PoolRelationClaim,
        *,
        display_name: str | None,
        known_parents: Collection[str],
    ) -> UsageAccountRelation:
        require_active_account(session, owner)
        # The owner can lack a users row under external auth. This upsert still
        # serializes graph edits, including two initially empty ancestor chains.
        UsageSnapshotStore.write_in_session(
            session, owner, "usage_pool.relations", {"schema_version": 1}
        )
        now = now_epoch()
        existing = self._resolve(session, owner, account_id, claim, known_parents=known_parents)
        if existing is not None:
            if not display_name or display_name == existing.display_name:
                return existing
            row = session.get(
                SqlUsageAccountRelation,
                (current_workspace_id(), owner, account_id),
            )
            if row is not None:
                row.display_name = display_name
                row.updated_at = now
            return UsageAccountRelation(
                workspace_id=existing.workspace_id,
                owner_id=existing.owner_id,
                account_id=existing.account_id,
                parent_account_id=existing.parent_account_id,
                pool_id=existing.pool_id,
                channel=existing.channel,
                display_name=display_name,
                effective_from_hour=existing.effective_from_hour,
                created_at=existing.created_at,
                updated_at=now,
            )
        session.add(
            SqlUsageAccountRelation(
                workspace_id=current_workspace_id(),
                owner_id=owner,
                account_id=account_id,
                parent_account_id=claim.parent_account_id,
                pool_id=claim.pool_id,
                channel=claim.channel,
                display_name=display_name,
                effective_from_hour=claim.effective_from_hour,
                created_at=now,
                updated_at=now,
            )
        )
        return UsageAccountRelation(
            workspace_id=current_workspace_id(),
            owner_id=owner,
            account_id=account_id,
            parent_account_id=claim.parent_account_id,
            pool_id=claim.pool_id,
            channel=claim.channel,
            display_name=display_name,
            effective_from_hour=claim.effective_from_hour,
            created_at=now,
            updated_at=now,
        )

    def _resolve(
        self,
        session: Session,
        owner: str,
        account_id: str,
        claim: PoolRelationClaim,
        *,
        known_parents: Collection[str],
    ) -> UsageAccountRelation | None:
        """Prove one claim against stored state, every relation and its identity."""
        scope = (
            SqlUsageAccountRelation.workspace_id == current_workspace_id(),
            SqlUsageAccountRelation.owner_id == owner,
        )
        rows = list(
            session.scalars(
                select(SqlUsageAccountRelation)
                .where(*scope)
                .order_by(SqlUsageAccountRelation.account_id)
                .limit(MAX_POOL_RELATIONS)
                .with_for_update()
            )
        )
        relations = {row.account_id: _relation_from_orm(row) for row in rows}
        existing = relations.get(account_id)
        if existing is not None:
            if existing.identity != (
                claim.parent_account_id,
                claim.pool_id,
                claim.channel,
                claim.effective_from_hour,
            ):
                raise UsagePoolConflictError(
                    "relation is immutable; a reparent needs a new source identity"
                )
            return existing
        if claim.channel not in POOL_CHANNELS:
            raise UsagePoolRelationError(f"unknown pool channel {claim.channel!r}")
        if len(rows) >= MAX_POOL_RELATIONS:
            raise UsagePoolRelationError("pool relations exceed the supported count")
        taken = next(
            (
                relation
                for relation in relations.values()
                if relation.account_id != account_id
                and relation.parent_account_id == claim.parent_account_id
                and relation.pool_id == claim.pool_id
                and relation.channel == claim.channel
            ),
            None,
        )
        if taken is not None:
            raise UsagePoolConflictError(
                "logical (parent, pool, channel) already has a destination account"
            )
        if not self._parent_is_known(
            session, owner, claim.parent_account_id, relations, known_parents
        ):
            raise UsagePoolRelationError(
                "parent account must exist for this owner before a pool reports to it"
            )
        self._reject_cycle(relations, account_id, claim.parent_account_id)
        return None

    @staticmethod
    def _parent_is_known(
        session: Session,
        owner: str,
        parent: str,
        relations: Mapping[str, UsageAccountRelation],
        known_parents: Collection[str],
    ) -> bool:
        """Prove the parent belongs to this owner in this workspace."""
        if parent in known_parents:
            return True
        raw = session.scalar(
            select(type_coerce(SqlPreference.value, LargeBinary)).where(
                SqlPreference.workspace_id == current_workspace_id(),
                SqlPreference.user_id == owner,
                SqlPreference.key == "usage.account." + parent,
            )
        )
        if raw is not None:
            value = json.loads(decode(raw) or "{}")
            if isinstance(value, dict) and value.get("report") is not None:
                return True
        if any(
            relation.account_id == parent or relation.parent_account_id == parent
            for relation in relations.values()
        ):
            return True
        return (
            session.execute(
                select(SqlUsageHour.account_id)
                .where(
                    SqlUsageHour.workspace_id == current_workspace_id(),
                    SqlUsageHour.owner_id == owner,
                    SqlUsageHour.account_id == parent,
                )
                .limit(1)
            ).first()
            is not None
        )

    @staticmethod
    def _reject_cycle(
        relations: Mapping[str, UsageAccountRelation],
        account_id: str,
        parent: str,
    ) -> None:
        """Refuse a link whose ancestor chain reaches the reporting account."""
        current: str | None = parent
        for _ in range(_MAX_CYCLE_WALK):
            if current is None:
                return
            if current == account_id:
                raise UsagePoolRelationError("pool relation would create a cycle")
            relation = relations.get(current)
            current = relation.parent_account_id if relation is not None else None

    def delete(self, owner: str, account_id: str) -> bool:
        """Remove one destination's relation, e.g. with its deleted account."""

        def remove(session: Session) -> bool:
            row = session.scalars(
                select(SqlUsageAccountRelation).where(
                    SqlUsageAccountRelation.workspace_id == current_workspace_id(),
                    SqlUsageAccountRelation.owner_id == owner,
                    SqlUsageAccountRelation.account_id == account_id,
                )
            ).one_or_none()
            if row is None:
                return False
            session.delete(row)
            return True

        return run_write_transaction(self._write_session(), "delete_usage_pool_relation", remove)


__all__ = [
    "MAX_POOL_RELATIONS",
    "UsageAccountRelation",
    "UsagePoolConflictError",
    "UsagePoolRelationError",
    "UsagePoolStore",
]
