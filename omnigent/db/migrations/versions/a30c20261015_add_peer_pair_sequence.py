"""Assign durable admission order to peer-message pairs.

Revision ID: a30c20261015
Revises: a29c20261010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a30c20261015"
down_revision: str | None = "a29c20261010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_session_peer_messages_pair_sequence"
_TABLE = "session_peer_messages"


def upgrade() -> None:
    """Add the nullable column and number historical rows per pair."""
    op.add_column(_TABLE, sa.Column("pair_sequence", sa.BigInteger(), nullable=True))
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT workspace_id, sender_session_id, receiver_session_id, id "
            "FROM session_peer_messages "
            "ORDER BY workspace_id, sender_session_id, receiver_session_id, created_at, id"
        )
    )
    previous_pair: tuple[object, object, object] | None = None
    sequence = 0
    for workspace_id, sender_id, receiver_id, peer_id in rows:
        pair = (workspace_id, sender_id, receiver_id)
        sequence = sequence + 1 if pair == previous_pair else 1
        previous_pair = pair
        conn.execute(
            sa.text(
                "UPDATE session_peer_messages SET pair_sequence = :sequence "
                "WHERE workspace_id = :workspace_id AND id = :peer_id"
            ),
            {"sequence": sequence, "workspace_id": workspace_id, "peer_id": peer_id},
        )
    op.create_index(
        _INDEX,
        _TABLE,
        ["workspace_id", "sender_session_id", "receiver_session_id", "pair_sequence"],
    )


def downgrade() -> None:
    """Remove the order metadata while preserving peer-message rows."""
    op.drop_index(_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("pair_sequence")
