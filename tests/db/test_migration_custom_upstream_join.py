"""The upstream and custom schema lineages join without losing custom state."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, _initialize_or_verify_schema, _run_migrations


@pytest.mark.parametrize("start", ["ff1b2c3d4e5", "ge1b2c3d4e5f", "legacy-custom-ge"])
@pytest.mark.parametrize("manual", [False, True])
def test_join_preserves_existing_data(tmp_path: Path, start: str, manual: bool) -> None:
    uri = f"sqlite:///{tmp_path / 'join.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    custom = start != "ge1b2c3d4e5f"
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "a09c20260909" if start == "legacy-custom-ge" else start)
        if start == "legacy-custom-ge":
            conn.execute(sa.text("UPDATE alembic_version SET version_num = 'ge1b2c3d4e5f'"))
        conn.execute(
            sa.text("INSERT INTO users (workspace_id, id, is_admin) VALUES (0, 'user', 0)")
        )
        conn.execute(
            sa.text(
                "INSERT INTO hosts (workspace_id,host_id,user_id,name,status) "
                "VALUES (0,'host','user','preserved',2)"
            )
        )
        if custom:
            conn.execute(
                sa.text("UPDATE users SET preferences = :value WHERE id = 'user'"),
                {"value": b"exact-custom-preferences"},
            )
        if start == "ff1b2c3d4e5":
            conn.execute(
                sa.text(
                    "UPDATE hosts SET cli_retention_policy = :value, cli_retention_revision = 7"
                ),
                {"value": b"exact-retention-policy"},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO cli_release_intents "
                    "(workspace_id,id,dedupe_key,reason,root_session_id,target_session_id,"
                    "status,next_attempt_at,created_at,updated_at) "
                    "VALUES (0,'intent','dedupe','archive',:id,:id,'pending',0,1,1)"
                ),
                {"id": bytes.fromhex("00112233445566778899aabbccddeeff")},
            )

    migrate = _run_migrations if manual else _initialize_or_verify_schema
    migrate(engine, uri)
    migrate(engine, uri)
    columns = {column["name"] for column in sa.inspect(engine).get_columns("users")}
    assert "background_session_titles_enabled" not in columns
    assert "preferences" in columns
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "a10c20260910"
        assert conn.scalar(sa.text("SELECT name FROM hosts WHERE host_id = 'host'")) == "preserved"
        if custom:
            assert (
                conn.scalar(sa.text("SELECT preferences FROM users WHERE id = 'user'"))
                == b"exact-custom-preferences"
            )
        if start == "ff1b2c3d4e5":
            assert conn.execute(
                sa.text("SELECT cli_retention_policy, cli_retention_revision FROM hosts")
            ).one() == (b"exact-retention-policy", 7)
            assert (
                conn.scalar(sa.text("SELECT status FROM cli_release_intents WHERE id = 'intent'"))
                == "pending"
            )
    engine.dispose()
