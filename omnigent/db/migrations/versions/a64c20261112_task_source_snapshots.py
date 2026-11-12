"""Expand task sources with nullable display and publication state."""

import sqlalchemy as sa
from alembic import op

revision = "a64c20261112"
down_revision = "a63c20261111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("task_board_sources") as batch:
        batch.add_column(sa.Column("source_generation", sa.String(32), nullable=True))
        batch.add_column(sa.Column("source_display", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("snapshot_state", sa.JSON(), nullable=True))


def downgrade() -> None:
    # UI rollback retains source data; schema contraction is a separate operation.
    pass
