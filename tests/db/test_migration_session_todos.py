"""Native-harness plans have a durable metadata owner."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, _create_engine, _get_current_db_revision

_PREVIOUS = "fa1b2c3d4e5"
_HEAD = "fb1b2c3d4e5"


def test_session_todos_column_upgrade_and_downgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = _create_engine(uri)
    config = _build_alembic_config(uri)

    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, _PREVIOUS)
    with engine.connect() as connection:
        before = {
            row[1]
            for row in connection.execute(
                sa.text("PRAGMA table_info(omnigent_conversation_metadata)")
            )
        }
    assert "session_todos" not in before

    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, _HEAD)
    assert _get_current_db_revision(engine) == _HEAD
    with engine.connect() as connection:
        after = {
            row[1]
            for row in connection.execute(
                sa.text("PRAGMA table_info(omnigent_conversation_metadata)")
            )
        }
    assert "session_todos" in after

    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, _PREVIOUS)
    with engine.connect() as connection:
        downgraded = {
            row[1]
            for row in connection.execute(
                sa.text("PRAGMA table_info(omnigent_conversation_metadata)")
            )
        }
    assert "session_todos" not in downgraded
    engine.dispose()
