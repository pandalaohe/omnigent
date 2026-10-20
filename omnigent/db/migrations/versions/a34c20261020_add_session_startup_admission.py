"""Add durable startup admission without copying pending message payloads."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a34c20261020"
down_revision: str = "a33c20261020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_startup_budgets",
        sa.Column("scope", sa.String(80), primary_key=True),
        sa.Column("resident_limit", sa.Integer(), nullable=False),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_family_id", Uuid16(), nullable=True),
        sa.Column("fenced", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "session_startups",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("owner_user_id", sa.String(128), nullable=False),
        sa.Column("host_id", Uuid16(), nullable=False),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("operation_key", sa.String(128), nullable=False),
        sa.Column("runtime_generation", sa.Integer(), nullable=False),
        sa.Column("input_id", Uuid16(), nullable=True),
        sa.Column("parent_startup_id", Uuid16(), nullable=True),
        sa.Column("family_id", Uuid16(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(128), nullable=True),
        sa.Column("attempt_id", Uuid16(), nullable=True),
        sa.Column("runtime_id", sa.String(128), nullable=True),
        sa.Column("occupied", sa.Boolean(), nullable=False),
        sa.Column("resident", sa.Boolean(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("workspace_id", "owner_user_id", "operation_key"),
        sa.UniqueConstraint("workspace_id", "session_id", "runtime_generation"),
    )
    op.create_index(
        "ix_session_startups_host_queue",
        "session_startups",
        ["workspace_id", "host_id", "state", "sequence"],
    )
    op.create_index("ix_session_startups_resident", "session_startups", ["resident"])
    op.execute(
        sa.text(
            "INSERT INTO session_startup_budgets "
            "(scope, resident_limit, next_sequence, fenced) VALUES ('deployment', 0, 0, false)"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_session_startups_resident", table_name="session_startups")
    op.drop_index("ix_session_startups_host_queue", table_name="session_startups")
    op.drop_table("session_startups")
    op.drop_table("session_startup_budgets")
