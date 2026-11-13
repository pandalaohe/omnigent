"""Allow subscription renewals and account cost classes without inventing payments."""

import sqlalchemy as sa
from alembic import op

revision = "a71c20261113"
down_revision = "a70c20261113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_pricing_defaults", sa.Column("account_classes", sa.Text(), nullable=True))
    with op.batch_alter_table("usage_pricing_cards") as batch:
        batch.alter_column("allowance_usd", existing_type=sa.Numeric(38, 18), nullable=True)


def downgrade() -> None:
    # A repeating subscription without an allowance cannot be represented by the old schema.
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM usage_pricing_cards WHERE allowance_usd IS NULL LIMIT 1"))
        .first()
    ):
        raise RuntimeError("cannot downgrade subscriptions without allowance")
    with op.batch_alter_table("usage_pricing_cards") as batch:
        batch.alter_column("allowance_usd", existing_type=sa.Numeric(38, 18), nullable=False)
    op.drop_column("usage_pricing_defaults", "account_classes")
