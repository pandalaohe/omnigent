"""Authenticated, guarded project session-default reads and updates."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from omnigent.calling_defaults import (
    CallingResolution,
    _catalog_efforts,
    _catalog_model_ids,
    check_calling_defaults,
    load_master,
    model_option_harnesses,
    resolve_calling,
    validate_project_calling_defaults,
)
from omnigent.db.db_models import InvalidUuidError
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_availability import HARNESS_NEEDS_AUTH, reported_harness_availability
from omnigent.harness_capabilities import IntegrationMode
from omnigent.harness_plugins import capability_for_harness
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.project_host_agents import resolve_project_host_template
from omnigent.server.project_placement import default_host, host_roots
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.calling_defaults import _catalog_row
from omnigent.server.routes.project_collaboration import (
    _DEFAULT_MANIFEST_PATH,
    _is_managed_worktree_path,
    _record_setup_outcome,
    _resolve_stored_workspace,
    run_post_bind_request,
)
from omnigent.server.routes.sessions import announce_projects_changed
from omnigent.session_default_modes import mode_values, valid_mode
from omnigent.util.reasoning_effort import efforts_for_harness

_FIELDS = ("model", "effort", "speed", "permission")


class _HarnessPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None
    effort: str | None = None
    speed: str | None = None
    permission: str | None = None


class _ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host_id: str | None = None
    agent_id: str | None = None
    model: str | None = None


class _HostPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host_id: str
    workspace: str | None = None
    agent_id: str | None = None
    harnesses: dict[str, _HarnessPatch] | None = None


class _ProjectStandingPassPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remove: list[StrictStr] | None = None
    exceptions: (
        Annotated[list[Annotated[StrictStr, Field(max_length=120)]], Field(max_length=5)] | None
    ) = None


class _OrchestrationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    child_cap: Annotated[StrictInt, Field(gt=0)] | Literal["none"] | None = None
    setup: Annotated[StrictStr, Field(max_length=400)] | None = None
    standing_pass: _ProjectStandingPassPatch = Field(default_factory=_ProjectStandingPassPatch)


class _SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str
    project: _ProjectPatch | None = None
    host: _HostPatch | None = None
    orchestration: _OrchestrationPatch = Field(default_factory=_OrchestrationPatch)


def _invalid(field: str, message: str) -> OmnigentError:
    return OmnigentError(f"{field}: {message}", code=ErrorCode.INVALID_INPUT)


def _available_speed(harness: str, model: str | None, catalog: dict[str, Any] | None) -> list[str]:
    values = set(mode_values(harness, "speed", "default"))
    if harness in {"codex", "codex-native"} and catalog is not None:
        rows = catalog.get("models") or []
        selected = next(
            (
                row
                for row in rows
                if isinstance(row, dict) and (row.get("id") == model or row.get("model") == model)
            ),
            None,
        )
        if selected is None and model is None:
            selected = next(
                (row for row in rows if isinstance(row, dict) and row.get("isDefault")),
                None,
            )
        if selected is not None:
            values.add("standard")
            tiers = selected.get("serviceTiers")
            values.update(
                tier["id"]
                for tier in (tiers if isinstance(tiers, list) else [])
                if isinstance(tier, dict) and isinstance(tier.get("id"), str)
            )
            default_tier = selected.get("defaultServiceTier")
            if isinstance(default_tier, str) and valid_mode(
                default_tier, harness, "speed", "default"
            ):
                values.add(default_tier)
            if "priority" in values:
                values.add("fast")
    return sorted(values)


def _apply_patch(config: dict[str, Any], body: _SettingsPatch) -> tuple[dict[str, Any], list[str]]:
    """Apply named leaves only, preserving every unrelated config key."""
    result = deepcopy(config)
    named: list[str] = []
    if "orchestration" in body.model_fields_set:
        patch = body.orchestration
        stored = result.get("orchestration")
        orchestration = deepcopy(stored) if isinstance(stored, dict) else {}
        for field in ("child_cap", "setup"):
            if field in patch.model_fields_set:
                value = getattr(patch, field)
                if value is None:
                    orchestration.pop(field, None)
                else:
                    orchestration[field] = value
                named.append(f"orchestration.{field}")
        if "standing_pass" in patch.model_fields_set:
            stored = orchestration.get("standing_pass")
            standing_pass = deepcopy(stored) if isinstance(stored, dict) else {}
            for field in ("remove", "exceptions"):
                if field in patch.standing_pass.model_fields_set:
                    value = getattr(patch.standing_pass, field)
                    if value is None:
                        standing_pass.pop(field, None)
                    else:
                        standing_pass[field] = value
                    named.append(f"orchestration.standing_pass.{field}")
            if standing_pass:
                orchestration["standing_pass"] = standing_pass
            else:
                orchestration.pop("standing_pass", None)
        if orchestration:
            result["orchestration"] = orchestration
        else:
            result.pop("orchestration", None)
    if body.project is not None:
        for field in ("host_id", "agent_id", "model"):
            if field in body.project.model_fields_set:
                value = getattr(body.project, field)
                if value is None:
                    result.pop(field, None)
                else:
                    result[field] = value
                named.append(f"project.{field}")
    if body.host is not None:
        if not body.host.host_id or body.host.host_id == "__sandbox__":
            raise _invalid("host.host_id", "choose a registered host")
        defaults = deepcopy(result.get("calling_defaults") or {})
        host = deepcopy(defaults.get(body.host.host_id) or {})
        if "agent_id" in body.host.model_fields_set:
            if body.host.agent_id is None:
                host.pop("agent_id", None)
            else:
                host["agent_id"] = body.host.agent_id
            named.append("host.agent_id")
        if body.host.harnesses is not None:
            harnesses = deepcopy(host.get("harnesses") or {})
            for supplied_harness, patch in body.host.harnesses.items():
                harness = (
                    supplied_harness
                    if supplied_harness.startswith("acp:")
                    else canonicalize_harness(supplied_harness) or supplied_harness
                )
                if harness != supplied_harness or capability_for_harness(harness) is None:
                    raise _invalid(
                        f"host.harnesses.{supplied_harness}", "use a canonical supported harness"
                    )
                setting = deepcopy(harnesses.get(harness) or {})
                for field in _FIELDS:
                    if field in patch.model_fields_set:
                        value = getattr(patch, field)
                        if value is None:
                            setting.pop(field, None)
                        else:
                            setting[field] = value
                        named.append(f"host.harnesses.{harness}.{field}")
                if setting:
                    harnesses[harness] = setting
                else:
                    harnesses.pop(harness, None)
            if harnesses:
                host["harnesses"] = harnesses
            else:
                host.pop("harnesses", None)
        if host:
            defaults[body.host.host_id] = host
        else:
            defaults.pop(body.host.host_id, None)
        if defaults:
            result["calling_defaults"] = defaults
        else:
            result.pop("calling_defaults", None)
        if "workspace" in body.host.model_fields_set:
            named.append("host.workspace")
    if not named:
        raise _invalid("update", "name at least one setting leaf")
    if "calling_defaults" in result:
        validate_project_calling_defaults(result["calling_defaults"])
    for field in ("host_id", "agent_id", "model"):
        value = result.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise _invalid(f"project.{field}", "must be a non-empty string or null")
    return result, named


def create_project_settings_router(
    *,
    binding_store: Any,
    auth_provider: AuthProvider | None,
    permission_store: Any,
    host_store: Any,
    host_registry: Any,
    catalog_store: Any,
    agent_store: Any,
    agent_cache: Any,
    user_preferences_store: Any,
) -> APIRouter:
    """Serve one project at a time, using its owner for all dependent resources."""
    router = APIRouter()

    async def snapshot(request: Request, project_id: str) -> tuple[Any, list[Any], str]:
        caller = require_user(request, auth_provider)
        admin = bool(
            caller is not None
            and permission_store is not None
            and await asyncio.to_thread(permission_store.is_admin, caller)
        )
        try:
            return await asyncio.to_thread(
                binding_store.settings_snapshot, project_id, caller_id=caller, is_admin=admin
            )
        except OmnigentError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND) from exc
            raise

    async def context(
        project: Any,
    ) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]], list[Any], dict]:
        owner = project.user_id or RESERVED_USER_LOCAL
        hosts = await asyncio.to_thread(host_store.list_hosts, owner)
        host_map = {host.host_id: host for host in hosts}
        catalogs = await asyncio.to_thread(catalog_store.list, list(host_map))
        master = await load_master(owner, user_preferences_store)
        return host_map, {(row["host_id"], row["harness"]): row for row in catalogs}, hosts, master

    def agent_lookup(
        agent_id: str | None, owner: str, state: Any
    ) -> tuple[str | None, str | None]:
        if not agent_id:
            return None, None
        if agent_id.startswith("ca_"):
            library = getattr(state, "custom_agents_store", None)
            try:
                saved = library.get(owner, agent_id) if library is not None else None
            except OmnigentError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
                saved = None
            if saved is None:
                return None, None
            return saved.get("harness"), saved.get("name")
        try:
            agent = agent_store.get(agent_id) if agent_store is not None else None
        except (InvalidUuidError, OmnigentError, ValueError):
            agent = None
        if agent is None or (agent.kind == "user" and agent.created_by != owner):
            return None, None
        from omnigent.server.routes._sessions.orchestration import _create_resolved_harness

        return _create_resolved_harness(agent, None, agent_cache), agent.name

    async def render(
        request: Request,
        project: Any,
        entries: list[Any],
        revision: str,
        *,
        host_filter: str | None = None,
        harness_filter: str | None = None,
    ) -> dict[str, Any]:
        host_map, catalog_map, hosts, master = await context(project)
        owner = project.user_id or RESERVED_USER_LOCAL
        agent_choices: list[dict[str, Any]] = []
        if agent_store is not None:
            server_agents = await asyncio.to_thread(agent_store.list, limit=100)
            user_agents = await asyncio.to_thread(
                agent_store.list_user_agents, project.user_id, limit=100
            )
            for agent in (*server_agents.data, *user_agents.data):
                harness, _ = agent_lookup(agent.id, owner, request.app.state)
                if harness is not None:
                    agent_choices.append(
                        {"agent_id": agent.id, "name": agent.name, "harness": harness}
                    )
        library = getattr(request.app.state, "custom_agents_store", None)
        if library is not None:
            saved_agents = await asyncio.to_thread(library.list, owner, 100, 0)
            for saved in saved_agents:
                harness = saved.get("harness")
                if isinstance(harness, str):
                    agent_choices.append(
                        {"agent_id": saved["id"], "name": saved.get("name"), "harness": harness}
                    )
        bindings = await asyncio.to_thread(binding_store.list_by_project, project.id)
        roots = host_roots(project, bindings, entries=entries)
        chosen = default_host(project, roots, eligible_host_ids=frozenset(host_map))
        if host_filter is not None and host_filter not in host_map:
            raise OmnigentError("Host not found", code=ErrorCode.NOT_FOUND)
        if harness_filter is not None:
            canonical = (
                harness_filter
                if harness_filter.startswith("acp:")
                else canonicalize_harness(harness_filter) or harness_filter
            )
            if canonical != harness_filter or capability_for_harness(canonical) is None:
                raise _invalid("harness", "use a canonical supported harness")
        else:
            canonical = None
        rows: list[dict[str, Any]] = []
        for host in hosts:
            if host_filter is not None and host.host_id != host_filter:
                continue
            configured = set((host.configured_harnesses or {}).keys())
            saved = (project.config.get("calling_defaults") or {}).get(host.host_id) or {}
            configured.update((saved.get("harnesses") or {}).keys())
            configured.update(model_option_harnesses())
            if canonical is not None:
                configured = {canonical}
            for harness in sorted(configured):
                if capability_for_harness(harness) is None:
                    continue

                def agent_harness(agent_id: str) -> str | None:
                    return agent_lookup(agent_id, owner, request.app.state)[0]

                resolution = resolve_calling(
                    explicit={"harness_override": harness},
                    explicit_fields={"harness_override"},
                    project_config=project.config,
                    master=master,
                    host_id=host.host_id,
                    agent_harness=agent_harness,
                )
                catalog = catalog_map.get((host.host_id, harness))
                agent_name = agent_lookup(resolution.agent_id, owner, request.app.state)[1]
                problems = check_calling_defaults(
                    resolution=resolution,
                    host_id=host.host_id,
                    host_name=host.name,
                    configured_harnesses=host.configured_harnesses,
                    catalog=catalog,
                    agent_name=agent_name,
                    project_name=project.name,
                    path_label="server-side creates",
                )
                rows.append(
                    {
                        "host_id": host.host_id,
                        "harness": harness,
                        "effective": {
                            "agent_id": resolution.agent_id,
                            "model": resolution.model,
                            "effort": resolution.effort,
                            "speed": resolution.speed,
                            "permission": resolution.permission,
                        },
                        "sources": resolution.sources,
                        "fallbacks": {
                            field: "agent_or_harness_default"
                            for field in _FIELDS
                            if resolution.sources[field] == "none"
                        },
                        "problems": problems,
                        "catalog": _catalog_row(catalog, host_registry).model_dump()
                        if catalog is not None
                        else None,
                        "available": {
                            "models": catalog["models"] if catalog is not None else [],
                            "efforts": sorted(
                                (
                                    _catalog_efforts(catalog["models"], resolution.model)
                                    if catalog and isinstance(catalog.get("models"), list)
                                    else None
                                )
                                or efforts_for_harness(harness)
                                or ()
                            ),
                            "speed": _available_speed(harness, resolution.model, catalog),
                            "permission": sorted(mode_values(harness, "permission", "default")),
                        },
                    }
                )
        return {
            "project_id": project.id,
            "owner_user_id": project.user_id,
            "revision": revision,
            "config": project.config,
            "entries": [{"host_id": row.host_id, "workspace": row.workspace} for row in entries],
            "default_host": asdict(chosen),
            "roots": [asdict(root) for root in roots if root.host_id in host_map],
            "hosts": [
                {
                    "host_id": host.host_id,
                    "name": host.name,
                    "online": host_registry.get(host.host_id) is not None,
                    "configured_harnesses": host.configured_harnesses,
                }
                for host in hosts
            ],
            "agents": agent_choices,
            "rows": rows,
            "applies_to": "future sessions",
        }

    @router.get("/projects/{project_id}/settings")
    async def read_settings(
        request: Request,
        project_id: str,
        host_id: str | None = Query(default=None),
        harness: str | None = Query(default=None),
    ) -> dict[str, Any]:
        project, entries, revision = await snapshot(request, project_id)
        return await render(
            request, project, entries, revision, host_filter=host_id, harness_filter=harness
        )

    @router.put("/projects/{project_id}/settings")
    async def update_settings(
        request: Request,
        project_id: str,
        body: _SettingsPatch,
    ) -> dict[str, Any]:
        project, entries, revision = await snapshot(request, project_id)
        if body.expected_revision != revision:
            raise OmnigentError(
                "Project settings changed. Read again before updating.", code=ErrorCode.CONFLICT
            )
        next_config, named = _apply_patch(project.config, body)
        host_map, catalog_map, _hosts, master = await context(project)
        owner = project.user_id or RESERVED_USER_LOCAL
        entry_change = body.host is not None and "workspace" in body.host.model_fields_set
        entry_host_id = body.host.host_id if body.host else None
        entry_workspace = body.host.workspace if entry_change and body.host else None
        checked = None
        if body.host is not None and body.host.host_id not in host_map:
            raise OmnigentError("Host not found", code=ErrorCode.NOT_FOUND)
        selected_host = next_config.get("host_id")
        if (
            "project.host_id" in named
            and selected_host not in (None, "__sandbox__")
            and selected_host not in host_map
        ):
            raise _invalid("project.host_id", "choose an owned host")
        if entry_change and entry_workspace is not None and body.host is not None:
            entry_workspace, _name, checked = await _resolve_stored_workspace(
                user_id=project.user_id,
                host_id=body.host.host_id,
                workspace=entry_workspace,
                kind="project",
                host_store=host_store,
                host_registry=host_registry,
            )
            if checked and _is_managed_worktree_path(entry_workspace):
                raise _invalid(
                    "host.workspace", "a project directory cannot be inside a worktree folder"
                )
        if "project.host_id" in named or "host.workspace" in named:
            selected_entry = next(
                (entry.workspace for entry in entries if entry.host_id == selected_host), None
            )
            if entry_change and entry_host_id == selected_host:
                selected_entry = entry_workspace
            if selected_entry is not None:
                next_config["workspace"] = selected_entry
            elif "project.host_id" in named or entry_change:
                next_config.pop("workspace", None)
        # A changed Agent must not keep an All hosts model for another harness.
        if (
            "project.agent_id" in named
            and "project.model" not in named
            and next_config.get("model")
        ):
            previous = agent_lookup(project.config.get("agent_id"), owner, request.app.state)[0]
            current = agent_lookup(next_config.get("agent_id"), owner, request.app.state)[0]
            if previous is None or current is None or previous != current:
                raise _invalid("project.model", "set a compatible model or explicitly clear it")

        def require_harness(harness: str, field: str, host_id: str | None) -> None:
            if host_id is None:
                return
            if host_id not in host_map:
                raise _invalid(field, "choose an owned host to validate this setting")
            readiness = host_map[host_id].configured_harnesses or {}
            key = "acp" if harness.startswith("acp:") and harness not in readiness else harness
            if key not in readiness:
                return
            available, reason = reported_harness_availability(key, readiness)
            capability = capability_for_harness(harness)
            advisory_auth = (
                reason == HARNESS_NEEDS_AUTH
                and capability is not None
                and capability.integration_mode == IntegrationMode.SDK_IN_PROCESS
            )
            if available is False and not advisory_auth:
                raise _invalid(field, f"{harness} is unavailable on this host ({reason})")

        def require_agent(agent_id: str | None, field: str, host_id: str | None) -> str:
            harness, _name = agent_lookup(agent_id, owner, request.app.state)
            if harness is None:
                raise _invalid(field, "choose an available project-owner Agent")
            require_harness(harness, field, host_id)
            return harness

        if "project.agent_id" in named and next_config.get("agent_id"):
            require_agent(
                next_config["agent_id"],
                "project.agent_id",
                selected_host if selected_host != "__sandbox__" else None,
            )
        if "host.agent_id" in named and body.host and body.host.agent_id:
            require_agent(body.host.agent_id, "host.agent_id", body.host.host_id)
        if "project.model" in named and next_config.get("model"):
            if selected_host not in host_map:
                raise _invalid(
                    "project.model", "choose an owned default host to validate its model"
                )
            harness = require_agent(next_config.get("agent_id"), "project.agent_id", selected_host)
            catalog = catalog_map.get((selected_host, harness))
            if not catalog or catalog.get("error") or not catalog.get("models"):
                raise _invalid(
                    "project.model", "sync this host's model catalog before selecting a model"
                )
            if next_config["model"] not in _catalog_model_ids(catalog["models"]):
                raise _invalid("project.model", "model is not offered by the selected host")
        if body.host and body.host.harnesses:
            for harness, patch in body.host.harnesses.items():
                supplied = patch.model_fields_set
                if not supplied:
                    continue
                capability = capability_for_harness(harness)
                if capability is None or capability.calling_defaults is None:
                    raise _invalid(f"host.harnesses.{harness}", "calling defaults are unavailable")
                if any(getattr(patch, field) is not None for field in supplied):
                    require_harness(harness, f"host.harnesses.{harness}", body.host.host_id)
                catalog = catalog_map.get((body.host.host_id, harness))

                def agent_harness(agent_id: str) -> str | None:
                    return agent_lookup(agent_id, owner, request.app.state)[0]

                resolved = resolve_calling(
                    explicit={"harness_override": harness},
                    explicit_fields={"harness_override"},
                    project_config=next_config,
                    master=master,
                    host_id=body.host.host_id,
                    agent_harness=agent_harness,
                )
                for field in supplied:
                    value = getattr(patch, field)
                    if value is None:
                        continue
                    path = f"host.harnesses.{harness}.{field}"
                    if field == "model":
                        if not catalog or catalog.get("error") or not catalog.get("models"):
                            raise _invalid(
                                path, "sync this host's model catalog before selecting a model"
                            )
                        if value not in _catalog_model_ids(catalog["models"]):
                            raise _invalid(path, "model is not offered by this host")
                    elif field == "effort":
                        if resolved.model and (
                            not catalog or catalog.get("error") or not catalog.get("models")
                        ):
                            raise _invalid(
                                path, "sync this host's model catalog to validate effort"
                            )
                        offered = (
                            _catalog_efforts(catalog["models"], resolved.model)
                            if catalog
                            else None
                        )
                        if value not in (offered or efforts_for_harness(harness) or ()):
                            raise _invalid(path, "effort is unavailable for the selected model")
                    elif not valid_mode(value, harness, field, "default"):
                        raise _invalid(path, "mode is unavailable for this harness")
                    elif field == "speed" and value not in _available_speed(
                        harness, resolved.model, catalog
                    ):
                        raise _invalid(path, "speed is unavailable for the selected model")

        def resolved_for(config: dict[str, Any], host_id: str, harness: str) -> CallingResolution:
            return resolve_calling(
                explicit={"harness_override": harness},
                explicit_fields={"harness_override"},
                project_config=config,
                master=master,
                host_id=host_id,
                agent_harness=lambda agent_id: agent_lookup(agent_id, owner, request.app.state)[0],
            )

        # Clearing a model or editing All hosts can change several inherited rows.
        for host_id in host_map:
            for harness in model_option_harnesses():
                previous = resolved_for(project.config, host_id, harness)
                current = resolved_for(next_config, host_id, harness)
                if previous.model == current.model:
                    continue
                patch = (
                    (body.host.harnesses or {}).get(harness)
                    if body.host is not None and body.host.host_id == host_id
                    else None
                )
                catalog = catalog_map.get((host_id, harness))
                for field in ("effort", "speed"):
                    value = getattr(current, field)
                    if value is None or (
                        patch is not None
                        and field in patch.model_fields_set
                        and getattr(patch, field) is None
                    ):
                        continue
                    if field == "effort":
                        offered = (
                            (
                                _catalog_efforts(catalog["models"], current.model)
                                if catalog
                                else None
                            )
                            or efforts_for_harness(harness)
                            or ()
                        )
                    else:
                        offered = _available_speed(harness, current.model, catalog)
                    if value not in offered:
                        raise _invalid(
                            f"calling_defaults.{host_id}.{harness}.{field}",
                            f"change or clear {field} for the selected model",
                        )
        initial_agent_id = None
        initialization_notice = None
        if (
            entry_change
            and entry_workspace is not None
            and body.host
            and "project.agent_id" not in named
            and "host.agent_id" not in named
        ):
            initial_agent_id, initialization_notice = await resolve_project_host_template(
                request,
                replace(project, config=next_config),
                body.host.host_id,
                project.user_id,
                binding_store,
            )
        (
            updated,
            updated_entries,
            new_revision,
            config_changed,
            entry_changed,
            seeded,
        ) = await asyncio.to_thread(
            binding_store.update_settings,
            project_id,
            caller_id=require_user(request, auth_provider),
            is_admin=bool(
                project.user_id != require_user(request, auth_provider)
                and permission_store is not None
                and await asyncio.to_thread(
                    permission_store.is_admin, require_user(request, auth_provider)
                )
            ),
            expected_revision=body.expected_revision,
            config=next_config,
            entry_host_id=entry_host_id,
            entry_workspace=entry_workspace,
            entry_change=entry_change,
            initial_agent_id=initial_agent_id,
        )
        response = await render(request, updated, updated_entries, new_revision)
        response["changed_fields"] = [
            field
            for field in named
            if (field == "host.workspace" and entry_changed)
            or (field != "host.workspace" and config_changed)
        ]
        response["derived_fields"] = (
            ["project.workspace"]
            if project.config.get("workspace") != updated.config.get("workspace")
            else []
        )
        response["changed"] = config_changed or entry_changed
        if entry_change:
            response["entry"] = {
                "host_id": entry_host_id,
                "workspace": entry_workspace,
                "checked": checked,
                "changed": entry_changed,
                "host_agent_id": seeded,
                "initialization_notice": initialization_notice,
            }
        if entry_changed and entry_workspace is not None and checked and entry_host_id is not None:
            post_bind = await run_post_bind_request(
                host_registry=host_registry,
                host_id=entry_host_id,
                project_id=project_id,
                binding_name="",
                binding_id=f"entry:{project_id}",
                revision=0,
                repository_name="",
                workspace=entry_workspace,
                is_primary=True,
                context_manifest_path=_DEFAULT_MANIFEST_PATH,
                trigger="entry",
            )
            _record_setup_outcome(
                project_id=project_id,
                host_id=entry_host_id,
                kind="entry",
                name=None,
                result=post_bind,
            )
            response["entry"]["post_bind"] = post_bind
        if config_changed or entry_changed:
            announce_projects_changed(project.user_id)
        return response

    return router
