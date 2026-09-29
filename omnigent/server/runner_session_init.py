"""Server-owned coordination for runner session initialization."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx

from omnigent.debug_logging import debug_event, runner_log_scope
from omnigent.entities import Conversation
from omnigent.runner.session_init_protocol import (
    RunnerArchiveState,
    build_runner_session_init_payload,
    runner_archive_state,
)
from omnigent.runtime import current_global_instructions_text

if TYPE_CHECKING:
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.file_store import FileStore


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
_SessionInitKey = tuple[str, int, str, str, str | None, tuple[tuple[str, int, bool], ...], bool]
_logger = logging.getLogger(__name__)


class RunnerSessionInitializer:
    """Share initialization readiness within one runner tunnel generation."""

    def __init__(
        self,
        registry: TunnelRegistry,
        *,
        server_version: str,
        project_assignments_enabled: bool = False,
        peer_messaging_enabled: bool = False,
        peer_messaging_resolver: Callable[[Conversation], bool] | None = None,
        conversation_store: ConversationStore | None = None,
        file_store: FileStore | None = None,
    ) -> None:
        self._registry = registry
        self._server_version = server_version
        self._project_assignments_enabled = project_assignments_enabled
        self._peer_messaging_enabled = peer_messaging_enabled
        self._peer_messaging_resolver = peer_messaging_resolver
        self._conversation_store = conversation_store
        self._file_store = file_store
        self._tasks: dict[
            _SessionInitKey,
            asyncio.Task[httpx.Response],
        ] = {}
        self._recovery_ids: dict[_SessionInitKey, str] = {}
        # What each successful post carried, per (runner, generation, session):
        # a resolved value that differs means the init must be re-posted.
        self._applied_peer: dict[tuple[str, int, str], bool] = {}

    async def resolve_peer_messaging(self, conversation: Conversation) -> bool:
        """Resolve the peer-messaging snapshot for one session.

        The resolver reads the owner's master switch; a blocking store read,
        so it runs in a thread. Without a resolver the constructed flag wins
        (embedded/test transports and flag-off deployments).

        :param conversation: The session being initialized.
        :returns: Whether the runner should expose peer messaging.
        """
        if self._peer_messaging_resolver is None:
            return self._peer_messaging_enabled
        return await asyncio.to_thread(self._peer_messaging_resolver, conversation)

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
        peer = await self.resolve_peer_messaging(conversation)
        pkey = (runner_id, generation, conversation.id)
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
        if (
            task is not None
            and task.done()
            and not task.cancelled()
            and task.exception() is None
            and self._applied_peer.get(pkey) is not None
            and self._applied_peer[pkey] != peer
        ):
            # A flipped collaboration switch invalidates the cached success:
            # drop it so this call posts a fresh envelope.
            self._tasks.pop(key, None)
            task = None
        if task is None:
            recovery_id = (
                self._recovery_ids.setdefault(key, uuid4().hex)
                if resume_interrupted_turn
                else None
            )

            async def post_session_init() -> httpx.Response:
                # Built here, not before create_task: the store read blocks,
                # and awaiting between the single-flight lookup and the task
                # registration would let a second caller start its own init.
                payload = build_runner_session_init_payload(
                    conversation,
                    server_version=self._server_version,
                    suppress_recovery_turn=suppress_recovery_turn,
                    archive_states=effective_archive_states,
                    project_assignments_enabled=self._project_assignments_enabled,
                    peer_messaging_enabled=peer,
                    global_instructions=await asyncio.to_thread(current_global_instructions_text),
                    resume_interrupted_turn=resume_interrupted_turn,
                    recovery_id=recovery_id,
                )
                if self._conversation_store is not None and self._file_store is not None:
                    from omnigent.server.routes._sessions.helpers import (
                        _filesystem_attachment_in_history,
                        require_filesystem_attachment_runtime,
                    )

                    attachment = await asyncio.to_thread(
                        _filesystem_attachment_in_history,
                        conversation.id,
                        self._conversation_store,
                        self._file_store,
                    )
                    if attachment is not None:
                        require_filesystem_attachment_runtime(
                            host_id=None,
                            runner_id=runner_id,
                            host_registry=None,
                            tunnel_registry=self._registry,
                        )
                response = await self._post_initialize(
                    runner_client,
                    session_id=conversation.id,
                    runner_id=runner_id,
                    payload=payload,
                    timeout=timeout,
                )
                if 200 <= response.status_code < 300:
                    self._applied_peer[pkey] = peer
                return response

            task = asyncio.create_task(
                post_session_init(),
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

    async def peer_flag_stale(
        self,
        conversation: Conversation,
        runner_client: httpx.AsyncClient,
    ) -> bool:
        """Whether the current tunnel generation carries an outdated flag.

        Only a successful post in this generation counts: a session whose
        runner just connected, or whose init is still in flight, reports not
        stale and picks the value up through its normal initialization.

        :param conversation: The session being initialized.
        :param runner_client: The runner client this request would post to.
        :returns: ``True`` when the applied snapshot differs from the
            currently resolved one.
        """
        runner_id = conversation.runner_id
        if runner_id is None:
            return False
        connection = self._registry.get(runner_id)
        generation = id(connection) if connection is not None else id(runner_client)
        applied = self._applied_peer.get((runner_id, generation, conversation.id))
        if applied is None:
            return False
        return applied != await self.resolve_peer_messaging(conversation)

    def invalidate_session(self, session_id: str) -> None:
        """A new binding needs fresh readiness and a new continuation identity."""
        for key in list(self._tasks.keys() | self._recovery_ids.keys()):
            if key[2] == session_id:
                self._tasks.pop(key, None)
                self._recovery_ids.pop(key, None)
        for pkey in list(self._applied_peer):
            if pkey[2] == session_id:
                self._applied_peer.pop(pkey, None)

    async def _post_initialize(
        self,
        runner_client: httpx.AsyncClient,
        *,
        session_id: str,
        runner_id: str,
        payload: dict[str, object],
        timeout: float,
    ) -> httpx.Response:
        with runner_log_scope(session_id, runner_id):
            _logger.info(
                "Initializing runner session",
                extra=debug_event("runner_session_init_started", stage="session_init"),
            )
            try:
                response = await runner_client.post(
                    "/v1/sessions",
                    json=payload,
                    timeout=timeout,
                )
            except Exception:
                _logger.exception(
                    "Runner session initialization failed",
                    extra=debug_event("runner_session_init_failed", stage="session_init"),
                )
                raise
            failed = not 200 <= response.status_code < 300
            log = _logger.error if failed else _logger.info
            log(
                "Runner session initialization finished",
                extra=debug_event(
                    "runner_session_init_failed" if failed else "runner_session_initialized",
                    stage="session_init",
                    status_code=response.status_code,
                ),
            )
            return response

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
        for pkey in list(self._applied_peer):
            if pkey[0] == runner_id:
                self._applied_peer.pop(pkey)
