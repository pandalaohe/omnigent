"""Add the host model catalog cache and pending calling-default columns.

Revision ID: a23c20260928
Revises: a22c20260928

Creates ``host_model_catalog_cache`` (one row per workspace × host ×
harness: the host's last synced model catalog plus its fetch time and
error) and adds the calling-defaults columns later batches persist:

* ``scheduled_tasks.project_id`` — the project whose defaults apply at
  fire time (NULL = master table only).
* ``scheduled_tasks.explicit_null_fields`` / ``assignments.
  explicit_null_fields`` — JSON lists of request fields the creator sent
  as explicit nulls (D9), so a deferred placement / fire does not refill
  them.
* ``assignments.target_agent_id`` — was NOT NULL, now nullable because
  the agent may come from the destination host's default (D27).
* ``assignments.reasoning_effort`` — per-assignment effort.

Downgrade drops the table and the added columns. It restores
``assignments.target_agent_id`` NOT NULL only when no row has a NULL
agent; otherwise the column is left nullable and a warning names the
count, because the old image cannot place those rows.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a23c20260928"
down_revision: str | None = "a22c20260928"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_logger = logging.getLogger(__name__)


def upgrade() -> None:
    """Create the catalog cache and add the calling-defaults columns."""
    op.create_table(
        "host_model_catalog_cache",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("host_id", Uuid16(), nullable=False),
        sa.Column("harness", sa.String(128), nullable=False),
        # JSON list of model rows. Opaque to SQL, never queried.
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "host_id", "harness"),
    )
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("project_id", Uuid16(), nullable=True))
        batch_op.add_column(sa.Column("explicit_null_fields", sa.Text(), nullable=True))
    with op.batch_alter_table("assignments") as batch_op:
        batch_op.alter_column("target_agent_id", existing_type=Uuid16(), nullable=True)
        batch_op.add_column(sa.Column("reasoning_effort", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("explicit_null_fields", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the cache and the added columns."""
    bind = op.get_bind()
    null_agents = bind.scalar(
        sa.text("SELECT count(*) FROM assignments WHERE target_agent_id IS NULL")
    )
    with op.batch_alter_table("assignments") as batch_op:
        batch_op.drop_column("explicit_null_fields")
        batch_op.drop_column("reasoning_effort")
        if null_agents:
            _logger.warning(
                "Leaving assignments.target_agent_id nullable: %s row(s) carry no "
                "target agent and the previous schema required one",
                null_agents,
            )
        else:
            batch_op.alter_column("target_agent_id", existing_type=Uuid16(), nullable=False)
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("explicit_null_fields")
        batch_op.drop_column("project_id")
    op.drop_table("host_model_catalog_cache")
