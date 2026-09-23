"""Durable session hand-off record."""

from __future__ import annotations

from dataclasses import dataclass

HANDOFF_UNFINISHED_STATES = ("creating", "open", "delivered", "cancel_requested")
HANDOFF_TERMINAL_STATES = ("completed", "incomplete", "failed", "cancelled", "expired")


@dataclass
class SessionHandoff:
    """One hand-off from a sender session to a receiver session."""

    id: str
    owner_user_id: str
    sender_session_id: str
    receiver_session_id: str
    create_session: bool
    project_id: str
    state: str
    brief_hash: str
    brief: str
    allow_onward: bool
    brief_peer_id: str
    created_at: int
    updated_at: int
    expires_at: int
    host_id: str | None = None
    root: str | None = None
    git_branch: str | None = None
    git_plan: dict | None = None
    reason: str | None = None
    parent_handoff_id: str | None = None
    disclosure: dict | None = None
    result_peer_id: str | None = None
    result_state: str | None = None
    stop_peer_id: str | None = None
    stop_state: str | None = None
    outcome: dict | None = None
    lease_until: int | None = None
    cancel_requested_at: int | None = None
    reported_at: int | None = None
