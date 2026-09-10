"""Tests for the connections migration and legacy custom-lineage repair.

The single-head test is the guard: a stacked PR whose migration chains off a
revision that isn't a real ancestor leaves the tree with two heads, and
``alembic upgrade head`` — which runs on every server boot and every DB-touching
test — then raises. Asserting a single head catches that before CI does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import (
    _build_alembic_config,
    _initialize_or_verify_schema,
    _run_migrations,
    clear_engine_cache,
)


def _upgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def _downgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, revision)


def test_single_alembic_head() -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert heads == ["a10c20260910"], f"expected a single head, got {heads!r}"


@pytest.mark.parametrize("manual", [False, True])
def test_legacy_custom_gc_collision_upgrades_without_data_loss(
    tmp_path: Path, manual: bool
) -> None:
    """A deployed custom ``gc1`` stamp reaches head through the repaired merge."""
    uri = f"sqlite:///{tmp_path / 'legacy-custom-gc.db'}"
    engine = sa.create_engine(uri)

    # Recreate the two schema branches that the historical custom ``gc1``
    # merge represented, then collapse their truthful stamps to its collided
    # single revision id.
    _upgrade(uri, engine, "fd1b2c3d4e5")
    _upgrade(uri, engine, "gb1b2c3d4e5f")
    preference_bytes = b"preserve-custom-preferences"
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM alembic_version"))
        conn.execute(sa.text("INSERT INTO alembic_version (version_num) VALUES ('gc1b2c3d4e5f')"))
        conn.execute(
            sa.text(
                "INSERT INTO users (id, is_admin, workspace_id, preferences) "
                "VALUES ('user_custom', 1, 0, :preferences)"
            ),
            {"preferences": preference_bytes},
        )
        conn.execute(
            sa.text(
                "INSERT INTO hosts "
                "(workspace_id, host_id, user_id, name, status, default_workspace) "
                "VALUES (0, 'host_custom', 'user_custom', 'Custom host', 2, "
                "'D:/AIProgram/Projects')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO custom_agents "
                "(workspace_id, id, owner_id, name, harness, bundle_location, version, "
                "created_at, updated_at) VALUES "
                "(0, 'agent_custom', 'user_custom', 'Durable agent', 'codex', "
                "'local://durable-agent', 3, 1, 2)"
            )
        )

    migrate = _run_migrations if manual else _initialize_or_verify_schema
    migrate(engine, uri)

    inspector = sa.inspect(engine)
    assert {column["name"] for column in inspector.get_columns("hosts")} >= {
        "deleted_at",
        "cli_retention_policy",
        "cli_retention_revision",
        "cli_retention_claim_token",
    }
    assert {column["name"] for column in inspector.get_columns("conversations")} >= {
        "archive_revision",
        "archive_close_requested_revision",
        "archive_close_completed_revision",
    }
    assert "cli_release_intents" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("users")} >= {
        "preferences",
    }
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "a10c20260910"
        assert (
            conn.scalar(sa.text("SELECT preferences FROM users WHERE id = 'user_custom'"))
            == preference_bytes
        )
        assert (
            conn.scalar(
                sa.text("SELECT default_workspace FROM hosts WHERE host_id = 'host_custom'")
            )
            == "D:/AIProgram/Projects"
        )
        assert (
            conn.scalar(sa.text("SELECT name FROM custom_agents WHERE id = 'agent_custom'"))
            == "Durable agent"
        )

    engine.dispose()
    clear_engine_cache()


def test_upgrade_creates_table_downgrade_drops_it(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'connections.db'}"
    engine = sa.create_engine(uri)

    # Upgrading to head exercises the full chain onto our migration — this is
    # exactly the call that raises on multiple heads.
    _upgrade(uri, engine, "head")
    inspector = sa.inspect(engine)
    assert "connections" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("connections")}
    assert {
        "workspace_id",
        "user_id",
        "provider",
        "account_id",
        "secret_enc",
        "metadata_json",
        "created_at",
        "updated_at",
    } <= columns
    pk = set(inspector.get_pk_constraint("connections")["constrained_columns"])
    assert pk == {"workspace_id", "user_id", "provider", "account_id"}

    _downgrade(uri, engine, "za2b3c4d5e6f")
    assert "connections" not in sa.inspect(engine).get_table_names()

    engine.dispose()
    clear_engine_cache()


def test_legacy_custom_stamp_repairs_missing_connections_table(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'legacy-connections.db'}"
    engine = sa.create_engine(uri)

    _upgrade(uri, engine, "fb1b2c3d4e5")
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE connections"))
    assert "connections" not in sa.inspect(engine).get_table_names()

    _upgrade(uri, engine, "head")
    assert "connections" in sa.inspect(engine).get_table_names()

    engine.dispose()
    clear_engine_cache()
