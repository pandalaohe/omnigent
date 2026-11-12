"""The additive pool migration preserves existing hourly usage on every backend."""

import sqlalchemy as sa
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, get_or_create_engine
from omnigent.stores.usage_hours_store import UsageHoursStore
from tests.db.test_migration_usage_hours import _alembic_command


def test_pool_migration_additive_roundtrip(db_uri):
    engine = get_or_create_engine(db_uri)
    migration_engine = (
        engine.execution_options(isolation_level="SERIALIZABLE")
        if engine.dialect.name == "cockroachdb"
        else engine
    )
    hours = UsageHoursStore(db_uri)
    hours.add_turn(
        "owner@example.test",
        "native:codex",
        1794304800,
        "model",
        "host",
        "metered",
        True,
        1,
        0,
        0,
        2,
        3,
        0.5,
    )
    try:
        _alembic_command(db_uri, migration_engine, "downgrade", "a63c20261111")
        assert "usage_account_relations" not in sa.inspect(engine).get_table_names()
        _alembic_command(db_uri, migration_engine, "upgrade", "head")
        assert "usage_account_relations" in sa.inspect(engine).get_table_names()
        assert hours.read("owner@example.test", 1794304800, 1794308400)[0].total_tokens == 3
        key = sa.inspect(engine).get_pk_constraint("usage_account_relations")[
            "constrained_columns"
        ]
        assert key == ["workspace_id", "owner_id", "account_id"]
        script = ScriptDirectory.from_config(_build_alembic_config(db_uri))
        assert script.get_heads() == ["a64c20261112"]
    finally:
        _alembic_command(db_uri, migration_engine, "upgrade", "head")
