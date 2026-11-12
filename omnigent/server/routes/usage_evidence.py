"""Authenticated source evidence intake and reads."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.usage_evidence import UsageEvidenceService
from omnigent.usage.evidence import Account, EvidenceBody, Severity, parse_cloudevents
from omnigent.usage.ranges import RangeKey


def create_usage_evidence_router(
    service: UsageEvidenceService,
    auth_provider: AuthProvider | None,
    *,
    enabled: bool = True,
    body_limit: int = 1024 * 1024,
) -> APIRouter:
    router = APIRouter()

    def owner(request: Request) -> str:
        if not enabled:
            raise HTTPException(404, "Not found")
        if auth_provider is not None:
            user = auth_provider.get_user_id(request)
            if user is None and getattr(request.state, "api_key_scope_denied", False):
                raise HTTPException(403, "Usage scope required")
            if user is not None:
                return user
        return require_user(request, auth_provider) or RESERVED_USER_LOCAL

    async def body(request: Request) -> bytes:
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > min(body_limit, 1024 * 1024):
                raise HTTPException(422, "Evidence body exceeds size limit")
            data.extend(chunk)
        return bytes(data)

    async def call(function: Any, *args: Any, **kwargs: Any) -> JSONResponse:
        try:
            result = await asyncio.to_thread(function, *args, **kwargs)
        except ValueError:
            raise HTTPException(422, "Invalid usage evidence") from None
        except (SQLAlchemyError, RuntimeError):
            raise HTTPException(503, "Usage storage unavailable; retry later") from None
        return JSONResponse(result, headers={"Cache-Control": "private, no-store"})

    @router.put("/me/usage/evidence")
    async def evidence(request: Request) -> JSONResponse:
        user = owner(request)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "Expected application/json")
        try:
            parsed = EvidenceBody.model_validate_json(await body(request))
        except (ValidationError, ValueError, RecursionError):
            raise HTTPException(422, "Invalid evidence envelope") from None
        return await call(service.report_sources, user, parsed)

    @router.post("/me/usage/events")
    async def events(request: Request) -> JSONResponse:
        user = owner(request)
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        try:
            parsed = parse_cloudevents(await body(request), media)
        except (ValueError, RecursionError):
            raise HTTPException(422, "Invalid CloudEvents envelope") from None
        return await call(service.report_events, user, parsed)

    @router.get("/me/usage/speed")
    async def speed(
        request: Request,
        range_key: Annotated[RangeKey, Query(alias="range")] = "h24",
        tz: str = "UTC",
        account: Account | None = None,
        model: Annotated[str | None, Query(max_length=128)] = None,
        by: Literal["model", "account"] = "model",
        from_date: Annotated[date | None, Query(alias="from")] = None,
        to_date: Annotated[date | None, Query(alias="to")] = None,
    ) -> JSONResponse:
        return await call(
            service.read_speed,
            owner(request),
            range_key,
            tz,
            account,
            model,
            by,
            from_date,
            to_date,
        )

    @router.get("/me/usage/sources")
    async def sources(request: Request, account: Account | None = None) -> JSONResponse:
        return await call(service.read_sources, owner(request), account)

    @router.get("/me/usage/events")
    async def read_events(
        request: Request,
        account: Account | None = None,
        source: Annotated[str | None, Query(max_length=128)] = None,
        severity: Severity | None = None,
        before: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> JSONResponse:
        return await call(
            service.read_events, owner(request), account, source, severity, before, limit
        )

    return router
