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
    op.drop_column("usage_hours", "price_ref")
