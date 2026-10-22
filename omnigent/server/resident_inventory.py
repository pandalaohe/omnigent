"""Read current runner owner evidence without interpreting backends as contexts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx

from omnigent.db.account_authority import AccountAuthority
from omnigent.db.db_models import current_workspace_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    HostOwnedRunner,
    HostOwnerInventoryFrame,
    HostOwnerInventoryResultFrame,
    HostOwnerInventorySnapshot,
    encode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore

_HOST_BINDING_PAGE_SIZE = 100
_MAX_HOST_BINDING_PAGES = 10


@dataclass(frozen=True)
class HostRunnerMembership:
    runner_id: str
    host_owner: HostOwnedRunner | None
    session_ids: tuple[str, ...]


@dataclass(frozen=True)
class HostMembershipInventory:
    """Two sampled registration sources, not atomic vendor membership or absence proof."""

    connection_id: str
    host_generation: str
    host: HostOwnerInventorySnapshot
    runners: tuple[HostRunnerMembership, ...]
    unbound_session_ids: tuple[str, ...]
    aliases_complete: bool = False
    context_coverage_complete: bool = False


async def read_host_membership_inventory(
    hosts: HostRegistry,
    conn: HostConnection,
    conversations: ConversationStore,
    *,
    timeout_s: float = 5.0,
) -> HostMembershipInventory | None:
    """Union current host registrations and all host bindings without launching work."""
    if (
        not 0 < timeout_s <= 5.0
        or current_workspace_id() != conn.workspace_id
        or type(conversations).list_host_conversation_ids
        is ConversationStore.list_host_conversation_ids
    ):
        return None
    owner = (conn.ws, conn.workspace_id, conn.owner, conn.account_generation)

    def bindings() -> tuple[tuple[str, str | None], ...] | None:
        result: list[tuple[str, str | None]] = []
        after = None
        for _ in range(_MAX_HOST_BINDING_PAGES):
            page = conversations.list_host_conversation_ids(
                conn.host_id, after=after, limit=_HOST_BINDING_PAGE_SIZE
            )
            if (
                len(page) > _HOST_BINDING_PAGE_SIZE
                or page != sorted(set(page))
                or (page and after is not None and page[0] <= after)
            ):
                return None
            runners = conversations.get_runner_ids(page)
            if set(runners) != set(page):
                return None
            result.extend((session_id, runners[session_id]) for session_id in page)
            if len(page) < _HOST_BINDING_PAGE_SIZE:
                return tuple(result)
            after = page[-1]
        return None

    try:
        async with asyncio.timeout(timeout_s):
            first = await read_host_owner_inventory(hosts, conn, timeout_s=timeout_s)
            if first is None or first.inventory is None:
                return None
            selected = await asyncio.to_thread(bindings)
            if selected is None:
                return None
            final = await read_host_owner_inventory(
                hosts,
                conn,
                expected_registration_token=first.inventory.registration_token,
                timeout_s=timeout_s,
            )
            if (
                final is None
                or final.inventory != first.inventory
                or final.connection_id != first.connection_id
                or final.host_generation != first.host_generation
                or await asyncio.to_thread(bindings) != selected
                or hosts.get(conn.host_id, workspace_id=owner[1]) is not conn
                or (conn.ws, conn.workspace_id, conn.owner, conn.account_generation) != owner
                or conn.startup_generation != first.host_generation
                or conn.hello.connection_id != first.connection_id
            ):
                return None
    except (ConnectionError, TimeoutError, NotImplementedError):
        return None
    host_owners = {item.runner_id: item for item in first.inventory.runners}
    sessions: dict[str, list[str]] = {
        runner_id: [] for runner_id in (*host_owners, *first.inventory.pending_runner_ids)
    }
    unbound = []
    for session_id, runner_id in selected:
        if runner_id is None:
            unbound.append(session_id)
        else:
            sessions.setdefault(runner_id, []).append(session_id)
    return HostMembershipInventory(
        first.connection_id,
        first.host_generation,
        first.inventory,
        tuple(
            HostRunnerMembership(runner_id, host_owners.get(runner_id), tuple(session_ids))
            for runner_id, session_ids in sorted(sessions.items())
        ),
        tuple(unbound),
    )


async def read_host_owner_inventory(
    hosts: HostRegistry,
    conn: HostConnection,
    *,
    expected_registration_token: str | None = None,
    timeout_s: float = 5.0,
) -> HostOwnerInventoryResultFrame | None:
    """Read or revalidate copied registrations on this exact authenticated host tunnel."""
    connection_id = conn.hello.connection_id
    ws = conn.ws
    owner = (conn.workspace_id, conn.owner, conn.account_generation, conn.startup_generation)
    if (
        not 0 < timeout_s <= 5.0
        or not connection_id
        or hosts.get(conn.host_id, workspace_id=conn.workspace_id) is not conn
        or "resident_inventory_v1" not in conn.hello.capabilities
    ):
        return None
    request_id = uuid4().hex
    future: asyncio.Future[HostOwnerInventoryResultFrame] = (
        asyncio.get_running_loop().create_future()
    )
    conn.pending_owner_inventory[request_id] = future
    try:
        async with asyncio.timeout(timeout_s):
            hosts.send_text(
                conn,
                encode_host_frame(
                    HostOwnerInventoryFrame(
                        request_id, connection_id, owner[3], expected_registration_token
                    )
                ),
            )
            result = await future
        if (
            hosts.get(conn.host_id, workspace_id=owner[0]) is not conn
            or conn.ws is not ws
            or (conn.workspace_id, conn.owner, conn.account_generation, conn.startup_generation)
            != owner
            or conn.hello.connection_id != connection_id
            or result.request_id != request_id
            or result.connection_id != connection_id
            or result.host_generation != owner[3]
            or result.inventory is None
            or (
                expected_registration_token is not None
                and result.inventory.registration_token != expected_registration_token
            )
        ):
            return None
        return result
    except (ConnectionError, TimeoutError):
        return None
    finally:
        conn.pending_owner_inventory.pop(request_id, None)
        if not future.done():
            future.cancel()
        elif not future.cancelled():
            future.exception()


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
