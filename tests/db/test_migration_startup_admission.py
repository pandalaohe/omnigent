"""Startup migrations round-trip and backfill grants with a portable JSON default."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, get_or_create_engine


def test_populated_startup_migrations_round_trip(db_uri):
    engine = get_or_create_engine(db_uri)
    config = _build_alembic_config(db_uri)
    command.downgrade(config, "a36c20261020")
    startups = sa.Table("session_startups", sa.MetaData(), autoload_with=engine)
    identity = uuid4().bytes
    with engine.begin() as connection:
        connection.execute(
            startups.insert().values(
                workspace_id=0,
                id=identity,
                owner_user_id="owner",
                host_id=uuid4().bytes,
                session_id=uuid4().bytes,
                operation_key="existing-start",
                runtime_generation=1,
                family_id=uuid4().bytes,
                sequence=1,
                state="uncertain",
                occupied=True,
                resident=True,
                cancel_requested=False,
                created_at=100,
                updated_at=100,
            )
        )
    command.upgrade(config, "head")
    startups = sa.Table("session_startups", sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        row = connection.execute(sa.select(startups).where(startups.c.id == identity)).one()
        assert row.cold_claims == {}
        assert row.state == "uncertain" and row.occupied and row.resident
        assert row.admitted_at is None and row.first_dispatch_at is None
    column = next(
        item
        for item in sa.inspect(engine).get_columns("session_startups")
        if item["name"] == "cold_claims"
    )
    assert not column["nullable"]
    command.downgrade(config, "a33c20261020")
    assert not {"session_startups", "session_startup_budgets"} & set(
        sa.inspect(engine).get_table_names()
    )
    command.upgrade(config, "head")
    assert "session_startups" in sa.inspect(engine).get_table_names()
