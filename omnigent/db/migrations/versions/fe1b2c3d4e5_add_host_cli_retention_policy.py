"""add Host CLI retention policy

Revision ID: fe1b2c3d4e5
Revises: ge1b2c3d4e5f
Create Date: 2026-09-08 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fe1b2c3d4e5"
down_revision: str | None = "ge1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist an optional versioned policy and its compare-and-swap revision."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("cli_retention_policy", sa.LargeBinary(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "cli_retention_revision",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    """Remove the Host CLI retention policy columns."""
    connection = op.get_bind()
    active_policy = connection.execute(
        sa.text("SELECT 1 FROM hosts WHERE cli_retention_policy IS NOT NULL LIMIT 1")
    ).first()
    if active_policy is not None:
        raise RuntimeError(
            "Cannot downgrade fe1b2c3d4e5 while a Host CLI retention policy is active; "
            "reset every Host to legacy retention first"
        )
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("cli_retention_revision")
        batch_op.drop_column("cli_retention_policy")
