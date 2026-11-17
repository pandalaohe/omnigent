"""Add optional agent splits without changing hourly accounting identities."""

import sqlalchemy as sa
from alembic import op

from omnigent.db.migrations.versions.a71c20261113_usage_renewal import validated_downgrade_cards

revision = "a72c20261117"
down_revision = "a71c20261113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_hour_agents",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("hour_utc", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("host_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("basis", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("priced", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("agent", sa.String(32), nullable=False),
        *(
            sa.Column(name, sa.BigInteger(), nullable=True)
            for name in (
                "input_tokens",
                "cached_tokens",
                "cache_write_tokens",
                "output_tokens",
                "total_tokens",
                "reasoning_tokens",
            )
        ),
        sa.Column("requests", sa.Integer(), nullable=True),
        sa.Column("failed", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.CheckConstraint("priced IN (0, 1)", name="ck_usage_hour_agents_priced"),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "owner_id",
            "account_id",
            "hour_utc",
            "model",
            "host_id",
            "basis",
            "priced",
            "agent",
        ),
    )
    op.create_index(
        "ix_usage_hour_agents_owner_hour",
        "usage_hour_agents",
        ["workspace_id", "owner_id", "hour_utc"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "mysql":
        migration_context = op.get_context()
        script = migration_context.script
        if (
            script is None
            or not {"destination_rev", "starting_heads"} <= migration_context.opts.keys()
        ):
            raise RuntimeError("cannot validate subscription downgrade target")
        steps = script.iterate_revisions(
            migration_context.opts["starting_heads"],
            migration_context.opts["destination_rev"],
            select_for_downgrade=True,
        )
        # MySQL commits DROP before a later migration can refuse its downgrade.
        if any(step.revision == down_revision for step in steps):
            validated_downgrade_cards(connection)
    op.drop_table("usage_hour_agents")
