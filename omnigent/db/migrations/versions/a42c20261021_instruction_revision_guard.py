"""Serialize instruction revisions, including a layer's first save."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a42c20261021"
down_revision: str = "a41c20261021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("global_instruction_revisions") as batch:
        batch.add_column(sa.Column("parent_revision_id", sa.String(64), nullable=True))
        batch.create_unique_constraint(
            "uq_instruction_parent", ["workspace_id", "parent_revision_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("global_instruction_revisions") as batch:
        batch.drop_constraint("uq_instruction_parent", type_="unique")
        batch.drop_column("parent_revision_id")
