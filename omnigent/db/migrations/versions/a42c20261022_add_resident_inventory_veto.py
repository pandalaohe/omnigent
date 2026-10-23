"""Retain unknown resident coverage independently of the recovery work fence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a42c20261022"
down_revision: str = "a47c20261022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session_startup_budgets",
        sa.Column(
            "resident_inventory_unknown", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("session_startup_budgets") as batch_op:
        batch_op.drop_column("resident_inventory_unknown")
