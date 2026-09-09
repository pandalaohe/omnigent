"""Host CLI retention policy columns preserve legacy defaults across migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, _create_engine

_PREVIOUS = "ge1b2c3d4e5f"
_MIGRATION = "fe1b2c3d4e5"


def test_upgrade_adds_nullable_policy_and_zero_revision(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = _create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, _PREVIOUS)
        connection.execute(
            sa.text(
                "INSERT INTO hosts "
                "(workspace_id,host_id,user_id,name,status,created_at,updated_at) "
                "VALUES (0,:host_id,'local','test-host',1,1,1)"
            ),
            {"host_id": bytes.fromhex("0123456789abcdef0123456789abcdef")},
        )
        command.upgrade(config, _MIGRATION)

    columns = {column["name"] for column in sa.inspect(engine).get_columns("hosts")}
    assert {"cli_retention_policy", "cli_retention_revision"} <= columns
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT cli_retention_policy,cli_retention_revision FROM hosts")
        ).one()
    assert row == (None, 0)
    engine.dispose()


def test_downgrade_requires_every_host_policy_to_be_reset(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = _create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, _MIGRATION)
        connection.execute(
            sa.text(
                "INSERT INTO hosts "
                "(workspace_id,host_id,user_id,name,status,created_at,updated_at,"
                "cli_retention_policy) "
                "VALUES (0,:host_id,'local','test-host',1,1,1,:policy)"
            ),
            {
                "host_id": bytes.fromhex("0123456789abcdef0123456789abcdef"),
                "policy": b"{}",
            },
        )
        with pytest.raises(RuntimeError, match="reset every Host"):
            command.downgrade(config, _PREVIOUS)
        connection.execute(sa.text("UPDATE hosts SET cli_retention_policy = NULL"))
        command.downgrade(config, _PREVIOUS)

    columns = {column["name"] for column in sa.inspect(engine).get_columns("hosts")}
    assert "cli_retention_policy" not in columns
    assert "cli_retention_revision" not in columns
    engine.dispose()
