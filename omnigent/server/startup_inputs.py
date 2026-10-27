"""Accept startup messages into conversation history and recover them on the host's replica."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from starlette.datastructures import Headers

from omnigent.db.db_models import current_workspace_id
from omnigent.entities.conversation import (
    Conversation,
    ConversationItem,
    ErrorData,
    MessageData,
    NewConversationItem,
    SlashCommandData,
    StartupInputOptions,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import pending_inputs
from omnigent.server.annotation_batch import AnnotationFailure
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.schemas import SessionEventInput, SessionStartupInput
from omnigent.startup_protocol import SDK_STARTUP_SOURCES, StartupAttemptRef
from omnigent.stores.conversation_store import (
    PENDING_INPUT_RESPONSE_ID,
    ConversationNotFoundError,
    ConversationStore,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import host_is_live
from omnigent.stores.session_startup_store import StartupSnapshot

_logger = logging.getLogger(__name__)
STARTUP_LABEL = "omnigent.startup"


def initial_input_id(session_id: str) -> str:
    """Stable identity for a create request's first input."""
    return uuid5(NAMESPACE_URL, f"initial-input:{session_id}").hex


def input_content(item: ConversationItem) -> list[dict[str, Any]]:
    """Render a saved message or skill invocation as a pending bubble."""
    data = item.data.model_dump()
    if item.type == "slash_command":
        return [
            {"type": "input_text", "text": f"/{data['name']} {data.get('arguments', '')}".strip()}
        ]
    return data.get("content", [])


async def restore_pending_inputs(
    store: ConversationStore, session_id: str, *, annotation_only: bool = False
) -> None:
    """Restore native receipt matching after an AP restart, without submitting again."""
    if annotation_only and not isinstance(store, SqlAlchemyConversationStore):
        return
    for item in await asyncio.to_thread(store.list_pending_inputs, session_id):
        if annotation_only and (
            not isinstance(store, SqlAlchemyConversationStore)
            or await asyncio.to_thread(store.annotation_receipt_for_item, session_id, item.id)
            is None
        ):
            continue
        if (
            item.status == "incomplete"
            and pending_inputs.pending_id_for_stable_id(session_id, item.id) is None
        ):
            options = (
                item.data.startup_options
                if isinstance(item.data, (MessageData, SlashCommandData))
                else None
            )
            pending_id = pending_inputs.record(
                session_id,
                input_content(item),
                stable_id=item.id,
                created_by=item.created_by,
                background_titles_enabled=bool(options and options.background_titles_enabled),
            )
            conv = await asyncio.to_thread(store.get_conversation, session_id)
            if conv is not None and conv.labels.get(STARTUP_LABEL) == "cancelled":
                pending_inputs.mark_interrupted(session_id, [pending_id])


async def reconcile_accepted_startup_input(
    conversations: ConversationStore, startup: StartupSnapshot
) -> None:
    """Finish an interrupted handoff from durable acceptance without dispatching again."""
    if not (
        startup.input_accepted
        and startup.proof_source in SDK_STARTUP_SOURCES
        and startup.input_id
        and startup.attempt_id
    ):
        return
    if isinstance(conversations, SqlAlchemyConversationStore):
        annotation = await asyncio.to_thread(
            conversations.annotation_receipt_for_item, startup.session_id, startup.input_id
        )
        if annotation is not None:
            # Annotation confirmation requires its native transcript receipt.
            return
    pending = await asyncio.to_thread(conversations.get_item, startup.session_id, startup.input_id)
    if pending is None or pending.response_id != PENDING_INPUT_RESPONSE_ID:
        return
    [consumed] = await asyncio.to_thread(
        conversations.append,
        startup.session_id,
        [
            NewConversationItem(
                type=pending.type,
                response_id=f"turn_{startup.attempt_id}",
                status="completed",
                stable_id=pending.id,
                replaces_pending_id=pending.id,
                data=pending.data,
                created_by=pending.created_by,
            )
        ],
    )
    from omnigent.server.routes._sessions.orchestration import _publish_input_consumed

    _publish_input_consumed(startup.session_id, consumed)


@dataclass
class StartupInputContext:
    """The accepted sender's identity, independent of the browser's lifetime."""

    app: Any
    headers: Headers
    user_id: str | None
    input_id: str
    submitted: bool = False
    forward_started: bool = False
    forward_rejected: bool = False
    startup: StartupAttemptRef | None = None

    async def is_disconnected(self) -> bool:
        return False

    async def claim(self, store: ConversationStore, session_id: str) -> bool:
        # Leave the row queued throughout launch, authorization and policy checks.
        # A submitted native input is never replayed without its transcript receipt.
        if self.startup is not None:
            from omnigent.db.account_authority import AccountAuthority
            from omnigent.db.db_models import current_workspace_id

            admission = self.app.state.startup_store
            startup = await asyncio.to_thread(admission.get, self.startup.startup_id)
            service = getattr(self.app.state, "startup_admission", None)
            conn = service.hosts.get(startup.host_id) if service and startup else None
            if (
                conn is None
                or conn.startup_generation != self.startup.host_generation
                or startup is None
                or startup.input_id != self.input_id
                or startup.attempt_id != self.startup.attempt_id
            ):
                return False
            account = (
                AccountAuthority(conn.owner, conn.account_generation, current_workspace_id())
                if conn.owner is not None
                else None
            )
            if not await asyncio.to_thread(
                admission.claim_boundary,
                self.startup.startup_id,
                self.startup.attempt_id,
                runtime_id=self.startup.runtime_id,
                host_generation=self.startup.host_generation,
                boundary="input_dispatch",
                claim_id=self.input_id,
                account=account,
            ):
                return False
        self.submitted = await asyncio.to_thread(
            store.transition_pending_input,
            session_id,
            self.input_id,
            expected="in_progress",
            status="incomplete",
        )
        return self.submitted


StartupDispatch = Callable[
    [StartupInputContext, str, SessionEventInput],
    Coroutine[Any, Any, dict[str, bool | str | None]],
]


_services: weakref.WeakSet[ManagedStartupInputs] = weakref.WeakSet()


def wake_startup_worker(host_id: str) -> None:
    """Prompt a running host worker to look for the next saved input now."""
    key = (current_workspace_id(), host_id)
    for service in _services:
        entry = service._workers.get(key)
        if entry is not None:
            entry[2].set()


class ManagedStartupInputs:
    """Recover pending conversation items on the replica connected to their host."""

    def __init__(self, conversations: ConversationStore, hosts: HostRegistry) -> None:
        self.conversations = conversations
        self.hosts = hosts
        _services.add(self)
        self.dispatch: StartupDispatch | None = None
        self.on_saved: Callable[[str, str], Awaitable[None]] | None = None
        self.cancel_attempt: Callable[[str], Awaitable[bool]] | None = None
        self._workers: dict[
            tuple[int, str], tuple[HostConnection, asyncio.Task[None], asyncio.Event]
        ] = {}
        self._deliveries: dict[tuple[int, str], asyncio.Task[Any]] = {}
        self._session_barriers: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def session_barrier(self, session_id: str) -> asyncio.Lock:
        """Fence normal sends behind release's final drain on this server replica."""
        key = (current_workspace_id(), session_id)
        lock = self._session_barriers.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_barriers[key] = lock
        return lock

    async def dispatch_annotation(
        self,
        app: Any,
        session_id: str,
        item_id: str,
        user_id: str,
        grant_id: str,
        generation: str,
        batch_id: str,
        *,
        target_live: Callable[[str], bool],
        explicit_retry: bool = False,
    ) -> None:
        """Enter the existing saved-input dispatch after the durable pre-forward CAS."""
        if not isinstance(self.conversations, SqlAlchemyConversationStore):
            return
        live = await asyncio.to_thread(target_live, session_id)
        try:
            started = await asyncio.to_thread(
                self.conversations.begin_annotation_dispatch,
                user_id,
                grant_id,
                generation,
                batch_id,
                target_live=live,
                explicit_retry=explicit_retry,
            )
        except AnnotationFailure:
            # Admission already committed. A later revoke/archive cannot erase it.
            return
        if started is None:
            return
        if explicit_retry:
            # A native runner may have marked the prior queue entry rejected
            # after accepting the POST. Re-record it with fresh delivery state.
            pending_inputs.resolve(session_id, pending_inputs.make_pending_id(session_id, item_id))
        item = await asyncio.to_thread(self.conversations.get_item, session_id, item_id)
        if item is not None:
            outcome = await self._deliver(app, session_id, item, annotation=True)
            if outcome == "not_dispatched":
                await asyncio.to_thread(
                    self.conversations.defer_unsubmitted_annotation, session_id, item_id
                )
        else:
            await asyncio.to_thread(
                self.conversations.defer_unsubmitted_annotation, session_id, item_id
            )

    async def enqueue(
        self,
        session_id: str,
        event: SessionEventInput,
        *,
        user_id: str | None,
        headers: Headers,
        first: bool = True,
        while_label: str | None = None,
    ) -> ConversationItem:
        from omnigent.server.background_session_titles import background_session_titles_enabled
        from omnigent.server.routes._sessions.helpers import (
            _build_new_item,
            _stable_id_reuse_is_exact_retry,
        )

        try:
            SessionStartupInput.model_validate(event.model_dump())
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        raw_id = event.data.get("stable_id")
        input_id = (
            initial_input_id(session_id)
            if first
            else (
                raw_id
                if isinstance(raw_id, str) and re.fullmatch(r"[0-9a-f]{32}", raw_id)
                else uuid4().hex
            )
        )
        if event.type == "slash_command":
            event = event.model_copy(
                update={"data": {"agent": "pending", "arguments": "", **event.data}}
            )
        item = _build_new_item(event, PENDING_INPUT_RESPONSE_ID, created_by=user_id).model_copy(
            update={"stable_id": input_id, "status": "in_progress"}
        )
        item.data = item.data.model_copy(
            update={
                "startup_options": StartupInputOptions(
                    model_override=event.model_override,
                    tools=event.tools,
                    background_titles_enabled=background_session_titles_enabled(headers),
                )
            }
        )
        if while_label is not None:
            saved = await asyncio.to_thread(
                self.conversations.append, session_id, [item], while_label=while_label
            )
        else:
            saved = await asyncio.to_thread(self.conversations.append, session_id, [item])
        if saved[0].deduplicated and not _stable_id_reuse_is_exact_retry(saved[0], item):
            raise OmnigentError(
                "This input id already belongs to a different message", code=ErrorCode.CONFLICT
            )
        if first:
            await asyncio.to_thread(
                self.conversations.set_labels, session_id, {STARTUP_LABEL: "pending"}
            )
        else:
            conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
            if conv is not None and STARTUP_LABEL not in conv.labels:
                # The startup finished as this was saved; keep later messages behind it.
                await asyncio.to_thread(
                    self.conversations.set_labels, session_id, {STARTUP_LABEL: "ready"}
                )
        if self.on_saved is not None:
            await self.on_saved(session_id, saved[0].id)
        return saved[0]

    async def record_launch_failure(self, session_id: str, reason: str) -> None:
        """
        Mark a failed create launch on the session, for replicas without its tracker.

        No sandbox is left to deliver the saved inputs, so they are failed and
        the transcript says why.
        """
        if await asyncio.to_thread(self.conversations.get_conversation, session_id) is None:
            return
        with contextlib.suppress(ConversationNotFoundError):
            await asyncio.to_thread(
                self.conversations.set_labels, session_id, {STARTUP_LABEL: "failed"}
            )
        undelivered = False
        annotation_store = (
            self.conversations
            if isinstance(self.conversations, SqlAlchemyConversationStore)
            else None
        )
        for item in await asyncio.to_thread(self.conversations.list_pending_inputs, session_id):
            if (
                annotation_store is not None
                and await asyncio.to_thread(
                    annotation_store.annotation_receipt_for_item, session_id, item.id
                )
                is not None
            ):
                await asyncio.to_thread(
                    annotation_store.defer_annotation_input, session_id, item.id
                )
                continue
            if item.status == "in_progress":
                undelivered |= await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected="in_progress",
                    status="failed",
                )
        if undelivered:
            await self._report_failure(
                session_id,
                "launch_failed",
                f"The message could not be delivered: {reason[:1000]}",
            )

    async def accepts(self, conv: Conversation, app: Any) -> bool:
        """Whether a new message must wait behind the session's startup."""
        admission = getattr(app.state, "startup_admission", None)
        if admission is not None:
            current = await asyncio.to_thread(admission.startups.for_session, conv.id)
            if current is not None and current.state in ("cancelled", "stopped", "cancelling"):
                raise OmnigentError(
                    "Startup is stopped or still stopping; "
                    "resume the session before sending input",
                    code=ErrorCode.CONFLICT,
                )
            if (conv.host_id is not None or current is not None) and (
                current is None or current.state not in ("working", "opened", "command_completed")
            ):
                return True
            if current is not None and current.state == "opened" and current.input_id is not None:
                pending = await asyncio.to_thread(self.conversations.list_pending_inputs, conv.id)
                if any(item.id == current.input_id for item in pending):
                    return True
        state = conv.labels.get(STARTUP_LABEL)
        if state not in ("pending", "ready"):
            return False
        # A failed launch can't deliver anything; the message path reports why.
        # The tracker knows on the launching replica, the label everywhere.
        tracker = getattr(app.state, "managed_launches", None)
        launch = tracker.get(conv.id) if tracker is not None else None
        if launch is not None and launch.error is not None:
            return False
        if any(
            item.status in ("in_progress", "incomplete")
            for item in await asyncio.to_thread(self.conversations.list_pending_inputs, conv.id)
        ):
            return True
        if state != "pending":
            return False
        # Nothing is saved yet: hold the message only while the launch can
        # still deliver it. A bound host that is gone needs the message path's
        # relaunch instead.
        if launch is not None or conv.host_id is None:
            return True
        host_store = getattr(app.state, "host_store", None)
        host = (
            await asyncio.to_thread(host_store.get_host, conv.host_id)
            if host_store is not None
            else None
        )
        return host is not None and host_is_live(host)

    async def pending_snapshot(self, session_id: str) -> list[dict[str, Any]]:
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.labels.get(STARTUP_LABEL) == "cancelled":
            return []
        return [
            {
                "pending_id": pending_inputs.make_pending_id(session_id, item.id),
                "content": input_content(item),
                **({"created_by": item.created_by} if item.created_by else {}),
            }
            for item in await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
            if item.status != "failed"
        ]

    def host_changed(self, app: Any, host_id: str) -> None:
        key = (current_workspace_id(), host_id)
        connection = self.hosts.get(host_id)
        existing = self._workers.get(key)
        if existing is not None:
            previous, worker, wake = existing
            if connection is previous and not worker.done():
                wake.set()
                return
            worker.cancel()
        if connection is None or (
            not connection.registered_with_managed_token
            and getattr(app.state, "startup_admission", None) is None
        ):
            return
        wake = asyncio.Event()

        async def serve() -> None:
            after = None
            delay = 0.25
            while self.hosts.get(host_id) is connection:
                busy = False
                try:
                    admission = getattr(app.state, "startup_admission", None)
                    if admission is not None:
                        await admission.advance(host_id)
                    ids = await asyncio.to_thread(
                        self.conversations.list_host_conversation_ids, host_id, after=after
                    )
                    for session_id in ids:
                        if self.hosts.get(host_id) is not connection:
                            return
                        busy |= await self._recover_session(app, session_id, connection)
                    after = ids[-1] if len(ids) == 32 else None
                    idle_limit = 10.0 if admission is not None else 30.0
                    delay = 0.25 if busy or after else min(delay * 2, idle_limit)
                except Exception:
                    _logger.exception("Unable to recover startup messages on host %s", host_id)
                    after = None
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=delay)
                wake.clear()

        task = asyncio.create_task(serve(), name=f"startup-inputs-{host_id}")
        self._workers[key] = (connection, task, wake)

        def remove_worker(done: asyncio.Task[None]) -> None:
            if self._workers.get(key, (None, None, None))[1] is done:
                self._workers.pop(key, None)

        task.add_done_callback(remove_worker)

    async def _recover_session(
        self, app: Any, session_id: str, connection: HostConnection
    ) -> bool:
        """Advance one session's startup here; return whether work is in flight."""
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.host_id != connection.host_id:
            return False
        from omnigent.server.host_load_queue import held_host

        if held_host(conv):
            return False
        from omnigent.server.startup_registration import (
            STARTUP_CREATE_PREPARATION_LABEL_KEY,
            STARTUP_CREATE_PREPARATION_PENDING,
        )

        if (
            conv.labels.get(STARTUP_CREATE_PREPARATION_LABEL_KEY)
            == STARTUP_CREATE_PREPARATION_PENDING
        ):
            return False
        admission = getattr(app.state, "startup_admission", None)
        if admission is not None:
            startup = await asyncio.to_thread(admission.startups.for_session, session_id)
            if startup is not None:
                await reconcile_accepted_startup_input(self.conversations, startup)
        state = conv.labels.get(STARTUP_LABEL)
        if state == "failed":
            return False
        items = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        if state == "cancelled":
            if not items:
                await self._finish(session_id)
            return False
        # A refused input waits for an explicit retry without holding back the
        # ones behind it. One submitted to a native harness and awaiting its
        # transcript receipt still does: it may already have run.
        head = next((item for item in items if item.status != "failed"), None)
        annotation_store = (
            self.conversations
            if isinstance(self.conversations, SqlAlchemyConversationStore)
            else None
        )
        if head is not None and annotation_store is not None:
            receipt = await asyncio.to_thread(
                annotation_store.annotation_receipt_for_item, session_id, head.id
            )
            if receipt is not None:
                key = (current_workspace_id(), session_id)
                if receipt["dispatch_phase"] == "dispatching" and key not in self._deliveries:
                    await asyncio.to_thread(
                        annotation_store.mark_annotation_uncertain, session_id, head.id
                    )
                elif (
                    receipt["dispatch_phase"] == "ready"
                    and not receipt["retry_required"]
                    and isinstance(receipt["generation"], str)
                    and head.status == "in_progress"
                ):
                    task = asyncio.create_task(
                        self.dispatch_annotation(
                            app,
                            session_id,
                            head.id,
                            str(receipt["user_id"]),
                            str(receipt["grant_id"]),
                            receipt["generation"],
                            str(receipt["batch_id"]),
                            target_live=app.state.annotation_target_live,
                        ),
                        name=f"annotation-input-{session_id}",
                    )
                    self._deliveries[key] = task

                    def clear_annotation_delivery(done: asyncio.Task[None]) -> None:
                        if self._deliveries.get(key) is done:
                            self._deliveries.pop(key, None)

                    task.add_done_callback(clear_annotation_delivery)
                return False
        admission = getattr(app.state, "startup_admission", None)
        if admission is not None:
            current = await asyncio.to_thread(admission.startups.for_session, session_id)
            if (
                (
                    current is not None
                    and current.state
                    not in ("working", "opened", "command_completed", "ready_empty")
                )
                or (head is not None and (current is None or current.state == "ready_empty"))
                or (state == "pending" and conv.runner_id is None)
            ):
                from omnigent.server.startup_admission import StartupQueued

                try:
                    return await admission.recover_session(session_id, connection)
                except StartupQueued:
                    return False
            if (
                current is not None
                and current.state == "opened"
                and head is not None
                and head.id == current.input_id
            ):
                return False
        if head is not None:
            # Delivered even without the label: a message saved while the label
            # was being cleared still belongs to this session's startup.
            key = (current_workspace_id(), session_id)
            if head.status == "in_progress" and key not in self._deliveries:
                task = asyncio.create_task(
                    self._deliver(app, session_id, head), name=f"startup-input-{session_id}"
                )
                self._deliveries[key] = task
            return head.status == "in_progress"
        if state == "pending" and conv.runner_id is None:
            # The launch bound this host on a replica without its tunnel, so it
            # could not start the runner; start it here.
            from omnigent.server.routes.sessions import _launch_runner_on_host

            attempt = await _launch_runner_on_host(
                conv, self.conversations, self.hosts, connection
            )
            if attempt.error or attempt.error_code:
                reason = attempt.error or attempt.error_code or "the host refused the launch"
                await self._report_failure(
                    session_id,
                    f"launch_{attempt.runner_id}",
                    f"The agent could not start on the sandbox: {reason[:1000]}",
                )
            return True
        if state is not None and not items:
            # A runner is bound and nothing is left to deliver.
            await self._finish(session_id)
        return False

    async def _finish(self, session_id: str) -> None:
        """Clear a drained session's startup state so later reads skip it."""
        await asyncio.to_thread(self.conversations.delete_label, session_id, STARTUP_LABEL)
        # An input saved while the label was being cleared must still be delivered.
        if await asyncio.to_thread(self.conversations.list_pending_inputs, session_id):
            await asyncio.to_thread(
                self.conversations.set_labels, session_id, {STARTUP_LABEL: "ready"}
            )

    async def _deliver(
        self,
        app: Any,
        session_id: str,
        item: ConversationItem,
        *,
        startup: StartupAttemptRef | None = None,
        annotation: bool = False,
    ) -> Literal["submitted", "not_dispatched", "unconfirmed"]:
        if self.dispatch is None:
            return "not_dispatched"
        key = (current_workspace_id(), session_id)
        task = asyncio.current_task()
        assert task is not None
        if key in self._deliveries and self._deliveries[key] is not task:
            return "not_dispatched" if annotation else "unconfirmed"
        self._deliveries[key] = task
        context: StartupInputContext | None = None
        try:
            conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
            current = await asyncio.to_thread(self.conversations.get_item, session_id, item.id)
            from omnigent.server.host_load_queue import start_allowed

            if (
                conv is None
                or not start_allowed(conv)
                or conv.labels.get(STARTUP_LABEL) == "cancelled"
                or current is None
                or current.response_id != PENDING_INPUT_RESPONSE_ID
                or current.status != "in_progress"
            ):
                return "not_dispatched"
            options = (
                item.data.startup_options
                if isinstance(item.data, (MessageData, SlashCommandData))
                else None
            )
            context = StartupInputContext(
                app,
                Headers(
                    {
                        "x-omnigent-background-session-titles": "on"
                        if options and options.background_titles_enabled
                        else "off",
                    }
                ),
                item.created_by,
                item.id,
                startup=startup,
            )
            event = SessionEventInput(
                type=item.type,
                data={**item.data.model_dump(exclude={"startup_options"}), "stable_id": item.id},
                model_override=options.model_override if options else None,
                tools=options.tools if options else None,
            )
            response = await self.dispatch(context, session_id, event)
            if response.get("denied"):
                if annotation:
                    return "not_dispatched"
                raise ValueError(str(response.get("reason", "Input denied by policy")))
            if context.submitted:
                if not response.get("pending_id") and not response.get("item_id"):
                    # Native overlays such as /btw have no transcript echo.
                    from omnigent.server.routes.sessions import (
                        _build_new_item,
                        _publish_input_consumed,
                    )

                    receipt = _build_new_item(
                        event, "startup_command", created_by=item.created_by
                    ).model_copy(update={"stable_id": item.id, "replaces_pending_id": item.id})
                    [saved] = await asyncio.to_thread(
                        self.conversations.append, session_id, [receipt]
                    )
                    _publish_input_consumed(
                        session_id,
                        saved,
                        cleared_pending_id=pending_inputs.make_pending_id(session_id, item.id),
                    )
                await asyncio.to_thread(
                    self.conversations.set_labels, session_id, {STARTUP_LABEL: "ready"}
                )
                current_conv = await asyncio.to_thread(
                    self.conversations.get_conversation, session_id
                )
                initial = (
                    current_conv.labels.get("omnigent.initial_message") if current_conv else None
                )
                if initial:
                    with contextlib.suppress(ValueError):
                        outcome = json.loads(initial)
                        if (
                            isinstance(outcome, dict)
                            and outcome.get("status") == "queued"
                            and outcome.get("reason") == "host_load"
                        ):
                            await asyncio.to_thread(
                                self.conversations.compare_and_set_label,
                                session_id,
                                "omnigent.initial_message",
                                initial,
                                json.dumps({"status": "sent"}),
                            )
            return "submitted" if context.submitted else "not_dispatched"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.exception("Unable to deliver saved message for session %s", session_id)
            if annotation:
                if context is None or not context.forward_started or context.forward_rejected:
                    return "not_dispatched"
                if isinstance(self.conversations, SqlAlchemyConversationStore):
                    await asyncio.to_thread(
                        self.conversations.mark_annotation_uncertain, session_id, item.id
                    )
                return "unconfirmed"
            if context is None or not context.submitted:
                await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected="in_progress",
                    status="failed",
                )
            uncertain = bool(context and context.submitted)
            await self._report_failure(
                session_id,
                item.id,
                (
                    "Delivery could not be confirmed, so later messages wait behind it. "
                    "Check the terminal; if the agent never got it, press Stop, which "
                    "also clears the waiting messages, then send them again. "
                    if uncertain
                    else "The message is saved but could not be delivered. "
                )
                + str(exc)[:1000],
            )
            return "unconfirmed" if uncertain else "not_dispatched"
        finally:
            self._deliveries.pop(key, None)

    async def deliver_session(self, app: Any, session_id: str) -> int:
        """Drain saved inputs in order, including hostless children on a parent runner."""
        delivered = 0
        while True:
            items = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
            head = next((item for item in items if item.status != "failed"), None)
            if head is None:
                return delivered
            if head.status != "in_progress":
                raise TimeoutError("Saved input is still waiting for its runtime receipt")
            outcome = await self._deliver(app, session_id, head)
            if outcome != "submitted":
                raise RuntimeError(f"Saved input delivery was {outcome}")
            delivered += 1
            current = await asyncio.to_thread(self.conversations.get_item, session_id, head.id)
            if current is not None and current.status == "incomplete":
                raise TimeoutError("Saved input is still waiting for its runtime receipt")

    async def retry(self, session_id: str) -> bool:
        changed = False
        for item in await asyncio.to_thread(self.conversations.list_pending_inputs, session_id):
            if item.status == "failed":
                changed |= await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected="failed",
                    status="in_progress",
                )
        return changed

    async def cancel(self, session_id: str) -> bool:
        """Cancel saved inputs; return whether startup was cancelled before dispatch."""
        fenced_before_dispatch = (
            await self.cancel_attempt(session_id) if self.cancel_attempt is not None else False
        )
        items = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if not items and (conv is None or conv.labels.get(STARTUP_LABEL) != "pending"):
            return fenced_before_dispatch
        cancelled_before_dispatch = (
            conv is not None and conv.labels.get(STARTUP_LABEL) == "pending"
        )
        await asyncio.to_thread(
            self.conversations.set_labels, session_id, {STARTUP_LABEL: "cancelled"}
        )
        key = (current_workspace_id(), session_id)
        task = self._deliveries.get(key)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        annotation_store = (
            self.conversations
            if isinstance(self.conversations, SqlAlchemyConversationStore)
            else None
        )
        for item in items:
            if (
                annotation_store is not None
                and await asyncio.to_thread(
                    annotation_store.annotation_receipt_for_item, session_id, item.id
                )
                is not None
            ):
                await asyncio.to_thread(
                    annotation_store.defer_annotation_input, session_id, item.id
                )
                cancelled_before_dispatch = False
                continue
            if item.status == "incomplete":
                cancelled_before_dispatch = False
                pending_inputs.mark_interrupted(
                    session_id, [pending_inputs.make_pending_id(session_id, item.id)]
                )
            else:
                removed = await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected=item.status,
                    status=None,
                )
                cancelled_before_dispatch &= removed
        return cancelled_before_dispatch

    async def _report_failure(self, session_id: str, failure_id: str, message: str) -> None:
        """Record a startup failure in the transcript and the session's status."""
        from omnigent.server.routes.sessions import _publish_sandbox_status, _publish_status

        if await asyncio.to_thread(self.conversations.get_conversation, session_id) is None:
            return

        await asyncio.to_thread(
            self.conversations.append,
            session_id,
            [
                NewConversationItem(
                    type="error",
                    response_id=f"startup_{failure_id}",
                    stable_id=uuid5(
                        NAMESPACE_URL, f"startup-failure:{session_id}:{failure_id}"
                    ).hex,
                    data=ErrorData(
                        source="execution", code="startup_input_failed", message=message
                    ),
                )
            ],
        )
        _publish_sandbox_status(session_id, "failed", message)
        _publish_status(session_id, "failed", failure_origin="startup_input_failed")

    async def shutdown(self) -> None:
        tasks = [task for _, task, _ in self._workers.values()] + list(self._deliveries.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
