"""Read a project's eligible host directories."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.project_placement import (
    bindings_apply,
    default_host,
    host_roots,
    load_bindings,
    load_eligible_host_ids,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.project_store import ProjectStore


def create_project_host_roots_router(
    project_store: ProjectStore, auth_provider: AuthProvider | None = None
) -> APIRouter:
    """Build the owner-scoped host-roots route.

    :param project_store: Store for owner-scoped project reads.
    :param auth_provider: Caller identity provider, or ``None`` in local mode.
    :returns: Router with the host-roots endpoint.
    """
    router = APIRouter()

    @router.get("/projects/{project_id}/host-roots")
    async def get_project_host_roots(request: Request, project_id: str) -> dict[str, Any]:
        """Return eligible roots and the project's default host.

        :param request: Request carrying app stores and the caller identity.
        :param project_id: Owner-scoped project id.
        :returns: Eligible roots and the default host choice.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        bindings = await load_bindings(request.app.state.project_host_binding_store, project_id)
        roots = host_roots(
            project, bindings, gates_on=bindings_apply(project, request.app.state.feature_flags)
        )
        eligible = await load_eligible_host_ids(
            request.app.state.host_store, user_id, (root.host_id for root in roots)
        )
        chosen = default_host(project, roots, eligible_host_ids=eligible)
        return {
            "roots": [
                asdict(root) for root in roots if eligible is None or root.host_id in eligible
            ],
            "default_host_id": chosen.host_id,
            "default_host_reason": chosen.reason,
        }

    return router
