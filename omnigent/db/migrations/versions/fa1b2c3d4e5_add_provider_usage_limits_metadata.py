"""store provider usage limits outside bounded conversation labels

Revision ID: fa1b2c3d4e5
Revises: f9a1b2c3d4e5
Create Date: 2026-09-02 02:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fa1b2c3d4e5"
down_revision: str | None = "f9a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_KEY = "omnigent.last_provider_usage_limits"


def _recover_json(value: str) -> str | None:
    """Return valid compact JSON, repairing only one clipped closing brace."""
    for candidate in (value, f"{value}}}" if len(value) == 256 else ""):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return json.dumps(parsed, separators=(",", ":"))
    return None


def upgrade() -> None:
    """Add the metadata field and recover any readable legacy snapshots."""
    op.add_column(
        "omnigent_conversation_metadata",
        sa.Column("provider_usage_limits", sa.LargeBinary(), nullable=True),
    )
    bind = op.get_bind()
    labels = sa.table(
        "conversation_labels",
        sa.column("workspace_id"),
        sa.column("conversation_id"),
        sa.column("key"),
        sa.column("value"),
    )
    rows = bind.execute(
        sa.select(labels.c.workspace_id, labels.c.conversation_id, labels.c.value).where(
            labels.c.key == sa.bindparam("key")
        ),
        {"key": _LEGACY_KEY},
    ).fetchall()
    for workspace_id, conversation_id, value in rows:
        recovered = _recover_json(value)
        if recovered is None:
            continue
        # Unframed UTF-8 is a supported legacy representation for CompressedText.
        bind.execute(
            sa.text(
                "UPDATE omnigent_conversation_metadata "
                "SET provider_usage_limits = :payload "
                "WHERE workspace_id = :workspace_id AND id = :conversation_id"
            ),
            {
                "payload": recovered if bind.dialect.name == "sqlite" else recovered.encode(),
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
            },
        )


def downgrade() -> None:
    """Remove the metadata field; legacy labels remain available when valid."""
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("provider_usage_limits")
