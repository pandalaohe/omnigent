"""Workspace-scoped source evidence with idempotent dialect-native writes."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from itertools import batched
from typing import Any, cast

from sqlalchemy import CursorResult, SQLColumnExpression, delete, func, select, tuple_
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlPreference,
    SqlUsagePriceAliases,
    SqlUsagePriceEvidence,
    SqlUsageSourceEvent,
    SqlUsageSourceStatus,
    SqlUsageSpeedPoint,
    current_workspace_id,
)
from omnigent.db.utils import now_epoch, run_write_transaction
from omnigent.quota_pace import QuotaTiers
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore
from omnigent.usage.contract import UsageAccountView, UsageWindowView
from omnigent.usage.evidence import SourceEvent, SourceStatus
from omnigent.usage.prices import PriceAliases, PriceEvidence
from omnigent.usage.source_events import change_event_id
from omnigent.usage.speed import SpeedPoint

EVIDENCE_TABLES = (
    SqlUsageSourceStatus,
    SqlUsageSourceEvent,
    SqlUsageSpeedPoint,
    SqlUsagePriceEvidence,
    SqlUsagePriceAliases,
)


class UsageEvidenceStore:
    def __init__(self, storage_location: str) -> None:
        self.sessions = UsageSnapshotStore(storage_location)

    @staticmethod
    def scope(table: Any, owner: str) -> tuple[Any, ...]:
        return (table.workspace_id == current_workspace_id(), table.owner_id == owner)

    @staticmethod
    def _write(
        session: Session, table: Any, values: dict[str, Any], *, once: bool = False
    ) -> bool:
        dialect = session.get_bind().dialect.name
        key = [column.name for column in table.__table__.primary_key]
        insert = (
            mysql_insert
            if dialect == "mysql"
            else sqlite_insert
            if dialect == "sqlite"
            else pg_insert
        )
        proposed: Any = insert(table).values(**values)
        if dialect == "mysql":
            if once:
                # IGNORE can hide invalid values; a no-op conflict update cannot.
                statement = proposed.on_duplicate_key_update(event_id=proposed.inserted.event_id)
                # MySQL reports a no-op update as 1 with FOUND_ROWS; check under
                # the owner lock instead of using ambiguous affected-row counts.
                if session.get(table, tuple(values[name] for name in key)) is not None:
                    return False
            else:
                statement = proposed.on_duplicate_key_update(
                    **{name: value for name, value in values.items() if name not in key}
                )
        elif once:
            statement = proposed.on_conflict_do_nothing(index_elements=key).returning(
                table.event_id
            )
            return session.execute(statement).scalar_one_or_none() is not None
        else:
            statement = proposed.on_conflict_do_update(
                index_elements=key,
                set_={name: value for name, value in values.items() if name not in key},
            )
        return cast(CursorResult[Any], session.execute(statement)).rowcount == 1

    def _lock(self, session: Session, owner: str) -> None:
        require_active_account(session, owner)
        UsageSnapshotStore.write_in_session(
            session, owner, "usage.evidence.lock", {"schema_version": 1}
        )

    def upsert_prices(
        self, owner: str, rows: Sequence[PriceEvidence], aliases: Sequence[PriceAliases]
    ) -> None:
        def write(session: Session) -> None:
            self._lock(session, owner)
            for row in rows:
                values: dict[str, Any] = {
                    "workspace_id": current_workspace_id(),
                    "owner_id": owner,
                    "account_id": row.account,
                    "hour_utc": row.hour,
                    "model": row.model,
                    "updated_at": now_epoch(),
                }
                if row.estimate is not None:
                    table = SqlUsagePriceEvidence
                    existing = session.get(
                        table,
                        tuple(values[column.name] for column in table.__table__.primary_key),
                    )
                    # Unknown or equal ordering keeps arrival-order replacement.
                    older = (
                        existing is not None
                        and existing.as_of is not None
                        and row.estimate.as_of is not None
                        and row.estimate.as_of < existing.as_of
                    )
                    if not older:
                        values.update(row.estimate.model_dump())
                    elif row.partitions is None:
                        continue
                if row.partitions is not None:
                    values["partitions"] = [part.model_dump() for part in row.partitions]
                self._write(session, SqlUsagePriceEvidence, values)
                session.expire_all()
            for row in aliases:
                self._write(
                    session,
                    SqlUsagePriceAliases,
                    {
                        "workspace_id": current_workspace_id(),
                        "owner_id": owner,
                        "account_id": row.account,
                        "mappings": row.mappings,
                        "observed_at": row.observed_at,
                        "updated_at": now_epoch(),
                    },
                )

        run_write_transaction(self.sessions._write_session(), "report_price_evidence", write)

    def read_prices(
        self,
        owner: str,
        start: int,
        end: float,
        account: str | None = None,
        model: str | None = None,
        *,
        keys: set[tuple[str, int, str]] | None = None,
        include_aliases: bool = True,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        table = SqlUsagePriceEvidence
        query = select(table).where(
            *self.scope(table, owner), table.hour_utc >= start, table.hour_utc < end
        )
        alias_query = select(SqlUsagePriceAliases).where(*self.scope(SqlUsagePriceAliases, owner))
        if account is not None:
            query = query.where(table.account_id == account)
            alias_query = alias_query.where(SqlUsagePriceAliases.account_id == account)
        if model is not None:
            query = query.where(table.model == model)
        with self.sessions._read_session()("read_price_evidence") as session:
            # Three binds per key plus scope/range predicates fit even SQLite's 999 limit.
            queries = (
                (
                    query.where(tuple_(table.account_id, table.hour_utc, table.model).in_(batch))
                    for batch in batched(keys, 250)
                )
                if keys is not None
                else (query,)
            )
            rows = [
                {column.name: getattr(row, column.name) for column in table.__table__.columns}
                for selected in queries
                for row in session.scalars(selected)
            ]
            aliases = [
                {
                    "account": row.account_id,
                    "mappings": row.mappings,
                    "observed_at": row.observed_at,
                    "updated_at": row.updated_at,
                }
                for row in (
                    session.scalars(alias_query.order_by(SqlUsagePriceAliases.account_id))
                    if include_aliases
                    else []
                )
            ]
            return rows, aliases

    def upsert_speed(self, owner: str, rows: Sequence[SpeedPoint]) -> int:
        def write(session: Session) -> int:
            self._lock(session, owner)
            held = 0
            table = SqlUsageSpeedPoint
            for row in rows:
                values = dict(
                    row.values(),
                    workspace_id=current_workspace_id(),
                    owner_id=owner,
                    updated_at=now_epoch(),
                )
                existing = session.get(
                    table, tuple(values[column.name] for column in table.__table__.primary_key)
                )
                if (
                    existing is not None
                    and existing.coverage == "complete"
                    and not row.correction
                    and (row.coverage == "partial" or row.count < existing.count)
                ):
                    held += 1
                    continue
                self._write(session, table, values)
                session.expire_all()
            return held

        return run_write_transaction(self.sessions._write_session(), "report_speed_points", write)

    def read_speed(
        self,
        owner: str,
        start: int,
        end: float,
        account: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        table = SqlUsageSpeedPoint
        query = select(table).where(
            *self.scope(table, owner), table.start_utc >= start, table.end_utc <= end
        )
        for column, value in ((table.account_id, account), (table.model, model)):
            if value is not None:
                query = query.where(column == value)
        with self.sessions._read_session()("read_speed_points") as session:
            return [
                {column.name: getattr(row, column.name) for column in table.__table__.columns}
                for row in session.scalars(query)
            ]

    def upsert_status(self, owner: str, rows: Sequence[SourceStatus]) -> int:
        def write(session: Session) -> int:
            self._lock(session, owner)
            for row in rows:
                values = row.model_dump(exclude={"account"})
                self._write(
                    session,
                    SqlUsageSourceStatus,
                    dict(
                        values,
                        workspace_id=current_workspace_id(),
                        owner_id=owner,
                        account_id=row.account or "",
                        updated_at=now_epoch(),
                    ),
                )
            return len(rows)

        return run_write_transaction(self.sessions._write_session(), "report_source_status", write)

    def insert_events(self, owner: str, events: Sequence[SourceEvent]) -> dict[str, int]:
        return run_write_transaction(
            self.sessions._write_session(),
            "archive_source_events",
            lambda session: self.insert_events_in_session(session, owner, events),
        )

    def insert_events_in_session(
        self, session: Session, owner: str, events: Sequence[SourceEvent]
    ) -> dict[str, int]:
        self._lock(session, owner)
        accepted = 0
        for event in events:
            accepted += self._write(
                session,
                SqlUsageSourceEvent,
                {
                    "workspace_id": current_workspace_id(),
                    "owner_id": owner,
                    "source": event.source,
                    "event_id": event.id,
                    "account_id": event.subject or "",
                    "type": event.type,
                    "time_utc": event.time_utc,
                    "severity": event.severity,
                    "data": event.data,
                    "received_at": now_epoch(),
                },
                once=True,
            )
        return {"accepted": accepted, "duplicates": len(events) - accepted}

    def report_log_scopes(self) -> list[tuple[int, str]]:
        with self.sessions._read_session()("read_report_log_scopes") as session:
            return list(
                session.execute(
                    select(SqlPreference.workspace_id, SqlPreference.user_id)
                    .where(SqlPreference.key.startswith("usage.account.", autoescape=True))
                    .distinct()
                ).tuples()
            )

    def record_quota_tiers(
        self, owner: str, accounts: Sequence[UsageAccountView], tiers: QuotaTiers, now: float
    ) -> None:
        desired: dict[str, tuple[str, UsageAccountView, UsageWindowView]] = {}
        for account in accounts:
            if account.freshness != "fresh":
                continue
            for window in account.windows:
                remaining = window.remaining_pct
                if remaining is None or window.past_reset:
                    continue
                state = (
                    "error"
                    if remaining < tiers.low_below_pct
                    else "warn"
                    if remaining < tiers.tight_below_pct
                    else "recover"
                )
                desired[f"{account.id}:{window.id}"] = (state, account, window)
        if not desired:
            return
        key = "usage.events.tiers"

        def read_states(session: Session, *, lock: bool = False) -> dict[str, str]:
            query = select(SqlPreference.value).where(
                SqlPreference.workspace_id == current_workspace_id(),
                SqlPreference.user_id == owner,
                SqlPreference.key == key,
            )
            if lock:
                query = query.with_for_update()
            value = session.scalar(query)
            return json.loads(value) if value is not None else {}

        def changes(
            states: dict[str, str],
        ) -> dict[str, tuple[str, UsageAccountView, UsageWindowView]]:
            return {
                identity: value
                for identity, value in desired.items()
                if states.get(identity, "recover") != value[0]
            }

        with self.sessions._read_session()("read_quota_tier_changes") as session:
            if not changes(read_states(session)):
                return

        def write(session: Session) -> None:
            self._lock(session, owner)
            # A locking re-read observes the winner even under MySQL repeatable read.
            states = read_states(session, lock=True)
            changed = changes(states)
            if not changed:
                return
            events = []
            for identity, (state, account, window) in changed.items():
                states[identity] = state
                events.append(
                    SourceEvent.model_validate(
                        {
                            "specversion": "1.0",
                            "id": change_event_id(),
                            "source": f"quota:{account.id}",
                            "subject": account.id,
                            "type": f"tier_{window.id}",
                            "severity": state,
                            "time": datetime.fromtimestamp(now, UTC).isoformat(),
                            "data": {
                                "window": window.label,
                                "remaining_pct": window.remaining_pct,
                            },
                        }
                    )
                )
            self.insert_events_in_session(session, owner, events)
            UsageSnapshotStore.write_in_session(session, owner, key, states)

        run_write_transaction(self.sessions._write_session(), "record_quota_tier_changes", write)

    @staticmethod
    def _case_sensitive_event_type(session: Session) -> SQLColumnExpression[str]:
        event_type = SqlUsageSourceEvent.type
        dialect = session.get_bind().dialect.name
        if dialect == "mysql":
            return event_type.collate("utf8mb4_0900_bin")
        if dialect == "sqlite":
            return event_type.collate("BINARY")
        return event_type

    def event_neighbor(
        self, owner: str, row: dict[str, Any], *, after: bool
    ) -> dict[str, Any] | None:
        table = SqlUsageSourceEvent
        query = select(table).where(
            *self.scope(table, owner),
            table.account_id == (row["account"] or ""),
            table.source == row["source"],
            table.severity.in_(("error", "warn", "recover")),
        )
        later = (table.time_utc > row["time"]) | (
            (table.time_utc == row["time"]) & (table.event_id > row["id"])
        )
        earlier = (table.time_utc < row["time"]) | (
            (table.time_utc == row["time"]) & (table.event_id < row["id"])
        )
        query = (
            query.where(later if after else earlier)
            .order_by(
                table.time_utc.asc() if after else table.time_utc.desc(),
                table.event_id.asc() if after else table.event_id.desc(),
            )
            .limit(1)
        )
        with self.sessions._read_session()("pair_source_event") as session:
            query = query.where(self._case_sensitive_event_type(session) == row["type"])
            match = session.scalar(query)
            return (
                None
                if match is None
                else {"id": match.event_id, "time": match.time_utc, "severity": match.severity}
            )

    def read_sources(self, owner: str, account: str | None = None) -> list[dict[str, Any]]:
        table = SqlUsageSourceStatus
        query = select(table).where(*self.scope(table, owner))
        if account is not None:
            query = query.where(table.account_id == account)
        with self.sessions._read_session()("read_source_status") as session:
            return [
                dict(
                    account=row.account_id or None,
                    **{
                        name: getattr(row, name)
                        for name in (
                            "source",
                            "state",
                            "last_ok",
                            "last_error_at",
                            "observed_at",
                            "fresh_until",
                            "updated_at",
                        )
                    },
                )
                for row in session.scalars(query.order_by(table.account_id, table.source))
            ]

    def read_events(
        self,
        owner: str,
        *,
        account: str | None = None,
        source: str | None = None,
        severity: str | None = None,
        before: tuple[int, str, str] | None = None,
        limit: int = 50,
        kind: str | None = None,
    ) -> tuple[list[dict[str, Any]], int | None, int]:
        table = SqlUsageSourceEvent
        query = select(table).where(*self.scope(table, owner))
        for column, value in (
            (table.account_id, account),
            (table.source, source),
            (table.severity, severity),
        ):
            if value is not None:
                query = query.where(column == value)
        with self.sessions._read_session()("read_source_events") as session:
            event_type = self._case_sensitive_event_type(session)
            query = query.where(
                ~(
                    table.source.startswith("report:", autoescape=True)
                    & (event_type == "cloudevent")
                )
            )
            if kind == "disconnect":
                query = query.where(event_type == "connection")
            elif kind is not None:
                query = query.where(
                    event_type != "connection",
                    table.severity.in_(
                        {
                            "error": ("error",),
                            "warn": ("warn",),
                            "update": ("info", "recover"),
                        }[kind]
                    ),
                )
            filtered = query.subquery()
            oldest, stored = session.execute(
                select(func.min(filtered.c.time_utc), func.count()).select_from(filtered)
            ).one()
            if before is not None:
                stamp, source_key, event_id = before
                query = query.where(
                    (table.time_utc < stamp)
                    | ((table.time_utc == stamp) & (table.source < source_key))
                    | (
                        (table.time_utc == stamp)
                        & (table.source == source_key)
                        & (table.event_id < event_id)
                    )
                )
            rows = session.scalars(
                query.order_by(
                    table.time_utc.desc(), table.source.desc(), table.event_id.desc()
                ).limit(limit + 1)
            )
            return (
                [
                    {
                        "id": row.event_id,
                        "source": row.source,
                        "type": row.type,
                        "time": row.time_utc,
                        "account": row.account_id or None,
                        "severity": row.severity,
                        "data": row.data,
                    }
                    for row in rows
                ],
                oldest,
                stored,
            )

    @staticmethod
    def delete_in_session(session: Session, owner: str, account: str | None = None) -> None:
        for table in EVIDENCE_TABLES:
            query = delete(table).where(*UsageEvidenceStore.scope(table, owner))
            if account is not None:
                query = query.where(table.account_id == account)
            session.execute(query)

    def delete_account(self, owner: str, account: str) -> None:
        run_write_transaction(
            self.sessions._write_session(),
            "remove_account_evidence",
            lambda session: self.delete_in_session(session, owner, account),
        )

    def delete_owner(self, owner: str) -> None:
        run_write_transaction(
            self.sessions._write_session(),
            "remove_owner_evidence",
            lambda session: self.delete_in_session(session, owner),
        )

    def prune(self, before: Mapping[str, int]) -> Mapping[str, int]:
        def write(session: Session) -> dict[str, int]:
            result = {}
            for table, column in (
                (SqlUsageSourceEvent, SqlUsageSourceEvent.time_utc),
                (SqlUsageSourceStatus, SqlUsageSourceStatus.updated_at),
                (SqlUsageSpeedPoint, SqlUsageSpeedPoint.end_utc),
                (SqlUsagePriceEvidence, SqlUsagePriceEvidence.hour_utc),
                (SqlUsagePriceAliases, SqlUsagePriceAliases.updated_at),
            ):
                if table.__tablename__ in before:
                    result[table.__tablename__] = cast(
                        CursorResult[Any],
                        session.execute(
                            delete(table).where(
                                column < before[table.__tablename__],
                            )
                        ),
                    ).rowcount
            return result

        return run_write_transaction(self.sessions._write_session(), "prune_usage_evidence", write)
