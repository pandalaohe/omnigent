"""Durable archive and CLI release intent schema migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, _create_engine

_PREVIOUS = "fe1b2c3d4e5"
_MIGRATION = "ff1b2c3d4e5"


def test_upgrade_adds_archive_state_release_queue_and_host_lease(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = _create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, _PREVIOUS)
        command.upgrade(config, _MIGRATION)

    inspector = sa.inspect(engine)
    conversation_columns = {column["name"] for column in inspector.get_columns("conversations")}
    assert {
        "archive_revision",
        "archive_close_requested_revision",
        "archive_close_completed_revision",
        "archive_close_claim_token",
        "archive_close_claimed_at",
        "archive_close_last_error",
    } <= conversation_columns
    host_columns = {column["name"] for column in inspector.get_columns("hosts")}
    assert {"cli_retention_claim_token", "cli_retention_claimed_at"} <= host_columns
    assert "cli_release_intents" in inspector.get_table_names()
    assert {
        "ix_cli_release_intents_due",
        "ix_cli_release_intents_host",
        "ix_cli_release_intents_root",
    } <= {index["name"] for index in inspector.get_indexes("cli_release_intents")}
    engine.dispose()


@pytest.mark.parametrize("active_state", ["intent", "archive", "host_claim"])
def test_downgrade_rejects_active_lifecycle_state(
    tmp_path: Path,
    active_state: str,
) -> None:
    uri = f"sqlite:///{tmp_path / f'{active_state}.db'}"
    engine = _create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, _MIGRATION)
        if active_state == "intent":
            connection.execute(
                sa.text(
                    "INSERT INTO cli_release_intents "
                    "(workspace_id,id,dedupe_key,reason,root_session_id,target_session_id,"
                    "status,next_attempt_at,created_at,updated_at) "
                    "VALUES (0,'intent-1','dedupe-1','archive',:root_id,:target_id,"
                    "'pending',0,1,1)"
                ),
                {
                    "root_id": bytes.fromhex("00112233445566778899aabbccddeeff"),
                    "target_id": bytes.fromhex("ffeeddccbbaa99887766554433221100"),
                },
            )
        elif active_state == "archive":
            connection.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(workspace_id,id,root_conversation_id,created_at,updated_at,"
                    "archive_revision,archive_close_requested_revision) "
                    "VALUES (0,:id,:id,1,1,1,1)"
                ),
                {"id": bytes.fromhex("00112233445566778899aabbccddeeff")},
            )
        else:
            connection.execute(
                sa.text(
                    "INSERT INTO hosts "
                    "(workspace_id,host_id,user_id,name,status,created_at,updated_at,"
                    "cli_retention_claim_token) "
                    "VALUES (0,:host_id,'local','test-host',1,1,1,'lease')"
                ),
                {"host_id": bytes.fromhex("0123456789abcdef0123456789abcdef")},
            )

        with pytest.raises(RuntimeError, match="drain lifecycle work"):
            command.downgrade(config, _PREVIOUS)

    assert "cli_release_intents" in sa.inspect(engine).get_table_names()
    engine.dispose()


def test_downgrade_succeeds_after_lifecycle_state_is_drained(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'drained.db'}"
    engine = _create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, _MIGRATION)
        command.downgrade(config, _PREVIOUS)

    inspector = sa.inspect(engine)
    assert "cli_release_intents" not in inspector.get_table_names()
    assert "archive_revision" not in {
        column["name"] for column in inspector.get_columns("conversations")
    }
    engine.dispose()
