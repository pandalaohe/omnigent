"""add deletion ownership claim

Revision ID: f9a1b2c3d4e5
Revises: f8a1b2c3d4e5
Create Date: 2026-09-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9a1b2c3d4e5"
down_revision: str | None = "f8a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the opaque, stale-recoverable deletion ownership fields."""
    op.add_column(
        "conversations",
        sa.Column(
            "archive_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE conversations SET archive_locked = true "
            "WHERE EXISTS ("
            "SELECT 1 FROM conversation_labels "
            "WHERE conversation_labels.workspace_id = conversations.workspace_id "
            "AND conversation_labels.conversation_id = conversations.id "
            "AND conversation_labels.key = 'omnigent.archive_locked' "
            "AND conversation_labels.value = '1'"
            ")"
        )
    )
    op.add_column(
        "conversations",
        sa.Column("deletion_claim_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("deletion_claimed_at", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Remove deletion claims; old Servers safely return to lock-only data."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("deletion_claimed_at")
        batch_op.drop_column("deletion_claim_token")
        batch_op.drop_column("archive_locked")
