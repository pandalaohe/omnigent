"""Token-free quota reporting and the session-owner's live quota evidence."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt
from sqlalchemy.exc import StatementError

from omnigent.harness_plugins import native_agents
from omnigent.quota_pace import accept_snapshot
from omnigent.server.agent_quota import AgentQuotaService
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.stores.host_store import HostStore

_Number = Annotated[StrictFloat | StrictInt, Field(allow_inf_nan=False)]
_Period = Annotated[
    str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
]


class QuotaReportWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: _Period
    used_percent: Annotated[_Number, Field(ge=0, le=100)]
    duration_mins: Annotated[StrictInt, Field(gt=0, le=2_628_000)]
    resets_at: Annotated[_Number, Field(gt=0)]


class QuotaReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    captured_at: Annotated[_Number, Field(gt=0)]
    windows: Annotated[list[QuotaReportWindow], Field(max_length=16)]
    pinned_period: _Period | None = None


def create_agent_quota_router(
    service: AgentQuotaService,
    *,
    auth_provider: AuthProvider | None,
    host_store: HostStore | None,
) -> APIRouter:
    router = APIRouter()

    @router.put("/me/agent-quota/{product}", status_code=204)
    async def report_quota(request: Request, product: str, body: QuotaReport) -> Response:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        if product not in {agent.key for agent in native_agents()}:
            raise HTTPException(422, "Unknown coding-agent product")
        now = time.time()
        if not accept_snapshot(body.captured_at, now=now):
            raise HTTPException(422, "captured_at must be within the last hour or 300s ahead")
        labels = [w.label for w in body.windows]
        if len(set(labels)) != len(labels):
            raise HTTPException(422, "Quota window labels must be unique")
        if body.pinned_period is not None and body.pinned_period not in labels:
            raise HTTPException(422, "pinned_period must name a reported window")
        if body.host_id is not None:
            try:
                host = (
                    await asyncio.to_thread(host_store.get_host, body.host_id)
                    if host_store is not None
                    else None
                )
            except (StatementError, ValueError):
                host = None
            if host is None or (auth_provider is not None and host.user_id != owner):
                raise HTTPException(404, "Host not found")
        await asyncio.to_thread(
            service.report, owner, body.host_id, product, body.model_dump(), now=now
        )
        return Response(status_code=204)

    @router.get("/sessions/{session_id}/agent-quota", include_in_schema=False)
    async def session_quota(request: Request, session_id: str) -> Response:
        from fastapi.responses import JSONResponse

        user = require_user(request, auth_provider)
        await require_access(
            user, session_id, LEVEL_READ, service.permissions, service.conversations
        )
        conversation = await asyncio.to_thread(service.conversations.get_conversation, session_id)
        if conversation is None:
            raise HTTPException(404, "Session not found")
        payload = await asyncio.to_thread(service.payload, conversation)
        return JSONResponse(payload, headers={"Cache-Control": "private, no-store"})

    return router
