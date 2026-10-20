"""Add durable owner-scoped usage feeds.

Revision ID: a32c20261020
Revises: a31c20261019
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary

from omnigent.db.db_models import Uuid16

revision: str = "a32c20261020"
down_revision: str | None = "a31c20261019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIGEST = sa.LargeBinary(32).with_variant(MySQLBinary(32), "mysql")


def upgrade() -> None:
    op.add_column("device_grants", sa.Column("scope", sa.String(32), nullable=True))
    op.execute(
        "UPDATE device_grants SET scope = 'sessions' "
        "WHERE client_id IS NULL OR client_id <> 'omnigent-cli'"
    )
    op.create_table(
        "usage_feeds",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_digest", _DIGEST, nullable=False),
        sa.Column("owner_scope_id", Uuid16(), nullable=False),
        sa.Column("feed_id", Uuid16(), nullable=False),
        sa.Column("feed_epoch", sa.Integer(), nullable=False),
        sa.Column("signing_key", _DIGEST, nullable=False),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False),
        sa.Column("read_timestamps", sa.Text(), nullable=False),
        sa.Column("progress", sa.Text(), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("workspace_id", "owner_digest"),
    )
    op.create_table(
        "usage_units",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_digest", _DIGEST, nullable=False),
        sa.Column("unit_digest", _DIGEST, nullable=False),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column("original_event_id", Uuid16(), nullable=False),
        sa.Column("latest_event_id", Uuid16(), nullable=False),
        sa.Column("latest_sequence", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("request_count", sa.SmallInteger(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "owner_digest", "unit_digest"),
    )
    op.create_table(
        "usage_call_allocations",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_digest", _DIGEST, nullable=False),
        sa.Column("call_digest", _DIGEST, nullable=False),
        sa.Column("first_unit_digest", _DIGEST, nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_digest", "call_digest"),
    )
    op.create_table(
        "usage_events",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_digest", _DIGEST, nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_digest", "sequence"),
    )
    op.create_table(
        "usage_producers",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_digest", _DIGEST, nullable=False),
        sa.Column("producer_digest", _DIGEST, nullable=False),
        sa.Column("producer_id", Uuid16(), nullable=False),
        sa.Column("binding", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_digest", "producer_digest"),
    )


def downgrade() -> None:
    op.drop_table("usage_producers")
    op.drop_table("usage_events")
    op.drop_table("usage_call_allocations")
    op.drop_table("usage_units")
    op.drop_table("usage_feeds")
    with op.batch_alter_table("device_grants") as batch_op:
        batch_op.drop_column("scope")
