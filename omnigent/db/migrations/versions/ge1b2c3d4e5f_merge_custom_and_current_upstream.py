"""Merge the durable custom schema with the locked upstream schema.

Revision ID: ge1b2c3d4e5f
Revises: fd1b2c3d4e5, gd1b2c3d4e5f
Create Date: 2026-09-09 00:00:00.000000

Older custom deployments used ``gc1b2c3d4e5f`` for their merge revision
before upstream assigned the same identifier to the managed-Host tombstone.
Startup records the already-present custom branch as ``fd1b2c3d4e5`` before
upgrading.  This merge then repairs the one upstream column that the collided
stamp could have skipped.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ge1b2c3d4e5f"
down_revision: tuple[str, str] = ("fd1b2c3d4e5", "gd1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Repair the upstream Host tombstone column for legacy custom stamps."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "deleted_at" not in {column["name"] for column in inspector.get_columns("hosts")}:
        with op.batch_alter_table("hosts") as batch_op:
            batch_op.add_column(sa.Column("deleted_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Split the lineages without dropping the upstream-owned column."""
