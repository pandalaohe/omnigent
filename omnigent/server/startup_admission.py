"""Compose host startup permits with the existing saved-input worker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.server_config import StartupAdmissionPolicy
from omnigent.server.session_startup import StartupCoordinator
from omnigent.server.startup_registration import (
    STARTUP_CREATE_PREPARATION_LABEL_KEY,
    STARTUP_CREATE_PREPARATION_PENDING,
    STARTUP_REGISTRATION_LABEL_KEY,
    STARTUP_REGISTRATION_PENDING,
)
from omnigent.startup_protocol import SDK_STARTUP_SOURCES, StartupAttemptRef
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore, StartupSnapshot


class StartupQueued(OmnigentError):
    """No cold command was issued; the accepted startup stays durable."""

    def __init__(self, startup_id: str | None) -> None:
        super().__init__("Session startup is queued", code=ErrorCode.CONFLICT)
        self.startup_id = startup_id


_dispatch: ContextVar[tuple[str, StartupAttemptRef] | None] = ContextVar(
    "startup_dispatch", default=None
)


def active_startup_attempt(session_id: str) -> StartupAttemptRef | None:
    """Return the active dispatch only for its exact session."""
    scope = _dispatch.get()
    return scope[1] if scope is not None and scope[0] == session_id else None


class StartupAdmission:
    def __init__(
        self,
        startups: SessionStartupStore,
        conversations: ConversationStore,
        hosts: HostRegistry,
        policy: StartupAdmissionPolicy,
    ) -> None:
        self.startups, self.conversations, self.hosts, self.policy = (
            startups,
            conversations,
            hosts,
            policy,
        )
        self._app: Any = None
        self.prepare_dispatch: Callable[[Any, StartupSnapshot], Awaitable[None]] | None = None
        self.coordinator = StartupCoordinator(
            startups, conversations, eligible=self._eligible, launch=self._launch
        )

    def bind(self, app: Any) -> None:
        self._app = app
        app.state.startup_store = self.startups
        app.state.startup_admission = self
        self.hosts.startup_admitter = self.admit_launch
        inputs = app.state.startup_inputs
        if inputs is not None:
            inputs.on_saved = self.input_saved
            inputs.cancel_attempt = self.cancel_session

    async def input_saved(self, session_id: str, _input_id: str) -> None:
        current = await asyncio.to_thread(self.startups.for_session, session_id)
        if current is not None and current.state == "queued" and current.input_id is None:
            pending = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
            head = next((item for item in pending if item.status != "failed"), None)
            if head is not None and head.status == "in_progress":
                await asyncio.to_thread(self.startups.attach_queued_input, current.id, head.id)

    async def cancel_session(self, session_id: str) -> bool:
        cancelled = await asyncio.to_thread(self.startups.cancel_session_current, session_id)
        return cancelled is not None and cancelled.state == "cancelled" and not cancelled.resident

    async def resolve_harness(self, startup: StartupSnapshot) -> str | None:
        from omnigent.server.routes._sessions.helpers import _resolve_harness_async

        conv = await asyncio.to_thread(self.conversations.get_conversation, startup.session_id)
        return await _resolve_harness_async(conv) if conv is not None else None

    async def _eligible(self, startup: StartupSnapshot) -> bool:
        conv = await asyncio.to_thread(self.conversations.get_conversation, startup.session_id)
        if (
            conv is None
            or conv.archived
            or conv.labels.get(STARTUP_REGISTRATION_LABEL_KEY) == STARTUP_REGISTRATION_PENDING
            or conv.labels.get(STARTUP_CREATE_PREPARATION_LABEL_KEY)
            == STARTUP_CREATE_PREPARATION_PENDING
        ):
            return False
        if conv.parent_conversation_id is not None:
            parent = await asyncio.to_thread(
                self.startups.for_session, conv.parent_conversation_id
            )
            if parent is None or parent.state not in (
                "working",
                "ready_empty",
                "command_completed",
            ):
                return False
            if startup.state == "queued":
                if not await asyncio.to_thread(
                    self.startups.attach_parent_dependency, startup.id, parent.id
                ):
                    return False
            elif startup.parent_startup_id != parent.id:
                return False
        conn = self.hosts.get(startup.host_id)
        if conn is None:
            return False
        capabilities = conn.hello.capabilities
        if "startup_admission_v1" not in capabilities:
            return False
        if not await self.inventory_ready(conn, startup.session_id):
            return False
        harness = await self.resolve_harness(startup)
        return (
            self.hosts.get(startup.host_id) is conn
            and harness is not None
            and "startup_admission_v1" in conn.hello.capabilities
            and f"startup_receipts:{harness}" in conn.hello.capabilities
        )

    async def inventory_ready(self, conn: HostConnection, session_id: str) -> bool:
        """Compatibility and partial owner evidence cannot authorize resident adoption."""
        if self._app is not None and "resident_inventory_v1" in conn.hello.capabilities:
            from omnigent.server.resident_inventory import retain_unknown_owner_inventory

            await retain_unknown_owner_inventory(
                self._app, self.conversations, self.hosts, self.startups, conn, session_id
            )
        # SDK contexts live in harness owners, and native side-thread aliases
        # remain incomplete. No current snapshot establishes full host coverage.
        return False

    async def queue_session(self, conn: HostConnection, session_id: str) -> StartupSnapshot:
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.archived or conv.host_id != conn.host_id:
            raise OmnigentError("Session host binding changed", code=ErrorCode.CONFLICT)
        if (
            conv.labels.get(STARTUP_CREATE_PREPARATION_LABEL_KEY)
            == STARTUP_CREATE_PREPARATION_PENDING
        ):
            raise StartupQueued(None)
        current = await asyncio.to_thread(self.startups.for_session, session_id)
        pending = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        head = next((item for item in pending if item.status != "failed"), None)
        if current is not None:
            if (
                current.state == "queued"
                and current.input_id is None
                and head is not None
                and head.status == "in_progress"
            ):
                attached = await asyncio.to_thread(
                    self.startups.attach_queued_input, current.id, head.id
                )
                return attached or current
            if (
                current.state in ("ready_empty", "waiting_input")
                and head is not None
                and head.status == "in_progress"
            ):
                return await self.coordinator.enqueue(
                    session_id,
                    input_id=head.id,
                    owner_user_id=conn.owner or "__local__",
                    operation_key=f"first-input:{head.id}",
                    runtime_generation=current.runtime_generation + 1,
                    reuse_startup_id=current.id if current.state == "ready_empty" else None,
                )
            return current
        if head is not None and head.status != "in_progress":
            # A submitted input predates admission. Reconcile it instead of replaying.
            raise StartupQueued(None)
        await asyncio.to_thread(
            self.startups.configure,
            conn.host_id,
            host_resident_limit=self.policy.host_resident_limit,
            deployment_resident_limit=self.policy.deployment_resident_limit,
            startup_limit=self.policy.startup_limit,
        )
        return await self.coordinator.enqueue(
            session_id,
            input_id=head.id if head else None,
            owner_user_id=conn.owner or "__local__",
            operation_key=f"startup:{session_id}",
        )

    async def queue_preparation(
        self, conn: HostConnection, session_id: str, preparation: dict
    ) -> StartupSnapshot:
        current = await asyncio.to_thread(self.startups.for_session, session_id)
        if (
            current is not None
            and current.state == "blocked"
            and not current.resident
            and current.reason == "preparation_refused"
        ):
            current = await asyncio.to_thread(
                self.startups.cancel_current, current.id, expected_attempt_id=current.attempt_id
            )
            if current is None:
                raise OmnigentError("Startup preparation changed", code=ErrorCode.CONFLICT)
        if (
            current is not None
            and current.preparation is not None
            and current.state not in ("cancelled", "stopped")
        ):
            fields = ("session_id", "workspace", "git", "user_id", "account_generation")
            if current.host_id == conn.host_id and all(
                current.preparation[name] == preparation[name] for name in fields
            ):
                return current
            raise OmnigentError("Startup preparation changed", code=ErrorCode.CONFLICT)
        pending = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        head = next((item for item in pending if item.status != "failed"), None)
        if head is not None and head.status != "in_progress":
            raise OmnigentError("Previous input requires reconciliation", code=ErrorCode.CONFLICT)
        if current is not None and current.state == "queued" and current.host_id == conn.host_id:
            attached = await asyncio.to_thread(
                self.startups.attach_preparation, current.id, preparation
            )
            if head is not None and attached.input_id is None:
                attached = await asyncio.to_thread(
                    self.startups.attach_queued_input, attached.id, head.id
                )
            return attached or current
        if current is not None and (
            current.resident or current.state not in ("stopped", "cancelled")
        ):
            raise OmnigentError(
                "Previous startup must be reconciled first", code=ErrorCode.CONFLICT
            )
        await asyncio.to_thread(
            self.startups.configure,
            conn.host_id,
            host_resident_limit=self.policy.host_resident_limit,
            deployment_resident_limit=self.policy.deployment_resident_limit,
            startup_limit=self.policy.startup_limit,
        )
        generation = current.runtime_generation + 1 if current else 1
        return await asyncio.to_thread(
            self.startups.enqueue,
            owner_user_id=conn.owner or "__local__",
            host_id=conn.host_id,
            session_id=session_id,
            operation_key=f"prepare:{session_id}:{generation}",
            input_id=head.id if head else None,
            runtime_generation=generation,
            preparation=preparation,
        )

    def dispatching(self, session_id: str) -> bool:
        scope = _dispatch.get()
        return scope is not None and scope[0] == session_id

    async def _runner_available(self, session_id: str) -> bool:
        router = getattr(self._app.state, "runner_router", None)
        if router is None:
            return False
        try:
            return (
                await asyncio.to_thread(router.client_for_existing_conversation, session_id)
                is not None
            )
        except OmnigentError:
            return False

    async def _resume_prepared_input(self, current: StartupSnapshot, conn: HostConnection) -> bool:
        if (
            current.state not in ("verifying", "uncertain")
            or current.preparation is None
            or current.input_id is None
            or current.attempt_id is None
            or current.runtime_id is None
            or current.host_generation != conn.startup_generation
            or self.hosts.get(conn.host_id) is not conn
            or not await self._runner_available(current.session_id)
        ):
            return False
        item = await asyncio.to_thread(
            self.conversations.get_item, current.session_id, current.input_id
        )
        if item is None or item.status != "in_progress":
            return False
        ref = StartupAttemptRef(
            startup_id=current.id,
            attempt_id=current.attempt_id,
            input_id=current.input_id,
            runtime_id=current.runtime_id,
            host_generation=conn.startup_generation,
        )
        token = _dispatch.set((current.session_id, ref))
        try:
            outcome = await self._app.state.startup_inputs._deliver(
                self._app, current.session_id, item, startup=ref
            )
            if outcome == "unconfirmed":
                await asyncio.to_thread(
                    self.startups.mark_uncertain,
                    current.id,
                    current.attempt_id,
                    reason="input_dispatch_unconfirmed",
                )
            return outcome == "submitted"
        finally:
            _dispatch.reset(token)

    async def admit_launch(self, conn: HostConnection, session_id: str) -> StartupAttemptRef:
        scope = _dispatch.get()
        if scope is not None and scope[0] == session_id:
            ref = scope[1]
            current = await asyncio.to_thread(self.startups.get, ref.startup_id)
            if (
                self.hosts.get(conn.host_id) is conn
                and conn.startup_generation == ref.host_generation
                and current is not None
                and current.host_id == conn.host_id
                and current.attempt_id == ref.attempt_id
                and current.runtime_id == ref.runtime_id
                and current.state in ("starting", "verifying")
            ):
                return ref
            raise StartupQueued(ref.startup_id)
        if self.hosts.get(conn.host_id) is not conn:
            raise StartupQueued(None)
        startup = await self.queue_session(conn, session_id)
        raise StartupQueued(startup.id)

    async def _launch(self, startup: StartupSnapshot) -> None:
        conn = self.hosts.get(startup.host_id)
        generation = conn.startup_generation if conn is not None else None
        if (
            self._app is None
            or conn is None
            or not generation
            or startup.attempt_id is None
            or not await self._eligible(startup)
            or conn.startup_generation != generation
        ):
            await asyncio.to_thread(self.startups.cancel_unissued, startup.id, startup.attempt_id)
            return
        ref = StartupAttemptRef(
            startup_id=startup.id,
            attempt_id=startup.attempt_id,
            input_id=startup.input_id,
            runtime_id=startup.runtime_id or startup.attempt_id,
            host_generation=generation,
        )
        if not await asyncio.to_thread(
            self.startups.claim, startup.id, startup.attempt_id, runtime_id=ref.runtime_id
        ):
            return
        token = _dispatch.set((startup.session_id, ref))
        try:
            if startup.preparation is not None:
                if self.prepare_dispatch is None:
                    raise StartupQueued(startup.id)
                await self.prepare_dispatch(self._app, startup)
            if startup.input_id is not None:
                if startup.preparation is not None and not await self._runner_available(
                    startup.session_id
                ):
                    return
                item = await asyncio.to_thread(
                    self.conversations.get_item, startup.session_id, startup.input_id
                )
                if item is None:
                    raise StartupQueued(startup.id)
                outcome = await self._app.state.startup_inputs._deliver(
                    self._app, startup.session_id, item, startup=ref
                )
                if outcome != "submitted":
                    discarded = outcome == "not_dispatched" and await asyncio.to_thread(
                        self.startups.discard_ungranted_attempt,
                        startup.id,
                        ref.attempt_id,
                        runtime_id=ref.runtime_id,
                    )
                    if not discarded:
                        await asyncio.to_thread(
                            self.startups.mark_uncertain,
                            startup.id,
                            ref.attempt_id,
                            reason="input_dispatch_unconfirmed",
                        )
            elif startup.preparation is None:
                from omnigent.server.routes.sessions import _launch_runner_on_host

                conv = await asyncio.to_thread(
                    self.conversations.get_conversation, startup.session_id
                )
                if conv is None or self.hosts.get(startup.host_id) is not conn:
                    raise StartupQueued(startup.id)
                attempt = await _launch_runner_on_host(conv, self.conversations, self.hosts, conn)
                if attempt.error or attempt.error_code:
                    await asyncio.to_thread(
                        self.startups.mark_uncertain,
                        startup.id,
                        startup.attempt_id,
                        reason="host_launch_unconfirmed",
                    )
        finally:
            _dispatch.reset(token)

    async def recover_session(self, session_id: str, conn: HostConnection) -> bool:
        current = await asyncio.to_thread(self.startups.for_session, session_id)
        if current is not None and current.state in ("starting", "verifying", "uncertain"):
            from omnigent.server.startup_observations import reconcile_startup_observations

            await reconcile_startup_observations(
                self._app, self.conversations, self.hosts, self.startups, current
            )
        await self.queue_session(conn, session_id)
        await self.advance(conn.host_id)
        current = await asyncio.to_thread(self.startups.for_session, session_id)
        if (
            current is not None
            and current.state in ("starting", "verifying")
            and current.input_id is None
            and current.attempt_id is not None
            and current.runtime_id is not None
            and current.host_generation == conn.startup_generation
            and await self.resolve_harness(current) in SDK_STARTUP_SOURCES
            and await self._runner_available(session_id)
        ):
            conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
            if (
                conv is not None
                and conv.runner_id is not None
                and self.hosts.get(conn.host_id) is conn
                and await asyncio.to_thread(
                    self.startups.defer_empty_sdk,
                    current.id,
                    current.attempt_id,
                    runtime_id=current.runtime_id,
                    host_generation=conn.startup_generation,
                    expected_runner_id=conv.runner_id,
                )
            ):
                await self.coordinator.advance(conn.host_id)
                return False
        if (
            current is not None
            and current.preparation is not None
            and current.input_id is not None
        ):
            return await self._resume_prepared_input(current, conn)
        # Receipt waiting has no pending command. The host worker wakes on
        # notifications and uses a bounded idle poll instead of a busy loop.
        return False

    async def initialization_attempt(self, conversation: Conversation) -> StartupAttemptRef | None:
        """Keep a verified owned attempt observable across runner tunnel reconnects."""
        current = await asyncio.to_thread(self.startups.for_session, conversation.id)
        if (
            current is None
            or current.state
            not in ("starting", "verifying", "uncertain", "working", "ready_empty")
            or current.attempt_id is None
            or current.runtime_id is None
            or current.host_generation is None
            or conversation.archived
            or not conversation.runner_id
            or conversation.host_id != current.host_id
        ):
            return None
        conn = self.hosts.get(current.host_id)
        live = await asyncio.to_thread(self.conversations.get_conversation, conversation.id)
        if (
            conn is None
            or conn.startup_generation != current.host_generation
            or self.hosts.get(current.host_id) is not conn
            or live is None
            or live.archived
            or (live.host_id, live.runner_id) != (current.host_id, conversation.runner_id)
        ):
            return None
        return StartupAttemptRef(
            startup_id=current.id,
            attempt_id=current.attempt_id,
            input_id=current.input_id,
            runtime_id=current.runtime_id,
            host_generation=current.host_generation,
        )

    async def advance(self, host_id: str) -> None:
        """Observe fixed deadlines before admitting another cold attempt."""
        from omnigent.runtime import session_stream
        from omnigent.server.schemas import SessionStartupEvent

        changed = await asyncio.to_thread(self.startups.expire_observations, host_id)
        for startup in changed:
            status = await asyncio.to_thread(self.startups.presentation, startup.session_id)
            session_stream.publish(
                startup.session_id,
                SessionStartupEvent(
                    type="session.startup", conversation_id=startup.session_id, startup=status
                ).model_dump(),
            )
        await self.coordinator.advance(host_id)

    async def shutdown(self) -> None:
        await self.coordinator.shutdown()
