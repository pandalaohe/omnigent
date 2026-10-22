"""Owner-authenticated cutover control plane (holds, receipts, release)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.routes.usage_events import _error, _json_body
from omnigent.stores import ConversationStore
from omnigent.usage_export.cutover import UsageCutoverStore
from omnigent.usage_export.store import UsageExportError

HostOwner = Callable[[str, str | None], str | None]


def _store_error(exc: UsageExportError) -> JSONResponse:
    return _error(exc.code, exc.status_code, retry_after=exc.retry_after, detail=exc.detail)


def _unavailable() -> JSONResponse:
    return _error("usage_feed_unavailable", 503, retry_after=5)


def create_usage_cutover_router(
    storage_location: str,
    *,
    auth_provider: AuthProvider | None,
    host_owner: HostOwner | None = None,
    conversation_store: ConversationStore | None = None,
) -> APIRouter:
    """Mount the cutover routes.

    :param storage_location: Storage the conversation store uses; the cutover
        rows live beside the usage feed in the same database.
    :param auth_provider: Auth provider for the owner identity.
    :param host_owner: Resolves a host id to the user who owns it. ``None``
        trusts the caller (auth disabled, single-user mode).
    """
    store = UsageCutoverStore(storage_location)
    router = APIRouter()

    @router.post("/usage-cutover/admissions", dependencies=[Depends(require_json_content_type)])
    async def admit_runner_turn(request: Request) -> JSONResponse:
        require_trusted_origin(request)
        body = await _json_body(request)
        if (
            body is None
            or conversation_store is None
            or not isinstance(body.get("session_id"), str)
        ):
            return _error("usage_cutover_bad_request", 400)
        conv = await asyncio.to_thread(conversation_store.get_conversation, body["session_id"])
        if conv is None or not conv.host_id:
            return _error("session_not_found", 404)
        owner, denied = _authorized(request, conv.host_id)
        if denied is not None:
            return denied
        from omnigent.server.routes.usage import _resolve_session_harness
        from omnigent.server.usage_cutover import admit_turn

        try:
            admission_id = body.get("admission_id")
            if admission_id is None:
                source = body.get("source")
                if source not in {"native", "recovery", "plan"}:
                    return _error("usage_cutover_bad_request", 400)
                admission_id = await asyncio.to_thread(
                    admit_turn,
                    storage_location,
                    conv,
                    source=source,
                    resolve_harness=_resolve_session_harness,
                    owner_id=owner,
                )
            if not isinstance(admission_id, str):
                return _error("usage_cutover_bad_request", 400)
            snapshot = await asyncio.to_thread(store.admission_snapshot, owner, admission_id)
            if snapshot is None or snapshot["session_id"] != conv.id:
                return _error("usage_admission_not_found", 404)
            if body.get("ended") is True:
                await asyncio.to_thread(store.end_admission, owner, admission_id)
            return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})
        except UsageExportError as exc:
            return _store_error(exc)
        except SQLAlchemyError:
            return _unavailable()

    def domain_sessions(request: Request, host_id: str, harness: str) -> dict[str, Any]:
        from omnigent.server.routes.usage import _resolve_session_harness
        from omnigent.server.usage_cutover import feed_harness

        live: list[str] = []
        owners: set[str] = set()
        goal_active = False
        after = None
        if conversation_store is not None:
            while ids := conversation_store.list_host_conversation_ids(
                host_id, after=after, limit=100
            ):
                for session_id in ids:
                    conv = conversation_store.get_conversation(session_id)
                    if (
                        conv is None
                        or conv.archived
                        or feed_harness(conv, _resolve_session_harness) != harness
                    ):
                        continue
                    goal_active |= conv.labels.get("omnigent.goal_state") in {"active", "paused"}
                    registry = getattr(request.app.state, "tunnel_registry", None)
                    if (
                        conv.runner_id
                        and registry is not None
                        and registry.get(conv.runner_id) is not None
                    ):
                        live.append(session_id)
                        authority = conversation_store.get_session_owner_authority(
                            conv.root_conversation_id or conv.id, owner_only=True
                        )
                        if authority is not None:
                            owners.add(authority.user_id)
                after = ids[-1]
        return {
            "live_session_ids": live,
            "multi_owner_domain": len(owners) > 1,
            "goal_active": goal_active,
        }

    def _authorized(request: Request, host_id: str) -> tuple[str, JSONResponse | None]:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        if host_owner is None or auth_provider is None:
            return owner, None
        owner_of_host = host_owner(host_id, None)
        if owner_of_host is None or owner_of_host != owner:
            return owner, _error("forbidden", 403)
        return owner, None

    @router.post("/usage-cutover/holds", dependencies=[Depends(require_json_content_type)])
    async def create_hold(request: Request) -> JSONResponse:
        require_trusted_origin(request)
        body = await _json_body(request)
        if body is None or set(body) != {"host_id", "harness", "generation"}:
            return _error("usage_cutover_bad_request", 400)
        owner, denied = _authorized(request, str(body["host_id"]))
        if denied is not None:
            return denied
        try:
            domain = await asyncio.to_thread(
                domain_sessions, request, str(body["host_id"]), str(body["harness"])
            )
            result = await asyncio.to_thread(
                store.create_hold,
                owner,
                str(body["host_id"]),
                str(body["harness"]),
                body["generation"],
                goal_active=domain["goal_active"],
            )
        except UsageExportError as exc:
            return _store_error(exc)
        except SQLAlchemyError:
            return _unavailable()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/usage-cutover/holds/{host_id}/{harness}")
    async def read_hold(request: Request, host_id: str, harness: str) -> JSONResponse:
        owner, denied = _authorized(request, host_id)
        if denied is not None:
            return denied
        try:
            result = await asyncio.to_thread(store.hold_status, owner, host_id, harness)
            result.update(await asyncio.to_thread(domain_sessions, request, host_id, harness))
        except UsageExportError as exc:
            return _store_error(exc)
        except SQLAlchemyError:
            return _unavailable()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post(
        "/usage-cutover/holds/{host_id}/{harness}/{generation}/receipts",
        dependencies=[Depends(require_json_content_type)],
    )
    async def submit_receipt(
        request: Request, host_id: str, harness: str, generation: str
    ) -> JSONResponse:
        require_trusted_origin(request)
        owner, denied = _authorized(request, host_id)
        if denied is not None:
            return denied
        if not generation.isascii() or not generation.isdigit() or len(generation) > 9:
            return _error("usage_cutover_bad_request", 400)
        body = await _json_body(request, max_bytes=64 * 1024 * 1024)
        if body is None:
            return _error("usage_cutover_bad_request", 400)
        try:
            result = await asyncio.to_thread(
                store.submit_drain, owner, host_id, harness, int(generation), body
            )
        except UsageExportError as exc:
            return _store_error(exc)
        except SQLAlchemyError:
            return _unavailable()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post(
        "/usage-cutover/holds/{host_id}/{harness}/{generation}/release",
        dependencies=[Depends(require_json_content_type)],
    )
    async def release_hold(
        request: Request, host_id: str, harness: str, generation: str
    ) -> JSONResponse:
        require_trusted_origin(request)
        owner, denied = _authorized(request, host_id)
        if denied is not None:
            return denied
        if not generation.isascii() or not generation.isdigit() or len(generation) > 9:
            return _error("usage_cutover_bad_request", 400)
        body = await _json_body(request)
        if body is None:
            return _error("usage_cutover_bad_request", 400)
        consumer_receipt = body.get("consumer_receipt")
        if not isinstance(consumer_receipt, dict):
            return _error("usage_cutover_bad_request", 400)
        rescan = body.get("rescan")
        if not isinstance(rescan, dict):
            rescan = {}
        try:
            if body.get("abandon") is True:
                reason = body.get("reason")
                if not isinstance(reason, str):
                    return _error("usage_cutover_bad_request", 400)
                result = await asyncio.to_thread(
                    store.abandon,
                    owner,
                    host_id,
                    harness,
                    int(generation),
                    consumer_receipt,
                    reason,
                )
            else:
                cutover_at = body.get("cutover_at")
                if cutover_at is not None and not isinstance(cutover_at, str):
                    return _error("usage_cutover_bad_request", 400)
                result = await asyncio.to_thread(
                    store.release,
                    owner,
                    host_id,
                    harness,
                    int(generation),
                    consumer_receipt,
                    cutover_at,
                    rescan,
                )
        except UsageExportError as exc:
            return _store_error(exc)
        except SQLAlchemyError:
            return _unavailable()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/usage-cutover/receipts")
    async def read_receipts(request: Request) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        query = request.query_params
        host_id = query.get("host_id")
        harness = query.get("harness")
        raw_generation = query.get("generation")
        raw_after = query.get("after_seq", "0")
        raw_limit = query.get("limit", "10")
        if (
            not host_id
            or not harness
            or raw_generation is None
            or not raw_generation.isascii()
            or not raw_generation.isdigit()
            or len(raw_generation) > 9
            or not raw_after.isascii()
            or not raw_after.isdigit()
            or len(raw_after) > 18
            or not raw_limit.isascii()
            or not raw_limit.isdigit()
            or len(raw_limit) > 3
        ):
            return _error("usage_cutover_bad_request", 400)
        generation = int(raw_generation)
        after_seq = int(raw_after)
        limit = int(raw_limit)
        if generation < 1 or limit < 1 or limit > 50:
            return _error("usage_cutover_bad_request", 400)
        if host_owner is not None and auth_provider is not None:
            owner_of_host = host_owner(host_id, None)
            if owner_of_host is None or owner_of_host != owner:
                return _error("forbidden", 403)
        try:
            page = await asyncio.to_thread(
                store.receipts_page,
                owner,
                host_id,
                harness,
                generation,
                after_seq,
                limit,
            )
        except UsageExportError as exc:
            return _store_error(exc)
        except SQLAlchemyError:
            return _unavailable()
        return JSONResponse(page, headers={"Cache-Control": "no-store"})

    @router.post("/usage-cutover/bindings", dependencies=[Depends(require_json_content_type)])
    async def load_bindings(request: Request) -> JSONResponse:
        require_trusted_origin(request)
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        body = await _json_body(request)
        if body is None:
            return _error("usage_cutover_bad_request", 400)
        document = body.get("document") if set(body) == {"document"} else body
        if not isinstance(document, dict):
            return _error("usage_cutover_bad_request", 400)
        try:
            result = await asyncio.to_thread(store.load_binding_revision, owner, document)
        except UsageExportError as exc:
            return _store_error(exc)
        except SQLAlchemyError:
            return _unavailable()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post(
        "/usage-cutover/bindings/activate", dependencies=[Depends(require_json_content_type)]
    )
    async def activate_bindings(request: Request) -> JSONResponse:
        require_trusted_origin(request)
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        body = await _json_body(request)
        if body is None or set(body) != {"revision", "digest"}:
            return _error("usage_cutover_bad_request", 400)
        try:
            result = await asyncio.to_thread(
                store.activate_binding_revision, owner, body["revision"], body["digest"]
            )
        except UsageExportError as exc:
            return _store_error(exc)
        except SQLAlchemyError:
            return _unavailable()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/usage-cutover/route", dependencies=[Depends(require_json_content_type)])
    async def record_route(request: Request) -> JSONResponse:
        require_trusted_origin(request)
        body: dict[str, Any] | None = await _json_body(request)
        if body is None or set(body) != {
            "host_id",
            "harness",
            "billing_route",
            "route_ref",
        }:
            return _error("usage_cutover_bad_request", 400)
        owner, denied = _authorized(request, str(body["host_id"]))
        if denied is not None:
            return denied
        try:
            result = await asyncio.to_thread(
                store.record_route_attestation,
                owner,
                body["host_id"],
                body["harness"],
                body["billing_route"],
                body["route_ref"],
            )
        except UsageExportError as exc:
            return _store_error(exc)
        except SQLAlchemyError:
            return _unavailable()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/usage-cutover/route")
    async def read_route(request: Request) -> JSONResponse:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        harness = request.query_params.get("harness")
        if not harness:
            return _error("usage_cutover_bad_request", 400)
        try:
            result = await asyncio.to_thread(store.route_attestation, owner, harness)
        except UsageExportError as exc:
            return _store_error(exc)
        except SQLAlchemyError:
            return _unavailable()
        return JSONResponse(result or {}, headers={"Cache-Control": "no-store"})

    return router
