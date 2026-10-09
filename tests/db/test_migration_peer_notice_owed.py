"""Tests for the peer notice-owed migration (``a27c20261009``).

Additive only: upgrade adds ``session_peer_messages.notice_owed_at``
(NULL for legacy rows) and downgrade drops the column, leaving the row.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "a26c20261009"
_MIGRATION = "a27c20261009"


def _run(uri: str, engine: sa.Engine, action: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, action)(config, revision)


def test_single_alembic_head_includes_peer_notice_owed() -> None:
    """One head, and the notice-owed revision is on the way to it."""
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, got {heads!r}"
    lineage = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
    assert _MIGRATION in lineage, "notice-owed migration is not an ancestor of head"
    assert script.get_revision(_MIGRATION).down_revision == _PREVIOUS


def test_upgrade_adds_and_downgrade_drops_the_column(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'peer-notice.db'}"
    engine = sa.create_engine(uri)
    try:
        _run(uri, engine, "upgrade", _PREVIOUS)
        columns = {c["name"] for c in sa.inspect(engine).get_columns("session_peer_messages")}
        assert "notice_owed_at" not in columns

        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO session_peer_messages "
                    "(workspace_id, id, sender_session_id, receiver_session_id, ref, "
                    "text, state, created_at, expires_at) "
                    "VALUES (0, :id, :sender, :receiver, 'r1', 'x', 'failed', 1, 2)"
                ),
                {"id": "a" * 32, "sender": "b" * 32, "receiver": "c" * 32},
            )

        _run(uri, engine, "upgrade", _MIGRATION)
        by_name = {c["name"]: c for c in sa.inspect(engine).get_columns("session_peer_messages")}
        assert by_name["notice_owed_at"]["nullable"] is True
        assert by_name["notice_owed_at"]["default"] is None
        with engine.connect() as conn:
            assert conn.scalar(sa.text("SELECT notice_owed_at FROM session_peer_messages")) is None

        _run(uri, engine, "downgrade", _PREVIOUS)
        columns = {c["name"] for c in sa.inspect(engine).get_columns("session_peer_messages")}
        assert "notice_owed_at" not in columns
        with engine.connect() as conn:
            assert conn.scalar(sa.text("SELECT COUNT(*) FROM session_peer_messages")) == 1
    finally:
        engine.dispose()
        clear_engine_cache()
