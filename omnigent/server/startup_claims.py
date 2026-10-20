"""Authenticate cold-boundary claims against the live host and saved binding."""

import asyncio

from omnigent.host.frames import HostStartupClaimFrame
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore


async def authorize_host_startup_claim(
    frame: HostStartupClaimFrame,
    *,
    conn: HostConnection,
    registry: HostRegistry,
    conversations: ConversationStore,
    startups: SessionStartupStore,
) -> bool:
    ref = frame.attempt
    if (
        registry.get(conn.host_id) is not conn
        or ref.host_generation != conn.startup_generation
        or registry.launch_authorizer is None
    ):
        return False
    current = await asyncio.to_thread(startups.get, ref.startup_id)
    conv = await asyncio.to_thread(conversations.get_conversation, frame.session_id)
    if (
        current is None
        or conv is None
        or conv.archived
        or (current.session_id, current.host_id, current.input_id, current.attempt_id)
        != (frame.session_id, conn.host_id, ref.input_id, ref.attempt_id)
        or conv.host_id != conn.host_id
        or conv.runner_id != frame.runner_id
    ):
        return False
    await registry.authorize_launch(conn, frame.session_id)
    conv = await asyncio.to_thread(conversations.get_conversation, frame.session_id)
    if (
        registry.get(conn.host_id) is not conn
        or conv is None
        or conv.archived
        or conv.host_id != conn.host_id
        or conv.runner_id != frame.runner_id
    ):
        return False
    granted = await asyncio.to_thread(
        startups.claim_boundary,
        ref.startup_id,
        ref.attempt_id,
        runtime_id=ref.runtime_id,
        host_generation=ref.host_generation,
        boundary=frame.boundary,
        claim_id=frame.request_id,
        expected_runner_id=frame.runner_id,
    )
    return granted and registry.get(conn.host_id) is conn
