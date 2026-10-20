"""Retain an exact recovery operation and work fence across replicas."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a38c20261020"
down_revision: str = "a37c20261020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("work_revision", "work_fence_epoch"):
        op.add_column(
            "session_startup_budgets",
            sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"),
        )
    op.add_column("session_startup_budgets", sa.Column("recovery", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("session_startup_budgets") as batch_op:
        batch_op.drop_column("recovery")
        batch_op.drop_column("work_fence_epoch")
        batch_op.drop_column("work_revision")
