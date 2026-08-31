"""add per-host default workspace

Revision ID: f6a1b2c3d4e5
Revises: e5d9bc8ac650
Create Date: 2026-08-31 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a1b2c3d4e5"
down_revision: str | None = "e5d9bc8ac650"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the optional host-native starting directory."""
    op.add_column(
        "hosts",
        sa.Column("default_workspace", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    """Remove the per-host starting directory."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("default_workspace")
