"""First-party receiving state and narrow browser-annotation session discovery."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import jwt
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, Response

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
) -> APIRouter:
    """Mount only when accounts-mode annotation pairing is configured."""
    config = auth_provider._accounts_config
    assert config is not None
    router = APIRouter()
    public_url = urlsplit(config.base_url)
    first_party_origin = f"{public_url.scheme}://{public_url.netloc}"

    def first_party_user(request: Request) -> str:
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
        if user_id is None or claims.get("sub") != user_id:
            raise HTTPException(status_code=401)
        return user_id

    def narrow_user(request: Request) -> str:
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
            or not device_grant_store.authorize_annotation_request(
                claims["grant_id"], user_id, claims.get("account_generation")
            )
        ):
            raise HTTPException(status_code=401)
        return user_id

    def require_storage() -> None:
        if not storage_supported:
            raise HTTPException(status_code=503, detail="annotation_storage_unsupported")

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
