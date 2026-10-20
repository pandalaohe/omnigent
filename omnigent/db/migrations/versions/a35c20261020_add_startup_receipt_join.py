"""Join independent startup readiness, acceptance and work observations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a35c20261020"
down_revision: str = "a34c20261020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, length in (
        ("proof_source", 64),
        ("vendor_session_id", 256),
        ("vendor_turn_id", 256),
    ):
        op.add_column("session_startups", sa.Column(name, sa.String(length), nullable=True))
    for name in ("runtime_ready", "tools_ready", "input_accepted", "work_started"):
        op.add_column(
            "session_startups",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    with op.batch_alter_table("session_startups") as batch_op:
        for name in (
            "work_started",
            "input_accepted",
            "tools_ready",
            "runtime_ready",
            "vendor_turn_id",
            "vendor_session_id",
            "proof_source",
        ):
            batch_op.drop_column(name)
