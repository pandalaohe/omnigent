"""Keep the provenance of recorded native valuations."""

import sqlalchemy as sa
from alembic import op

revision = "a70c20261113"
down_revision = "a69c20261113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_hours", sa.Column("price_ref", sa.String(160), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("usage_hours") as batch_op:
        batch_op.drop_column("price_ref")
