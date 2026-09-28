"""REST routes for the per-host calling defaults (``/v1/calling-defaults``).

Three surfaces, all scoped to the caller's own hosts:

* ``POST /sync`` — ask each online owned host for the model catalogs of
  the harnesses it reports as configured, and cache the answers.
* ``GET /catalogs`` — read the caller's cached catalogs back.
* ``GET /resolve`` — resolve the agent / harness / model / effort one
  create would use, and report any default-sourced value the cached
  catalog does not offer. Pure read: it never syncs.

The sync work goes through ``routes.hosts._proxy_model_options`` so a
host's own failure text (including ``"unsupported"``) is recorded rather
than collapsed into a transport error.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from omnigent.calling_defaults import (
    MODEL_OPTION_HARNESSES,
    check_calling_defaults,
    load_master,
    resolve_calling,
)
from omnigent.db.utils import now_epoch
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.hosts import _proxy_model_options
from omnigent.server.schemas import (
    CallingDefaultsCatalogRow,
    CallingDefaultsCatalogsResponse,
    CallingDefaultsResolveResponse,
    CallingDefaultsSyncRequest,
)
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores import AgentStore
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
from omnigent.stores.host_store import Host, HostStore
from omnigent.stores.project_store import ProjectStore

#: Per-sync limit on concurrent host round-trips.
_SYNC_CONCURRENCY = 4


def _owner(user_id: str | None) -> str:
    """The host / preferences owner; single-user mode owns the reserved local user."""
    return user_id if user_id is not None else RESERVED_USER_LOCAL


def _harness_configured(host: Host, harness: str) -> bool:
    """Whether a host reports *harness* as configured.

    A missing readiness map (older host build) or a missing entry is
    "unknown" and stays a candidate; only an explicit negative excludes
    the pair.
    """
    readiness = host.configured_harnesses
    if not readiness:
        return True
    value = readiness.get(harness)
    return value is None or value is True


async def _sync_pair(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    catalog_store: HostModelCatalogCacheStore,
    semaphore: asyncio.Semaphore,
    host_id: str,
    harness: str,
) -> None:
    """Refresh one ``(host, harness)`` catalog, recording failures as errors."""
    async with semaphore:
        try:
            result = await _proxy_model_options(
                host_registry=host_registry,
                host_conn=host_conn,
                harness=harness,
            )
        except HTTPException as exc:
            detail = str(exc.detail) if exc.detail else str(exc)
            await asyncio.to_thread(catalog_store.mark_error, host_id, harness, detail)
            return
        except Exception as exc:  # noqa: BLE001 — one pair never fails the sync
            await asyncio.to_thread(catalog_store.mark_error, host_id, harness, str(exc))
            return
        models = result.get("models")
        if result.get("status") == "ok" and isinstance(models, list):
            payload = [row for row in models if isinstance(row, dict)]
            await asyncio.to_thread(catalog_store.upsert, host_id, harness, payload, now_epoch())
            return
        error = result.get("error")
        if not isinstance(error, str) or not error:
            error = "host model-options lookup failed"
        await asyncio.to_thread(catalog_store.mark_error, host_id, harness, error)


def _catalog_row(
    record: dict[str, Any],
    host_registry: HostRegistry,
) -> CallingDefaultsCatalogRow:
    """Add the registry-based offline half of the staleness flag."""
    offline = host_registry.get(record["host_id"]) is None
    return CallingDefaultsCatalogRow(
        host_id=record["host_id"],
        harness=record["harness"],
        models=record["models"],
        fetched_at=record["fetched_at"],
        stale=bool(record["error"]) or offline,
        error=record["error"],
    )


def create_calling_defaults_router(
    *,
    host_store: HostStore,
    host_registry: HostRegistry,
    catalog_store: HostModelCatalogCacheStore,
    project_store: ProjectStore | None = None,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
    user_preferences_store: SqlAlchemyUserPreferencesStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the calling-defaults router (``/v1/calling-defaults``).

    :param host_store: Host store used to scope every pair to the caller.
    :param host_registry: Live connections; a host absent here is offline
        on this replica and is never asked for a catalog.
    :param catalog_store: Persistent catalog cache.
    :param project_store: Project store for the optional ``project_id``
        lookup; ``None`` makes every ``project_id`` a 404.
    :param agent_store: Agent store for resolving an agent id to its harness.
    :param agent_cache: Cache for loading the agent's parsed spec.
    :param user_preferences_store: Preferences store holding the caller's
        master table.
    :param auth_provider: Optional auth provider for user identity.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.post("/calling-defaults/sync")
    async def sync_calling_defaults(
        request: Request,
        body: CallingDefaultsSyncRequest,
    ) -> CallingDefaultsCatalogsResponse:
        """Refresh the caller's cached host model catalogs.

        :param request: The incoming request, used to identify the caller.
        :param body: Optional host / harness filters.
        :returns: Every cached row for the caller's hosts after the sync.
        :raises OmnigentError: 401 unauthenticated, 404 for an unowned
            ``host_id`` filter.
        """
        user_id = require_user(request, auth_provider)
        owner = _owner(user_id)

        if body.host_id is not None:
            host = await asyncio.to_thread(host_store.get_host, body.host_id)
            if host is None or host.user_id != owner:
                raise OmnigentError("Host not found", code=ErrorCode.NOT_FOUND)
            hosts = [host]
        else:
            hosts = await asyncio.to_thread(host_store.list_hosts, owner)

        harness_filter = (
            canonicalize_harness(body.harness) or body.harness if body.harness else None
        )
        semaphore = asyncio.Semaphore(_SYNC_CONCURRENCY)
        syncs = []
        for host in hosts:
            conn = host_registry.get(host.host_id)
            if conn is None:
                continue
            for harness in MODEL_OPTION_HARNESSES:
                if harness_filter is not None and harness != harness_filter:
                    continue
                if not _harness_configured(host, harness):
                    continue
                syncs.append(
                    _sync_pair(
                        host_registry=host_registry,
                        host_conn=conn,
                        catalog_store=catalog_store,
                        semaphore=semaphore,
                        host_id=host.host_id,
                        harness=harness,
                    )
                )
        if syncs:
            await asyncio.gather(*syncs)

        records = await asyncio.to_thread(catalog_store.list, [host.host_id for host in hosts])
        return CallingDefaultsCatalogsResponse(
            rows=[_catalog_row(record, host_registry) for record in records]
        )

    @router.get("/calling-defaults/catalogs")
    async def list_calling_default_catalogs(
        request: Request,
    ) -> CallingDefaultsCatalogsResponse:
        """Return the caller's own cached catalogs, stale rows included.

        :param request: The incoming request, used to identify the caller.
        :returns: ``{"rows": [...]}`` for the caller's hosts.
        :raises OmnigentError: 401 unauthenticated.
        """
        user_id = require_user(request, auth_provider)
        hosts = await asyncio.to_thread(host_store.list_hosts, _owner(user_id))
        records = await asyncio.to_thread(catalog_store.list, [host.host_id for host in hosts])
        return CallingDefaultsCatalogsResponse(
            rows=[_catalog_row(record, host_registry) for record in records]
        )

    @router.get("/calling-defaults/resolve")
    async def resolve_calling_defaults(
        request: Request,
        project_id: str | None = Query(default=None),
        host_id: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
        harness: str | None = Query(default=None),
    ) -> CallingDefaultsResolveResponse:
        """Resolve one create's calling triple; a pure read.

        :param request: The incoming request, used to identify the caller.
        :param project_id: Owning project whose per-host set applies, or
            ``None`` for the master layers only.
        :param host_id: Placement host, or ``None`` when unknown yet.
        :param agent_id: Explicit agent, or ``None`` to take the default.
        :param harness: Explicit harness override, or ``None``.
        :returns: The resolved triple, its per-field sources, and any
            default values the cached catalog does not offer.
        :raises OmnigentError: 401 unauthenticated, 404 for an unknown /
            foreign project or an unowned host.
        """
        from omnigent.server.library_agent_launch import is_library_agent_id

        user_id = require_user(request, auth_provider)
        owner = _owner(user_id)

        host: Host | None = None
        if host_id is not None:
            host = await asyncio.to_thread(host_store.get_host, host_id)
            if host is None or host.user_id != owner:
                raise OmnigentError("Host not found", code=ErrorCode.NOT_FOUND)

        project = None
        if project_id is not None:
            if project_store is None:
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
            project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
            if project is None:
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)

        explicit: dict[str, Any] = {}
        explicit_fields: set[str] = set()
        if agent_id is not None:
            explicit["agent_id"] = agent_id
            explicit_fields.add("agent_id")
        if harness is not None:
            explicit["harness_override"] = canonicalize_harness(harness) or harness
            explicit_fields.add("harness_override")

        def agent_harness(effective_agent_id: str) -> str | None:
            from omnigent.server.routes._sessions.orchestration import _create_resolved_harness

            # A saved library Agent has no ``agents`` row; the id would not
            # even bind against the UUID-typed lookup.
            if agent_store is None or is_library_agent_id(effective_agent_id):
                return None
            agent = agent_store.get(effective_agent_id)
            if agent is None:
                return None
            return _create_resolved_harness(agent, None, agent_cache)

        master = await load_master(owner, user_preferences_store)
        resolved_host_id = host.host_id if host is not None else None
        resolution = await asyncio.to_thread(
            resolve_calling,
            explicit=explicit,
            explicit_fields=explicit_fields,
            project_config=project.config if project is not None else None,
            master=master,
            host_id=resolved_host_id,
            agent_harness=agent_harness,
        )

        catalog: dict[str, Any] | None = None
        if resolved_host_id is not None and resolution.harness is not None:
            records = await asyncio.to_thread(catalog_store.list, [resolved_host_id])
            record = next((row for row in records if row["harness"] == resolution.harness), None)
            if record is not None:
                catalog = {
                    "models": record["models"],
                    "error": record["error"],
                    "fetched_at": record["fetched_at"],
                }

        agent_name: str | None = None
        if (
            resolution.agent_id is not None
            and agent_store is not None
            and not is_library_agent_id(resolution.agent_id)
        ):
            agent = await asyncio.to_thread(agent_store.get, resolution.agent_id)
            agent_name = getattr(agent, "name", None)
        problems = check_calling_defaults(
            resolution=resolution,
            host_id=resolved_host_id or "",
            host_name=host.name if host is not None and host.name else None,
            configured_harnesses=host.configured_harnesses if host is not None else None,
            catalog=catalog,
            agent_name=agent_name,
            project_name=project.name if project is not None else None,
            # The preview names the paths that would refuse; New Chat itself
            # launches a saved joint agent through its own library flow.
            path_label="server-side creates",
        )
        return CallingDefaultsResolveResponse(
            agent_id=resolution.agent_id,
            harness=resolution.harness,
            model=resolution.model,
            effort=resolution.effort,
            sources=resolution.sources,
            problems=problems,
        )

    return router
