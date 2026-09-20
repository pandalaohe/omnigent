"""Peer-message sweeper — deferred delivery, expiry, and back-notices.

A ``session_peer_messages`` record that could not deliver inline
(``pending`` / ``queued``) waits for this background loop: every tick it
expires overdue records, fails records whose receiver has closed, and
delivers the rest once the receiver's true state goes idle, through the
same ``deliver`` callable the inline send route uses
(``routes_peer._deliver``). Every terminal transition — delivered, failed,
expired — and the action route's ``refuse`` post a ``[System: ...]``
back-notice to the sender, idle-gated the same way: notices generated
while the sender is mid-turn park in an in-memory per-sender queue and
post as one batched message on a later tick when the sender goes idle.
The parked queue is process-local and does not survive a restart (a
survivable choice: the ``session_peer_messages`` row itself is durable and
the sweeper's own tick keeps searching for it).

On startup, before the first tick, every ``delivering`` record left behind
by a crash between runner acceptance and its own transition is reconciled
against the receiver's transcript.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from starlette.requests import Request

from omnigent.db.utils import now_epoch
from omnigent.entities import SessionPeerMessage
from omnigent.entities.conversation import Conversation
from omnigent.server.routes.sessions.routes_peer import (
    effective_owner_id,
    format_peer_back_notice,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.stores import ConversationStore
from omnigent.stores.peer_message_store import PeerMessageStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.util.session_lifecycle import is_session_closed

_logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 2.0
_DEFAULT_BATCH_LIMIT = 500
_RECONCILE_GRACE_S = 120

TrueStateFn = Callable[[Conversation], Awaitable[tuple[str, bool | None]]]
DeliverFn = Callable[..., Awaitable[tuple[str, str | None]]]


class PeerSweeper:
    """Deliver due peer-message records and post idle-gated back-notices.

    Shaped like :class:`~omnigent.server.managed_sandbox_reaper.ManagedSandboxReaper`
    (``start``/``shutdown`` own one background task; ``_run`` loops on
    ``asyncio.sleep``), except ``start`` also takes the owning FastAPI app —
    delivery and notices post through a synthetic ``Request`` this sweeper
    builds itself (no inbound HTTP request drives a tick).
    """

    def __init__(
        self,
        *,
        peer_store: PeerMessageStore,
        conversation_store: ConversationStore,
        permission_store: PermissionStore | None,
        true_state: TrueStateFn,
        deliver: DeliverFn,
        post_event_impl: Callable[..., Awaitable[Any]],
        interval: float = _DEFAULT_INTERVAL_S,
        clock: Callable[[], int] = now_epoch,
        batch_limit: int = _DEFAULT_BATCH_LIMIT,
    ) -> None:
        """
        :param peer_store: Durable record store.
        :param conversation_store: Store for fresh sender/receiver reads —
            a due record only carries ids, never a session's live state.
        :param permission_store: Permission store, for owner resolution
            (``None`` in single-user local mode, where every resolution
            passes as ``None``).
        :param true_state: ``routes_peer._true_state`` — the D6/D7
            offline/not_ready/busy/idle gate, reused verbatim so delivery
            readiness and the notice idle-gate agree with the inline route.
        :param deliver: ``routes_peer._deliver`` — the shared envelope +
            post + outcome-mapping step the inline route also calls.
        :param post_event_impl: The raw events-post callable, for back-
            notices (no envelope, no native-ready probe — just a message).
        :param interval: Seconds between ticks.
        :param clock: Epoch-seconds clock, overridable for expiry tests.
        :param batch_limit: Max records per ``list_due`` page.
        """
        self._store = peer_store
        self._conversation_store = conversation_store
        self._permission_store = permission_store
        self._true_state = true_state
        self._deliver = deliver
        self._post_event_impl = post_event_impl
        self._interval = interval
        self._clock = clock
        self._batch_limit = batch_limit
        self._parked: dict[str, list[str]] = {}
        self._flush_locks: dict[str, asyncio.Lock] = {}
        self._app: Any | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self, app: Any) -> None:
        """Reconcile crash-orphaned ``delivering`` records, then start the loop."""
        if self._task is not None and not self._task.done():
            return
        self._app = app
        await self._reconcile_startup()
        self._task = asyncio.create_task(self._run(), name="peer-sweeper")

    async def shutdown(self) -> None:
        """Stop the loop and wait for cancellation to settle."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def notify(
        self,
        record: SessionPeerMessage,
        receiver_title: str | None,
        *,
        app: Any,
    ) -> None:
        """Park (and maybe flush) a back-notice for a route-driven transition.

        The action route's ``refuse`` calls this directly — it always has
        a live inbound ``request.app`` to pass, so it does not need
        :meth:`start` to have run first.
        """
        await self._notify_for(record, record.state, record.reason, receiver_title, app)

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Peer sweeper tick failed; retrying later")
            await asyncio.sleep(self._interval)

    async def _tick(self) -> None:
        """One sweep: process due records, reconcile stale deliveries, flush notices."""
        assert self._app is not None, "PeerSweeper.start() must run before _tick()"
        now = self._clock()
        try:
            due = await asyncio.to_thread(
                self._store.list_due, ("pending", "queued", "held"), self._batch_limit
            )
        except Exception:
            _logger.exception("Peer sweeper failed to list due records")
            due = []
        for record in due:
            try:
                await self._process_due(record, now)
            except Exception:
                _logger.exception("Peer sweeper failed to process record %s", record.id)
        await self._reconcile_stale_delivering(now)
        for sender_id in list(self._parked.keys()):
            if not self._parked.get(sender_id):
                continue
            try:
                sender = await asyncio.to_thread(
                    self._conversation_store.get_conversation, sender_id
                )
                if sender is not None:
                    await self._maybe_flush(sender, self._app)
            except Exception:
                _logger.exception("Peer sweeper failed to flush notices for sender %s", sender_id)

    async def _process_due(self, record: SessionPeerMessage, now: int) -> None:
        receiver = await asyncio.to_thread(
            self._conversation_store.get_conversation, record.receiver_session_id
        )
        receiver_title = receiver.title if receiver is not None else None
        if record.expires_at <= now:
            moved = await asyncio.to_thread(
                self._store.transition, record.id, "expired", None, (record.state,)
            )
            if moved:
                await self._notify_for(record, "expired", None, receiver_title, self._app)
            return
        if (
            receiver is None
            or is_session_closed(receiver.labels, receiver.title)
            or receiver.archived_at is not None
        ):
            moved = await asyncio.to_thread(
                self._store.transition, record.id, "failed", "closed", (record.state,)
            )
            if moved:
                await self._notify_for(record, "failed", "closed", receiver_title, self._app)
            return
        if record.state == "held":
            # Held records only expire (above) or get released by the
            # action route (held -> pending); the sweeper never delivers
            # one on its own.
            return
        # Captured before the CAS below: some store implementations hand
        # back the same mutable row on every read, so ``record.state``
        # itself can flip to ``delivering`` as a side effect of that CAS.
        origin_state = record.state
        state, _runner_online = await self._true_state(receiver)
        if state != "idle":
            return
        moved = await asyncio.to_thread(
            self._store.transition, record.id, "delivering", None, (origin_state,)
        )
        if not moved:
            # Lost the CAS race — another tick (or the inline route) already
            # claimed this record.
            return
        sender = await asyncio.to_thread(
            self._conversation_store.get_conversation, record.sender_session_id
        )
        if sender is None:
            await asyncio.to_thread(
                self._store.transition, record.id, "failed", "closed", ("delivering",)
            )
            return
        # Re-check busy immediately before delivering: the CAS above awaited
        # a store write, during which a turn can start concurrently. A
        # Codex-native receiver is never steered mid-turn (design
        # Decision 5) — abandon this attempt and let a later tick retry.
        recheck_state, _recheck_runner_online = await self._true_state(receiver)
        if recheck_state != "idle":
            await self._revert_delivering(record, origin_state, "busy_recheck")
            return
        request = self._synthetic_request(receiver.id, self._app)
        receiver_owner = effective_owner_id(
            receiver, self._conversation_store, self._permission_store
        )
        try:
            result_state, reason = await self._deliver(
                request,
                sender,
                receiver,
                record.ref,
                record.id,
                record.text,
                acting_user_id=receiver_owner,
            )
        except Exception:
            # ``_deliver`` already maps its own expected failures to a
            # reason; anything reaching here is unexpected.
            _logger.exception("Peer sweeper delivery raised for record %s", record.id)
            result_state, reason = "failed", "not_ready"
        if result_state == "failed":
            # A transient delivery failure is not the sweeper's call to end
            # the record — the sender chose to wait. Revert to the state it
            # came from so the next tick retries; only the closed check
            # above ends a record from here.
            await self._revert_delivering(record, origin_state, reason or "not_ready")
            return
        await asyncio.to_thread(
            self._store.transition, record.id, result_state, reason, ("delivering",)
        )
        await self._notify_for(record, result_state, reason, receiver_title, self._app)

    async def _revert_delivering(
        self, record: SessionPeerMessage, origin_state: str, reason: str
    ) -> None:
        """Abandon a claimed ``delivering`` record without a terminal state.

        Reverts to ``origin_state`` (the state the record came from,
        captured before the CAS to ``delivering``) so the next tick
        retries; posts no notice, since nothing terminal happened yet from
        the sender's point of view.
        """
        await asyncio.to_thread(
            self._store.transition, record.id, origin_state, reason, ("delivering",)
        )
        _logger.warning(
            "Peer sweeper reverted delivering record %s to %s (%s)",
            record.id,
            origin_state,
            reason,
        )

    async def _notify_for(
        self,
        record: SessionPeerMessage,
        state: str,
        reason: str | None,
        receiver_title: str | None,
        app: Any,
    ) -> None:
        sender = await asyncio.to_thread(
            self._conversation_store.get_conversation, record.sender_session_id
        )
        if (
            sender is None
            or is_session_closed(sender.labels, sender.title)
            or sender.archived_at is not None
        ):
            return
        line = format_peer_back_notice(
            peer_id=record.id,
            receiver_session_id=record.receiver_session_id,
            receiver_title=receiver_title,
            state=state,
            reason=reason,
        )
        self._parked.setdefault(sender.id, []).append(line)
        await self._maybe_flush(sender, app)

    async def _maybe_flush(self, sender: Conversation, app: Any) -> None:
        if not self._parked.get(sender.id):
            return
        lock = self._flush_locks.setdefault(sender.id, asyncio.Lock())
        async with lock:
            # Take the parked lines out of the map before any await, so a
            # concurrent flush attempt (the tick's own pass racing the
            # action route's direct ``notify()``) sees an empty slot and
            # bails at the top instead of posting the same lines twice; a
            # notice that parks while we're mid-flush lands in the fresh
            # list this leaves behind, not the one we're about to post.
            lines = self._parked.get(sender.id)
            if not lines:
                return
            self._parked[sender.id] = []
            if is_session_closed(sender.labels, sender.title) or sender.archived_at is not None:
                return
            state, _runner_online = await self._true_state(sender)
            if state != "idle":
                self._parked[sender.id] = lines + self._parked.get(sender.id, [])
                return
            joined = "\n".join(lines)
            request = self._synthetic_request(sender.id, app)
            sender_owner = effective_owner_id(
                sender, self._conversation_store, self._permission_store
            )
            try:
                await self._post_event_impl(
                    request,
                    sender.id,
                    SessionEventInput(
                        type="message",
                        data={"role": "user", "content": [{"type": "input_text", "text": joined}]},
                    ),
                    acting_user_id=sender_owner,
                )
            except Exception:
                _logger.exception(
                    "Peer sweeper failed to post back-notice to sender %s", sender.id
                )
                # Restore (old lines first) for a later attempt rather than
                # silently dropping the notice.
                self._parked[sender.id] = lines + self._parked.get(sender.id, [])

    @staticmethod
    def _synthetic_request(session_id: str, app: Any) -> Request:
        """Build the one synthetic ``Request`` the events path needs.

        No inbound HTTP request drives a sweeper tick or a route-triggered
        notify past its own handler, so this stands in for it — ``app`` is
        the only piece the events path actually reads off the request.
        """
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"/v1/sessions/{session_id}/events",
                "headers": [],
                "query_string": b"",
                "app": app,
                "scheme": "http",
                "root_path": "",
                "client": None,
                "server": None,
            }
        )

    async def _reconcile_startup(self) -> None:
        """Reconcile ``delivering`` records left behind by a crash, at boot."""
        await self._reconcile_stale_delivering(self._clock())

    async def _reconcile_stale_delivering(self, now: int) -> None:
        """Reconcile ``delivering`` records whose last write is past the grace.

        Called at startup and from every tick. A record younger than
        ``_RECONCILE_GRACE_S`` is left alone — native acceptance precedes
        transcript mirroring, so reconciling it this soon would risk a
        duplicate resend of a delivery that actually succeeded; it ages
        into a later tick's pass once the grace elapses.
        """
        try:
            records = await asyncio.to_thread(
                self._store.list_due, ("delivering",), self._batch_limit
            )
        except Exception:
            _logger.exception("Peer sweeper failed to list delivering records")
            return
        for record in records:
            age_from = record.updated_at if record.updated_at is not None else record.created_at
            if now - age_from < _RECONCILE_GRACE_S:
                continue
            try:
                await self._reconcile_one(record, now)
            except Exception:
                _logger.exception("Peer sweeper reconciliation failed for record %s", record.id)

    async def _reconcile_one(self, record: SessionPeerMessage, now: int) -> None:
        matches = await asyncio.to_thread(
            self._conversation_store.search_visible_items_literal,
            record.receiver_session_id,
            f"msg={record.id}",
            1,
        )
        if matches:
            moved = await asyncio.to_thread(
                self._store.transition, record.id, "delivered", None, ("delivering",)
            )
            if moved:
                receiver = await asyncio.to_thread(
                    self._conversation_store.get_conversation, record.receiver_session_id
                )
                await self._notify_for(
                    record,
                    "delivered",
                    None,
                    receiver.title if receiver is not None else None,
                    self._app,
                )
            return
        new_expiry = record.expires_at if record.expires_at > now else now + _RECONCILE_GRACE_S
        await asyncio.to_thread(
            self._store.transition,
            record.id,
            "pending",
            None,
            ("delivering",),
            expires_at=new_expiry,
        )


__all__ = ["PeerSweeper"]
