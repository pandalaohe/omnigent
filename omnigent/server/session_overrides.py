"""Session identity and preference override lookup shared by sweepers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from sqlalchemy.exc import StatementError

from omnigent.entities import Conversation
from omnigent.errors import OmnigentError
from omnigent.server.routes._sessions.helpers import _resolve_harness

_Value = TypeVar("_Value")
AGENT_TEMPLATE_ID_LABEL = "omnigent:agent-template-id"


def harness_for_session(
    conv: Conversation,
    cache: dict[str, str | None],
    *,
    bundles: dict[str, Any | None] | None = None,
) -> str | None:
    """Resolve the effective harness once per session in a sweep pass."""
    if conv.id not in cache:
        if bundles is None:
            harness = _resolve_harness(conv)
            if harness == "acp":
                harness = _installed_acp_slug(conv) or harness
        else:
            harness = _harness_from_shared_bundle(conv, bundles)
        cache[conv.id] = harness
    return cache[conv.id]


def _harness_from_shared_bundle(conv: Conversation, bundles: dict[str, Any | None]) -> str | None:
    """Read each agent bundle once while preserving per-session overrides."""
    if conv.harness_override:
        return conv.harness_override
    if conv.agent_id is None:
        return None
    if conv.agent_id not in bundles:
        bundles[conv.agent_id] = None
        from omnigent.runtime import get_agent_cache
        from omnigent.runtime._globals import _agent_store

        agent = _agent_store.get(conv.agent_id) if _agent_store is not None else None
        bundles[conv.agent_id] = (
            get_agent_cache().load(
                agent.id, agent.bundle_location, expand_env=agent.operator_authored
            )
            if agent is not None
            else None
        )
    loaded = bundles[conv.agent_id]
    if loaded is None:
        return None

    from omnigent.harness_aliases import canonicalize_harness

    executor = loaded.spec.executor
    if conv.sub_agent_name:
        from omnigent.runtime.workflow import _find_spec_by_name

        sub = _find_spec_by_name(loaded.spec, conv.sub_agent_name)
        if sub is not None:
            executor = sub.executor
    declared = (
        executor.config.get("harness")
        or loaded.spec.executor.config.get("harness")
        or executor.type
    )
    harness = canonicalize_harness(declared) or declared
    if harness == "acp" and isinstance(declared, str) and declared.startswith("acp:"):
        return declared
    return harness


def _installed_acp_slug(conv: Conversation) -> str | None:
    """Keep a bundle's ACP agent selection distinct from the base ACP runner."""
    if conv.harness_override or conv.agent_id is None:
        return None
    try:
        from omnigent.runtime import get_agent_cache
        from omnigent.runtime._globals import _agent_store

        if _agent_store is None:
            return None
        agent = _agent_store.get(conv.agent_id)
        if agent is None:
            return None
        loaded = get_agent_cache().load(
            agent.id, agent.bundle_location, expand_env=agent.operator_authored
        )
        executor = loaded.spec.executor
        if conv.sub_agent_name:
            from omnigent.runtime.workflow import _find_spec_by_name

            sub = _find_spec_by_name(loaded.spec, conv.sub_agent_name)
            if sub is not None:
                executor = sub.executor
        declared = (
            executor.config.get("harness")
            or loaded.spec.executor.config.get("harness")
            or executor.type
        )
        return declared if isinstance(declared, str) and declared.startswith("acp:") else None
    except (
        KeyError,
        AttributeError,
        ValueError,
        ImportError,
        OSError,
        RuntimeError,
        StatementError,
        OmnigentError,
    ):
        return None


def resolve_override(
    agents: Mapping[str, _Value],
    default: _Value,
    *,
    template_id: str | None,
    agent_id: str | None,
    harness: str | None,
) -> _Value:
    """Use template, direct agent, then harness exception before the default."""
    for key in (template_id, agent_id, f"harness:{harness}" if harness else None):
        if key is not None and key in agents:
            return agents[key]
    return default
