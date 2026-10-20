"""Fence old host channels before publishing a replacement registration."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a39c20261020"
down_revision: str = "a38c20261020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session_startup_budgets", sa.Column("current_generation", sa.String(128), nullable=True)
    )


def downgrade() -> None:
    raise RuntimeError("Retain host generation fences until attempts are reconciled")
