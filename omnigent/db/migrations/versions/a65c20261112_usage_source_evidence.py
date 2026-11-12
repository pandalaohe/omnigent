"""Add source status snapshots and an insert-once source event archive.

Revision ID: a65c20261112
Revises: a66c20261112
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR

revision = "a65c20261112"
down_revision = "a66c20261112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    def key(length):
        return sa.String(length).with_variant(
            VARCHAR(length, charset="utf8mb4", collation="utf8mb4_0900_bin"), "mysql"
        )

    def scope():
        return [
            sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("owner_id", key(128), nullable=False, server_default=""),
            sa.Column("account_id", key(64), nullable=False, server_default=""),
            sa.Column("source", key(128), nullable=False, server_default=""),
        ]

    op.create_table(
        "usage_source_status",
        *scope(),
        sa.Column("state", sa.String(24), nullable=False),
        *[
            sa.Column(name, sa.BigInteger())
            for name in ("last_ok", "last_error_at", "observed_at", "fresh_until")
        ],
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_id", "account_id", "source"),
    )
    op.create_table(
        "usage_source_events",
        *scope(),
        sa.Column("event_id", key(128), nullable=False, server_default=""),
        sa.Column("type", sa.String(128), nullable=False),
        sa.Column("time_utc", sa.BigInteger(), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("data", sa.JSON()),
        sa.Column("received_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_id", "source", "event_id"),
    )
    op.create_index(
        "ix_usage_source_events_account_time",
        "usage_source_events",
        ["workspace_id", "owner_id", "account_id", "time_utc"],
    )


def downgrade() -> None:
    op.drop_table("usage_source_events")
    op.drop_table("usage_source_status")
