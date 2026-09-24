"""Routes for the server-wide global instructions text.

One admin-saved text is appended to the instructions of every
Omnigent-started session. Every save is kept as a revision (the newest
one is live). Reading is open to any authenticated caller; writing and
the revision history require admin privileges in multi-user mode.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import attribution_user, get_user_id
from omnigent.server.routes.default_policies import _require_admin
from omnigent.stores.global_instructions_store import (
    GLOBAL_INSTRUCTIONS_MAX_CHARS,
    GlobalInstructionRevision,
    GlobalInstructionsStore,
)
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)


class _GlobalInstructionsTooLong(OmnigentError):
    """Over-cap saves are rejected as unprocessable (422).

    The cap is a body-validation rule, and this API reserves 422 for
    body-validation rejections — the schema-validation handler attributes
    its 422s as ``invalid_input``. ``invalid_input`` alone maps to 400,
    so the status is overridden here rather than adding a code.
    """

    @property
    def http_status(self) -> int:
        return 422


class _SaveGlobalInstructionsRequest(BaseModel):
    """Body for ``PUT /v1/global-instructions``.

    :param text: The full replacement text; empty means "off".
    """

    model_config = ConfigDict(extra="forbid")

    text: str


def _to_response(revision: GlobalInstructionRevision | None) -> dict[str, Any]:
    """Serialize the live revision (or its absence) for the API."""
    return {
        "text": revision.text if revision is not None else "",
        "revision_id": revision.id if revision is not None else None,
        "updated_at": revision.created_at if revision is not None else None,
        "updated_by": revision.created_by if revision is not None else None,
        "max_chars": GLOBAL_INSTRUCTIONS_MAX_CHARS,
    }


def _revision_to_response(revision: GlobalInstructionRevision) -> dict[str, Any]:
    """Serialize one revision-history entry."""
    return {
        "id": revision.id,
        "text": revision.text,
        "created_at": revision.created_at,
        "created_by": revision.created_by,
    }


def create_global_instructions_router(
    store: GlobalInstructionsStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the global instructions router.

    All routes are scoped to ``/global-instructions``.

    Read endpoints need authentication in multi-user mode; writes and
    the revision history additionally require admin privileges.

    :param store: The :class:`GlobalInstructionsStore` instance.
    :param auth_provider: Auth provider used to identify the
        requesting user. ``None`` in single-user mode.
    :param permission_store: Permission store used to check admin
        status. ``None`` disables permission enforcement.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.get("/global-instructions")
    async def get_global_instructions(request: Request) -> dict[str, Any]:
        """Return the live text and its metadata.

        Requires authentication in multi-user mode.

        :param request: The incoming request, used to extract the user
            identity.
        :returns: ``{"text", "revision_id", "updated_at", "updated_by",
            "max_chars"}``; ``text`` is ``""`` when nothing was saved.
        :raises OmnigentError: 401 if unauthenticated in multi-user
            mode.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and user_id is None:
            raise OmnigentError(
                "Authentication required",
                code=ErrorCode.UNAUTHORIZED,
            )
        revision = await asyncio.to_thread(store.current)
        return _to_response(revision)

    @router.put("/global-instructions")
    async def save_global_instructions(
        request: Request,
        body: _SaveGlobalInstructionsRequest,
    ) -> dict[str, Any]:
        """Save a new revision and return the live text.

        Requires admin privileges in multi-user mode. Over-cap text is
        rejected outright — never truncated, nothing written. Empty text
        is a valid save and turns injection off from the next session
        initialization.

        :param request: The incoming request, used to extract the user
            identity.
        :param body: The full replacement text.
        :returns: The same shape as :func:`get_global_instructions`.
        :raises OmnigentError: 401/403 if the user lacks admin
            privileges, or 422 when the text exceeds the cap.
        """
        user_id = await _require_admin(request, auth_provider, permission_store)
        if len(body.text) > GLOBAL_INSTRUCTIONS_MAX_CHARS:
            raise _GlobalInstructionsTooLong(
                f"Global instructions are limited to "
                f"{GLOBAL_INSTRUCTIONS_MAX_CHARS} characters; got {len(body.text)}",
                code=ErrorCode.INVALID_INPUT,
            )
        revision = await asyncio.to_thread(
            store.save,
            body.text,
            created_by=attribution_user(user_id),
        )
        _logger.info(
            "global-instructions/save: user=%s revision_id=%s chars=%d",
            user_id or "(single-user)",
            revision.id,
            len(body.text),
        )
        return _to_response(revision)

    @router.get("/global-instructions/revisions")
    async def list_global_instruction_revisions(
        request: Request,
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        """List saved revisions, newest first.

        Requires admin privileges in multi-user mode.

        :param request: The incoming request, used to extract the user
            identity.
        :param limit: Maximum revisions to return, 1..200.
        :returns: ``{"data": [{"id", "text", "created_at",
            "created_by"}]}``, newest first.
        :raises OmnigentError: 401/403 if the user lacks admin
            privileges.
        """
        await _require_admin(request, auth_provider, permission_store)
        revisions = await asyncio.to_thread(store.list_revisions, limit=limit)
        return {"data": [_revision_to_response(r) for r in revisions]}

    return router
