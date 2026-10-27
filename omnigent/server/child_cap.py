"""Per-session child limits and process-local create serialization."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeVar

from omnigent.db.db_models import current_workspace_id
from omnigent.server.session_collab import collab_owner_for
from omnigent.server.user_preferences_store import read_collab_settings

if TYPE_CHECKING:
    from omnigent.entities import Conversation
    from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.permission_store import PermissionStore
    from omnigent.stores.project_store import ProjectStore

CHILD_CAP_LABEL_KEY = "omnigent.child_cap"
_T = TypeVar("_T")


async def complete_child_write(write: Awaitable[_T]) -> _T:
    """Drain an admitted write before cancellation releases its parent's slot lock."""
    pending = asyncio.ensure_future(write)
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(pending)
            break
        except asyncio.CancelledError as exc:
            if pending.cancelled():
                raise
            cancelled = exc
        except Exception:
            if cancelled is not None:
                raise cancelled from None
            raise
    if cancelled is not None:
        raise cancelled
    return result


def child_cap_for(
    parent: Conversation,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    prefs_store: SqlAlchemyUserPreferencesStore | None,
    project_store: ProjectStore | None = None,
) -> tuple[int | None, Literal["session", "project", "user", "none"]]:
    """Resolve the session override, project setting, then root owner's preference."""
    label = parent.labels.get(CHILD_CAP_LABEL_KEY)
    if label == "none":
        return None, "session"
    if label is not None and label.isdecimal() and int(label) > 0:
        return int(label), "session"
    owner = collab_owner_for(parent, conversation_store, permission_store)
    if project_store is not None and parent.project_id is not None:
        from omnigent.server.routes.sessions.routes_peer import effective_owner_id

        project = project_store.get(
            parent.project_id,
            user_id=effective_owner_id(parent, conversation_store, permission_store),
        )
        orchestration = project.config.get("orchestration") if project is not None else None
        cap = orchestration.get("child_cap") if isinstance(orchestration, dict) else None
        if cap == "none":
            return None, "project"
        if type(cap) is int and cap > 0:
            return cap, "project"
    cap = read_collab_settings(prefs_store, owner).child_cap
    if type(cap) is int and cap > 0:
        return cap, "user"
    return None, "none"


@dataclass
class _Entry:
    lock: asyncio.Lock
    users: int = 0


class ChildCapLocks:
    """Hold one parent's slot check through its row insert on this replica."""

    def __init__(self) -> None:
        self._entries: dict[tuple[int, str], _Entry] = {}

    @asynccontextmanager
    async def hold(self, parent_id: str) -> AsyncIterator[None]:
        key = (current_workspace_id(), parent_id)
        entry = self._entries.setdefault(key, _Entry(asyncio.Lock()))
        entry.users += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            entry.users -= 1
            if entry.users == 0:
                self._entries.pop(key, None)
