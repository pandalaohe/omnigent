"""REST routes for the per-host calling defaults (``/v1/calling-defaults``).

Three surfaces, all scoped to the caller's own hosts:

* ``POST /sync`` — ask each online owned host for the model catalogs of
  the harnesses it reports as configured, and cache the answers.
* ``GET /catalogs`` — read cached catalogs, refreshing expired agy rows in
  the background and fetching missing ACP delivery metadata.
* ``GET /resolve`` — resolve the agent / harness / model / effort one
  create would use, and report any default-sourced value the cached
  catalog does not offer. Pure read: it never syncs.

The sync work goes through ``routes.hosts._proxy_model_options`` so a
host's own failure text (including ``"unsupported"``) is recorded rather
than collapsed into a transport error.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Query, Request

from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES, is_acp_harness
from omnigent.calling_defaults import (
    check_calling_defaults,
    load_master,
    model_option_harnesses,
    resolve_calling,
)
from omnigent.db.db_models import InvalidUuidError, current_workspace_id, uuid_to_bytes
from omnigent.db.utils import now_epoch
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_model_options import record_host_model_options
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
_AGY_HARNESS = "antigravity-native"
_AGY_CATALOG_TTL_S = 300
_AGY_REFRESH_BACKOFF_S = 60
# Leave time for cache reads and HTTP overhead within sys_list_models' 30s GET.
_AGY_FILTERED_READ_WAIT_S = 20
_logger = logging.getLogger(__name__)


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
    refresh: str | None = None,
) -> None:
    """Refresh one ``(host, harness)`` catalog, recording failures as errors."""
    async with semaphore:
        try:
            result = await _proxy_model_options(
                host_registry=host_registry,
                host_conn=host_conn,
                harness=harness,
                **({"refresh": refresh} if refresh else {}),
            )
        except HTTPException as exc:
            detail = str(exc.detail) if exc.detail else str(exc)
            await asyncio.to_thread(catalog_store.mark_error, host_id, harness, detail)
            return
        except Exception as exc:  # noqa: BLE001 — one pair never fails the sync
            await asyncio.to_thread(catalog_store.mark_error, host_id, harness, str(exc))
            return
        await record_host_model_options(
            catalog_store, host_id, harness, result, fetched_at=now_epoch()
        )


def _agy_catalog_expired(record: dict[str, Any] | None) -> bool:
    fetched_at = record["fetched_at"] if record is not None else None
    return fetched_at is None or now_epoch() - fetched_at >= _AGY_CATALOG_TTL_S


def _catalog_row(
    record: dict[str, Any],
    host_registry: HostRegistry,
) -> CallingDefaultsCatalogRow:
    """Expose offline, probe-failure and age-based staleness."""
    offline = host_registry.get(record["host_id"]) is None
    return CallingDefaultsCatalogRow(
        host_id=record["host_id"],
        harness=record["harness"],
        models=record["models"],
        permission_options=record.get("permission_options", []),
        injects_prompt=record.get("injects_prompt"),
        fetched_at=record["fetched_at"],
        stale=bool(record["error"])
        or offline
        or (record["harness"] == _AGY_HARNESS and _agy_catalog_expired(record)),
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
    agy_refreshes: dict[tuple[int, str], asyncio.Task[None]] = {}
    agy_attempts: TTLCache[tuple[int, str], int] = TTLCache(
        maxsize=4096, ttl=_AGY_REFRESH_BACKOFF_S
    )
    agy_sync_limit = asyncio.Semaphore(_SYNC_CONCURRENCY)

    def start_agy_refresh(
        host_id: str, conn: HostConnection, *, manual: bool = False
    ) -> asyncio.Task[None] | None:
        # The picker and host-catalog tool can reach the same expired row at once.
        # Keep one CLI probe in flight even if one reader disconnects.
        key = (current_workspace_id(), host_id)
        if task := agy_refreshes.get(key):
            return task
        if not manual and key in agy_attempts:
            return None

        async def refresh() -> None:
            attempted_at = now_epoch()
            try:
                await _sync_pair(
                    host_registry=host_registry,
                    host_conn=conn,
                    catalog_store=catalog_store,
                    semaphore=agy_sync_limit,
                    host_id=host_id,
                    harness=_AGY_HARNESS,
                )
            except Exception:
                _logger.exception("Could not refresh the Antigravity catalog for host %s", host_id)
            finally:
                # Cool down after completion, even when a slow probe fails.
                # Keep fetched_at reserved for the last successful payload.
                agy_attempts[key] = attempted_at
                agy_refreshes.pop(key, None)

        task = asyncio.create_task(refresh(), name=f"agy-catalog-{host_id}")
        agy_refreshes[key] = task
        return task

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
            body.harness
            if body.harness and body.harness.startswith("acp:")
            else canonicalize_harness(body.harness) or body.harness
            if body.harness
            else None
        )
        semaphore = asyncio.Semaphore(_SYNC_CONCURRENCY)
        syncs: list[Awaitable[None]] = []
        for host in hosts:
            conn = host_registry.get(host.host_id)
            if conn is None:
                continue
            acp_harnesses = [
                key for key in (host.configured_harnesses or {}) if key.startswith("acp:")
            ]
            if (
                harness_filter
                and harness_filter.startswith("acp:")
                and harness_filter not in acp_harnesses
            ):
                acp_harnesses.append(harness_filter)
            for harness in (*model_option_harnesses(), *acp_harnesses):
                if harness_filter is not None and harness != harness_filter:
                    continue
                if not _harness_configured(host, harness):
                    continue
                if harness == _AGY_HARNESS:
                    if task := start_agy_refresh(host.host_id, conn, manual=True):
                        syncs.append(asyncio.shield(task))
                    continue
                syncs.append(
                    await_acp_catalog(host.host_id, conn, harness, manual=True)
                    if canonicalize_harness(harness) == "acp" or is_acp_harness(harness)
                    else _sync_pair(
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
        host_id: str | None = Query(default=None),
        harness: str | None = Query(default=None),
    ) -> CallingDefaultsCatalogsResponse:
        """Return the caller's own cached catalogs, stale rows included.

        Only an explicit AG harness filter waits briefly for an AG refresh.
        Other reads use cached AG rows while the refresh runs in the background.
        First-use ACP metadata sync remains exclusive to unfiltered reads.

        :param request: The incoming request, used to identify the caller.
        :param host_id: Optional filter narrowing the rows to one owned host.
        :param harness: Optional filter narrowing the rows to one harness.
        :returns: ``{"rows": [...]}`` for the caller's hosts.
        :raises OmnigentError: 401 unauthenticated, 404 for an unowned
            ``host_id`` filter.
        """
        user_id = require_user(request, auth_provider)
        owner = _owner(user_id)
        if host_id is not None:
            host = await asyncio.to_thread(host_store.get_host, host_id)
            if host is None or host.user_id != owner:
                raise OmnigentError("Host not found", code=ErrorCode.NOT_FOUND)
            hosts = [host]
        else:
            hosts = await asyncio.to_thread(host_store.list_hosts, owner)
        wanted = (
            harness
            if harness is None or harness.startswith("acp:")
            else canonicalize_harness(harness) or harness
        )
        host_ids = [host.host_id for host in hosts]
        records = await asyncio.to_thread(catalog_store.list, host_ids)
        if wanted is None or wanted == _AGY_HARNESS:
            agy_records = {
                row["host_id"]: row for row in records if row["harness"] == _AGY_HARNESS
            }
            refreshes = [
                task
                for host in hosts
                if (conn := host_registry.get(host.host_id)) is not None
                and _harness_configured(host, _AGY_HARNESS)
                and (
                    host.host_id in agy_records
                    or (host.configured_harnesses or {}).get(_AGY_HARNESS) is True
                )
                and _agy_catalog_expired(agy_records.get(host.host_id))
                and (task := start_agy_refresh(host.host_id, conn)) is not None
            ]
            if wanted == _AGY_HARNESS and refreshes:
                # A timed-out reader leaves the shared probes running.
                done, _ = await asyncio.wait(refreshes, timeout=_AGY_FILTERED_READ_WAIT_S)
                if done:
                    records = await asyncio.to_thread(catalog_store.list, host_ids)
        if host_id is not None or harness is not None:
            return CallingDefaultsCatalogsResponse(
                rows=[
                    read_catalog_row(record)
                    for record in records
                    if wanted is None or record["harness"] == wanted
                ],
            )
        # Configuration metadata must be visible on first use, independently
        # of a model probe succeeding or an explicit Sync models action.
        known = {
            (row["host_id"], row["harness"])
            for row in records
            if row.get("injects_prompt") is not None or acp_catalog_expired(row)
        }
        await asyncio.gather(
            *(
                await_acp_catalog(host.host_id, conn, harness)
                for host in hosts
                if (conn := host_registry.get(host.host_id)) is not None
                for harness, _ready in (host.configured_harnesses or {}).items()
                if (harness.startswith("acp:") or harness in ACP_CLI_HARNESSES)
                and _harness_configured(host, harness)
                and (host.host_id, harness) not in known
            )
        )
        records = await asyncio.to_thread(catalog_store.list, host_ids)
        return CallingDefaultsCatalogsResponse(
            rows=[read_catalog_row(record) for record in records]
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
            explicit["harness_override"] = (
                harness if harness.startswith("acp:") else canonicalize_harness(harness) or harness
            )
            explicit_fields.add("harness_override")

        def agent_row(effective_agent_id: str) -> Any | None:
            # A saved library Agent or a built-in joint member (named, not an
            # id) has no ``agents`` row; its id would not even bind against the
            # UUID-typed lookup, which answers 404 for the whole request.
            if agent_store is None or is_library_agent_id(effective_agent_id):
                return None
            try:
                uuid_to_bytes(effective_agent_id)
            except InvalidUuidError:
                return None
            return agent_store.get(effective_agent_id)

        def agent_harness(effective_agent_id: str) -> str | None:
            from omnigent.server.routes._sessions.orchestration import _create_resolved_harness

            agent = agent_row(effective_agent_id)
            if agent is None:
                return None
            return _create_resolved_harness(agent, None, agent_cache)

        warnings: list[str] = []
        master = await load_master(owner, user_preferences_store, warnings=warnings)
        resolved_host_id = host.host_id if host is not None else None
        records = (
            await asyncio.to_thread(catalog_store.list, [resolved_host_id])
            if resolved_host_id
            else []
        )
        from omnigent.server.model_resolution import ResolutionContext, read_provider_groups

        model_context = ResolutionContext(
            resolved_host_id or "",
            {row["harness"]: row for row in records},
            await asyncio.to_thread(read_provider_groups, user_preferences_store, owner),
        )
        resolution = await asyncio.to_thread(
            resolve_calling,
            explicit=explicit,
            explicit_fields=explicit_fields,
            project_config=project.config if project is not None else None,
            master=master,
            host_id=resolved_host_id,
            agent_harness=agent_harness,
            model_resolver=model_context.resolve,
            permission_options=lambda host_id, harness: next(
                (
                    row.get("permission_options", [])
                    for row in records
                    if row["host_id"] == host_id and row["harness"] == harness
                ),
                [],
            ),
        )

        catalog: dict[str, Any] | None = None
        if resolved_host_id is not None and resolution.harness is not None:
            record = next((row for row in records if row["harness"] == resolution.harness), None)
            if record is not None:
                catalog = {
                    "models": record["models"],
                    "error": record["error"],
                    "fetched_at": record["fetched_at"],
                }

        agent_name: str | None = None
        if resolution.agent_id is not None:
            agent = await asyncio.to_thread(agent_row, resolution.agent_id)
            agent_name = getattr(agent, "name", None)
            if agent is not None and resolution.model is None:
                from omnigent.entities import Conversation
                from omnigent.server.routes._sessions.helpers import _resolve_llm_model

                default_model = await asyncio.to_thread(
                    _resolve_llm_model,
                    Conversation(
                        id="",
                        root_conversation_id="",
                        created_at=0,
                        updated_at=0,
                        agent_id=resolution.agent_id,
                    ),
                    agent_store=agent_store,
                    agent_cache=agent_cache,
                )
                resolution = model_context.apply_default(resolution, default_model)
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
        warnings.extend(model_context.warnings())
        return CallingDefaultsResolveResponse(
            agent_id=resolution.agent_id,
            harness=resolution.harness,
            model=resolution.model,
            effort=resolution.effort,
            speed=resolution.speed,
            permission=resolution.permission,
            sources=resolution.sources,
            level=resolution.level,
            problems=problems,
            warnings=warnings,
            notes=list(resolution.notes),
        )

    # Share one probe per workspace, host and ACP agent across readers and manual sync.
    acp_refreshes: dict[tuple[int, str, str], asyncio.Task[None]] = {}
    acp_attempts: TTLCache[tuple[int, str, str], bool] = TTLCache(maxsize=4096, ttl=60)
    acp_sync_limit = asyncio.Semaphore(_SYNC_CONCURRENCY)

    def acp_catalog_expired(record: dict[str, Any]) -> bool:
        if canonicalize_harness(record["harness"]) != "acp" and not is_acp_harness(
            record["harness"]
        ):
            return False
        fetched_at = record["fetched_at"]
        return fetched_at is None or now_epoch() - fetched_at >= 300

    def start_acp_catalog_refresh(
        host_id: str, conn: HostConnection, harness: str, *, manual: bool = False
    ) -> asyncio.Task[None] | None:
        key = (current_workspace_id(), host_id, harness)
        if task := acp_refreshes.get(key):
            return task
        if not manual and key in acp_attempts:
            return None

        async def refresh() -> None:
            try:
                await _sync_pair(
                    host_registry=host_registry,
                    host_conn=conn,
                    catalog_store=catalog_store,
                    semaphore=acp_sync_limit,
                    host_id=host_id,
                    harness=harness,
                    refresh="force",
                )
            finally:
                acp_attempts[key] = True
                acp_refreshes.pop(key, None)

        task = asyncio.create_task(refresh(), name=f"acp-catalog-{host_id}")
        acp_refreshes[key] = task
        return task

    def read_catalog_row(record: dict[str, Any]) -> CallingDefaultsCatalogRow:
        row = _catalog_row(record, host_registry)
        if acp_catalog_expired(record):
            row.stale = True
            conn = host_registry.get(row.host_id)
            if conn is not None:
                readiness = conn.hello.configured_harnesses
                value = readiness.get(row.harness) if readiness else None
                if value is None or value is True:
                    start_acp_catalog_refresh(row.host_id, conn, row.harness)
        return row

    async def await_acp_catalog(
        host_id: str, conn: HostConnection, harness: str, *, manual: bool = False
    ) -> None:
        if task := start_acp_catalog_refresh(host_id, conn, harness, manual=manual):
            await asyncio.shield(task)

    return router
