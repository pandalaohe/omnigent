"""The additive task schema rolls forward and back without touching existing tables."""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlTaskBoardHistory, SqlTaskBoardItem, SqlTaskBoardSource


def test_task_board_sqlite_migration_round_trip():
    migration = importlib.import_module(
        "omnigent.db.migrations.versions.a54c20261106_add_task_board"
    )
    precision = importlib.import_module(
        "omnigent.db.migrations.versions.a55c20261106_task_board_complete_version"
    )
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE existing_data (value TEXT)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                precision.upgrade()
        with Session(engine) as session:
            session.add(
                SqlTaskBoardSource(
                    owner_id="owner",
                    source_id="example",
                    created_at=1,
                    issues=[],
                    complete_version="2026-11-06T06:30:00.0000001Z",
                )
            )
            session.add(
                SqlTaskBoardItem(
                    owner_id="owner",
                    source_id="example",
                    uid="one",
                    body={"@type": "Task", "uid": "one"},
                    progress="needs-action",
                    group_id="",
                    source_updated=1,
                    sequence=0,
                    changed_at=1,
                )
            )
            session.add(
                SqlTaskBoardHistory(
                    owner_id="owner",
                    source_id="example",
                    uid="one",
                    entry_id="created",
                    time=1,
                    kind="created",
                    entry={"id": "created"},
                )
            )
            session.commit()
            assert session.scalar(sa.select(SqlTaskBoardSource.complete_version)) == (
                "2026-11-06T06:30:00.0000001Z"
            )
            assert session.scalar(sa.select(SqlTaskBoardItem.body)) == {
                "@type": "Task",
                "uid": "one",
            }
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                precision.downgrade()
                migration.downgrade()
            assert set(sa.inspect(connection).get_table_names()) == {"existing_data"}
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                precision.upgrade()
            assert {"task_board_sources", "task_board_items", "task_board_history"} <= set(
                sa.inspect(connection).get_table_names()
            )
    finally:
        engine.dispose()
