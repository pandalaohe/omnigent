"""Add source-owned JSCalendar task reports and append-only history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR

revision: str = "a54c20261106"
down_revision: str = "zb1c2d3e4f5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _key(length: int) -> sa.String:
    # Preserve case and trailing spaces in opaque source-provided identifiers.
    return sa.String(length).with_variant(
        VARCHAR(length, charset="utf8mb4", collation="utf8mb4_0900_bin"), "mysql"
    )


def _identity() -> list[sa.Column]:
    return [
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_id", _key(128), nullable=False),
        sa.Column("source_id", _key(64), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "task_board_sources",
        *_identity(),
        sa.Column("name", sa.String(128)),
        sa.Column("source", sa.Text()),
        sa.Column("created_at", sa.Double(), nullable=False),
        sa.Column("last_write", sa.JSON()),
        sa.Column("complete_updated", sa.Double()),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_id", "source_id"),
    )
    op.create_table(
        "task_board_items",
        *_identity(),
        sa.Column("uid", _key(255), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("progress", sa.String(16), nullable=False),
        sa.Column("group_id", _key(64), nullable=False),
        sa.Column("source_updated", sa.Double(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("closed_at", sa.Double()),
        sa.Column("closed_seen_at", sa.Double()),
        sa.Column("changed_at", sa.Double(), nullable=False),
        sa.Column("gone_at", sa.Double()),
        sa.PrimaryKeyConstraint("workspace_id", "owner_id", "source_id", "uid"),
    )
    op.create_table(
        "task_board_history",
        *_identity(),
        sa.Column("uid", _key(255), nullable=False),
        sa.Column("entry_id", _key(128), nullable=False),
        sa.Column("time", sa.Double(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("entry", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_id", "source_id", "uid", "entry_id"),
    )
    for suffix, column in (("changed", "changed_at"), ("closed", "closed_at")):
        op.create_index(
            "ix_task_board_items_" + suffix,
            "task_board_items",
            ["workspace_id", "owner_id", column],
        )
    op.create_index(
        "ix_task_board_history_time", "task_board_history", ["workspace_id", "owner_id", "time"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_board_history_time", table_name="task_board_history")
    op.drop_table("task_board_history")
    op.drop_index("ix_task_board_items_closed", table_name="task_board_items")
    op.drop_index("ix_task_board_items_changed", table_name="task_board_items")
    op.drop_table("task_board_items")
    op.drop_table("task_board_sources")
