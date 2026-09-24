"""add project host entries and the session worktree column

Revision ID: a17c20260924
Revises: a16c20260923
Create Date: 2026-09-24 00:00:00.000000

Adds ``project_host_entries`` (one project directory per host, independent of
the repository/host bindings) and the nullable
``omnigent_conversation_metadata.worktree`` column (the session's working tree
when it differs from its launch directory ``workspace``).

No data step: a project without entries resolves exactly as before, and legacy
rows read a NULL ``worktree`` as "the launch directory is the working tree".
Column adds/drops go through ``op.batch_alter_table`` so the chain stays
runnable on SQLite.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a17c20260924"
down_revision: str | None = "a16c20260923"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the entries table and add the session worktree column."""
    op.create_table(
        "project_host_entries",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("host_id", Uuid16(), nullable=False),
        sa.Column("workspace", sa.String(2048), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "host_id"),
    )
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("worktree", sa.String(2048), nullable=True))


def downgrade() -> None:
    """Drop the session worktree column and the entries table."""
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("worktree")
    op.drop_table("project_host_entries")
