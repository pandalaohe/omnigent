"""Persist one physical cold-boundary grant per startup attempt."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a37c20261020"
down_revision: str = "a36c20261020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session_startups",
        sa.Column("cold_claims", sa.JSON(), nullable=False, server_default=sa.text("('{}')")),
    )
    op.add_column("session_startups", sa.Column("host_generation", sa.String(128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("session_startups") as batch_op:
        batch_op.drop_column("host_generation")
        batch_op.drop_column("cold_claims")
