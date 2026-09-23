"""Add durable session hand-off records.

Revision ID: a17c20260923
Revises: a16c20260923
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a17c20260923"
down_revision: str | None = "a16c20260923"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create hand-off records and their lookup indexes."""
    op.create_table(
        "session_handoffs",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("owner_user_id", sa.String(128), nullable=False),
        sa.Column("sender_session_id", Uuid16(), nullable=False),
        sa.Column("receiver_session_id", Uuid16(), nullable=False),
        sa.Column("create_session", sa.Boolean(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("host_id", Uuid16(), nullable=True),
        sa.Column("root", sa.Text(), nullable=True),
        sa.Column("git_branch", sa.String(255), nullable=True),
        sa.Column("git_plan", sa.LargeBinary(), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(128), nullable=True),
        sa.Column("brief_hash", sa.String(64), nullable=False),
        sa.Column("brief", sa.LargeBinary(), nullable=False),
        sa.Column("allow_onward", sa.Boolean(), nullable=False),
        sa.Column("parent_handoff_id", Uuid16(), nullable=True),
        sa.Column("disclosure", sa.LargeBinary(), nullable=True),
        sa.Column("brief_peer_id", Uuid16(), nullable=False),
        sa.Column("result_peer_id", Uuid16(), nullable=True),
        sa.Column("result_state", sa.String(32), nullable=True),
        sa.Column("stop_peer_id", Uuid16(), nullable=True),
        sa.Column("stop_state", sa.String(32), nullable=True),
        sa.Column("outcome", sa.LargeBinary(), nullable=True),
        sa.Column("lease_until", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("cancel_requested_at", sa.Integer(), nullable=True),
        sa.Column("reported_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    for name, columns in (
        ("owner_state", ["owner_user_id", "state"]),
        ("sender_created", ["sender_session_id", "created_at"]),
        ("receiver_state", ["receiver_session_id", "state"]),
        ("state_expires", ["state", "expires_at"]),
        ("branch_state", ["host_id", "root", "git_branch", "state"]),
    ):
        if name == "branch_state":
            op.create_index(
                f"ix_session_handoffs_{name}",
                "session_handoffs",
                ["workspace_id", *columns],
                mysql_length={"root": 191},
            )
        else:
            op.create_index(
                f"ix_session_handoffs_{name}", "session_handoffs", ["workspace_id", *columns]
            )


def downgrade() -> None:
    """Drop hand-off records."""
    op.drop_table("session_handoffs")
