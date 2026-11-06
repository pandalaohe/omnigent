"""Preserve exact source timestamps when ordering complete task reports."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a55c20261106"
down_revision: str = "a54c20261106"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("task_board_sources", sa.Column("complete_version", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_board_sources", "complete_version")
