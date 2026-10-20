"""Owner-scoped metering feed and authenticated producer admission."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse

from omnigent.entities import Conversation
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.stores import ConversationStore
from omnigent.usage_export.models import UsageObservation
from omnigent.usage_export.store import UsageEventStore, UsageExportError


class _Registration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    producer_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    harness: str = Field(min_length=1, max_length=256)

    @field_validator("producer_id")
    @classmethod
    def canonical_producer(cls, value: str) -> str:
        return _producer_id(value)


def _producer_id(value: str) -> str:
    parsed = uuid.UUID(value)
    if parsed.version != 4:
        raise ValueError("invalid_producer_id")
    return parsed.hex


def _error(
    code: str, status: int, *, retry_after: int | None = None, detail: object = None
) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, min(retry_after, 3600)))
    error = {
        "message": "Usage export request failed.",
        **(detail if isinstance(detail, dict) else {}),
        "code": code,
    }
    return JSONResponse({"error": error}, status_code=status, headers=headers)


async def _json_body(request: Request) -> dict[str, Any] | None:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 32768:
            return None
        data.extend(chunk)
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def create_usage_events_router(
    conversation_store: ConversationStore,
    resolve_harness: Callable[[Conversation], str | None],
    *,
    auth_provider: AuthProvider | None,
) -> APIRouter:
    store = UsageEventStore(conversation_store.storage_location)
    router = APIRouter()

    @router.get("/usage-events")
    async def read_events(request: Request) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        raw_limit = request.query_params.get("limit", "500")
        if not raw_limit.isascii() or not raw_limit.isdigit() or len(raw_limit) > 4:
            return _error("invalid_limit", 422)
        cursor = request.query_params.get("cursor")
        if cursor is not None and len(cursor) > 4096:
            return _error("invalid_cursor", 400)
        try:
            page = await asyncio.to_thread(store.page, owner, cursor=cursor, limit=int(raw_limit))
        except UsageExportError as exc:
            return _error(
                exc.code, exc.status_code, retry_after=exc.retry_after, detail=exc.detail
            )
        except SQLAlchemyError:
            return _error("usage_feed_unavailable", 503, retry_after=5)
        return JSONResponse(page, headers={"Cache-Control": "no-store"})

    @router.post("/usage-producers", dependencies=[Depends(require_json_content_type)])
    async def register_producer(request: Request) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        require_trusted_origin(request)
        body = await _json_body(request)
        try:
            registration = _Registration.model_validate(body)
        except ValidationError:
            return _error("invalid_producer", 422)
        if any(len(value.encode()) > 256 for value in registration.model_dump().values()):
            return _error("invalid_producer", 422)
        try:
            existing = await asyncio.to_thread(store.get_producer, owner, registration.producer_id)
        except UsageExportError as exc:
            return _error(exc.code, exc.status_code, retry_after=exc.retry_after)
        except SQLAlchemyError:
            return _error("usage_feed_unavailable", 503, retry_after=5)
        if existing is not None:
            if (existing["session_id"], existing["harness"]) != (
                registration.session_id,
                registration.harness,
            ):
                return _error("producer_conflict", 409)
            return JSONResponse(existing, headers={"Cache-Control": "no-store"})
        conv = await asyncio.to_thread(
            conversation_store.get_conversation, registration.session_id
        )
        if conv is None:
            return _error("session_not_found", 404)
        authority = await asyncio.to_thread(
            conversation_store.get_session_owner_authority,
            conv.id,
            owner_only=True,
        )
        if authority is None and conv.root_conversation_id != conv.id:
            authority = await asyncio.to_thread(
                conversation_store.get_session_owner_authority,
                conv.root_conversation_id,
                owner_only=True,
            )
        if authority is None or authority.user_id != owner:
            return _error("producer_forbidden", 403)
        actual = resolve_harness(conv)
        if actual is not None:
            actual = {"codex": "codex-sdk", "claude-code-native": "claude-native"}.get(
                actual, actual
            )
        if actual != registration.harness or not conv.agent_id or not conv.host_id:
            return _error("producer_identity_pending", 409)
        binding = {
            "session_id": conv.id,
            "agent_id": conv.agent_id,
            "host_id": conv.host_id,
            "harness": actual,
            "parent_session_id": conv.parent_conversation_id,
        }
        try:
            result = await asyncio.to_thread(
                store.register_producer, owner, registration.producer_id, binding
            )
        except UsageExportError as exc:
            return _error(exc.code, exc.status_code, retry_after=exc.retry_after)
        except SQLAlchemyError:
            return _error("usage_feed_unavailable", 503, retry_after=5)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/usage-observations", dependencies=[Depends(require_json_content_type)])
    async def append_observation(request: Request) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        require_trusted_origin(request)
        body = await _json_body(request)
        if body is None or set(body) != {"producer_id", "observation"}:
            return _error("invalid_observation", 422)
        producer_id = body["producer_id"]
        if not isinstance(producer_id, str) or not 1 <= len(producer_id.encode()) <= 256:
            return _error("invalid_producer", 422)
        try:
            producer_id = _producer_id(producer_id)
        except ValueError:
            return _error("invalid_producer", 422)
        try:
            observation = UsageObservation.model_validate(body["observation"])
        except ValidationError:
            return _error("invalid_observation", 422)
        try:
            binding = await asyncio.to_thread(store.get_producer, owner, producer_id)
        except UsageExportError as exc:
            return _error(exc.code, exc.status_code, retry_after=exc.retry_after)
        except SQLAlchemyError:
            return _error("usage_feed_unavailable", 503, retry_after=5)
        if binding is None:
            return _error("producer_forbidden", 403)
        values = observation.model_dump(mode="json")
        if any(values[key] != value for key, value in binding.items() if key != "producer_id"):
            return _error("producer_identity_conflict", 409)
        try:
            event = await asyncio.to_thread(store.append, owner, observation)
        except UsageExportError as exc:
            return _error(exc.code, exc.status_code, retry_after=exc.retry_after)
        except SQLAlchemyError:
            return _error("usage_feed_unavailable", 503, retry_after=5)
        return JSONResponse({"event_id": event["event_id"]}, headers={"Cache-Control": "no-store"})

    return router
