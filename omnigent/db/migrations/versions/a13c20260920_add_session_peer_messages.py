"""add the session_peer_messages table for durable peer-message records

Revision ID: a13c20260920
Revises: a12c20260913
Create Date: 2026-09-20 00:00:00.000000

Adds the ``session_peer_messages`` table backing peer messaging between
sessions the same user owns: one row per send that cannot be delivered
inline (receiver busy, offline, policy hold), swept by a server
background task into delivered / failed / expired / refused_by_user.

Cross-table references are application-owned, never DB foreign keys
(schema Rule R032). Timestamps are Integer epoch seconds; the message
text is a compressed blob (CompressedText → LargeBinary here, matching
``z6a2b3c4d5e6``).

Downgrade is destructive by definition: peer-message history has no
home in the old schema. Any record not in a terminal state is lost. A
pre-drop count of non-terminal rows is logged so the operator sees what
they are discarding in the migration output rather than afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a13c20260920"
down_revision: str | None = "a12c20260913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_logger = logging.getLogger(__name__)


def upgrade() -> None:
    """Create the ``session_peer_messages`` table and its three indexes."""
    op.create_table(
        "session_peer_messages",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("sender_session_id", Uuid16(), nullable=False),
        sa.Column("receiver_session_id", Uuid16(), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("ref", sa.String(64), nullable=False),
        # Opaque free text stored compressed (CompressedText → LargeBinary).
        sa.Column("text", sa.LargeBinary(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("reply_peer_id", Uuid16(), nullable=True),
        sa.Column("replied_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_session_peer_messages_receiver",
        "session_peer_messages",
        ["workspace_id", "receiver_session_id", "state", "id"],
        unique=False,
    )
    op.create_index(
        "ix_session_peer_messages_sender",
        "session_peer_messages",
        ["workspace_id", "sender_session_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_session_peer_messages_ref",
        "session_peer_messages",
        ["workspace_id", "ref", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``session_peer_messages`` table and its indexes."""
    connection = op.get_bind()
    non_terminal = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM session_peer_messages WHERE state IN "
            "('pending', 'queued', 'held', 'delivering')"
        )
    ).scalar()
    _logger.warning(
        "downgrading a13c20260920 with %s non-terminal peer messages "
        "(pending/queued/held/delivering); their history is lost",
        non_terminal,
    )
    op.drop_index("ix_session_peer_messages_ref", table_name="session_peer_messages")
    op.drop_index("ix_session_peer_messages_sender", table_name="session_peer_messages")
    op.drop_index("ix_session_peer_messages_receiver", table_name="session_peer_messages")
    op.drop_table("session_peer_messages")
