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
    raise RuntimeError("Retain startup observation origins until attempts are reconciled")
