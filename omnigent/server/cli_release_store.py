"""Database queue for generation-fenced CLI release intents."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from sqlalchemy import Engine, or_, select, update

from omnigent.db.db_models import SqlCliReleaseIntent, current_workspace_id
from omnigent.db.utils import (
    get_or_create_conversation_engine,
    make_named_managed_session_maker,
    now_epoch,
)

ReleaseReason = Literal["archive", "idle_pool_overflow"]


class _RowCountResult(Protocol):
    rowcount: int


@dataclass(frozen=True)
class CliReleaseIntent:
    id: str
    reason: ReleaseReason
    root_session_id: str
    target_session_id: str
    host_id: str | None
    runner_id: str | None
    family: str | None
    policy_revision: int | None
    archive_revision: int | None
    runtime_generation: str | None
    activity_token: str | None
    idle_threshold_seconds: int | None
    status: str
    claim_token: str | None
    claimed_at: int | None
    next_attempt_at: int
    last_error: str | None
    created_at: int


def _to_intent(row: SqlCliReleaseIntent) -> CliReleaseIntent:
    return CliReleaseIntent(
        id=row.id,
        reason=cast(ReleaseReason, row.reason),
        root_session_id=row.root_session_id,
        target_session_id=row.target_session_id,
        host_id=row.host_id,
        runner_id=row.runner_id,
        family=row.family,
        policy_revision=row.policy_revision,
        archive_revision=row.archive_revision,
        runtime_generation=row.runtime_generation,
        activity_token=row.activity_token,
        idle_threshold_seconds=row.idle_threshold_seconds,
        status=row.status,
        claim_token=row.claim_token,
        claimed_at=row.claimed_at,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        created_at=row.created_at,
    )


class CliReleaseIntentStore:
    """Persist release work independently of any Server process lifetime."""

    def __init__(self, storage_location: str) -> None:
        self._engine: Engine = get_or_create_conversation_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.cli_release_intent_store"
        )

    def ensure_archive_targets(
        self,
        root_session_id: str,
        archive_revision: int,
        targets: list[Any],
    ) -> list[CliReleaseIntent]:
        """Idempotently materialize one target intent per archived tree member."""
        now = now_epoch()
        result: list[CliReleaseIntent] = []
        with self._session("ensure_archive_targets") as session:
            for target in targets:
                dedupe = f"archive:{root_session_id}:{archive_revision}:{target.id}"
                row = session.execute(
                    select(SqlCliReleaseIntent).where(
                        SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                        SqlCliReleaseIntent.dedupe_key == dedupe,
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = SqlCliReleaseIntent(
                        id=secrets.token_hex(16),
                        dedupe_key=dedupe,
                        reason="archive",
                        root_session_id=root_session_id,
                        target_session_id=target.id,
                        host_id=target.host_id,
                        runner_id=target.runner_id,
                        archive_revision=archive_revision,
                        status="pending",
                        next_attempt_at=0,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    session.flush()
                result.append(_to_intent(row))
        return result

    def ensure_idle_intent(
        self,
        *,
        host_id: str,
        target_session_id: str,
        runner_id: str,
        family: str,
        policy_revision: int,
        runtime_generation: str,
        activity_token: str,
        idle_threshold_seconds: int,
    ) -> CliReleaseIntent:
        """Reserve one exact idle runtime once for a policy generation."""
        dedupe = f"idle:{host_id}:{policy_revision}:{target_session_id}:{runtime_generation}"
        now = now_epoch()
        with self._session("ensure_idle_intent") as session:
            row = session.execute(
                select(SqlCliReleaseIntent).where(
                    SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                    SqlCliReleaseIntent.dedupe_key == dedupe,
                )
            ).scalar_one_or_none()
            if row is None:
                row = SqlCliReleaseIntent(
                    id=secrets.token_hex(16),
                    dedupe_key=dedupe,
                    reason="idle_pool_overflow",
                    root_session_id=target_session_id,
                    target_session_id=target_session_id,
                    host_id=host_id,
                    runner_id=runner_id,
                    family=family,
                    policy_revision=policy_revision,
                    runtime_generation=runtime_generation,
                    activity_token=activity_token,
                    idle_threshold_seconds=idle_threshold_seconds,
                    status="pending",
                    next_attempt_at=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            return _to_intent(row)

    def list_due(self, *, now: int, limit: int = 200) -> list[CliReleaseIntent]:
        with self._session("list_due") as session:
            rows = session.scalars(
                select(SqlCliReleaseIntent)
                .where(
                    SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                    SqlCliReleaseIntent.status.in_(("pending", "claimed")),
                    SqlCliReleaseIntent.next_attempt_at <= now,
                )
                .order_by(SqlCliReleaseIntent.next_attempt_at, SqlCliReleaseIntent.id)
                .limit(limit)
            ).all()
            return [_to_intent(row) for row in rows]

    def list_due_for_root(
        self, root_session_id: str, *, now: int, limit: int = 1
    ) -> list[CliReleaseIntent]:
        with self._session("list_due_for_root") as session:
            rows = session.scalars(
                select(SqlCliReleaseIntent)
                .where(
                    SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                    SqlCliReleaseIntent.status.in_(("pending", "claimed")),
                    SqlCliReleaseIntent.next_attempt_at <= now,
                    SqlCliReleaseIntent.root_session_id == root_session_id,
                    SqlCliReleaseIntent.reason == "archive",
                )
                .order_by(SqlCliReleaseIntent.next_attempt_at, SqlCliReleaseIntent.id)
                .limit(limit)
            ).all()
            return [_to_intent(row) for row in rows]

    def due_workspaces(self, *, now: int) -> set[int]:
        """Return tenant partitions containing retryable release work."""
        with self._session("due_workspaces") as session:
            return set(
                session.scalars(
                    select(SqlCliReleaseIntent.workspace_id)
                    .where(
                        SqlCliReleaseIntent.status.in_(("pending", "claimed")),
                        SqlCliReleaseIntent.next_attempt_at <= now,
                    )
                    .distinct()
                )
            )

    def claim(
        self,
        intent_id: str,
        token: str,
        *,
        claimed_at: int,
        stale_before: int,
    ) -> bool:
        with self._session("claim") as session:
            result = cast(
                _RowCountResult,
                session.execute(
                    update(SqlCliReleaseIntent)
                    .where(
                        SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                        SqlCliReleaseIntent.id == intent_id,
                        or_(
                            SqlCliReleaseIntent.status == "pending",
                            (
                                (SqlCliReleaseIntent.status == "claimed")
                                & or_(
                                    SqlCliReleaseIntent.claimed_at.is_(None),
                                    SqlCliReleaseIntent.claimed_at < stale_before,
                                )
                            ),
                        ),
                    )
                    .values(
                        status="claimed",
                        claim_token=token,
                        claimed_at=claimed_at,
                        updated_at=claimed_at,
                    )
                ),
            )
            return bool(result.rowcount)

    def renew(self, intent_id: str, token: str, *, claimed_at: int) -> bool:
        with self._session("renew") as session:
            result = cast(
                _RowCountResult,
                session.execute(
                    update(SqlCliReleaseIntent)
                    .where(
                        SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                        SqlCliReleaseIntent.id == intent_id,
                        SqlCliReleaseIntent.status == "claimed",
                        SqlCliReleaseIntent.claim_token == token,
                    )
                    .values(claimed_at=claimed_at, updated_at=claimed_at)
                ),
            )
            return bool(result.rowcount)

    def complete(self, intent_id: str, token: str) -> bool:
        now = now_epoch()
        with self._session("complete") as session:
            result = cast(
                _RowCountResult,
                session.execute(
                    update(SqlCliReleaseIntent)
                    .where(
                        SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                        SqlCliReleaseIntent.id == intent_id,
                        SqlCliReleaseIntent.status == "claimed",
                        SqlCliReleaseIntent.claim_token == token,
                    )
                    .values(
                        status="completed",
                        claim_token=None,
                        claimed_at=None,
                        last_error=None,
                        updated_at=now,
                    )
                ),
            )
            return bool(result.rowcount)

    def retry(
        self,
        intent_id: str,
        token: str,
        *,
        error: str,
        next_attempt_at: int,
    ) -> bool:
        now = now_epoch()
        with self._session("retry") as session:
            result = cast(
                _RowCountResult,
                session.execute(
                    update(SqlCliReleaseIntent)
                    .where(
                        SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                        SqlCliReleaseIntent.id == intent_id,
                        SqlCliReleaseIntent.status == "claimed",
                        SqlCliReleaseIntent.claim_token == token,
                    )
                    .values(
                        status="pending",
                        claim_token=None,
                        claimed_at=None,
                        last_error=error[:512],
                        next_attempt_at=next_attempt_at,
                        updated_at=now,
                    )
                ),
            )
            return bool(result.rowcount)

    def cancel(self, intent_id: str) -> bool:
        now = now_epoch()
        with self._session("cancel") as session:
            result = cast(
                _RowCountResult,
                session.execute(
                    update(SqlCliReleaseIntent)
                    .where(
                        SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                        SqlCliReleaseIntent.id == intent_id,
                        SqlCliReleaseIntent.status == "pending",
                    )
                    .values(status="cancelled", updated_at=now)
                ),
            )
            return bool(result.rowcount)

    def cancel_claimed(self, intent_id: str, token: str) -> bool:
        """Cancel a superseded intent only while its worker still owns it."""
        now = now_epoch()
        with self._session("cancel_claimed") as session:
            result = cast(
                _RowCountResult,
                session.execute(
                    update(SqlCliReleaseIntent)
                    .where(
                        SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                        SqlCliReleaseIntent.id == intent_id,
                        SqlCliReleaseIntent.status == "claimed",
                        SqlCliReleaseIntent.claim_token == token,
                    )
                    .values(
                        status="cancelled",
                        claim_token=None,
                        claimed_at=None,
                        updated_at=now,
                    )
                ),
            )
            return bool(result.rowcount)

    def count_active_idle(self, *, host_id: str, family: str, policy_revision: int) -> int:
        with self._session("count_active_idle") as session:
            rows = session.scalars(
                select(SqlCliReleaseIntent.id).where(
                    SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                    SqlCliReleaseIntent.reason == "idle_pool_overflow",
                    SqlCliReleaseIntent.host_id == host_id,
                    SqlCliReleaseIntent.family == family,
                    SqlCliReleaseIntent.policy_revision == policy_revision,
                    SqlCliReleaseIntent.status.in_(("pending", "claimed")),
                )
            ).all()
            return len(rows)

    def active_idle_runtime_keys(
        self, *, host_id: str, family: str, policy_revision: int
    ) -> set[tuple[str, str]]:
        """Return target/runtime pairs already reserved for this pool revision."""
        with self._session("active_idle_runtime_keys") as session:
            rows = session.execute(
                select(
                    SqlCliReleaseIntent.target_session_id,
                    SqlCliReleaseIntent.runtime_generation,
                ).where(
                    SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                    SqlCliReleaseIntent.reason == "idle_pool_overflow",
                    SqlCliReleaseIntent.host_id == host_id,
                    SqlCliReleaseIntent.family == family,
                    SqlCliReleaseIntent.policy_revision == policy_revision,
                    SqlCliReleaseIntent.status.in_(("pending", "claimed")),
                )
            ).all()
            return {
                (target_id, generation) for target_id, generation in rows if generation is not None
            }

    def cancel_pending_idle_for_host(self, host_id: str) -> int:
        """Cancel idle releases not yet claimed when a Host exits the policy."""
        now = now_epoch()
        with self._session("cancel_pending_idle_for_host") as session:
            result = cast(
                _RowCountResult,
                session.execute(
                    update(SqlCliReleaseIntent)
                    .where(
                        SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                        SqlCliReleaseIntent.reason == "idle_pool_overflow",
                        SqlCliReleaseIntent.host_id == host_id,
                        SqlCliReleaseIntent.status == "pending",
                    )
                    .values(status="cancelled", updated_at=now)
                ),
            )
            return int(result.rowcount or 0)

    def archive_targets_complete(self, root_session_id: str, revision: int) -> bool:
        with self._session("archive_targets_complete") as session:
            statuses = list(
                session.scalars(
                    select(SqlCliReleaseIntent.status).where(
                        SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                        SqlCliReleaseIntent.reason == "archive",
                        SqlCliReleaseIntent.root_session_id == root_session_id,
                        SqlCliReleaseIntent.archive_revision == revision,
                    )
                )
            )
            return bool(statuses) and all(status == "completed" for status in statuses)

    def archive_binding_ready_to_stop(
        self,
        *,
        root_session_id: str,
        revision: int,
        current_intent_id: str,
        host_id: str,
        runner_id: str,
    ) -> bool:
        """Return whether this is the last unfinished target on one Runner."""
        with self._session("archive_binding_ready_to_stop") as session:
            unfinished_other = session.execute(
                select(SqlCliReleaseIntent.id)
                .where(
                    SqlCliReleaseIntent.workspace_id == current_workspace_id(),
                    SqlCliReleaseIntent.reason == "archive",
                    SqlCliReleaseIntent.root_session_id == root_session_id,
                    SqlCliReleaseIntent.archive_revision == revision,
                    SqlCliReleaseIntent.host_id == host_id,
                    SqlCliReleaseIntent.runner_id == runner_id,
                    SqlCliReleaseIntent.id != current_intent_id,
                    SqlCliReleaseIntent.status != "completed",
                )
                .limit(1)
            ).first()
            return unfinished_other is None
