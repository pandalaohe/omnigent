"""Allow subscription renewals and account cost classes without inventing payments."""

import json

import sqlalchemy as sa
from alembic import op

revision = "a71c20261113"
down_revision = "a70c20261113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_pricing_defaults", sa.Column("account_classes", sa.Text(), nullable=True))
    with op.batch_alter_table("usage_pricing_cards") as batch:
        batch.alter_column("allowance_usd", existing_type=sa.Numeric(38, 18), nullable=True)


def downgrade() -> None:
    connection = op.get_bind()
    cards = sa.table(
        "usage_pricing_cards",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("owner_id", sa.String()),
        sa.column("card_id", sa.String()),
        sa.column("allowance_usd", sa.Numeric(38, 18)),
        sa.column("payload", sa.Text()),
    )
    compatible = []
    # Validate every row before changing payloads or schema; renewal history has no old shape.
    for row in connection.execute(sa.select(cards)).mappings():
        payload = json.loads(row["payload"])
        if payload.get("repeats") or payload.get("ends_on") is not None:
            raise RuntimeError("cannot downgrade repeating subscription history")
        if row["allowance_usd"] is None:
            raise RuntimeError("cannot downgrade subscriptions without allowance")
        payload.pop("repeats", None)
        payload.pop("ends_on", None)
        compatible.append(
            (row["workspace_id"], row["owner_id"], row["card_id"], json.dumps(payload))
        )
    for workspace, owner, card, payload in compatible:
        connection.execute(
            cards.update()
            .where(
                cards.c.workspace_id == workspace,
                cards.c.owner_id == owner,
                cards.c.card_id == card,
            )
            .values(payload=payload)
        )
    with op.batch_alter_table("usage_pricing_cards") as batch:
        batch.alter_column("allowance_usd", existing_type=sa.Numeric(38, 18), nullable=False)
    with op.batch_alter_table("usage_pricing_defaults") as batch:
        batch.drop_column("account_classes")
