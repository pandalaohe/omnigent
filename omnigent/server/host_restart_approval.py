"""Materialize restart cards and bind their verdicts to stored previews."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import HTTPException

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import CAP_HOST_RESTART
from omnigent.runtime import pending_elicitations
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.host_restart import RestartPreview
from omnigent.stores import ConversationStore


def approved_tool_preview(
    registry: HostRegistry,
    conn: HostConnection,
    owner: str | None,
    session_id: str | None,
    force: bool,
) -> RestartPreview:
    candidates = [
        preview
        for preview in registry.restarts.previews.values()
        if preview.conn is conn
        and preview.owner == owner
        and preview.requester == session_id
        and preview.force == force
        and preview.drain_timeout_s == 120
        and preview.tool_approved
        and preview.expires > registry.restarts.clock()
    ]
    if not candidates:
        raise HTTPException(409, "fresh host restart approval required")
    return max(candidates, key=lambda preview: preview.expires)


async def restart_card_fields(
    session_id: str,
    elicitation_id: str,
    tool_name: str | None,
    tool_input: dict[str, Any] | None,
    store: ConversationStore | None,
) -> dict[str, Any]:
    if (
        tool_name != "sys_host_restart"
        or not isinstance(tool_input, dict)
        or tool_input.get("dry_run", True) is True
    ):
        return {}
    from omnigent.server.routes._sessions.common import get_server_host_registry

    registry = get_server_host_registry()
    if registry is None or store is None:
        raise OmnigentError("Host restart is unavailable", code=ErrorCode.CONFLICT)
    host = tool_input.get("host")
    if not isinstance(host, str) or not host.strip():
        raise OmnigentError("Invalid host restart target", code=ErrorCode.INVALID_INPUT)
    host = host.strip()
    candidates = [
        conn
        for host_id in registry.online_host_ids()
        if (conn := registry.get(host_id)) is not None and host in (conn.host_id, conn.hello.name)
    ]
    if len(candidates) != 1:
        raise OmnigentError(
            "Host not found or ambiguous; use its exact id", code=ErrorCode.CONFLICT
        )
    conn = candidates[0]
    conv = await asyncio.to_thread(store.get_conversation, session_id)
    if conv is None:
        raise OmnigentError("Restart session is unavailable", code=ErrorCode.CONFLICT)
    authority = await asyncio.to_thread(
        store.get_session_owner_authority, conv.root_conversation_id or conv.id, owner_only=True
    )
    owner = authority.user_id if authority is not None else None
    if owner != conn.owner and not (conn.owner is None and owner == "local"):
        raise OmnigentError("Only the host owner may restart it", code=ErrorCode.FORBIDDEN)
    if CAP_HOST_RESTART not in conn.hello.capabilities or conn.hello.supervised is None:
        raise OmnigentError("Host is not supervised for restart", code=ErrorCode.CONFLICT)
    force = tool_input.get("force", False)
    if not isinstance(force, bool):
        raise OmnigentError("Invalid restart force option", code=ErrorCode.INVALID_INPUT)
    fields: dict[str, Any] = {"host_restart_owner": conn.owner}
    try:
        preview_id = tool_input.get("preview_id")
        if preview_id is None:
            # A hook retry reuses the same card and therefore its original preview.
            existing = next(
                (
                    p
                    for p in registry.restarts.previews.values()
                    if p.tool_approval_id == elicitation_id and p.requester == session_id
                ),
                None,
            )
            if existing is not None:
                preview_id = existing.payload["preview_id"]
            else:
                from omnigent.runner.routing import routing_host_id

                payload = await registry.restarts.preview(
                    conn,
                    owner=conn.owner,
                    requester=session_id,
                    force=force,
                    drain_timeout_s=120,
                    caller_on_host=await asyncio.to_thread(routing_host_id, conv, store)
                    == conn.host_id,
                )
                preview_id = payload["preview_id"]
        if not isinstance(preview_id, str):
            raise OmnigentError("Invalid restart preview", code=ErrorCode.INVALID_INPUT)
        preview = registry.restarts.get_preview(preview_id, conn, conn.owner)
        if (
            preview.requester != session_id
            or preview.force != force
            or preview.drain_timeout_s != 120
        ):
            raise OmnigentError("Restart preview options do not match", code=ErrorCode.CONFLICT)
        if preview.tool_approval_id != elicitation_id:
            preview.tool_approval_id, preview.tool_approved = elicitation_id, False
        fields.update(
            {
                "host_restart_preview_id": preview_id,
                "content_preview": json.dumps(preview.payload, ensure_ascii=False),
                "message": f"Restart host {conn.hello.name}? "
                "This ends all its runners and terminals. "
                f"Supervisor: {preview.payload['supervisor']}; "
                f"runners: {preview.payload['runner_count']}; "
                f"running sessions: {', '.join(preview.payload['running_sessions']) or 'none'}; "
                f"requester on host: {preview.payload['caller_on_host']}. "
                "Preview expires after 120 seconds.",
            }
        )
    except HTTPException as exc:
        raise OmnigentError(str(exc.detail), code=ErrorCode.CONFLICT) from exc
    return fields


def authorize_restart_resolution(
    session_id: str, elicitation_id: str, user_id: str | None, action: object
) -> None:
    """Both HTTP resolution paths require the host owner, without admin bypass."""
    found = pending_elicitations.lookup(elicitation_id, conversation_id=session_id)
    if found is None:
        return
    params = found[1].get("params")
    if not isinstance(params, dict) or "host_restart_owner" not in params:
        return
    if params["host_restart_owner"] != user_id:
        raise OmnigentError(
            "Only the host owner may resolve this restart card", code=ErrorCode.FORBIDDEN
        )
    if action != "accept":
        return
    from omnigent.server.routes._sessions.common import get_server_host_registry

    registry = get_server_host_registry()
    preview_id = params.get("host_restart_preview_id")
    preview = (
        registry.restarts.previews.get(preview_id)
        if registry is not None and isinstance(preview_id, str)
        else None
    )
    if preview is None or preview.tool_approval_id != elicitation_id:
        raise OmnigentError("Restart preview is no longer available", code=ErrorCode.CONFLICT)
    assert registry is not None
    registry.restarts.get_preview(preview_id, preview.conn, user_id)
    preview.tool_approved = True


def require_restart_card_approval(session_id: str, elicitation_id: str) -> None:
    """An MCP retry's inputResponses cannot manufacture an application verdict."""
    from omnigent.server.routes._sessions.common import get_server_host_registry

    registry = get_server_host_registry()
    if registry is not None and any(
        p.requester == session_id
        and p.tool_approval_id == elicitation_id
        and p.tool_approved
        and p.expires > registry.restarts.clock()
        for p in registry.restarts.previews.values()
    ):
        return
    raise OmnigentError(
        "Host restart has not been approved by its owner", code=ErrorCode.FORBIDDEN
    )
