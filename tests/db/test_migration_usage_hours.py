"""The usage_hours migration, and startup against a store that already has it.

Two properties matter beyond the schema itself. Startup must bring an existing
store up to head (deploying a build whose only change is this table), and a
store the build does not know must still be refused rather than run against a
schema it cannot interpret. Both run on whatever database the lane selects —
SQLite by default, PostgreSQL, CockroachDB or MySQL under
``OMNIGENT_TEST_DB_URI`` — because the upsert and the composite primary key are
the parts a dialect can break.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import (
    _build_alembic_config,
    _initialize_or_verify_schema,
    get_or_create_engine,
)
from omnigent.stores.usage_hours_store import UsageHoursStore, hours_of

_REVISION = "zb1c2d3e4f5a"
_PARENT_REVISION = "a53c20261103"


def _alembic_command(uri: str, engine: sa.Engine, direction: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        if direction == "upgrade":
            command.upgrade(config, revision)
        else:
            command.downgrade(config, revision)


def test_usage_hours_is_on_the_single_head() -> None:
    """One head, and this migration is in the lineage that head runs."""
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, got {heads!r}"
    lineage = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
    assert _REVISION in lineage, "usage_hours migration is not an ancestor of head"
    assert script.get_revision(_REVISION).down_revision == _PARENT_REVISION


def test_migrated_store_has_the_table_and_its_key(db_uri) -> None:
    """The lane's migrated database carries the table, its columns and index."""
    engine = get_or_create_engine(db_uri)
    inspector = sa.inspect(engine)
    assert "usage_hours" in inspector.get_table_names()

    columns = {column["name"] for column in inspector.get_columns("usage_hours")}
    assert columns == {
        "workspace_id",
        "owner_id",
        "account_id",
        "hour_utc",
        "model",
        "host_id",
        "basis",
        "priced",
        "input_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "output_tokens",
        "total_tokens",
        "requests",
        "failed",
        "reasons",
        "cost_usd",
        "updated_at",
        "reasoning_tokens",
        "coverage",
    }
    primary_key = inspector.get_pk_constraint("usage_hours")["constrained_columns"]
    assert primary_key == [
        "workspace_id",
        "owner_id",
        "account_id",
        "hour_utc",
        "model",
        "host_id",
        "basis",
        "priced",
    ]
    indexes = {index["name"] for index in inspector.get_indexes("usage_hours")}
    assert "ix_usage_hours_owner_hour" in indexes


def test_upsert_and_range_read_work_on_this_dialect(db_uri) -> None:
    """The composite-key upsert and a range read behave on the lane's engine."""
    store = UsageHoursStore(db_uri)
    hour = hours_of(1_800_000_000)
    store.add_turn(
        "owner@example.test",
        "native:codex",
        hour,
        "model",
        "host-1",
        "metered",
        True,
        10,
        0,
        0,
        2,
        12,
        0.3,
    )
    store.add_turn(
        "owner@example.test",
        "native:codex",
        hour,
        "model",
        "host-1",
        "metered",
        True,
        5,
        0,
        0,
        1,
        6,
        0.2,
    )
    [row] = store.read("owner@example.test", hour, hour + 3600)
    assert (row.input_tokens, row.total_tokens, row.cost_usd) == (15, 18, 0.5)


def test_startup_accepts_a_store_that_already_has_the_migration(db_uri) -> None:
    """A kept-migration application starts: at head, the table simply unused."""
    engine = get_or_create_engine(db_uri)
    _initialize_or_verify_schema(engine, db_uri)

    assert "usage_hours" in sa.inspect(engine).get_table_names()
    # Nothing was recorded, and nothing needs to be: an older build that keeps
    # the migration leaves the table empty and continues.
    store = UsageHoursStore(db_uri)
    assert store.read("owner@example.test", 0, 4_000_000_000) == []


def test_startup_refuses_a_revision_this_build_cannot_interpret(db_uri) -> None:
    """An unknown newer revision still stops startup instead of migrating."""
    engine = get_or_create_engine(db_uri)
    unknown = "zz0000000000"
    with engine.begin() as conn:
        original = [
            row[0] for row in conn.execute(sa.text("SELECT version_num FROM alembic_version"))
        ]
        conn.execute(sa.text("DELETE FROM alembic_version"))
        conn.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": unknown},
        )
    try:
        try:
            _initialize_or_verify_schema(engine, db_uri)
        except RuntimeError as error:
            assert unknown in str(error)
        else:  # pragma: no cover - the guard regressed
            raise AssertionError("startup accepted a revision this build does not know")
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM alembic_version"))
            for revision in original:
                conn.execute(
                    sa.text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                    {"revision": revision},
                )


def test_downgrade_drops_the_table(tmp_path) -> None:
    """The downgrade removes the table and the index it created."""
    uri = f"sqlite:///{tmp_path / 'usage-hours.db'}"
    # A raw engine: ``get_or_create_engine`` migrates on first use, which would
    # hide the downgrade being tested.
    engine = sa.create_engine(uri)

    _alembic_command(uri, engine, "upgrade", "head")
    assert "usage_hours" in sa.inspect(engine).get_table_names()

    _alembic_command(uri, engine, "downgrade", _PARENT_REVISION)
    inspector = sa.inspect(engine)
    assert "usage_hours" not in inspector.get_table_names()
    assert "ix_usage_hours_owner_hour" not in {
        index["name"]
        for table in inspector.get_table_names()
        for index in inspector.get_indexes(table)
    }


def test_startup_upgrades_an_existing_store_from_the_previous_revision(tmp_path) -> None:
    """An existing store behind this revision is migrated, then usable."""
    uri = f"sqlite:///{tmp_path / 'existing.db'}"
    engine = sa.create_engine(uri)
    _alembic_command(uri, engine, "upgrade", _PARENT_REVISION)
    assert "usage_hours" not in sa.inspect(engine).get_table_names()

    _initialize_or_verify_schema(engine, uri)

    assert "usage_hours" in sa.inspect(engine).get_table_names()
    store = UsageHoursStore(uri)
    hour = hours_of(1_800_000_000)
    store.add_turn(
        "owner@example.test", "native:codex", hour, "m", "h", "metered", True, 1, 0, 0, 1, 2, 0.1
    )
    assert len(store.read("owner@example.test", hour, hour + 3600)) == 1
