"""Usage-feed schema and client defaults work on every supported DB backend."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
from omnigent.db.db_models import SqlUsageFeed
from omnigent.db.enum_codecs import encode_device_grant_status
from omnigent.db.utils import (
    _build_alembic_config,
    _get_current_db_revision,
    get_or_create_engine,
)


def _migrate(engine: sa.Engine, uri: str, *, downgrade: bool = False) -> None:
    config = _build_alembic_config(uri)
    with engine.connect() as connection:
        if engine.dialect.name == "cockroachdb":
            _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        config.attributes["connection"] = connection
        if downgrade:
            command.downgrade(config, "a31c20261019")
        else:
            command.upgrade(config, "a32c20261020")
        connection.commit()


def test_usage_feed_model_creates_and_defaults_progress(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    table = SqlUsageFeed.__table__.to_metadata(sa.MetaData(), name="usage_feed_defaults_probe")
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "cockroachdb":
                _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
            table.create(connection)
            connection.commit()
        with engine.begin() as connection:
            connection.execute(
                table.insert().values(
                    workspace_id=0,
                    owner_digest=b"o" * 32,
                    owner_scope_id="1" * 32,
                    feed_id="2" * 32,
                    feed_epoch=1,
                    signing_key=b"k" * 32,
                    next_sequence=1,
                    read_timestamps="[]",
                )
            )
            assert connection.execute(sa.select(table.c.progress)).scalar_one() == "{}"
    finally:
        with engine.connect() as connection:
            if engine.dialect.name == "cockroachdb":
                _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
            table.drop(connection, checkfirst=True)
            connection.commit()


def test_usage_feed_migration_backfills_scope_and_round_trips(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    _migrate(engine, db_uri, downgrade=True)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO device_grants "
                "(workspace_id,id,device_code_hash,user_code,status,client_id,"
                "created_at,expires_at) "
                "VALUES (0,:id,:digest,:code,:status,:client_id,1000,1600)"
            ),
            [
                {
                    "id": str(i),
                    "digest": str(i) * 64,
                    "code": f"CODE-{i}",
                    "status": encode_device_grant_status("redeemed"),
                    "client_id": client,
                }
                for i, client in enumerate([None, "slack", "omnigent-cli"], 1)
            ],
        )
    _migrate(engine, db_uri)
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT client_id,scope FROM device_grants ORDER BY id")
        ).all() == [(None, "sessions"), ("slack", "sessions"), ("omnigent-cli", None)]
    assert _get_current_db_revision(engine) == "a32c20261020"
    progress = next(
        c for c in sa.inspect(engine).get_columns("usage_feeds") if c["name"] == "progress"
    )
    assert not progress["nullable"]
    if engine.dialect.name == "mysql":
        assert progress["default"] is None
    _migrate(engine, db_uri, downgrade=True)
    assert "usage_feeds" not in sa.inspect(engine).get_table_names()
    assert "scope" not in {c["name"] for c in sa.inspect(engine).get_columns("device_grants")}
    _migrate(engine, db_uri)
    assert "usage_feeds" in sa.inspect(engine).get_table_names()


def test_usage_feed_upgrade_resumes_after_scope_schema_commit(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name != "cockroachdb":
        pytest.skip("CockroachDB publishes new columns at a separate commit")
    _migrate(engine, db_uri, downgrade=True)

    def interrupt_backfill(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE device_grants SET scope"):
            raise RuntimeError("injected scope backfill interruption")

    sa.event.listen(engine, "before_cursor_execute", interrupt_backfill)
    try:
        with pytest.raises(RuntimeError, match="injected scope backfill interruption"):
            _migrate(engine, db_uri)
    finally:
        sa.event.remove(engine, "before_cursor_execute", interrupt_backfill)
    assert _get_current_db_revision(engine) == "a31c20261019"
    assert "scope" in {c["name"] for c in sa.inspect(engine).get_columns("device_grants")}
    _migrate(engine, db_uri)
    assert _get_current_db_revision(engine) == "a32c20261020"
