"""Widen hosts.deleted_at and index managed-sandbox reaper scans.

Revision ID: gg1b2c3d4e5f
Revises: gf1b2c3d4e5f
Create Date: 2026-09-10 00:00:00.000000

Unix epoch seconds exceed a signed 32-bit integer in 2038. Keep the logical
deletion timestamp consistent with the other 64-bit host timestamps. Index
the active and terminating sandbox-id slots so the reaper can traverse each
slot in bounded keyset pages instead of running cross-column OR scans.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "gg1b2c3d4e5f"
down_revision: str | None = "gf1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen the timestamp and add reaper traversal indexes."""
    inspector = sa.inspect(op.get_bind())
    host_columns = {column["name"]: column["type"] for column in inspector.get_columns("hosts")}
    existing_indexes = {index["name"] for index in inspector.get_indexes("hosts")}

    with op.batch_alter_table("hosts") as batch_op:
        # A deployment whose merge revision collided with this lineage's
        # ``gc1b2c3d4e5f`` never ran the step that creates the column, and the
        # repair that covers that case can be ordered after this one, so create
        # it at the widened type rather than widening what is not there.
        if "deleted_at" not in host_columns:
            batch_op.add_column(sa.Column("deleted_at", sa.BigInteger(), nullable=True))
        elif not isinstance(host_columns["deleted_at"], sa.BigInteger):
            batch_op.alter_column(
                "deleted_at",
                existing_type=sa.Integer(),
                type_=sa.BigInteger(),
                existing_nullable=True,
            )
        if "ix_hosts_sandbox_scan" not in existing_indexes:
            batch_op.create_index(
                "ix_hosts_sandbox_scan",
                ["sandbox_id", "workspace_id", "host_id"],
            )
        if "ix_hosts_terminating_sandbox_scan" not in existing_indexes:
            batch_op.create_index(
                "ix_hosts_terminating_sandbox_scan",
                ["terminating_sandbox_id", "workspace_id", "host_id"],
            )


def downgrade() -> None:
    """Drop reaper indexes and restore the 32-bit timestamp."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_index("ix_hosts_terminating_sandbox_scan")
        batch_op.drop_index("ix_hosts_sandbox_scan")
        batch_op.alter_column(
            "deleted_at",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
