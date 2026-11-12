"""Read-only task board and source reports; every operation is scoped to its caller."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
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
from omnigent.stores.task_board_store import HistoryQuery, ItemQuery, TaskBoardStore


def _query_time(value: str | None, path: str) -> str | None:
    if value is None:
        return None
    try:
        utc_timestamp(value)
        return value
    except ValueError:
        raise ReportError("bad_value", path, "Invalid query time") from None


class _TaskBoardRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def report_validation_error(request: Request) -> Response:
            try:
                return await handler(request)
            except ReportError as error:
                return JSONResponse(
                    error.payload(),
                    status_code=error.status,
                    headers={"Cache-Control": "no-store"},
                )
            except RequestValidationError as error:
                first = error.errors()[0]
                path = ".".join(str(part) for part in first["loc"][1:])
                kind = first["type"]
                code = (
                    "too_long"
                    if kind.endswith("_too_long")
                    else "bad_type"
                    if kind.endswith(("_type", "_parsing"))
                    else "bad_value"
                )
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
            outcome = await asyncio.to_thread(store.write, owner, source_id, report)
        except ReportError as error:
            if error.status in {413, 422}:
                try:
                    await asyncio.to_thread(
                        store.record_issue, owner, source_id, error, datetime.now(UTC)
                    )
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
        changed_after: str | None = None,
        snapshot: str | None = None,
        source_generation: str | None = None,
        include_gone: Literal["0", "1"] = "0",
        include_history: Literal["0", "1"] = "0",
        cursor: str | None = Query(default=None, max_length=4096),
        limit: int = Query(default=1000, ge=1, le=5000),
    ) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        started = datetime.now(UTC).timestamp()
        query = ItemQuery(
            source=source,
            group=group,
            state=state,
            closed_after=_query_time(closed_after, "closed_after"),
            closed_before=_query_time(closed_before, "closed_before"),
            changed_after=_query_time(changed_after, "changed_after"),
            read_at=started,
            snapshot=snapshot,
            source_generation=source_generation,
            include_gone=include_gone == "1",
            include_history=include_history == "1",
            cursor=cursor,
            limit=limit,
        )
        entries, next_cursor = await asyncio.to_thread(store.list_items, owner, query)
        payload = {
            "@type": "Group",
            "uid": "omnigent:tasks",
            "updated": utc_text(datetime.now(UTC).timestamp()),
            "entries": entries,
        }
        if next_cursor is not None:
            payload["omnigent.ai:cursor"] = next_cursor
        if changed_after is not None and cursor is None:
            payload["omnigent.ai:changedThrough"] = utc_text(started)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.get("/item")
    async def read_item(
        request: Request,
        source: str,
        uid: str,
        snapshot: str | None = None,
        source_generation: str | None = None,
    ) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        item = await asyncio.to_thread(
            store.get_item,
            owner,
            source,
            uid,
            snapshot=snapshot,
            source_generation=source_generation,
        )
        if item is None:
            raise ReportError("not_found", "uid", "Task not found", status=404)
        return JSONResponse(item, headers={"Cache-Control": "no-store"})

    @router.get("/history")
    async def read_history(
        request: Request,
        snapshot: str | None = None,
        source_generation: str | None = None,
        after: str | None = None,
        before: str | None = None,
        source: str | None = None,
        cursor: str | None = Query(default=None, max_length=4096),
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        _query_time(after, "after")
        _query_time(before, "before")
        entries, next_cursor = await asyncio.to_thread(
            store.list_history,
            owner,
            HistoryQuery(
                source=source,
                snapshot=snapshot,
                source_generation=source_generation,
                after=after,
                before=before,
                cursor=cursor,
                limit=limit,
            ),
        )
        payload: dict[str, Any] = {"entries": entries}
        if next_cursor is not None:
            payload["cursor"] = next_cursor
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.get("/closed-counts")
    async def read_closed_counts(
        request: Request,
        snapshot: str | None = None,
        source_generation: str | None = None,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        offset: int = Query(default=0, ge=-1439, le=1439),
        source: str | None = None,
    ) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        days = await asyncio.to_thread(
            store.closed_counts,
            owner,
            after=_query_time(from_, "from"),
            before=_query_time(to, "to"),
            offset=offset,
            source=source,
            snapshot=snapshot,
            source_generation=source_generation,
        )
        return JSONResponse({"days": days}, headers={"Cache-Control": "no-store"})

    @router.get("/sources/{source_id}/display")
    async def read_display(
        source_id: str,
        request: Request,
        snapshot: str | None = None,
        source_generation: str | None = None,
        part: Literal["identity", "future", "closedDays", "accountDays", "releases"] = "identity",
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        cursor: str | None = None,
        limit: int = Query(default=1000, ge=1, le=1000),
    ) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        if cursor and (not snapshot or not source_generation):
            raise ReportError(
                "bad_value", "cursor", "Paged display reads require source and snapshot pins"
            )
        if cursor is not None and len(cursor) > 4096:
            raise ReportError("too_long", "cursor", "Display cursor exceeds limit")
        payload = await asyncio.to_thread(
            store.read_display,
            owner,
            source_id,
            snapshot=snapshot,
            source_generation=source_generation,
            part=part,
            after=from_,
            before=to,
            cursor=cursor,
            limit=limit,
        )
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.delete("/sources/{source_id}", status_code=204)
    async def delete_source(source_id: str, request: Request) -> Response:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        require_trusted_origin(request)
        if not await asyncio.to_thread(store.delete_source, owner, source_id):
            raise ReportError("not_found", "source_id", "Source not found", status=404)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @router.get("/sources")
    async def read_sources(request: Request) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        sources = await asyncio.to_thread(store.list_sources, owner)
        return JSONResponse({"sources": sources}, headers={"Cache-Control": "no-store"})

    return router
