"""Add per-user browser annotation receiving references."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a46c20261022"
down_revision: str = "a45c20261022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "annotation_receivers",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("account_generation", sa.String(32), nullable=True),
        sa.Column("session_id", Uuid16(), nullable=True),
        sa.Column("session_archive_revision", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("set_at", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_annotation_receivers_session",
        "annotation_receivers",
        ["workspace_id", "session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_annotation_receivers_session", table_name="annotation_receivers")
    op.drop_table("annotation_receivers")
