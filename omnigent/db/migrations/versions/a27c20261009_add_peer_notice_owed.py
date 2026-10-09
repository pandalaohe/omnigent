"""Add the peer back-notice owed mark.

Revision ID: a27c20261009
Revises: a26c20261009

Adds ``notice_owed_at`` (NULL) to ``session_peer_messages``. A non-NULL
value marks a terminal record whose back-notice to the sender has not
posted yet; the sweeper replays it after a restart and clears the mark
once claimed. Rows that predate the migration stay NULL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a27c20261009"
down_revision: str | None = "a26c20261009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the notice-owed column."""
    with op.batch_alter_table("session_peer_messages") as batch_op:
        batch_op.add_column(sa.Column("notice_owed_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the notice-owed column."""
    with op.batch_alter_table("session_peer_messages") as batch_op:
        batch_op.drop_column("notice_owed_at")
