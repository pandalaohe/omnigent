"""Scoped instruction revisions, effective previews and shared compatibility routes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import StatementError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.instruction_layers import (
    INSTRUCTION_SIZE_REMINDER_CHARS,
    InstructionAgent,
    instruction_agents,
)
from omnigent.server.agent_quota import QuotaContext
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.instruction_layers import resolve_instruction_route, route_session_text
from omnigent.server.instruction_preview import preview_instructions
from omnigent.server.routes._auth_helpers import (
    attribution_user,
    get_user_id,
    require_access,
    require_user,
)
from omnigent.server.routes.default_policies import _require_admin
from omnigent.stores.agent_store import AgentStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.global_instructions_store import (
    GlobalInstructionRevision,
    GlobalInstructionsStore,
    InstructionLayerKey,
    InstructionRevisionConflict,
)
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)


class _SaveGlobalInstructionsRequest(BaseModel):
    """Body for ``PUT /v1/global-instructions``.

    :param text: The full replacement text; empty means "off".
    """

    model_config = ConfigDict(extra="forbid")

    text: str


class _SaveInstructionLayerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    agent_key: str = ""
    host_id: str = ""
    text: str
    expected_revision_id: str | None = None


def _layer_response(
    revision: GlobalInstructionRevision,
    *,
    host_removed: bool = False,
    stale_layers: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "scope": revision.scope,
        "agent_key": revision.agent_key,
        "host_id": revision.host_id,
        "text": revision.text,
        "revision_id": revision.id,
        "updated_at": revision.created_at,
        "updated_by": revision.created_by,
        "chars": len(revision.text),
        "host_removed": host_removed,
        "stale_layers": stale_layers or [],
    }


def _key(scope: str, agent_key: str, host_id: str) -> InstructionLayerKey:
    try:
        key = InstructionLayerKey(scope, agent_key, host_id)
    except ValueError as exc:
        raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
    if key.agent_key and key.agent_key not in {a.key for a in instruction_agents()}:
        raise OmnigentError("Unknown coding agent", code=ErrorCode.INVALID_INPUT)
    return key


def _to_response(revision: GlobalInstructionRevision | None) -> dict[str, Any]:
    """Serialize the live revision (or its absence) for the API."""
    return {
        "text": revision.text if revision is not None else "",
        "revision_id": revision.id if revision is not None else None,
        "updated_at": revision.created_at if revision is not None else None,
        "updated_by": revision.created_by if revision is not None else None,
        "max_chars": None,
    }


def _revision_to_response(revision: GlobalInstructionRevision) -> dict[str, Any]:
    """Serialize one revision-history entry."""
    return {
        "id": revision.id,
        "text": revision.text,
        "created_at": revision.created_at,
        "created_by": revision.created_by,
    }


def create_global_instructions_router(
    store: GlobalInstructionsStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    host_store: HostStore | None = None,
    agent_store: AgentStore | None = None,
    conversation_store: ConversationStore | None = None,
    agent_cache: AgentCache | None = None,
) -> APIRouter:
    """Build the global instructions router.

    Read endpoints need authentication in multi-user mode; writes and
    the revision history additionally require admin privileges.

    :param store: The :class:`GlobalInstructionsStore` instance.
    :param auth_provider: Auth provider used to identify the
        requesting user. ``None`` in single-user mode.
    :param permission_store: Permission store used to check admin
        status. ``None`` disables permission enforcement.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def authenticated_user(request: Request) -> str | None:
        user_id = require_user(request, auth_provider)
        if permission_store is not None and user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        return user_id

    async def owned_host(host_id: str, user_id: str | None) -> None:
        if host_store is None:
            raise OmnigentError("Host not found", code=ErrorCode.NOT_FOUND)
        try:
            host = await asyncio.to_thread(host_store.get_host, host_id)
        except (StatementError, ValueError) as exc:
            raise OmnigentError("Host not found", code=ErrorCode.NOT_FOUND) from exc
        if host is None or (permission_store is not None and host.user_id != user_id):
            raise OmnigentError("Host not found", code=ErrorCode.NOT_FOUND)

    async def history_host(key: InstructionLayerKey, user_id: str | None) -> None:
        if host_store is None:
            raise OmnigentError("Host not found", code=ErrorCode.NOT_FOUND)
        try:
            host = await asyncio.to_thread(host_store.get_host, key.host_id)
        except (StatementError, ValueError) as exc:
            raise OmnigentError("Host not found", code=ErrorCode.NOT_FOUND) from exc
        if host is not None:
            if permission_store is not None and host.user_id != user_id:
                raise OmnigentError("Host not found", code=ErrorCode.NOT_FOUND)
            return
        latest = await asyncio.to_thread(
            store.current, scope=key.scope, agent_key=key.agent_key, host_id=key.host_id
        )
        if latest is None or (permission_store is not None and latest.created_by != user_id):
            raise OmnigentError("Host not found", code=ErrorCode.NOT_FOUND)

    async def visible_layers(user_id: str | None) -> list[GlobalInstructionRevision]:
        layers = await asyncio.to_thread(store.list_layers)
        if host_store is None:
            return [r for r in layers if not r.host_id]
        host_ids = sorted({r.host_id for r in layers if r.host_id})
        hosts = await asyncio.gather(
            *(asyncio.to_thread(host_store.get_host, host_id) for host_id in host_ids)
        )
        known = dict(zip(host_ids, hosts, strict=True))
        visible = []
        for revision in layers:
            if not revision.host_id:
                visible.append(revision)
                continue
            host = known[revision.host_id]
            if host is not None:
                if permission_store is None or host.user_id == user_id:
                    visible.append(revision)
            elif permission_store is None or revision.created_by == user_id:
                visible.append(revision)
        return visible

    async def built_in_subject(
        agent_key: str, descriptors: list[InstructionAgent] | None = None
    ) -> tuple[str, str | None, Any]:
        descriptor = next(
            (
                a
                for a in (descriptors if descriptors is not None else instruction_agents())
                if a.key == agent_key
            ),
            None,
        )
        if descriptor is None:
            raise OmnigentError("Unknown coding agent", code=ErrorCode.INVALID_INPUT)
        if agent_store is None or agent_cache is None:
            return descriptor.subject[0], descriptor.subject[1], None

        try:
            row = await asyncio.to_thread(agent_store.get_by_name, descriptor.subject[0])
            if row is None:
                return descriptor.subject[0], descriptor.subject[1], None
            loaded = await asyncio.to_thread(
                agent_cache.load,
                row.id,
                row.bundle_location,
                expand_env=row.operator_authored,
            )
            spec = loaded.spec
            harness = spec.executor.config.get("harness") or spec.executor.type
            return row.name, harness, spec
        except Exception:  # noqa: BLE001 - broken bundles render user layers only
            _logger.warning("instruction-layers/subject-unavailable: agent=%s", agent_key)
            return descriptor.subject[0], descriptor.subject[1], None

    async def current_quota(
        request: Request, user_id: str | None, host_id: str | None, conversation: Any = None
    ) -> str:
        service = getattr(request.app.state, "agent_quota_service", None)
        if service is None:
            return ""
        if conversation is not None:
            return await asyncio.to_thread(service.line, conversation)
        return await asyncio.to_thread(
            service.context_line, QuotaContext(user_id or "local", host_id)
        )

    async def route_sizes(
        user_id: str | None,
        layers: list[GlobalInstructionRevision],
        subjects: list[tuple[str, str | None, Any]] | None = None,
        request: Request | None = None,
        descriptors: list[InstructionAgent] | None = None,
    ) -> list[dict[str, Any]]:
        if host_store is None:
            return []
        hosts = await asyncio.to_thread(host_store.list_hosts, user_id or "local")
        host_ids = {host.host_id for host in hosts}
        # A removed host remains visible to the saving admin until its layer is cleared.
        host_ids.update(r.host_id for r in layers if r.host_id)
        descriptors = descriptors if descriptors is not None else instruction_agents()
        if subjects is None:
            subjects = list(
                await asyncio.gather(*(built_in_subject(a.key, descriptors) for a in descriptors))
            )
        results: list[dict[str, Any]] = []
        for host_id in sorted(host_ids):
            line = await current_quota(request, user_id, host_id) if request else ""
            for descriptor, (name, harness, spec) in zip(descriptors, subjects, strict=True):
                sizes = [
                    preview_instructions(
                        layers,
                        host_id=host_id,
                        agent_key=descriptor.key,
                        spec=spec,
                        name=name,
                        harness=harness,
                        has_parent=child,
                        spawn=bool(spec and spec.spawn),
                        peer=True,
                        quota_line=line,
                    )
                    for child in (False, True)
                ]
                total = max(size["total_chars"] for size in sizes)
                results.append(
                    {
                        "host_id": host_id,
                        "agent_key": descriptor.key,
                        "total_chars": total,
                        "over_budget": total > INSTRUCTION_SIZE_REMINDER_CHARS,
                        "framework_unavailable": spec is None,
                    }
                )
        return results

    async def session_context(user_id: str | None, session_id: str) -> tuple[Any, Any]:
        if conversation_store is None:
            raise OmnigentError("Conversation not found", code=ErrorCode.NOT_FOUND)
        try:
            await require_access(
                user_id, session_id, LEVEL_READ, permission_store, conversation_store
            )
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        except (StatementError, ValueError) as exc:
            raise OmnigentError("Conversation not found", code=ErrorCode.NOT_FOUND) from exc
        if conv is None:
            raise OmnigentError("Conversation not found", code=ErrorCode.NOT_FOUND)
        if conv.host_id:
            await owned_host(conv.host_id, user_id)
        route = await resolve_instruction_route(conv)
        return conv, route

    @router.get("/global-instructions")
    async def get_global_instructions(
        request: Request,
        host_id: str | None = None,
        agent: str | None = None,
        session_id: str | None = None,
        spawn: bool = False,
    ) -> dict[str, Any]:
        """Return the live text and its metadata.

        Requires authentication in multi-user mode.

        :param request: The incoming request, used to extract the user
            identity.
        :returns: ``{"text", "revision_id", "updated_at", "updated_by",
            "max_chars"}``; ``text`` is ``""`` when nothing was saved.
        :raises OmnigentError: 401 if unauthenticated in multi-user
            mode.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and user_id is None:
            raise OmnigentError(
                "Authentication required",
                code=ErrorCode.UNAUTHORIZED,
            )
        if session_id:
            conv, route = await session_context(user_id, session_id)
            text = await asyncio.to_thread(
                route_session_text,
                store,
                route,
                has_parent=conv.parent_conversation_id is not None,
                quota_line=await current_quota(request, user_id, route.host_id, conv),
            )
            result = _to_response(None)
            result["text"] = text or ""
            return result
        if host_id:
            await owned_host(host_id, user_id)
        if agent:
            _key("agent", agent, "")
        if host_id or agent or spawn:
            from omnigent.runtime.instruction_layers import compose_route, compose_session_text

            layers = await asyncio.to_thread(store.latest_layers, host_id, agent)
            route_text = compose_route(layers).text
            mother = await asyncio.to_thread(store.current, scope="mother") if spawn else None
            text = compose_session_text(
                route_text,
                has_parent=False,
                spawn=spawn,
                mother_text=mother.text if mother else "",
                quota_line=await current_quota(request, user_id, host_id) if spawn else "",
            )
            result = _to_response(None)
            result["text"] = text
            return result
        revision = await asyncio.to_thread(store.current)
        return _to_response(revision)

    @router.get("/instruction-layers")
    async def list_instruction_layers(request: Request) -> dict[str, Any]:
        user_id = authenticated_user(request)
        layers = await visible_layers(user_id)
        descriptors = instruction_agents()
        by_key = {(r.scope, r.agent_key, r.host_id): r for r in layers}
        data = []
        for revision in layers:
            host_removed = False
            if revision.host_id and host_store is not None:
                host_removed = (
                    await asyncio.to_thread(host_store.get_host, revision.host_id) is None
                )
            stale: list[str] = []
            if revision.scope == "independent" and revision.text.strip():
                for scope, key, host in (
                    ("shared", "", ""),
                    ("agent", revision.agent_key, ""),
                    ("host", "", revision.host_id),
                ):
                    other = by_key.get((scope, key, host))
                    if other is not None and other.created_us > revision.created_us:
                        stale.append(scope)
            data.append(_layer_response(revision, host_removed=host_removed, stale_layers=stale))
        return {
            "data": data,
            "agents": [a.as_dict() for a in descriptors],
            "order": ["shared", "agent", "host"],
            "route_sizes": await route_sizes(
                user_id, layers, request=request, descriptors=descriptors
            ),
            "size_reminder_chars": INSTRUCTION_SIZE_REMINDER_CHARS,
        }

    @router.put("/instruction-layers")
    async def save_instruction_layer(
        request: Request, body: _SaveInstructionLayerRequest
    ) -> dict[str, Any]:
        user_id = await _require_admin(request, auth_provider, permission_store)
        key = _key(body.scope, body.agent_key, body.host_id)
        if key.host_id:
            await owned_host(key.host_id, user_id)
        before_layers = await visible_layers(user_id)
        descriptors = instruction_agents()
        subjects = list(
            await asyncio.gather(*(built_in_subject(a.key, descriptors) for a in descriptors))
        )
        before_sizes = await route_sizes(
            user_id, before_layers, subjects, request=request, descriptors=descriptors
        )
        try:
            revision = await asyncio.to_thread(
                store.save,
                body.text,
                created_by=attribution_user(user_id),
                scope=key.scope,
                agent_key=key.agent_key,
                host_id=key.host_id,
                expected_revision_id=body.expected_revision_id,
                check_revision="expected_revision_id" in body.model_fields_set,
            )
        except InstructionRevisionConflict as exc:
            raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
        after_layers = [
            r
            for r in before_layers
            if (r.scope, r.agent_key, r.host_id) != (key.scope, key.agent_key, key.host_id)
        ] + [revision]
        before_over = {(s["host_id"], s["agent_key"]) for s in before_sizes if s["over_budget"]}
        warnings = [
            {
                "host_id": size["host_id"],
                "agent_key": size["agent_key"],
                "total_chars": size["total_chars"],
            }
            for size in await route_sizes(
                user_id, after_layers, subjects, request=request, descriptors=descriptors
            )
            if size["over_budget"] and (size["host_id"], size["agent_key"]) not in before_over
        ]
        return {**_layer_response(revision), "over_budget_routes": warnings}

    @router.get("/instruction-layers/revisions")
    async def list_instruction_layer_revisions(
        request: Request,
        scope: str,
        agent_key: str = "",
        host_id: str = "",
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        user_id = await _require_admin(request, auth_provider, permission_store)
        key = _key(scope, agent_key, host_id)
        if key.host_id:
            await history_host(key, user_id)
        revisions = await asyncio.to_thread(
            store.list_revisions,
            scope=key.scope,
            agent_key=key.agent_key,
            host_id=key.host_id,
            limit=limit,
        )
        return {"data": [_revision_to_response(r) for r in revisions]}

    @router.get("/instruction-layers/preview")
    async def preview_instruction_layers(
        request: Request,
        host_id: str | None = None,
        agent_key: str | None = None,
        child: bool = False,
        peer: bool = True,
        spawn: bool | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        user_id = authenticated_user(request)
        conv = None
        if session_id:
            conv, route = await session_context(user_id, session_id)
            host_id, agent_key = route.host_id, route.agent_key
            child = conv.parent_conversation_id is not None
            peer = await request.app.state.runner_session_initializer.resolve_peer_messaging(conv)
            spawn = route.spawn
            spec, harness = route.spec, route.harness
            if conv.sub_agent_name:
                name = conv.sub_agent_name
            elif conv.agent_id and agent_store is not None:
                agent_row = await asyncio.to_thread(agent_store.get, conv.agent_id)
                name = agent_row.name if agent_row is not None else "Session agent"
            else:
                name = "Session agent"
            layers = await asyncio.to_thread(store.list_layers)
        else:
            if not host_id or not agent_key:
                raise OmnigentError(
                    "host_id and agent_key are required", code=ErrorCode.INVALID_INPUT
                )
            _key("independent", agent_key, host_id)
            await owned_host(host_id, user_id)
            name, harness, spec = await built_in_subject(agent_key)
            spawn = bool(spec and spec.spawn) if spawn is None else spawn
            layers = await visible_layers(user_id)
        result = preview_instructions(
            layers,
            host_id=host_id,
            agent_key=agent_key,
            spec=spec,
            name=name,
            harness=harness,
            has_parent=child,
            spawn=bool(spawn),
            peer=peer,
            quota_line=(
                await current_quota(request, user_id, host_id, conv if session_id else None)
                if not child and spawn
                else ""
            ),
        )
        result["route"] = {
            "host_id": host_id,
            "agent_key": agent_key,
            "child": child,
            "peer": peer,
            "spawn": bool(spawn),
        }
        return result

    @router.put("/global-instructions")
    async def save_global_instructions(
        request: Request,
        body: _SaveGlobalInstructionsRequest,
    ) -> dict[str, Any]:
        """Save a new revision and return the live text.

        Requires admin privileges in multi-user mode. Text is saved intact
        without a product size cap. Empty text turns injection off from
        the next session initialization.

        :param request: The incoming request, used to extract the user
            identity.
        :param body: The full replacement text.
        :returns: The same shape as :func:`get_global_instructions`.
        :raises OmnigentError: 401/403 if the user lacks admin privileges.
        """
        user_id = await _require_admin(request, auth_provider, permission_store)
        revision = await asyncio.to_thread(
            store.save,
            body.text,
            created_by=attribution_user(user_id),
        )
        _logger.info(
            "global-instructions/save: user=%s revision_id=%s chars=%d",
            user_id or "(single-user)",
            revision.id,
            len(body.text),
        )
        return _to_response(revision)

    @router.get("/global-instructions/revisions")
    async def list_global_instruction_revisions(
        request: Request,
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        """List saved revisions, newest first.

        Requires admin privileges in multi-user mode.

        :param request: The incoming request, used to extract the user
            identity.
        :param limit: Maximum revisions to return, 1..200.
        :returns: ``{"data": [{"id", "text", "created_at",
            "created_by"}]}``, newest first.
        :raises OmnigentError: 401/403 if the user lacks admin
            privileges.
        """
        await _require_admin(request, auth_provider, permission_store)
        revisions = await asyncio.to_thread(store.list_revisions, limit=limit)
        return {"data": [_revision_to_response(r) for r in revisions]}

    return router
