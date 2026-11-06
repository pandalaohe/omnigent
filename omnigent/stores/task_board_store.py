"""Task body replacement and independent history append in one owner-scoped transaction."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlTaskBoardHistory,
    SqlTaskBoardItem,
    SqlTaskBoardSource,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.server.task_board.models import (
    OPEN_PROGRESS,
    Report,
    ReportError,
    TaskBoardLimits,
    WriteOutcome,
    task_progress,
    utc_sort_key,
    utc_text,
    utc_timestamp,
)


@dataclass(frozen=True)
class ItemQuery:
    source: str | None = None
    group: str | None = None
    state: str = "all"
    closed_after: float | None = None
    closed_before: float | None = None
    include_gone: bool = False
    include_history: bool = False
    cursor: str | None = None
    limit: int = 1000


class TaskBoardStore:
    def __init__(self, storage_location: str, limits: TaskBoardLimits | None = None) -> None:
        engine = get_or_create_engine(storage_location)
        self.limits = limits or TaskBoardLimits()
        self._reads = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.task_board"
        )
        self._writes = make_named_managed_session_maker(
            engine,
            query_name_prefix="omnigent.task_board",
            immediate=True,
        )

    @staticmethod
    def _scope(model: Any, owner: str) -> tuple[Any, ...]:
        return (model.workspace_id == current_workspace_id(), model.owner_id == owner)

    def _source(
        self, session: Session, owner: str, source_id: str, now: float
    ) -> SqlTaskBoardSource:
        scope = (
            *self._scope(SqlTaskBoardSource, owner),
            SqlTaskBoardSource.source_id == source_id,
        )
        source = session.scalar(select(SqlTaskBoardSource).where(*scope).with_for_update())
        if source is not None:
            return source
        count = session.scalar(
            select(func.count())
            .select_from(SqlTaskBoardSource)
            .where(
                *self._scope(SqlTaskBoardSource, owner),
            )
        )
        if count is not None and count >= self.limits.max_sources:
            raise ReportError("source_limit", "", "Source limit reached", status=409)
        values = {
            "workspace_id": current_workspace_id(),
            "owner_id": owner,
            "source_id": source_id,
            "created_at": now,
            "issues": [],
        }
        dialect = session.get_bind().dialect.name
        if dialect == "mysql":
            statement = (
                mysql_insert(SqlTaskBoardSource)
                .values(**values)
                .on_duplicate_key_update(source_id=source_id)
            )
        elif dialect == "sqlite":
            statement = sqlite_insert(SqlTaskBoardSource).values(**values).on_conflict_do_nothing()
        else:
            statement = pg_insert(SqlTaskBoardSource).values(**values).on_conflict_do_nothing()
        session.execute(statement)
        return session.scalars(select(SqlTaskBoardSource).where(*scope).with_for_update()).one()

    def write(self, owner: str, source_id: str, report: Report, now: datetime) -> WriteOutcome:
        timestamp = now.timestamp()

        def apply(session: Session) -> WriteOutcome:
            source = self._source(session, owner, source_id, timestamp)
            if report.group is not None:
                if "title" in report.group:
                    source.name = report.group["title"]
                if "source" in report.group:
                    source.source = report.group["source"]
            items = {
                row.uid: row
                for row in session.scalars(
                    select(SqlTaskBoardItem).where(
                        *self._scope(SqlTaskBoardItem, owner),
                        SqlTaskBoardItem.source_id == source_id,
                    )
                )
            }
            history_ids = set(
                session.execute(
                    select(SqlTaskBoardHistory.uid, SqlTaskBoardHistory.entry_id).where(
                        *self._scope(SqlTaskBoardHistory, owner),
                        SqlTaskBoardHistory.source_id == source_id,
                    )
                ).tuples()
            )
            outcome = WriteOutcome()
            for task in report.tasks:
                uid = task["uid"]
                body = {key: value for key, value in task.items() if key != "omnigent.ai:history"}
                updated, sequence = utc_timestamp(task["updated"]), task.get("sequence", 0)
                item = items.get(uid)
                incoming_version = (utc_sort_key(task["updated"]), sequence)
                stored_version = (
                    (utc_sort_key(item.body["updated"]), item.sequence) if item else ("", -1)
                )
                if item is None or incoming_version > stored_version:
                    if item is None:
                        item = SqlTaskBoardItem(
                            workspace_id=current_workspace_id(),
                            owner_id=owner,
                            source_id=source_id,
                            uid=uid,
                        )
                        session.add(item)
                    progress = task_progress(body)
                    if progress in OPEN_PROGRESS:
                        item.closed_seen_at = None
                        item.closed_at = None
                    else:
                        item.closed_seen_at = item.closed_seen_at or timestamp
                        item.closed_at = (
                            utc_timestamp(body["progressUpdated"])
                            if "progressUpdated" in body
                            else None
                        )
                    item.body, item.progress = body, progress
                    item.group_id = body.get("omnigent.ai:group", {}).get("id", "")
                    item.source_updated, item.sequence, item.changed_at = (
                        updated,
                        sequence,
                        timestamp,
                    )
                    outcome.applied += 1
                elif incoming_version == stored_version and body == item.body:
                    outcome.unchanged += 1
                else:
                    outcome.held.append({"uid": uid, "reason": "older"})
                if item.gone_at is not None:
                    item.gone_at, item.changed_at = None, timestamp
                    outcome.returned += 1
                for entry in task.get("omnigent.ai:history", []):
                    identity = (uid, entry["id"])
                    if identity in history_ids:
                        continue
                    session.add(
                        SqlTaskBoardHistory(
                            workspace_id=current_workspace_id(),
                            owner_id=owner,
                            source_id=source_id,
                            uid=uid,
                            entry_id=entry["id"],
                            time=utc_timestamp(entry["time"]),
                            kind=entry["kind"],
                            entry=entry,
                        )
                    )
                    history_ids.add(identity)
                    outcome.history_added += 1
                    item.changed_at = timestamp
            source.last_write = {"time": utc_text(timestamp), **asdict(outcome)}
            return outcome

        return run_write_transaction(self._writes, "report_tasks", apply)

    def list_items(self, owner: str, query: ItemQuery) -> tuple[list[dict[str, Any]], str | None]:
        filters = {
            key: value for key, value in asdict(query).items() if key not in {"cursor", "limit"}
        }
        fingerprint = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
        offset = 0
        if query.cursor:
            try:
                cursor = json.loads(base64.urlsafe_b64decode(query.cursor))
                offset = cursor["offset"]
                if cursor["query"] != fingerprint or type(offset) is not int or offset < 0:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise ReportError("bad_value", "cursor", "Invalid cursor for this query") from None
        with self._reads("read_tasks") as session:
            statement = select(SqlTaskBoardItem).where(*self._scope(SqlTaskBoardItem, owner))
            if query.source is not None:
                statement = statement.where(SqlTaskBoardItem.source_id == query.source)
            if query.group is not None:
                statement = statement.where(SqlTaskBoardItem.group_id == query.group)
            if not query.include_gone:
                statement = statement.where(SqlTaskBoardItem.gone_at.is_(None))
            if query.state != "all":
                is_open = SqlTaskBoardItem.progress.in_(OPEN_PROGRESS)
                statement = statement.where(is_open if query.state == "open" else ~is_open)
            for boundary, after in ((query.closed_after, True), (query.closed_before, False)):
                if boundary is not None:
                    closed_time = func.coalesce(
                        SqlTaskBoardItem.closed_at, SqlTaskBoardItem.closed_seen_at
                    )
                    statement = statement.where(
                        closed_time >= boundary if after else closed_time < boundary
                    )
            rows = list(session.scalars(statement))
            rows.sort(
                key=lambda row: (
                    row.source_id,
                    row.group_id,
                    utc_sort_key(row.body.get("created", "")),
                    row.uid,
                )
            )
            page = rows[offset : offset + query.limit]
            history: dict[tuple[str, str], list[dict[str, Any]]] = {}
            if query.include_history:
                identities = {(row.source_id, row.uid) for row in page}
                for row in session.scalars(
                    select(SqlTaskBoardHistory)
                    .where(
                        *self._scope(SqlTaskBoardHistory, owner),
                    )
                    .order_by(SqlTaskBoardHistory.time, SqlTaskBoardHistory.entry_id)
                ):
                    if (row.source_id, row.uid) in identities:
                        history.setdefault((row.source_id, row.uid), []).append(row.entry)
            entries = []
            for row in page:
                entry = {**row.body, "progress": row.progress, "omnigent.ai:source": row.source_id}
                if row.gone_at is not None:
                    entry["omnigent.ai:gone"] = {"at": utc_text(row.gone_at)}
                if query.include_history:
                    entry["omnigent.ai:history"] = history.get((row.source_id, row.uid), [])
                entries.append(entry)
            next_cursor = None
            if len(rows) > offset + query.limit:
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps({"query": fingerprint, "offset": offset + query.limit}).encode()
                ).decode()
            return entries, next_cursor

    def list_sources(self, owner: str) -> list[dict[str, Any]]:
        with self._reads("read_task_sources") as session:
            counts: dict[str, dict[str, int]] = {}
            for source_id, progress, gone_at, count in session.execute(
                select(
                    SqlTaskBoardItem.source_id,
                    SqlTaskBoardItem.progress,
                    SqlTaskBoardItem.gone_at,
                    func.count(),
                )
                .where(*self._scope(SqlTaskBoardItem, owner))
                .group_by(
                    SqlTaskBoardItem.source_id,
                    SqlTaskBoardItem.progress,
                    SqlTaskBoardItem.gone_at,
                )
            ):
                totals = counts.setdefault(source_id, {"open": 0, "closed": 0, "gone": 0})
                totals[
                    "gone"
                    if gone_at is not None
                    else "open"
                    if progress in OPEN_PROGRESS
                    else "closed"
                ] += count
            return [
                {
                    "id": row.source_id,
                    "name": row.name,
                    "source": row.source,
                    "created": utc_text(row.created_at),
                    "last_write": row.last_write,
                    "counts": counts.get(row.source_id, {"open": 0, "closed": 0, "gone": 0}),
                    "issues": row.issues,
                }
                for row in session.scalars(
                    select(SqlTaskBoardSource)
                    .where(
                        *self._scope(SqlTaskBoardSource, owner),
                    )
                    .order_by(SqlTaskBoardSource.source_id)
                )
            ]

    def record_issue(self, owner: str, source_id: str, error: ReportError, now: datetime) -> None:
        def record(session: Session) -> None:
            source = self._source(session, owner, source_id, now.timestamp())
            source.issues = [
                *source.issues,
                {"time": utc_text(now.timestamp()), "status": error.status, **error.payload()},
            ][-20:]

        run_write_transaction(self._writes, "record_report_issue", record)
