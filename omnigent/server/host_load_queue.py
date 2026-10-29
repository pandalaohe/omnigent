"""Durable child startup holds, released on the host-connection server replica.

This queue supports a single server replica; it does not relay launches across replicas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.load_level import FACTORY_LINES
from omnigent.stores.conversation_store import ConversationStore
from omnigent.util.session_lifecycle import is_session_closed

LABEL = "omnigent.startup.host_load"
_release_session: ContextVar[str | None] = ContextVar("host_load_release", default=None)
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostLoadHold:
    host_id: str
    at: float


def held_host(conv: Conversation) -> str | None:
    """Read only the two server-owned hold states."""
    parts = conv.labels.get(LABEL, "").split("@")
    return parts[1] if len(parts) >= 2 and parts[0] in {"queued", "releasing"} else None


def start_allowed(conv: Conversation) -> bool:
    return held_host(conv) is None or _release_session.get() == conv.id


def require_start_allowed(conv: Conversation) -> None:
    if not start_allowed(conv):
        raise OmnigentError(
            "This session is queued until its host's load drops below the new-session line.",
            code=ErrorCode.CONFLICT,
        )


def presentation(conv: Conversation) -> dict[str, Any] | None:
    host = held_host(conv)
    return {"reason": "host_load", "host_id": host, "since": conv.created_at} if host else None


async def decide_hold(
    request: Any, parent_id: str | None, host_id: str | None, store: ConversationStore
) -> HostLoadHold | None:
    if (
        parent_id is None
        or request.headers.get("x-omnigent-create-origin", "").strip().lower() != "agent"
    ):
        return None
    hub = getattr(request.app.state, "system_status", None)
    if hub is None or not hub.queue_enabled(current_workspace_id()):
        return None
    if host_id is None:
        from omnigent.server.routes._sessions.helpers import _inherited_placement

        host_id = (await asyncio.to_thread(_inherited_placement, store, parent_id)).host_id
    if host_id and hub.host_load(current_workspace_id(), host_id)["level"] == "new_session_hold":
        return HostLoadHold(host_id, time.time())
    return None


def create_presentation(request: Any, response: Any, host_id: str | None) -> None:
    if request.headers.get("x-omnigent-create-origin", "").strip().lower() != "agent":
        return
    hub = getattr(request.app.state, "system_status", None)
    load: dict[str, Any] = (
        hub.host_load(current_workspace_id(), host_id or "")
        if hub is not None
        else {
            "level": "unknown",
            "load_per_core": None,
            "lines": {"heavy": FACTORY_LINES.heavy, "new_session": FACTORY_LINES.new_session},
        }
    )
    response.host_load = {
        "host_id": host_id,
        **{key: load[key] for key in ("load_per_core", "level", "lines")},
    }
    if response.labels.get(LABEL):
        response.startup = {
            "status": "queued",
            "reason": "host_load",
            "host_id": host_id,
            "load_per_core": load["load_per_core"],
            "line": load["lines"]["new_session"],
        }


class HostLoadQueue:
    def __init__(self, conversations: ConversationStore) -> None:
        self.conversations = conversations
        self._tasks: dict[tuple[int, str], asyncio.Task[None]] = {}

    def _rows(self) -> list[Conversation]:
        rows: list[Conversation] = []
        after = None
        while True:
            page = self.conversations.list_conversations(
                kind="sub_agent", include_archived=True, limit=200, after=after
            )
            rows.extend(conv for conv in page.data if held_host(conv))
            if not page.has_more or page.last_id is None:
                return sorted(rows, key=lambda conv: (conv.created_at, conv.id))
            after = page.last_id

    async def tick(self, hub: Any, app: Any) -> None:
        for workspace, host in hub.host_load_keys():
            key = (workspace, host)
            if key in self._tasks:
                continue
            with workspace_scope(workspace):
                rows = [
                    conv for conv in await asyncio.to_thread(self._rows) if held_host(conv) == host
                ]
            hub.set_queued_children(workspace, host, len(rows))
            conn = app.state.host_registry.get(host, workspace_id=workspace)
            if conn is None:
                continue
            # Disabling new holds drains existing work even if the level remains high.
            if (
                hub.queue_enabled(workspace)
                and hub.host_load(workspace, host)["level"] == "new_session_hold"
            ):
                continue
            task = asyncio.create_task(
                self._release_host(app, workspace, host, rows, conn), name="host-load-release"
            )
            self._tasks[key] = task
            task.add_done_callback(lambda _done, key=key: self._tasks.pop(key, None))

    async def _release_host(
        self, app: Any, workspace: int, host: str, rows: list[Conversation], conn: Any
    ) -> None:
        with workspace_scope(workspace):
            released = 0
            for snapshot in rows:
                if app.state.host_registry.get(host) is not conn:
                    return
                if released >= 2:
                    break
                try:
                    released += await self.release(app, snapshot.id, host)
                except Exception:
                    _logger.exception(
                        "Unable to release host-load hold for session %s", snapshot.id
                    )

    async def _start(self, app: Any, conv: Conversation, host: str, was_queued: bool) -> None:
        from omnigent.server.routes._sessions.helpers import _inherited_placement
        from omnigent.server.routes.sessions import (
            _get_runner_client,
            _launch_runner_on_host,
            _wait_for_runner_client,
        )
        from omnigent.server.startup_admission import StartupQueued

        client = await _get_runner_client(conv.id, app.state.runner_router, conversation=conv)
        assert conv.parent_conversation_id is not None
        inherited = await asyncio.to_thread(
            _inherited_placement, self.conversations, conv.parent_conversation_id
        )
        admission = getattr(app.state, "startup_admission", None)
        if admission is not None and conv.host_id is None:
            # Admission's durable dispatcher requires the resolved host on its row.
            workspace = conv.workspace or inherited.cwd
            if inherited.host_id != host or workspace is None:
                raise OmnigentError("Queued session placement changed", code=ErrorCode.CONFLICT)
            conv = await asyncio.to_thread(
                self.conversations.set_host_id, conv.id, host, workspace
            )
        if admission is not None or (
            client is None
            and (conv.runner_id is None or (was_queued and inherited.host_id != host))
        ):
            conn = app.state.host_registry.get(host)
            attempt = await _launch_runner_on_host(
                conv, self.conversations, app.state.host_registry, conn
            )
            if isinstance(attempt, StartupQueued):
                raise attempt
            if attempt.error or attempt.error_code:
                raise OmnigentError(
                    attempt.error or attempt.error_code or "Host launch failed",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                )
            refreshed = await asyncio.to_thread(self.conversations.get_conversation, conv.id)
            if refreshed is None:
                raise OmnigentError("Queued session no longer exists", code=ErrorCode.NOT_FOUND)
            conv = refreshed
        if client is None:
            client = await _wait_for_runner_client(
                conv.id,
                app.state.runner_router,
                app.state.tunnel_registry,
                runner_id=conv.runner_id,
                timeout_s=10.0,
            )
        if client is None:
            raise TimeoutError("Host-load release is waiting for its runner connection")
        response = await app.state.runner_session_initializer.initialize(
            conv, client, timeout=10.0, suppress_recovery_turn=True
        )
        response.raise_for_status()

    async def release(self, app: Any, session_id: str, host: str) -> bool:
        from omnigent.server.startup_admission import StartupQueued

        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or held_host(conv) != host:
            return False
        marker = conv.labels[LABEL]
        inputs = app.state.host_load_inputs
        if conv.archived or is_session_closed(conv.labels, conv.title):
            await inputs.cancel(session_id)
            await asyncio.to_thread(
                self.conversations.compare_and_set_label, session_id, LABEL, marker, None
            )
            return False
        was_queued = marker.startswith("queued@")
        now = time.time()
        if not was_queued:
            with contextlib.suppress(ValueError):
                if now - float(marker.rsplit("@", 1)[-1]) < 600:
                    return False
        releasing = f"releasing@{host}@{now}"
        if not await asyncio.to_thread(
            self.conversations.compare_and_set_label, session_id, LABEL, marker, releasing
        ):
            return False
        token = _release_session.set(session_id)
        async with inputs.session_barrier(session_id):
            try:
                await self._start(app, conv, host, was_queued)
                await inputs.deliver_session(app, session_id)
                await asyncio.to_thread(
                    self.conversations.compare_and_set_label, session_id, LABEL, releasing, None
                )
                await inputs.deliver_session(app, session_id)
                current = await asyncio.to_thread(self.conversations.get_conversation, session_id)
                raw = current.labels.get("omnigent.initial_message") if current else None
                if raw:
                    with contextlib.suppress(ValueError):
                        if json.loads(raw).get("status") == "queued":
                            await asyncio.to_thread(
                                self.conversations.compare_and_set_label,
                                session_id,
                                "omnigent.initial_message",
                                raw,
                                json.dumps({"status": "sent"}),
                            )
            except StartupQueued as exc:
                # A durable admission row owns both launch and pending input from now on.
                if exc.startup_id is not None:
                    await asyncio.to_thread(
                        self.conversations.compare_and_set_label,
                        session_id,
                        LABEL,
                        releasing,
                        None,
                    )
                    app.state.startup_inputs.host_changed(app, host)
                else:
                    await asyncio.to_thread(
                        self.conversations.compare_and_set_label,
                        session_id,
                        LABEL,
                        releasing,
                        f"queued@{host}",
                    )
                    _logger.warning(
                        "Admission handoff unavailable for session %s; will retry", session_id
                    )
            except (TimeoutError, httpx.TimeoutException, httpx.TransportError, ConnectionError):
                _logger.warning(
                    "Host-load release outcome uncertain for session %s", session_id, exc_info=True
                )
            except Exception:
                await asyncio.to_thread(
                    self.conversations.compare_and_set_label,
                    session_id,
                    LABEL,
                    releasing,
                    f"queued@{host}",
                )
                _logger.warning(
                    "Host-load release failed for session %s; will retry",
                    session_id,
                    exc_info=True,
                )
            finally:
                _release_session.reset(token)
        return True

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
