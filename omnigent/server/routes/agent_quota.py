"""Quota reporting and owner-scoped usage readings."""

from __future__ import annotations

import asyncio
import re
import time
from copy import deepcopy
from datetime import date
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, ValidationError
from sqlalchemy.exc import StatementError

from omnigent.harness_plugins import native_agents
from omnigent.quota_pace import accept_snapshot
from omnigent.server.agent_quota import AgentQuotaService, UsageAccountLimitError
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.stores.host_store import HostStore
from omnigent.usage.contract import UsageOverview, UsageReport
from omnigent.usage.ranges import RangeKey

_Number = Annotated[StrictFloat | StrictInt, Field(allow_inf_nan=False)]
_Period = Annotated[
    str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
]
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def _inline_openapi_schema(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = deepcopy(schema.get("$defs", {}))

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            reference = node.get("$ref")
            if reference is not None:
                name = reference.rsplit("/", 1)[-1]
                siblings = {key: value for key, value in node.items() if key != "$ref"}
                return resolve({**deepcopy(definitions[name]), **siblings})
            return {key: resolve(value) for key, value in node.items() if key != "$defs"}
        if isinstance(node, list):
            return [resolve(value) for value in node]
        return node

    return resolve(schema)


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

    def require_usage() -> None:
        if not service.usage_enabled:
            raise HTTPException(404, "Not found")

    async def reject_report(owner: str, account_id: str) -> None:
        try:
            await asyncio.to_thread(service.note_rejected, owner, account_id)
        except UsageAccountLimitError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.put(
        "/me/usage/accounts/{account_id}",
        status_code=204,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _inline_openapi_schema(UsageReport.model_json_schema())
                    }
                },
            }
        },
    )
    async def report_usage_account(request: Request, account_id: str) -> Response:
        require_usage()
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        if _ACCOUNT_ID.fullmatch(account_id) is None:
            raise HTTPException(422, "Invalid usage account id")
        try:
            payload: Any = await request.json()
            body = UsageReport.model_validate(payload)
        except (ValueError, ValidationError):
            await reject_report(owner, account_id)
            raise HTTPException(422, "Invalid usage report") from None
        now = time.time()
        if not accept_snapshot(body.observed_at, now=now):
            await reject_report(owner, account_id)
            raise HTTPException(422, "observed_at must be within the last hour or 300s ahead")
        products = {agent.key for agent in native_agents()}
        if body.product is not None and body.product not in products:
            await reject_report(owner, account_id)
            raise HTTPException(422, "Unknown coding-agent product")
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
        try:
            await asyncio.to_thread(service.report_account, owner, account_id, body, now=now)
        except UsageAccountLimitError as exc:
            raise HTTPException(409, str(exc)) from None
        return Response(status_code=204)

    @router.get("/me/usage", response_model=UsageOverview)
    async def read_usage(request: Request) -> Response:
        require_usage()
        from fastapi.responses import JSONResponse

        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        overview = await asyncio.to_thread(service.overview, owner)
        return JSONResponse(
            overview.model_dump(mode="json"), headers={"Cache-Control": "private, no-store"}
        )

    @router.delete("/me/usage/accounts/{account_id}", status_code=204)
    async def delete_usage_account(request: Request, account_id: str) -> Response:
        require_usage()
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        if _ACCOUNT_ID.fullmatch(account_id) is None:
            raise HTTPException(422, "Invalid usage account id")
        deleted = await asyncio.to_thread(service.delete_account, owner, account_id)
        if not deleted:
            raise HTTPException(404, "Usage account not found")
        return Response(status_code=204)

    @router.get("/me/usage/activity")
    @router.get("/me/usage/issues")
    async def read_activity(
        request: Request,
        range_key: Annotated[RangeKey, Query(alias="range")] = "d7",
        from_date: Annotated[date | None, Query(alias="from")] = None,
        to_date: Annotated[date | None, Query(alias="to")] = None,
        tz: str = "UTC",
        account: str | None = None,
        include: Literal["models"] | None = None,
    ) -> Response:
        require_usage()
        from fastapi.responses import JSONResponse

        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        issues = request.url.path.endswith("/issues")
        options: dict[str, Any] = {
            "range_key": range_key,
            "from_date": from_date,
            "to_date": to_date,
            "tz": tz,
            "account": account,
            "include_models": issues or include == "models",
        }
        try:
            if issues:
                payload = await asyncio.to_thread(service.issues, owner, **options)
            else:
                payload = await asyncio.to_thread(service.activity, owner, **options)
        except (ValueError, ZoneInfoNotFoundError):
            raise HTTPException(422, "Invalid usage range or timezone") from None
        return JSONResponse(payload, headers={"Cache-Control": "private, no-store"})

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
        payload = await asyncio.to_thread(
            service.payload,
            conversation,
            requester_id=user or RESERVED_USER_LOCAL,
            agent_store=getattr(request.app.state, "agent_store", None),
            agent_cache=getattr(request.app.state, "agent_cache", None),
        )
        return JSONResponse(payload, headers={"Cache-Control": "private, no-store"})

    return router
