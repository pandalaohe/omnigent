"""Workspace-scoped retry journal; relationships are application-owned."""

from sqlalchemy import JSON, BigInteger, Boolean, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from omnigent.db.db_models import OmnigentBase, Uuid16, current_workspace_id


class SqlRetryOwner(OmnigentBase):
    __tablename__ = "subagent_retry_owners"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=current_workspace_id, server_default="0"
    )
    parent_id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    runner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SqlRetryControl(OmnigentBase):
    __tablename__ = "subagent_retry_controls"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=current_workspace_id, server_default="0"
    )
    child_id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turn_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    input_id: Mapped[str | None] = mapped_column(String(256), nullable=True)


class SqlRetryChain(OmnigentBase):
    __tablename__ = "subagent_retry_chains"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=current_workspace_id, server_default="0"
    )
    chain_id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    child_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    parent_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    owner_runner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    inbox_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    root_turn_id: Mapped[str] = mapped_column(String(256), nullable=False)
    expected_turn_id: Mapped[str] = mapped_column(String(256), nullable=False)
    expected_input_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retry_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    latest_error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    latest_terminal: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_failure_at: Mapped[float] = mapped_column(Float, nullable=False)
    due_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    pending_input_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    retry_closed: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "child_id", "root_turn_id"),
        Index("ix_retry_chains_parent_phase", "workspace_id", "parent_id", "phase"),
    )


class SqlRetryTerminal(OmnigentBase):
    __tablename__ = "subagent_retry_terminals"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=current_workspace_id, server_default="0"
    )
    child_id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    turn_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), primary_key=True)
    chain_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    envelope: Mapped[dict] = mapped_column(JSON, nullable=False)


class SqlRetryInput(OmnigentBase):
    __tablename__ = "subagent_retry_inputs"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=current_workspace_id, server_default="0"
    )
    input_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    chain_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    target_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_input_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    input_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (UniqueConstraint("workspace_id", "chain_id", "kind", "attempt"),)


class SqlRetryFinal(OmnigentBase):
    __tablename__ = "subagent_retry_finals"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=current_workspace_id, server_default="0"
    )
    chain_id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    child_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    parent_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    result_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    notice_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    outcome: Mapped[dict] = mapped_column(JSON, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    inbox_parent_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    wake_receipt: Mapped[str | None] = mapped_column(String(256), nullable=True)
    drain_receipt: Mapped[str | None] = mapped_column(String(256), nullable=True)

    __table_args__ = (
        UniqueConstraint("workspace_id", "result_id"),
        UniqueConstraint("workspace_id", "notice_id"),
        Index("ix_retry_finals_parent_drain", "workspace_id", "parent_id", "drain_receipt"),
    )
