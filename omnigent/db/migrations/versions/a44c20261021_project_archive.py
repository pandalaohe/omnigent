"""Add durable project archive state and task membership markers."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import CompressedText

revision: str = "a44c20261021"
down_revision: str | None = "a42c20261021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("archived_at", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("archive_state", CompressedText(), nullable=True))
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.add_column(sa.Column("paused_by_archive_batch", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_column("paused_by_archive_batch")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("archive_state")
        batch.drop_column("archived_at")
