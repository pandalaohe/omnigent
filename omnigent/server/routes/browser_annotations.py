"""First-party receiving state and narrow browser-annotation session discovery."""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, TypeVar
from urllib.parse import urlsplit

import jwt
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from omnigent.server.annotation_batch import AnnotationBatch, AnnotationFailure
from omnigent.server.annotation_receiver_store import (
    AnnotationReceiverStore,
    InvalidTargetCursor,
    ReceiverConflict,
    ReceiverTargetUnavailable,
)
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.routes._oauth import NO_STORE_HEADERS
from omnigent.server.routes.device_auth import _require_browser_origin
from omnigent.server.startup_inputs import ManagedStartupInputs
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_MULTIPART_BYTES = _MAX_IMAGE_BYTES + 64 * 1024
_WriteResult = TypeVar("_WriteResult")


async def _await_claimed_write(write: Callable[[], _WriteResult]) -> _WriteResult:
    """Finish a committed-reservation write before propagating request cancellation."""
    operation = asyncio.create_task(asyncio.to_thread(write))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
        if not operation.cancelled():
            operation.exception()
        raise


class _AnnotationRoute(APIRoute):
    """Bound multipart parsing and attach exact-origin CORS to error responses."""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").endswith("/screenshots"):
            await super().handle(scope, receive, send)
            return
        headers = Headers(scope=scope)
        configured = getattr(scope["app"].state, "chrome_annotation_origin", None)
        cors = (
            {"Access-Control-Allow-Origin": configured, "Vary": "Origin"}
            if configured and headers.get("origin") == configured
            else {}
        )
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > _MAX_MULTIPART_BYTES:
                    response = JSONResponse(
                        {"error": "image_too_large"}, status_code=413, headers=cors
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse({"error": "invalid_length"}, status_code=400, headers=cors)
                await response(scope, receive, send)
                return
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > _MAX_MULTIPART_BYTES:
                    raise HTTPException(status_code=413, detail="image_too_large")
            return message

        await super().handle(scope, bounded_receive, send)

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def with_cors(request: Request) -> Response:
            try:
                response = await original(request)
            except AnnotationFailure as error:
                response = JSONResponse(
                    {
                        "error": error.code,
                        **({"capture_ids": error.capture_ids} if error.capture_ids else {}),
                    },
                    status_code=error.status,
                )
            except HTTPException as error:
                response = JSONResponse({"error": error.detail}, status_code=error.status_code)
            except RequestValidationError:
                response = JSONResponse({"error": "invalid_request"}, status_code=422)
            configured = getattr(request.app.state, "chrome_annotation_origin", None)
            if configured and request.headers.get("origin") == configured:
                response.headers["Access-Control-Allow-Origin"] = configured
                response.headers["Vary"] = "Origin"
            return response

        return with_cors


class ReceiverUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None
    revision: int = Field(ge=0)


def create_browser_annotations_router(
    auth_provider: UnifiedAuthProvider,
    store: AnnotationReceiverStore,
    device_grant_store: DeviceGrantStore,
    *,
    chrome_origin: str,
    storage_supported: bool,
    conversations: SqlAlchemyConversationStore | None = None,
    artifacts: ArtifactStore | None = None,
    startup_inputs: ManagedStartupInputs | None = None,
    target_live: Callable[[str], bool] | None = None,
) -> APIRouter:
    """Mount only when accounts-mode annotation pairing is configured."""
    config = auth_provider._accounts_config
    assert config is not None
    router = APIRouter(route_class=_AnnotationRoute)
    public_url = urlsplit(config.base_url)
    first_party_origin = f"{public_url.scheme}://{public_url.netloc}"

    def first_party_identity(request: Request) -> tuple[str, str]:
        if request.headers.get("authorization"):
            raise HTTPException(status_code=401)
        token = request.cookies.get(config.session_cookie_name)
        if not token:
            raise HTTPException(status_code=401)
        try:
            claims = jwt.decode(token, config.cookie_secret, algorithms=["HS256"])
        except jwt.InvalidTokenError as error:
            raise HTTPException(status_code=401) from error
        if "scope" in claims or "grant_id" in claims:
            raise HTTPException(status_code=401)
        user_id = auth_provider.get_user_id(request)
        if (
            user_id is None
            or claims.get("sub") != user_id
            or not isinstance(claims.get("account_generation"), str)
        ):
            raise HTTPException(status_code=401)
        return user_id, claims["account_generation"]

    def first_party_user(request: Request) -> str:
        return first_party_identity(request)[0]

    def narrow_identity(request: Request) -> tuple[str, str, str]:
        if request.headers.get("origin") != chrome_origin:
            raise HTTPException(status_code=403)
        bearer = request.headers.get("authorization", "")
        if not bearer.startswith("Bearer "):
            raise HTTPException(status_code=401)
        try:
            claims = jwt.decode(bearer[7:], config.cookie_secret, algorithms=["HS256"])
        except jwt.InvalidTokenError as error:
            raise HTTPException(status_code=401) from error
        user_id = auth_provider.get_user_id(request)
        if (
            user_id is None
            or claims.get("sub") != user_id
            or claims.get("scope") != "annotations:write"
            or not isinstance(claims.get("grant_id"), str)
            or not isinstance(claims.get("account_generation"), str)
            or not device_grant_store.authorize_annotation_request(
                claims["grant_id"], user_id, claims.get("account_generation")
            )
        ):
            raise HTTPException(status_code=401)
        return user_id, claims["grant_id"], claims["account_generation"]

    def narrow_user(request: Request) -> str:
        return narrow_identity(request)[0]

    def require_storage() -> None:
        if not storage_supported:
            raise HTTPException(status_code=503, detail="annotation_storage_unsupported")

    def require_delivery() -> SqlAlchemyConversationStore:
        require_storage()
        if conversations is None or artifacts is None or startup_inputs is None:
            raise HTTPException(status_code=503, detail="annotation_storage_unsupported")
        return conversations

    def live(session_id: str) -> bool:
        return bool(target_live and target_live(session_id))

    def check_id(value: str) -> None:
        if _HEX_ID.fullmatch(value) is None:
            raise HTTPException(status_code=422, detail="invalid_id")

    def validate_image(data: bytes) -> str:
        if not data or len(data) > _MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="image_too_large")
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ("PNG", "JPEG") or image.width * image.height > 16_000_000:
                    raise HTTPException(status_code=422, detail="invalid_image")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                return "image/png" if image.format == "PNG" else "image/jpeg"
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
        ) as error:
            raise HTTPException(status_code=422, detail="invalid_image") from error

    @router.options("/v1/browser-annotations/sessions")
    async def sessions_preflight(request: Request) -> Response:
        if request.headers.get("origin") != chrome_origin:
            return Response(status_code=403)
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": chrome_origin,
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
                "Vary": "Origin",
            },
        )

    @router.get("/v1/browser-annotations/sessions")
    async def list_sessions(
        request: Request,
        query: str = Query(default="", max_length=120),
        cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> Response:
        user_id = narrow_user(request)
        require_storage()
        try:
            page = store.list_targets(user_id, query=query.strip(), cursor=cursor, limit=limit)
        except InvalidTargetCursor as error:
            raise HTTPException(status_code=400, detail="invalid_cursor") from error
        return JSONResponse(
            page,
            headers={
                **NO_STORE_HEADERS,
                "Access-Control-Allow-Origin": chrome_origin,
                "Vary": "Origin",
            },
        )

    @router.options("/v1/browser-annotations/sessions/{session_id}/screenshots")
    @router.options("/v1/browser-annotations/sessions/{session_id}/batches")
    @router.options("/v1/browser-annotations/batches/{batch_id}")
    async def annotation_preflight(request: Request) -> Response:
        if request.headers.get("origin") != chrome_origin:
            return Response(status_code=403)
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": chrome_origin,
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
                "Vary": "Origin",
            },
        )

    @router.post("/v1/browser-annotations/sessions/{session_id}/screenshots")
    async def stage_screenshot(
        request: Request,
        session_id: str,
        file: Annotated[UploadFile, File()],
        batch_id: str = Form(),
        capture_id: str = Form(),
        sha256: str = Form(),
    ) -> Response:
        user_id, grant_id, generation = narrow_identity(request)
        conversation_store = require_delivery()
        check_id(session_id)
        check_id(batch_id)
        check_id(capture_id)
        if _SHA256.fullmatch(sha256) is None:
            raise HTTPException(status_code=422, detail="invalid_sha256")
        content = await file.read(_MAX_IMAGE_BYTES + 1)
        mime_type = validate_image(content)
        if hashlib.sha256(content).hexdigest() != sha256:
            raise HTTPException(status_code=422, detail="screenshot_hash_mismatch")
        assert artifacts is not None
        await asyncio.to_thread(
            conversation_store.cleanup_expired_annotation_staging,
            user_id,
            grant_id,
            artifacts.delete,
        )
        reservation, needs_upload = await asyncio.to_thread(
            conversation_store.reserve_annotation_screenshot,
            user_id,
            grant_id,
            generation,
            session_id,
            batch_id,
            capture_id,
            sha256,
            artifacts.delete,
        )
        if not needs_upload:
            return JSONResponse(reservation, headers=NO_STORE_HEADERS)
        staged = await _await_claimed_write(
            lambda: conversation_store.complete_annotation_screenshot(
                user_id,
                grant_id,
                generation,
                session_id,
                batch_id,
                capture_id,
                sha256,
                str(reservation["file_id"]),
                content,
                mime_type,
                artifacts.put,
            )
        )
        return JSONResponse(staged, headers=NO_STORE_HEADERS)

    @router.post("/v1/browser-annotations/sessions/{session_id}/batches")
    async def send_batch(request: Request, session_id: str, body: AnnotationBatch) -> Response:
        user_id, grant_id, generation = narrow_identity(request)
        conversation_store = require_delivery()
        check_id(session_id)
        receipt, created = await asyncio.to_thread(
            conversation_store.admit_annotation_batch,
            user_id,
            grant_id,
            generation,
            session_id,
            body,
            target_live=live(session_id),
        )
        if created or (body.intent == "retry_delivery" and receipt["dispatch_phase"] == "ready"):
            assert startup_inputs is not None
            await startup_inputs.dispatch_annotation(
                request.app,
                session_id,
                str(receipt["item_id"]),
                user_id,
                grant_id,
                generation,
                body.batch_id,
                target_live=live,
                explicit_retry=body.intent == "retry_delivery",
            )
            receipt = (
                await asyncio.to_thread(
                    conversation_store.get_annotation_receipt, user_id, grant_id, body.batch_id
                )
                or receipt
            )
        return JSONResponse(receipt, headers=NO_STORE_HEADERS)

    @router.get("/v1/browser-annotations/batches/{batch_id}")
    async def own_batch_receipt(request: Request, batch_id: str) -> Response:
        user_id, grant_id, _ = narrow_identity(request)
        conversation_store = require_delivery()
        check_id(batch_id)
        receipt = await asyncio.to_thread(
            conversation_store.get_annotation_receipt, user_id, grant_id, batch_id
        )
        if receipt is None:
            raise HTTPException(status_code=404, detail="not_recorded")
        return JSONResponse(receipt, headers=NO_STORE_HEADERS)

    @router.get("/v1/me/annotation-receipts/{old_grant_id}/{batch_id}")
    async def old_grant_receipt(request: Request, old_grant_id: str, batch_id: str) -> Response:
        user_id, generation = first_party_identity(request)
        conversation_store = require_delivery()
        check_id(batch_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", old_grant_id):
            raise HTTPException(status_code=422, detail="invalid_grant_id")
        receipt = await asyncio.to_thread(
            conversation_store.get_old_annotation_receipt,
            user_id,
            generation,
            old_grant_id,
            batch_id,
        )
        return JSONResponse(receipt, headers=NO_STORE_HEADERS)

    @router.get("/v1/me/annotation-receiver")
    async def get_receiver(request: Request, session_id: str | None = None) -> Response:
        user_id = first_party_user(request)
        require_storage()
        if session_id is not None and not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise HTTPException(status_code=422, detail="invalid_session_id")
        state = store.read(user_id)
        return JSONResponse(
            {
                "session_id": state.session_id,
                "revision": state.revision,
                "set_at": state.set_at,
                "target_editable": store.is_editable(user_id, session_id)
                if session_id is not None
                else None,
            },
            headers=NO_STORE_HEADERS,
        )

    @router.put("/v1/me/annotation-receiver")
    async def set_receiver(request: Request, body: ReceiverUpdate) -> Response:
        _require_browser_origin(request)
        if request.headers.get("origin") != first_party_origin:
            raise HTTPException(status_code=403, detail="untrusted_origin")
        user_id = first_party_user(request)
        require_storage()
        if body.session_id is not None and not re.fullmatch(r"[0-9a-f]{32}", body.session_id):
            raise HTTPException(status_code=422, detail="invalid_session_id")
        try:
            state = store.set(user_id, body.session_id, body.revision)
        except ReceiverConflict as error:
            raise HTTPException(status_code=409, detail="receiver_revision_conflict") from error
        except ReceiverTargetUnavailable as error:
            raise HTTPException(status_code=404, detail="receiver_target_unavailable") from error
        return JSONResponse(
            {"session_id": state.session_id, "revision": state.revision, "set_at": state.set_at},
            headers=NO_STORE_HEADERS,
        )

    return router
