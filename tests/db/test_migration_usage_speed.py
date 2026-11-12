"""The additive speed migration round trips without losing source evidence."""

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from omnigent.db.utils import get_or_create_engine
from tests.db.test_migration_usage_hours import _alembic_command


def test_speed_migration_roundtrip(db_uri):
    engine = get_or_create_engine(db_uri)
    migration_engine = (
        engine.execution_options(isolation_level="SERIALIZABLE")
        if engine.dialect.name == "cockroachdb"
        else engine
    )
    try:
        _alembic_command(db_uri, migration_engine, "downgrade", "a65c20261112")
        assert "usage_speed_points" not in sa.inspect(engine).get_table_names()
        assert "usage_source_events" in sa.inspect(engine).get_table_names()
        _alembic_command(db_uri, migration_engine, "upgrade", "head")
        assert sa.inspect(engine).get_pk_constraint("usage_speed_points")[
            "constrained_columns"
        ] == ["workspace_id", "owner_id", "account_id", "model", "metric", "start_utc", "end_utc"]
        config = Config()
        config.set_main_option("script_location", "omnigent/db/migrations")
        script = ScriptDirectory.from_config(config)
        assert script.get_heads() == ["a68c20261112"]
    finally:
        _alembic_command(db_uri, migration_engine, "upgrade", "head")
