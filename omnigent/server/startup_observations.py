"""Accept exact runner-owned startup metadata from relay or read-only reconciliation."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import FastAPI

from omnigent.db.account_authority import AccountAuthority
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import session_stream
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.schemas import SessionStartupEvent
from omnigent.server.startup_inputs import reconcile_accepted_startup_input
from omnigent.startup_protocol import StartupAttemptRef, StartupObservation
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore, StartupSnapshot


async def _current_binding(
    conversations: ConversationStore,
    hosts: HostRegistry,
    startups: SessionStartupStore,
    session_id: str,
    runner_id: str,
    observation: StartupObservation,
    *,
    expected_connection: HostConnection | None = None,
) -> tuple[StartupSnapshot, HostConnection] | None:
    conv = await asyncio.to_thread(conversations.get_conversation, session_id)
    current = await asyncio.to_thread(startups.for_session, session_id)
    if conv is None or current is None or conv.archived or not conv.host_id:
        return None
    conn = hosts.get(conv.host_id)
    ref = observation.attempt
    if (
        conv.runner_id != runner_id
        or conn is None
        or (expected_connection is not None and conn is not expected_connection)
        or current.id != ref.startup_id
        or current.session_id != session_id
        or current.host_id != conv.host_id
        or current.attempt_id != ref.attempt_id
        or current.runtime_id != ref.runtime_id
        or current.input_id != ref.input_id
        or current.host_generation != ref.host_generation
        or conn.startup_generation != ref.host_generation
        or "startup_admission_v1" not in conn.hello.capabilities
        or (
            observation.source != "runner"
            and f"startup_receipts:{observation.source}" not in conn.hello.capabilities
        )
    ):
        return None
    return current, conn


async def accept_observation(
    app: FastAPI,
    conversations: ConversationStore,
    session_id: str,
    runner_id: str,
    body: StartupObservation,
) -> bool:
    """Apply one typed receipt only while its runner and host nonce still own it."""
    startups: SessionStartupStore | None = getattr(app.state, "startup_store", None)
    hosts: HostRegistry | None = getattr(app.state, "host_registry", None)
    if startups is None or hosts is None:
        return False
    binding = await _current_binding(conversations, hosts, startups, session_id, runner_id, body)
    if binding is None:
        return False
    current, conn = binding
    account_authority = (
        AccountAuthority(conn.owner, conn.account_generation, conn.workspace_id)
        if conn.owner is not None
        else None
    )
    if body.phase in ("input_dispatched", "input_accepted", "work_started"):
        assert body.vendor_session_id and body.vendor_turn_id
        try:
            await asyncio.to_thread(
                startups.bind_dispatch,
                current.id,
                body.attempt.attempt_id,
                runtime_id=body.attempt.runtime_id,
                source=body.source,
                vendor_session_id=body.vendor_session_id,
                vendor_turn_id=body.vendor_turn_id,
                expected_runner_id=runner_id,
                host_generation=body.attempt.host_generation,
                account_authority=account_authority,
            )
        except OmnigentError as exc:
            if exc.code == ErrorCode.UNAUTHORIZED:
                return False
            raise
    if (
        await _current_binding(
            conversations,
            hosts,
            startups,
            session_id,
            runner_id,
            body,
            expected_connection=conn,
        )
        is None
    ):
        return False
    receipt = body.receipt()
    if body.source == "codex" and body.phase in ("runtime_ready", "tools_ready"):
        receipt = body.model_copy(update={"source": "runner"}).receipt()
    try:
        accepted = await asyncio.to_thread(
            startups.observe,
            receipt,
            expected_runner_id=runner_id,
            host_generation=body.attempt.host_generation,
            account_authority=account_authority,
        )
    except OmnigentError as exc:
        if exc.code == ErrorCode.UNAUTHORIZED:
            return False
        raise
    if not accepted:
        return False
    confirmed = await asyncio.to_thread(startups.get, current.id)
    if confirmed is not None:
        await reconcile_accepted_startup_input(conversations, confirmed)
    status = await asyncio.to_thread(startups.presentation, session_id)
    session_stream.publish(
        session_id,
        SessionStartupEvent(
            type="session.startup", conversation_id=session_id, startup=status
        ).model_dump(),
    )
    admission = getattr(app.state, "startup_admission", None)
    if admission is not None:
        await admission.coordinator.advance(current.host_id)
    return True


async def reconcile_startup_observations(
    app: FastAPI,
    conversations: ConversationStore,
    hosts: HostRegistry,
    startups: SessionStartupStore,
    startup: StartupSnapshot,
) -> bool:
    """Read a runner's current-attempt cache; never dispatch or infer vendor work."""
    if (
        startup.attempt_id is None
        or startup.runtime_id is None
        or startup.host_generation is None
        or startup.state in ("blocked", "cancelled", "stopped")
    ):
        return False
    conv = await asyncio.to_thread(conversations.get_conversation, startup.session_id)
    current = await asyncio.to_thread(startups.for_session, startup.session_id)
    if (
        conv is None
        or conv.archived
        or not conv.runner_id
        or conv.host_id != startup.host_id
        or current is None
        or current.id != startup.id
        or current.attempt_id != startup.attempt_id
        or current.runtime_id != startup.runtime_id
        or current.input_id != startup.input_id
        or current.host_generation != startup.host_generation
    ):
        return False
    conn = hosts.get(startup.host_id)
    if (
        conn is None
        or conn.startup_generation != startup.host_generation
        or "startup_admission_v1" not in conn.hello.capabilities
        or "startup_receipts:codex" not in conn.hello.capabilities
    ):
        return False
    router = getattr(app.state, "runner_router", None)
    if router is None:
        return False
    try:
        routed = await asyncio.to_thread(
            router.client_for_existing_conversation, startup.session_id
        )
    except OmnigentError:
        return False
    if (
        routed is None
        or routed.runner_id != conv.runner_id
        or hosts.get(startup.host_id) is not conn
    ):
        return False
    ref = StartupAttemptRef(
        startup_id=startup.id,
        attempt_id=startup.attempt_id,
        input_id=startup.input_id,
        runtime_id=startup.runtime_id,
        host_generation=startup.host_generation,
    )
    try:
        response = await routed.client.post(
            f"/v1/sessions/{startup.session_id}/startup/observations",
            json=ref.model_dump(),
            timeout=5.0,
        )
        if response.status_code != 200:
            return False
        result: Any = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    if hosts.get(startup.host_id) is not conn:
        return False
    conv_after = await asyncio.to_thread(conversations.get_conversation, startup.session_id)
    current_after = await asyncio.to_thread(startups.for_session, startup.session_id)
    if (
        conv_after is None
        or conv_after.archived
        or conv_after.runner_id != routed.runner_id
        or conv_after.host_id != startup.host_id
        or current_after is None
        or current_after.id != startup.id
        or current_after.attempt_id != ref.attempt_id
        or current_after.runtime_id != ref.runtime_id
        or current_after.input_id != ref.input_id
        or current_after.host_generation != ref.host_generation
        or hosts.get(startup.host_id) is not conn
    ):
        return False
    raw = result.get("observations") if isinstance(result, dict) else None
    if not isinstance(raw, list) or len(raw) > 7:
        return False
    observations: list[StartupObservation] = []
    seen_phases: set[str] = set()
    for item in raw:
        try:
            observation = StartupObservation.model_validate(item)
        except ValueError:
            return False
        if (
            observation.attempt != ref
            or observation.source != "codex"
            or observation.phase in seen_phases
        ):
            return False
        observations.append(observation)
        seen_phases.add(observation.phase)
    accepted = False
    for observation in observations:
        accepted = (
            await accept_observation(
                app, conversations, startup.session_id, routed.runner_id, observation
            )
            or accepted
        )
    return accepted
