"""Read-only task board and source reports; every operation is scoped to its caller."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse, Response

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.task_board.models import (
    SOURCE_ID_RE,
    ReportError,
    parse_report,
    utc_text,
    utc_timestamp,
)
from omnigent.stores.task_board_store import ItemQuery, TaskBoardStore


class _TaskBoardRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def report_validation_error(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as error:
                first = error.errors()[0]
                path = ".".join(str(part) for part in first["loc"][1:])
                kind = first["type"]
                code = "bad_type" if kind.endswith(("_type", "_parsing")) else "bad_value"
                return JSONResponse(
                    ReportError(
                        code, path, "Invalid request parameter", params={"path": path}
                    ).payload(),
                    status_code=422,
                    headers={"Cache-Control": "no-store"},
                )

        return report_validation_error


def create_task_board_router(
    store: TaskBoardStore, *, auth_provider: AuthProvider | None
) -> APIRouter:
    router = APIRouter(prefix="/me/tasks", route_class=_TaskBoardRoute)

    @router.put("/sources/{source_id}")
    async def report_tasks(source_id: str, request: Request) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        require_trusted_origin(request)
        if SOURCE_ID_RE.fullmatch(source_id) is None:
            return JSONResponse(
                ReportError("bad_value", "source_id", "Invalid source id").payload(),
                status_code=422,
            )
        now = datetime.now(UTC)
        try:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > store.limits.max_body_bytes:
                    raise ReportError(
                        "body_too_large", "", "Report exceeds the body limit", status=413
                    )
                body.extend(chunk)
            report = parse_report(
                bytes(body), request.headers.get("content-type", ""), store.limits
            )
            outcome = await asyncio.to_thread(store.write, owner, source_id, report, now)
        except ReportError as error:
            if error.status in {413, 422}:
                try:
                    await asyncio.to_thread(store.record_issue, owner, source_id, error, now)
                except ReportError as issue_error:
                    if issue_error.code != "source_limit":
                        raise
            return JSONResponse(
                error.payload(), status_code=error.status, headers={"Cache-Control": "no-store"}
            )
        return JSONResponse(asdict(outcome), headers={"Cache-Control": "no-store"})

    @router.get("")
    async def read_tasks(
        request: Request,
        source: str | None = None,
        group: str | None = None,
        state: Literal["open", "closed", "all"] = "all",
        closed_after: str | None = None,
        closed_before: str | None = None,
        include_gone: Literal["0", "1"] = "0",
        include_history: Literal["0", "1"] = "0",
        cursor: str | None = None,
        limit: int = 1000,
    ) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        try:
            if not 1 <= limit <= 5000:
                raise ReportError("bad_value", "limit", "limit must be between 1 and 5000")
            if cursor is not None and len(cursor) > 4096:
                raise ReportError("too_long", "cursor", "Cursor exceeds the length limit")
            query = ItemQuery(
                source=source,
                group=group,
                state=state,
                closed_after=utc_timestamp(closed_after) if closed_after is not None else None,
                closed_before=utc_timestamp(closed_before) if closed_before is not None else None,
                include_gone=include_gone == "1",
                include_history=include_history == "1",
                cursor=cursor,
                limit=limit,
            )
            entries, next_cursor = await asyncio.to_thread(store.list_items, owner, query)
        except ReportError as error:
            return JSONResponse(
                error.payload(), status_code=error.status, headers={"Cache-Control": "no-store"}
            )
        except ValueError:
            return JSONResponse(
                ReportError("bad_value", "", "Invalid query time").payload(), status_code=422
            )
        payload = {
            "@type": "Group",
            "uid": "omnigent:tasks",
            "updated": utc_text(datetime.now(UTC).timestamp()),
            "entries": entries,
        }
        if next_cursor is not None:
            payload["omnigent.ai:cursor"] = next_cursor
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.get("/sources")
    async def read_sources(request: Request) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        sources = await asyncio.to_thread(store.list_sources, owner)
        return JSONResponse({"sources": sources}, headers={"Cache-Control": "no-store"})

    return router
