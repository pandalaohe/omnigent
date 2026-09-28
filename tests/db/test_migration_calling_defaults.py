"""Tests for the calling-defaults migration (``a23c20260928``).

The single-head test guards the chain; the SQLite tests cover the added
cache table and nullable columns, and the downgrade's two arms: restoring
``assignments.target_agent_id`` NOT NULL when every row has an agent and
leaving it nullable (with a warning) when one does not.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "a22c20260928"
_MIGRATION = "a23c20260928"


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


def _insert_assignment(conn: sa.Connection, *, target_agent_id: str | None) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO assignments "
            "(workspace_id, id, project_id, source_session_id, target_agent_id, "
            "task, inputs_json, idempotency_key, request_digest, created_at) "
            "VALUES (0, :id, :project, :source, :target, 'task', '[]', 'key', "
            "'digest', 1)"
        ),
        {
            "id": "a" * 32,
            "project": "b" * 32,
            "source": "c" * 32,
            "target": target_agent_id,
        },
    )


def test_single_alembic_head_includes_calling_defaults() -> None:
    """One head, and the calling-defaults revision is on the way to it."""
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, got {heads!r}"
    lineage = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
    assert _MIGRATION in lineage, "calling-defaults migration is not an ancestor of head"


def test_upgrade_adds_cache_and_columns_then_downgrade_reverses(tmp_path: Path) -> None:
    """Upgrade creates the cache and nullable columns; downgrade removes them."""
    uri = f"sqlite:///{tmp_path / 'calling-defaults.db'}"
    engine = sa.create_engine(uri)

    _upgrade(uri, engine, _PREVIOUS)
    inspector = sa.inspect(engine)
    assert "host_model_catalog_cache" not in inspector.get_table_names()
    by_name = {c["name"]: c for c in inspector.get_columns("assignments")}
    assert by_name["target_agent_id"]["nullable"] is False

    _upgrade(uri, engine, _MIGRATION)
    inspector = sa.inspect(engine)
    assert "host_model_catalog_cache" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("host_model_catalog_cache")}
    assert columns == {"workspace_id", "host_id", "harness", "payload", "fetched_at", "error"}
    pk = set(inspector.get_pk_constraint("host_model_catalog_cache")["constrained_columns"])
    assert pk == {"workspace_id", "host_id", "harness"}

    scheduled = {c["name"]: c for c in inspector.get_columns("scheduled_tasks")}
    assert scheduled["project_id"]["nullable"] is True
    assert scheduled["explicit_null_fields"]["nullable"] is True
    assignments = {c["name"]: c for c in inspector.get_columns("assignments")}
    assert assignments["target_agent_id"]["nullable"] is True
    assert assignments["reasoning_effort"]["nullable"] is True
    assert assignments["explicit_null_fields"]["nullable"] is True
    with engine.begin() as conn:
        _insert_assignment(conn, target_agent_id=None)

    _downgrade(uri, engine, _PREVIOUS)
    inspector = sa.inspect(engine)
    assert "host_model_catalog_cache" not in inspector.get_table_names()
    assignments = {c["name"]: c for c in inspector.get_columns("assignments")}
    assert "reasoning_effort" not in assignments
    assert "explicit_null_fields" not in assignments
    assert assignments["target_agent_id"]["nullable"] is True  # the NULL row blocks it
    scheduled = {c["name"] for c in inspector.get_columns("scheduled_tasks")}
    assert "project_id" not in scheduled
    assert "explicit_null_fields" not in scheduled

    engine.dispose()
    clear_engine_cache()


def test_downgrade_restores_target_agent_not_null_without_null_rows(tmp_path: Path) -> None:
    """With no null-agent rows, downgrade restores the old NOT NULL."""
    uri = f"sqlite:///{tmp_path / 'calling-defaults-restore.db'}"
    engine = sa.create_engine(uri)

    _upgrade(uri, engine, _MIGRATION)
    _downgrade(uri, engine, _PREVIOUS)

    inspector = sa.inspect(engine)
    by_name = {c["name"]: c for c in inspector.get_columns("assignments")}
    assert by_name["target_agent_id"]["nullable"] is False
    assert "host_model_catalog_cache" not in inspector.get_table_names()

    engine.dispose()
    clear_engine_cache()
