"""Read current runner owner evidence without interpreting backends as contexts."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import httpx

from omnigent.db.account_authority import AccountAuthority
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore


async def retain_unknown_owner_inventory(
    app: Any,
    conversations: ConversationStore,
    hosts: HostRegistry,
    startups: SessionStartupStore,
    conn: HostConnection,
    session_id: str,
) -> bool:
    """Apply only a same-account/host/runner-generation partial read.

    Success means an unknown-coverage veto was retained, not successful adoption.
    Even an empty owner map cannot enumerate independent vendor contexts yet.
    """
    if (
        hosts.get(conn.host_id) is not conn
        or "resident_inventory_v1" not in conn.hello.capabilities
    ):
        return False
    owner = (conn.workspace_id, conn.owner, conn.account_generation)
    conv = await asyncio.to_thread(conversations.get_conversation, session_id)
    router = getattr(app.state, "runner_router", None)
    initializer = getattr(app.state, "runner_session_initializer", None)
    if (
        conv is None
        or conv.archived
        or conv.host_id != conn.host_id
        or not conv.runner_id
        or router is None
        or initializer is None
        or not router.runner_has_capability(conv.runner_id, "resident_inventory_v1")
    ):
        return False
    authority = (
        AccountAuthority(conn.owner, conn.account_generation, conn.workspace_id)
        if conn.owner is not None
        else None
    )
    host_generation = conn.startup_generation
    try:
        routed = await asyncio.to_thread(router.client_for_existing_conversation, session_id)
        if routed is None or routed.runner_id != conv.runner_id:
            return False
        generation = initializer.generation_for(routed.runner_id, routed.client)
        read_id = uuid4().hex
        response = await routed.client.get(
            "/v1/startup/owner-inventory", params={"read_id": read_id}, timeout=5.0
        )
        if response.status_code != 200:
            return False
        body = response.json()
        if (
            not isinstance(body, dict)
            or body.get("read_id") != read_id
            or not isinstance(body.get("native_backends"), dict)
            or "terminals" not in body
            or body.get("executor_contexts_complete") is not False
            or body.get("external_owners_complete") is not False
        ):
            return False
        initializer.require_generation(routed.runner_id, routed.client, generation)
        live = await asyncio.to_thread(conversations.get_conversation, session_id)
        if (
            live is None
            or live.archived
            or (live.host_id, live.runner_id) != (conn.host_id, routed.runner_id)
            or hosts.get(conn.host_id) is not conn
            or conn.startup_generation != host_generation
            or (conn.workspace_id, conn.owner, conn.account_generation) != owner
        ):
            return False
        return await asyncio.to_thread(
            startups.note_unknown_resident_inventory,
            conn.host_id,
            host_generation=host_generation,
            session_id=session_id,
            expected_runner_id=routed.runner_id,
            account=authority,
            runner_generation=initializer.generation_guard(
                routed.runner_id, routed.client, generation
            ),
        )
    except (httpx.HTTPError, ValueError, ConnectionError):
        return False
    except OmnigentError as exc:
        if exc.code == ErrorCode.UNAUTHORIZED:
            return False
        raise
