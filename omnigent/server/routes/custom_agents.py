"""Private custom Agent library; launching reuses session-scoped bundle uploads."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from omnigent.db.utils import builtin_agent_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.member_snapshot import frozen_member_lock_label
from omnigent.server.auth import (
    LEVEL_OWNER,
    RESERVED_USER_LOCAL,
    AuthProvider,
    local_single_user_enabled,
)
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.custom_agent_bundles import MAX_BUNDLE_BYTES, patch_bundle, project_members
from omnigent.server.custom_agents_store import CustomAgentsStore
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.spec.validator import _AGENT_NAME_PATTERN
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore

MAX_MULTIPART_REQUEST_BYTES = MAX_BUNDLE_BYTES + 1024 * 1024
_INSTRUCTIONS_CACHE_SIZE = 256


class AgentMember(BaseModel):
    """One member of a joint Agent; the lead is the bundle's root spec.

    ``host_id`` is library-only: it lives in the stored members projection and
    never enters the portable bundle.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=8192)
    harness: str = Field(min_length=1, max_length=128)
    model: str | None = Field(default=None, max_length=512)
    reasoning_effort: str | None = None
    lead: bool
    host_id: str | None = Field(default=None, max_length=256)

    @field_validator("name")
    @classmethod
    def valid_role_name(cls, value: str) -> str:
        # The spec's own agent-name rule: a role is also an archive path segment.
        if not _AGENT_NAME_PATTERN.match(value):
            raise ValueError("name must match [a-zA-Z0-9_-]+ (no dots, slashes, or whitespace)")
        return value


class CustomAgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=8192)
    instructions: str | None = Field(default=None, max_length=262144)
    members: list[AgentMember] | None = None
    version: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str | None) -> str:
        if value is None or not value.strip():
            raise ValueError("name cannot be empty")
        return value.strip()


class CustomAgentImport(BaseModel):
    """Duplicate a source Agent: a session snapshot or a built-in row."""

    model_config = ConfigDict(extra="forbid")
    source_session_id: str | None = Field(default=None, min_length=1, max_length=256)
    source_agent_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def exactly_one_source(self) -> CustomAgentImport:
        # The two sources carry different ownership rules; exactly one must
        # be named so the handler picks a branch without guessing.
        if (self.source_session_id is None) == (self.source_agent_id is None):
            raise ValueError("exactly one of source_session_id or source_agent_id is required")
        return self


def create_custom_agents_router(
    store: CustomAgentsStore,
    artifact_store: ArtifactStore,
    agent_store: AgentStore,
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    host_store: HostStore | None = None,
) -> APIRouter:
    router = APIRouter()
    instructions_cache: OrderedDict[str, str | None] = OrderedDict()
    instructions_cache_lock = threading.Lock()
    cache_miss = object()

    def owner(request: Request) -> str:
        return require_user(request, auth_provider) or RESERVED_USER_LOCAL

    def public(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key != "bundle_location"}

    def validate(data: bytes):
        if len(data) > MAX_BUNDLE_BYTES:
            raise OmnigentError("Agent bundle exceeds 32 MiB", code=ErrorCode.INVALID_INPUT)
        spec = validate_agent_bundle(
            data, enforce_handler_allowlist=not local_single_user_enabled()
        )
        limits = {
            "name": (spec.name, 256),
            "description": (spec.description, 8192),
            "harness": (spec.executor.harness_kind, 128),
            "model": (spec.executor.model, 512),
            "instructions": (spec.instructions, 262144),
        }
        for field, (value, limit) in limits.items():
            if value is not None and len(value) > limit:
                raise OmnigentError(
                    f"Agent {field} exceeds {limit} characters", code=ErrorCode.INVALID_INPUT
                )
        return spec

    def artifact_bytes(location: str) -> bytes:
        try:
            return artifact_store.get(location)
        except KeyError as exc:
            raise OmnigentError("Custom Agent bundle not found", code=ErrorCode.NOT_FOUND) from exc

    def cache_instructions(location: str, instructions: str | None) -> None:
        with instructions_cache_lock:
            instructions_cache[location] = instructions
            instructions_cache.move_to_end(location)
            while len(instructions_cache) > _INSTRUCTIONS_CACHE_SIZE:
                instructions_cache.popitem(last=False)

    def instructions_for(location: str) -> str | None:
        with instructions_cache_lock:
            cached = instructions_cache.get(location, cache_miss)
            if cached is not cache_miss:
                instructions_cache.move_to_end(location)
                return cached if isinstance(cached, str) else None
        spec = validate(artifact_bytes(location))
        cache_instructions(location, spec.instructions)
        return spec.instructions

    def detail(owner_id: str, row: dict[str, Any]) -> dict[str, Any]:
        if row["members"] is None:
            location = row["bundle_location"]
            spec = validate(artifact_bytes(location))
            cache_instructions(location, spec.instructions)
            row = store.backfill_members(owner_id, row["id"], location, project_members(spec))
        return {
            **public(row),
            "instructions": instructions_for(row["bundle_location"]),
        }

    def hosts_by_role(rows: list[dict[str, Any]] | None) -> dict[str, str | None]:
        """Recover ``{role: host_id}`` from stored or request members."""
        hosts: dict[str, str | None] = {}
        for row in rows or []:
            name = row.get("name")
            if isinstance(name, str):
                hosts[name] = row.get("host_id")
        return hosts

    def merge_member_hosts(
        projected: list[dict[str, Any]], hosts: dict[str, str | None]
    ) -> list[dict[str, Any]]:
        """Fold library-only host ids into a freshly projected roster.

        Hosts merge by role, so a rewrite that keeps a role keeps its host and a
        role that disappeared (or was renamed) drops it.
        """
        merged: list[dict[str, Any]] = []
        for member in projected:
            host_id = hosts.get(str(member["name"]))
            merged.append({**member, "host_id": host_id} if host_id else member)
        return merged

    def scalar_member_hosts(
        stored: list[dict[str, Any]] | None, projected: list[dict[str, Any]]
    ) -> dict[str, str | None]:
        """Recover stored hosts for a scalar PATCH's rebuilt roster.

        Hosts merge by role, but a rename rewrites the lead's role name. The
        lead is the stored member flagged ``lead``, so its host follows the
        renamed role instead of dropping with the old name.
        """
        hosts = hosts_by_role(stored)
        stored_lead = next((member for member in stored or [] if member.get("lead")), None)
        projected_lead = next((member for member in projected if member["lead"]), None)
        if (
            stored_lead is not None
            and stored_lead.get("host_id")
            and projected_lead is not None
            and str(projected_lead["name"]) not in hosts
        ):
            hosts[str(projected_lead["name"])] = stored_lead["host_id"]
        return hosts

    def requested_member_hosts(
        stored: list[dict[str, Any]] | None, requested: list[dict[str, Any]]
    ) -> dict[str, str | None]:
        """Recover ``{role: host_id}`` from a members PATCH roster.

        A member that omits ``host_id`` — a client from before the field
        existed — keeps the stored host for that role; an explicit ``null``
        clears it.
        """
        hosts: dict[str, str | None] = {}
        stored_hosts = hosts_by_role(stored)
        for member in requested:
            name = member.get("name")
            if isinstance(name, str):
                hosts[name] = member["host_id"] if "host_id" in member else stored_hosts.get(name)
        return hosts

    async def validate_member_hosts(user_id: str | None, host_ids: set[str]) -> None:
        """Refuse a host id the owner cannot use; an offline host is accepted."""
        for host_id in sorted(host_ids):
            host = (
                await asyncio.to_thread(host_store.get_host, host_id)
                if host_store is not None
                else None
            )
            if host is None or (user_id is not None and host.user_id != user_id):
                raise OmnigentError(
                    f"unknown host {host_id!r}; pick one of your hosts or the session host",
                    code=ErrorCode.INVALID_INPUT,
                )

    def persist_new(owner_id: str, data: bytes) -> dict[str, Any]:
        spec = validate(data)
        agent_id = f"ca_{uuid.uuid4().hex}"
        location = bundle_location(agent_id, data)
        artifact_store.put(location, data)
        row = store.create(
            owner_id,
            {
                "id": agent_id,
                "name": spec.name,
                "description": spec.description,
                "harness": spec.executor.harness_kind,
                "model": spec.executor.model,
                "members": project_members(spec),
                "bundle_location": location,
            },
        )
        cache_instructions(location, spec.instructions)
        return {**public(row), "instructions": spec.instructions}

    async def multipart_form(request: Request):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_MULTIPART_REQUEST_BYTES:
                    raise HTTPException(413, "Agent bundle exceeds 32 MiB")
            except ValueError:
                pass

        original_receive = request.receive
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await original_receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_MULTIPART_REQUEST_BYTES:
                    raise MultiPartException("Agent bundle exceeds 32 MiB")
            return message

        request._receive = bounded_receive
        try:
            return await request.form(max_files=1, max_fields=0)
        except StarletteHTTPException as exc:
            if exc.detail == "Agent bundle exceeds 32 MiB":
                raise HTTPException(413, exc.detail) from exc
            raise
        finally:
            request._receive = original_receive

    @router.get("/custom-agents")
    async def list_custom_agents(
        request: Request,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        rows = await asyncio.to_thread(store.list, owner(request), limit + 1, offset)
        return {"data": [public(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    @router.post(
        "/custom-agents",
        status_code=201,
        dependencies=[Depends(require_trusted_origin)],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": CustomAgentImport.model_json_schema()},
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["bundle"],
                            "properties": {"bundle": {"type": "string", "format": "binary"}},
                        }
                    },
                },
            }
        },
    )
    async def create_custom_agent(request: Request) -> dict[str, Any]:
        owner_id = owner(request)
        source_session_id: str | None = None
        pre_save_labels: dict[str, str] = {}
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type == "multipart/form-data":
            form = await multipart_form(request)
            try:
                upload = form.get("bundle")
                if not isinstance(upload, UploadFile):
                    raise HTTPException(422, "bundle upload is required")
                data = await upload.read(MAX_BUNDLE_BYTES + 1)
            finally:
                await form.close()
        elif media_type == "application/json":
            try:
                body = CustomAgentImport.model_validate(await request.json())
            except (ValueError, ValidationError) as exc:
                raise HTTPException(
                    422, "exactly one of source_session_id or source_agent_id is required"
                ) from exc
            if body.source_session_id is not None:
                user_id = require_user(request, auth_provider)
                if auth_provider is not None and permission_store is None:
                    raise OmnigentError(
                        "Session ownership checks unavailable", code=ErrorCode.FORBIDDEN
                    )
                await require_access(
                    user_id,
                    body.source_session_id,
                    LEVEL_OWNER,
                    permission_store,
                    conversation_store,
                )
                conv = await asyncio.to_thread(
                    conversation_store.get_conversation, body.source_session_id
                )
                pre_save_labels = conv.labels if conv is not None else {}
                agent = (
                    await asyncio.to_thread(agent_store.get, conv.agent_id)
                    if conv and conv.agent_id
                    else None
                )
                if agent is None or agent.session_id is None:
                    raise OmnigentError("Custom session Agent not found", code=ErrorCode.NOT_FOUND)
                data = await asyncio.to_thread(artifact_bytes, agent.bundle_location)
                source_session_id = body.source_session_id
            else:
                # Only seeded built-ins carry the deterministic, name-derived
                # id; session-scoped copies and uploads get random ids.
                assert body.source_agent_id is not None
                source_agent_id = body.source_agent_id
                require_user(request, auth_provider)
                agent = await asyncio.to_thread(agent_store.get, source_agent_id)
                if agent is None or agent.id != builtin_agent_id(agent.name):
                    raise OmnigentError("Built-in Agent not found", code=ErrorCode.NOT_FOUND)
                data = await asyncio.to_thread(artifact_bytes, agent.bundle_location)
        else:
            raise HTTPException(415, "Use application/json or multipart/form-data")
        created = await asyncio.to_thread(persist_new, owner_id, data)
        if source_session_id is not None:
            try:
                await asyncio.to_thread(
                    conversation_store.set_labels,
                    source_session_id,
                    {
                        **frozen_member_lock_label(pre_save_labels),
                        "omnigent:agent-template-id": created["id"],
                    },
                )
            except Exception:
                await asyncio.to_thread(store.delete, owner_id, created["id"])
                raise
        return created

    @router.get("/custom-agents/{agent_id}")
    async def get_custom_agent(request: Request, agent_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(store.get, owner(request), agent_id)
        return await asyncio.to_thread(detail, owner(request), row)

    @router.get(
        "/custom-agents/{agent_id}/contents",
        response_class=Response,
        responses={200: {"content": {"application/gzip": {}, "application/x-tar": {}}}},
    )
    async def get_custom_agent_contents(request: Request, agent_id: str) -> Response:
        row = await asyncio.to_thread(store.get, owner(request), agent_id)
        data = await asyncio.to_thread(artifact_bytes, row["bundle_location"])
        media_type = "application/gzip" if data.startswith(b"\x1f\x8b") else "application/x-tar"
        return Response(data, media_type=media_type, headers={"Cache-Control": "no-store"})

    @router.patch("/custom-agents/{agent_id}", dependencies=[Depends(require_json_content_type)])
    async def patch_custom_agent(
        request: Request, agent_id: str, body: CustomAgentPatch
    ) -> dict[str, Any]:
        owner_id = owner(request)
        user_id = require_user(request, auth_provider)
        row = await asyncio.to_thread(store.get, owner_id, agent_id)
        if body.version is not None and body.version != row["version"]:
            raise OmnigentError(
                "Custom Agent changed; reload before saving", code=ErrorCode.CONFLICT
            )
        if "members" in body.model_fields_set:
            members = body.members or []
            if not members:
                raise OmnigentError(
                    "members must include at least one member", code=ErrorCode.INVALID_INPUT
                )
            if body.version is None:
                raise OmnigentError(
                    "version is required when patching members", code=ErrorCode.INVALID_INPUT
                )
            roles = [member.name for member in members]
            if len(set(roles)) != len(roles):
                raise OmnigentError("member names must be unique", code=ErrorCode.INVALID_INPUT)
            leads = [member for member in members if member.lead]
            if len(leads) != 1:
                raise OmnigentError(
                    "members must include exactly one lead", code=ErrorCode.INVALID_INPUT
                )
            resulting_name = body.name if body.name is not None else row["name"]
            if leads[0].name != resulting_name:
                raise OmnigentError(
                    "lead member name must match the Agent name", code=ErrorCode.INVALID_INPUT
                )
            resulting_description = (
                body.description if "description" in body.model_fields_set else row["description"]
            )
            if leads[0].description != resulting_description:
                raise OmnigentError(
                    "lead member description must match the Agent description",
                    code=ErrorCode.INVALID_INPUT,
                )
            await validate_member_hosts(
                user_id, {member.host_id for member in members if member.host_id}
            )
        changes = body.model_dump(exclude_unset=True, exclude={"version"})
        if not changes:
            return await asyncio.to_thread(detail, owner_id, row)
        request_members: list[dict[str, Any]] | None = changes.get("members")
        if request_members is not None:
            # The host id is library-only; the bundle's member rewrite must
            # never see it.
            changes["members"] = [
                {key: value for key, value in member.items() if key != "host_id"}
                for member in request_members
            ]

        def update() -> dict[str, Any]:
            data = patch_bundle(artifact_bytes(row["bundle_location"]), changes)
            spec = validate(data)
            projected = project_members(spec)
            # The lead is projected first; its role must stay unique across the
            # roster, which role-keyed hosts, labels, and routing cannot split.
            lead_name = str(projected[0]["name"])
            if any(str(member["name"]) == lead_name for member in projected[1:]):
                raise OmnigentError(
                    f"lead name {lead_name!r} is already a member role",
                    code=ErrorCode.INVALID_INPUT,
                )
            location = bundle_location(agent_id, data)
            artifact_store.put(location, data)
            if request_members is not None:
                # A members PATCH is a full roster replacement: each request
                # member's host is authoritative, except that an omitted
                # field falls back to the stored host (an explicit null
                # clears it).
                member_hosts = requested_member_hosts(row["members"], request_members)
            else:
                # A scalar PATCH rebuilds the column from the bundle, which
                # never carries hosts, so carry the stored ones over by role.
                member_hosts = scalar_member_hosts(row["members"], projected)
            updated = store.update(
                owner_id,
                agent_id,
                row["version"],
                {
                    "name": spec.name,
                    "description": spec.description,
                    "harness": spec.executor.harness_kind,
                    "model": spec.executor.model,
                    "members": merge_member_hosts(projected, member_hosts),
                    "bundle_location": location,
                },
            )
            cache_instructions(location, spec.instructions)
            return {**public(updated), "instructions": spec.instructions}

        return await asyncio.to_thread(update)

    @router.delete("/custom-agents/{agent_id}", status_code=204)
    async def delete_custom_agent(request: Request, agent_id: str) -> Response:
        await asyncio.to_thread(store.delete, owner(request), agent_id)
        return Response(status_code=204)

    return router
