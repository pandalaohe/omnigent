"""Compose host startup permits with the existing saved-input worker."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.server_config import StartupAdmissionPolicy
from omnigent.server.session_startup import StartupCoordinator
from omnigent.startup_protocol import StartupAttemptRef
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
        self.coordinator = StartupCoordinator(
            startups, conversations, eligible=self._eligible, launch=self._launch
        )

    def bind(self, app: Any) -> None:
        self._app = app
        self.hosts.startup_admitter = self.admit_launch

    async def resolve_harness(self, startup: StartupSnapshot) -> str | None:
        from omnigent.server.routes._sessions.helpers import _resolve_harness_async

        conv = await asyncio.to_thread(self.conversations.get_conversation, startup.session_id)
        return await _resolve_harness_async(conv) if conv is not None else None

    async def _eligible(self, startup: StartupSnapshot) -> bool:
        conn = self.hosts.get(startup.host_id)
        if conn is None or not conn.hello.connection_id:
            return False
        capabilities = conn.hello.capabilities
        if "startup_admission_v1" not in capabilities:
            return False
        harness = await self.resolve_harness(startup)
        return (
            self.hosts.get(startup.host_id) is conn
            and harness is not None
            and "startup_admission_v1" in conn.hello.capabilities
            and f"startup_receipts:{harness}" in conn.hello.capabilities
        )

    async def queue_session(self, conn: HostConnection, session_id: str) -> StartupSnapshot:
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.archived or conv.host_id != conn.host_id:
            raise OmnigentError("Session host binding changed", code=ErrorCode.CONFLICT)
        current = await asyncio.to_thread(self.startups.for_session, session_id)
        if current is not None:
            return current
        pending = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        head = next((item for item in pending if item.status != "failed"), None)
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

    async def admit_launch(self, conn: HostConnection, session_id: str) -> StartupAttemptRef:
        scope = _dispatch.get()
        if scope is not None and scope[0] == session_id:
            ref = scope[1]
            current = await asyncio.to_thread(self.startups.get, ref.startup_id)
            if (
                self.hosts.get(conn.host_id) is conn
                and conn.hello.connection_id == ref.host_generation
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
        generation = conn.hello.connection_id if conn is not None else None
        if (
            self._app is None
            or conn is None
            or not generation
            or startup.attempt_id is None
            or not await self._eligible(startup)
            or conn.hello.connection_id != generation
        ):
            await asyncio.to_thread(self.startups.cancel_unissued, startup.id, startup.attempt_id)
            return
        ref = StartupAttemptRef(
            startup_id=startup.id,
            attempt_id=startup.attempt_id,
            input_id=startup.input_id,
            runtime_id=startup.attempt_id,
            host_generation=generation,
        )
        if not await asyncio.to_thread(
            self.startups.claim, startup.id, startup.attempt_id, runtime_id=ref.runtime_id
        ):
            return
        token = _dispatch.set((startup.session_id, ref))
        try:
            if startup.input_id is not None:
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
            else:
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
        await self.queue_session(conn, session_id)
        await self.coordinator.advance(conn.host_id)
        return True

    async def shutdown(self) -> None:
        await self.coordinator.shutdown()
