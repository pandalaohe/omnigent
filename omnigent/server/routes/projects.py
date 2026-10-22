"""REST API routes for projects (``/v1/projects``).

Projects are first-class, owner-private containers that group sessions (see
``designs/PROJECTS_PRD.md``). These endpoints let the web UI create empty
projects, list them, rename them, and delete them. Session membership
(filing a session into a project) is managed on the sessions API via the
conversation store's ``project_id``.

Because projects are owner-private and carry no ACL of their own, every handler
scopes to the requesting user: a caller only ever sees and mutates their own
projects. In single-user mode (no auth provider) the owner is ``None`` and all
projects share that scope.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request

from omnigent.calling_defaults import validate_project_calling_defaults
from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.server.auth import AuthProvider
from omnigent.server.project_archive import ProjectArchiveService
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.sessions import announce_projects_changed
from omnigent.server.schemas import (
    CreateProjectRequest,
    PatchProjectCallingDefaultsRequest,
    ProjectOrderRequest,
    ProjectOrderResponse,
    UpdateProjectRequest,
)
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import (
    ProjectOrderPreference,
    ProjectStore,
    apply_project_order,
    project_config_revision,
)
from omnigent.stores.scheduled_task_store import ScheduledTaskStore


def _to_response(project: Project) -> dict[str, Any]:
    """Convert a :class:`Project` entity to a ``ProjectObject`` response dict.

    :param project: The entity to convert.
    :returns: Dict matching the :class:`ProjectObject` shape.
    """
    return {
        "id": project.id,
        "object": "project",
        "name": project.name,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "config": project.config,
        "config_revision": project_config_revision(project.config),
        "archived_at": project.archived_at,
        "archive_summary": {"sessions": 0, "tasks": 0},
    }


def _validate_calling_defaults(config: dict[str, Any]) -> None:
    """Validate the config's per-host calling defaults when the key is present.

    :param config: Incoming project config.
    :raises OmnigentError: 400 when ``calling_defaults`` has a bad shape.
    """
    if "calling_defaults" in config:
        validate_project_calling_defaults(config["calling_defaults"])


def create_projects_router(
    project_store: ProjectStore,
    auth_provider: AuthProvider | None = None,
    conversation_store: ConversationStore | None = None,
    permission_store: PermissionStore | None = None,
    scheduled_task_store: ScheduledTaskStore | None = None,
    runner_router: RunnerRouter | None = None,
) -> APIRouter:
    """Build the projects router (``/v1/projects``).

    :param project_store: The store backing project persistence.
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` in single-user mode (owner scope is ``None``).
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()
    archive_service = (
        ProjectArchiveService(
            project_store,
            conversation_store,
            permission_store,
            scheduled_task_store,
            runner_router,
        )
        if conversation_store is not None
        else None
    )

    async def response(project: Project) -> dict[str, Any]:
        value = _to_response(project)
        if archive_service is not None:
            value["archive_summary"] = await archive_service.summary(project)
        return value

    @router.post("/projects")
    async def create_project(
        request: Request,
        body: CreateProjectRequest,
    ) -> dict[str, Any]:
        """Create a new, empty project owned by the caller.

        :param request: The incoming request, used to identify the user.
        :param body: Project payload (name).
        :returns: The created project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 409
            if the caller already has a project with this name.
        """
        user_id = require_user(request, auth_provider)
        _validate_calling_defaults(body.config)
        project = await asyncio.to_thread(
            project_store.create,
            uuid.uuid4().hex,
            body.name,
            user_id,
            body.config,
        )
        # Push the change to the owner's other connected clients so their
        # sidebars pick up the new folder without a reload.
        announce_projects_changed(user_id)
        return _to_response(project)

    @router.get("/projects")
    async def list_projects(
        request: Request,
        include_archived: bool = False,
        archived: Literal["only"] | None = None,
    ) -> dict[str, Any]:
        """List the caller's projects.

        :param request: The incoming request, used to identify the user.
        :returns: ``{"object": "list", "data": [...]}``.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode.
        """
        user_id = require_user(request, auth_provider)

        def read_projects() -> list[Project]:
            return apply_project_order(
                project_store.list(
                    user_id=user_id,
                    archived="only" if archived else "include" if include_archived else "exclude",
                ),
                project_store.get_order(user_id=user_id),
                project_id=lambda p: p.id,
                project_name=lambda p: p.name,
            )

        projects = await asyncio.to_thread(read_projects)
        return {"object": "list", "data": [await response(p) for p in projects]}

    @router.get(
        "/projects/order", response_model=None, responses={200: {"model": ProjectOrderResponse}}
    )
    async def get_project_order(request: Request) -> ProjectOrderPreference:
        """Read the caller's custom order."""
        user_id = require_user(request, auth_provider)
        return await asyncio.to_thread(project_store.get_order_preference, user_id=user_id)

    @router.put(
        "/projects/order", response_model=None, responses={200: {"model": ProjectOrderResponse}}
    )
    async def save_project_order(
        request: Request, body: ProjectOrderRequest
    ) -> ProjectOrderPreference:
        """Save the caller's order and notify their connected clients."""
        user_id = require_user(request, auth_provider)
        preference = await asyncio.to_thread(
            project_store.save_order, body.ordered_project_ids, user_id=user_id
        )
        announce_projects_changed(user_id)
        return preference

    @router.get("/projects/{project_id}")
    async def get_project(request: Request, project_id: str) -> dict[str, Any]:
        """Return one of the caller's projects.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to fetch.
        :returns: The project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return await response(project)

    @router.get("/projects/{project_id}/archive-preview")
    async def archive_preview(request: Request, project_id: str) -> dict[str, Any]:
        owner = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=owner)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if archive_service is None:
            raise OmnigentError("Session store unavailable", code=ErrorCode.INTERNAL_ERROR)
        return await archive_service.preview(request, project)

    async def transition_archive(
        request: Request, project_id: str, kind: Literal["archive", "unarchive"]
    ) -> dict[str, Any]:
        owner = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=owner)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if archive_service is None:
            raise OmnigentError("Session store unavailable", code=ErrorCode.INTERNAL_ERROR)
        result = await archive_service.transition(request, project, kind)
        result["project"] = await response(result["project"])
        announce_projects_changed(owner)
        return result

    @router.post("/projects/{project_id}/archive")
    async def archive_project(request: Request, project_id: str) -> dict[str, Any]:
        return await transition_archive(request, project_id, "archive")

    @router.post("/projects/{project_id}/unarchive")
    async def unarchive_project(request: Request, project_id: str) -> dict[str, Any]:
        return await transition_archive(request, project_id, "unarchive")

    @router.patch("/projects/{project_id}")
    async def update_project(
        request: Request,
        project_id: str,
        body: UpdateProjectRequest,
    ) -> dict[str, Any]:
        """Update one of the caller's projects (e.g. rename).

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to update.
        :param body: Fields to change; ``None`` fields are left unchanged.
        :returns: The updated project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned, 409 if the new name collides with another of the caller's
            projects.
        """
        user_id = require_user(request, auth_provider)
        if body.config is not None:
            _validate_calling_defaults(body.config)
        project = await asyncio.to_thread(
            project_store.update,
            project_id,
            user_id=user_id,
            name=body.name,
            config=body.config,
        )
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        # A rename/config change is only ever seen by other connected clients
        # (another tab, the mobile app) if it is pushed; nothing else refreshes
        # their projects cache until a full reload.
        announce_projects_changed(user_id)
        return _to_response(project)

    @router.patch("/projects/{project_id}/calling-defaults")
    async def patch_project_calling_defaults(
        request: Request,
        project_id: str,
        body: PatchProjectCallingDefaultsRequest,
    ) -> dict[str, Any]:
        """Apply selected defaults without replacing the project's other config."""
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(
            project_store.patch_calling_defaults,
            project_id,
            user_id=user_id,
            expected_revision=body.expected_revision,
            host_id=body.host_id,
            harness=body.harness,
            fields=body.fields,
            agent_id=body.agent_id,
            copy_agent=body.copy_agent,
        )
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        announce_projects_changed(user_id)
        return _to_response(project)

    @router.delete("/projects/{project_id}")
    async def delete_project(request: Request, project_id: str) -> dict[str, Any]:
        """Delete one of the caller's projects.

        Member sessions are not deleted; they are left for the caller to
        unfile (clearing their ``project_id``).

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to delete.
        :returns: ``{"id": ..., "object": "project.deleted", "deleted": True}``.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        deleted = await asyncio.to_thread(project_store.delete, project_id, user_id=user_id)
        if not deleted:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        # Drop the folder from the owner's other connected clients live.
        announce_projects_changed(user_id)
        return {"id": project_id, "object": "project.deleted", "deleted": True}

    return router
