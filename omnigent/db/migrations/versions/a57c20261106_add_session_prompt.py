"""Persist optional startup instructions on each conversation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import MEDIUMBLOB

revision: str = "a57c20261106"
down_revision: str = "a56c20261106"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("session_prompt", sa.Text(), nullable=True))
    if op.get_bind().dialect.name == "mysql":
        with op.batch_alter_table("preferences") as batch:
            batch.alter_column(
                "value",
                existing_type=sa.LargeBinary(),
                type_=MEDIUMBLOB(),
                existing_nullable=False,
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "mysql":
        if (
            op.get_bind()
            .execute(
                sa.text("SELECT 1 FROM preferences WHERE OCTET_LENGTH(value) > 65535 LIMIT 1")
            )
            .first()
        ):
            raise RuntimeError(
                "Remove large starting prompts before downgrading preference storage"
            )
        with op.batch_alter_table("preferences") as batch:
            batch.alter_column(
                "value",
                existing_type=MEDIUMBLOB(),
                type_=sa.LargeBinary(),
                existing_nullable=False,
            )
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("session_prompt")
