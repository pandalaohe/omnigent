"""Tests for the detached-card migration (``a26c20261009``).

Additive only: upgrade creates ``detached_cards`` and downgrade drops it (with
any records it holds), leaving the previous head's schema as it was.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "a25c20261004"
_MIGRATION = "a26c20261009"


def _run(uri: str, engine: sa.Engine, action: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, action)(config, revision)


def test_single_alembic_head_includes_detached_cards() -> None:
    """One head, and the detached-card revision is on the way to it."""
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, got {heads!r}"
    lineage = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
    assert _MIGRATION in lineage, "detached-card migration is not an ancestor of head"
    assert script.get_revision(_MIGRATION).down_revision == _PREVIOUS


def test_upgrade_creates_and_downgrade_drops_the_table(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'detached.db'}"
    engine = sa.create_engine(uri)
    try:
        _run(uri, engine, "upgrade", _PREVIOUS)
        before = set(sa.inspect(engine).get_table_names())
        assert "detached_cards" not in before

        _run(uri, engine, "upgrade", _MIGRATION)
        inspector = sa.inspect(engine)
        assert "detached_cards" in inspector.get_table_names()
        assert inspector.get_pk_constraint("detached_cards")["constrained_columns"] == [
            "workspace_id",
            "elicitation_id",
        ]
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO detached_cards (workspace_id, elicitation_id, session_id, kind,"
                    " state, mirror, params, payload, created_at, expires_at) VALUES (0,"
                    " 'elicit_1', :sid, 'approval', 'pending', 1, '{}', '{}', 1, 2)"
                ),
                {"sid": bytes(16)},
            )

        # A rollback drops the records with the table and nothing else.
        _run(uri, engine, "downgrade", _PREVIOUS)
        assert set(sa.inspect(engine).get_table_names()) == before
    finally:
        engine.dispose()
        clear_engine_cache()
