"""Quota reporting and owner-scoped usage readings."""

from __future__ import annotations

import asyncio
import json
import re
import time
import zlib
from copy import deepcopy
from datetime import date, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from google.protobuf.json_format import ParseError
from google.protobuf.message import DecodeError
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, ValidationError
from sqlalchemy.exc import SQLAlchemyError, StatementError

from omnigent.harness_plugins import native_agents
from omnigent.quota_pace import accept_snapshot
from omnigent.server.agent_quota import (
    AgentQuotaService,
    UsageAccountLimitError,
    UsageStorageUnavailableError,
    UsageWriterConflictError,
)
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.server_config import load_server_config
from omnigent.server.usage_price_source import PriceSourceSelection, UsagePriceSource
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
from omnigent.stores.host_store import HostStore
from omnigent.usage.contract import (
    UsageOverview,
    UsageReport,
    UsageReportV2,
    WriteOutcome,
    parse_usage_report,
)
from omnigent.usage.imports import READERS, ImportFormatError, ImportOptions, read_import
from omnigent.usage.otlp import OtlpReceiver, Signal, decode_export, export_response
from omnigent.usage.ranges import RangeKey, range_spans

_Number = Annotated[StrictFloat | StrictInt, Field(allow_inf_nan=False)]
_Period = Annotated[
    str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
]
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_OTLP_CONTENT = {
    "application/x-protobuf": {"schema": {"type": "string", "format": "binary"}},
    "application/json": {"schema": {"type": "object"}},
}
_OTLP_OPENAPI = {
    "requestBody": {"required": True, "content": _OTLP_CONTENT},
    "responses": {
        "200": {"description": "OTLP success or partial success", "content": _OTLP_CONTENT},
        "400": {"description": "Malformed OTLP export"},
        "413": {"description": "Decompressed body too large"},
        "503": {"description": "Storage unavailable; retry later"},
    },
}


async def _usage_report_body(request: Request) -> tuple[Any, dict[str, Any] | None]:
    media = (
        request.headers.get("content-type", "application/json").split(";", 1)[0].strip().lower()
    )
    if media not in {"application/json", "application/cloudevents+json"}:
        raise HTTPException(415, "Expected JSON or a structured CloudEvent")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 1024 * 1024:
            raise HTTPException(413, "Usage report is too large")
        data.extend(chunk)
    payload = json.loads(data)
    event = None
    if media == "application/cloudevents+json":
        if not isinstance(payload, dict):
            raise ValueError("CloudEvent must be an object")
        event = {key: payload.get(key) for key in ("specversion", "id", "source", "type", "time")}
        payload = payload.get("data")
    elif any(key.startswith("ce-") for key in request.headers):
        event = {
            key: request.headers.get(f"ce-{key}")
            for key in ("specversion", "id", "source", "type", "time")
        }
    if event is not None:
        if event["specversion"] != "1.0" or event["type"] != "dev.omnigent.usage.report":
            raise ValueError("Unsupported CloudEvent")
        for key in ("id", "source", "type"):
            value = event[key]
            if not isinstance(value, str) or not value or len(value) > 2048:
                raise ValueError("Missing CloudEvent context")
        if event["time"] is not None:
            if not isinstance(event["time"], str):
                raise ValueError("Invalid CloudEvent time")
            stamp = datetime.fromisoformat(event["time"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("CloudEvent time needs an offset")
        event = {
            key: value
            for key, value in event.items()
            if key != "specversion" and value is not None
        }
        if (
            isinstance(payload, dict)
            and payload.get("schema_version") == 2
            and not payload.get("source")
        ):
            payload["source"] = event["source"][:64]
    return payload, event


def _report_validation_detail(payload: Any, error: ValidationError) -> str:
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        return "Invalid usage report"
    for item in error.errors(include_input=False, include_url=False):
        location = item["loc"]
        if not location:
            continue
        if item["type"] == "too_long" and location[0] in {
            "windows",
            "balances",
            "activity",
            "models",
        }:
            limit = (item.get("ctx") or {}).get("max_length")
            return f"{location[0]} allows at most {limit} entries"
        if (
            location[0] == "activity"
            and len(location) == 2
            and item["msg"].endswith(
                "an activity entry without figures requires coverage: partial"
            )
        ):
            return f"activity[{location[1]}] without figures requires coverage: partial"
    return "Invalid usage report"


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
    price_source: UsagePriceSource | None = None,
    model_catalogs: HostModelCatalogCacheStore | None = None,
    import_max_body_bytes: int = 5 * 1024 * 1024,
) -> APIRouter:
    router = APIRouter()
    otlp_receiver = OtlpReceiver(service)

    @router.post("/me/usage/otlp/v1/metrics", response_class=Response, openapi_extra=_OTLP_OPENAPI)
    async def report_otlp_metrics(request: Request) -> Response:
        return await report_otlp(request, "metrics")

    @router.post("/me/usage/otlp/v1/traces", response_class=Response, openapi_extra=_OTLP_OPENAPI)
    async def report_otlp_traces(request: Request) -> Response:
        return await report_otlp(request, "traces")

    @router.post("/me/usage/otlp/v1/logs", response_class=Response, openapi_extra=_OTLP_OPENAPI)
    async def report_otlp_logs(request: Request) -> Response:
        return await report_otlp(request, "logs")

    async def report_otlp(request: Request, signal: Signal) -> Response:
        require_usage()
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        media = (
            request.headers.get("content-type", "application/x-protobuf")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        if media not in {"application/x-protobuf", "application/json"}:
            raise HTTPException(415, "Expected OTLP protobuf or JSON")
        if signal == "logs":
            return Response(export_response(signal, media), media_type=media)
        encoding = request.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"identity", "gzip"}:
            raise HTTPException(415, "Expected identity or gzip encoding")
        limit = (
            load_server_config()
            .get("usage", {})
            .get("ingest", {})
            .get("max_body_bytes", {})
            .get("otlp", 4 * 1024 * 1024)
        )
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            limit = 4 * 1024 * 1024
        body = bytearray()
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
        try:
            async for chunk in request.stream():
                decoded = (
                    decoder.decompress(chunk, max(1, limit + 1 - len(body))) if decoder else chunk
                )
                body.extend(decoded)
                if len(body) > limit or (decoder is not None and decoder.unconsumed_tail):
                    raise HTTPException(413, "OTLP body exceeds the decompressed size limit")
            if decoder is not None and (not decoder.eof or decoder.unused_data):
                raise ValueError("Invalid gzip stream")
            message = decode_export(bytes(body), media, signal)
        except (DecodeError, ParseError, ValueError, TypeError, AttributeError, zlib.error):
            raise HTTPException(400, "Malformed OTLP export") from None
        try:
            result = await asyncio.to_thread(otlp_receiver.receive, owner, message)
        except (SQLAlchemyError, RuntimeError):
            raise HTTPException(503, "Usage storage unavailable; retry later") from None
        return Response(
            export_response(signal, media, result.rejected, result.reasons), media_type=media
        )

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
        except UsageStorageUnavailableError:
            raise HTTPException(503, "Usage storage unavailable; retry later") from None
        except (UsageAccountLimitError, UsageWriterConflictError) as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post(
        "/me/usage/accounts/{account_id}/import",
        response_model=WriteOutcome,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "description": "Unmodified vendor response for the selected format",
                        }
                    }
                },
            }
        },
    )
    async def import_usage_account(
        request: Request,
        account_id: str,
        format: str,
        observed_at: str | None = None,
        units_per_usd: str = "500000",
        correction: str = "0",
    ) -> WriteOutcome:
        require_usage()
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        if _ACCOUNT_ID.fullmatch(account_id) is None:
            raise HTTPException(422, "Invalid usage account id")
        if format not in READERS:
            raise HTTPException(404, "Unknown usage import format")
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            raise HTTPException(415, "Expected application/json")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > import_max_body_bytes:
                raise HTTPException(413, "Usage import is too large")
            data.extend(chunk)
        now = time.time()
        try:
            if correction not in {"0", "1"}:
                raise ImportFormatError("correction must be 0 or 1")
            try:
                options = ImportOptions(
                    observed_at=None if observed_at is None else float(observed_at),
                    units_per_usd=float(units_per_usd),
                    correction=correction == "1",
                )
            except ValueError:
                raise ImportFormatError(
                    "Invalid import observation time or units_per_usd"
                ) from None
            body = read_import(format, json.loads(data), options, now)
        except (ValueError, OverflowError, RecursionError) as exc:
            try:
                await asyncio.to_thread(
                    service.note_rejected, owner, account_id, now=now, format=format
                )
            except UsageStorageUnavailableError:
                raise HTTPException(503, "Usage storage unavailable; retry later") from None
            except (UsageAccountLimitError, UsageWriterConflictError) as limit:
                raise HTTPException(409, str(limit)) from None
            detail = str(exc) if isinstance(exc, ImportFormatError) else "Invalid vendor JSON body"
            raise HTTPException(422, detail) from None
        try:
            outcome = await asyncio.to_thread(
                service.report_account, owner, account_id, body, now=now, source=format
            )
        except UsageStorageUnavailableError:
            raise HTTPException(503, "Usage storage unavailable; retry later") from None
        except (UsageAccountLimitError, UsageWriterConflictError) as exc:
            raise HTTPException(409, str(exc)) from None
        assert outcome is not None
        return outcome

    @router.put(
        "/me/usage/accounts/{account_id}",
        status_code=204,
        responses={200: {"model": WriteOutcome, "description": "Report v2 write outcome"}},
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "oneOf": [
                                _inline_openapi_schema(model.model_json_schema())
                                for model in (UsageReport, UsageReportV2)
                            ]
                        }
                    },
                    "application/cloudevents+json": {
                        "schema": {
                            "type": "object",
                            "required": ["specversion", "id", "source", "type", "data"],
                            "properties": {
                                "data": {
                                    "oneOf": [
                                        _inline_openapi_schema(model.model_json_schema())
                                        for model in (UsageReport, UsageReportV2)
                                    ]
                                }
                            },
                        }
                    },
                },
            }
        },
    )
    async def report_usage_account(request: Request, account_id: str) -> Response:
        require_usage()
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        if _ACCOUNT_ID.fullmatch(account_id) is None:
            raise HTTPException(422, "Invalid usage account id")
        payload: Any = None
        try:
            payload, event = await _usage_report_body(request)
            body = parse_usage_report(payload)
        except ValidationError as exc:
            await reject_report(owner, account_id)
            raise HTTPException(422, _report_validation_detail(payload, exc)) from None
        except ValueError:
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
            outcome = await asyncio.to_thread(
                service.report_account, owner, account_id, body, now=now, event=event
            )
        except UsageStorageUnavailableError:
            raise HTTPException(503, "Usage storage unavailable; retry later") from None
        except (UsageAccountLimitError, UsageWriterConflictError) as exc:
            raise HTTPException(409, str(exc)) from None
        if outcome is not None:
            return JSONResponse(outcome.model_dump(mode="json"))
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
        if issues and price_source is not None and price_source.failures:
            span, _ = range_spans(
                range_key, time.time(), ZoneInfo(tz), from_date=from_date, to_date=to_date
            )
            selected_failures = [
                entry
                for entry in price_source.failures
                if span.counted_from <= entry["at"] < span.until
            ]
            if selected_failures:
                payload["price_source"] = selected_failures
        return JSONResponse(payload, headers={"Cache-Control": "private, no-store"})

    @router.get("/me/usage/price-source")
    async def read_price_source(request: Request) -> dict[str, Any]:
        require_usage()
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        if price_source is None:
            raise HTTPException(503, "Price source is unavailable")
        can_edit = (
            auth_provider is None
            or service.permissions is None
            or await asyncio.to_thread(service.permissions.is_admin, owner)
        )
        return {**price_source.status(), "can_edit": can_edit}

    @router.put("/me/usage/price-source")
    async def write_price_source(request: Request, body: PriceSourceSelection) -> dict[str, Any]:
        require_usage()
        from omnigent.server.routes.default_policies import _require_admin

        await _require_admin(
            request, auth_provider, service.permissions if auth_provider is not None else None
        )
        if price_source is None:
            raise HTTPException(503, "Price source is unavailable")
        try:
            return {**await price_source.select(body), "can_edit": True}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except (OSError, SQLAlchemyError):
            raise HTTPException(503, "Price source could not be saved") from None

    @router.get("/me/usage/models")
    async def read_models(
        request: Request,
        account: str,
        range_key: Annotated[RangeKey, Query(alias="range")] = "d7",
        from_date: Annotated[date | None, Query(alias="from")] = None,
        to_date: Annotated[date | None, Query(alias="to")] = None,
        tz: str = "UTC",
        host_id: str | None = None,
    ) -> dict[str, Any]:
        require_usage()
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        from omnigent.server.usage_models import usage_models

        try:
            return await asyncio.to_thread(
                usage_models,
                service,
                model_catalogs,
                owner,
                account,
                host_id=host_id,
                range_key=range_key,
                from_date=from_date,
                to_date=to_date,
                tz=tz,
            )
        except (ValueError, ZoneInfoNotFoundError):
            raise HTTPException(422, "Invalid usage range or timezone") from None
        except LookupError:
            raise HTTPException(404, "Usage account not found") from None

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
