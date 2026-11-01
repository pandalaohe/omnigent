"""Owner-only REST boundary for host restart previews and status."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.db_models import current_workspace_id
from omnigent.host.frames import CAP_HOST_RESTART
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.host_restart import RestartRecord
from omnigent.server.peer_sweeper import PeerSweeper
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.server.schemas import SessionEventInput
from omnigent.stores import ConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore


class HostRestartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True
    preview_id: str | None = None
    force: bool = False
    drain_timeout_s: int = Field(default=120, ge=0, le=600)
    requested_by_session_id: str | None = None


def register_host_restart_routes(
    router: APIRouter,
    registry: HostRegistry,
    host_store: HostStore,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    async def owner(request: Request, host_id: str) -> tuple[str | None, Any]:
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        return user_id, host

    def requester_owned(session_id: str, user_id: str | None) -> bool:
        seen: set[str] = set()
        while session_id not in seen:
            seen.add(session_id)
            conv = conversation_store.get_conversation(session_id)
            if conv is None:
                return False
            if conv.parent_conversation_id is None:
                return permission_store is None or permission_store.check_access(
                    user_id, session_id, LEVEL_OWNER
                )
            session_id = conv.parent_conversation_id
        return False

    @router.post("/hosts/{host_id}/restart")
    async def restart_host(
        request: Request, host_id: str, body: HostRestartRequest
    ) -> dict[str, Any]:
        user_id, host = await owner(request, host_id)
        requester = body.requested_by_session_id
        if requester is not None and not await asyncio.to_thread(
            requester_owned, requester, user_id
        ):
            raise HTTPException(403, "requester session is not owned by you")
        conn = registry.get(host.host_id)
        if conn is None:
            raise host_absent_error(host)
        if CAP_HOST_RESTART not in conn.hello.capabilities or conn.hello.supervised is None:
            raise HTTPException(409, "host is not supervised; restart it on the machine")
        if body.dry_run:
            conv = (
                await asyncio.to_thread(conversation_store.get_conversation, requester)
                if requester
                else None
            )
            return await registry.restarts.preview(
                conn,
                owner=user_id,
                requester=requester,
                force=body.force,
                drain_timeout_s=body.drain_timeout_s,
                caller_on_host=bool(conv and conv.host_id == host.host_id),
            )

        async def notify(record: RestartRecord) -> bool:
            post_event = getattr(request.app.state, "host_restart_post_event", None)
            if post_event is None or requester is None:
                return False
            text = (
                f"[System: host restart] Host {host.name}: {record.phase}. "
                f"Offline seconds: {record.offline_seconds}. Version: {record.version}. "
                f"Cut sessions: {', '.join(record.cut_sessions) or 'none'}."
                + (f" Reason: {record.reason}." if record.reason else "")
            )
            result = await post_event(
                PeerSweeper._synthetic_request(requester, request.app),
                requester,
                SessionEventInput(
                    type="message",
                    data={"role": "user", "content": [{"type": "input_text", "text": text}]},
                ),
                acting_user_id=user_id,
            )
            return not (isinstance(result, dict) and result.get("forwarded") is False)

        record = await registry.restarts.restart(
            body.preview_id,
            conn,
            owner=user_id,
            requester=requester,
            force=body.force,
            drain_timeout_s=body.drain_timeout_s,
            notify=notify,
        )
        return {"request_id": record.request_id, "phase": record.phase}

    @router.get("/hosts/{host_id}/restart/{request_id}")
    async def restart_status(request: Request, host_id: str, request_id: str) -> dict[str, Any]:
        user_id, host = await owner(request, host_id)
        record = registry.restarts.records.get((current_workspace_id(), host.host_id, request_id))
        if record is None or record.preview.owner != user_id:
            raise HTTPException(404, "restart request not found")
        registry.restarts.advance(record)
        return record.status()
