"""Add immutable usage account pool relations.

Revision ID: a63c20261112
Revises: a63c20261111
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a63c20261112"
down_revision: str | Sequence[str] | None = "a63c20261111"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``usage_account_relations`` table."""
    op.create_table(
        "usage_account_relations",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("parent_account_id", sa.String(64), nullable=False),
        # The exporter's namespace + digest identity, never a display label.
        sa.Column("pool_id", sa.String(128), nullable=False),
        # usage | metered_cost | list_cost | quota
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("display_name", sa.String(64), nullable=True),
        # Epoch seconds of the UTC hour boundary; earlier rows stay at the parent.
        sa.Column("effective_from_hour", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_id", "account_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "owner_id",
            "parent_account_id",
            "pool_id",
            "channel",
            name="uq_usage_account_relations_logical",
        ),
        sa.CheckConstraint(
            "channel IN ('usage', 'metered_cost', 'list_cost', 'quota')",
            name="ck_usage_account_relations_channel",
        ),
    )
    op.create_index(
        "ix_usage_account_relations_owner_parent",
        "usage_account_relations",
        ["workspace_id", "owner_id", "parent_account_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``usage_account_relations`` table."""
    op.drop_index("ix_usage_account_relations_owner_parent", table_name="usage_account_relations")
    op.drop_table("usage_account_relations")
