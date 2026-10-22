"""Resolve the instruction route once for session initialization and preview."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import StatementError

from omnigent.entities import Conversation
from omnigent.errors import OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.runtime import get_agent_cache, get_agent_store, get_global_instructions_store
from omnigent.runtime.instruction_layers import (
    agent_key_for_harness,
    compose_route,
    compose_session_text,
)
from omnigent.spec import AgentSpec
from omnigent.stores.global_instructions_store import GlobalInstructionsStore


@dataclass(frozen=True)
class InstructionRoute:
    """A session's own host, coding-agent product and resolved capabilities."""

    host_id: str | None
    agent_key: str | None
    harness: str | None
    spec: AgentSpec | None

    @property
    def spawn(self) -> bool:
        return bool(self.spec is not None and self.spec.spawn)


def _session_spec(conversation: Conversation) -> AgentSpec | None:
    if conversation.agent_id is None:
        return None
    try:
        agent = get_agent_store().get(conversation.agent_id)
        if agent is None:
            return None
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.operator_authored)
            .spec
        )
        if conversation.sub_agent_name:
            from omnigent.runtime.workflow import _find_spec_by_name

            return _find_spec_by_name(spec, conversation.sub_agent_name) or spec
        return spec
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


async def resolve_instruction_route(conversation: Conversation) -> InstructionRoute:
    """Honor the session override and existing bundle/sub-agent harness resolver."""
    from omnigent.server.routes._sessions.helpers import _resolve_harness_async

    harness = (
        conversation.harness_override
        if conversation.harness_override and conversation.harness_override.startswith("acp:")
        else canonicalize_harness(conversation.harness_override)
        if conversation.harness_override
        else await _resolve_harness_async(conversation)
    )
    spec = await asyncio.to_thread(_session_spec, conversation)
    if harness == "acp" and not conversation.harness_override and spec is not None:
        declared = spec.executor.config.get("harness")
        if isinstance(declared, str) and declared.startswith("acp:"):
            harness = declared
    return InstructionRoute(conversation.host_id, agent_key_for_harness(harness), harness, spec)


def route_session_text(
    store: GlobalInstructionsStore,
    route: InstructionRoute,
    *,
    has_parent: bool,
    quota_line: str = "",
) -> str | None:
    """Compose user layers and the exclusive role without changing the runner envelope."""
    text = compose_route(store.latest_layers(route.host_id, route.agent_key)).text
    role = "child" if has_parent else "mother" if route.spawn else None
    block = store.current(scope=role) if role else None
    result = compose_session_text(
        text,
        has_parent=has_parent,
        spawn=route.spawn,
        mother_text=block.text if block is not None and role == "mother" else "",
        child_text=block.text if block is not None and role == "child" else "",
        quota_line=quota_line,
    )
    return result or None


async def session_instructions_text(
    conversation: Conversation,
    *,
    quota_resolver: Callable[[Conversation], str] | None = None,
) -> str | None:
    """Return the newly initialized session's route text, or no injection if disabled."""
    store = get_global_instructions_store()
    if store is None:
        return None
    route = await resolve_instruction_route(conversation)

    def compose() -> str | None:
        has_parent = conversation.parent_conversation_id is not None
        mother = store.current(scope="mother") if not has_parent and route.spawn else None
        line = (
            quota_resolver(conversation)
            if quota_resolver is not None and mother is not None and mother.text.strip()
            else ""
        )
        return route_session_text(store, route, has_parent=has_parent, quota_line=line)

    return await asyncio.to_thread(compose)
