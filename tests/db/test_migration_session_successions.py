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


def test_single_alembic_head_is_session_successions() -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    assert script.get_heads() == [_MIGRATION]
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
