"""Exercise only the project-archive migration node on the selected test backend."""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.db_models import SqlProject
from omnigent.db.utils import get_or_create_engine


def test_project_archive_node_roundtrip_preserves_project(db_uri: str) -> None:
    migration = importlib.import_module(
        "omnigent.db.migrations.versions.a44c20261021_project_archive"
    )
    engine = get_or_create_engine(db_uri)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(SqlProject).values(
                id="f" * 32,
                name="Saved project",
                created_at=1,
                config='{"agent_id":"saved-agent"}',
            )
        )
        original = connection.execute(sa.select(SqlProject.name, SqlProject.config)).one()
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            assert "archived_at" not in {
                c["name"] for c in sa.inspect(connection).get_columns("projects")
            }
            assert "archive_state" not in {
                c["name"] for c in sa.inspect(connection).get_columns("projects")
            }
            assert "paused_by_archive_batch" not in {
                c["name"] for c in sa.inspect(connection).get_columns("scheduled_tasks")
            }
            assert (
                connection.execute(sa.select(SqlProject.name, SqlProject.config)).one() == original
            )
            migration.upgrade()
        columns = {c["name"]: c for c in sa.inspect(connection).get_columns("projects")}
        assert columns["archived_at"]["nullable"]
        assert columns["archive_state"]["nullable"]
        assert next(
            c
            for c in sa.inspect(connection).get_columns("scheduled_tasks")
            if c["name"] == "paused_by_archive_batch"
        )["nullable"]
        assert connection.execute(sa.select(SqlProject.name, SqlProject.config)).one() == original
        assert connection.execute(
            sa.select(SqlProject.archived_at, SqlProject.archive_state)
        ).one() == (None, None)
