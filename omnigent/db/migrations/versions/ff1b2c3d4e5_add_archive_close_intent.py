"""add durable archive CLI close intent

Revision ID: ff1b2c3d4e5
Revises: fe1b2c3d4e5
Create Date: 2026-09-08 00:00:01.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "ff1b2c3d4e5"
down_revision: str | None = "fe1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Store archive revisions, retryable close work, and its short lease."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(
            sa.Column("cli_retention_claim_token", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(sa.Column("cli_retention_claimed_at", sa.Integer(), nullable=True))
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "archive_revision",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column("archive_close_requested_revision", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("archive_close_completed_revision", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("archive_close_claim_token", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(sa.Column("archive_close_claimed_at", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("archive_close_last_error", sa.String(length=512), nullable=True)
        )
    op.create_table(
        "cli_release_intents",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=512), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("root_session_id", Uuid16(), nullable=False),
        sa.Column("target_session_id", Uuid16(), nullable=False),
        sa.Column("host_id", sa.String(length=128), nullable=True),
        sa.Column("runner_id", sa.String(length=128), nullable=True),
        sa.Column("family", sa.String(length=64), nullable=True),
        sa.Column("policy_revision", sa.Integer(), nullable=True),
        sa.Column("archive_revision", sa.Integer(), nullable=True),
        sa.Column("runtime_generation", sa.String(length=128), nullable=True),
        sa.Column("activity_token", sa.String(length=256), nullable=True),
        sa.Column("idle_threshold_seconds", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.Integer(), nullable=True),
        sa.Column("next_attempt_at", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint("workspace_id", "dedupe_key", name="uq_cli_release_intents_dedupe"),
    )
    op.create_index(
        "ix_cli_release_intents_due",
        "cli_release_intents",
        ["workspace_id", "status", "next_attempt_at", "id"],
    )
    op.create_index(
        "ix_cli_release_intents_host",
        "cli_release_intents",
        ["workspace_id", "host_id", "status", "id"],
    )
    op.create_index(
        "ix_cli_release_intents_root",
        "cli_release_intents",
        ["workspace_id", "root_session_id", "archive_revision", "status"],
    )


def downgrade() -> None:
    """Remove durable archive close intent state."""
    connection = op.get_bind()
    active_intent = connection.execute(
        sa.text("SELECT 1 FROM cli_release_intents WHERE status IN ('pending', 'claimed') LIMIT 1")
    ).first()
    incomplete_archive = connection.execute(
        sa.text(
            "SELECT 1 FROM conversations "
            "WHERE archive_close_requested_revision IS NOT NULL "
            "AND (archive_close_completed_revision IS NULL "
            "OR archive_close_completed_revision != archive_close_requested_revision) LIMIT 1"
        )
    ).first()
    active_host_claim = connection.execute(
        sa.text("SELECT 1 FROM hosts WHERE cli_retention_claim_token IS NOT NULL LIMIT 1")
    ).first()
    if (
        active_intent is not None
        or incomplete_archive is not None
        or active_host_claim is not None
    ):
        raise RuntimeError(
            "Cannot downgrade ff1b2c3d4e5 while CLI release work, archive close work, "
            "or a Host retention lease is active; drain lifecycle work first"
        )
    op.drop_index("ix_cli_release_intents_root", table_name="cli_release_intents")
    op.drop_index("ix_cli_release_intents_host", table_name="cli_release_intents")
    op.drop_index("ix_cli_release_intents_due", table_name="cli_release_intents")
    op.drop_table("cli_release_intents")
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("archive_close_last_error")
        batch_op.drop_column("archive_close_claimed_at")
        batch_op.drop_column("archive_close_claim_token")
        batch_op.drop_column("archive_close_completed_revision")
        batch_op.drop_column("archive_close_requested_revision")
        batch_op.drop_column("archive_revision")
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("cli_retention_claimed_at")
        batch_op.drop_column("cli_retention_claim_token")
