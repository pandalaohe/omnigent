"""Resolve the instruction route once for session initialization and preview."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.exc import StatementError

from omnigent.entities import Conversation
from omnigent.errors import OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.runtime import get_agent_cache, get_agent_store, get_global_instructions_store
from omnigent.runtime.instruction_layers import (
    agent_key_for_harness,
    compose_role,
    compose_route,
    compose_session_text,
    select_role_layers,
)
from omnigent.spec import AgentSpec
from omnigent.stores.global_instructions_store import GlobalInstructionsStore

if TYPE_CHECKING:
    from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.permission_store import PermissionStore
    from omnigent.stores.project_store import ProjectStore


def project_setup_line(
    conversation: Conversation,
    *,
    project_store: ProjectStore | None,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    prefs_store: SqlAlchemyUserPreferencesStore | None,
) -> str:
    """Quote saved project setup as one runtime line, scoped to the session owner."""
    from omnigent.server.child_cap import child_cap_for
    from omnigent.server.routes.sessions.routes_peer import effective_owner_id

    if project_store is None or conversation.project_id is None:
        return ""
    project = project_store.get(
        conversation.project_id,
        user_id=effective_owner_id(conversation, conversation_store, permission_store),
    )
    orchestration = project.config.get("orchestration") if project is not None else None
    setup = orchestration.get("setup") if isinstance(orchestration, dict) else None
    if project is None or not isinstance(setup, str) or not setup.strip():
        return ""
    cap, _ = child_cap_for(
        conversation, conversation_store, permission_store, prefs_store, project_store
    )
    date = datetime.fromtimestamp(project.updated_at or project.created_at, UTC).date()
    quoted = json.dumps(" ".join(setup.split()), ensure_ascii=False)
    return (
        f"Saved setup for this project ({date}): {quoted}; child cap "
        f"{cap if cap is not None else 'none'}. State it in your first reply; "
        "change it only on the user's word."
    )


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
    quota_resolver: Callable[[], str] | None = None,
    setup_line: str = "",
) -> str | None:
    """Compose user layers and the exclusive role without changing the runner envelope."""
    role = "child" if has_parent else "mother" if route.spawn else None
    layers = store.latest_layers(route.host_id, route.agent_key, role=role)
    text = compose_route(layers).text
    role_text = (
        compose_role(select_role_layers(layers, role, route.host_id, route.agent_key))
        if role
        else ""
    )
    if role == "mother" and role_text.strip() and quota_resolver is not None:
        quota_line = quota_resolver()
    result = compose_session_text(
        text,
        has_parent=has_parent,
        spawn=route.spawn,
        mother_text=role_text if role == "mother" else "",
        child_text=role_text if role == "child" else "",
        quota_line=quota_line,
    )
    if role == "mother" and setup_line:
        result = f"{result}\n\n{setup_line}" if result else setup_line
    return result or None


async def session_instructions_text(
    conversation: Conversation,
    *,
    quota_resolver: Callable[[Conversation], str] | None = None,
    setup_resolver: Callable[[Conversation], str] | None = None,
) -> str | None:
    """Return the newly initialized session's route text, or no injection if disabled."""
    store = get_global_instructions_store()
    if store is None and setup_resolver is None:
        return None
    route = await resolve_instruction_route(conversation)
    setup_line = (
        await asyncio.to_thread(setup_resolver, conversation)
        if setup_resolver is not None
        and route.spawn
        and conversation.parent_conversation_id is None
        else ""
    )
    if store is None:
        return setup_line or None

    return await asyncio.to_thread(
        route_session_text,
        store,
        route,
        has_parent=conversation.parent_conversation_id is not None,
        quota_resolver=(lambda: quota_resolver(conversation)) if quota_resolver else None,
        setup_line=setup_line,
    )
