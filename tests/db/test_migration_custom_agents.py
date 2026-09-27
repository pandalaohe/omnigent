"""Tests for the owner-scoped custom Agent library revision on the custom line.

The custom line created ``custom_agents`` in fd1b2c3d4e5, so upstream's
c7a9e2f4b610 must be a guarded no-op there: upgrading through it keeps the
table and its rows, and downgrading it must not drop what it does not own.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_EXPECTED_COLUMNS = {
    "workspace_id",
    "id",
    "owner_id",
    "name",
    "description",
    "harness",
    "model",
    "bundle_location",
    "version",
    "created_at",
    "updated_at",
    "deleted_at",
}


def _migrate(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _downgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def _library_shape(engine: sa.Engine) -> tuple[set[str], set[str], set[str]]:
    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("custom_agents")}
    primary_key = set(inspector.get_pk_constraint("custom_agents")["constrained_columns"])
    indexes = {index["name"] for index in inspector.get_indexes("custom_agents")}
    return columns, primary_key, indexes


def test_guarded_upgrade_keeps_fd1_table_and_rows(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'custom-agents.db'}"
    engine = sa.create_engine(uri)

    _migrate(uri, engine, "a20c20260927")
    assert "custom_agents" in sa.inspect(engine).get_table_names()
    before = _library_shape(engine)
    assert before[0] == _EXPECTED_COLUMNS
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO custom_agents "
                "(workspace_id, id, owner_id, name, harness, bundle_location, version, "
                "created_at, updated_at) "
                "VALUES (0, 'agent-1', 'owner-1', 'Saved agent', 'claude-native', "
                "'bundles/agent-1', 1, 1, 1)"
            )
        )

    _migrate(uri, engine, "c7a9e2f4b610")
    assert _library_shape(engine) == before
    with engine.begin() as connection:
        rows = connection.execute(sa.text("SELECT id FROM custom_agents")).scalars().all()
    assert rows == ["agent-1"]

    _downgrade(uri, engine, "a20c20260927")
    assert "custom_agents" in sa.inspect(engine).get_table_names()
    assert _library_shape(engine) == before
    with engine.begin() as connection:
        rows = connection.execute(sa.text("SELECT id FROM custom_agents")).scalars().all()
    assert rows == ["agent-1"]

    engine.dispose()
    clear_engine_cache()


def test_members_column_upgrade_and_downgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'custom-agent-members.db'}"
    engine = sa.create_engine(uri)

    _migrate(uri, engine, "c7a9e2f4b610")
    assert "members" not in {
        column["name"] for column in sa.inspect(engine).get_columns("custom_agents")
    }

    _migrate(uri, engine, "c1a6e2f4b610")
    columns = {
        column["name"]: column for column in sa.inspect(engine).get_columns("custom_agents")
    }
    assert columns["members"]["nullable"] is True

    _downgrade(uri, engine, "c7a9e2f4b610")
    assert "members" not in {
        column["name"] for column in sa.inspect(engine).get_columns("custom_agents")
    }

    engine.dispose()
    clear_engine_cache()
