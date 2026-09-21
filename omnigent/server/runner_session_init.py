"""Server-owned coordination for runner session initialization."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx

from omnigent.entities import Conversation
from omnigent.runner.session_init_protocol import (
    RunnerArchiveState,
    build_runner_session_init_payload,
    runner_archive_state,
)

if TYPE_CHECKING:
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
    from omnigent.stores import ConversationStore


async def conversation_archive_lineage(
    conversation: Conversation,
    conversation_store: ConversationStore,
) -> list[Conversation]:
    """Load the session and every persisted ancestor exactly once."""
    lineage = [conversation]
    seen = {conversation.id}
    parent_id = getattr(conversation, "parent_conversation_id", None)
    while parent_id is not None and parent_id not in seen:
        parent = await asyncio.to_thread(
            conversation_store.get_conversation,
            parent_id,
        )
        if parent is None:
            break
        lineage.append(parent)
        seen.add(parent.id)
        parent_id = getattr(parent, "parent_conversation_id", None)
    root_id = getattr(conversation, "root_conversation_id", None)
    if root_id and root_id not in seen:
        root = await asyncio.to_thread(
            conversation_store.get_conversation,
            root_id,
        )
        if root is not None:
            lineage.append(root)
    return lineage


async def runner_archive_states_for_conversation(
    conversation: Conversation,
    conversation_store: ConversationStore,
) -> list[RunnerArchiveState]:
    """Project every archive scope that can fence this session runtime."""
    lineage = await conversation_archive_lineage(conversation, conversation_store)
    return [runner_archive_state(item) for item in lineage]


def runner_inference_verified(conversation: Conversation, response: httpx.Response) -> bool:
    """Configured sessions require a runner that accepted their saved routing."""
    if conversation.inference_snapshot is None:
        return True
    if response.status_code >= 400:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("inference_config_verified") is True


# Memo key: runner, tunnel generation, conversation, agent, sub-agent, the
# archive revisions this init was built from, and whether it resumes a turn.
_SessionInitKey = tuple[
    str, int, str, str, str | None, tuple[tuple[str, int, bool], ...], bool
]


class RunnerSessionInitializer:
    """Share initialization readiness within one runner tunnel generation."""

    def __init__(
        self,
        registry: TunnelRegistry,
        *,
        server_version: str,
        project_assignments_enabled: bool = False,
        peer_messaging_enabled: bool = False,
    ) -> None:
        self._registry = registry
        self._server_version = server_version
        self._project_assignments_enabled = project_assignments_enabled
        self._peer_messaging_enabled = peer_messaging_enabled
        self._tasks: dict[
            _SessionInitKey,
            asyncio.Task[httpx.Response],
        ] = {}
        self._recovery_ids: dict[_SessionInitKey, str] = {}

    async def initialize(
        self,
        conversation: Conversation,
        runner_client: httpx.AsyncClient,
        *,
        timeout: float,
        suppress_recovery_turn: bool = False,
        archive_states: list[RunnerArchiveState] | None = None,
        resume_interrupted_turn: bool = False,
    ) -> httpx.Response:
        """Initialize once for the current connection and persisted snapshot."""
        runner_id = conversation.runner_id
        agent_id = conversation.agent_id
        if runner_id is None or agent_id is None:
            raise ValueError("runner session initialization requires runner_id and agent_id")
        connection = self._registry.get(runner_id)
        # Production routed clients always have a registry entry. The client
        # identity fallback keeps embedded/test transports usable without
        # weakening the real tunnel-generation key.
        generation = id(connection) if connection is not None else id(runner_client)
        effective_archive_states = archive_states or [runner_archive_state(conversation)]
        archive_key = tuple(
            (state.scope_id, state.revision, state.archived) for state in effective_archive_states
        )
        key = (
            runner_id,
            generation,
            conversation.id,
            agent_id,
            conversation.sub_agent_name,
            archive_key,
            resume_interrupted_turn,
        )
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                runner_client.post(
                    "/v1/sessions",
                    json=build_runner_session_init_payload(
                        conversation,
                        server_version=self._server_version,
                        suppress_recovery_turn=suppress_recovery_turn,
                        archive_states=effective_archive_states,
                        project_assignments_enabled=self._project_assignments_enabled,
                        peer_messaging_enabled=self._peer_messaging_enabled,
                        resume_interrupted_turn=resume_interrupted_turn,
                        recovery_id=(
                            self._recovery_ids.setdefault(key, uuid4().hex)
                            if resume_interrupted_turn
                            else None
                        ),
                    ),
                    timeout=timeout,
                ),
                name=f"runner-session-init-{conversation.id}",
            )
            self._tasks[key] = task

            def _drop_failed(done: asyncio.Task[httpx.Response]) -> None:
                if self._tasks.get(key) is not done:
                    return
                if done.cancelled():
                    self._tasks.pop(key, None)
                    return
                if done.exception() is not None:
                    self._tasks.pop(key, None)
                    return
                response = done.result()
                if response.status_code >= 400:
                    self._tasks.pop(key, None)

            task.add_done_callback(_drop_failed)
        try:
            response = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._tasks.get(key) is task:
                self._tasks.pop(key, None)
            raise
        if not runner_inference_verified(conversation, response):
            response = httpx.Response(
                409,
                json={"error": "The runner did not accept this session's inference configuration"},
                request=httpx.Request("POST", "/v1/sessions"),
            )
        if response.status_code >= 400 and self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        return response

    def invalidate_session(self, session_id: str) -> None:
        """A new binding needs fresh readiness and a new continuation identity."""
        for key in list(self._tasks.keys() | self._recovery_ids.keys()):
            if key[2] == session_id:
                self._tasks.pop(key, None)
                self._recovery_ids.pop(key, None)

    def invalidate_runner(self, runner_id: str) -> None:
        """Forget completed readiness when a runner tunnel goes away."""
        for key in list(self._recovery_ids):
            if key[0] == runner_id:
                self._recovery_ids.pop(key)
        stale = [key for key in self._tasks if key[0] == runner_id]
        for key in stale:
            task = self._tasks.pop(key)
            if not task.done():
                task.cancel()
