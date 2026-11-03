"""add usage_hours table

Revision ID: zb1c2d3e4f5a
Revises: a53c20261103
Create Date: 2026-11-03 00:00:00.000000

Adds the ``usage_hours`` table: one row per (workspace, owner, account, UTC
hour, model, host, basis, priced) holding a running total of requests, tokens
and cost. Usage is accumulated at the two points that already record a turn's
spend, and by the ``activity`` of a pushed usage report.

The table is new, so it carries the tenant-partition ``workspace_id`` column as
the leading primary-key member (matching every other table after
``r1a2b3c4d5e6``). No foreign-key constraints (schema Rule R032): the owner and
account relationships are enforced by the application. Every key column is
non-nullable — the empty string stands for "unknown" — so the dialect upserts
compare on plain equality.

``priced`` is a 0/1 small integer rather than a BOOLEAN so the primary key stays
portable across SQLite, PostgreSQL, CockroachDB and MySQL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zb1c2d3e4f5a"
down_revision: str | None = "a53c20261103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``usage_hours`` table."""
    op.create_table(
        "usage_hours",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        # Epoch seconds of the UTC hour start the usage fell in.
        sa.Column("hour_utc", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("host_id", sa.String(64), nullable=False, server_default=""),
        # How the money was incurred, frozen at write time: metered,
        # subscription, estimate or unknown.
        sa.Column("basis", sa.String(16), nullable=False, server_default="unknown"),
        # 1 = cost_usd covers this row's tokens; 0 = token split kept for a
        # later one-time fill.
        sa.Column("priced", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("requests", sa.Integer(), nullable=True),
        sa.Column("failed", sa.Integer(), nullable=True),
        # JSON object of failure reason to count, when a source reports one.
        sa.Column("reasons", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.CheckConstraint("priced IN (0, 1)", name="ck_usage_hours_priced"),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "owner_id",
            "account_id",
            "hour_utc",
            "model",
            "host_id",
            "basis",
            "priced",
        ),
    )
    op.create_index(
        "ix_usage_hours_owner_hour",
        "usage_hours",
        ["workspace_id", "owner_id", "hour_utc"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``usage_hours`` table."""
    op.drop_index("ix_usage_hours_owner_hour", table_name="usage_hours")
    op.drop_table("usage_hours")
