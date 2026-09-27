"""Migration coverage for durable session hand-offs."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache


def test_single_head_includes_session_handoffs() -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1
    lineage = {revision.revision for revision in script.iterate_revisions(heads[0], "base")}
    assert "a20c20260927" in lineage


def test_upgrade_and_downgrade_session_handoffs(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'handoffs.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    inspector = sa.inspect(engine)
    assert "session_handoffs" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("session_handoffs")}
    assert columns == {
        "id",
        "workspace_id",
        "owner_user_id",
        "sender_session_id",
        "receiver_session_id",
        "create_session",
        "project_id",
        "host_id",
        "root",
        "checkout",
        "worktree",
        "git_branch",
        "git_plan",
        "state",
        "reason",
        "brief_hash",
        "brief",
        "allow_onward",
        "parent_handoff_id",
        "disclosure",
        "brief_peer_id",
        "result_peer_id",
        "result_state",
        "stop_peer_id",
        "stop_state",
        "outcome",
        "lease_until",
        "created_at",
        "updated_at",
        "expires_at",
        "cancel_requested_at",
        "reported_at",
    }
    indexes = {index["name"] for index in inspector.get_indexes("session_handoffs")}
    assert indexes == {
        "ix_session_handoffs_owner_state",
        "ix_session_handoffs_sender_created",
        "ix_session_handoffs_receiver_state",
        "ix_session_handoffs_state_expires",
        "ix_session_handoffs_branch_state",
    }
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "a16c20260923")
    assert "session_handoffs" not in sa.inspect(engine).get_table_names()
    engine.dispose()
    clear_engine_cache()
