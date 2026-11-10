"""Add personal API key metadata and one-way credential digests."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a58c20261110"
down_revision: str = "a57c20261106"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_api_keys",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("secret_sha256", sa.String(64), nullable=False),
        sa.Column("scopes", sa.String(255), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=True),
        sa.Column("last_used_at", sa.Integer(), nullable=True),
        sa.Column("regenerated_at", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_user_api_keys_digest", "user_api_keys", ["workspace_id", "secret_sha256"], unique=True
    )
    op.create_index(
        "ix_user_api_keys_name", "user_api_keys", ["workspace_id", "user_id", "name"], unique=True
    )
    op.create_index("ix_user_api_keys_owner", "user_api_keys", ["workspace_id", "user_id"])


def downgrade() -> None:
    op.drop_table("user_api_keys")
