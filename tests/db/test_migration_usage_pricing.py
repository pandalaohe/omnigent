"""The pricing migration expands schema without altering existing usage."""

import sqlalchemy as sa

from omnigent.db.utils import get_or_create_engine
from tests.db.test_migration_usage_hours import _alembic_command


def test_pricing_migration_additive_roundtrip(db_uri):
    engine = get_or_create_engine(db_uri)
    migration_engine = (
        engine.execution_options(isolation_level="SERIALIZABLE")
        if engine.dialect.name == "cockroachdb"
        else engine
    )
    try:
        _alembic_command(db_uri, migration_engine, "downgrade", "a63c20261112")
        assert "usage_pricing_cards" not in sa.inspect(engine).get_table_names()
        assert "usage_account_relations" in sa.inspect(engine).get_table_names()
        _alembic_command(db_uri, migration_engine, "upgrade", "head")
        for name in (
            "usage_pricing_defaults",
            "usage_pricing_cards",
            "usage_quota_closings",
            "usage_price_overrides",
        ):
            assert name in sa.inspect(engine).get_table_names()
        amount = next(
            column
            for column in sa.inspect(engine).get_columns("usage_pricing_cards")
            if column["name"] == "retail_amount"
        )
        assert amount["type"].precision == 38
        assert amount["type"].scale == 18
    finally:
        _alembic_command(db_uri, migration_engine, "upgrade", "head")


def test_account_price_and_native_ref_migrations(db_uri):
    engine = get_or_create_engine(db_uri)
    migration_engine = (
        engine.execution_options(isolation_level="SERIALIZABLE")
        if engine.dialect.name == "cockroachdb"
        else engine
    )
    try:
        _alembic_command(db_uri, migration_engine, "downgrade", "a68c20261112")
        assert "usage_price_overrides" not in sa.inspect(engine).get_table_names()
        assert "price_ref" not in {
            c["name"] for c in sa.inspect(engine).get_columns("usage_hours")
        }
        _alembic_command(db_uri, migration_engine, "upgrade", "head")
        columns = {c["name"]: c for c in sa.inspect(engine).get_columns("usage_price_overrides")}
        assert columns["read_rate"]["nullable"]
        assert columns["read_rate"]["type"].precision == 38
        assert columns["read_rate"]["type"].scale == 18
        assert next(
            c for c in sa.inspect(engine).get_columns("usage_hours") if c["name"] == "price_ref"
        )["nullable"]
    finally:
        _alembic_command(db_uri, migration_engine, "upgrade", "head")
