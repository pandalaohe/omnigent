"""add cross-host project-assignment tables and collaboration columns

Revision ID: a12c20260913
Revises: a11c20260912
Create Date: 2026-09-13 00:00:00.000000

Adds the five project-assignment tables (``project_repositories``,
``project_host_bindings``, ``assignments``, ``assignment_attempts``,
``assignment_messages``) and the two ``projects`` collaboration columns
(``collaboration_enabled``, ``collaboration_revision``).

The two ``projects`` columns are NOT NULL with server defaults (``false``
and ``0``): the database backfills existing rows in the same statement, so
no separate data migration runs, and no application code ever sees a third
"unset" state. The defaults stay on the columns rather than being dropped
after backfill, so a row inserted by an older binary still lands valid.

All cross-table references are application-owned, never DB foreign keys
(schema Rule R032). Timestamps are Integer epoch seconds; opaque JSON and
free text are compressed blobs (CompressedText → LargeBinary here, matching
``z6a2b3c4d5e6``). Column adds/drops go through ``op.batch_alter_table``
so the chain stays runnable on SQLite.

Downgrade is destructive by definition: assignment history has no home in
the old schema. Any assignment not in a terminal state is lost, and the
published ``refs/omnigent/assignments/*`` refs survive the downgrade with
nothing left to interpret them. A pre-drop count of non-terminal rows is
logged so the operator sees what they are discarding in the migration
output rather than afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a12c20260913"
down_revision: str | None = "a11c20260912"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_logger = logging.getLogger(__name__)


def upgrade() -> None:
    """Create the five assignment tables and the two ``projects`` columns."""
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "collaboration_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "collaboration_revision",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )

    op.create_table(
        "project_repositories",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("remote_url", sa.String(2048), nullable=False),
        sa.Column("default_branch", sa.String(255), nullable=False),
        sa.Column(
            "context_manifest_path",
            sa.String(512),
            nullable=False,
            server_default=".agents/project/manifest.json",
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id", "project_id", "name", name="uq_project_repositories_name"
        ),
    )
    op.create_index(
        "ix_project_repositories_project",
        "project_repositories",
        ["workspace_id", "project_id", "id"],
        unique=False,
    )

    op.create_table(
        "project_host_bindings",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("host_id", Uuid16(), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("repository_id", Uuid16(), nullable=False),
        sa.Column("workspace", sa.String(2048), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("path_verified_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "project_id",
            "host_id",
            "name",
            name="uq_project_host_bindings_name",
        ),
    )
    op.create_index(
        "ix_project_host_bindings_project",
        "project_host_bindings",
        ["workspace_id", "project_id", "id"],
        unique=False,
    )

    op.create_table(
        "assignments",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("source_session_id", Uuid16(), nullable=False),
        sa.Column("owner_user_id", sa.String(128), nullable=True),
        sa.Column("target_agent_id", Uuid16(), nullable=False),
        sa.Column("requested_host_id", Uuid16(), nullable=True),
        sa.Column("resolved_host_id", Uuid16(), nullable=True),
        sa.Column("binding_name", sa.String(128), nullable=False, server_default="primary"),
        sa.Column("resolved_binding_id", Uuid16(), nullable=True),
        sa.Column("resolved_binding_revision", sa.Integer(), nullable=True),
        sa.Column("project_revision", sa.Integer(), nullable=False, server_default="0"),
        # Opaque free text / JSON stored compressed (CompressedText → LargeBinary).
        sa.Column("task", sa.LargeBinary(), nullable=False),
        sa.Column("metadata_json", sa.LargeBinary(), nullable=True),
        sa.Column("inputs_json", sa.LargeBinary(), nullable=False),
        sa.Column("model_override", sa.String(128), nullable=True),
        sa.Column("harness_override", sa.String(128), nullable=True),
        sa.Column("start_deadline", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="preparing"),
        sa.Column("wait_reason", sa.String(256), nullable=True),
        sa.Column("next_check_at", sa.Integer(), nullable=True),
        sa.Column("active_attempt_id", Uuid16(), nullable=True),
        sa.Column("outputs_json", sa.LargeBinary(), nullable=True),
        sa.Column("result_summary", sa.LargeBinary(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("cancel_requested_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "source_session_id",
            "idempotency_key",
            name="uq_assignments_source_idempotency",
        ),
    )
    op.create_index(
        "ix_assignments_due",
        "assignments",
        ["workspace_id", "state", "next_check_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_assignments_host",
        "assignments",
        ["workspace_id", "resolved_host_id", "state", "id"],
        unique=False,
    )

    op.create_table(
        "assignment_attempts",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("assignment_id", Uuid16(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("host_id", Uuid16(), nullable=False),
        sa.Column("runner_id", sa.String(128), nullable=True),
        sa.Column("session_id", Uuid16(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("lease_expires_at", sa.Integer(), nullable=True),
        sa.Column("event_dispatched_at", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.Integer(), nullable=True),
        sa.Column("ended_at", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "assignment_id",
            "number",
            name="uq_assignment_attempts_number",
        ),
    )
    op.create_index(
        "ix_assignment_attempts_assignment",
        "assignment_attempts",
        ["workspace_id", "assignment_id", "id"],
        unique=False,
    )

    op.create_table(
        "assignment_messages",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("assignment_id", Uuid16(), nullable=False),
        sa.Column("sender_session_id", Uuid16(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        # Opaque free text stored compressed (CompressedText → LargeBinary).
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "assignment_id",
            "sender_session_id",
            "idempotency_key",
            name="uq_assignment_messages_idempotency",
        ),
    )
    op.create_index(
        "ix_assignment_messages_assignment",
        "assignment_messages",
        ["workspace_id", "assignment_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the five assignment tables and the two ``projects`` columns."""
    connection = op.get_bind()
    non_terminal = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM assignments WHERE state IN "
            "('preparing', 'waiting', 'starting', 'running', 'publishing', "
            "'stopping', 'interrupted')"
        )
    ).scalar()
    _logger.warning(
        "downgrading a12c20260913 with %s non-terminal assignments "
        "(preparing/waiting/starting/running/publishing/stopping/interrupted); "
        "their history is lost and published refs/omnigent/assignments/* refs "
        "survive with nothing to interpret them",
        non_terminal,
    )
    op.drop_index("ix_assignment_messages_assignment", table_name="assignment_messages")
    op.drop_table("assignment_messages")
    op.drop_index("ix_assignment_attempts_assignment", table_name="assignment_attempts")
    op.drop_table("assignment_attempts")
    op.drop_index("ix_assignments_host", table_name="assignments")
    op.drop_index("ix_assignments_due", table_name="assignments")
    op.drop_table("assignments")
    op.drop_index("ix_project_host_bindings_project", table_name="project_host_bindings")
    op.drop_table("project_host_bindings")
    op.drop_index("ix_project_repositories_project", table_name="project_repositories")
    op.drop_table("project_repositories")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("collaboration_revision")
        batch_op.drop_column("collaboration_enabled")
