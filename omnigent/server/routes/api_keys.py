"""Browser-only management of the caller's personal integration keys."""

import re
import time
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from omnigent.server.api_key_store import UNSET, ApiKeyNameTaken, UserApiKeyStore
from omnigent.server.auth import API_KEY_SCOPES, UnifiedAuthProvider
from omnigent.server.browser_approval import browser_action_user

NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def create_api_keys_router(
    auth_provider: UnifiedAuthProvider, store: UserApiKeyStore
) -> APIRouter:
    router = APIRouter(prefix="/v1/me/api-keys")

    async def body_fields(
        request: Request, user: str, *, creating: bool = False, regenerating: bool = False
    ) -> dict:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected a JSON object") from None
        allowed = {"expires_at"} if regenerating else {"name", "scopes", "expires_at"}
        if not isinstance(body, dict) or body.keys() - allowed:
            raise HTTPException(400, "Unknown API key fields")
        if creating and not {"name", "scopes"} <= body.keys():
            raise HTTPException(400, "Name and permissions are required")
        if "name" in body:
            name = body["name"]
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) is None
            ):
                raise HTTPException(400, "Use 1–64 letters, numbers, dots, underscores or hyphens")
            if any(key.name == name for key, _ in auth_provider.read_only_key_usage(user)):
                raise HTTPException(409, "API key name already used by a server config entry")
        if "scopes" in body:
            scopes = body["scopes"]
            if (
                not isinstance(scopes, list)
                or not scopes
                or not all(isinstance(scope, str) for scope in scopes)
                or len(set(scopes)) != len(scopes)
                or (scopes != ["*"] and any(scope not in API_KEY_SCOPES for scope in scopes))
            ):
                raise HTTPException(400, "Choose at least one supported permission")
            body["scopes"] = tuple(scopes)
        expiry = body.get("expires_at")
        if expiry is not None and (
            type(expiry) is not int or expiry <= int(time.time()) or expiry > 2147483647
        ):
            raise HTTPException(400, "Expiration must be a future UTC epoch second before 2038")
        return body

    @router.get("")
    async def list_keys(request: Request) -> Response:
        user = browser_action_user(request, auth_provider, mutating=False)
        keys = await run_in_threadpool(store.list_for_user, user)
        return JSONResponse(
            {
                "keys": [asdict(key) for key in keys],
                "read_only": [
                    {
                        "name": key.name,
                        "scopes": key.scopes,
                        "source": key.source,
                        "last_used_at": used,
                    }
                    for key, used in auth_provider.read_only_key_usage(user)
                ],
                "available_scopes": API_KEY_SCOPES,
            },
            headers=NO_STORE_HEADERS,
        )

    @router.post("", status_code=201)
    async def create_key(request: Request) -> Response:
        user = browser_action_user(request, auth_provider, mutating=True)
        fields = await body_fields(request, user, creating=True)
        try:
            key, value = await run_in_threadpool(
                store.create,
                user,
                name=fields["name"],
                scopes=fields["scopes"],
                expires_at=fields.get("expires_at"),
                now=int(time.time()),
            )
        except ApiKeyNameTaken:
            raise HTTPException(409, "API key name already used") from None
        return JSONResponse(
            {"key": asdict(key), "value": value}, status_code=201, headers=NO_STORE_HEADERS
        )

    @router.patch("/{key_id}")
    async def edit_key(key_id: str, request: Request) -> Response:
        user = browser_action_user(request, auth_provider, mutating=True)
        fields = await body_fields(request, user)
        try:
            key = await run_in_threadpool(
                store.update,
                user,
                key_id,
                name=fields.get("name"),
                scopes=fields.get("scopes"),
                expires_at=fields.get("expires_at", UNSET),
            )
        except ApiKeyNameTaken:
            raise HTTPException(409, "API key name already used") from None
        if key is None:
            raise HTTPException(404, "API key not found")
        return JSONResponse({"key": asdict(key)}, headers=NO_STORE_HEADERS)

    @router.post("/{key_id}/regenerate")
    async def regenerate_key(key_id: str, request: Request) -> Response:
        user = browser_action_user(request, auth_provider, mutating=True)
        fields = await body_fields(request, user, regenerating=True)
        result = await run_in_threadpool(
            store.regenerate,
            user,
            key_id,
            expires_at=fields.get("expires_at"),
            now=int(time.time()),
        )
        if result is None:
            raise HTTPException(404, "API key not found")
        key, value = result
        return JSONResponse({"key": asdict(key), "value": value}, headers=NO_STORE_HEADERS)

    @router.delete("/{key_id}", status_code=204)
    async def delete_key(key_id: str, request: Request) -> Response:
        user = browser_action_user(request, auth_provider, mutating=True)
        if not await run_in_threadpool(store.delete, user, key_id):
            raise HTTPException(404, "API key not found")
        return Response(status_code=204, headers=NO_STORE_HEADERS)

    return router
