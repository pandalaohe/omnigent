"""Add cutover holds, turn admissions, pinned bindings and receipts.

Revision ID: a43c20261021
Revises: a44c20261021
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary

from omnigent.db.db_models import Uuid16

revision: str = "a43c20261021"
down_revision: str | None = "a44c20261021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIGEST = sa.LargeBinary(32).with_variant(MySQLBinary(32), "mysql")


def upgrade() -> None:
    op.create_table(
        "usage_cutover_holds",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("host_id", sa.String(256), nullable=False),
        sa.Column("harness", sa.String(16), nullable=False),
        sa.Column("owner_digest", _DIGEST, nullable=False),
        sa.Column("generation", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="none"),
        sa.Column("created_at", sa.Double(), nullable=True),
        sa.Column("released_at", sa.Double(), nullable=True),
        sa.Column("state_changed_at", sa.Double(), nullable=True),
        sa.Column("refused_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("workspace_id", "host_id", "harness"),
    )
    op.create_table(
        "usage_turn_admissions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_digest", _DIGEST, nullable=False),
        sa.Column("admission_id", sa.String(256), nullable=False),
        sa.Column("session_id", sa.String(256), nullable=True),
        sa.Column("host_id", sa.String(256), nullable=True),
        sa.Column("harness", sa.String(16), nullable=True),
        sa.Column("admitted_at", sa.Double(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("binding_revision", sa.Integer(), nullable=True),
        sa.Column("binding_digest", _DIGEST, nullable=True),
        sa.Column("account_hint", sa.Text(), nullable=True),
        sa.Column("route_check", sa.String(16), nullable=True),
        sa.Column("main_turn_key", sa.String(256), nullable=True),
        sa.Column("terminal_state", sa.String(128), nullable=True),
        sa.Column("terminal_evidence", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "owner_digest", "admission_id"),
    )
    op.create_table(
        "usage_cutover_receipts",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_digest", _DIGEST, nullable=False),
        sa.Column("host_id", sa.String(256), nullable=False),
        sa.Column("harness", sa.String(16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("receipt_seq", sa.Integer(), nullable=False),
        sa.Column("receipt_id", Uuid16(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("digest", _DIGEST, nullable=False),
        sa.Column("created_at", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "owner_digest", "host_id", "harness", "generation", "receipt_seq"
        ),
    )
    op.create_table(
        "usage_account_binding_revisions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_digest", _DIGEST, nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("digest", _DIGEST, nullable=False),
        sa.Column("created_at", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_digest", "config_revision"),
    )
    op.create_table(
        "usage_account_binding_activation",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_digest", _DIGEST, nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("digest", _DIGEST, nullable=False),
        sa.Column("activated_at", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_digest"),
    )
    op.create_table(
        "usage_route_attestations",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("host_id", sa.String(256), nullable=False),
        sa.Column("harness", sa.String(16), nullable=False),
        sa.Column("owner_digest", _DIGEST, nullable=False),
        sa.Column("billing_route", sa.String(16), nullable=False),
        sa.Column("route_ref", sa.String(256), nullable=False),
        sa.Column("updated_at", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "host_id", "harness"),
    )
    op.add_column("usage_events", sa.Column("host_id", sa.String(256), nullable=True))
    op.add_column("usage_events", sa.Column("harness", sa.String(16), nullable=True))
    op.create_index(
        "ix_usage_events_domain_sequence",
        "usage_events",
        ["workspace_id", "owner_digest", "host_id", "harness", "sequence"],
    )
    op.add_column("usage_units", sa.Column("admission_id", sa.String(256), nullable=True))
    op.add_column("usage_units", sa.Column("association", sa.String(32), nullable=True))
    op.add_column(
        "usage_feeds",
        sa.Column(
            "grant_positions",
            sa.Text(),
            nullable=False,
            server_default=None if op.get_bind().dialect.name == "mysql" else sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("usage_feeds") as batch_op:
        batch_op.drop_column("grant_positions")
    with op.batch_alter_table("usage_units") as batch_op:
        batch_op.drop_column("association")
        batch_op.drop_column("admission_id")
    op.drop_index("ix_usage_events_domain_sequence", table_name="usage_events")
    with op.batch_alter_table("usage_events") as batch_op:
        batch_op.drop_column("harness")
        batch_op.drop_column("host_id")
    op.drop_table("usage_route_attestations")
    op.drop_table("usage_account_binding_activation")
    op.drop_table("usage_account_binding_revisions")
    op.drop_table("usage_cutover_receipts")
    op.drop_table("usage_turn_admissions")
    op.drop_table("usage_cutover_holds")
