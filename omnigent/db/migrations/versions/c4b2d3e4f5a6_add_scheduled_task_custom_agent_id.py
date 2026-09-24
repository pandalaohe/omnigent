"""Add ``scheduled_tasks.custom_agent_id`` for saved library Agent bindings.

Revision ID: c4b2d3e4f5a6
Revises: c1a6e2f4b610
Create Date: 2026-09-24 00:00:00.000000

A scheduled task could only bind an ``agents`` row, so ``agent_id`` was a
required ``Uuid16`` column (16 raw bytes, surfaced by the store as the bare
32-char hex uuid). A task can now also target a saved library Agent —
``custom_agents.id`` is a ``ca_<32-hex>`` string, which a binary column cannot
hold.

The library binding gets its own nullable text column and ``agent_id`` becomes
nullable alongside it, so every stored-agent row keeps its bytes untouched (no
byte→hex rewrite on any dialect). Exactly one of the two columns is set per
row: the store picks the column from the id shape, and the table's existing
CHECK idiom carries the invariant in the database too.

Downgrade removes ``ca_``-bound tasks together with their run history (no
faithful pre-change form exists for them; same stance as ``d7a6b3c91f48``),
drops the column, and restores ``agent_id`` NOT NULL. Stored-agent tasks
survive untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "c4b2d3e4f5a6"
down_revision: str | None = "c1a6e2f4b610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK_NAME = "ck_scheduled_tasks_agent_binding"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """Add the library-Agent column and make ``agent_id`` optional."""
    with op.batch_alter_table(
        "scheduled_tasks", recreate="always" if _is_sqlite() else "auto"
    ) as batch_op:
        batch_op.add_column(sa.Column("custom_agent_id", sa.String(40), nullable=True))
        # existing_type drives MySQL's full-column MODIFY: Uuid16 resolves to
        # BINARY(16) there and BLOB/BYTEA on the other dialects.
        batch_op.alter_column("agent_id", existing_type=Uuid16(), nullable=True)
        batch_op.create_check_constraint(
            _CHECK_NAME, "(agent_id IS NULL) <> (custom_agent_id IS NULL)"
        )


def downgrade() -> None:
    """Drop the library-Agent column and restore the required ``agent_id``."""
    # A ca_-bound task has no stored-agent binding to fall back to, so it goes
    # with its run history (the store's task deletion drops the same rows).
    # Task keys are (workspace_id, id), so correlate both: another workspace's
    # stored-agent task may reuse this id and must keep its runs.
    op.execute(
        sa.text(
            "DELETE FROM scheduled_task_runs WHERE EXISTS ("
            "SELECT 1 FROM scheduled_tasks "
            "WHERE scheduled_tasks.workspace_id = scheduled_task_runs.workspace_id "
            "AND scheduled_tasks.id = scheduled_task_runs.scheduled_task_id "
            "AND scheduled_tasks.custom_agent_id IS NOT NULL)"
        )
    )
    op.execute(sa.text("DELETE FROM scheduled_tasks WHERE custom_agent_id IS NOT NULL"))

    with op.batch_alter_table(
        "scheduled_tasks", recreate="always" if _is_sqlite() else "auto"
    ) as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
        batch_op.drop_column("custom_agent_id")
        batch_op.alter_column("agent_id", existing_type=Uuid16(), nullable=False)
