"""Retain child retry, execution-admission and final-delivery receipts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "a51c20261029"
down_revision: str = "a50c20261027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "subagent_retry_owners",
    "subagent_retry_controls",
    "subagent_retry_chains",
    "subagent_retry_terminals",
    "subagent_retry_inputs",
    "subagent_retry_finals",
)


def _workspace() -> sa.Column:
    return sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0")


def _id(name: str, *, primary_key: bool = False, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.LargeBinary(16).with_variant(mysql.BINARY(16), "mysql"),
        primary_key=primary_key,
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        _TABLES[0],
        _workspace(),
        _id("parent_id", primary_key=True),
        sa.Column("runner_id", sa.String(64), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        _TABLES[1],
        _workspace(),
        _id("child_id", primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("turn_id", sa.String(256), nullable=True),
        sa.Column("input_id", sa.String(256), nullable=True),
    )
    op.create_table(
        _TABLES[2],
        _workspace(),
        _id("chain_id", primary_key=True),
        _id("child_id"),
        _id("parent_id"),
        sa.Column("owner_runner_id", sa.String(64), nullable=False),
        sa.Column("inbox_epoch", sa.BigInteger(), nullable=False),
        sa.Column("root_turn_id", sa.String(256), nullable=False),
        sa.Column("expected_turn_id", sa.String(256), nullable=False),
        sa.Column("expected_input_generation", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("retry_limit", sa.Integer(), nullable=False),
        sa.Column("accepted_attempts", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("latest_error", sa.JSON(), nullable=True),
        sa.Column("latest_terminal", sa.JSON(), nullable=False),
        sa.Column("observed_failure_at", sa.Float(), nullable=False),
        sa.Column("due_at", sa.Float(), nullable=True),
        sa.Column("pending_input_id", sa.String(256), nullable=True),
        sa.Column("retry_closed", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("workspace_id", "child_id", "root_turn_id"),
    )
    op.create_index(
        "ix_retry_chains_parent_phase", _TABLES[2], ["workspace_id", "parent_id", "phase"]
    )
    op.create_table(
        _TABLES[3],
        _workspace(),
        _id("child_id", primary_key=True),
        sa.Column("turn_id", sa.String(256), primary_key=True),
        sa.Column("status", sa.String(16), primary_key=True),
        _id("chain_id"),
        sa.Column("envelope", sa.JSON(), nullable=False),
    )
    op.create_table(
        _TABLES[4],
        _workspace(),
        sa.Column("input_id", sa.String(256), primary_key=True),
        _id("chain_id", nullable=True),
        _id("target_id"),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("expected_input_generation", sa.BigInteger(), nullable=True),
        sa.Column("turn_id", sa.String(256), nullable=True),
        sa.Column("input_generation", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.JSON(), nullable=True),
        sa.UniqueConstraint("workspace_id", "chain_id", "kind", "attempt"),
    )
    op.create_table(
        _TABLES[5],
        _workspace(),
        _id("chain_id", primary_key=True),
        _id("child_id"),
        _id("parent_id"),
        _id("result_id"),
        _id("notice_id"),
        sa.Column("outcome", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retry_limit", sa.Integer(), nullable=False),
        _id("inbox_parent_id", nullable=True),
        sa.Column("wake_receipt", sa.String(256), nullable=True),
        sa.Column("drain_receipt", sa.String(256), nullable=True),
        sa.UniqueConstraint("workspace_id", "result_id"),
        sa.UniqueConstraint("workspace_id", "notice_id"),
    )
    op.create_index(
        "ix_retry_finals_parent_drain",
        _TABLES[5],
        ["workspace_id", "parent_id", "drain_receipt"],
    )


def downgrade() -> None:
    # Receipts may still owe delivery after scheduling is disabled.
    for name in _TABLES:
        table = sa.table(name, sa.column("workspace_id"))
        if op.get_bind().execute(sa.select(table.c.workspace_id).limit(1)).first():
            raise RuntimeError("Retry journal contains receipts; retain its additive schema.")
    for name in reversed(_TABLES):
        op.drop_table(name)
