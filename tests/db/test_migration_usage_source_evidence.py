"""Source evidence migration is additive and keeps its archive on code rollback."""

import sqlalchemy as sa

from omnigent.db.utils import get_or_create_engine
from tests.db.test_migration_usage_hours import _alembic_command


def test_evidence_migration_roundtrip(db_uri):
    engine = get_or_create_engine(db_uri)
    migration_engine = (
        engine.execution_options(isolation_level="SERIALIZABLE")
        if engine.dialect.name == "cockroachdb"
        else engine
    )
    try:
        _alembic_command(db_uri, migration_engine, "downgrade", "a66c20261112")
        assert "usage_source_events" not in sa.inspect(engine).get_table_names()
        assert "usage_pricing_cards" in sa.inspect(engine).get_table_names()
        _alembic_command(db_uri, migration_engine, "upgrade", "head")
        inspector = sa.inspect(engine)
        assert inspector.get_pk_constraint("usage_source_events")["constrained_columns"] == [
            "workspace_id",
            "owner_id",
            "source",
            "event_id",
        ]
        assert inspector.get_pk_constraint("usage_source_status")["constrained_columns"] == [
            "workspace_id",
            "owner_id",
            "account_id",
            "source",
        ]
        assert "ix_usage_source_events_account_time" in {
            row["name"] for row in inspector.get_indexes("usage_source_events")
        }
    finally:
        _alembic_command(db_uri, migration_engine, "upgrade", "head")
