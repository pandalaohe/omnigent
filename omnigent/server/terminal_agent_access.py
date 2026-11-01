"""Instance-bound reservations and human verdicts for terminal access cards."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Request

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.entities import Conversation
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import routing_host_id
from omnigent.runtime import pending_elicitations
from omnigent.server.auth import AuthProvider
from omnigent.server.browser_approval import require_owner_browser_approval
from omnigent.server.host_registry import HostConnection
from omnigent.stores import ConversationStore

logger = logging.getLogger(__name__)
TERMINAL_ACCESS_TOOL = "sys_terminal_request_access"


@dataclass
class _AccessRequest:
    session_id: str
    terminal_id: str
    instance_id: str
    terminal: str
    session_key: str
    reason: str
    elicitation_id: str
    host_id: str
    owner: str | None
    deadline: float
    approved: bool = False


_requests: WorkspaceScopedCache[tuple[str, str, str], _AccessRequest] = WorkspaceScopedCache()
_resolution_locks: WorkspaceScopedCache[tuple[str, str, str], asyncio.Lock] = WorkspaceScopedCache(
    weakref.WeakValueDictionary
)
_lock = threading.Lock()


def terminal_access_lock(session_id: str, terminal_id: str, instance_id: str) -> asyncio.Lock:
    """Serialize browser switches, card verdicts and withdrawal for one instance."""
    with _lock:
        key = (session_id, terminal_id, instance_id)
        lock = _resolution_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _resolution_locks[key] = lock
        return lock


async def _terminal_context(
    session_id: str, terminal_id: str, store: ConversationStore
) -> tuple[Conversation, HostConnection, httpx.AsyncClient, dict[str, Any]]:
    from omnigent.server.routes._sessions.common import get_server_host_registry
    from omnigent.server.routes._sessions.helpers import _get_runner_client_for_resource_access

    conv = await asyncio.to_thread(store.get_conversation, session_id)
    if conv is None:
        raise OmnigentError("Terminal session not found", code=ErrorCode.NOT_FOUND)
    host_id = await asyncio.to_thread(routing_host_id, conv, store)
    hosts = get_server_host_registry()
    conn = hosts.get(host_id) if hosts is not None and host_id is not None else None
    client = await _get_runner_client_for_resource_access(session_id, conversation=conv)
    if conn is None or client is None:
        raise OmnigentError("Terminal host is unavailable", code=ErrorCode.CONFLICT)
    try:
        response = await client.get(
            f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}", timeout=10
        )
    except httpx.HTTPError as exc:
        raise OmnigentError("Terminal host is unavailable", code=ErrorCode.CONFLICT) from exc
    if response.status_code != 200:
        raise OmnigentError("Terminal not found", code=ErrorCode.NOT_FOUND)
    try:
        resource = response.json()
    except ValueError as exc:
        raise OmnigentError("Invalid terminal resource", code=ErrorCode.CONFLICT) from exc
    if not isinstance(resource, dict) or not isinstance(resource.get("metadata"), dict):
        raise OmnigentError("Invalid terminal resource", code=ErrorCode.CONFLICT)
    return conv, conn, client, resource


def _current_instance(resource: dict[str, Any], instance_id: str) -> dict[str, Any]:
    metadata = resource["metadata"]
    if metadata.get("instance_id") != instance_id or metadata.get("running") is not True:
        raise OmnigentError("Terminal instance not found", code=ErrorCode.CONFLICT)
    if metadata.get("elevated") is not True:
        raise OmnigentError("Not an admin terminal", code=ErrorCode.CONFLICT)
    return metadata


async def terminal_access_card_fields(
    session_id: str,
    elicitation_id: str,
    tool_name: str | None,
    tool_input: dict[str, Any] | None,
    store: ConversationStore | None,
    timeout_s: float,
) -> dict[str, Any]:
    """Validate and reserve before either policy path publishes an ASK card."""
    if tool_name is None or tool_name.removeprefix("mcp__omnigent__") != TERMINAL_ACCESS_TOOL:
        return {}
    if store is None or not isinstance(tool_input, dict):
        raise OmnigentError("Terminal access approval is unavailable", code=ErrorCode.CONFLICT)
    if any(
        not isinstance(tool_input.get(key), str) or not tool_input[key].strip()
        for key in ("terminal", "session", "instance_id", "reason")
    ):
        raise OmnigentError(
            "Terminal, session, instance_id and reason are required", code=ErrorCode.INVALID_INPUT
        )
    terminal, session_key, instance_id, reason = (
        tool_input[key] for key in ("terminal", "session", "instance_id", "reason")
    )
    if len(reason) > 4096:
        raise OmnigentError("Terminal access reason is too long", code=ErrorCode.INVALID_INPUT)
    terminal_id = terminal_resource_id(terminal, session_key)
    conv, conn, _, resource = await _terminal_context(session_id, terminal_id, store)
    if conn.owner is None:
        raise OmnigentError("Terminal host has no owner", code=ErrorCode.CONFLICT)
    metadata = _current_instance(resource, instance_id)
    key = (session_id, terminal_id, instance_id)
    with _lock:
        previous = _requests.get(key)
    if previous is not None and not previous.approved and previous.deadline <= time.monotonic():
        await end_terminal_access_requests(session_id, terminal_id, instance_id, store)
    with _lock:
        existing = _requests.get(key)
        if metadata.get("agent_access") is True:
            if (
                existing is None
                or not existing.approved
                or existing.elicitation_id != elicitation_id
            ):
                raise OmnigentError("Agent access is already granted", code=ErrorCode.CONFLICT)
        elif (
            existing is not None
            and existing.approved
            and existing.elicitation_id == elicitation_id
        ):
            raise OmnigentError("Agent access was revoked", code=ErrorCode.FORBIDDEN)
        if (
            existing is not None
            and not existing.approved
            and existing.deadline > time.monotonic()
            and existing.elicitation_id != elicitation_id
        ):
            raise OmnigentError("Terminal access request is pending", code=ErrorCode.CONFLICT)
        if existing is None or existing.elicitation_id != elicitation_id:
            existing = _AccessRequest(
                session_id,
                terminal_id,
                instance_id,
                terminal,
                session_key,
                reason,
                elicitation_id,
                conn.host_id,
                conn.owner,
                time.monotonic() + timeout_s,
            )
            _requests[key] = existing
        elif (
            existing.reason != reason
            or existing.host_id != conn.host_id
            or existing.owner != conn.owner
        ):
            raise OmnigentError("Terminal request identity changed", code=ErrorCode.CONFLICT)
    return {
        "terminal_access_owner": existing.owner,
        "terminal_access_terminal_id": terminal_id,
        "terminal_access_instance_id": instance_id,
        "terminal_access_host_id": conn.host_id,
        "message": f"Allow this session's agent to use admin terminal {terminal}:{session_key} "
        f"on {conn.hello.name}? "
        f"Session: {conv.title or session_id}; agent: {conv.agent_id}. "
        f"Access lasts until this terminal closes or you turn it off. Reason: {reason}",
    }


async def authorize_terminal_access_resolution(
    session_id: str,
    elicitation_id: str,
    user_id: str | None,
    action: object,
    *,
    request: Request | None,
    auth_provider: AuthProvider | None,
    store: ConversationStore,
) -> None:
    """Both human-answer paths use the same browser proof and atomic runner switch."""
    found = pending_elicitations.lookup(elicitation_id, conversation_id=session_id)
    params = found[1].get("params") if found is not None else None
    if not isinstance(params, dict) or "terminal_access_owner" not in params:
        return
    terminal_id = params.get("terminal_access_terminal_id")
    instance_id = params.get("terminal_access_instance_id")
    if not isinstance(terminal_id, str) or not isinstance(instance_id, str):
        raise OmnigentError("Invalid terminal card identity", code=ErrorCode.CONFLICT)
    key = (session_id, terminal_id, instance_id)
    with _lock:
        pending = _requests.get(key)
    if pending is None or pending.elicitation_id != elicitation_id:
        raise OmnigentError(
            "Terminal access request is no longer pending", code=ErrorCode.CONFLICT
        )
    if request is None:
        raise OmnigentError("Only a browser may answer this card", code=ErrorCode.FORBIDDEN)
    require_owner_browser_approval(request, auth_provider, pending.owner)
    if user_id != pending.owner:
        raise OmnigentError("Only the host owner may answer this card", code=ErrorCode.FORBIDDEN)
    async with terminal_access_lock(session_id, terminal_id, instance_id):
        if pending.approved:
            if action == "accept":
                return
            raise OmnigentError(
                "Terminal access card was already answered", code=ErrorCode.CONFLICT
            )
        with _lock:
            if _requests.get(key) is not pending:
                raise OmnigentError("Terminal request ended", code=ErrorCode.CONFLICT)
        if pending.deadline <= time.monotonic():
            raise OmnigentError("Terminal access card expired", code=ErrorCode.CONFLICT)
        if action != "accept":
            with _lock:
                _requests.pop(key, None)
            return
        from omnigent.server.routes._host_launch import resolve_host_owner

        host_store = getattr(request.app.state, "host_store", None)
        if host_store is None:
            raise OmnigentError("Host ownership cannot be verified", code=ErrorCode.FORBIDDEN)
        await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=pending.host_id, host_store=host_store
        )
        _, conn, client, resource = await _terminal_context(session_id, pending.terminal_id, store)
        _current_instance(resource, pending.instance_id)
        if conn.host_id != pending.host_id or conn.owner != pending.owner:
            raise OmnigentError("Terminal host ownership changed", code=ErrorCode.FORBIDDEN)
        try:
            response = await client.patch(
                f"/v1/sessions/{session_id}/resources/terminals/{pending.terminal_id}",
                json={"agent_access": True, "instance_id": pending.instance_id},
                timeout=10,
            )
        except httpx.HTTPError as exc:
            raise OmnigentError("Terminal host is unavailable", code=ErrorCode.CONFLICT) from exc
        if response.status_code != 200:
            raise OmnigentError("Terminal instance no longer available", code=ErrorCode.CONFLICT)
        try:
            updated = response.json()
        except ValueError as exc:
            raise OmnigentError("Invalid terminal resource", code=ErrorCode.CONFLICT) from exc
        if not isinstance(updated, dict) or not isinstance(updated.get("metadata"), dict):
            raise OmnigentError("Invalid terminal resource", code=ErrorCode.CONFLICT)
        if _current_instance(updated, pending.instance_id).get("agent_access") is not True:
            raise OmnigentError("Terminal access was not granted", code=ErrorCode.CONFLICT)
        pending.approved = True
        from omnigent.server.routes._sessions.helpers import _publish_and_persist_resource_event

        _publish_and_persist_resource_event(
            session_id,
            "session.resource.created",
            pending.terminal_id,
            "terminal",
            store,
            updated,
        )
        logger.info(
            "Terminal agent access changed user=%s host=%s session=%s "
            "terminal=%s instance=%s enabled=True route=C",
            user_id,
            pending.host_id,
            session_id,
            pending.terminal_id,
            pending.instance_id,
        )


def require_terminal_card_approval(session_id: str, elicitation_id: str) -> None:
    """MCP inputResponses cannot substitute for the human browser verdict."""
    with _lock:
        if any(
            item.session_id == session_id
            and item.elicitation_id == elicitation_id
            and item.approved
            for item in _requests.values()
        ):
            return
    raise OmnigentError(
        "Terminal access has not been approved by its owner", code=ErrorCode.FORBIDDEN
    )


def release_terminal_access_request(session_id: str, elicitation_id: str) -> None:
    """Release declined/expired reservations while retaining accepted retry proof."""
    with _lock:
        for key, item in list(_requests.items()):
            if (
                item.session_id == session_id
                and item.elicitation_id == elicitation_id
                and not item.approved
            ):
                _requests.pop(key, None)


async def settle_terminal_access_switch(
    session_id: str, terminal_id: str, instance_id: str, enabled: bool, store: ConversationStore
) -> None:
    """Settle a web switch while its caller holds the instance resolution lock."""
    from omnigent.server.routes._sessions.common import get_server_runner_router
    from omnigent.server.routes._sessions.orchestration import _resolve_elicitation

    key = (session_id, terminal_id, instance_id)
    with _lock:
        pending = _requests.get(key)
        if pending is None:
            return
        pending.approved = enabled
        if not enabled:
            _requests.pop(key, None)
    if pending_elicitations.lookup(pending.elicitation_id, conversation_id=session_id) is not None:
        await _resolve_elicitation(
            session_id,
            {
                "elicitation_id": pending.elicitation_id,
                "action": "accept" if enabled else "cancel",
            },
            get_server_runner_router(),
            store,
        )


async def end_terminal_access_requests(
    session_id: str, terminal_id: str, instance_id: str | None, store: ConversationStore
) -> None:
    """An ended instance withdraws its card and releases any parked caller."""
    from omnigent.server.routes._sessions.common import get_server_runner_router
    from omnigent.server.routes._sessions.orchestration import _resolve_elicitation

    with _lock:
        ended = [
            item
            for item in _requests.values()
            if item.session_id == session_id
            and item.terminal_id == terminal_id
            and (instance_id is None or item.instance_id == instance_id)
        ]
    for item in ended:
        async with terminal_access_lock(session_id, terminal_id, item.instance_id):
            with _lock:
                key = (session_id, terminal_id, item.instance_id)
                if _requests.get(key) is not item:
                    continue
                _requests.pop(key, None)
            if (
                pending_elicitations.lookup(item.elicitation_id, conversation_id=session_id)
                is not None
            ):
                await _resolve_elicitation(
                    session_id,
                    {"elicitation_id": item.elicitation_id, "action": "cancel"},
                    get_server_runner_router(),
                    store,
                )
