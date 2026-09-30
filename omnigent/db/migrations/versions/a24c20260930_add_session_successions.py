"""Add the session_successions succession receipt table.

Revision ID: a24c20260930
Revises: a23c20260928

Creates ``session_successions``: one durable receipt per session
succession (a rotating session hands its live children to its
successor). The row carries the moved membership as JSON lists and the
phase the operation resumes from; the (workspace, old, new) triple is
unique so a repeat call on the same pair finds its receipt.

Downgrade drops the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a24c20260930"
down_revision: str | None = "a23c20260928"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the succession receipt table."""
    op.create_table(
        "session_successions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("old_id", Uuid16(), nullable=False),
        sa.Column("new_id", Uuid16(), nullable=False),
        sa.Column(
            "phase",
            sa.String(16),
            nullable=False,
            server_default="planned",
        ),
        sa.Column("direct_ids", sa.Text(), nullable=False),
        sa.Column("moved_ids", sa.Text(), nullable=False),
        sa.Column("opening", sa.Text(), nullable=True),
        sa.Column("opening_item_id", sa.String(128), nullable=True),
        sa.Column("dropped", sa.Text(), nullable=True),
        sa.Column("questions", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "old_id",
            "new_id",
            name="uq_session_successions_pair",
        ),
    )


def downgrade() -> None:
    """Drop the succession receipt table."""
    op.drop_table("session_successions")
