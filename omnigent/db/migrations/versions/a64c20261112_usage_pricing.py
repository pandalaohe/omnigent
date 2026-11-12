"""Add user purchase baselines and measured quota closings.

Revision ID: a64c20261112
Revises: a63c20261112
"""

import sqlalchemy as sa
from alembic import op

revision = "a64c20261112"
down_revision = "a63c20261112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    def scope():
        return [
            sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("owner_id", sa.String(128), nullable=False),
        ]

    op.create_table(
        "usage_pricing_defaults",
        *scope(),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("default_discount", sa.Numeric(38, 18, asdecimal=True), nullable=False),
        *[
            sa.Column(name, sa.Numeric(38, 18, asdecimal=True))
            for name in ("cny_fx", "usd_fx", "hkd_fx")
        ],
        sa.Column("payload", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_id"),
    )
    op.create_table(
        "usage_pricing_cards",
        *scope(),
        sa.Column("card_id", sa.String(128), nullable=False),
        sa.Column("retail_amount", sa.Numeric(38, 18, asdecimal=True), nullable=False),
        sa.Column("allowance_usd", sa.Numeric(38, 18, asdecimal=True), nullable=False),
        *[
            sa.Column(name, sa.Numeric(38, 18, asdecimal=True))
            for name in (
                "discount_override",
                "read_rate",
                "write_rate",
                "cache_read_rate",
                "cache_write_rate",
            )
        ],
        sa.Column("payload", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_id", "card_id"),
    )
    op.create_table(
        "usage_quota_closings",
        *scope(),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("window_id", sa.String(32), nullable=False),
        sa.Column("resets_at", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "owner_id", "account_id", "window_id", "resets_at"
        ),
    )


def downgrade() -> None:
    op.drop_table("usage_quota_closings")
    op.drop_table("usage_pricing_cards")
    op.drop_table("usage_pricing_defaults")
