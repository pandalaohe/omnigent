"""Read and write a project's per-host entry directories.

An entry is the directory a project's sessions open in — a project setting of
its own, separate from the registered repositories and host bindings, and
usable with collaboration off. Every route requires the caller to own the
project; unlike the collaboration surface, nothing here is feature-flag
gated.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict

from omnigent.entities import ProjectHostEntry
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.project_collaboration import _canonical_binding_workspace
from omnigent.stores.project_host_binding_store import ProjectHostBindingStore
from omnigent.stores.project_store import ProjectStore

_SANDBOX_HOST_ID = "__sandbox__"


class EntryPutRequest(BaseModel):
    """Request body for ``PUT /v1/projects/{project_id}/entries/{host_id}``.

    :param workspace: Absolute path on the host. Validated live via
        ``host.stat``; the canonical path the host returns is stored.
    """

    model_config = ConfigDict(extra="forbid")

    workspace: str


def _is_managed_worktree_path(path: str) -> bool:
    """Whether *path* lies inside an Omnigent-managed worktree area.

    Assignment release and session cleanup remove those directories, so an
    entry there could later be matched by a removal that never intended it.

    :param path: Canonical path returned by the host.
    :returns: ``True`` for a ``.worktrees`` component or an
        ``.omnigent/worktrees`` pair, on either path separator.
    """
    components = path.replace("\\", "/").split("/")
    if ".worktrees" in components:
        return True
    return any(
        components[index] == ".omnigent" and components[index + 1] == "worktrees"
        for index in range(len(components) - 1)
    )


def _entry_to_response(entry: ProjectHostEntry) -> dict[str, Any]:
    """Convert an entry entity to a response dict.

    :param entry: The entity to convert.
    :returns: Dict with the entry fields.
    """
    return {
        "host_id": entry.host_id,
        "workspace": entry.workspace,
        "updated_at": entry.updated_at,
    }


def create_project_entries_router(
    project_store: ProjectStore,
    binding_store: ProjectHostBindingStore,
    auth_provider: AuthProvider | None = None,
    host_store: Any | None = None,
    host_registry: Any | None = None,
) -> APIRouter:
    """Build the project-entries router (under ``/v1/projects``).

    :param project_store: The store backing project persistence.
    :param binding_store: The store backing per-host entries.
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` in single-user mode (owner scope is ``None``).
    :param host_store: Persistent host registrations for entry
        authorization; ``None`` skips the ownership check.
    :param host_registry: Live host tunnels, used to validate entry paths on
        the host.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    async def _require_owned_project(project_id: str, user_id: str | None) -> Any:
        """Return the caller's project, or 404 when absent / not owned.

        :param project_id: The project to fetch.
        :param user_id: The requesting owner.
        :returns: The project entity.
        :raises OmnigentError: ``NOT_FOUND`` when not found / not owned.
        """
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return project

    @router.get("/projects/{project_id}/entries")
    async def list_project_entries(request: Request, project_id: str) -> dict[str, Any]:
        """Return the project's entry directory per host.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to inspect.
        :returns: ``{"entries": [{"host_id", "workspace", "updated_at"}]}``.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project is
            not found / not owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        await _require_owned_project(project_id, user_id)
        entries = await asyncio.to_thread(binding_store.list_entries, project_id)
        return {"entries": [_entry_to_response(entry) for entry in entries]}

    @router.put("/projects/{project_id}/entries/{host_id}")
    async def put_project_entry(
        request: Request,
        project_id: str,
        host_id: str,
        body: EntryPutRequest,
    ) -> dict[str, Any]:
        """Validate and store a host's entry directory.

        The typed path is validated live on the host; the canonical path the
        host returns is what gets stored, never the typed one.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project the entry belongs to.
        :param host_id: The host the directory lives on.
        :param body: Workspace path on the host.
        :returns: The inserted or updated entry.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project is
            not found / not owned, 400 for the sandbox host, a bad path or a
            path inside a worktree folder, 409 when the host is offline.
        """
        user_id = require_user(request, auth_provider)
        await _require_owned_project(project_id, user_id)
        if host_id == _SANDBOX_HOST_ID:
            raise OmnigentError(
                "the sandbox host has no project directory",
                code=ErrorCode.INVALID_INPUT,
            )
        canonical, _host_name = await _canonical_binding_workspace(
            user_id=user_id,
            host_id=host_id,
            workspace=body.workspace,
            host_store=host_store,
            host_registry=host_registry,
        )
        if _is_managed_worktree_path(canonical):
            raise OmnigentError(
                "a project directory cannot be inside a worktree folder "
                "(.worktrees or .omnigent/worktrees)",
                code=ErrorCode.INVALID_INPUT,
            )
        entry = await asyncio.to_thread(
            binding_store.put_entry,
            project_id,
            host_id,
            canonical,
        )
        return _entry_to_response(entry)

    @router.delete(
        "/projects/{project_id}/entries/{host_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    async def delete_project_entry(request: Request, project_id: str, host_id: str) -> Response:
        """Delete a host's entry directory.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project the entry belongs to.
        :param host_id: The host whose entry to remove.
        :returns: An empty 204 response.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project or
            entry is not found / not owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        await _require_owned_project(project_id, user_id)
        removed = await asyncio.to_thread(binding_store.delete_entry, project_id, host_id)
        if not removed:
            raise OmnigentError("Entry not found", code=ErrorCode.NOT_FOUND)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
