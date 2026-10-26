"""Keep pending child-runner re-keys after a rotation has finished."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a48c20261026"
down_revision: str = "a42c20261022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("session_successions", sa.Column("deferred_runners", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("session_successions") as batch_op:
        batch_op.drop_column("deferred_runners")
