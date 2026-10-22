"""Store display labels and credential activity for connected apps."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a45c20261022"
down_revision: str = "a44c20261021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("device_grants") as batch:
        batch.add_column(sa.Column("label", sa.String(80), nullable=True))
        batch.add_column(sa.Column("last_used_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("device_grants") as batch:
        batch.drop_column("last_used_at")
        batch.drop_column("label")
