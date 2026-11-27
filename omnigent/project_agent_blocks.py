"""Per-host blocked agent lists for projects.

A project may block specific catalog agents globally (``"*"``) or on named hosts
via ``config.blocked_agents``. Blocking stops new sessions for the project on
matching hosts; existing sessions and their lineage stay unaffected.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.util.uuid import normalize_uuid

RESERVED_ALL_HOSTS = "*"
FORK_SAME_AGENT_LABEL_KEY = "omnigent.fork.same_agent"
MAX_BLOCKED_HOSTS = 64
MAX_BLOCKED_AGENTS_PER_HOST = 64
MAX_AGENT_ID_LEN = 200
_LIBRARY_AGENT_ID_PREFIX = "ca_"


def _invalid(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def _setting(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def normalize_block_id(value: str) -> str:
    """Normalize supported UUID spellings while preserving unknown ids."""
    if value == RESERVED_ALL_HOSTS or value.startswith(_LIBRARY_AGENT_ID_PREFIX):
        return value
    return normalize_uuid(value) or value


_norm_id = normalize_block_id


def validate_blocked_agents(value: object) -> dict[str, list[str]]:
    """Validate and normalize ``config.blocked_agents``; drops empty host lists.

    :param value: Raw ``config["blocked_agents"]`` value.
    :returns: Normalized mapping containing only non-empty lists.
    :raises OmnigentError: ``INVALID_INPUT`` (400) on shape, limit, or duplicate errors.
    """
    if not isinstance(value, dict):
        raise _invalid("blocked_agents must be an object")
    if len(value) > MAX_BLOCKED_HOSTS:
        raise _invalid(f"blocked_agents supports at most {MAX_BLOCKED_HOSTS} host keys")
    normalized: dict[str, list[str]] = {}
    for host_key, raw_agents in value.items():
        if not isinstance(host_key, str) or not host_key.strip():
            raise _invalid("blocked_agents keys must be non-empty strings")
        if not isinstance(raw_agents, list):
            raise _invalid(f"blocked_agents[{host_key!r}] must be a list")
        if len(raw_agents) > MAX_BLOCKED_AGENTS_PER_HOST:
            raise _invalid(
                f"blocked_agents[{host_key!r}] supports at most "
                f"{MAX_BLOCKED_AGENTS_PER_HOST} agents"
            )
        norm_host = _norm_id(host_key)
        seen: set[str] = set()
        entries: list[str] = []
        for item in raw_agents:
            if not isinstance(item, str) or not item.strip() or len(item) > MAX_AGENT_ID_LEN:
                raise _invalid(
                    f"blocked_agents[{host_key!r}] entries must be non-empty strings "
                    f"of at most {MAX_AGENT_ID_LEN} characters"
                )
            norm_item = _norm_id(item)
            if norm_item in seen:
                raise _invalid(f"blocked_agents[{host_key!r}] contains duplicate agent {item!r}")
            seen.add(norm_item)
            entries.append(norm_item)
        if entries:
            if norm_host in normalized:
                raise _invalid(f"blocked_agents contains duplicate host key {host_key!r}")
            normalized[norm_host] = entries
    return normalized


def blocked_agents(config: Mapping[str, Any] | None, host_id: str | None) -> frozenset[str]:
    """Return blocked agent ids for *host_id*: ``"*"`` union ``[host_id]``.

    When *host_id* is ``None`` (managed sandbox or hostless create), only ``"*"``
    entries apply.
    """
    if not isinstance(config, Mapping):
        return frozenset()
    raw = config.get("blocked_agents")
    if not isinstance(raw, Mapping):
        return frozenset()
    norm_target_host = _norm_id(host_id) if host_id is not None else None
    wanted_keys: set[str] = {RESERVED_ALL_HOSTS}
    if norm_target_host is not None and norm_target_host != RESERVED_ALL_HOSTS:
        wanted_keys.add(norm_target_host)
    blocked: set[str] = set()
    for raw_key, items in raw.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            continue
        if _norm_id(raw_key) not in wanted_keys:
            continue
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str) and item.strip():
                    blocked.add(_norm_id(item))
    return frozenset(blocked)


def check_default_agents_not_blocked(config: Mapping[str, Any]) -> None:
    """Enforce that no effective default agent is blocked on its host.

    Checks legacy ``config.agent_id`` against ``"*"`` and, for every host key in
    ``calling_defaults ∪ blocked_agents``, checks ``calling_defaults[H].agent_id
    ?? config.agent_id`` against ``blocked_agents(config, H)``.

    :raises OmnigentError: ``INVALID_INPUT`` (400) naming host and agent.
    """
    legacy_agent = _setting(config.get("agent_id"))
    if legacy_agent is not None and _norm_id(legacy_agent) in blocked_agents(config, None):
        raise _invalid(
            f"Default agent {legacy_agent!r} for host {RESERVED_ALL_HOSTS!r} cannot be blocked"
        )
    host_keys: set[str] = set()
    raw_defaults = config.get("calling_defaults")
    if isinstance(raw_defaults, Mapping):
        host_keys.update(k for k in raw_defaults if isinstance(k, str) and k != RESERVED_ALL_HOSTS)
    raw_blocked = config.get("blocked_agents")
    if isinstance(raw_blocked, Mapping):
        host_keys.update(k for k in raw_blocked if isinstance(k, str) and k != RESERVED_ALL_HOSTS)
    for host_id in sorted(host_keys):
        default_agent: str | None = None
        if isinstance(raw_defaults, Mapping):
            exact = raw_defaults.get(host_id)
            if isinstance(exact, Mapping):
                default_agent = _setting(exact.get("agent_id"))
            if default_agent is None:
                norm_h = _norm_id(host_id)
                for dk, dv in raw_defaults.items():
                    if isinstance(dk, str) and _norm_id(dk) == norm_h and isinstance(dv, Mapping):
                        default_agent = _setting(dv.get("agent_id"))
                        if default_agent is not None:
                            break
        if default_agent is None:
            default_agent = legacy_agent
        if default_agent is not None and _norm_id(default_agent) in blocked_agents(
            config, host_id
        ):
            raise _invalid(
                f"Default agent {default_agent!r} for host {host_id!r} cannot be blocked"
            )


def assert_agent_allowed(
    *,
    project: Project | None,
    host_id: str | None,
    agent_id: str | None,
    agent_label: str | None = None,
    host_label: str | None = None,
) -> None:
    """Raise ``AGENT_BLOCKED_FOR_PROJECT`` (409) when *agent_id* is blocked.

    No-op without a project, without an agent, or for saved library / joint
    agents (``ca_``), which are outside catalog-agent-id blocking.
    """
    if project is None or not agent_id or agent_id.startswith(_LIBRARY_AGENT_ID_PREFIX):
        return
    if _norm_id(agent_id) not in blocked_agents(project.config, host_id):
        return
    display_agent = agent_label or agent_id
    display_host = host_label or host_id or "all hosts"
    raise OmnigentError(
        f"Agent {display_agent!r} is blocked for project {project.name!r} on host "
        f"{display_host!r}. Pick another agent or remove it from the project's blocked agents.",
        code=ErrorCode.AGENT_BLOCKED_FOR_PROJECT,
    )
