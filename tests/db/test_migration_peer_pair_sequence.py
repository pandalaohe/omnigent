"""Upgrade and downgrade of durable peer-pair admission order."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "a29c20261010"
_MIGRATION = "a30c20261015"
_INDEX = "ix_session_peer_messages_pair_sequence"


def _run(uri: str, engine: sa.Engine, action: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, action)(config, revision)


def test_pair_sequence_migration_is_only_head() -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    assert script.get_heads() == [_MIGRATION]
    assert script.get_revision(_MIGRATION).down_revision == _PREVIOUS


def test_sqlite_upgrade_backfills_each_pair_and_downgrade_preserves_rows(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'peer-pair-order.db'}"
    engine = sa.create_engine(uri)
    try:
        _run(uri, engine, "upgrade", _PREVIOUS)
        assert "pair_sequence" not in {
            column["name"] for column in sa.inspect(engine).get_columns("session_peer_messages")
        }
        with engine.begin() as conn:
            for workspace, sender, receiver, peer, created in (
                (0, "a", "b", "f", 10),
                (0, "a", "b", "1", 10),
                (0, "a", "b", "2", 20),
                (0, "a", "c", "3", 10),
                (1, "a", "b", "4", 10),
            ):
                conn.execute(
                    sa.text(
                        "INSERT INTO session_peer_messages "
                        "(workspace_id, id, sender_session_id, receiver_session_id, "
                        "ref, text, state, created_at, expires_at) "
                        "VALUES (:workspace, :id, :sender, :receiver, 'ref', 'text', "
                        "'pending', :created, 100)"
                    ),
                    {
                        "workspace": workspace,
                        "id": bytes.fromhex(peer * 32),
                        "sender": bytes.fromhex(sender * 32),
                        "receiver": bytes.fromhex(receiver * 32),
                        "created": created,
                    },
                )

        _run(uri, engine, "upgrade", _MIGRATION)
        inspector = sa.inspect(engine)
        column = next(
            c
            for c in inspector.get_columns("session_peer_messages")
            if c["name"] == "pair_sequence"
        )
        assert column["nullable"] is True
        assert _INDEX in {
            index["name"] for index in inspector.get_indexes("session_peer_messages")
        }
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT workspace_id, id, pair_sequence FROM session_peer_messages")
            ).all()
        assert {(w, id_.hex()[0], seq) for w, id_, seq in rows} == {
            (0, "1", 1),
            (0, "f", 2),
            (0, "2", 3),
            (0, "3", 1),
            (1, "4", 1),
        }

        _run(uri, engine, "downgrade", _PREVIOUS)
        inspector = sa.inspect(engine)
        assert "pair_sequence" not in {
            column["name"] for column in inspector.get_columns("session_peer_messages")
        }
        assert _INDEX not in {
            index["name"] for index in inspector.get_indexes("session_peer_messages")
        }
        with engine.connect() as conn:
            assert conn.scalar(sa.text("SELECT COUNT(*) FROM session_peer_messages")) == 5
    finally:
        engine.dispose()
        clear_engine_cache()
