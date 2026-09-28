"""Add peer relay depth and not-before scheduling.

Revision ID: a22c20260928
Revises: a21c20260927

Adds ``relay_depth`` (NOT NULL, default 1) and ``not_before`` (NULL) to
``session_peer_messages``. ``relay_depth`` counts hops since the sending
session's latest human input; rows that predate the migration read 1.
``not_before`` defers a rate-delayed record's delivery until its slot.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a22c20260928"
down_revision: str | None = "a21c20260927"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the relay-depth and not-before columns."""
    with op.batch_alter_table("session_peer_messages") as batch_op:
        batch_op.add_column(
            sa.Column("relay_depth", sa.Integer(), nullable=False, server_default="1"),
        )
        batch_op.add_column(sa.Column("not_before", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the relay-depth and not-before columns."""
    with op.batch_alter_table("session_peer_messages") as batch_op:
        batch_op.drop_column("not_before")
        batch_op.drop_column("relay_depth")
