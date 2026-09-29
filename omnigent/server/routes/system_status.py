"""System-status routes: the read path for the resource monitor.

``GET /v1/system/status`` returns the per-caller view (an admin sees the
server card and every host in the workspace; a member sees only their own
hosts and findings). ``GET /v1/system/history`` serves one target's 24 h
points, ``GET /v1/system/brief`` the admin health-check snapshot, and
``GET|PUT /v1/system/settings`` the admin-editable thresholds and
health-check target. Mounted only when a host store is configured, like the
host routes themselves.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Request

from omnigent.db.db_models import current_workspace_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.system_status import DEFAULT_HEALTH_CHECK_PROMPT, SystemStatusHub
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.version import VERSION


def create_system_status_router(
    host_store: HostStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    host_versions: Callable[[list[str]], dict[str, str]] | None = None,
) -> APIRouter:
    """Build the system-status router (mounted under ``/v1``).

    :param host_store: Host registrations, merged into the view so a
        never-connected host still appears.
    :param auth_provider: Optional auth provider for user identity.
    :param permission_store: Admin authority; ``None`` (single-user
        mode) treats every caller as an admin.
    :param host_versions: Resolves ``{host_id: version}`` for the brief's
        host lines; a host absent from the map reads ``unknown``.
    """
    router = APIRouter()

    async def _is_admin(request: Request) -> bool:
        """Whether the caller may see the server card and all hosts."""
        if permission_store is None:
            return True
        user_id = require_user(request, auth_provider)
        if user_id is None:
            return False
        return await asyncio.to_thread(permission_store.is_admin, user_id)

    def _hub(request: Request) -> SystemStatusHub:
        hub = getattr(request.app.state, "system_status", None)
        if not isinstance(hub, SystemStatusHub):
            raise OmnigentError(
                "System status is not enabled on this server",
                code=ErrorCode.NOT_FOUND,
            )
        return hub

    async def _caller_scope(
        request: Request,
    ) -> tuple[str | None, bool, set[str], list[dict[str, Any]]]:
        """Resolve identity, admin flag, and the caller's host-store rows."""
        user_id = require_user(request, auth_provider)
        is_admin = await _is_admin(request)
        own = await asyncio.to_thread(host_store.list_hosts, user_id or "local")
        own_host_ids = {host.host_id for host in own}
        own_offline_hosts = [
            {"host_id": host.host_id, "name": host.name} for host in own if host.status != "online"
        ]
        return user_id, is_admin, own_host_ids, own_offline_hosts

    @router.get("/system/status")
    async def get_system_status(
        request: Request,
        live: int = 0,
        summary: int = 0,
    ) -> dict[str, Any]:
        """Return the caller's system-status view.

        ``live=1`` records a viewer lease so the fast-mode loop samples the
        returned hosts every 10 s while the page is open. ``summary=1``
        returns only revision / level / findings (the sidebar poll).
        """
        hub = _hub(request)
        user_id, is_admin, own_host_ids, own_offline_hosts = await _caller_scope(request)
        workspace_id = current_workspace_id()
        view = hub.view(
            user_id=user_id,
            is_admin=is_admin,
            own_host_ids=own_host_ids,
            own_offline_hosts=own_offline_hosts,
            workspace_id=workspace_id,
            summary=bool(summary),
        )
        if live and not summary:
            host_ids = [host["host_id"] for host in view.get("hosts", [])]
            if host_ids:
                hub.mark_viewer(host_ids, workspace_id, time.time())
        return view

    @router.get("/system/history")
    async def get_system_history(request: Request, target: str) -> dict[str, Any]:
        """Return one target's age-filtered points; 404 when not visible."""
        hub = _hub(request)
        user_id, is_admin, own_host_ids, _ = await _caller_scope(request)
        points = hub.history(
            target,
            user_id=user_id,
            is_admin=is_admin,
            own_host_ids=own_host_ids,
            workspace_id=current_workspace_id(),
        )
        if points is None:
            raise OmnigentError(
                f"Unknown or inaccessible history target {target!r}",
                code=ErrorCode.NOT_FOUND,
            )
        return {"target": target, "points": points}

    @router.get("/system/brief")
    async def get_system_brief(request: Request) -> dict[str, Any]:
        """Return the monitor brief for the caller's admin host set."""
        if not await _is_admin(request):
            raise OmnigentError(
                "Admin privileges required to run the system-status health check",
                code=ErrorCode.FORBIDDEN,
            )
        hub = _hub(request)
        user_id, _admin, _own_host_ids, own_offline_hosts = await _caller_scope(request)
        workspace_id = current_workspace_id()
        now = time.time()
        view = hub.view(
            user_id=user_id,
            is_admin=True,
            own_host_ids=set(),
            own_offline_hosts=own_offline_hosts,
            workspace_id=workspace_id,
            summary=False,
        )
        host_ids = [host["host_id"] for host in view.get("hosts", [])]
        text = hub.brief(
            now=now,
            workspace_id=workspace_id,
            own_offline_hosts=own_offline_hosts,
            host_versions=host_versions(host_ids) if host_versions is not None else {},
            server_version=VERSION,
        )
        return {
            "text": text,
            "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        }

    @router.get("/system/settings")
    async def get_system_settings(request: Request) -> dict[str, Any]:
        """Return the thresholds and health-check settings (admin only)."""
        if not await _is_admin(request):
            raise OmnigentError(
                "Admin privileges required to manage system-status settings",
                code=ErrorCode.FORBIDDEN,
            )
        settings = _hub(request).get_settings()
        return {**settings, "default_health_check_prompt": DEFAULT_HEALTH_CHECK_PROMPT}

    @router.put("/system/settings")
    async def put_system_settings(
        request: Request,
        payload: Annotated[dict[str, Any], Body()],
    ) -> dict[str, Any]:
        """Validate and store settings, returning the effective set."""
        if not await _is_admin(request):
            raise OmnigentError(
                "Admin privileges required to manage system-status settings",
                code=ErrorCode.FORBIDDEN,
            )
        hub = _hub(request)
        try:
            settings = hub.put_settings(payload)
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        await hub.save_settings(settings)
        return {**settings, "default_health_check_prompt": DEFAULT_HEALTH_CHECK_PROMPT}

    return router
