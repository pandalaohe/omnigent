"""add stable archive timestamp

Revision ID: f8a1b2c3d4e5
Revises: f7a1b2c3d4e5
Create Date: 2026-09-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8a1b2c3d4e5"
down_revision: str | None = "f7a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add and backfill the timestamp used by archive filtering and sorting."""
    op.add_column(
        "conversations",
        sa.Column("archived_at", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE conversations SET archived_at = updated_at "
            "WHERE archived = true AND archived_at IS NULL"
        )
    )
    op.create_index(
        "ix_conversations_archived_archived_at",
        "conversations",
        ["workspace_id", "archived", "archived_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the stable archive timestamp and its list index."""
    op.drop_index(
        "ix_conversations_archived_archived_at",
        table_name="conversations",
    )
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("archived_at")
