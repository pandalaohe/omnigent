"""Provider allowance snapshots survive the old 256-character label ceiling."""

from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.compression import decode
from omnigent.db.utils import _build_alembic_config, _create_engine, _get_current_db_revision

_PREVIOUS = "f9a1b2c3d4e5"
_LABEL_KEY = "omnigent.last_provider_usage_limits"


def test_upgrade_recovers_one_character_clipped_provider_snapshot(tmp_path: Path) -> None:
    """A production-shaped clipped label moves into the unbounded metadata field."""
    uri = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = _create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, _PREVIOUS)

    limits = {
        "provider": "Claude",
        "captured_at": 1_788_312_443,
        "windows": [
            {
                "label": "5h",
                "aria_label": "5 hour",
                "used_percent": 3.0,
                "duration_mins": 300,
                "resets_at": 1_788_324_000,
            },
            {
                "label": "w",
                "aria_label": "weekly",
                "used_percent": 1.0,
                "duration_mins": 10_080,
                "resets_at": 1_788_627_600,
            },
        ],
    }
    encoded = json.dumps(limits, separators=(",", ":"))
    assert len(encoded) == 257
    clipped = encoded[:256]
    conversation_id = bytes.fromhex("2e13ff39b4ff480095fe55077d87861e")

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO conversations "
                "(workspace_id,id,created_at,updated_at,title,root_conversation_id,next_position,"
                "archived,archive_locked) "
                "VALUES (0,:id,1,1,'',:id,0,false,false)"
            ),
            {"id": conversation_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO omnigent_conversation_metadata (workspace_id,id,kind) "
                "VALUES (0,:id,1)"
            ),
            {"id": conversation_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO conversation_labels "
                "(workspace_id,conversation_id,key,value,updated_at) "
                "VALUES (0,:id,:key,:value,1)"
            ),
            {"id": conversation_id, "key": _LABEL_KEY, "value": clipped},
        )

    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

    assert (
        _get_current_db_revision(engine) == ScriptDirectory.from_config(config).get_current_head()
    )
    with engine.connect() as connection:
        raw = connection.execute(
            sa.text(
                "SELECT provider_usage_limits FROM omnigent_conversation_metadata "
                "WHERE workspace_id=0 AND id=:id"
            ),
            {"id": conversation_id},
        ).scalar_one()
    assert json.loads(decode(raw) or "null") == limits

    engine.dispose()
