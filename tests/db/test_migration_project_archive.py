"""Exercise only the project-archive migration node on the selected test backend."""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
from omnigent.db.db_models import SqlProject
from omnigent.db.utils import get_or_create_engine


def test_project_archive_node_roundtrip_preserves_project(db_uri: str) -> None:
    migration = importlib.import_module(
        "omnigent.db.migrations.versions.a44c20261021_project_archive"
    )
    engine = get_or_create_engine(db_uri)

    def migrate(action: str) -> None:
        with engine.connect() as connection:
            if engine.dialect.name == "cockroachdb":
                _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
            with Operations.context(MigrationContext.configure(connection)):
                getattr(migration, action)()
            connection.commit()

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
    migrate("downgrade")
    with engine.connect() as connection:
        assert "archived_at" not in {
            c["name"] for c in sa.inspect(connection).get_columns("projects")
        }
        assert "archive_state" not in {
            c["name"] for c in sa.inspect(connection).get_columns("projects")
        }
        assert "paused_by_archive_batch" not in {
            c["name"] for c in sa.inspect(connection).get_columns("scheduled_tasks")
        }
        assert connection.execute(sa.select(SqlProject.name, SqlProject.config)).one() == original
    migrate("upgrade")
    with engine.connect() as connection:
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
