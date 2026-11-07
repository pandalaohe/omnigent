"""Task body replacement and independent history append in one owner-scoped transaction."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
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
    closed_after: str | None = None
    closed_before: str | None = None
    include_gone: bool = False
    include_history: bool = False
    changed_after: str | None = None
    read_at: float | None = None
    cursor: str | None = None
    limit: int = 1000


@dataclass(frozen=True)
class HistoryQuery:
    source: str | None = None
    after: str | None = None
    before: str | None = None
    cursor: str | None = None
    limit: int = 500


def _read_cursor(owner: str, kind: str, query: Any) -> tuple[str, dict[str, Any]]:
    filters = {
        key: value
        for key, value in asdict(query).items()
        if key not in {"cursor", "limit", "read_at"}
    }
    fingerprint = hashlib.sha256(
        json.dumps([current_workspace_id(), owner, kind, filters], sort_keys=True).encode()
    ).hexdigest()
    if not query.cursor:
        return fingerprint, {}
    if len(query.cursor) > 4096:
        raise ReportError("too_long", "cursor", "Cursor exceeds the length limit")
    try:
        cursor = json.loads(base64.b64decode(query.cursor, altchars=b"-_", validate=True))
        if not isinstance(cursor, dict) or cursor["query"] != fingerprint:
            raise ValueError
        position = cursor["position"]
        expected = 3 if kind == "changes" else 4
        if (
            not isinstance(position, list)
            or len(position) != expected
            or not all(isinstance(v, str) for v in position)
        ):
            raise ValueError
        if kind in {"items", "changes"}:
            utc_timestamp(cursor["through"])
        return fingerprint, cursor
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, RecursionError):
        raise ReportError("bad_value", "cursor", "Invalid cursor for this query") from None


def _next_cursor(fingerprint: str, position: tuple[str, ...], **metadata: Any) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"query": fingerprint, "position": position, **metadata}).encode()
    ).decode()


def _in_time_window(value: str | None, after: str | None, before: str | None) -> bool:
    return (
        value is not None
        and (after is None or utc_sort_key(value) >= utc_sort_key(after))
        and (before is None or utc_sort_key(value) < utc_sort_key(before))
    )


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
        self, session: Session, owner: str, source_id: str, now: float | None
    ) -> SqlTaskBoardSource:
        self._lock_owner(session, owner)
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
                SqlTaskBoardSource.source_id != "",
            )
        )
        if count is not None and count >= self.limits.max_sources:
            raise ReportError("source_limit", "", "Source limit reached", status=409)
        values = {
            "workspace_id": current_workspace_id(),
            "owner_id": owner,
            "source_id": source_id,
            "created_at": now if now is not None else datetime.now(UTC).timestamp(),
            "issues": [],
        }
        self._insert_source(session, values)
        return session.scalars(select(SqlTaskBoardSource).where(*scope).with_for_update()).one()

    @staticmethod
    def _insert_source(session: Session, values: dict[str, Any]) -> None:
        dialect = session.get_bind().dialect.name
        if dialect == "mysql":
            statement = (
                mysql_insert(SqlTaskBoardSource)
                .values(**values)
                .on_duplicate_key_update(source_id=values["source_id"])
            )
        elif dialect == "sqlite":
            statement = sqlite_insert(SqlTaskBoardSource).values(**values).on_conflict_do_nothing()
        else:
            statement = pg_insert(SqlTaskBoardSource).values(**values).on_conflict_do_nothing()
        session.execute(statement)

    def _lock_owner(self, session: Session, owner: str) -> None:
        # Empty source ids are reserved for the owner's transaction lock, never feeds.
        self._insert_source(
            session,
            {
                "workspace_id": current_workspace_id(),
                "owner_id": owner,
                "source_id": "",
                "created_at": 0,
                "issues": [],
            },
        )
        session.scalars(
            select(SqlTaskBoardSource)
            .where(
                *self._scope(SqlTaskBoardSource, owner),
                SqlTaskBoardSource.source_id == "",
            )
            .with_for_update()
        ).one()

    def write(
        self, owner: str, source_id: str, report: Report, now: datetime | None = None
    ) -> WriteOutcome:

        def apply(session: Session) -> WriteOutcome:
            source = self._source(session, owner, source_id, now.timestamp() if now else None)
            timestamp = now.timestamp() if now is not None else datetime.now(UTC).timestamp()
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
            if report.group is not None and (complete := report.group.get("omnigent.ai:complete")):
                version = report.group["updated"]
                if source.complete_version is not None and utc_sort_key(version) < utc_sort_key(
                    source.complete_version
                ):
                    outcome.complete = "skipped_older"
                else:
                    present = {task["uid"] for task in report.tasks}
                    for item in items.values():
                        if (
                            item.uid not in present
                            and item.gone_at is None
                            and (complete == "all" or item.progress in OPEN_PROGRESS)
                        ):
                            item.gone_at = item.changed_at = timestamp
                            outcome.gone += 1
                    source.complete_updated = utc_timestamp(version)
                    source.complete_version = version
                    outcome.complete = "applied"
            source.last_write = {"time": utc_text(timestamp), **asdict(outcome)}
            return outcome

        return run_write_transaction(self._writes, "report_tasks", apply)

    def list_items(self, owner: str, query: ItemQuery) -> tuple[list[dict[str, Any]], str | None]:
        changes = query.changed_after is not None
        fingerprint, cursor = _read_cursor(owner, "changes" if changes else "items", query)
        through = cursor.get(
            "through",
            utc_text(
                query.read_at if query.read_at is not None else datetime.now(UTC).timestamp()
            ),
        )
        with self._reads("read_tasks") as session:
            statement = select(SqlTaskBoardItem).where(
                *self._scope(SqlTaskBoardItem, owner),
                SqlTaskBoardItem.changed_at <= utc_timestamp(through),
            )
            if query.source is not None:
                statement = statement.where(SqlTaskBoardItem.source_id == query.source)
            if query.group is not None:
                statement = statement.where(SqlTaskBoardItem.group_id == query.group)
            if not query.include_gone and not changes:
                statement = statement.where(SqlTaskBoardItem.gone_at.is_(None))
            if query.changed_after is not None:
                statement = statement.where(
                    SqlTaskBoardItem.changed_at >= utc_timestamp(query.changed_after)
                )
            if query.state != "all":
                is_open = SqlTaskBoardItem.progress.in_(OPEN_PROGRESS)
                statement = statement.where(is_open if query.state == "open" else ~is_open)
            rows = list(session.scalars(statement))
            if query.changed_after is not None:
                rows = [
                    row
                    for row in rows
                    if utc_sort_key(utc_text(row.changed_at)) >= utc_sort_key(query.changed_after)
                ]
            if query.closed_after is not None or query.closed_before is not None:
                rows = [
                    row
                    for row in rows
                    if _in_time_window(
                        self._closed_time(row), query.closed_after, query.closed_before
                    )
                ]

            def position(row: SqlTaskBoardItem) -> tuple[str, ...]:
                if changes:
                    return utc_sort_key(utc_text(row.changed_at)), row.source_id, row.uid
                return (
                    row.source_id,
                    row.group_id,
                    utc_sort_key(row.body.get("created", "")),
                    row.uid,
                )

            rows.sort(key=position)
            if cursor:
                rows = [row for row in rows if position(row) > tuple(cursor["position"])]
            page = rows[: query.limit]
            entries = self._entries(session, owner, page, history=query.include_history)
            next_cursor = None
            if len(rows) > query.limit:
                next_cursor = _next_cursor(fingerprint, position(page[-1]), through=through)
            return entries, next_cursor

    @staticmethod
    def _closed_time(row: SqlTaskBoardItem) -> str | None:
        if row.progress in OPEN_PROGRESS:
            return None
        if "progressUpdated" in row.body:
            return row.body["progressUpdated"]
        return utc_text(row.closed_seen_at) if row.closed_seen_at is not None else None

    def _entries(
        self, session: Session, owner: str, rows: list[SqlTaskBoardItem], *, history: bool
    ) -> list[dict[str, Any]]:
        histories: dict[tuple[str, str], list[dict[str, Any]]] = {}
        identities = {(row.source_id, row.uid) for row in rows}
        if history and identities:
            for entry in session.scalars(
                select(SqlTaskBoardHistory).where(*self._scope(SqlTaskBoardHistory, owner))
            ):
                if (entry.source_id, entry.uid) in identities:
                    histories.setdefault((entry.source_id, entry.uid), []).append(entry.entry)
            for entries in histories.values():
                entries.sort(key=lambda entry: (utc_sort_key(entry["time"]), entry["id"]))
        targets = {uid for row in rows for uid in row.body.get("relatedTo", {})}
        found: set[str] = set()
        # Bound IN lists for SQLite and database-driver parameter limits.
        target_list = sorted(targets)
        for offset in range(0, len(target_list), 500):
            found.update(
                session.scalars(
                    select(SqlTaskBoardItem.uid).where(
                        *self._scope(SqlTaskBoardItem, owner),
                        SqlTaskBoardItem.uid.in_(target_list[offset : offset + 500]),
                    )
                )
            )
        result = []
        for row in rows:
            item = {**row.body, "progress": row.progress, "omnigent.ai:source": row.source_id}
            if row.gone_at is not None:
                item["omnigent.ai:gone"] = {"at": utc_text(row.gone_at)}
            if row.closed_seen_at is not None and "progressUpdated" not in row.body:
                item["omnigent.ai:closedSeen"] = utc_text(row.closed_seen_at)
            unresolved = [uid for uid in row.body.get("relatedTo", {}) if uid not in found]
            if unresolved:
                item["omnigent.ai:unresolved"] = unresolved
            if history:
                item["omnigent.ai:history"] = histories.get((row.source_id, row.uid), [])
            result.append(item)
        return result

    def get_item(self, owner: str, source: str, uid: str) -> dict[str, Any] | None:
        with self._reads("read_task") as session:
            row = session.scalar(
                select(SqlTaskBoardItem).where(
                    *self._scope(SqlTaskBoardItem, owner),
                    SqlTaskBoardItem.source_id == source,
                    SqlTaskBoardItem.uid == uid,
                )
            )
            return self._entries(session, owner, [row], history=True)[0] if row else None

    def list_history(
        self, owner: str, query: HistoryQuery
    ) -> tuple[list[dict[str, Any]], str | None]:
        fingerprint, cursor = _read_cursor(owner, "history", query)
        with self._reads("read_task_history") as session:
            statement = select(SqlTaskBoardHistory).where(*self._scope(SqlTaskBoardHistory, owner))
            if query.source is not None:
                statement = statement.where(SqlTaskBoardHistory.source_id == query.source)

            def position(row: SqlTaskBoardHistory) -> tuple[str, ...]:
                return utc_sort_key(row.entry["time"]), row.source_id, row.uid, row.entry_id

            rows = sorted(session.scalars(statement), key=position, reverse=True)
            rows = [
                row
                for row in rows
                if _in_time_window(row.entry["time"], query.after, query.before)
            ]
            if cursor:
                rows = [row for row in rows if position(row) < tuple(cursor["position"])]
            page = rows[: query.limit]
            return [
                {**row.entry, "source": row.source_id, "uid": row.uid} for row in page
            ], _next_cursor(fingerprint, position(page[-1])) if len(rows) > query.limit else None

    def closed_counts(
        self,
        owner: str,
        *,
        after: str | None,
        before: str | None,
        offset: int,
        source: str | None,
    ) -> list[dict[str, Any]]:
        with self._reads("count_closed_tasks") as session:
            statement = select(SqlTaskBoardItem).where(
                *self._scope(SqlTaskBoardItem, owner),
                SqlTaskBoardItem.gone_at.is_(None),
                ~SqlTaskBoardItem.progress.in_(OPEN_PROGRESS),
            )
            if source is not None:
                statement = statement.where(SqlTaskBoardItem.source_id == source)
            counts: dict[tuple[str, str], int] = {}
            for row in session.scalars(statement):
                closed = self._closed_time(row)
                if closed is not None and _in_time_window(closed, after, before):
                    day = (datetime.fromisoformat(closed) + timedelta(minutes=offset)).date()
                    key = day.isoformat(), row.source_id
                    counts[key] = counts.get(key, 0) + 1
            return [
                {"day": day, "source": source_id, "closed": count}
                for (day, source_id), count in sorted(counts.items())
            ]

    def delete_source(self, owner: str, source_id: str) -> bool:
        if not source_id:
            return False

        def remove(session: Session) -> bool:
            self._lock_owner(session, owner)
            source = session.scalar(
                select(SqlTaskBoardSource)
                .where(
                    *self._scope(SqlTaskBoardSource, owner),
                    SqlTaskBoardSource.source_id == source_id,
                )
                .with_for_update()
            )
            if source is None:
                return False
            for model in (SqlTaskBoardHistory, SqlTaskBoardItem, SqlTaskBoardSource):
                session.execute(
                    delete(model).where(*self._scope(model, owner), model.source_id == source_id)
                )
            return True

        return run_write_transaction(self._writes, "delete_task_source", remove)

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
                        SqlTaskBoardSource.source_id != "",
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
