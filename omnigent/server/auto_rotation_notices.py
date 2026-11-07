"""Context notices and bounded recovery of durable turn-end rotation requests."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.db.utils import generate_task_id
from omnigent.entities import Conversation, ErrorData, NewConversationItem
from omnigent.runner.routing import RunnerRouter
from omnigent.server.auto_rotation import NOTICED_COUNT_LABEL_KEY, auto_rotation_for
from omnigent.server.peer_sweeper import PeerSweeper
from omnigent.server.routes._sessions.helpers import _publish_external_conversation_item
from omnigent.server.schemas import AutoRotationStatus, SessionEventInput
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.conversation_store import (
    ROTATE_REQUESTED_LABEL_KEY,
    SUCCEEDED_BY_LABEL_KEY,
    ConversationStore,
    LabelGroupChange,
)
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)


def context_notice(status: AutoRotationStatus) -> str:
    """Render the audited notice from a fresh context reading."""
    approx = " (approximate for this session type)" if status.approximate else ""
    return (
        f"[System: context] This session has compacted its context {status.compactions} times "
        f"and has {status.remaining_pct}% left before its next automatic compaction{approx}. "
        f"Auto-rotation is on for it (after {status.compactions_setting} compactions, at "
        f"{status.remaining_pct_setting}% left). Hand over to yourself now: do not wait for "
        "the current step to finish. Write a handover note that covers the work in progress, "
        "call sys_session_handover with rotate=true and auto=true, then end your turn. "
        "If the result says the rotation was deferred, keep working; another notice follows."
    )


@dataclass
class _Retry:
    revision: str
    idle_ticks: int = 0
    attempts: int = 0
    unconfirmed: bool = False
    draft_may_remain: bool = False


class AutoRotationNotices:
    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        permission_store: PermissionStore | None,
        prefs_store: SqlAlchemyUserPreferencesStore | None,
        runner_router: RunnerRouter | None,
        owner_for: Callable[[Conversation], str | None],
        owns_parent: Callable[[Conversation], bool],
        true_state: Callable[[Conversation], Awaitable[tuple[str, bool | None]]],
        post_event: Callable[..., Awaitable[Any]],
    ) -> None:
        self._store = conversation_store
        self._permissions = permission_store
        self._prefs = prefs_store
        self._router = runner_router
        self._owner_for = owner_for
        self._owns = owns_parent
        self._state = true_state
        self._post = post_event
        self._due: dict[tuple[int, str], int] = {}
        self._retry: dict[tuple[int, str], _Retry] = {}
        self._deliveries: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._task: asyncio.Task[None] | None = None
        self._app: Any = None

    def _status(self, conv: Conversation) -> AutoRotationStatus:
        return auto_rotation_for(conv, self._store, self._permissions, self._prefs)

    def mark_due(self, session_id: str, marker: int) -> None:
        key = (current_workspace_id(), session_id)
        self._due[key] = marker
        if self._app is not None:
            self._schedule(key)

    def track(self, session_id: str, revision: str) -> None:
        key = (current_workspace_id(), session_id)
        prior = self._retry.get(key)
        if prior is None or prior.revision != revision:
            self._retry[key] = _Retry(revision)

    def _rediscover(self) -> None:
        for session_id, revision in self._store.list_label_values(
            ROTATE_REQUESTED_LABEL_KEY
        ).items():
            conv = self._store.get_conversation(session_id)
            if conv is not None:
                status = self._status(conv)
                if (
                    status.enabled
                    and status.eligible
                    and not conv.labels.get(SUCCEEDED_BY_LABEL_KEY)
                ):
                    self.track(session_id, revision)

    async def start(self, app: Any) -> None:
        self._app = app
        try:
            await asyncio.to_thread(self._rediscover)
        except Exception:
            _logger.exception("Could not rediscover pending rotations at startup")
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="auto-rotation-notices")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.tick()
            except Exception:
                _logger.exception("Auto-rotation tick failed; continuing")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        tasks = list(self._deliveries.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule(self, key: tuple[int, str]) -> None:
        if key not in self._deliveries:
            self._deliveries[key] = asyncio.create_task(
                self._deliver(key, self._due[key]), name="context-notice"
            )

    def _forget_due(self, key: tuple[int, str], marker: int) -> None:
        if self._due.get(key) == marker:
            self._due.pop(key, None)

    async def _deliver(self, key: tuple[int, str], marker: int) -> None:
        workspace, session_id = key
        try:
            with workspace_scope(workspace):
                conv = await asyncio.to_thread(self._store.get_conversation, session_id)
                if conv is None:
                    self._forget_due(key, marker)
                    return
                status = await asyncio.to_thread(self._status, conv)
                if not status.due or marker != status.round_marker:
                    self._forget_due(key, marker)
                    return
                if not self._owns(conv):
                    return
                state, _ = await self._state(conv)
                if state not in {"idle", "steerable"}:
                    return
                fresh = await asyncio.to_thread(self._store.get_conversation, session_id)
                if fresh is None or fresh.runner_id != conv.runner_id or not self._owns(fresh):
                    return
                status = await asyncio.to_thread(self._status, fresh)
                if not status.due or marker != status.round_marker:
                    self._forget_due(key, marker)
                    return
                owner = await asyncio.to_thread(self._owner_for, fresh)
                result = await self._post(
                    PeerSweeper._synthetic_request(session_id, self._app),
                    session_id,
                    SessionEventInput(
                        type="message",
                        data={
                            "role": "user",
                            "content": [{"type": "input_text", "text": context_notice(status)}],
                        },
                    ),
                    acting_user_id=owner,
                )
                if isinstance(result, dict) and result.get("denied") is True:
                    self._forget_due(key, marker)
                    return
                if not isinstance(result, dict) or result.get("forwarded") is not True:
                    return

                def mark(labels: dict[str, str]) -> LabelGroupChange:
                    # A new round arriving during delivery must remain un-noticed.
                    latest = self._store.get_conversation(session_id)
                    if latest is not None:
                        current = self._status(replace(latest, labels=labels))
                        if current.due and current.round_marker == marker:
                            return LabelGroupChange({NOTICED_COUNT_LABEL_KEY: str(marker)}, set())
                    return LabelGroupChange({}, set())

                await asyncio.to_thread(
                    self._store.mutate_label_group, session_id, "omnigent.", mark
                )
                self._forget_due(key, marker)
        except Exception:
            _logger.exception("Context notice delivery failed for %s", session_id)
        finally:
            self._deliveries.pop(key, None)
            if key in self._due and self._due[key] != marker:
                self._schedule(key)

    async def tick(self) -> None:
        for key in list(self._due):
            self._schedule(key)
        for key, retry in list(self._retry.items()):
            try:
                with workspace_scope(key[0]):
                    await self._retry_one(key, retry)
            except Exception:
                _logger.exception("Rotation recovery failed for %s", key[1])

    def _pending_snapshot(self, session_id: str, retry: _Retry) -> Conversation | None:
        conv = self._store.get_conversation(session_id)
        status = self._status(conv) if conv is not None else None
        if (
            conv is None
            or status is None
            or not status.enabled
            or not status.eligible
            or conv.labels.get(SUCCEEDED_BY_LABEL_KEY)
            or conv.labels.get(ROTATE_REQUESTED_LABEL_KEY) != retry.revision
            or str(conv.archive_revision) != retry.revision
        ):
            return None
        return conv

    async def _retry_one(self, key: tuple[int, str], retry: _Retry) -> None:
        session_id = key[1]
        conv = await asyncio.to_thread(self._pending_snapshot, session_id, retry)
        if conv is None:
            self._retry.pop(key, None)
            return
        if not self._owns(conv):
            retry.idle_ticks = 0
            return
        state, _ = await self._state(conv)
        retry.idle_ticks = retry.idle_ticks + 1 if state == "idle" else 0
        if retry.idle_ticks < 2:
            return
        retry.idle_ticks = 0
        fresh = await asyncio.to_thread(self._pending_snapshot, session_id, retry)
        if fresh is None or fresh.runner_id != conv.runner_id or not self._owns(fresh):
            self._retry.pop(key, None)
            return
        if retry.attempts < 2 and not retry.unconfirmed:
            if self._router is None:
                return
            routed = await asyncio.to_thread(self._router.client_for_session_resources, session_id)
            fresh = await asyncio.to_thread(self._pending_snapshot, session_id, retry)
            if fresh is None or fresh.runner_id != conv.runner_id or not self._owns(fresh):
                self._retry.pop(key, None)
                return
            retry.attempts += 1
            response = await routed.client.post(
                f"/v1/sessions/{quote(session_id, safe='')}/pending-rotation",
                json={"archive_revision": retry.revision},
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()
            retry.unconfirmed = result.get("status") in {"unconfirmed", "draft_may_remain"}
            retry.draft_may_remain = result.get("status") == "draft_may_remain"
            if not retry.unconfirmed:
                return
        if retry.attempts >= 2 or retry.unconfirmed:
            items = await asyncio.to_thread(
                self._store.append,
                session_id,
                [
                    NewConversationItem(
                        type="error",
                        response_id=generate_task_id(),
                        data=ErrorData(
                            source="harness",
                            code="session_rotation_failed",
                            level="warning",
                            message=(
                                "Rotation unconfirmed; draft may remain. Inspect and clear "
                                "the terminal input before pressing Enter."
                                if retry.draft_may_remain
                                else "Rotation unconfirmed. The command was already injected "
                                "and will not be retried automatically. Inspect the terminal "
                                "before typing /clear."
                                if retry.unconfirmed
                                else "This session could not continue in a new session "
                                "automatically. Type /clear to continue."
                            ),
                        ),
                        stable_id=hashlib.sha256(
                            f"rotation-failed:{session_id}:{retry.revision}".encode()
                        ).hexdigest()[:32],
                    )
                ],
            )
            for item in items:
                if not item.deduplicated:
                    _publish_external_conversation_item(session_id, item)
            self._retry.pop(key, None)
            return
