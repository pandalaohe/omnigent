"""Tests for the session-peer-messages migration (``a13c20260920``).

The single-head test guards the chain: upgrading to head on a fresh
SQLite database creates ``session_peer_messages`` with its three
indexes, and the downgrade drops the table again.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_EXPECTED_INDEXES = {
    "ix_session_peer_messages_receiver",
    "ix_session_peer_messages_sender",
    "ix_session_peer_messages_ref",
}


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


def test_single_alembic_head_includes_peer_messages() -> None:
    """One head, and the peer-messages revision is on the way to it.

    Pinning the head to a literal revision breaks on every upstream merge,
    which adds a mergepoint above it. What must hold is that the chain never
    forks and that this migration stays in the lineage that head runs.
    """
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, got {heads!r}"
    lineage = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
    assert "a13c20260920" in lineage, "peer-messages migration is not an ancestor of head"


def test_upgrade_head_creates_peer_messages_with_indexes(tmp_path: Path) -> None:
    """A fresh SQLite database upgraded to head has the table + indexes."""
    uri = f"sqlite:///{tmp_path / 'peer-messages.db'}"
    engine = sa.create_engine(uri)

    _upgrade(uri, engine, "head")
    inspector = sa.inspect(engine)
    assert "session_peer_messages" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("session_peer_messages")}
    assert {
        "workspace_id",
        "id",
        "sender_session_id",
        "receiver_session_id",
        "correlation_id",
        "ref",
        "text",
        "state",
        "reason",
        "created_at",
        "updated_at",
        "expires_at",
        "reply_peer_id",
        "replied_at",
    } <= columns
    pk = set(inspector.get_pk_constraint("session_peer_messages")["constrained_columns"])
    assert pk == {"workspace_id", "id"}
    got_indexes = {
        index["name"] for index in inspector.get_indexes("session_peer_messages")
    }
    assert _EXPECTED_INDEXES <= got_indexes

    _downgrade(uri, engine, "a12c20260913")
    inspector = sa.inspect(engine)
    assert "session_peer_messages" not in inspector.get_table_names()
    with engine.connect() as conn:
        # Each upstream mergepoint forks the chain, so unwinding this branch
        # leaves the other branch's head stamped alongside it.
        stamped = set(conn.scalars(sa.text("SELECT version_num FROM alembic_version")))
        assert "a12c20260913" in stamped, stamped

    engine.dispose()
    clear_engine_cache()
