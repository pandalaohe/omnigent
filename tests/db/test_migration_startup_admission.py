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
                runtime_ready=False,
                tools_ready=False,
                input_accepted=False,
                work_started=False,
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


def test_inventory_veto_migration_backfills_and_downgrades(db_uri):
    engine = get_or_create_engine(db_uri)
    config = _build_alembic_config(db_uri)
    command.downgrade(config, "a44c20261021")
    budgets = sa.Table("session_startup_budgets", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            budgets.insert().values(
                scope="legacy-host",
                resident_limit=7,
                startup_limit=2,
                next_sequence=5,
                fenced=False,
                work_revision=0,
                work_fence_epoch=0,
            )
        )
    command.upgrade(config, "head")
    budgets = sa.Table("session_startup_budgets", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        row = connection.execute(sa.select(budgets).where(budgets.c.scope == "legacy-host")).one()
        assert row.resident_inventory_unknown is False
        assert row.resident_limit == 7 and row.next_sequence == 5
        connection.execute(
            budgets.update()
            .where(budgets.c.scope == "legacy-host")
            .values(resident_inventory_unknown=True)
        )
    command.downgrade(config, "a44c20261021")
    budgets = sa.Table("session_startup_budgets", sa.MetaData(), autoload_with=engine)
    assert "resident_inventory_unknown" not in budgets.c
    inspector = sa.inspect(engine)
    assert {"archived_at", "archive_state"} <= {
        column["name"] for column in inspector.get_columns("projects")
    }
    assert "paused_by_archive_batch" in {
        column["name"] for column in inspector.get_columns("scheduled_tasks")
    }
    with engine.connect() as connection:
        row = connection.execute(sa.select(budgets).where(budgets.c.scope == "legacy-host")).one()
        assert row.resident_limit == 7 and row.next_sequence == 5
    command.upgrade(config, "head")
