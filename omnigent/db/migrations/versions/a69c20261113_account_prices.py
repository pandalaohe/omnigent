"""Add nullable account model price overrides."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR

revision = "a69c20261113"
down_revision = "a68c20261112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    def key(length):
        return sa.String(length).with_variant(
            VARCHAR(length, charset="utf8mb4", collation="utf8mb4_0900_bin"), "mysql"
        )

    op.create_table(
        "usage_price_overrides",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_id", key(128), nullable=False),
        sa.Column("account_id", key(64), nullable=False),
        sa.Column("model", key(128), nullable=False),
        *[
            sa.Column(name + "_rate", sa.Numeric(38, 18, asdecimal=True))
            for name in ("read", "write", "cache_read", "cache_write")
        ],
        sa.Column("payload", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_id", "account_id", "model"),
    )


def downgrade() -> None:
    op.drop_table("usage_price_overrides")
