"""add project repository role

Revision ID: a27c20261009
Revises: a26c20261009
Create Date: 2026-10-09 00:00:00.000000

Adds ``project_repositories.role`` (``"code"`` or ``"related"``, NOT NULL,
default ``"related"``) and backfills one code repository per project: the
repository most primary+enabled bindings reference (tie: oldest
``created_at``, then smallest ``id``). Nothing else is rewritten, so
placement is identical before and after. Column add/drop goes through
``op.batch_alter_table`` so the chain stays runnable on SQLite; the backfill
uses SQLAlchemy core selects so it runs on SQLite and Postgres alike.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "a27c20261009"
down_revision: str | None = "a26c20261009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _promote_code_repositories(connection: sa.Connection) -> None:
    """Set ``role='code'`` on each project's most-referenced repository."""
    repositories = sa.table(
        "project_repositories",
        sa.column("workspace_id"),
        sa.column("project_id"),
        sa.column("id"),
        sa.column("created_at"),
        sa.column("role"),
    )
    bindings = sa.table(
        "project_host_bindings",
        sa.column("workspace_id"),
        sa.column("project_id"),
        sa.column("repository_id"),
        sa.column("is_primary"),
        sa.column("enabled"),
    )

    created_at: dict[tuple[Any, Any], int] = {}
    for row in connection.execute(
        sa.select(
            repositories.c.workspace_id,
            repositories.c.id,
            repositories.c.created_at,
        )
    ):
        created_at[(row.workspace_id, row.id)] = row.created_at

    counts: dict[tuple[Any, Any, Any], int] = {}
    for row in connection.execute(
        sa.select(bindings.c.workspace_id, bindings.c.project_id, bindings.c.repository_id)
        .where(bindings.c.is_primary.is_(True))
        .where(bindings.c.enabled.is_(True))
    ):
        key = (row.workspace_id, row.project_id, row.repository_id)
        counts[key] = counts.get(key, 0) + 1

    # A project's winner sorts by most bindings, then oldest repository,
    # then smallest id; only repositories that still have a row qualify.
    best: dict[tuple[Any, Any], tuple[int, int, Any]] = {}
    for (workspace_id, project_id, repository_id), count in counts.items():
        if (workspace_id, repository_id) not in created_at:
            continue
        candidate = (-count, created_at[(workspace_id, repository_id)], repository_id)
        current = best.get((workspace_id, project_id))
        if current is None or candidate < current:
            best[(workspace_id, project_id)] = candidate

    for (workspace_id, _project_id), (_neg_count, _created, repository_id) in best.items():
        connection.execute(
            sa.update(repositories)
            .where(repositories.c.workspace_id == workspace_id)
            .where(repositories.c.id == repository_id)
            .values(role="code")
        )


def upgrade() -> None:
    """Add the role column and mark each project's code repository."""
    with op.batch_alter_table("project_repositories") as batch_op:
        batch_op.add_column(
            sa.Column("role", sa.String(16), nullable=False, server_default="related")
        )
    _promote_code_repositories(op.get_bind())


def downgrade() -> None:
    """Drop the role column."""
    with op.batch_alter_table("project_repositories") as batch_op:
        batch_op.drop_column("role")
