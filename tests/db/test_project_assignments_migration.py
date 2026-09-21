"""Tests for the project-assignments migration (``a12c20260913``).

Upgrades to head, inserts one non-terminal and one terminal assignment,
downgrades one step to ``a11c20260912`` and asserts the five tables and
the two ``projects`` columns are gone.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_ASSIGNMENT_TABLES = (
    "project_repositories",
    "project_host_bindings",
    "assignments",
    "assignment_attempts",
    "assignment_messages",
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


def test_downgrade_drops_assignment_tables_and_columns(tmp_path: Path) -> None:
    """Non-terminal rows do not block the downgrade; schema reverts.

    One ``waiting`` plus one ``succeeded`` row are present, so the
    downgrade logs exactly one non-terminal row before dropping.
    """
    uri = f"sqlite:///{tmp_path / 'assignments-migration.db'}"
    engine = sa.create_engine(uri)

    _upgrade(uri, engine, "head")
    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())
    assert set(_ASSIGNMENT_TABLES) <= tables
    project_columns = {c["name"] for c in inspector.get_columns("projects")}
    assert {"collaboration_enabled", "collaboration_revision"} <= project_columns

    # One non-terminal and one terminal assignment: the downgrade logs
    # its non-terminal count, then drops.
    insert_stmt = sa.text(
        "INSERT INTO assignments "
        "(workspace_id, id, project_id, source_session_id, "
        "target_agent_id, task, inputs_json, idempotency_key, "
        "request_digest, state, created_at) "
        "VALUES (0, :id, :project_id, :source, :agent, :task, :inputs, "
        ":key, :digest, :state, 1700000000)"
    )
    with engine.begin() as conn:
        conn.execute(
            insert_stmt,
            {
                "id": uuid.uuid5(uuid.NAMESPACE_DNS, "assign-1").bytes,
                "project_id": uuid.uuid5(uuid.NAMESPACE_DNS, "proj-1").bytes,
                "source": uuid.uuid5(uuid.NAMESPACE_DNS, "sess-1").bytes,
                "agent": uuid.uuid5(uuid.NAMESPACE_DNS, "agent-1").bytes,
                "task": b"do the thing",
                "inputs": b"[]",
                "key": "key-1",
                "digest": "d" * 64,
                "state": "waiting",
            },
        )
        conn.execute(
            insert_stmt,
            {
                "id": uuid.uuid5(uuid.NAMESPACE_DNS, "assign-2").bytes,
                "project_id": uuid.uuid5(uuid.NAMESPACE_DNS, "proj-1").bytes,
                "source": uuid.uuid5(uuid.NAMESPACE_DNS, "sess-2").bytes,
                "agent": uuid.uuid5(uuid.NAMESPACE_DNS, "agent-1").bytes,
                "task": b"do the other thing",
                "inputs": b"[]",
                "key": "key-2",
                "digest": "d" * 64,
                "state": "succeeded",
            },
        )

    # Alembic routes migration logs to stderr, past caplog, and loads the
    # version file as ``<basename>_py`` (``.`` → ``_``): capture on that
    # runtime logger.
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    migration_logger = logging.getLogger("a12c20260913_add_project_assignments_py")
    capture = _Capture()
    migration_logger.addHandler(capture)
    try:
        _downgrade(uri, engine, "a11c20260912")
    finally:
        migration_logger.removeHandler(capture)
    assert any("with 1 non-terminal assignments" in message for message in messages)

    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())
    assert not (set(_ASSIGNMENT_TABLES) & tables)
    project_columns = {c["name"] for c in inspector.get_columns("projects")}
    assert not ({"collaboration_enabled", "collaboration_revision"} & project_columns)
    with engine.connect() as conn:
        # The chain forks at each upstream mergepoint, so unwinding this branch
        # leaves the other branch's head stamped alongside it; assert this
        # branch landed rather than that it is the only row.
        stamped = set(conn.scalars(sa.text("SELECT version_num FROM alembic_version")))
        assert "a11c20260912" in stamped, stamped

    engine.dispose()
    clear_engine_cache()
