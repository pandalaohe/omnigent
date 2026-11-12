"""Expand task sources with nullable display and publication state."""

import sqlalchemy as sa
from alembic import op

revision = "a66c20261112"
down_revision = "a64c20261112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A downgrade retains these columns and their source data.
    existing = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("task_board_sources")
    }
    with op.batch_alter_table("task_board_sources") as batch:
        for column in (
            sa.Column("source_generation", sa.String(32), nullable=True),
            sa.Column("source_display", sa.JSON(), nullable=True),
            sa.Column("snapshot_state", sa.JSON(), nullable=True),
        ):
            if column.name not in existing:
                batch.add_column(column)


def downgrade() -> None:
    # UI rollback retains source data; schema contraction is a separate operation.
    pass
