"""Resolution of the per-host calling defaults for a new session.

One chain supplies the agent, model, and reasoning effort: the explicit
request values, then the project's per-host set, then the user's master
table, then — for an SDK harness — its native parent (``codex`` inherits
``codex-native``). Each field resolves independently and the first non-empty
value wins; the returned ``source`` records which layer supplied it so
callers can refuse a broken setting by name. Everything here is pure; the
store loaders and route wiring live with their callers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_availability import is_harness_availability
from omnigent.session_default_modes import (
    PERMISSION_DEFAULT_VALUES,
    SPEED_TIER_HARNESSES,
    SPEED_TIER_VALUES,
)
from omnigent.util.reasoning_effort import EFFORT_VALUES, efforts_for_harness

_logger = logging.getLogger(__name__)

#: SDK harnesses whose settings fall back to a native sibling's entries.
SDK_NATIVE_PARENT: dict[str, str] = {
    "codex": "codex-native",
    "claude-sdk": "claude-native",
}

#: Harnesses a host can answer a model-options request for.
MODEL_OPTION_HARNESSES: tuple[str, ...] = (
    "codex-native",
    "codex",
    "claude-native",
    "claude-sdk",
    "pi-native",
    "devin-native",
)

_EXPLICIT_SOURCES = frozenset({"explicit", "none"})

#: Saved library Agent ids (``POST /v1/custom-agents``); no agent-omitted
#: server create path can launch one, so a default that names one is refused.
_LIBRARY_AGENT_ID_PREFIX = "ca_"


@dataclass(frozen=True)
class CallingResolution:
    """One resolved calling triple plus where each field came from.

    :param agent_id: Effective agent id, or ``None`` when no layer named one.
    :param harness: Effective harness for *agent_id* (or an explicit
        ``harness_override``), or ``None`` when neither is known.
    :param model: Effective model override, or ``None``.
    :param effort: Effective reasoning effort, or ``None``.
    :param speed: Session speed tier, or ``None``.
    :param permission: Harness-specific permission preset, or ``None``.
    :param sources: ``{"agent", "model", "effort", "speed", "permission"}`` source tokens, e.g.
        ``"explicit"``, ``"project_host"``, or ``"master_native"``.
    """

    agent_id: str | None
    harness: str | None
    model: str | None
    effort: str | None
    sources: dict[str, str]
    speed: str | None = None
    permission: str | None = None


def _setting(value: object) -> str | None:
    """Read one stored setting; blank / non-string values count as absent."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _explicit(value: object) -> str | None:
    """Read one explicit request value: presence wins, blank means unset."""
    return value if isinstance(value, str) and value else None


def _project_host_entry(
    project_config: Mapping[str, Any] | None, host_id: str | None
) -> Mapping[str, Any] | None:
    """The project's ``calling_defaults[host]`` object, or ``None``."""
    if not isinstance(project_config, Mapping) or host_id is None:
        return None
    defaults = project_config.get("calling_defaults")
    if not isinstance(defaults, Mapping):
        return None
    entry = defaults.get(host_id)
    return entry if isinstance(entry, Mapping) else None


def _project_harness_entry(
    project_config: Mapping[str, Any] | None, host_id: str | None, harness: str | None
) -> Mapping[str, Any] | None:
    """The project's ``calling_defaults[host].harnesses[harness]`` object."""
    entry = _project_host_entry(project_config, host_id)
    if entry is None or harness is None:
        return None
    harnesses = entry.get("harnesses")
    if not isinstance(harnesses, Mapping):
        return None
    harness_entry = harnesses.get(harness)
    return harness_entry if isinstance(harness_entry, Mapping) else None


def _project_legacy(project_config: Mapping[str, Any] | None, key: str) -> str | None:
    """Read one legacy project-config key (``agent_id`` / ``model``)."""
    if not isinstance(project_config, Mapping):
        return None
    return _setting(project_config.get(key))


def default_agent_for_host(
    project_config: Mapping[str, Any] | None, host_id: str | None
) -> str | None:
    """The project's per-host default agent id, or ``None`` when unset.

    The single-layer read a caller needs before it commits to an agent; a
    full create resolves through :func:`resolve_calling` instead.
    """
    entry = _project_host_entry(project_config, host_id)
    return _setting(entry.get("agent_id")) if entry is not None else None


def default_agent_for_host_or_legacy(
    project_config: Mapping[str, Any] | None, host_id: str | None
) -> str | None:
    """The project's default agent for one host: per-host entry, then legacy.

    The same order :func:`resolve_calling` walks when a request omits the
    agent; assignment dispatch and placement need the read without a full
    resolution.
    """
    return default_agent_for_host(project_config, host_id) or _project_legacy(
        project_config, "agent_id"
    )


def has_default_agent(project_config: Mapping[str, Any] | None) -> bool:
    """Whether any host could supply a project default agent.

    Counts the legacy ``agent_id`` (host-independent) and every per-host
    entry; the caller decides whether the found agent is launchable.
    """
    if _project_legacy(project_config, "agent_id") is not None:
        return True
    if not isinstance(project_config, Mapping):
        return False
    defaults = project_config.get("calling_defaults")
    if not isinstance(defaults, Mapping):
        return False
    return any(
        isinstance(entry, Mapping) and _setting(entry.get("agent_id")) is not None
        for entry in defaults.values()
    )


def _master_entry(
    master: Mapping[str, Any], host_id: str | None, harness: str | None
) -> Mapping[str, Any] | None:
    """The master table's ``[host][harness]`` object, or ``None``."""
    if not isinstance(master, Mapping) or host_id is None or harness is None:
        return None
    host_entry = master.get(host_id)
    if not isinstance(host_entry, Mapping):
        return None
    entry = host_entry.get(harness)
    return entry if isinstance(entry, Mapping) else None


def resolve_calling(
    *,
    explicit: dict[str, Any],
    explicit_fields: set[str],
    project_config: dict | None,
    master: dict,
    host_id: str | None,
    agent_harness: Callable[[str], str | None],
) -> CallingResolution:
    """Resolve agent / harness / model / effort for one create.

    All three fields follow the same shape — a field listed in
    *explicit_fields* is kept even when its value is ``None``, otherwise the
    project's per-host set, the legacy project key, the master table, and
    (for an SDK harness) the native parent are tried in that order. The
    legacy project ``model`` applies only when the effective agent equals the
    legacy ``agent_id``, matching today's project prefill. A projectless
    caller passes ``project_config=None`` and gets the master layers only.
    ``speed`` and ``permission`` are looked up under the canonical harness the
    create launches (an alias override is canonicalised; an explicit null /
    blank override falls back to the agent's harness), while ``model`` and
    ``effort`` keep the override as given.

    :param explicit: Request values keyed ``agent_id`` / ``harness_override``
        / ``model_override`` / ``reasoning_effort``.
    :param explicit_fields: Request fields the caller actually supplied
        (``model_fields_set`` semantics).
    :param project_config: The project's opaque config, or ``None``.
    :param master: The owner's ``calling_defaults`` preference namespace.
    :param host_id: The host the caller will place the session on.
    :param agent_harness: Resolves an agent id to its harness.
    :returns: The resolution plus per-field sources.
    """
    if "agent_id" in explicit_fields:
        agent_id = _explicit(explicit.get("agent_id"))
        agent_source = "explicit"
    else:
        host_entry = _project_host_entry(project_config, host_id)
        agent_id = _setting(host_entry.get("agent_id")) if host_entry is not None else None
        agent_source = "project_host" if agent_id is not None else "none"
        if agent_id is None:
            agent_id = _project_legacy(project_config, "agent_id")
            if agent_id is not None:
                agent_source = "project_legacy"

    if "harness_override" in explicit_fields:
        harness = _explicit(explicit.get("harness_override"))
        # Speed / permission follow the harness the create launches: the
        # canonical override, or the agent's own harness when the override is
        # explicitly null / blank.
        if harness is not None:
            mode_harness = canonicalize_harness(harness) or harness
        else:
            mode_harness = agent_harness(agent_id) if agent_id is not None else None
    elif agent_id is not None:
        harness = agent_harness(agent_id)
        mode_harness = harness
    else:
        harness = None
        mode_harness = None

    model, model_source = _resolve_model(
        explicit,
        explicit_fields,
        project_config,
        master,
        host_id,
        harness,
        agent_id,
    )
    effort, effort_source = _resolve_effort(
        explicit, explicit_fields, project_config, master, host_id, harness
    )
    speed, speed_source = _resolve_harness_setting(
        "speed",
        project_config,
        master,
        host_id,
        mode_harness,
        SPEED_TIER_VALUES if mode_harness in SPEED_TIER_HARNESSES else frozenset(),
    )
    permission, permission_source = _resolve_harness_setting(
        "permission",
        project_config,
        master,
        host_id,
        mode_harness,
        PERMISSION_DEFAULT_VALUES.get(mode_harness or "", frozenset()),
    )
    return CallingResolution(
        agent_id=agent_id,
        harness=harness,
        model=model,
        effort=effort,
        speed=speed,
        permission=permission,
        sources={
            "agent": agent_source,
            "model": model_source,
            "effort": effort_source,
            "speed": speed_source,
            "permission": permission_source,
        },
    )


def _resolve_model(
    explicit: dict[str, Any],
    explicit_fields: set[str],
    project_config: dict | None,
    master: dict,
    host_id: str | None,
    harness: str | None,
    agent_id: str | None,
) -> tuple[str | None, str]:
    """Walk the model chain, returning the value and its source token."""
    if "model_override" in explicit_fields:
        return _explicit(explicit.get("model_override")), "explicit"
    entry = _project_harness_entry(project_config, host_id, harness)
    value = _setting(entry.get("model")) if entry is not None else None
    if value is not None:
        return value, "project_host"
    legacy_agent = _project_legacy(project_config, "agent_id")
    # A legacy model belongs to the legacy agent only, and only when that
    # agent actually resolved — absent / blank ids never match each other.
    if agent_id is not None and agent_id == legacy_agent:
        value = _project_legacy(project_config, "model")
        if value is not None:
            return value, "project_legacy"
    entry = _master_entry(master, host_id, harness)
    value = _setting(entry.get("model")) if entry is not None else None
    if value is not None:
        return value, "master"
    native = SDK_NATIVE_PARENT.get(harness) if harness is not None else None
    if native is not None:
        entry = _project_harness_entry(project_config, host_id, native)
        value = _setting(entry.get("model")) if entry is not None else None
        if value is not None:
            return value, "project_host_native"
        entry = _master_entry(master, host_id, native)
        value = _setting(entry.get("model")) if entry is not None else None
        if value is not None:
            return value, "master_native"
    return None, "none"


def _resolve_effort(
    explicit: dict[str, Any],
    explicit_fields: set[str],
    project_config: dict | None,
    master: dict,
    host_id: str | None,
    harness: str | None,
) -> tuple[str | None, str]:
    """Walk the effort chain, returning the value and its source token."""
    if "reasoning_effort" in explicit_fields:
        return _explicit(explicit.get("reasoning_effort")), "explicit"
    return _resolve_harness_setting("effort", project_config, master, host_id, harness)


def _resolve_harness_setting(
    field: str,
    project_config: dict | None,
    master: dict,
    host_id: str | None,
    harness: str | None,
    values: frozenset[str] | None = None,
) -> tuple[str | None, str]:
    """Walk one per-harness setting's chain, ignoring unsupported stored values."""
    native = SDK_NATIVE_PARENT.get(harness) if harness is not None else None
    entries = [
        (_project_harness_entry(project_config, host_id, harness), "project_host"),
        (_master_entry(master, host_id, harness), "master"),
    ]
    if native is not None:
        entries.extend(
            [
                (_project_harness_entry(project_config, host_id, native), "project_host_native"),
                (_master_entry(master, host_id, native), "master_native"),
            ]
        )
    for entry, source in entries:
        value = _setting(entry.get(field)) if entry is not None else None
        if value is not None and (values is None or value in values):
            return value, source
    return None, "none"


def _catalog_model_ids(rows: list[Mapping[str, Any]]) -> set[str]:
    """Every model id a catalog's rows advertise (``id`` or ``model``)."""
    ids: set[str] = set()
    for row in rows:
        for key in ("id", "model"):
            value = row.get(key)
            if isinstance(value, str) and value:
                ids.add(value)
    return ids


def _catalog_efforts(rows: list[Mapping[str, Any]], model: str | None) -> set[str] | None:
    """Effort values one matching catalog row advertises, or ``None``.

    ``supportedReasoningEfforts`` rows arrive either as bare strings or as
    ``{"reasoningEffort": ...}`` objects; a row that carries none of them
    returns ``None`` so the caller falls back to the harness ladder.
    """
    if model is None:
        return None
    for row in rows:
        if row.get("id") != model and row.get("model") != model:
            continue
        raw = row.get("supportedReasoningEfforts")
        if not isinstance(raw, list):
            return None
        values: set[str] = set()
        for item in raw:
            if isinstance(item, str) and item:
                values.add(item)
            elif isinstance(item, Mapping):
                effort = item.get("reasoningEffort")
                if isinstance(effort, str) and effort:
                    values.add(effort)
        return values or None
    return None


def _format_sync_time(fetched_at: object) -> str:
    """Render a catalog's ``fetched_at`` epoch seconds for an error message."""
    if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
        return "never"
    return datetime.fromtimestamp(fetched_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _setting_phrase(setting: str, project_name: str | None) -> str:
    """Name the layer a source token came from in the user's words."""
    if setting in ("project_host", "project_host_native"):
        project = f"project {project_name!r}" if project_name else "the project"
        return f"{project} host settings"
    if setting == "project_legacy":
        project = f"project {project_name!r}" if project_name else "the project"
        return f"{project} All hosts row"
    return "the master table"


def _offered_problem(
    field: str,
    value: str,
    setting: str,
    harness: str,
    host_id: str,
    project_name: str | None,
    catalog: Mapping[str, Any],
) -> dict[str, str]:
    """Build one ``check_offered`` problem in the shared message shape."""
    return {
        "field": field,
        "setting": setting,
        "message": (
            f"Default {field} {value!r} from {_setting_phrase(setting, project_name)} is not "
            f"offered by host {host_id!r} ({harness}, last sync "
            f"{_format_sync_time(catalog.get('fetched_at'))}). "
            f"Change the setting, pass {field}, or run Sync models (Settings › Calling "
            f"defaults) if the host offers it now."
        ),
    }


def check_offered(
    *,
    harness: str,
    model: str | None,
    effort: str | None,
    catalog: dict | None,
    sources: dict[str, str],
    host_id: str,
    project_name: str | None = None,
) -> list[dict]:
    """Report default-sourced model / effort values a fresh catalog lacks.

    Only values that came from a default layer (any source other than
    ``explicit`` / ``none``) are checked; explicit values keep today's
    validation only. A catalog that is stale (``error`` set) or empty skips
    both checks, so a host that never synced never blocks a create. Effort
    offerings come from the matching model row's ``supportedReasoningEfforts``
    when it advertises any, else the harness's family ladder.

    :param harness: Effective harness the catalog belongs to.
    :param model: Resolved model override, or ``None``.
    :param effort: Resolved reasoning effort, or ``None``.
    :param catalog: ``{"models": [...], "error": str | None, "fetched_at":
        int | None}`` for the (host, harness) pair, or ``None``.
    :param sources: The resolution's per-field source tokens.
    :param host_id: Host the message names, e.g. ``"HDS"``.
    :param project_name: Project the message names for a project-sourced
        value, or ``None`` when no project applies.
    :returns: Zero or more ``{"field", "setting", "message"}`` problems.
    """
    if not isinstance(catalog, Mapping):
        return []
    if catalog.get("error") or not isinstance(catalog.get("models"), list):
        return []
    rows = [row for row in catalog["models"] if isinstance(row, Mapping)]
    if not rows:
        return []
    problems: list[dict] = []
    model_source = sources.get("model", "none")
    if (
        model is not None
        and model_source not in _EXPLICIT_SOURCES
        and model not in _catalog_model_ids(rows)
    ):
        problems.append(
            _offered_problem("model", model, model_source, harness, host_id, project_name, catalog)
        )
    effort_source = sources.get("effort", "none")
    if effort is not None and effort_source not in _EXPLICIT_SOURCES:
        offered = _catalog_efforts(rows, model)
        if offered is None:
            offered = efforts_for_harness(harness)
        if offered and effort not in offered:
            problems.append(
                _offered_problem(
                    "effort", effort, effort_source, harness, host_id, project_name, catalog
                )
            )
    return problems


def _readiness_problem(
    *,
    resolution: CallingResolution,
    agent_label: str,
    host_id: str,
    host_name: str | None,
    configured_harnesses: Mapping[str, Any] | None,
    project_name: str | None,
) -> dict[str, str] | None:
    """A default agent's harness that the host reports as not launchable.

    A missing readiness map (older host build), a missing entry, or a value
    outside the known readiness states all mean "unknown" and pass.
    """
    if configured_harnesses is None or resolution.harness is None:
        return None
    readiness = configured_harnesses.get(resolution.harness)
    if readiness is True or not is_harness_availability(readiness):
        # Ready, or a value the host never reports: unknown passes.
        return None
    source = resolution.sources.get("agent", "none")
    label = host_name or host_id
    return {
        "field": "agent",
        "setting": source,
        "message": (
            f"Default agent {agent_label!r} from "
            f"{_setting_phrase(source, project_name)} is not ready on host {label!r}: "
            f"harness {resolution.harness!r} is {readiness!r}. "
            "Change the setting or pass agent_id."
        ),
    }


def check_calling_defaults(
    *,
    resolution: CallingResolution,
    host_id: str,
    host_name: str | None,
    configured_harnesses: Mapping[str, Any] | None,
    catalog: dict | None,
    agent_name: str | None,
    project_name: str | None,
    path_label: str,
) -> list[dict]:
    """Report every K5 problem a resolved calling triple has.

    Shared by :func:`omnigent.server.routes._session_create_validation.
    resolve_create_calling` (which raises the first message) and the resolve
    route (which returns them as ``problems``), so the web preview and the
    server agree. An explicit agent keeps today's validation only.

    :param resolution: The resolved calling triple.
    :param host_id: Host id, used by the offered messages.
    :param host_name: Host display name for the readiness message, or ``None``.
    :param configured_harnesses: The host's per-harness readiness map, or
        ``None`` when unknown.
    :param catalog: The cached ``(host, harness)`` catalog row, or ``None``.
    :param agent_name: The resolved agent's display name, or ``None``.
    :param project_name: Project the messages name, or ``None``.
    :param path_label: The create path named in the library-agent refusal,
        e.g. ``"session_create"``.
    :returns: Zero or more ``{"field", "setting", "message"}`` problems, agent
        problems first.
    """
    sources = resolution.sources
    agent_source = sources.get("agent", "none")
    problems: list[dict] = []
    if resolution.agent_id is not None and agent_source not in _EXPLICIT_SOURCES:
        agent_label = agent_name or resolution.agent_id
        if resolution.agent_id.startswith(_LIBRARY_AGENT_ID_PREFIX):
            problems.append(
                {
                    "field": "agent",
                    "setting": agent_source,
                    "message": (
                        f"Default agent {agent_label!r} for host "
                        f"{host_name or host_id!r} is a saved joint agent; {path_label} "
                        "cannot launch it. Pass agent_id."
                    ),
                }
            )
        readiness = _readiness_problem(
            resolution=resolution,
            agent_label=agent_label,
            host_id=host_id,
            host_name=host_name,
            configured_harnesses=configured_harnesses,
            project_name=project_name,
        )
        if readiness is not None:
            problems.append(readiness)
    problems.extend(
        check_offered(
            harness=resolution.harness or "",
            model=resolution.model,
            effort=resolution.effort,
            catalog=catalog,
            sources=sources,
            host_id=host_id,
            project_name=project_name,
        )
    )
    return problems


def _invalid(message: str) -> OmnigentError:
    """Build an ``INVALID_INPUT`` error for a bad calling-defaults shape."""
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def _require_agent_id(value: object, path: str) -> str:
    """Validate an ``agent_id`` entry."""
    if not isinstance(value, str) or not value:
        raise _invalid(f"{path} must be a non-empty string")
    return value


def _require_model(value: object, path: str) -> str:
    """Validate a ``model`` entry: a non-empty id without whitespace."""
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise _invalid(f"{path} must be a non-empty model id without whitespace")
    return value


def validate_project_calling_defaults(value: object) -> dict:
    """Validate and copy a project's ``config.calling_defaults`` value.

    The project config blob is otherwise opaque, so this new key is
    shape-validated on write: unknown keys are rejected at every level rather
    than silently ignored, which would hide a typo'd default.

    :param value: The raw ``config["calling_defaults"]`` value.
    :returns: A copy containing only known keys.
    :raises OmnigentError: ``INVALID_INPUT`` when the shape is wrong.
    """
    if not isinstance(value, dict):
        raise _invalid("calling_defaults must be an object")
    validated: dict = {}
    for host_id, host_entry in value.items():
        if not isinstance(host_id, str) or not host_id:
            raise _invalid("calling_defaults keys must be non-empty host ids")
        if not isinstance(host_entry, dict):
            raise _invalid(f"calling_defaults[{host_id!r}] must be an object")
        unknown = set(host_entry) - {"agent_id", "harnesses"}
        if unknown:
            raise _invalid(
                f"calling_defaults[{host_id!r}] has unknown key {next(iter(unknown))!r}"
            )
        clean: dict = {}
        if "agent_id" in host_entry:
            clean["agent_id"] = _require_agent_id(
                host_entry["agent_id"], f"calling_defaults[{host_id!r}].agent_id"
            )
        if "harnesses" in host_entry:
            harnesses = host_entry["harnesses"]
            if not isinstance(harnesses, dict):
                raise _invalid(f"calling_defaults[{host_id!r}].harnesses must be an object")
            clean_harnesses: dict = {}
            for harness, entry in harnesses.items():
                if not isinstance(harness, str) or not harness:
                    raise _invalid(
                        f"calling_defaults[{host_id!r}].harnesses keys must be harness names"
                    )
                base = f"calling_defaults[{host_id!r}].harnesses[{harness!r}]"
                if not isinstance(entry, dict):
                    raise _invalid(f"{base} must be an object")
                unknown = set(entry) - {"model", "effort", "speed", "permission"}
                if unknown:
                    raise _invalid(f"{base} has unknown key {next(iter(unknown))!r}")
                clean_entry: dict = {}
                if "model" in entry:
                    clean_entry["model"] = _require_model(entry["model"], f"{base}.model")
                if "effort" in entry:
                    effort = entry["effort"]
                    if not isinstance(effort, str) or effort not in EFFORT_VALUES:
                        raise _invalid(f"{base}.effort must be one of {sorted(EFFORT_VALUES)}")
                    clean_entry["effort"] = effort
                for field, values in (
                    (
                        "speed",
                        SPEED_TIER_VALUES if harness in SPEED_TIER_HARNESSES else frozenset(),
                    ),
                    ("permission", PERMISSION_DEFAULT_VALUES.get(harness, frozenset())),
                ):
                    if field in entry:
                        value = entry[field]
                        if not isinstance(value, str) or value not in values:
                            raise _invalid(f"{base}.{field} must be one of {sorted(values)}")
                        clean_entry[field] = value
                clean_harnesses[harness] = clean_entry
            clean["harnesses"] = clean_harnesses
        validated[host_id] = clean
    return validated


class _PreferencesReader(Protocol):
    """The read surface :func:`load_master` needs from a preferences store."""

    def get(self, user_id: str) -> dict[str, Any] | None: ...


async def load_master(user_id: str | None, store: _PreferencesReader | None) -> dict:
    """Read one owner's ``calling_defaults`` master table, defaulting on gaps.

    Fail-safe like the approval-timeout reader: a missing store, a store
    error, or a non-object namespace resolves to ``{}`` so no create path
    ever fails on a malformed preference row. A ``None`` owner is the
    single-user server's reserved ``"local"`` identity, where its
    preferences are stored, so that owner still reads the master table.

    :param user_id: Owner whose master table applies, or ``None`` for the
        single-user ``"local"`` owner.
    :param store: Preferences store, or ``None`` when the server has none.
    :returns: The owner's master table, or ``{}``.
    """
    if store is None:
        return {}
    if user_id is None:
        # Lazy so this pure module stays importable without the server stack.
        from omnigent.server.auth import RESERVED_USER_LOCAL

        user_id = RESERVED_USER_LOCAL
    try:
        envelope = await asyncio.to_thread(store.get, user_id)
    except Exception:  # noqa: BLE001 — a preference read must never fail a create
        _logger.warning("Failed to read calling_defaults preferences", exc_info=True)
        return {}
    if not isinstance(envelope, dict):
        return {}
    settings = envelope.get("settings")
    if not isinstance(settings, dict):
        return {}
    value = settings.get("calling_defaults")
    if not isinstance(value, dict):
        if value is not None:
            _logger.warning("Ignoring malformed calling_defaults preferences")
        return {}
    return value
