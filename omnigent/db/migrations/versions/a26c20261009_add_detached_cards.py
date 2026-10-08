"""Add the detached_cards table.

Revision ID: a26c20261009
Revises: a25c20261004

Creates ``detached_cards``: one durable row per detached card (deferred
approval or async question) so a server restart can re-park the card,
re-deliver a verdict still owed to the agent and re-arm an accepted
approval's one-shot grant.

Downgrade drops the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a26c20261009"
down_revision: str | None = "a25c20261004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the detached card table."""
    op.create_table(
        "detached_cards",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("elicitation_id", sa.String(64), nullable=False),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("mirror", sa.Boolean(), nullable=False),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("grant_key", sa.Text(), nullable=True),
        sa.Column("verdict", sa.Text(), nullable=True),
        sa.Column("delivery_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "elicitation_id"),
    )
    op.create_index("ix_detached_cards_session", "detached_cards", ["workspace_id", "session_id"])


def downgrade() -> None:
    """Drop the detached card table."""
    op.drop_index("ix_detached_cards_session", table_name="detached_cards")
    op.drop_table("detached_cards")
