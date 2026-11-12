"""Add hourly price evidence and account price aliases."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR

revision = "a68c20261112"
down_revision = "a67c20261112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    def key(length):
        return sa.String(length).with_variant(
            VARCHAR(length, charset="utf8mb4", collation="utf8mb4_0900_bin"), "mysql"
        )

    def identity():
        return [
            sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("owner_id", key(128), nullable=False, server_default=""),
            sa.Column("account_id", key(64), nullable=False, server_default=""),
        ]

    op.create_table(
        "usage_price_evidence",
        *identity(),
        sa.Column("hour_utc", sa.BigInteger(), nullable=False),
        sa.Column("model", key(128), nullable=False, server_default=""),
        sa.Column("low_usd", sa.String(40)),
        sa.Column("high_usd", sa.String(40)),
        sa.Column("state", sa.String(16)),
        sa.Column("missing", sa.JSON()),
        sa.Column("priced_units", sa.BigInteger()),
        sa.Column("usage_units", sa.BigInteger()),
        sa.Column("cache_savings_low_usd", sa.String(40)),
        sa.Column("cache_savings_high_usd", sa.String(40)),
        sa.Column("rates", sa.JSON()),
        sa.Column("priced_as", sa.JSON()),
        sa.Column("price_source", sa.String(256)),
        sa.Column("price_version", sa.String(128)),
        sa.Column("fetched_at", sa.BigInteger()),
        sa.Column("as_of", sa.BigInteger()),
        sa.Column("partitions", sa.JSON()),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_id", "account_id", "hour_utc", "model"),
    )
    op.create_table(
        "usage_price_aliases",
        *identity(),
        sa.Column("mappings", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.BigInteger()),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_id", "account_id"),
    )


def downgrade() -> None:
    op.drop_table("usage_price_aliases")
    op.drop_table("usage_price_evidence")
