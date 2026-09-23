"""Add the host process platform to hosts.

Revision ID: a16c20260923
Revises: a15c20260922
Create Date: 2026-09-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a16c20260923"
down_revision: str | None = "a15c20260922"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a nullable platform column for old host rows."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("platform", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the host platform column."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("platform")
