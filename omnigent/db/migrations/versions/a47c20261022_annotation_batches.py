"""Add durable browser annotation receipt and capture ownership."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a47c20261022"
down_revision: str = "a46c20261022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "annotation_grant_owners",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("grant_id", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("account_generation", sa.String(32), nullable=False),
        sa.Column("approved_at", sa.Integer(), nullable=False),
    )
    op.create_table(
        "annotation_receipts",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("grant_id", sa.String(128), primary_key=True),
        sa.Column("batch_id", Uuid16(), primary_key=True),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("item_id", Uuid16(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Integer(), nullable=False),
        sa.Column("dispatch_phase", sa.String(16), nullable=False),
        sa.Column("retry_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_annotation_receipts_item",
        "annotation_receipts",
        ["workspace_id", "session_id", "item_id"],
    )
    op.create_table(
        "annotation_captures",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("grant_id", sa.String(128), primary_key=True),
        sa.Column("capture_id", Uuid16(), primary_key=True),
        sa.Column("batch_id", Uuid16(), nullable=False),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("file_id", Uuid16(), nullable=True),
        sa.Column("expires_at", sa.Integer(), nullable=True),
        sa.Column("upload_state", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("committed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_annotation_captures_expiry",
        "annotation_captures",
        ["workspace_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_annotation_captures_expiry", table_name="annotation_captures")
    op.drop_table("annotation_captures")
    op.drop_index("ix_annotation_receipts_item", table_name="annotation_receipts")
    op.drop_table("annotation_receipts")
    op.drop_table("annotation_grant_owners")
