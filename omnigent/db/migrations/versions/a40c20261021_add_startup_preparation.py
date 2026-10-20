"""Keep launch preparation intent durable before host-side work."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a40c20261021"
down_revision: str = "a39c20261020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("session_startups", sa.Column("preparation", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("session_startups") as batch_op:
        batch_op.drop_column("preparation")
