"""Human and host ownership checks for elevated terminal operations."""

from __future__ import annotations

import asyncio
from typing import cast
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.requests import HTTPConnection

from omnigent.entities import Conversation
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.runner.routing import routing_host_id
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.ws_origin import origin_allowed, parse_allowed_origins
from omnigent.stores import ConversationStore


def require_terminal_browser_session(
    connection: HTTPConnection, auth_provider: AuthProvider | None
) -> None:
    """Keep browser consent and PTY access out of runner credentials."""
    require_trusted_origin(cast(Request, connection))
    if connection.headers.get(RUNNER_TUNNEL_TOKEN_HEADER):
        raise HTTPException(
            status_code=403, detail="runner credentials cannot authorize admin terminal access"
        )
    if auth_provider is None:
        return
    user_id = auth_provider.get_user_id(connection)
    if user_id == RESERVED_USER_LOCAL:
        return
    origin = connection.headers.get("origin")
    try:
        parts = urlsplit(origin or "")
        _ = parts.port
        valid_origin = (
            parts.scheme in ("http", "https")
            and bool(parts.netloc)
            and not (
                parts.path or parts.query or parts.fragment or parts.username or parts.password
            )
        )
    except ValueError:
        valid_origin = False
    scheme = {"ws": "http", "wss": "https"}.get(connection.url.scheme, connection.url.scheme)
    same_origin = f"{scheme}://{connection.url.netloc}"
    if not valid_origin or not origin_allowed(
        origin, local_mode=False, extra_allowed=parse_allowed_origins() | frozenset({same_origin})
    ):
        raise HTTPException(status_code=403, detail="a trusted browser origin is required")
    if not auth_provider.is_browser_session(connection):
        raise HTTPException(status_code=403, detail="a human browser session is required")


async def require_terminal_host_owner(
    connection: HTTPConnection,
    conv: Conversation,
    auth_provider: AuthProvider | None,
    conversation_store: ConversationStore,
) -> str:
    """Authorize the owner of the host actually running this session's terminal."""
    host_id = await asyncio.to_thread(routing_host_id, conv, conversation_store)
    if not host_id:
        raise HTTPException(status_code=409, detail="admin terminals require a host-bound session")
    host_store = getattr(connection.app.state, "host_store", None)
    if host_store is None:
        raise HTTPException(status_code=403, detail="host ownership cannot be verified")
    user_id = auth_provider.get_user_id(connection) if auth_provider else None
    if auth_provider is not None and user_id is None:
        raise HTTPException(status_code=403, detail="authentication required")
    await asyncio.to_thread(
        resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
    )
    return host_id
