"""Scope instruction history by agent, host and session role.

Revision ID: a31c20261019
Revises: a30c20261015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a31c20261019"
down_revision: str | None = "a30c20261015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "global_instruction_revisions"
_OLD_INDEX = "ix_global_instruction_revisions_latest"
_INDEX = "ix_global_instruction_revisions_layer_latest"


def upgrade() -> None:
    """Preserve existing history as shared while adding scoped lookup keys."""
    op.add_column(
        _TABLE, sa.Column("scope", sa.String(16), nullable=False, server_default="shared")
    )
    op.add_column(_TABLE, sa.Column("agent_key", sa.String(64), nullable=False, server_default=""))
    op.add_column(_TABLE, sa.Column("host_id", sa.String(64), nullable=False, server_default=""))
    op.drop_index(_OLD_INDEX, table_name=_TABLE)
    op.create_index(
        _INDEX, _TABLE, ["workspace_id", "scope", "agent_key", "host_id", "created_us", "id"]
    )


def downgrade() -> None:
    """Discard scoped history for dev rollback; shared history stays intact."""
    bind = op.get_bind()
    if bind.dialect.name == "cockroachdb":
        inspector = sa.inspect(bind)
        columns = {column["name"] for column in inspector.get_columns(_TABLE)}
        if {"scope", "agent_key", "host_id"}.isdisjoint(columns) and any(
            index["name"] == _OLD_INDEX for index in inspector.get_indexes(_TABLE)
        ):
            # A later downgrade failed after this schema was already published.
            return
    table = sa.table(_TABLE, sa.column("scope", sa.String(16)))
    op.execute(table.delete().where(table.c.scope != "shared"))
    op.drop_index(_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("host_id")
        batch.drop_column("agent_key")
        batch.drop_column("scope")
    op.create_index(_OLD_INDEX, _TABLE, ["workspace_id", "created_us", "id"])
    if bind.dialect.name == "cockroachdb":
        # Older downgrades may drop this index; publish it before their DDL.
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
