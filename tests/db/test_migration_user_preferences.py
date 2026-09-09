"""Tests for the cross-device user preferences migration."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


def test_users_preferences_column_is_nullable_binary(db_uri: str) -> None:
    """The head schema carries an optional compressed preferences column."""
    engine = sa.create_engine(db_uri)
    try:
        columns = {column["name"]: column for column in sa.inspect(engine).get_columns("users")}
    finally:
        engine.dispose()

    assert columns["preferences"]["nullable"] is True
    assert isinstance(columns["preferences"]["type"], sa.LargeBinary)


def test_conversations_archived_at_column_and_index(db_uri: str) -> None:
    """The head schema owns a nullable stable archive timestamp and list index."""
    engine = sa.create_engine(db_uri)
    try:
        inspector = sa.inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("conversations")}
        indexes = {index["name"]: index for index in inspector.get_indexes("conversations")}
    finally:
        engine.dispose()

    assert columns["archived_at"]["nullable"] is True
    assert indexes["ix_conversations_archived_archived_at"]["column_names"] == [
        "workspace_id",
        "archived",
        "archived_at",
        "id",
    ]
    assert columns["archive_locked"]["nullable"] is False
    assert columns["deletion_claim_token"]["nullable"] is True
    assert columns["deletion_claimed_at"]["nullable"] is True


def test_deletion_claim_migration_round_trips_and_rebuilds_lock_mirror(tmp_path: Path) -> None:
    """f8 -> f9 -> f8 -> f9 preserves the public lock label and backfill."""
    uri = f"sqlite:///{tmp_path / 'deletion-claim.db'}"
    config = _build_alembic_config(uri)
    command.upgrade(config, "f8a1b2c3d4e5")
    conversation_id = bytes.fromhex("1" * 32)
    engine = sa.create_engine(uri)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO conversations "
                "(workspace_id, id, created_at, updated_at, title, root_conversation_id, "
                "next_position, archived, archived_at) "
                "VALUES (0, :id, 1, 1, '', :id, 0, true, 1)"
            ),
            {"id": conversation_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO conversation_labels "
                "(workspace_id, conversation_id, key, value, updated_at) "
                "VALUES (0, :id, 'omnigent.archive_locked', '1', 1)"
            ),
            {"id": conversation_id},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(uri)
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT archive_locked, deletion_claim_token, deletion_claimed_at "
                "FROM conversations WHERE id = :id"
            ),
            {"id": conversation_id},
        ).one()
    assert tuple(row) == (1, None, None)
    engine.dispose()

    command.downgrade(config, "f8a1b2c3d4e5")
    engine = sa.create_engine(uri)
    columns = {column["name"] for column in sa.inspect(engine).get_columns("conversations")}
    with engine.connect() as connection:
        label = connection.scalar(
            sa.text(
                "SELECT value FROM conversation_labels "
                "WHERE conversation_id = :id AND key = 'omnigent.archive_locked'"
            ),
            {"id": conversation_id},
        )
    assert "archive_locked" not in columns
    assert "deletion_claim_token" not in columns
    assert label == "1"
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(uri)
    with engine.connect() as connection:
        assert connection.scalar(
            sa.text("SELECT archive_locked FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        ) == 1
    engine.dispose()
