"""Server-owned coordination for runner session initialization."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import httpx

from omnigent.debug_logging import debug_event, runner_log_scope
from omnigent.entities import Conversation
from omnigent.errors import SESSION_AGENT_MISSING_MESSAGE, ErrorCategory, ErrorCode, ErrorImpact
from omnigent.runner.session_init_protocol import (
    ProjectCodeLocation,
    RunnerArchiveState,
    build_runner_session_init_payload,
    runner_archive_state,
)
from omnigent.server.instruction_layers import session_instructions_text
from omnigent.server.permission_tiers import (
    PERMISSION_CAPABILITY_LABEL_KEY,
    PERMISSION_CAPABILITY_VALUE,
    PERMISSION_MIGRATION_LABEL_KEY,
    PERMISSION_RUNTIME_ID_LABEL_KEY,
    PERMISSION_STATE_LABEL_KEY,
    TIER_HARNESSES,
    ConfirmedPermissionState,
    legacy_permission_tier,
)
from omnigent.server.project_placement import project_code_locations
from omnigent.startup_protocol import StartupAttemptRef

if TYPE_CHECKING:
    from omnigent.runner.transports.ws_tunnel.registry import RunnerGenerationGuard, TunnelRegistry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.stores.agent_store import AgentStore
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.file_store import FileStore
    from omnigent.stores.project_host_binding_store import ProjectHostBindingStore
    from omnigent.stores.project_repository_store import ProjectRepositoryStore


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


def runner_permission_verified(conversation: Conversation, response: httpx.Response) -> bool:
    """A tiered session needs an exact revision ACK from this runner."""
    raw = conversation.labels.get(PERMISSION_STATE_LABEL_KEY)
    if raw is None:
        return True
    if response.status_code >= 400:
        return False
    try:
        state = json.loads(raw)
        payload = response.json()
    except (ValueError, TypeError):
        return False
    return (
        isinstance(state, dict)
        and isinstance(payload, dict)
        and type(state.get("revision")) is int
        and payload.get("permission_tiers_capability") == PERMISSION_CAPABILITY_VALUE
        and payload.get("permission_tier_revision") == state["revision"]
        and isinstance(payload.get("permission_tier_runtime_id"), str)
        and len(payload["permission_tier_runtime_id"]) == 32
        and (
            state.get("status") != "ready"
            or conversation.labels.get(PERMISSION_RUNTIME_ID_LABEL_KEY)
            == payload["permission_tier_runtime_id"]
        )
    )


# Memo key: runner, tunnel generation, conversation, agent, sub-agent, the
# archive revisions this init was built from, and whether it resumes a turn.
_SessionInitKey = tuple[
    str,
    int,
    str,
    str,
    str | None,
    tuple[tuple[str, int, bool], ...],
    bool,
    str | None,
    str | None,
    str | None,
    str | None,
    bool,
    StartupAttemptRef | None,
]
_BODY_SNIPPET_MAX = 500

# Rejections that mean the runner or session is transiently gone, not a server bug.
TRANSIENT_REJECTION_STATUSES = frozenset({404, 410, 503})


def runner_response_error_body(response: httpx.Response) -> str:
    """Return a bounded error snippet from a non-2xx runner response.

    Prefers the ``error``, ``detail``, or ``message`` field when the body is
    JSON, otherwise falls back to the raw text.
    """
    try:
        payload = response.json()
        if isinstance(payload, dict):
            for key in ("error", "detail", "message"):
                val = payload.get(key)
                if isinstance(val, str) and val:
                    return val[:_BODY_SNIPPET_MAX]
    except ValueError:
        pass
    return response.text[:_BODY_SNIPPET_MAX]


def is_session_agent_removed(response: httpx.Response) -> bool:
    """
    Whether *response* is the initializer skipping a session whose agent was removed.

    The runner could only reject that session, and it says so itself on the
    session's next message, so callers treat this as expected, not a failure.

    :param response: A response from :meth:`RunnerSessionInitializer.initialize`.
    :returns: ``True`` for the removed-agent response.
    """
    if response.status_code != 410:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("error") == ErrorCode.SESSION_AGENT_MISSING


_logger = logging.getLogger(__name__)


class RunnerSessionInitializer:
    """Share initialization readiness within one runner tunnel generation."""

    def __init__(
        self,
        registry: TunnelRegistry,
        *,
        server_version: str,
        peer_messaging_enabled: bool = False,
        peer_messaging_resolver: Callable[[Conversation], bool] | None = None,
        startup_control_resolver: (
            Callable[[Conversation], tuple[bool, StartupAttemptRef | None]] | None
        ) = None,
        startup_recovery_resolver: (
            Callable[[Conversation], Awaitable[StartupAttemptRef | None]] | None
        ) = None,
        quota_resolver: Callable[[Conversation], str] | None = None,
        conversation_store: ConversationStore | None = None,
        file_store: FileStore | None = None,
        agent_store: AgentStore | None = None,
        project_repository_store: ProjectRepositoryStore | None = None,
        project_host_binding_store: ProjectHostBindingStore | None = None,
        agent_cache: AgentCache | None = None,
    ) -> None:
        self._registry = registry
        self._server_version = server_version
        self._peer_messaging_enabled = peer_messaging_enabled
        self._peer_messaging_resolver = peer_messaging_resolver
        self._startup_control_resolver = startup_control_resolver
        self._startup_recovery_resolver = startup_recovery_resolver
        self._quota_resolver = quota_resolver
        self._conversation_store = conversation_store
        self._file_store = file_store
        self._agent_store = agent_store
        self._project_repository_store = project_repository_store
        self._project_host_binding_store = project_host_binding_store
        self._agent_cache = agent_cache
        self._tasks: dict[
            _SessionInitKey,
            asyncio.Task[httpx.Response],
        ] = {}
        self._recovery_ids: dict[_SessionInitKey, str] = {}
        # What each successful post carried, per (runner, generation, session):
        # a resolved value that differs means the init must be re-posted.
        self._applied_peer: dict[tuple[str, int, str], bool] = {}
        # (task, value) for each in-flight post, keyed like ``_applied_peer``.
        # The task identity lets a finished post clear only its own entry.
        self._pending_peer: dict[
            tuple[str, int, str],
            tuple[asyncio.Task[httpx.Response], bool],
        ] = {}

    def _startup_control(
        self, conversation: Conversation
    ) -> tuple[bool, StartupAttemptRef | None]:
        if self._startup_control_resolver is None:
            return False, None
        required, attempt = self._startup_control_resolver(conversation)
        return required, attempt if required else None

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

    async def resolve_project_code(
        self, conversation: Conversation
    ) -> list[ProjectCodeLocation] | None:
        """Resolve this host's code locations for the init snapshot.

        Reads the project's repositories and bindings in a worker thread.
        A store failure reads as "no repositories registered": the session
        must start even when the collaboration stores are unavailable.

        :param conversation: The session being initialized.
        :returns: The ordered locations, or ``None`` when none apply.
        """
        if (
            self._project_repository_store is None
            or self._project_host_binding_store is None
            or not conversation.project_id
        ):
            return None
        try:
            repositories, bindings = await asyncio.gather(
                asyncio.to_thread(
                    self._project_repository_store.list_by_project, conversation.project_id
                ),
                asyncio.to_thread(
                    self._project_host_binding_store.list_by_project, conversation.project_id
                ),
            )
        except Exception:  # noqa: BLE001 — a store failure must not block the start
            _logger.warning(
                "Could not load project code locations for session %s",
                conversation.id,
                exc_info=True,
            )
            return None
        return project_code_locations(repositories, bindings, conversation.host_id)

    def generation_for(self, runner_id: str, runner_client: httpx.AsyncClient) -> int:
        """Identify the current tunnel, or the client for embedded transports."""
        connection = self._registry.get(runner_id)
        return connection.generation if connection is not None else id(runner_client)

    def require_generation(
        self, runner_id: str, runner_client: httpx.AsyncClient, generation: int
    ) -> None:
        """Prevent delayed recovery work from following a replacement tunnel."""
        if self.generation_for(runner_id, runner_client) != generation:
            raise ConnectionError("runner tunnel changed during session recovery")

    def generation_guard(
        self, runner_id: str, runner_client: httpx.AsyncClient, generation: int
    ) -> RunnerGenerationGuard:
        """Retain the tunnel identity through an inventory transaction's commit."""
        self.require_generation(runner_id, runner_client, generation)
        return self._registry.generation_guard(runner_id, generation)

    async def _migrate_legacy_permission_state(
        self,
        conversation: Conversation,
        *,
        store_slots: asyncio.Semaphore | None,
    ) -> Conversation:
        """Fence an idle legacy session before any new runner can launch it."""
        if PERMISSION_STATE_LABEL_KEY in conversation.labels:
            return conversation
        from omnigent.server.routes._sessions.helpers import (
            _resolve_harness,
            _session_status_cache,
        )

        if self._agent_store is None and self._agent_cache is None:
            harness = _resolve_harness(conversation)
        else:
            async with store_slots or nullcontext():
                harness = await asyncio.to_thread(
                    _resolve_harness,
                    conversation,
                    agent_store=self._agent_store,
                    agent_cache=self._agent_cache,
                )
        if harness not in TIER_HARNESSES:
            return conversation
        harness = cast(str, harness)
        if self._conversation_store is None:
            raise ValueError("Permission migration requires the conversation store")
        live = _session_status_cache.get(conversation.id) or conversation.live_status
        if live in {"running", "waiting", "launching", "queued"}:
            raise ValueError("Permission migration requires an idle session")
        tier = legacy_permission_tier(
            harness=harness,
            labels=conversation.labels,
            launch_args=conversation.terminal_launch_args or [],
        )
        updates = {
            **ConfirmedPermissionState(tier, 1, "blocked").labels(harness),
            PERMISSION_MIGRATION_LABEL_KEY: tier,
        }
        async with store_slots or nullcontext():
            await asyncio.to_thread(self._conversation_store.set_labels, conversation.id, updates)
            refreshed = await asyncio.to_thread(
                self._conversation_store.get_conversation, conversation.id
            )
        if refreshed is None:
            raise ValueError("Permission migration lost its session")
        return refreshed

    def is_initialized(self, conversation: Conversation, runner_client: httpx.AsyncClient) -> bool:
        """Whether initialization accepted this session on its current tunnel."""
        runner_id, agent_id = conversation.runner_id, conversation.agent_id
        if runner_id is None or agent_id is None:
            return False
        session_key = (
            runner_id,
            self.generation_for(runner_id, runner_client),
            conversation.id,
            agent_id,
            conversation.sub_agent_name,
        )
        state = runner_archive_state(conversation)
        current_archive = (state.scope_id, state.revision, state.archived)
        startup_control = self._startup_control(conversation)
        for key, task in self._tasks.items():
            if (
                key[:5] != session_key
                or current_archive not in key[5]
                or key[7:]
                != (
                    conversation.labels.get(PERMISSION_STATE_LABEL_KEY),
                    conversation.labels.get(PERMISSION_RUNTIME_ID_LABEL_KEY),
                    conversation.host_id,
                    conversation.worktree or conversation.workspace,
                    *startup_control,
                )
            ):
                continue
            if not task.done() or task.cancelled() or task.exception() is not None:
                continue
            response = task.result()
            if (
                200 <= response.status_code < 300
                and runner_inference_verified(conversation, response)
                and runner_permission_verified(conversation, response)
            ):
                return True
        return False

    async def initialize(
        self,
        conversation: Conversation,
        runner_client: httpx.AsyncClient,
        *,
        timeout: float,
        suppress_recovery_turn: bool = False,
        archive_states: list[RunnerArchiveState] | None = None,
        resume_interrupted_turn: bool = False,
        generation: int | None = None,
        store_slots: asyncio.Semaphore | None = None,
    ) -> httpx.Response:
        """Initialize once for the current connection and persisted snapshot."""
        runner_id = conversation.runner_id
        agent_id = conversation.agent_id
        if runner_id is None or agent_id is None:
            raise ValueError("runner session initialization requires runner_id and agent_id")
        if generation is None:
            generation = self.generation_for(runner_id, runner_client)
        self.require_generation(runner_id, runner_client, generation)
        conversation = await self._migrate_legacy_permission_state(
            conversation, store_slots=store_slots
        )
        startup_control = self._startup_control(conversation)
        async with store_slots or nullcontext():
            peer = await self.resolve_peer_messaging(conversation)
        pkey = (runner_id, generation, conversation.id)
        effective_archive_states = archive_states or [runner_archive_state(conversation)]
        archive_key = tuple(
            (state.scope_id, state.revision, state.archived) for state in effective_archive_states
        )
        connection = self._registry.get(runner_id)
        if (
            connection is not None
            and self._agent_store is not None
            and self._agent_cache is not None
        ):
            from omnigent.server.mcp_compatibility import require_session_registry_mcp_support

            async with store_slots or nullcontext():
                await asyncio.to_thread(
                    require_session_registry_mcp_support,
                    conversation,
                    connection.hello.capabilities,
                    component="runner",
                    agent_store=self._agent_store,
                    agent_cache=self._agent_cache,
                )
            self.require_generation(runner_id, runner_client, generation)
        key = (
            runner_id,
            generation,
            conversation.id,
            agent_id,
            conversation.sub_agent_name,
            archive_key,
            resume_interrupted_turn,
            conversation.labels.get(PERMISSION_STATE_LABEL_KEY),
            conversation.labels.get(PERMISSION_RUNTIME_ID_LABEL_KEY),
            conversation.host_id,
            conversation.worktree or conversation.workspace,
            *startup_control,
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
        if task is None and self._agent_store is not None:
            async with store_slots or nullcontext():
                agent = await asyncio.to_thread(self._agent_store.get, agent_id)
            if agent is None:
                # The user removed the agent (`omnigent agent remove`). The runner
                # could only reject this init; the session reports the removal on
                # its next message, so this is expected and not worth a failure.
                _logger.info(
                    "Not initializing session %s on its runner: its agent %s was removed",
                    conversation.id,
                    agent_id,
                )
                return httpx.Response(
                    410,
                    json={
                        "error": ErrorCode.SESSION_AGENT_MISSING,
                        "detail": SESSION_AGENT_MISSING_MESSAGE,
                    },
                    request=httpx.Request("POST", "/v1/sessions"),
                )
            # Another caller may have started this initialization during the lookup.
            task = self._tasks.get(key)
        created = task is None
        if created:
            task = self._start_post(
                conversation,
                runner_client,
                key=key,
                pkey=pkey,
                runner_id=runner_id,
                peer=peer,
                timeout=timeout,
                suppress_recovery_turn=suppress_recovery_turn,
                archive_states=effective_archive_states,
                resume_interrupted_turn=resume_interrupted_turn,
                generation=generation,
                store_slots=store_slots,
            )
        # Record what the awaited post carries before it lands, so a switch
        # flip racing it (or an invalidation) cannot misattribute readiness.
        pending = self._pending_peer.get(pkey)
        if created:
            carried = peer
        elif pending is not None and pending[0] is task:
            carried = pending[1]
        else:
            carried = self._applied_peer.get(pkey, peer)
        response = await self._await_initialized(
            conversation,
            task,
            key,
            runner_id=runner_id,
            runner_client=runner_client,
            generation=generation,
        )
        if not (200 <= response.status_code < 300 and carried != peer):
            return response
        # The awaited post carried a stale value: post this call's value,
        # reusing a retry another joined caller already started, and never
        # resurrecting a key an invalidation removed.
        current = self._tasks.get(key)
        if current is task:
            self._tasks.pop(key, None)
            retry = self._start_post(
                conversation,
                runner_client,
                key=key,
                pkey=pkey,
                runner_id=runner_id,
                peer=peer,
                timeout=timeout,
                suppress_recovery_turn=suppress_recovery_turn,
                archive_states=effective_archive_states,
                resume_interrupted_turn=resume_interrupted_turn,
                generation=generation,
                store_slots=store_slots,
            )
        elif current is not None and self._pending_peer.get(pkey) == (current, peer):
            retry = current
        else:
            return response
        return await self._await_initialized(
            conversation,
            retry,
            key,
            runner_id=runner_id,
            runner_client=runner_client,
            generation=generation,
        )

    async def _advance_permission_epoch(
        self,
        conversation: Conversation,
        response: httpx.Response,
        *,
        store_slots: asyncio.Semaphore | None,
    ) -> Conversation | None:
        """Revoke old-process approvals before a replacement runner can start."""
        if response.status_code != 409 or self._conversation_store is None:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict) or body.get("error") != "permission_tier_epoch_changed":
            return None
        runtime_id = body.get("permission_tier_runtime_id")
        if not isinstance(runtime_id, str) or len(runtime_id) != 32:
            return None
        raw = conversation.labels.get(PERMISSION_STATE_LABEL_KEY)
        if raw is None:
            return None
        try:
            state = json.loads(raw)
        except ValueError:
            return None
        if (
            not isinstance(state, dict)
            or state.get("status") != "ready"
            or type(state.get("revision")) is not int
        ):
            return None
        blocked = json.dumps(
            {
                **state,
                "revision": state["revision"] + 1,
                "status": "blocked",
                "transaction_id": None,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        async with store_slots or nullcontext():
            changed = await asyncio.to_thread(
                self._conversation_store.compare_and_set_label,
                conversation.id,
                PERMISSION_STATE_LABEL_KEY,
                raw,
                blocked,
            )
            if not changed:
                return None
            await asyncio.to_thread(
                self._conversation_store.set_labels,
                conversation.id,
                {PERMISSION_RUNTIME_ID_LABEL_KEY: runtime_id},
            )
            return await asyncio.to_thread(
                self._conversation_store.get_conversation, conversation.id
            )

    async def _confirm_permission_ready(
        self,
        conversation: Conversation,
        response: httpx.Response,
        *,
        store_slots: asyncio.Semaphore | None,
    ) -> httpx.Response:
        """Stamp capability only for the revision this runner actually accepted."""
        raw = conversation.labels.get(PERMISSION_STATE_LABEL_KEY)
        if raw is None:
            return response
        if self._conversation_store is None:
            return httpx.Response(
                503,
                json={"error": "Permission state store unavailable"},
                request=httpx.Request("POST", "/v1/sessions"),
            )
        state = json.loads(raw)
        if state.get("status") == "ready":
            return response
        if state.get("status") != "blocked" or state.get("transaction_id") is not None:
            return httpx.Response(
                409,
                json={"error": "Permission state requires controlled recovery"},
                request=httpx.Request("POST", "/v1/sessions"),
            )
        ready = json.dumps({**state, "status": "ready"}, separators=(",", ":"), sort_keys=True)
        runtime_id = response.json().get("permission_tier_runtime_id")
        if not isinstance(runtime_id, str) or len(runtime_id) != 32:
            return httpx.Response(
                409,
                json={"error": "Runner permission runtime identity unavailable"},
                request=httpx.Request("POST", "/v1/sessions"),
            )
        async with store_slots or nullcontext():
            await asyncio.to_thread(
                self._conversation_store.set_labels,
                conversation.id,
                {
                    PERMISSION_CAPABILITY_LABEL_KEY: PERMISSION_CAPABILITY_VALUE,
                    PERMISSION_RUNTIME_ID_LABEL_KEY: runtime_id,
                },
            )
            changed = await asyncio.to_thread(
                self._conversation_store.compare_and_set_label,
                conversation.id,
                PERMISSION_STATE_LABEL_KEY,
                raw,
                ready,
            )
        if changed:
            return response
        return httpx.Response(
            409,
            json={"error": "Permission tier changed during initialization"},
            request=httpx.Request("POST", "/v1/sessions"),
        )

    def _start_post(
        self,
        conversation: Conversation,
        runner_client: httpx.AsyncClient,
        *,
        key: _SessionInitKey,
        pkey: tuple[str, int, str],
        runner_id: str,
        peer: bool,
        timeout: float,
        suppress_recovery_turn: bool,
        archive_states: list[RunnerArchiveState],
        resume_interrupted_turn: bool,
        generation: int,
        store_slots: asyncio.Semaphore | None,
    ) -> asyncio.Task[httpx.Response]:
        """Start one init post, tracking its carried value while in flight."""
        recovery_id = (
            self._recovery_ids.setdefault(key, uuid4().hex) if resume_interrupted_turn else None
        )

        async def post_session_init() -> httpx.Response:
            # Built here, not before create_task: the store read blocks,
            # and awaiting between the single-flight lookup and the task
            # registration would let a second caller start its own init.
            async with store_slots or nullcontext():
                global_instructions = (
                    await session_instructions_text(
                        conversation, quota_resolver=self._quota_resolver
                    )
                    if self._quota_resolver is not None
                    else await session_instructions_text(conversation)
                )
                project_code = await self.resolve_project_code(conversation)
            payload = build_runner_session_init_payload(
                conversation,
                server_version=self._server_version,
                suppress_recovery_turn=suppress_recovery_turn,
                archive_states=archive_states,
                peer_messaging_enabled=peer,
                global_instructions=global_instructions,
                project_code=project_code,
                resume_interrupted_turn=resume_interrupted_turn,
                recovery_id=recovery_id,
            )
            if self._conversation_store is not None and self._file_store is not None:
                from omnigent.server.routes._sessions.helpers import (
                    _filesystem_attachment_in_history,
                    require_filesystem_attachment_runtime,
                )

                async with store_slots or nullcontext():
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
            self.require_generation(runner_id, runner_client, generation)
            response = await self._post_initialize(
                runner_client,
                conversation=conversation,
                runner_id=runner_id,
                payload=payload,
                timeout=timeout,
                resume_interrupted_turn=resume_interrupted_turn,
                suppress_recovery_turn=suppress_recovery_turn,
                recovery_id=recovery_id,
                generation=generation,
            )
            refreshed = await self._advance_permission_epoch(
                conversation, response, store_slots=store_slots
            )
            if refreshed is not None:
                self.require_generation(runner_id, runner_client, generation)
                refreshed_payload = build_runner_session_init_payload(
                    refreshed,
                    server_version=self._server_version,
                    suppress_recovery_turn=suppress_recovery_turn,
                    archive_states=archive_states,
                    peer_messaging_enabled=peer,
                    global_instructions=global_instructions,
                    project_code=project_code,
                    resume_interrupted_turn=resume_interrupted_turn,
                    recovery_id=recovery_id,
                )
                response = await self._post_initialize(
                    runner_client,
                    conversation=refreshed,
                    runner_id=runner_id,
                    payload=refreshed_payload,
                    timeout=timeout,
                    resume_interrupted_turn=resume_interrupted_turn,
                    suppress_recovery_turn=suppress_recovery_turn,
                    recovery_id=recovery_id,
                    generation=generation,
                )
                conversation.labels = refreshed.labels
            if 200 <= response.status_code < 300 and runner_permission_verified(
                conversation, response
            ):
                response = await self._confirm_permission_ready(
                    conversation, response, store_slots=store_slots
                )
            if 200 <= response.status_code < 300:
                self._applied_peer[pkey] = peer
            return response

        task = asyncio.create_task(
            post_session_init(),
            name=f"runner-session-init-{conversation.id}",
        )
        self._tasks[key] = task
        self._pending_peer[pkey] = (task, peer)

        def _drop_finished(done: asyncio.Task[httpx.Response]) -> None:
            pending = self._pending_peer.get(pkey)
            if pending is not None and pending[0] is done:
                self._pending_peer.pop(pkey, None)
            failed = done.cancelled() or done.exception() is not None
            if self._tasks.get(key) is not done:
                return
            if failed:
                self._tasks.pop(key, None)
                return
            response = done.result()
            if response.status_code >= 400:
                self._tasks.pop(key, None)

        task.add_done_callback(_drop_finished)
        return task

    async def _await_initialized(
        self,
        conversation: Conversation,
        task: asyncio.Task[httpx.Response],
        key: _SessionInitKey,
        *,
        runner_id: str,
        runner_client: httpx.AsyncClient,
        generation: int,
    ) -> httpx.Response:
        """Await one post and normalize a rejected inference snapshot."""
        try:
            response = await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and current is not None and not current.cancelling():
                # Retiring a shared attempt must not cancel its independent callers.
                raise ConnectionError("runner session initialization was cancelled") from None
            raise
        except Exception:
            if self._tasks.get(key) is task:
                self._tasks.pop(key, None)
            raise
        self.require_generation(runner_id, runner_client, generation)
        if not runner_inference_verified(conversation, response):
            response = httpx.Response(
                409,
                json={"error": "The runner did not accept this session's inference configuration"},
                request=httpx.Request("POST", "/v1/sessions"),
            )
        if not runner_permission_verified(conversation, response):
            response = httpx.Response(
                409,
                json={"error": "Runner did not confirm this permission tier revision"},
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

        The current value is the one an in-flight post carries, since that
        post lands when it finishes; without one, the last applied value
        counts. Neither value present — a session whose runner just
        connected — reports not stale and picks the value up through its
        normal initialization.

        :param conversation: The session being initialized.
        :param runner_client: The runner client this request would post to.
        :returns: ``True`` when the current snapshot differs from the
            currently resolved one.
        """
        runner_id = conversation.runner_id
        if runner_id is None:
            return False
        generation = self.generation_for(runner_id, runner_client)
        pkey = (runner_id, generation, conversation.id)
        pending = self._pending_peer.get(pkey)
        current = pending[1] if pending is not None else self._applied_peer.get(pkey)
        if current is None:
            return False
        return current != await self.resolve_peer_messaging(conversation)

    def invalidate_session(
        self, session_id: str, *, runner_id: str | None = None
    ) -> list[asyncio.Task[httpx.Response]]:
        """Retire session readiness, optionally only for its former runner binding."""
        cancelled = []
        for key in list(self._tasks.keys() | self._recovery_ids.keys()):
            if key[2] == session_id and (runner_id is None or key[0] == runner_id):
                task = self._tasks.pop(key, None)
                if task is not None and not task.done():
                    task.cancel()
                    cancelled.append(task)
                self._recovery_ids.pop(key, None)
        for pkey in list(self._applied_peer.keys() | self._pending_peer.keys()):
            if pkey[2] == session_id and (runner_id is None or pkey[0] == runner_id):
                self._applied_peer.pop(pkey, None)
                self._pending_peer.pop(pkey, None)
        return cancelled

    async def _post_initialize(
        self,
        runner_client: httpx.AsyncClient,
        *,
        conversation: Conversation,
        runner_id: str,
        payload: dict[str, object],
        timeout: float,
        resume_interrupted_turn: bool,
        suppress_recovery_turn: bool,
        recovery_id: str | None,
        generation: int,
    ) -> httpx.Response:
        session_id = conversation.id
        with runner_log_scope(session_id, runner_id):
            # The flags name the caller: neither set is the tunnel-reconnect
            # hook, resume is a sub-agent restore, suppress is a message forward.
            _logger.info(
                "Initializing runner session",
                extra=debug_event(
                    "runner_session_init_started",
                    stage="session_init",
                    resume_interrupted_turn=resume_interrupted_turn,
                    suppress_recovery_turn=suppress_recovery_turn,
                    recovery_id=recovery_id,
                ),
            )
            try:
                required, attempt = self._startup_control(conversation)
                if required and attempt is None and self._startup_recovery_resolver is not None:
                    attempt = await self._startup_recovery_resolver(conversation)
                if required:
                    payload["startup_required"] = True
                    if attempt is not None:
                        payload["startup_attempt"] = attempt.model_dump()
                response = await runner_client.post(
                    "/v1/sessions",
                    json=payload,
                    timeout=timeout,
                    extensions={"runner_tunnel_generation": generation},
                )
            except (httpx.TransportError, ConnectionError) as exc:
                # Tunnel dropped mid-request; both callers recover on the next
                # runner reconnect, so log without a traceback. The request
                # rides the runner's tunnel: this is the runner going away.
                _logger.warning(
                    "Runner session initialization lost tunnel (%s: %s)",
                    type(exc).__name__,
                    exc,
                    extra=debug_event(
                        "runner_session_init_failed",
                        stage="session_init",
                        exc_type=type(exc).__name__,
                        error_category=ErrorCategory.RUNNER.value,
                        error_impact=ErrorImpact.TRANSIENT.value,
                    ),
                )
                raise
            except Exception:
                _logger.exception(
                    "Runner session initialization failed",
                    extra=debug_event(
                        "runner_session_init_failed",
                        stage="session_init",
                    ),
                )
                raise
            if 200 <= response.status_code < 300:
                _logger.info(
                    "Runner session initialization finished",
                    extra=debug_event(
                        "runner_session_initialized",
                        stage="session_init",
                        status_code=response.status_code,
                    ),
                )
            else:
                body = runner_response_error_body(response)
                log = (
                    _logger.warning
                    if response.status_code in TRANSIENT_REJECTION_STATUSES
                    else _logger.error
                )
                log(
                    "Runner session initialization rejected with HTTP %d: %s",
                    response.status_code,
                    body,
                    # Keeps the failed event name: launch-success KPIs count it.
                    extra=debug_event(
                        "runner_session_init_failed",
                        stage="session_init",
                        failure_kind="rejected",
                        status_code=response.status_code,
                        response_body=body,
                    ),
                )
            return response

    def invalidate_runner(
        self, runner_id: str, *, generation: int | None = None
    ) -> list[asyncio.Task[httpx.Response]]:
        """Forget readiness and cancel work belonging to a retired connection."""
        for key in list(self._recovery_ids):
            if key[0] == runner_id and (generation is None or key[1] == generation):
                self._recovery_ids.pop(key)
        stale = [
            key
            for key in self._tasks
            if key[0] == runner_id and (generation is None or key[1] == generation)
        ]
        cancelled = []
        for key in stale:
            task = self._tasks.pop(key)
            if not task.done():
                task.cancel()
                cancelled.append(task)
        for pkey in list(self._applied_peer.keys() | self._pending_peer.keys()):
            if pkey[0] == runner_id and (generation is None or pkey[1] == generation):
                self._applied_peer.pop(pkey, None)
                self._pending_peer.pop(pkey, None)
        return cancelled
