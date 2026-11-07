"""Allow unknown token buckets and record report coverage."""

import sqlalchemy as sa
from alembic import op

revision = "a56c20261106"
down_revision = "a55c20261106"
branch_labels = None
depends_on = None

_TOKENS = ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "total_tokens")


def upgrade() -> None:
    with op.batch_alter_table("usage_hours") as batch:
        for name in _TOKENS:
            batch.alter_column(
                name,
                existing_type=sa.BigInteger(),
                existing_server_default=sa.text("0"),
                nullable=True,
            )
        batch.add_column(sa.Column("reasoning_tokens", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("coverage", sa.String(16), nullable=True))


def downgrade() -> None:
    hours = sa.table("usage_hours", *(sa.column(name) for name in _TOKENS))
    for name in _TOKENS:
        op.execute(hours.update().where(hours.c[name].is_(None)).values({name: 0}))
    with op.batch_alter_table("usage_hours") as batch:
        batch.drop_column("coverage")
        batch.drop_column("reasoning_tokens")
        for name in _TOKENS:
            batch.alter_column(
                name,
                existing_type=sa.BigInteger(),
                existing_server_default=sa.text("0"),
                nullable=False,
            )
