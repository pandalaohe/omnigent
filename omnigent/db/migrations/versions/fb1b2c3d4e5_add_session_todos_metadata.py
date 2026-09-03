"""persist native-harness session plans across server restarts

Revision ID: fb1b2c3d4e5
Revises: fa1b2c3d4e5
Create Date: 2026-09-02 04:55:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fb1b2c3d4e5"
down_revision: str | None = "fa1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the unbounded JSON metadata field for the latest plan."""
    op.add_column(
        "omnigent_conversation_metadata",
        sa.Column("session_todos", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    """Remove persisted plan metadata."""
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("session_todos")
