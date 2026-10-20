"""Allow operators to lower the two-attempt startup ceiling."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a36c20261020"
down_revision: str = "a35c20261020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session_startup_budgets",
        sa.Column("startup_limit", sa.Integer(), nullable=False, server_default="2"),
    )


def downgrade() -> None:
    raise RuntimeError("Retain configured startup limits until admission is paused")
