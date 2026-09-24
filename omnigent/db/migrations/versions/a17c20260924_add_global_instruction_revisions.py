"""add global_instruction_revisions table

Revision ID: a17c20260924
Revises: a16c20260923
Create Date: 2026-09-24 00:00:00.000000

Adds the append-only ``global_instruction_revisions`` table backing the
server-wide "global instructions" text: one row per save, the newest row
in a workspace is the live value session initialization reads.

Timestamps are BigInteger epoch microseconds, the text is a compressed
blob (CompressedText → LargeBinary here, matching ``z6a2b3c4d5e6``), and
there are no foreign keys (schema Rule R032).

Downgrade drops the history; the live text goes with it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a17c20260924"
down_revision: str | None = "a16c20260923"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``global_instruction_revisions`` table and its index."""
    op.create_table(
        "global_instruction_revisions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        # UUID PK stored as 16 raw bytes (Uuid16 → BINARY(16) on MySQL, BLOB/BYTEA
        # elsewhere).
        sa.Column("id", Uuid16(), nullable=False),
        # Opaque free text stored compressed (CompressedText → LargeBinary).
        sa.Column("text", sa.LargeBinary(), nullable=False),
        sa.Column("created_us", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_global_instruction_revisions_latest",
        "global_instruction_revisions",
        ["workspace_id", "created_us", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``global_instruction_revisions`` table and its index."""
    op.drop_index(
        "ix_global_instruction_revisions_latest",
        table_name="global_instruction_revisions",
    )
    op.drop_table("global_instruction_revisions")
