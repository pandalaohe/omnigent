"""Add the saved Agent member catalog projection.

Revision ID: c1a6e2f4b610
Revises: c7a9e2f4b610
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1a6e2f4b610"
down_revision: str | None = "c7a9e2f4b610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("custom_agents") as batch_op:
        batch_op.add_column(sa.Column("members", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("custom_agents") as batch_op:
        batch_op.drop_column("members")
