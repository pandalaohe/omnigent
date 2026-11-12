"""Add interval keyed speed histograms.

Revision ID: a67c20261112
Revises: a65c20261112
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR

revision = "a67c20261112"
down_revision = "a65c20261112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    def key(length):
        return sa.String(length).with_variant(
            VARCHAR(length, charset="utf8mb4", collation="utf8mb4_0900_bin"), "mysql"
        )

    op.create_table(
        "usage_speed_points",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_id", key(128), nullable=False, server_default=""),
        sa.Column("account_id", key(64), nullable=False, server_default=""),
        sa.Column("model", key(128), nullable=False, server_default=""),
        sa.Column("metric", sa.String(16), nullable=False),
        sa.Column("start_utc", sa.BigInteger(), nullable=False),
        sa.Column("end_utc", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.String(16), nullable=False),
        sa.Column("bounds", sa.JSON()),
        sa.Column("bucket_counts", sa.JSON()),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.Column("coverage", sa.String(16)),
        sa.Column("description", sa.String(256)),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "owner_id", "account_id", "model", "metric", "start_utc", "end_utc"
        ),
    )


def downgrade() -> None:
    op.drop_table("usage_speed_points")
