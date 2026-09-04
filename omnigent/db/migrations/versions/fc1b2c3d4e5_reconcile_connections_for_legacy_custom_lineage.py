"""Reconcile connections for databases upgraded on the old custom lineage.

Revision ID: fc1b2c3d4e5
Revises: fb1b2c3d4e5
Create Date: 2026-09-04 00:00:00.000000

Before the upstream connections migration landed, the local custom migration
chain started directly at ``e5d9bc8ac650``. Existing deployments may therefore
already be stamped at ``fb1b2c3d4e5`` without having executed
``ga1b2c3d4e5f``. Fresh databases execute the upstream migration normally; this
repair creates the table only when that legacy stamp/schema mismatch exists.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fc1b2c3d4e5"
down_revision: str | None = "fb1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create connections when a legacy custom database skipped its migration."""
    if sa.inspect(op.get_bind()).has_table("connections"):
        return

    op.create_table(
        "connections",
        sa.Column(
            "workspace_id", sa.BigInteger, primary_key=True, nullable=False, server_default="0"
        ),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("provider", sa.String(64), primary_key=True),
        sa.Column(
            "account_id", sa.String(128), primary_key=True, nullable=False, server_default=""
        ),
        sa.Column("secret_enc", sa.Text, nullable=False),
        sa.Column("metadata_json", sa.Text, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    """Leave the upstream-owned table intact when removing the repair revision."""
