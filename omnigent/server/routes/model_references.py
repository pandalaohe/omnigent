"""Check and repair the authenticated owner's saved model references."""

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from omnigent.models.model_reference import ALIASES_NAMESPACE, alias_pairs
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider, UnifiedAuthProvider
from omnigent.server.model_references import check_model_references, replace_model_references
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.session_choice import choice_revision
from omnigent.server.user_preferences_store import (
    UserPreferencesUserNotFoundError,
    UserPreferencesValidationError,
)


class ReplaceModelReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)
    mode: Literal["model", "provider"]
    source: Annotated[StrictStr, Field(alias="from", min_length=1, max_length=512)]
    target: Annotated[StrictStr, Field(alias="to", min_length=1, max_length=512)]


class ModelProviderAliasesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pairs: Annotated[list[list[Annotated[StrictStr, Field(max_length=256)]]], Field(max_length=64)]
    expected_revision: StrictStr | None = None


def create_model_references_router(auth_provider: AuthProvider | None) -> APIRouter:
    router = APIRouter()

    def accounts_mode() -> bool:
        return (
            isinstance(auth_provider, UnifiedAuthProvider) and auth_provider._source == "accounts"
        )

    @router.get("/me/model-references")
    async def check(request: Request) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        return await check_model_references(
            request.app, user_id or RESERVED_USER_LOCAL, user_id=user_id
        )

    @router.post("/me/model-references/replace", dependencies=[Depends(require_json_content_type)])
    async def replace(request: Request, body: ReplaceModelReferenceRequest) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        try:
            return await replace_model_references(
                request.app,
                user_id or RESERVED_USER_LOCAL,
                user_id=user_id,
                mode=body.mode,
                source=body.source,
                target=body.target,
                create_if_missing=not accounts_mode(),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except UserPreferencesUserNotFoundError as exc:
            raise HTTPException(401, "Account no longer exists") from exc

    @router.put("/me/model-references/aliases", dependencies=[Depends(require_json_content_type)])
    async def aliases(request: Request, body: ModelProviderAliasesRequest) -> dict[str, Any]:
        owner = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        store = getattr(request.app.state, "user_preferences_store", None)
        if store is None:
            raise HTTPException(503, "Synced user preferences are unavailable")
        value = {"pairs": body.pairs}
        try:
            alias_pairs(value)

            def update(current):
                if (
                    body.expected_revision is not None
                    and choice_revision(current or {"pairs": []}) != body.expected_revision
                ):
                    raise HTTPException(409, "Provider renames changed; reload before saving")
                return value

            await asyncio.to_thread(
                store.transform_namespace,
                owner,
                ALIASES_NAMESPACE,
                update,
                create_if_missing=not accounts_mode(),
            )
        except UserPreferencesUserNotFoundError as exc:
            raise HTTPException(401, "Account no longer exists") from exc
        except (ValueError, UserPreferencesValidationError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return value

    return router
