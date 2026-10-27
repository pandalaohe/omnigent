"""Tests for the session-succession receipt migration (``a24c20260930``).

Additive only: upgrade creates ``session_successions`` and downgrade drops it,
leaving the previous head's schema as it was.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "a23c20260928"
_MIGRATION = "a24c20260930"


def _run(uri: str, engine: sa.Engine, action: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, action)(config, revision)


def test_single_alembic_head_includes_session_successions() -> None:
    """One head, and the session-successions revision is on the way to it."""
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, got {heads!r}"
    lineage = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
    assert _MIGRATION in lineage, "session-successions migration is not an ancestor of head"
    assert script.get_revision(_MIGRATION).down_revision == _PREVIOUS


def test_upgrade_creates_and_downgrade_drops_the_table(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'succession.db'}"
    engine = sa.create_engine(uri)
    try:
        _run(uri, engine, "upgrade", _PREVIOUS)
        before = set(sa.inspect(engine).get_table_names())
        assert "session_successions" not in before

        _run(uri, engine, "upgrade", _MIGRATION)
        inspector = sa.inspect(engine)
        assert "session_successions" in inspector.get_table_names()
        unique = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("session_successions")
        }
        assert ("workspace_id", "old_id", "new_id") in unique

        _run(uri, engine, "downgrade", _PREVIOUS)
        assert set(sa.inspect(engine).get_table_names()) == before
    finally:
        engine.dispose()
        clear_engine_cache()


def test_deferred_runner_migration_preserves_existing_receipts(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'deferred.db'}"
    engine = sa.create_engine(uri)
    previous = "a42c20261022"
    migration = "a48c20261026"
    try:
        _run(uri, engine, "upgrade", previous)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO session_successions "
                    "(workspace_id, id, old_id, new_id, phase, direct_ids, moved_ids, "
                    "created_at, updated_at) "
                    "VALUES (0, 'receipt', :old, :new, 'done', '[]', '[]', 1, 1)"
                ),
                {"old": bytes.fromhex("11" * 16), "new": bytes.fromhex("22" * 16)},
            )
        _run(uri, engine, "upgrade", migration)
        columns = {
            column["name"]: column
            for column in sa.inspect(engine).get_columns("session_successions")
        }
        assert columns["deferred_runners"]["nullable"]
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.text("SELECT deferred_runners FROM session_successions")
                ).scalar_one()
                is None
            )
        _run(uri, engine, "downgrade", previous)
        with engine.connect() as conn:
            assert (
                conn.execute(sa.text("SELECT phase FROM session_successions")).scalar_one()
                == "done"
            )
    finally:
        engine.dispose()
        clear_engine_cache()
