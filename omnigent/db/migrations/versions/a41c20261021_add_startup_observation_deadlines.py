"""Persist fixed startup observation origins across server replicas."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a41c20261021"
down_revision: str = "a40c20261021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("session_startups", sa.Column("admitted_at", sa.BigInteger(), nullable=True))
    op.add_column(
        "session_startups", sa.Column("first_dispatch_at", sa.BigInteger(), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("session_startups") as batch_op:
        batch_op.drop_column("first_dispatch_at")
        batch_op.drop_column("admitted_at")
