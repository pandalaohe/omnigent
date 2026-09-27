"""Tests for the cross-device user preferences migration."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.compression import encode
from omnigent.db.db_models import SqlPreference
from omnigent.db.utils import _build_alembic_config

_PRE_MOVE_REVISION = "c4b2d3e4f5a6"
_SETTINGS_KEY_PREFIX = "settings."


def _seed_user(
    engine: sa.Engine,
    user_id: str,
    value: bytes | str | None,
    *,
    workspace_id: int = 0,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users (workspace_id, id, is_admin, preferences) "
                "VALUES (:workspace_id, :id, false, :value)"
            ),
            {"workspace_id": workspace_id, "id": user_id, "value": value},
        )


def _stored_value(engine: sa.Engine, user_id: str) -> bytes | str | None:
    """The raw ``users.preferences`` value, exactly as stored."""
    with engine.connect() as connection:
        return connection.scalar(
            sa.text("SELECT preferences FROM users WHERE id = :id"), {"id": user_id}
        )


def _settings_rows(engine: sa.Engine, user_id: str) -> dict[str, str]:
    """The user's settings rows, decompressed, keyed without the prefix."""
    with Session(engine) as session:
        rows = session.scalars(select(SqlPreference).where(SqlPreference.user_id == user_id)).all()
    return {
        row.key[len(_SETTINGS_KEY_PREFIX) :]: row.value
        for row in rows
        if row.key.startswith(_SETTINGS_KEY_PREFIX)
    }


def _envelope(settings: dict[str, object]) -> dict[str, object]:
    return {"version": 1, "settings": settings}


def _upgrade_to_pre_move(tmp_path: Path, name: str) -> tuple[str, object, sa.Engine]:
    uri = f"sqlite:///{tmp_path / name}"
    config = _build_alembic_config(uri)
    command.upgrade(config, _PRE_MOVE_REVISION)
    return uri, config, sa.create_engine(uri)


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
        assert (
            connection.scalar(
                sa.text("SELECT archive_locked FROM conversations WHERE id = :id"),
                {"id": conversation_id},
            )
            == 1
        )
    engine.dispose()


def test_preferences_envelope_moves_into_settings_rows(tmp_path: Path) -> None:
    """A framed envelope becomes settings.version + one settings.<ns> row."""
    uri, config, engine = _upgrade_to_pre_move(tmp_path, "move-envelope.db")
    envelope = _envelope({"keyboard_shortcuts": {"enabled": True}, "context_indicator": "compact"})
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    stored = encode(text)
    assert stored is not None
    _seed_user(engine, "alice", stored)
    _seed_user(engine, "bob", None)
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(uri)
    assert _settings_rows(engine, "alice") == {
        "version": "1",
        "keyboard_shortcuts": '{"enabled":true}',
        "context_indicator": '"compact"',
    }
    assert _settings_rows(engine, "bob") == {}
    # The envelope column is deliberately untouched for image rollback.
    assert _stored_value(engine, "alice") == stored
    engine.dispose()


def test_preferences_move_accepts_legacy_plaintext(tmp_path: Path) -> None:
    """A pre-compression envelope (unframed UTF-8) still moves."""
    uri, config, engine = _upgrade_to_pre_move(tmp_path, "move-plaintext.db")
    envelope = _envelope({"usage_context": {"visible": False}})
    raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    _seed_user(engine, "alice", raw.encode("utf-8"))
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(uri)
    assert _settings_rows(engine, "alice") == {
        "version": "1",
        "usage_context": '{"visible":false}',
    }
    engine.dispose()


def test_preferences_move_writes_version_for_empty_settings(tmp_path: Path) -> None:
    """An explicitly initialized all-defaults envelope moves to the marker row."""
    uri, config, engine = _upgrade_to_pre_move(tmp_path, "move-empty.db")
    _seed_user(engine, "alice", encode('{"settings":{},"version":1}'))
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(uri)
    assert _settings_rows(engine, "alice") == {"version": "1"}
    engine.dispose()


def test_preferences_move_skips_unknown_namespaces(tmp_path: Path) -> None:
    """Namespaces outside the allowlist are dropped, siblings still move."""
    uri, config, engine = _upgrade_to_pre_move(tmp_path, "move-unknown.db")
    _seed_user(
        engine,
        "alice",
        encode(
            json.dumps(
                _envelope({"agent_pins": {"ids": ["ag_polly"]}, "not_allowed": {"x": 1}}),
                separators=(",", ":"),
                sort_keys=True,
            )
        ),
    )
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(uri)
    assert _settings_rows(engine, "alice") == {
        "version": "1",
        "agent_pins": '{"ids":["ag_polly"]}',
    }
    engine.dispose()


def test_preferences_move_skips_bad_bytes_and_invalid_envelopes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Corrupt frames and non-envelopes are warned about and skipped."""
    uri, config, engine = _upgrade_to_pre_move(tmp_path, "move-bad.db")
    _seed_user(engine, "corrupt", b"\x00\x09broken-frame")
    _seed_user(engine, "notjson", b"preserve-custom-preferences")
    _seed_user(engine, "version2", encode('{"settings":{},"version":2}'))
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(uri)
    assert _settings_rows(engine, "corrupt") == {}
    assert _settings_rows(engine, "notjson") == {}
    assert _settings_rows(engine, "version2") == {}
    assert _stored_value(engine, "notjson") == b"preserve-custom-preferences"
    warnings = capsys.readouterr().err
    assert "Skipping preferences move for user corrupt" in warnings
    assert "Skipping preferences move for user notjson" in warnings
    assert "Skipping preferences move for user version2" in warnings
    engine.dispose()


def test_preferences_move_is_idempotent_when_version_row_exists(tmp_path: Path) -> None:
    """A user with a settings.version row is never overwritten by a move."""
    uri, config, engine = _upgrade_to_pre_move(tmp_path, "move-idempotent.db")
    _seed_user(
        engine, "alice", encode('{"settings":{"usage_context":{"visible":true}},"version":1}')
    )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO preferences (workspace_id, user_id, key, value) "
                "VALUES (0, 'alice', 'settings.version', :value)"
            ),
            {"value": encode("1")},
        )
        connection.execute(
            sa.text(
                "INSERT INTO preferences (workspace_id, user_id, key, value) "
                "VALUES (0, 'alice', 'settings.context_indicator', :value)"
            ),
            {"value": encode('"compact"')},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(uri)
    assert _settings_rows(engine, "alice") == {"version": "1", "context_indicator": '"compact"'}
    engine.dispose()


def test_preferences_move_skips_an_oversized_namespace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One namespace over the BLOB cap is skipped; its sibling still moves."""
    uri, config, engine = _upgrade_to_pre_move(tmp_path, "move-oversized.db")
    padding = secrets.token_urlsafe(96_000)
    _seed_user(
        engine,
        "alice",
        encode(
            json.dumps(
                _envelope({"usage_context": padding, "context_indicator": "compact"}),
                separators=(",", ":"),
                sort_keys=True,
            )
        ),
    )
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(uri)
    assert _settings_rows(engine, "alice") == {"version": "1", "context_indicator": '"compact"'}
    assert "stored value exceeds 65535 bytes" in capsys.readouterr().err
    engine.dispose()


def test_preferences_move_fails_when_a_batch_cannot_land(tmp_path: Path) -> None:
    """A batch that keeps failing fails the migration instead of stamping it."""
    uri, config, engine = _upgrade_to_pre_move(tmp_path, "move-fails.db")
    _seed_user(engine, "alice", encode('{"settings":{"context_indicator":"compact"},"version":1}'))
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO preferences (workspace_id, user_id, key, value) "
                "VALUES (0, 'alice', 'settings.context_indicator', :value)"
            ),
            {"value": encode('"wide"')},
        )
    engine.dispose()

    # The batch's second insert collides with the existing row, so every
    # attempt fails and the retry loop must surface the error.
    with pytest.raises(sa.exc.IntegrityError):
        command.upgrade(config, "head")

    engine = sa.create_engine(uri)
    with engine.connect() as connection:
        version = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert version == _PRE_MOVE_REVISION
    assert _settings_rows(engine, "alice") == {"context_indicator": '"wide"'}
    engine.dispose()


def test_preferences_move_downgrade_removes_only_settings_rows(tmp_path: Path) -> None:
    """Downgrade deletes settings.* and leaves users.preferences in place."""
    uri, config, engine = _upgrade_to_pre_move(tmp_path, "move-downgrade.db")
    stored = encode('{"settings":{"usage_context":{"visible":true}},"version":1}')
    assert stored is not None
    _seed_user(engine, "alice", stored)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO preferences (workspace_id, user_id, key, value) "
                "VALUES (0, 'alice', 'project_order', :value)"
            ),
            {"value": encode('{"sort_mode":"alphabetical"}')},
        )
    engine.dispose()

    command.upgrade(config, "head")
    command.downgrade(config, _PRE_MOVE_REVISION)
    engine = sa.create_engine(uri)
    assert _settings_rows(engine, "alice") == {}
    assert "preferences" in sa.inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT value FROM preferences "
                    "WHERE user_id = 'alice' AND key = 'project_order'"
                )
            )
            is not None
        )
    assert _stored_value(engine, "alice") == stored
    engine.dispose()
