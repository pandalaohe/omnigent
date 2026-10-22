"""Admission metadata; pending conversation items remain the only input owner."""

from sqlalchemy import JSON, BigInteger, Boolean, Index, Integer, String, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from omnigent.db.db_models import OmnigentBase, Uuid16, current_workspace_id


class SqlStartupBudget(OmnigentBase):
    __tablename__ = "session_startup_budgets"

    scope: Mapped[str] = mapped_column(String(80), primary_key=True)
    resident_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    startup_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    next_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_family_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    fenced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    work_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    work_fence_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    recovery: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    current_generation: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resident_inventory_unknown: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )


class SqlSessionStartup(OmnigentBase):
    __tablename__ = "session_startups"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=current_workspace_id, server_default="0"
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    host_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    session_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    input_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    parent_startup_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    family_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    runtime_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    proof_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vendor_session_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    vendor_turn_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    runtime_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tools_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    work_started: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    occupied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resident: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cold_claims: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    host_generation: Mapped[str | None] = mapped_column(String(128), nullable=True)
    preparation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    admitted_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    first_dispatch_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("workspace_id", "owner_user_id", "operation_key"),
        UniqueConstraint("workspace_id", "session_id", "runtime_generation"),
        Index("ix_session_startups_host_queue", "workspace_id", "host_id", "state", "sequence"),
        Index("ix_session_startups_resident", "resident"),
    )
