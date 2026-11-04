"""The single write API for the owner's session selection document."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider, UnifiedAuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.session_choice import (
    ChoiceRevisionConflict,
    SessionChoice,
    active_on,
    floor_off_active,
    parse_session_choice,
    state_for,
)
from omnigent.server.user_preferences_store import (
    SqlAlchemyUserPreferencesStore,
    UserPreferencesUserNotFoundError,
    UserPreferencesValidationError,
)


class SessionChoiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: dict[str, Any] | None
    expected_revision: StrictStr


class ChoicePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host_id: str
    work_type: str | None = None
    caller_id: str | None = None
    pin: str | None = None
    exclude: list[str] = []
    differ_from: str | None = None


def create_session_choice_router(
    store: SqlAlchemyUserPreferencesStore | None, auth_provider: AuthProvider | None
) -> APIRouter:
    router = APIRouter()

    def preferences() -> SqlAlchemyUserPreferencesStore:
        if store is None:
            raise HTTPException(503, "Synced user preferences are unavailable")
        return store

    async def owned_host(request: Request, host_id: str) -> str:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        hosts = getattr(request.app.state, "host_store", None)
        host = await asyncio.to_thread(hosts.get_host, host_id) if hosts else None
        if host is None or (auth_provider is not None and host.user_id != owner):
            raise HTTPException(404, "Host not found")
        return owner

    @router.post("/me/session-choice/preview")
    async def preview(request: Request, body: ChoicePreview) -> dict[str, Any]:
        from omnigent.server.session_choice_runtime import context_request, inputs
        from omnigent.server.session_collab import collab_owner_for
        from omnigent.server.user_preferences_store import read_collab_settings

        owner = await owned_host(request, body.host_id)
        settings = (
            await asyncio.to_thread(read_collab_settings, preferences(), owner)
        ).session_choice
        if not active_on(settings, body.host_id):
            return {"enabled": False}
        caller = (
            await asyncio.to_thread(
                request.app.state.conversation_store.get_conversation, body.caller_id
            )
            if body.caller_id
            else None
        )
        if (
            caller
            and await asyncio.to_thread(
                collab_owner_for,
                caller,
                request.app.state.conversation_store,
                request.app.state.permission_store,
            )
            != owner
        ):
            raise HTTPException(404, "Session not found")
        choice_request = await asyncio.to_thread(
            context_request,
            request.app,
            owner,
            body.host_id,
            settings,
            caller,
            preview=True,
            pin=body.pin,
            exclude=tuple(body.exclude),
            differ_from=body.differ_from,
        )
        now = time.time()
        readings, running, floors = await asyncio.to_thread(
            inputs, request.app, settings, choice_request, now
        )
        return (
            state_for(request.app)
            .select(settings, body.work_type, choice_request, readings, running, floors, now=now)
            .payload()
        )

    @router.get("/me/session-choice/notice")
    async def notice(
        request: Request, host_id: str, agent_id: str, model: str | None = None
    ) -> dict[str, Any]:
        from omnigent.entities import Conversation
        from omnigent.server.session_choice import pool_of_session
        from omnigent.server.session_choice_runtime import context_request, inputs
        from omnigent.server.user_preferences_store import read_collab_settings

        owner = await owned_host(request, host_id)
        settings = (
            await asyncio.to_thread(read_collab_settings, preferences(), owner)
        ).session_choice
        if not active_on(settings, host_id):
            return {"notice": None}
        context = await asyncio.to_thread(
            context_request, request.app, owner, host_id, settings, None, preview=True
        )
        session = Conversation(
            id="",
            created_at=0,
            updated_at=0,
            root_conversation_id="",
            agent_id=agent_id,
            model_override=model,
        )
        pool = await asyncio.to_thread(
            pool_of_session, session, settings, products=context.products
        )
        readings, _, floors = await asyncio.to_thread(
            inputs, request.app, settings, context, time.time()
        )
        reading = readings.get(pool)
        if reading is None or reading.freshness != "fresh" or reading.state == "ok":
            return {"notice": None}
        mode = (
            " Use up."
            if pool in floors and floors[pool].until == "manual"
            else " Floor off until reset."
            if pool in floors and floor_off_active(floors[pool], reading, time.time())
            else ""
        )
        return {"notice": f"{pool}: {reading.state}.{mode}", "pool": pool}

    @router.get("/me/session-choice")
    async def read(request: Request) -> dict[str, Any]:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        return await asyncio.to_thread(preferences().session_choice, owner)

    @router.put("/me/session-choice", dependencies=[Depends(require_json_content_type)])
    async def update(request: Request, body: SessionChoiceUpdate) -> dict[str, Any]:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        accounts_mode = (
            isinstance(auth_provider, UnifiedAuthProvider) and auth_provider._source == "accounts"
        )
        try:
            result = await asyncio.to_thread(
                preferences().replace_session_choice,
                owner,
                body.settings,
                body.expected_revision,
                create_if_missing=not accounts_mode,
            )
            state_for(request.app).saved(
                owner,
                parse_session_choice(result["settings"])
                if result["settings"]
                else SessionChoice(),
            )
            return result
        except ChoiceRevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValidationError, UserPreferencesValidationError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except UserPreferencesUserNotFoundError as exc:
            raise HTTPException(401, "Account no longer exists") from exc

    return router
