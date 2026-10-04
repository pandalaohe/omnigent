"""Shared validation for creating session-like conversations.

The interactive session route and scheduled tasks both persist values that
eventually cross runner or host boundaries. Keep the security-sensitive checks
in one place so scheduled task create/update/fire cannot drift from
``POST /v1/sessions``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from omnigent.calling_defaults import (
    CallingResolution,
    check_calling_defaults,
    load_master,
    resolve_calling,
)
from omnigent.entities.project import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.models.model_override import validate_model_override
from omnigent.runtime.agent_cache import AgentCache
from omnigent.sdk_permission_modes import CODEX_SDK_APPROVAL_MODES
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, local_single_user_enabled
from omnigent.server.feature_flags import FeatureFlags
from omnigent.server.project_placement import (
    checkout_on_host,
    default_host,
    host_roots,
    load_bindings,
    load_eligible_host_ids,
    load_entries,
    root_on_host,
)
from omnigent.server.routes._auth_helpers import require_access
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
from omnigent.stores.host_store import HostStore, host_is_live
from omnigent.stores.project_host_binding_store import ProjectHostBindingStore
from omnigent.stores.project_store import ProjectStore
from omnigent.util.reasoning_effort import EFFORT_VALUES, validate_effort

if TYPE_CHECKING:
    from starlette.requests import Request

_logger = logging.getLogger(__name__)

#: Request fields the calling-defaults chain may fill; presence (even a
#: JSON ``null``) always wins over a default.
_CALLING_FIELDS = frozenset({"agent_id", "harness_override", "model_override", "reasoning_effort"})


@dataclass(frozen=True)
class ProjectCreateResolution:
    """Project-aware request values after defaulting.

    :param entry: The project's entry on the resolved host, or ``None``
        (no entry, no host, or the sandbox host).
    :param checkout: The repository a worktree would source from on the
        resolved host, or ``None``.
    :param worktree_entry: Where a worktree this create cuts is placed —
        the request's project's entry on the resolved host, or for a child
        that names no project, its parent project's.
    :param worktree_checkout: Where a worktree this create cuts is sourced —
        the request's project's checkout on the resolved host, or for a
        child that names no project, its parent project's.
    """

    body: Any
    project_id: str | None = None
    entry: str | None = None
    checkout: str | None = None
    worktree_entry: str | None = None
    worktree_checkout: str | None = None


async def resolve_create_calling(
    *,
    request: Request | None,
    user_id: str | None,
    project: Project | None,
    host_id: str | None,
    explicit: dict[str, Any],
    explicit_fields: set[str],
    path_label: str,
    parent_session_id: str | None = None,
) -> CallingResolution:
    """Resolve one create's agent / model / effort from a request's stores.

    The route-facing wrapper over :func:`resolve_create_calling_stores`; it
    reads the stores wired on ``request.app.state`` and keeps every create
    route on one call shape. Non-request callers (the assignment
    coordinator) call the core directly with explicit stores.

    :param request: The create request whose ``app.state`` carries the
        stores; ``None`` degrades to the project layers only.
    :param user_id: Owner of the master table and the host.
    :param project: Project whose per-host set applies, or ``None``.
    :param host_id: Host the caller will place the session on, or ``None``.
    :param explicit: Request values keyed ``agent_id`` / ``harness_override``
        / ``model_override`` / ``reasoning_effort``.
    :param explicit_fields: The subset of those keys the request supplied.
    :param path_label: The create path named in the library-agent refusal,
        e.g. ``"session_create"``.
    :param parent_session_id: The caller's parent session, named when a child
        has no project and no default agent.
    :returns: The resolved calling triple.
    :raises OmnigentError: ``INVALID_INPUT`` for any refused default.
    """
    state = getattr(getattr(request, "app", None), "state", None)
    return await resolve_create_calling_stores(
        user_id=user_id,
        project=project,
        host_id=host_id,
        explicit=explicit,
        explicit_fields=explicit_fields,
        path_label=path_label,
        parent_session_id=parent_session_id,
        host_store=getattr(state, "host_store", None),
        agent_store=getattr(state, "agent_store", None),
        agent_cache=getattr(state, "agent_cache", None),
        preferences_store=getattr(state, "user_preferences_store", None),
        catalog_store=getattr(state, "host_model_catalog_cache_store", None),
    )


async def resolve_create_calling_stores(
    *,
    user_id: str | None,
    project: Project | None,
    host_id: str | None,
    explicit: dict[str, Any],
    explicit_fields: set[str],
    path_label: str,
    parent_session_id: str | None = None,
    host_store: HostStore | None = None,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
    preferences_store: Any | None = None,
    catalog_store: HostModelCatalogCacheStore | None = None,
) -> CallingResolution:
    """Resolve one create's agent / model / effort through the shared chain.

    Runs the pure :func:`omnigent.calling_defaults.resolve_calling` with the
    given stores, then applies the create-time K5 refusals: no resolved
    agent when the request omitted one, a default that names a saved joint
    (``ca_``) agent, a default agent whose harness the host reports as not
    launchable, and a default-sourced model / effort the cached catalog does
    not offer. Explicit request values keep today's validation only.

    :param user_id: Owner of the master table and the host.
    :param project: Project whose per-host set applies, or ``None``.
    :param host_id: Host the caller will place the session on, or ``None``.
    :param explicit: Request values keyed ``agent_id`` / ``harness_override``
        / ``model_override`` / ``reasoning_effort``.
    :param explicit_fields: The subset of those keys the request supplied.
    :param path_label: The create path named in the library-agent refusal,
        e.g. ``"session_create"``.
    :param parent_session_id: The caller's parent session, named when a child
        has no project and no default agent.
    :param host_store: Host registrations for the readiness check, or
        ``None`` when unknown.
    :param agent_store: Agent store resolving an id to its harness.
    :param agent_cache: Cache loading the agent's parsed spec.
    :param preferences_store: Preferences store holding the owner's master
        table, or ``None``.
    :param catalog_store: Cached host model catalogs for the offered check,
        or ``None``.
    :returns: The resolved calling triple.
    :raises OmnigentError: ``INVALID_INPUT`` for any refused default.
    """
    from omnigent.server.library_agent_launch import is_library_agent_id
    from omnigent.server.routes._sessions.orchestration import _create_resolved_harness

    host = None
    if host_id is not None and host_store is not None:
        host = await asyncio.to_thread(host_store.get_host, host_id)

    def get_agent(agent_id: str) -> Any | None:
        # Agent lookups only enrich the resolution (harness, display name);
        # a store fault degrades to the project layers rather than failing a
        # placement that could still launch.
        if agent_store is None:
            return None
        try:
            return agent_store.get(agent_id)
        except Exception:  # noqa: BLE001
            _logger.warning("Agent lookup failed for %s", agent_id, exc_info=True)
            return None

    def agent_harness(effective_agent_id: str) -> str | None:
        # A saved library Agent has no ``agents`` row; the id would not even
        # bind against the UUID-typed lookup.
        if is_library_agent_id(effective_agent_id):
            return None
        agent = get_agent(effective_agent_id)
        if agent is None:
            return None
        return _create_resolved_harness(agent, None, agent_cache)

    master = await load_master(user_id, preferences_store)
    resolution = await asyncio.to_thread(
        resolve_calling,
        explicit=explicit,
        explicit_fields=explicit_fields,
        project_config=project.config if project is not None else None,
        master=master,
        host_id=host_id,
        agent_harness=agent_harness,
    )

    if resolution.agent_id is None and "agent_id" not in explicit_fields:
        host_label = host.name if host is not None and host.name else host_id or "unknown"
        if project is not None:
            raise OmnigentError(
                f"Project '{project.name}' has no default agent on host '{host_label}'. "
                "Pass agent_id, or set it in Project settings › Hosts.",
                code=ErrorCode.INVALID_INPUT,
            )
        if parent_session_id is not None:
            raise OmnigentError(
                "agent_id is required: the parent session has no project, so there is no "
                "host default agent. Pass agent_id, or file the parent session in a project.",
                code=ErrorCode.INVALID_INPUT,
            )
        raise OmnigentError("agent_id is required", code=ErrorCode.INVALID_INPUT)

    agent = None
    if resolution.agent_id is not None and not is_library_agent_id(resolution.agent_id):
        agent = await asyncio.to_thread(get_agent, resolution.agent_id)
    catalog: dict[str, Any] | None = None
    if host_id is not None and resolution.harness is not None and catalog_store is not None:
        records = await asyncio.to_thread(catalog_store.list, [host_id])
        record = next((row for row in records if row["harness"] == resolution.harness), None)
        if record is not None:
            catalog = {
                "models": record["models"],
                "error": record["error"],
                "fetched_at": record["fetched_at"],
            }
    problems = check_calling_defaults(
        resolution=resolution,
        host_id=host_id or "",
        host_name=host.name if host is not None and host.name else None,
        configured_harnesses=host.configured_harnesses if host is not None else None,
        catalog=catalog,
        agent_name=getattr(agent, "name", None),
        project_name=project.name if project is not None else None,
        path_label=path_label,
    )
    if problems:
        raise OmnigentError(problems[0]["message"], code=ErrorCode.INVALID_INPUT)
    return resolution


async def resolve_project_session_create(
    *,
    body: Any,
    user_id: str | None,
    project_store: ProjectStore | None,
    binding_store: ProjectHostBindingStore | None = None,
    feature_flags: FeatureFlags | None = None,  # noqa: ARG001 — bindings are no longer flag-gated
    host_store: HostStore | None = None,
    fill_host: bool = False,
    request: Request | None = None,
    apply_calling_defaults: bool = False,
    parent_project: Project | None = None,
    parent_host_id: str | None = None,
    calling_path_label: str = "POST /v1/sessions",
) -> ProjectCreateResolution:
    """Apply opt-in project defaults before any create-side validation.

    Field presence, rather than value, controls defaulting.  Consequently an
    explicit JSON ``null`` remains explicit and is never replaced by a project
    hint.  Unknown and foreign projects deliberately share one 404 response.

    With *apply_calling_defaults* the host resolves first (the per-host
    default agent needs it) and agent / model / effort come from
    :func:`resolve_create_calling`; only omitted fields are filled. A child
    with no ``project_id`` uses *parent_project* for the chain and
    *parent_host_id* as its host when the request names none.
    """
    fields_set = set(body.model_fields_set)
    project_id = getattr(body, "project_id", None)
    explicit_fields = fields_set & _CALLING_FIELDS
    project: Project | None = None
    if "project_id" in fields_set and project_id is not None:
        if project_store is None:
            raise OmnigentError(
                "Project not found",
                code=ErrorCode.NOT_FOUND,
            )
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)

    updates: dict[str, Any] = {}
    bindings: Any = []
    entries: Any = []
    if project is not None:
        config = project.config
        # The legacy agent_id fill moved into the calling chain below; config
        # keeps supplying git, whose semantics have no chain.
        if "git" not in fields_set and "git" in config and "git" in body.__class__.model_fields:
            updates["git"] = config["git"]
        bindings = await load_bindings(binding_store, project.id)
        entries = await load_entries(binding_store, project.id)
        if (
            fill_host
            and "host_id" not in fields_set
            and "workspace" not in fields_set
            and body.parent_session_id is None
            and "host_type" not in fields_set
        ):
            roots = host_roots(project, bindings, entries=entries)
            eligible = await load_eligible_host_ids(
                host_store, user_id, (root.host_id for root in roots)
            )
            chosen = default_host(project, roots, eligible_host_ids=eligible)
            if chosen.reason == "ambiguous":
                names = ", ".join(
                    root.host_id for root in roots if eligible is None or root.host_id in eligible
                )
                raise OmnigentError(
                    f"Project '{project.name}' has a directory on several hosts "
                    f"({names}). Pass host_id.",
                    code=ErrorCode.INVALID_INPUT,
                )
            if chosen.host_id is not None:
                updates["host_id"] = chosen.host_id
        if "workspace" not in fields_set:
            host_id = updates.get("host_id", body.host_id)
            if host_id is not None:
                root = root_on_host(project, bindings, host_id, entries=entries)
                if root is None:
                    raise OmnigentError(
                        f"Project '{project.name}' has no directory on host '{host_id}'. "
                        "Pass workspace, or set this host's directory in the project settings.",
                        code=ErrorCode.INVALID_INPUT,
                    )
                updates["workspace"] = root.workspace
            elif "workspace" in config and "workspace" in body.__class__.model_fields:
                updates["workspace"] = config["workspace"]

    if apply_calling_defaults:
        calling_host_id = updates.get("host_id", getattr(body, "host_id", None)) or parent_host_id
        resolution = await resolve_create_calling(
            request=request,
            user_id=user_id,
            project=project if project is not None else parent_project,
            host_id=calling_host_id,
            explicit=body.model_dump(),
            explicit_fields=explicit_fields,
            path_label=calling_path_label,
            parent_session_id=getattr(body, "parent_session_id", None),
        )
        if "agent_id" not in fields_set and resolution.agent_id is not None:
            updates["agent_id"] = resolution.agent_id
        # Routing-on creates own model and effort per turn: a pinned default
        # would silently disable the router for the whole session (a pinned
        # model wins over the router). The agent fill above is unaffected.
        routing_requested = (
            getattr(body, "cost_control_mode_override", None) == "on"
            or getattr(body, "harness_override", None) == "auto"
        )
        if not routing_requested:
            if "model_override" not in fields_set and resolution.model is not None:
                updates["model_override"] = resolution.model
            if "reasoning_effort" not in fields_set and resolution.effort is not None:
                updates["reasoning_effort"] = resolution.effort
    elif project is None:
        if getattr(body, "agent_id", None) is None and "agent_id" in body.__class__.model_fields:
            raise OmnigentError("agent_id is required", code=ErrorCode.INVALID_INPUT)
        return ProjectCreateResolution(body=body)

    resolved_data = body.model_dump()
    resolved_data.update(updates)
    # Re-validate project hints because config is intentionally stored as
    # opaque JSON and may not match the session-create field types.
    try:
        resolved = body.__class__.model_validate(resolved_data)
    except ValidationError as exc:
        first = exc.errors(include_context=False)[0]
        field = ".".join(str(part) for part in first.get("loc", ())) or "configuration"
        raise OmnigentError(
            f"Invalid project config field {field!r}: {first['msg']}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc

    if getattr(resolved, "agent_id", None) is None and "agent_id" in body.__class__.model_fields:
        raise OmnigentError("agent_id is required", code=ErrorCode.INVALID_INPUT)
    if getattr(resolved, "git", None) is not None and getattr(resolved, "host_id", None) is None:
        raise OmnigentError(
            "git worktree creation requires host_id",
            code=ErrorCode.INVALID_INPUT,
        )

    # The entry and checkout on the resolved host, whether the workspace came
    # from the project or the caller sent it explicitly; placement needs both.
    entry: str | None = None
    checkout: str | None = None
    resolved_host_id = getattr(resolved, "host_id", None)
    if resolved_host_id is not None and resolved_host_id != "__sandbox__":
        entry = next((row.workspace for row in entries if row.host_id == resolved_host_id), None)
        checkout = checkout_on_host(bindings, entries, resolved_host_id)

    # Worktree placement and sourcing follow the request's project; a child
    # that named no project inherits its parent project's instead, so its
    # worktree still lands under the project's entry.
    worktree_entry, worktree_checkout = entry, checkout
    if (
        project is None
        and parent_project is not None
        and resolved_host_id is not None
        and resolved_host_id != "__sandbox__"
    ):
        p_bindings = await load_bindings(binding_store, parent_project.id)
        p_entries = await load_entries(binding_store, parent_project.id)
        worktree_entry = next(
            (row.workspace for row in p_entries if row.host_id == resolved_host_id), None
        )
        worktree_checkout = checkout_on_host(p_bindings, p_entries, resolved_host_id)

    return ProjectCreateResolution(
        body=resolved,
        project_id=project_id,
        entry=entry,
        checkout=checkout,
        worktree_entry=worktree_entry,
        worktree_checkout=worktree_checkout,
    )


# Claude Code's ``--permission-mode`` launch vocabulary — every value the CLI
# accepts at start, not just the shift+tab-switchable subset. ``dontAsk`` and
# ``bypassPermissions`` are launch-only (rejected on a running-session PATCH),
# but a scheduled task launches a fresh session each fire, so all of them are
# valid here. Mirrors the frontend's ``CLAUDE_NATIVE_PERMISSION_MODES``.
CLAUDE_NATIVE_LAUNCH_PERMISSION_MODES: frozenset[str] = frozenset(
    {"default", "auto", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}
)


# Only claude-native accepts the ``--permission-mode`` launch arg; SDK harnesses
# carry matching permission modes as labels. Other native CLIs (codex / cursor /
# …) use different flags, so injecting ``--permission-mode`` there would be an
# unknown flag that breaks the launch.
async def validate_permission_mode_agent_support(
    *,
    permission_mode: str | None,
    agent: Any,
    agent_cache: AgentCache | None,
) -> None:
    """Reject a ``permission_mode`` unsupported by the agent's harness.

    Mirrors the web dialog's capability gate on the server so the REST endpoint
    and agent tools enforce the same rule the UI does: ``claude-native`` and
    ``claude-sdk`` agents use the Claude launch vocabulary, while ``codex`` SDK
    agents use approval presets. Without this, a task on a codex / cursor agent
    could persist a mode that would be an unknown ``--permission-mode`` flag on
    that native CLI; a mismatched SDK mode would have no valid label semantics.

    This is an early, friendly 4xx at persist time. A ``None`` mode is always
    allowed (nothing to gate). When the harness cannot be resolved (no bundle /
    cache / a load error), this is a no-op rather than a rejection: the value has
    already passed the vocabulary allowlist, and the fire path's launch-arg
    derivation is itself harness-gated fail-safe (it injects ``--permission-mode``
    ONLY for a confirmed ``claude-native`` agent, omitting it otherwise), so a
    non-Claude mode can never actually reach the launch args regardless. SDK
    labels are stamped only for a confirmed harness and matching vocabulary.
    """
    if permission_mode is None or agent is None:
        return
    if agent_cache is None or getattr(agent, "bundle_location", None) is None:
        return
    from omnigent.harness_aliases import canonicalize_harness

    try:
        loaded = await asyncio.to_thread(agent_cache.load, agent.id, agent.bundle_location)
        executor = getattr(loaded.spec, "executor", None)
        raw_harness = None
        if executor is not None:
            raw_harness = executor.config.get("harness") or executor.type
        harness = canonicalize_harness(raw_harness) or raw_harness
    except Exception:
        # A spec that won't load fails elsewhere (workspace validation / fire);
        # don't turn an unrelated load error into a permission_mode rejection.
        _logger.exception("Failed to load agent spec for permission_mode gating")
        return
    if harness is None:
        return
    validate_permission_mode_harness_support(permission_mode=permission_mode, harness=harness)


def validate_permission_mode_harness_support(
    *,
    permission_mode: str | None,
    harness: str | None,
) -> None:
    """Reject a ``permission_mode`` its resolved *harness* does not support.

    Split from :func:`validate_permission_mode_agent_support` so a caller that
    already holds the parsed spec — the scheduled validation of a saved library
    Agent's bundle — runs the same gate without a second agent-cache load.
    """
    if permission_mode is None or harness is None:
        return
    modes = (
        CODEX_SDK_APPROVAL_MODES
        if harness == "codex"
        else CLAUDE_NATIVE_LAUNCH_PERMISSION_MODES
        if harness in ("claude-native", "claude-sdk")
        else frozenset()
    )
    if permission_mode not in modes:
        raise OmnigentError(
            f"permission_mode {permission_mode!r} is not supported for {harness!r} agents",
            code=ErrorCode.INVALID_INPUT,
        )


def validate_session_permission_mode(permission_mode: str | None) -> str | None:
    """Validate a persisted per-task permission mode shared by schedules.

    A scheduled task fires a fresh native session each run, so the whole launch
    vocabulary is allowed (including the launch-only ``dontAsk`` /
    ``bypassPermissions``). For Claude native, the value reaches the native CLI
    as the ``--permission-mode`` argv element the fire path derives; Claude SDK
    uses the same vocabulary in a session label, and Codex SDK accepts its own
    approval presets. Reject anything outside these known sets before a row
    persists it; the resolved harness is checked separately.
    """
    if permission_mode is None:
        return None
    allowed = CLAUDE_NATIVE_LAUNCH_PERMISSION_MODES | CODEX_SDK_APPROVAL_MODES
    if permission_mode not in allowed:
        raise OmnigentError(
            f"invalid permission_mode: {permission_mode!r} (expected one of {sorted(allowed)})",
            code=ErrorCode.INVALID_INPUT,
        )
    return permission_mode


def validate_session_model_metadata(
    *,
    model_override: str | None,
    reasoning_effort: str | None,
) -> tuple[str | None, str | None]:
    """Validate persisted model metadata shared by sessions and schedules."""
    # The persisted override reaches native CLIs as a ``--model`` argv element
    # at terminal launch, so reject shell-/flag-shaped values before any
    # session row or scheduled task row persists it.
    validated_model: str | None = None
    if model_override is not None:
        try:
            validated_model = validate_model_override(model_override)
        except ValueError as exc:
            raise OmnigentError(
                f"invalid model_override: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

    # Persisted effort reaches native CLIs as a ``--effort`` argv element at
    # terminal launch (and SDK harnesses via the spawn env). Validate against
    # the shared vocabulary before any row persists it; provider-specific
    # support is enforced downstream at launch, mirroring the multipart
    # metadata create path.
    validated_effort: str | None = None
    if reasoning_effort is not None:
        try:
            validated_effort = validate_effort(
                reasoning_effort,
                "session metadata",
                EFFORT_VALUES,
            )
        except ValueError as exc:
            raise OmnigentError(
                f"invalid reasoning_effort: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
    return validated_model, validated_effort


async def validate_session_agent(
    *,
    user_id: str | None,
    agent_id: str,
    agent_store: AgentStore,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
) -> Any:
    """Load a bindable agent and authorize session-scoped agent access."""
    agent = await asyncio.to_thread(agent_store.get, agent_id)
    if agent is None:
        raise OmnigentError(
            f"Agent not found: {agent_id!r}",
            code=ErrorCode.NOT_FOUND,
        )

    # Session-scoped agents belong to a specific session. The caller must have
    # at least READ access to that owning session — otherwise they can execute
    # another user's private agent by guessing the raw agent id.
    if agent.session_id is not None:
        # Single-user servers persist the local owner as NULL (scheduled tasks
        # store user_id=None), but session grants are keyed by the "local"
        # sentinel — the same identity POST /v1/sessions checks. Resolve None to
        # it here so a session-scoped agent authorizes, instead of tripping the
        # require_access unauthenticated guard. Multi-user servers leave None as
        # None, which still correctly 401s.
        access_user = user_id
        if access_user is None and local_single_user_enabled():
            access_user = RESERVED_USER_LOCAL
        await require_access(
            access_user,
            agent.session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
    return agent


def _require_absolute_host_workspace(workspace: str | None) -> str:
    """
    Enforce the shape checks shared by every host-workspace validation.

    :param workspace: Caller-supplied workspace, or ``None``.
    :returns: The workspace, guaranteed present and absolute.
    :raises OmnigentError: ``INVALID_INPUT`` when the workspace is
        missing or not an absolute path.
    """
    if workspace is None:
        raise OmnigentError(
            "workspace required when host_id is set",
            code=ErrorCode.INVALID_INPUT,
        )
    from omnigent.server.routes._workspace_validation import _is_windows_absolute_path

    if not workspace.startswith("/") and not _is_windows_absolute_path(workspace):
        raise OmnigentError(
            "workspace must be an absolute path starting with /",
            code=ErrorCode.INVALID_INPUT,
        )
    return workspace


async def _authorize_host_for_workspace(
    *,
    user_id: str | None,
    host_id: str,
    host_store: Any | None,
    host_registry: Any,
) -> str | None:
    """
    Authorize host ownership and classify a wrong-replica landing.

    Ownership runs FIRST — before any agent-spec load or the
    ``host.stat`` round-trip the caller performs next. A non-owner must
    be rejected (403/404 via the shared ``resolve_host_owner``) before
    we touch the host or even read the agent bundle (cross-user host
    probe).

    :param user_id: Authenticated caller, or ``None`` when auth is off.
    :param host_id: Target host id.
    :param host_store: Persistent host registrations; ``None`` skips
        the ownership check (minimal test wirings).
    :param host_registry: Live host tunnels on this replica.
    :returns: The host's display name for error messages, or ``None``.
    :raises OmnigentError: ``WRONG_REPLICA`` when the host is live but
        its tunnel is on another replica.
    """
    from omnigent.server.routes._host_launch import resolve_host_owner

    if host_store is None:
        return None
    host = await asyncio.to_thread(
        resolve_host_owner,
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
    )
    # Wrong-replica classification, same as the /v1/hosts/* endpoints and
    # RunnerRouter: validate_workspace does a local host_registry miss
    # → "host is offline" (invalid_input), which the client can't recover
    # from. If the host is live per the store but its tunnel isn't on this
    # replica, the create landed on the wrong replica — surface WRONG_REPLICA
    # so the client re-addresses WITHOUT the key. A genuinely offline host
    # falls through to the invalid_input case. Both are 400; the distinct
    # code, not the status, is what tells the client to re-address rather
    # than give up. Safe to raise here: workspace validation runs BEFORE
    # create_conversation, so no orphan row is left.
    if host_registry is not None and host_registry.get(host_id) is None and host_is_live(host):
        raise OmnigentError(
            f"host {host.name or host_id!r} is on another replica; retry",
            code=ErrorCode.WRONG_REPLICA,
        )
    return host.name


async def _canonical_workspace_or_invalid_input(
    *,
    host_registry: Any,
    host_id: str,
    workspace: str,
    spec_cwd: str | None,
    host_name: str | None,
) -> str:
    """Run the seven-step validation, mapping failures to ``INVALID_INPUT``."""
    from omnigent.server.routes._workspace_validation import (
        WorkspaceValidationError,
        validate_workspace,
    )

    try:
        return await validate_workspace(
            host_registry=host_registry,
            host_id=host_id,
            workspace=workspace,
            spec_cwd=spec_cwd,
            host_name_for_errors=host_name,
        )
    except WorkspaceValidationError as exc:
        raise OmnigentError(
            exc.message,
            code=ErrorCode.INVALID_INPUT,
        ) from exc


async def validate_existing_host_workspace(
    *,
    user_id: str | None,
    host_id: str,
    workspace: str | None,
    agent: Any,
    agent_cache: AgentCache | None,
    host_store: Any | None,
    host_registry: Any | None,
) -> str:
    """Validate a connected-host workspace against the agent's os_env boundary."""
    workspace = _require_absolute_host_workspace(workspace)
    if agent_cache is None:
        # Should never happen in production — the route factory always wires
        # an agent cache. Fail loud rather than silently skipping validation,
        # which would let bad workspaces through.
        raise OmnigentError(
            "workspace validation requires an agent cache",
            code=ErrorCode.INTERNAL_ERROR,
        )
    if host_registry is None:
        raise OmnigentError(
            "host registry is not configured on this server",
            code=ErrorCode.INTERNAL_ERROR,
        )

    host_name = await _authorize_host_for_workspace(
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
        host_registry=host_registry,
    )

    # Read the agent's os_env.cwd — None when the spec has no os_env block
    # (headless agents). Headless agents have no filesystem access at all but
    # still get launched on hosts for sessions that don't need it; treat their
    # cwd as relative-equivalent so the boundary is unrestricted.
    spec_cwd: str | None = None
    if agent.bundle_location is not None:
        try:
            loaded = await asyncio.to_thread(
                agent_cache.load,
                agent.id,
                agent.bundle_location,
            )
            os_env = getattr(loaded.spec, "os_env", None)
            spec_cwd = getattr(os_env, "cwd", None) if os_env is not None else None
        except Exception as exc:
            _logger.exception("Failed to load agent spec for workspace validation")
            raise OmnigentError(
                f"failed to load agent spec: {exc}",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc

    return await _canonical_workspace_or_invalid_input(
        host_registry=host_registry,
        host_id=host_id,
        workspace=workspace,
        spec_cwd=spec_cwd,
        host_name=host_name,
    )


async def validate_uploaded_bundle_host_workspace(
    *,
    user_id: str | None,
    host_id: str,
    workspace: str | None,
    spec_cwd: str | None,
    host_store: Any | None,
    host_registry: Any | None,
) -> str:
    """
    Validate a connected-host workspace for a bundle-upload create.

    The multipart ``POST /v1/sessions`` form carries the agent spec in
    the request itself, so — unlike
    :func:`validate_existing_host_workspace` — there is no registered
    agent row or cache entry to load the boundary from; the caller
    passes the freshly parsed spec's ``os_env.cwd`` directly. Shares
    every other check (shape, ownership-before-host-contact,
    wrong-replica classification, the seven-step host validation) so
    the two create forms cannot drift.

    :param user_id: Authenticated caller, or ``None`` when auth is off.
    :param host_id: Caller-supplied external host id.
    :param workspace: Caller-supplied absolute path on the host.
    :param spec_cwd: ``os_env.cwd`` from the uploaded bundle's spec,
        or ``None`` when the spec has no os_env block.
    :param host_store: Persistent host registrations.
    :param host_registry: Live host tunnels on this replica.
    :returns: The canonical workspace path to persist on the session
        row.
    :raises OmnigentError: On any validation failure; ``INTERNAL_ERROR``
        when the server has no host registry.
    """
    workspace = _require_absolute_host_workspace(workspace)
    if host_registry is None:
        raise OmnigentError(
            "host registry is not configured on this server",
            code=ErrorCode.INTERNAL_ERROR,
        )
    host_name = await _authorize_host_for_workspace(
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
        host_registry=host_registry,
    )
    return await _canonical_workspace_or_invalid_input(
        host_registry=host_registry,
        host_id=host_id,
        workspace=workspace,
        spec_cwd=spec_cwd,
        host_name=host_name,
    )
