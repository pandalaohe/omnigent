"""Latest usage readings in the existing workspace-scoped preferences table."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import LargeBinary, delete, select, type_coerce
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from omnigent.db.compression import decode
from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    NamedManagedSessionMaker,
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)

_PREFIX = "usage."
_MAX_DECODED_BYTES = 4 * 1024 * 1024


class UsageSnapshotStore:
    """Lazy synchronous access to one owner's ``usage.*`` preference rows."""

    def __init__(self, storage_location: str) -> None:
        self._storage_location = storage_location
        self._read_sessions: NamedManagedSessionMaker | None = None
        self._write_sessions: NamedManagedSessionMaker | None = None

    def _read_session(self) -> NamedManagedSessionMaker:
        if self._read_sessions is None:
            self._read_sessions = make_named_managed_session_maker(
                get_or_create_engine(self._storage_location),
                query_name_prefix="omnigent.usage_snapshot_store",
            )
        return self._read_sessions

    def _write_session(self) -> NamedManagedSessionMaker:
        if self._write_sessions is None:
            self._write_sessions = make_named_managed_session_maker(
                get_or_create_engine(self._storage_location),
                query_name_prefix="omnigent.usage_snapshot_store",
                immediate=True,
            )
        return self._write_sessions

    def read(self, owner: str) -> dict[str, dict[str, Any]]:
        with self._read_session()("read_usage_snapshots") as session:
            rows = session.execute(
                select(
                    SqlPreference.key,
                    type_coerce(SqlPreference.value, LargeBinary),
                ).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == owner,
                    SqlPreference.key >= _PREFIX,
                    SqlPreference.key < "usage;",
                )
            ).all()
        result: dict[str, dict[str, Any]] = {}
        for key, raw in rows:
            text = decode(raw, max_decoded_bytes=_MAX_DECODED_BYTES)
            value = json.loads(text or "")
            if not isinstance(value, dict):
                raise ValueError(f"usage snapshot {key} is not an object")
            result[key] = value
        return result

    def write(self, owner: str, key: str, record: dict[str, Any]) -> None:
        value = json.dumps(record, separators=(",", ":"), allow_nan=False)

        def write(session: Session) -> None:
            values = {
                "workspace_id": current_workspace_id(),
                "user_id": owner,
                "key": key,
                "value": value,
            }
            dialect = session.get_bind().dialect.name
            stmt: Insert
            if dialect == "mysql":
                stmt = (
                    mysql_insert(SqlPreference)
                    .values(**values)
                    .on_duplicate_key_update(value=value)
                )
            elif dialect == "sqlite":
                stmt = (
                    sqlite_insert(SqlPreference)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "user_id", "key"], set_={"value": value}
                    )
                )
            else:
                stmt = (
                    pg_insert(SqlPreference)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "user_id", "key"], set_={"value": value}
                    )
                )
            session.execute(stmt)

        run_write_transaction(self._write_session(), "write_usage_snapshot", write)

    def delete(self, owner: str, key: str) -> None:
        def remove(session: Session) -> None:
            session.execute(
                delete(SqlPreference).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == owner,
                    SqlPreference.key == key,
                )
            )

        run_write_transaction(self._write_session(), "delete_usage_snapshot", remove)
