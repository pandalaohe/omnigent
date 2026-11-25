"""Authenticated redacted provider catalogue and administrator mutations."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError

from omnigent.server import model_connections as connections
from omnigent.server.auth import effective_admin
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.default_policies import _require_admin


def create_model_connections_router(
    auth_provider=None, permission_store=None, user_preferences_store=None
) -> APIRouter:
    router = APIRouter()

    async def admin(request: Request) -> None:
        if auth_provider is not None and permission_store is None:
            raise HTTPException(403, "Administrator access unavailable")
        if auth_provider is not None:
            await _require_admin(request, auth_provider, permission_store)

    async def draft(request: Request, *, with_name: bool = False):
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Request body must be a JSON object") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "Request body must be a JSON object")
        name = body.pop("name", None) if with_name else None
        try:
            if with_name:
                if not isinstance(name, str):
                    raise ValueError("Invalid name")
                connections.validate_name(name)
            return name, connections.ConnectionWrite.model_validate(body)
        except (ValidationError, ValueError):
            # No input, exception text or context: these may contain submitted credentials.
            raise HTTPException(422, "Invalid provider configuration") from None

    async def safe_call(function, *args, **kwargs):
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except connections.ConnectionConflict as exc:
            raise HTTPException(409, exc.detail) from None
        except Exception:  # noqa: BLE001 - redact provider and credential failures
            raise HTTPException(422, "Provider configuration unavailable") from None

    @router.get("/model-connections")
    async def listing(request: Request):
        owner = require_user(request, auth_provider)
        is_admin = auth_provider is None or effective_admin(
            permission_store is not None
            and owner is not None
            and await asyncio.to_thread(permission_store.is_admin, owner)
        )
        return {
            "connections": await asyncio.to_thread(
                connections.load_connections, admin=is_admin, store=user_preferences_store
            ),
            "can_manage": is_admin,
        }

    @router.post("/model-connections/test")
    async def probe(request: Request):
        await admin(request)
        name, value = await draft(request, with_name=True)
        return await safe_call(connections.probe_connection, name, value)

    @router.put("/model-connections/{name}")
    async def put(name: str, request: Request, create: bool = False):
        await admin(request)
        _, value = await draft(request)
        return await safe_call(
            connections.upsert_connection, name, value, store=user_preferences_store, create=create
        )

    @router.post("/model-connections/{name}/refresh")
    async def refresh(name: str, request: Request):
        await admin(request)
        return await safe_call(connections.refresh_catalog, name, store=user_preferences_store)

    @router.get("/model-connections/{name}/catalog")
    async def catalog(name: str, request: Request):
        await admin(request)
        return await safe_call(connections.catalog, name)

    @router.delete("/model-connections/{name}", status_code=204)
    async def delete(name: str, request: Request):
        await admin(request)
        await safe_call(connections.delete_connection, name, store=user_preferences_store)
        return Response(status_code=204)

    return router
