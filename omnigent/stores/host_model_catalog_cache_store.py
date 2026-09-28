"""Persistent cache of the model catalogs hosts answer for each harness.

``/v1/calling-defaults/sync`` records each online owned host's
``host.model_options`` result here; the catalogs route and the resolve
route's offered check read it back instead of blocking on a live tunnel.
A failed sync keeps the previous payload and stores the error. Sync
SQLAlchemy; callers dispatch to a worker thread.
"""

from __future__ import annotations

import builtins
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from omnigent.db.db_models import SqlHostModelCatalogCache, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)

_EMPTY_PAYLOAD = "[]"


def _to_record(row: SqlHostModelCatalogCache) -> dict[str, Any]:
    """Convert a cache row to a ``{host_id, harness, models, fetched_at, error}`` dict."""
    try:
        decoded = json.loads(row.payload)
    except (TypeError, ValueError):
        # A payload the app never writes (manual edit / future format) must
        # read as "no models", not break the catalogs route.
        decoded = None
    return {
        "host_id": row.host_id,
        "harness": row.harness,
        "models": decoded if isinstance(decoded, list) else [],
        "fetched_at": row.fetched_at,
        "error": row.error,
    }


class HostModelCatalogCacheStore:
    """SQLAlchemy repository for per-host harness model catalogs."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._dialect = self._engine.dialect.name
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.host_model_catalog_cache_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.host_model_catalog_cache_store",
            immediate=True,
        )

    def list(self, host_ids: list[str]) -> list[dict[str, Any]]:
        """Return every cached catalog for *host_ids* in the active workspace."""
        unique_ids = list(dict.fromkeys(host_ids))
        if not unique_ids:
            return []
        with self._session("list_host_model_catalog_cache") as session:
            rows = (
                session.execute(
                    select(SqlHostModelCatalogCache)
                    .where(
                        SqlHostModelCatalogCache.workspace_id == current_workspace_id(),
                        SqlHostModelCatalogCache.host_id.in_(unique_ids),
                    )
                    .order_by(
                        SqlHostModelCatalogCache.host_id,
                        SqlHostModelCatalogCache.harness,
                    )
                )
                .scalars()
                .all()
            )
            return [_to_record(row) for row in rows]

    def upsert(
        self,
        host_id: str,
        harness: str,
        payload: builtins.list[dict[str, Any]],
        fetched_at: int,
    ) -> None:
        """Store a successful sync's catalog and clear any previous error."""
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        values = {
            "workspace_id": current_workspace_id(),
            "host_id": host_id,
            "harness": harness,
            "payload": encoded,
            "fetched_at": fetched_at,
            "error": None,
        }

        def write(session: Session) -> None:
            self._upsert_row(session, values, clear_error=True)

        run_write_transaction(self._session_immediate, "upsert_host_model_catalog_cache", write)

    def mark_error(self, host_id: str, harness: str, error: str) -> None:
        """Record a failed sync, keeping any previous payload and fetch time."""
        values = {
            "workspace_id": current_workspace_id(),
            "host_id": host_id,
            "harness": harness,
            # Insert branch only: an existing row keeps its payload/fetched_at.
            "payload": _EMPTY_PAYLOAD,
            "fetched_at": None,
            "error": error,
        }

        def write(session: Session) -> None:
            self._upsert_row(session, values, clear_error=False)

        run_write_transaction(self._session_immediate, "mark_host_model_catalog_error", write)

    def _upsert_row(self, session: Session, values: dict[str, Any], *, clear_error: bool) -> None:
        """Write one row with the backend's native upsert.

        :param session: Active write session.
        :param values: Full insert values.
        :param clear_error: ``True`` updates payload / fetched_at / error
            (a successful sync); ``False`` updates only ``error`` so the
            last good payload survives a failed one.
        """
        if self._dialect == "mysql":
            insert = mysql_insert(SqlHostModelCatalogCache).values(**values)
            if clear_error:
                stmt: Insert = insert.on_duplicate_key_update(
                    payload=values["payload"],
                    fetched_at=values["fetched_at"],
                    error=None,
                )
            else:
                stmt = insert.on_duplicate_key_update(error=values["error"])
        else:
            insert_fn = sqlite_insert if self._dialect == "sqlite" else pg_insert
            insert = insert_fn(SqlHostModelCatalogCache).values(**values)
            if clear_error:
                stmt = insert.on_conflict_do_update(
                    index_elements=["workspace_id", "host_id", "harness"],
                    set_={
                        "payload": values["payload"],
                        "fetched_at": values["fetched_at"],
                        "error": None,
                    },
                )
            else:
                stmt = insert.on_conflict_do_update(
                    index_elements=["workspace_id", "host_id", "harness"],
                    set_={"error": values["error"]},
                )
        session.execute(stmt)
