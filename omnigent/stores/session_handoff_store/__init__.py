"""Persistence contract for durable session hand-offs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities.session_handoff import SessionHandoff


class SessionHandoffStore(ABC):
    """Workspace-scoped hand-off record operations."""

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location

    @abstractmethod
    def create(self, record: SessionHandoff) -> SessionHandoff: ...

    @abstractmethod
    def get(self, handoff_id: str) -> SessionHandoff | None: ...

    @abstractmethod
    def claim(self, handoff_id: str, now: int, lease_s: int) -> bool: ...

    @abstractmethod
    def release(self, handoff_id: str) -> None: ...

    @abstractmethod
    def transition(
        self,
        handoff_id: str,
        to_state: str,
        reason: str | None,
        from_states: tuple[str, ...],
        **fields: Any,
    ) -> bool: ...

    @abstractmethod
    def set_fields(self, handoff_id: str, from_states: tuple[str, ...], **fields: Any) -> bool: ...

    @abstractmethod
    def count_unfinished(self, owner_user_id: str) -> int: ...

    @abstractmethod
    def count_recent(self, sender_session_id: str, since: int) -> int: ...

    @abstractmethod
    def find_unfinished_duplicate(
        self,
        sender_session_id: str,
        brief_hash: str,
    ) -> SessionHandoff | None: ...

    @abstractmethod
    def find_binding_for_receiver(self, session_id: str) -> SessionHandoff | None: ...

    @abstractmethod
    def find_branch_reservation(
        self,
        host_id: str,
        checkout: str,
        branch: str,
    ) -> SessionHandoff | None: ...

    @abstractmethod
    def list_for_sender(
        self,
        sender_session_id: str,
        since: int,
        limit: int,
    ) -> list[SessionHandoff]: ...

    @abstractmethod
    def list_needing_work(self, now: int, limit: int) -> list[SessionHandoff]: ...
