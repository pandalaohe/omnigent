"""The single write API for the owner's session selection document."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider, UnifiedAuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.session_choice import (
    ChoiceRevisionConflict,
    SessionChoice,
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


def create_session_choice_router(
    store: SqlAlchemyUserPreferencesStore | None, auth_provider: AuthProvider | None
) -> APIRouter:
    router = APIRouter()

    def preferences() -> SqlAlchemyUserPreferencesStore:
        if store is None:
            raise HTTPException(503, "Synced user preferences are unavailable")
        return store

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
