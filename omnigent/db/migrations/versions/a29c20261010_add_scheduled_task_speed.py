"""Add an optional speed override to scheduled tasks.

Revision ID: a29c20261010
Revises: a28c20261009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a29c20261010"
down_revision: str | None = "a28c20261009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scheduled_tasks", sa.Column("speed", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("speed")
