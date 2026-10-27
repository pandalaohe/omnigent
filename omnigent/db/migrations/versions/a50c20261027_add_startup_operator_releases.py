"""Retain exact operator releases separately from runtime stop evidence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a50c20261027"
down_revision: str = "a49c20261027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("session_startups", sa.Column("release_receipt", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("session_startups") as batch:
        batch.drop_column("release_receipt")
