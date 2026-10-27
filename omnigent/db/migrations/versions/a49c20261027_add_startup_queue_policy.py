"""Bind opening attempts to their admission mode and policy revision."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a49c20261027"
down_revision: str = "a48c20261026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("session_startup_budgets", "session_startups"):
        op.add_column(
            table,
            sa.Column("admission_mode", sa.String(32), nullable=False, server_default="guarded"),
        )
        op.add_column(
            table, sa.Column("policy_revision", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    for table in ("session_startups", "session_startup_budgets"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("policy_revision")
            batch.drop_column("admission_mode")
