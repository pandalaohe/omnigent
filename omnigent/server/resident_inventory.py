"""Read current runner owner evidence without interpreting backends as contexts."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import httpx
from pydantic import TypeAdapter

from omnigent.db.account_authority import AccountAuthority
from omnigent.db.db_models import current_workspace_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    HostContextInventoryV2Frame,
    HostContextInventoryV2ResultFrame,
    HostOwnedRunner,
    HostOwnerInventoryFrame,
    HostOwnerInventoryResultFrame,
    HostOwnerInventorySnapshot,
    encode_host_frame,
)
from omnigent.host.recovery import RecoveryAssessment
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.startup_protocol import (
    CONTEXT_INVENTORY_CONTRACT_HEADER,
    CONTEXT_INVENTORY_MAX_BYTES,
    CONTEXT_INVENTORY_V2,
    CONTEXT_WORK_INVENTORY_V2,
    ContextAuthorityV2,
    ContextEvidenceV2,
    ContextWorkEvidenceV2,
    ScopeKeyV2,
    decode_context_evidence_v2,
    decode_context_work_evidence_v2,
    harness_context_authority_v2,
    host_context_scope_v2,
    session_context_scopes_v2,
)
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore

if TYPE_CHECKING:
    from omnigent.runner.resident_inventory import OwnerRegistrationSnapshot
    from omnigent.runner.routing import RunnerRouter
    from omnigent.server.runner_session_init import RunnerSessionInitializer

_HOST_BINDING_PAGE_SIZE = 100
_MAX_HOST_BINDING_PAGES = 10
_MAX_CONCURRENT_RUNNER_READS = 4
_MAX_HOST_CONTEXT_SESSIONS = 1000
_MAX_HOST_CONTEXT_BYTES = CONTEXT_INVENTORY_MAX_BYTES


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


@dataclass(frozen=True)
class HostRunnerContextInventory:
    membership: HostRunnerMembership
    runner_generation: int | None
    inventory: OwnerRegistrationSnapshot | None


@dataclass(frozen=True)
class HostContextInventory:
    membership: HostMembershipInventory
    runners: tuple[HostRunnerContextInventory, ...]
    aliases_complete: bool = False
    context_coverage_complete: bool = False


def _decode_runner_context_inventory(payload: object) -> OwnerRegistrationSnapshot | None:
    from omnigent.runner.resident_inventory import OwnerRegistrationSnapshot

    try:
        snapshot = TypeAdapter(OwnerRegistrationSnapshot).validate_json(
            json.dumps(payload), strict=True
        )
        # Round-trip equality rejects ignored extras and omitted default fields at every depth.
        if (
            json.loads(json.dumps(asdict(snapshot))) != payload
            or snapshot.executor_contexts_complete
            or snapshot.external_owners_complete
            or snapshot.native_backends.aliases_complete
            or any(o.aliases_complete for o in snapshot.native_backends.owners)
        ):
            return None
        native_ids = [o.incarnation_id for o in snapshot.native_backends.owners]
        if (
            not all(native_ids)
            or len(set(native_ids)) != len(native_ids)
            or any(
                not owner.session_ids
                or not all(owner.session_ids)
                or len(set(owner.session_ids)) != len(owner.session_ids)
                for owner in snapshot.native_backends.owners
            )
            or any(
                owner.transport_generation is not None
                and (
                    owner.transport_generation < 0
                    or owner.harness != "codex-native"
                    or not owner.registered
                    or owner.closing
                    or owner.process_exit_confirmed
                    or owner.exit_confirmed
                )
                for owner in snapshot.native_backends.owners
            )
        ):
            return None
        native_threads = snapshot.native_thread_ids
        if native_threads is not None:
            if len(native_threads) != len(snapshot.native_backends.owners):
                return None
            for owner, ids in zip(snapshot.native_backends.owners, native_threads, strict=True):
                if ids is not None and (
                    owner.transport_generation is None
                    or len(ids) > 1000
                    or not all(ids)
                    or len(set(ids)) != len(ids)
                ):
                    return None
        harnesses = snapshot.harnesses
        if harnesses is not None:
            ids = [o.incarnation_id for o in harnesses.owners]
            if (
                not all(ids)
                or len(set(ids)) != len(ids)
                or any(not owner.session_id for owner in harnesses.owners)
            ):
                return None
            if harnesses.contexts is not None:
                if len(harnesses.contexts) != len(harnesses.owners):
                    return None
                for owner, read in zip(harnesses.owners, harnesses.contexts, strict=True):
                    if read is None:
                        continue
                    inventory = read.inventory
                    shared_context_ids = {
                        context_id
                        for context_id, count in Counter(
                            item.vendor_context_id
                            for item in inventory.owners
                            if item.vendor_context_id is not None
                        ).items()
                        if count > 1
                    }
                    if not shared_context_ids.issubset(inventory.ambiguous_context_ids):
                        return None
                    ids = [o.incarnation_id for o in inventory.owners]
                    if (
                        read.owner != owner
                        or owner.closing
                        or owner.wrapper_exited
                        or owner.exit_confirmed
                        or read.release_generation < 0
                        or inventory.aliases_complete
                        or inventory.context_coverage_complete
                        or not all(inventory.ambiguous_context_ids)
                        or len(set(inventory.ambiguous_context_ids))
                        != len(inventory.ambiguous_context_ids)
                        or not all(ids)
                        or len(set(ids)) != len(ids)
                        or any(
                            not o.session_keys
                            or not all(o.session_keys)
                            or len(set(o.session_keys)) != len(o.session_keys)
                            or (
                                o.transport_generation is not None
                                and (
                                    o.transport_generation < 0
                                    or not o.started
                                    or o.closing
                                    or o.cleanup_complete
                                )
                            )
                            for o in inventory.owners
                        )
                    ):
                        return None
        harness_threads = snapshot.harness_thread_ids
        if harness_threads is not None:
            if (
                harnesses is None
                or harnesses.contexts is None
                or len(harness_threads) != len(harnesses.owners)
            ):
                return None
            for read, thread_ids in zip(harnesses.contexts, harness_threads, strict=True):
                if thread_ids is None:
                    continue
                if read is None or len(thread_ids) != len(read.inventory.owners):
                    return None
                if any(
                    ids is not None
                    and (
                        owner.transport_generation is None
                        or len(ids) > 1000
                        or not all(ids)
                        or len(set(ids)) != len(ids)
                    )
                    for owner, ids in zip(read.inventory.owners, thread_ids, strict=True)
                ):
                    return None
        return snapshot
    except (ValueError, TypeError):
        return None


async def read_host_context_inventory(
    hosts: HostRegistry,
    conn: HostConnection,
    conversations: ConversationStore,
    router: RunnerRouter | None,
    initializer: RunnerSessionInitializer | None,
    *,
    timeout_s: float = 5.0,
) -> HostContextInventory | None:
    """Aggregate sampled runner contexts; unresolved owners remain explicit unknowns."""
    if router is None or initializer is None or not 0 < timeout_s <= 5.0:
        return None
    authority = (conn.ws, conn.workspace_id, conn.owner, conn.account_generation)
    reads: dict[str, tuple[httpx.AsyncClient, int, OwnerRegistrationSnapshot]] = {}
    try:
        async with asyncio.timeout(timeout_s):
            selected = await read_host_membership_inventory(
                hosts, conn, conversations, timeout_s=timeout_s
            )
            if selected is None:
                return None
            members = iter(selected.runners)

            async def read_runners() -> None:
                for member in members:
                    if (
                        member.host_owner is None
                        or member.host_owner.stop_requested
                        or not member.session_ids
                        or member.runner_id in selected.host.pending_runner_ids
                        or not router.runner_has_capability(
                            member.runner_id, "resident_inventory_v1"
                        )
                    ):
                        continue
                    try:
                        routed = await asyncio.to_thread(
                            router.client_for_existing_conversation, member.session_ids[0]
                        )
                        if routed is None or routed.runner_id != member.runner_id:
                            continue
                        generation = initializer.generation_for(routed.runner_id, routed.client)
                        read_id = uuid4().hex
                        response = await routed.client.get(
                            "/v1/startup/owner-inventory",
                            params={"read_id": read_id},
                            timeout=timeout_s,
                            follow_redirects=False,
                        )
                        initializer.require_generation(routed.runner_id, routed.client, generation)
                        payload = response.json() if response.status_code == 200 else None
                        if (
                            not isinstance(payload, dict)
                            or payload.pop("read_id", None) != read_id
                        ):
                            continue
                        inventory = _decode_runner_context_inventory(payload)
                        if inventory is not None:
                            reads[member.runner_id] = (routed.client, generation, inventory)
                    except (httpx.HTTPError, ConnectionError, ValueError):
                        continue
                    except OmnigentError as exc:
                        if exc.code not in {
                            ErrorCode.UNAUTHORIZED,
                            ErrorCode.WRONG_REPLICA,
                            ErrorCode.RUNNER_UNAVAILABLE,
                        }:
                            raise

            async with asyncio.TaskGroup() as workers:
                for _ in range(min(_MAX_CONCURRENT_RUNNER_READS, len(selected.runners))):
                    workers.create_task(read_runners())
            final = await read_host_membership_inventory(
                hosts, conn, conversations, timeout_s=timeout_s
            )
            if (
                final is None
                or final != selected
                or (conn.ws, conn.workspace_id, conn.owner, conn.account_generation) != authority
            ):
                return None
            result = []
            for member in final.runners:
                read = reads.get(member.runner_id)
                if read is not None:
                    client, generation, _inventory = read
                    try:
                        initializer.require_generation(member.runner_id, client, generation)
                        if client.is_closed or not router.runner_has_capability(
                            member.runner_id, "resident_inventory_v1"
                        ):
                            read = None
                    except ConnectionError:
                        read = None
                result.append(
                    HostRunnerContextInventory(
                        member,
                        read[1] if read is not None else None,
                        read[2] if read is not None else None,
                    )
                )
            return HostContextInventory(final, tuple(result))
    except (TimeoutError, ConnectionError):
        return None


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


@dataclass(frozen=True)
class ContextScopeStatus:
    """One expected scope's observed coverage for a consumer decision."""

    scope: ScopeKeyV2
    status: Literal["complete", "partial", "unsupported", "unknown"]
    reason: str | None = None


@dataclass(frozen=True)
class ContextInventoryNormalization:
    """Per-scope evidence, never a permission: nothing here releases capacity."""

    scopes: tuple[ContextScopeStatus, ...]
    readiness: Literal["unavailable", "partial", "complete"]
    diagnostics: tuple[str, ...] = ()


def _owning_sources(
    scope: ScopeKeyV2,
    runner_evidence: ContextEvidenceV2 | None,
    host_evidence: ContextEvidenceV2 | None,
) -> list[tuple[str, ContextEvidenceV2]]:
    """Each scope is read from its own hop; another hop's claim is not evidence."""
    claims: list[tuple[str, ContextEvidenceV2]] = []
    for hop, evidence in (("runner", runner_evidence), ("host", host_evidence)):
        if evidence is not None and any(item == scope for item in evidence.expected_scopes):
            claims.append((hop, evidence))
    return claims


def normalize_context_inventory_v2(
    *,
    expected_scopes: tuple[ScopeKeyV2, ...],
    runner_evidence: ContextEvidenceV2 | None,
    host_evidence: ContextEvidenceV2 | None,
    runner_capable: bool,
    host_capable: bool,
    v1_diagnostics: tuple[str, ...] = (),
) -> ContextInventoryNormalization:
    """Select and normalize v2 evidence; v1 evidence stays veto-only diagnostics."""
    diagnostics = tuple(v1_diagnostics)
    if not expected_scopes or len(set(expected_scopes)) != len(expected_scopes):
        return ContextInventoryNormalization((), "unavailable", diagnostics)
    statuses: list[ContextScopeStatus] = []
    for scope in expected_scopes:
        capable = host_capable if scope.namespace == "host" else runner_capable
        if not capable:
            statuses.append(ContextScopeStatus(scope, "unknown", "peer_needs_update"))
            continue
        claims = _owning_sources(scope, runner_evidence, host_evidence)
        if not claims:
            statuses.append(ContextScopeStatus(scope, "unknown", "missing_source"))
            continue
        expected_hop = "host" if scope.namespace == "host" else "runner"
        if len(claims) > 1 or claims[0][0] != expected_hop:
            statuses.append(ContextScopeStatus(scope, "unknown", "conflicting_observers"))
            continue
        _hop, evidence = claims[0]
        source = next(
            (candidate for candidate in evidence.sources if candidate.scope == scope), None
        )
        if source is None:
            statuses.append(ContextScopeStatus(scope, "unknown", "missing_source"))
            continue
        if evidence.authority.session_id != scope.owner_id:
            statuses.append(ContextScopeStatus(scope, "unknown", "foreign_session"))
            continue
        if source.coverage == "complete":
            statuses.append(ContextScopeStatus(scope, "complete"))
        else:
            reason = source.unresolved[0].reason if source.unresolved else None
            statuses.append(ContextScopeStatus(scope, source.coverage, reason))
    observed = {item.status for item in statuses}
    if observed == {"complete"}:
        readiness: Literal["unavailable", "partial", "complete"] = "complete"
    elif observed & {"complete", "partial", "unsupported"}:
        readiness = "partial"
    else:
        readiness = "unavailable"
    return ContextInventoryNormalization(tuple(statuses), readiness, diagnostics)


async def read_runner_context_inventory_v2(
    router: RunnerRouter,
    initializer: RunnerSessionInitializer,
    member: HostRunnerMembership,
    *,
    timeout_s: float = 5.0,
) -> ContextEvidenceV2 | None:
    """Read one runner's aggregate v2 evidence on its existing authenticated client."""
    read = await _prepare_runner_context_inventory_v2(
        router, initializer, member, timeout_s=timeout_s, work=False
    )
    evidence = read.reconcile()
    return evidence if isinstance(evidence, ContextEvidenceV2) else None


@dataclass(frozen=True)
class _PreparedRunnerContextV2:
    reconcile: Callable[[], ContextEvidenceV2 | ContextWorkEvidenceV2 | None]
    wire_bytes: int = 0


def _age_context_v2(evidence: ContextEvidenceV2, elapsed: float) -> ContextEvidenceV2:
    return evidence.model_copy(
        update={
            "sources": tuple(
                source.model_copy(update={"age_upper_bound_s": source.age_upper_bound_s + elapsed})
                if source.age_upper_bound_s is not None
                else source
                for source in evidence.sources
            )
        }
    )


async def _prepare_runner_context_inventory_v2(
    router: RunnerRouter,
    initializer: RunnerSessionInitializer,
    member: HostRunnerMembership,
    *,
    timeout_s: float,
    work: bool,
) -> _PreparedRunnerContextV2:
    missing = _PreparedRunnerContextV2(lambda: None)
    if not 0 < timeout_s <= 5.0 or not member.session_ids:
        return missing
    if not router.runner_has_capability(member.runner_id, CONTEXT_INVENTORY_V2):
        return missing
    session_id = member.session_ids[0]
    started = time.monotonic()
    contract = CONTEXT_WORK_INVENTORY_V2 if work else CONTEXT_INVENTORY_V2
    path = "/v2/startup/context-work-inventory" if work else "/v2/startup/context-inventory"
    try:
        # The tunnel transport ignores httpx's per-phase timeouts, so the whole
        # read (routing, head, body, cleanup) runs under one total deadline.
        async with asyncio.timeout(timeout_s):
            routed = await asyncio.to_thread(router.client_for_existing_conversation, session_id)
            if routed is None or routed.runner_id != member.runner_id:
                return missing
            generation = initializer.generation_for(routed.runner_id, routed.client)
            read_id = uuid4().hex
            async with routed.client.stream(
                "GET",
                path,
                params={"read_id": read_id, "session_id": session_id},
                timeout=timeout_s,
                follow_redirects=False,
            ) as response:
                if (
                    response.status_code != 200
                    or response.headers.get(CONTEXT_INVENTORY_CONTRACT_HEADER) != contract
                ):
                    return missing
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > CONTEXT_INVENTORY_MAX_BYTES:
                        return missing
                    payload.extend(chunk)
            initializer.require_generation(routed.runner_id, routed.client, generation)
            decode = decode_context_work_evidence_v2 if work else decode_context_evidence_v2
            evidence = decode(
                bytes(payload),
                read_id=read_id,
                authority=harness_context_authority_v2(session_id),
                expected_scopes=session_context_scopes_v2(session_id),
            )

            def reconcile() -> ContextEvidenceV2 | ContextWorkEvidenceV2 | None:
                try:
                    initializer.require_generation(routed.runner_id, routed.client, generation)
                except ConnectionError:
                    return None
                if routed.client.is_closed or not router.runner_has_capability(
                    member.runner_id, CONTEXT_INVENTORY_V2
                ):
                    return None
                elapsed = time.monotonic() - started
                if isinstance(evidence, ContextWorkEvidenceV2):
                    return evidence.model_copy(
                        update={"context": _age_context_v2(evidence.context, elapsed)}
                    )
                return _age_context_v2(evidence, elapsed)

            return _PreparedRunnerContextV2(reconcile, len(payload))
    except TimeoutError:
        return missing
    except (httpx.HTTPError, ConnectionError, ValueError):
        return missing
    except OmnigentError as exc:
        if exc.code in {
            ErrorCode.UNAUTHORIZED,
            ErrorCode.WRONG_REPLICA,
            ErrorCode.RUNNER_UNAVAILABLE,
        }:
            return missing
        raise


@dataclass(frozen=True)
class HostSessionWorkReadV2:
    session_id: str
    runner_id: str | None
    runner: ContextWorkEvidenceV2 | None
    host: ContextEvidenceV2 | None
    normalization: ContextInventoryNormalization
    reason: str | None


@dataclass(frozen=True)
class HostSessionWorkInventoryV2:
    membership: HostMembershipInventory
    sessions: tuple[HostSessionWorkReadV2, ...]
    diagnostics: tuple[str, ...]
    assessment: RecoveryAssessment


async def read_host_session_work_inventory_v2(
    hosts: HostRegistry,
    conn: HostConnection,
    conversations: ConversationStore,
    router: RunnerRouter | None,
    initializer: RunnerSessionInitializer | None,
    *,
    timeout_s: float = 5.0,
) -> HostSessionWorkInventoryV2 | None:
    """Union session observations; no combined authority or recovery permission."""
    if router is None or initializer is None or not 0 < timeout_s <= 5.0:
        return None
    owner = (
        conn.ws,
        conn.workspace_id,
        conn.owner,
        conn.account_generation,
        conn.startup_generation,
        conn.hello.connection_id,
    )
    try:
        async with asyncio.timeout(timeout_s):
            ownership = await read_host_context_inventory(
                hosts, conn, conversations, router, initializer, timeout_s=timeout_s
            )
            if ownership is None:
                return None
            selected = ownership.membership
            candidates: list[tuple[HostRunnerMembership | None, str]] = []
            diagnostics = []
            for owner_read in ownership.runners:
                member = owner_read.membership
                ids = set(member.session_ids)
                if member.host_owner is not None and member.host_owner.session_id is not None:
                    ids.add(member.host_owner.session_id)
                inventory = owner_read.inventory
                if inventory is None:
                    diagnostics.append("runner_owner_inventory_unknown")
                else:
                    ids.update(
                        sid
                        for native in inventory.native_backends.owners
                        for sid in native.session_ids
                    )
                    if inventory.harnesses is not None:
                        ids.update(owner.session_id for owner in inventory.harnesses.owners)
                    if inventory.terminals is not None:
                        ids.update(owner.conversation_id for owner in inventory.terminals)
                if not ids:
                    diagnostics.append("runner_without_session")
                candidates.extend((member, sid) for sid in sorted(ids))
            candidates.extend((None, sid) for sid in selected.unbound_session_ids)
            if len(candidates) > _MAX_HOST_CONTEXT_SESSIONS:
                return None
            prepared = {}
            host_reads = {}
            reasons = {}
            wire_bytes = 0
            budget_exhausted = False
            pending = iter(candidates)

            async def read_sessions() -> None:
                nonlocal wire_bytes, budget_exhausted
                for member, sid in pending:
                    if budget_exhausted:
                        return
                    key = (member.runner_id if member else None, sid)
                    reason = None
                    if member is None:
                        reason = "unbound_session"
                    elif member.host_owner is None:
                        reason = "host_owner_missing"
                    elif member.host_owner.stop_requested:
                        reason = "runner_stop_requested"
                    elif sid not in member.session_ids:
                        reason = (
                            "host_only_session"
                            if sid == member.host_owner.session_id
                            else "runner_only_session"
                        )
                    elif member.runner_id in selected.host.pending_runner_ids:
                        reason = "runner_launch_pending"
                    elif not router.runner_has_capability(member.runner_id, CONTEXT_INVENTORY_V2):
                        reason = "peer_needs_update"
                    reasons[key] = reason
                    if reason is not None or member is None:
                        continue
                    read = await _prepare_runner_context_inventory_v2(
                        router,
                        initializer,
                        replace(member, session_ids=(sid,)),
                        timeout_s=timeout_s,
                        work=True,
                    )
                    wire_bytes += read.wire_bytes
                    if wire_bytes > _MAX_HOST_CONTEXT_BYTES:
                        budget_exhausted = True
                        return
                    prepared[key] = read
                    started = time.monotonic()
                    host_evidence = await read_host_context_inventory_v2(
                        hosts, conn, sid, timeout_s=timeout_s
                    )
                    if host_evidence is not None:
                        wire_bytes += len(host_evidence.model_dump_json().encode())
                        if wire_bytes > _MAX_HOST_CONTEXT_BYTES:
                            budget_exhausted = True
                            return
                    host_reads[key] = (host_evidence, started)

            async with asyncio.TaskGroup() as workers:
                for _ in range(min(_MAX_CONCURRENT_RUNNER_READS, len(candidates))):
                    workers.create_task(read_sessions())
            if budget_exhausted:
                return None
            population_reads = []
            for owner_read in ownership.runners:
                if owner_read.runner_generation is not None:
                    member = owner_read.membership
                    try:
                        routed = await asyncio.to_thread(
                            router.client_for_existing_conversation, member.session_ids[0]
                        )
                    except OmnigentError as exc:
                        if exc.code in {
                            ErrorCode.UNAUTHORIZED,
                            ErrorCode.WRONG_REPLICA,
                            ErrorCode.RUNNER_UNAVAILABLE,
                        }:
                            return None
                        raise
                    if routed is None or routed.runner_id != member.runner_id:
                        return None
                    population_reads.append(
                        (member.runner_id, routed.client, owner_read.runner_generation)
                    )
            final = await read_host_membership_inventory(
                hosts, conn, conversations, timeout_s=timeout_s
            )
            if (
                final is None
                or final != selected
                or owner
                != (
                    conn.ws,
                    conn.workspace_id,
                    conn.owner,
                    conn.account_generation,
                    conn.startup_generation,
                    conn.hello.connection_id,
                )
            ):
                return None
            for runner_id, client, generation in population_reads:
                try:
                    initializer.require_generation(runner_id, client, generation)
                    if client.is_closed:
                        diagnostics.append("runner_owner_inventory_changed")
                except ConnectionError:
                    diagnostics.append("runner_owner_inventory_changed")
            sessions = []
            working = False
            for member, sid in candidates:
                key = (member.runner_id if member else None, sid)
                read = prepared.get(key)
                evidence = read.reconcile() if read is not None else None
                runner = evidence if isinstance(evidence, ContextWorkEvidenceV2) else None
                host_read = host_reads.get(key)
                host = (
                    _age_context_v2(host_read[0], time.monotonic() - host_read[1])
                    if host_read is not None and host_read[0] is not None
                    else None
                )
                normalization = normalize_context_inventory_v2(
                    expected_scopes=(*session_context_scopes_v2(sid), host_context_scope_v2(sid)),
                    runner_evidence=runner.context if runner is not None else None,
                    host_evidence=host,
                    runner_capable=member is not None
                    and router.runner_has_capability(member.runner_id, CONTEXT_INVENTORY_V2),
                    host_capable=CONTEXT_INVENTORY_V2 in conn.hello.capabilities,
                )
                sessions.append(
                    HostSessionWorkReadV2(
                        sid,
                        key[0],
                        runner,
                        host,
                        normalization,
                        reasons[key] or ("runner_read_unavailable" if runner is None else None),
                    )
                )
                if runner is not None:
                    working |= any(
                        row.live_turn
                        or row.live_tool
                        or row.background_job
                        or row.protected_interaction
                        for row in runner.inventory.observations
                    )
            return HostSessionWorkInventoryV2(
                final,
                tuple(sessions),
                tuple(diagnostics),
                RecoveryAssessment(
                    False, "other_session_working" if working else "inventory_incomplete"
                ),
            )
    except (TimeoutError, ConnectionError):
        return None


def _host_context_authority(conn: HostConnection, session_id: str) -> ContextAuthorityV2:
    return ContextAuthorityV2(
        session_id=session_id,
        account_id=None,
        host_id=conn.host_id,
        host_generation=conn.startup_generation,
        runner_id=None,
        runner_generation=None,
        connection_generation=conn.hello.connection_id,
    )


async def read_host_context_inventory_v2(
    hosts: HostRegistry,
    conn: HostConnection,
    session_id: str,
    *,
    timeout_s: float = 5.0,
) -> ContextEvidenceV2 | None:
    """Read the host's own scope; it owns membership, never vendor contexts."""
    connection_id = conn.hello.connection_id
    ws = conn.ws
    owner = (conn.workspace_id, conn.owner, conn.account_generation, conn.startup_generation)
    if (
        not 0 < timeout_s <= 5.0
        or not connection_id
        or CONTEXT_INVENTORY_V2 not in conn.hello.capabilities
        or hosts.get(conn.host_id, workspace_id=conn.workspace_id) is not conn
    ):
        return None
    request_id, read_id = uuid4().hex, uuid4().hex
    future: asyncio.Future[HostContextInventoryV2ResultFrame] = (
        asyncio.get_running_loop().create_future()
    )
    conn.pending_context_inventory[request_id] = future
    try:
        async with asyncio.timeout(timeout_s):
            hosts.send_text(
                conn,
                encode_host_frame(
                    HostContextInventoryV2Frame(
                        request_id, connection_id, owner[3], read_id, session_id
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
            or result.read_id != read_id
            or result.session_id != session_id
            or result.evidence is None
        ):
            return None
        payload = json.dumps(result.evidence, separators=(",", ":")).encode()
        return decode_context_evidence_v2(
            payload,
            read_id=read_id,
            authority=_host_context_authority(conn, session_id),
            expected_scopes=(host_context_scope_v2(session_id),),
        )
    except (ConnectionError, TimeoutError, ValueError):
        return None
    finally:
        conn.pending_context_inventory.pop(request_id, None)
        if not future.done():
            future.cancel()
        elif not future.cancelled():
            future.exception()
