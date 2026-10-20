"""Schedule durable startup identities without owning or replaying their input."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from omnigent.entities.session_startup import StartupReceipt
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID, ConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore, StartupSnapshot

_logger = logging.getLogger(__name__)


class StartupCoordinator:
    """Launch callbacks enforce the issued attempt again at the cold boundary."""

    def __init__(
        self,
        startups: SessionStartupStore,
        conversations: ConversationStore,
        *,
        eligible: Callable[[StartupSnapshot], Awaitable[bool]],
        launch: Callable[[StartupSnapshot], Awaitable[None]],
    ) -> None:
        self.startups = startups
        self.conversations = conversations
        self._eligible = eligible
        self._launch = launch
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._admissions = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def enqueue(
        self,
        session_id: str,
        *,
        input_id: str | None,
        owner_user_id: str,
        operation_key: str,
        parent_startup_id: str | None = None,
        runtime_generation: int = 1,
        reuse_startup_id: str | None = None,
    ) -> StartupSnapshot:
        """The entry point authorizes the owner and completes child registration first."""
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.host_id is None or conv.archived:
            raise ValueError("Startup requires a live session bound to a host")
        if input_id is not None:
            item = await asyncio.to_thread(self.conversations.get_item, session_id, input_id)
            if (
                item is None
                or item.response_id != PENDING_INPUT_RESPONSE_ID
                or item.status != "in_progress"
            ):
                raise ValueError("Startup requires its canonical queued pending input")
        return await asyncio.to_thread(
            self.startups.enqueue,
            owner_user_id=owner_user_id,
            host_id=conv.host_id,
            session_id=session_id,
            operation_key=operation_key,
            input_id=input_id,
            runtime_generation=runtime_generation,
            parent_startup_id=parent_startup_id,
            reuse_startup_id=reuse_startup_id,
        )

    async def advance(self, host_id: str) -> None:
        """Compatibility and other prerequisites are checked before reserving capacity."""
        if self._closing:
            return
        self._admissions += 1
        self._idle.clear()
        try:
            await self._advance(host_id)
        finally:
            self._admissions -= 1
            if self._admissions == 0:
                self._idle.set()

    async def _valid_input(self, startup: StartupSnapshot) -> bool:
        conv = await asyncio.to_thread(self.conversations.get_conversation, startup.session_id)
        if conv is None or conv.archived:
            return False
        if startup.preparation is None:
            if conv.host_id != startup.host_id:
                return False
        elif (conv.host_id, conv.runner_id) != (
            startup.preparation["previous_host_id"],
            startup.preparation["previous_runner_id"],
        ):
            return False
        if startup.input_id is None:
            return True
        item = await asyncio.to_thread(
            self.conversations.get_item, startup.session_id, startup.input_id
        )
        return (
            item is not None
            and item.response_id == PENDING_INPUT_RESPONSE_ID
            and item.status == "in_progress"
        )

    async def _advance(self, host_id: str) -> None:
        eligible_ids = set()
        for startup in await asyncio.to_thread(self.startups.list_queued, host_id):
            if not await self._valid_input(startup):
                await asyncio.to_thread(self.startups.cancel, startup.id)
                continue
            if await self._eligible(startup):
                eligible_ids.add(startup.id)
        if not eligible_ids:
            return
        for _ in range(2):
            if self._closing:
                break
            attempt = await asyncio.to_thread(
                self.startups.reserve, host_id, eligible_ids=eligible_ids
            )
            if attempt is None:
                break
            if self._closing:
                await asyncio.to_thread(
                    self.startups.cancel_unissued, attempt.id, attempt.attempt_id
                )
                break
            task = asyncio.create_task(
                self._dispatch(attempt), name=f"session-startup-{attempt.id}"
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _dispatch(self, attempt: StartupSnapshot) -> None:
        issued = False
        try:
            valid = await self._valid_input(attempt)
            if self._closing or not valid:
                await asyncio.to_thread(
                    self.startups.cancel_unissued, attempt.id, attempt.attempt_id
                )
                return
            issued = True
            await self._launch(attempt)
        except asyncio.CancelledError:
            if issued:
                await asyncio.shield(
                    asyncio.to_thread(
                        self.startups.mark_uncertain,
                        attempt.id,
                        attempt.attempt_id,
                        reason="coordinator_interrupted",
                    )
                )
            else:
                await asyncio.shield(
                    asyncio.to_thread(
                        self.startups.cancel_unissued, attempt.id, attempt.attempt_id
                    )
                )
            raise
        except Exception:  # noqa: BLE001 -- every ambiguous dispatch retains its permit
            if issued:
                # A callback may have sent its command before losing the response.
                await asyncio.to_thread(
                    self.startups.mark_uncertain,
                    attempt.id,
                    attempt.attempt_id,
                    reason="launch_response_unconfirmed",
                )
                _logger.warning("Startup dispatch unconfirmed")
            else:
                await asyncio.to_thread(
                    self.startups.cancel_unissued,
                    attempt.id,
                    attempt.attempt_id,
                    reason="startup_preflight_failed",
                )
                _logger.warning("Startup preflight failed before dispatch")

    async def observe(
        self,
        receipt: StartupReceipt,
        *,
        session_id: str,
        host_id: str,
        runtime_id: str,
        runner_id: str,
        host_generation: str,
    ) -> bool:
        """Bindings here come from the authenticated transport, never the message body."""
        current = await asyncio.to_thread(self.startups.get, receipt.startup_id)
        if (
            current is None
            or current.session_id != session_id
            or current.host_id != host_id
            or receipt.runtime_id != runtime_id
        ):
            return False
        accepted = await asyncio.to_thread(
            self.startups.observe,
            receipt,
            expected_runner_id=runner_id,
            host_generation=host_generation,
        )
        if accepted:
            await self.advance(host_id)
        return accepted

    async def drain(self) -> None:
        """Wait for issued control calls; first-turn observation is independent."""
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        self._closing = True
        await self._idle.wait()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
