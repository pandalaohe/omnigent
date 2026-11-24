"""Recovered streams get a fresh grace period for their next disconnect."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from omnigent._wrapper_labels import CLAUDE_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runner.transports.ws_tunnel.registry import RunnerSession, TunnelRegistry
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport
from omnigent.runtime import pending_inputs, session_stream
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.sessions import routes_hooks
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store import DetachedCard
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import start_session_stream_collector


class _RelayClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay
        await asyncio.sleep(0)


class _InterruptedStream(httpx.AsyncByteStream):
    def __init__(self, *, ready: bool, pause: asyncio.Event | None = None) -> None:
        self.ready = ready
        self.pause = pause

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.ready:
            yield b'data: {"type":"session.heartbeat"}\n\n'
        else:
            yield b": transport connected, no runner heartbeat\n\n"
        if self.pause is not None:
            await self.pause.wait()
        raise httpx.ReadError("test stream interrupted")


class _NaturalEofStream(httpx.AsyncByteStream):
    """Stream that ends without a terminal SSE sentinel."""

    def __init__(self, *frames: bytes) -> None:
        self.frames = frames

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for frame in self.frames:
            yield frame


@pytest.mark.asyncio
@pytest.mark.parametrize("expires", [False, True])
async def test_runner_pending_input_reconciles_or_expires(db_uri, monkeypatch, expires):
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation().id
    now = 0.0
    monkeypatch.setattr(pending_inputs, "_now", lambda: now)
    pending_id = pending_inputs.record(
        session_id, [{"type": "input_text", "text": "queued"}], runner_managed=True
    )
    pending_inputs.mark_delivery_stage(session_id, pending_id, "forward_accepted")
    for now in range(60, 661, 60):
        await orchestration._reconcile_runner_input_leases(
            store,
            session_id,
            {"active_pending_ids": [], "queued_pending_ids": [pending_id]},
            set(),
        )
    assert pending_inputs.snapshot_for(session_id)[0]["pending_id"] == pending_id
    assert "sending_since_ms" not in pending_inputs.snapshot_for(session_id)[0]
    assert pending_inputs.runner_lease_remaining(session_id, pending_id) == 600
    await orchestration._reconcile_runner_input_leases(
        store, session_id, {"active_pending_ids": [pending_id], "queued_pending_ids": []}, set()
    )
    assert "sending_since_ms" in pending_inputs.snapshot_for(session_id)[0]
    if expires:
        now += 601
        assert not pending_inputs.claim_runner_input(session_id, pending_id, "lapsed_before_watch")
        await orchestration._watch_runner_input_lease(store, session_id, pending_id)
    else:
        await orchestration._reconcile_runner_input_leases(
            store,
            session_id,
            {"active_pending_ids": [], "queued_pending_ids": []},
            pending_inputs.runner_accepted_ids(session_id),
        )
    assert pending_inputs.snapshot_for(session_id) == []
    message, error = store.list_items(session_id).data
    assert message.type == "message"
    assert error.type == "error"
    assert "not delivered" in error.data.model_dump()["message"]
    assert not pending_inputs.claim_runner_input(session_id, pending_id, "late_claim")


@pytest.mark.asyncio
async def test_claimed_input_survives_reconnect_and_expires_as_uncertain(db_uri, monkeypatch):
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation().id
    now = 0.0
    monkeypatch.setattr(pending_inputs, "_now", lambda: now)
    pending_id = pending_inputs.record(session_id, [{"type": "input_text", "text": "hello"}])
    pending_inputs.mark_delivery_stage(session_id, pending_id, "forward_accepted")
    pending_inputs.enable_runner_lease(session_id, pending_id)
    assert pending_inputs.claim_runner_input(session_id, pending_id, "first")
    assert pending_inputs.claim_runner_input(session_id, pending_id, "first")
    assert not pending_inputs.claim_runner_input(session_id, pending_id, "second")

    now = 601.0
    await orchestration._reconcile_runner_input_leases(
        store, session_id, {"active_pending_ids": [], "queued_pending_ids": []}, {pending_id}
    )
    assert not store.list_items(session_id).data
    assert pending_inputs.runner_lease_remaining(session_id, pending_id) == 6599

    now = 7201.0
    assert not pending_inputs.claim_runner_input(session_id, pending_id, "first")
    await orchestration._watch_runner_input_lease(store, session_id, pending_id)

    assert not pending_inputs.snapshot_for(session_id)
    message, error = store.list_items(session_id).data
    assert message.type == "message"
    assert error.data.model_dump()["code"] == "native_input_delivery_uncertain"
    assert "may have arrived" in error.data.model_dump()["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["prompt", "late", "blind", "missing"])
async def test_claimed_mid_turn_paste_waits_for_its_late_mirror(db_uri, monkeypatch, receipt):
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation(
        labels={WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE}
    ).id
    now = 0.0
    monkeypatch.setattr(pending_inputs, "_now", lambda: now)
    pending_id, mirror_text = _record_tagged_paste(session_id)
    attempt_id = pending_inputs.delivery_attributes_for(session_id, pending_id)[
        "delivery_attempt_id"
    ]
    assert pending_inputs.claim_runner_input(session_id, pending_id, "paste")
    collector = await start_session_stream_collector(session_id)
    try:
        if receipt == "prompt":
            _confirm_paste(session_id, pending_id)
        elif receipt == "blind":
            orchestration._record_native_input_lifecycle(
                session_id,
                {
                    "type": "response.completed",
                    "response": {"id": "resp_blind"},
                    "pending_id": pending_id,
                    "delivery_attempt_id": attempt_id,
                },
            )
        # A lost lease heartbeat or a restart could leave the claim without
        # its submit report. Waiting for the late mirror takes no runner slot.
        now = 54 * 60
        await orchestration._reconcile_runner_input_leases(
            store, session_id, {"active_pending_ids": [], "queued_pending_ids": []}, {pending_id}
        )
        assert pending_inputs.runner_lease_remaining(session_id, pending_id) == 3960
        assert not store.list_items(session_id).data
        assert orchestration._list_status_with_starting("idle", session_id) == "idle"
        assert not pending_inputs.claim_runner_input(session_id, pending_id, "other-attempt")
        if receipt == "late":
            _confirm_paste(session_id, pending_id)
        evidence = receipt in {"prompt", "late"}
        if evidence:
            received = await collector.next_event()
            assert received["type"] == "session.input.sending"
            assert received["pending_id"] == pending_id
            assert received["received"] is True
            assert received["since_ms"] is not None
        else:
            await collector.assert_no_event(within=0.05)
        # A reconnect snapshot is still pending until the real transcript arrives.
        [entry] = pending_inputs.snapshot_for(session_id)
        assert entry.get("received", False) is evidence
        assert entry["sending_since_ms"] is not None
        conversation = store.get_conversation(session_id)
        assert conversation is not None
        await orchestration._persist_external_conversation_item(
            session_id,
            conversation,
            SessionEventInput.model_validate(
                {
                    "type": "external_conversation_item",
                    "data": {
                        "source_id": "claude-queued-steer",
                        "item_type": "message",
                        "item_data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": mirror_text}],
                        },
                        "response_id": "resp_turn",
                    },
                }
            ),
            store,
            created_by=None,
        )
        # The late mirror commits the send once and clears its bubble by id.
        consumed = await collector.next_event()
        while consumed["type"] != "session.input.consumed":
            consumed = await collector.next_event()
        assert consumed["data"]["cleared_pending_id"] == pending_id
        assert not pending_inputs.has_pending(session_id)
        [user] = store.list_items(session_id, type="message").data
        assert user.id == consumed["data"]["item_id"]
        while True:
            try:
                extra = await collector.next_event(timeout=0.05)
            except TimeoutError:
                break
            assert extra["type"] != "session.input.consumed"
    finally:
        await collector.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_state", ["lapsed", "interrupted"])
async def test_a_completion_after_the_claim_ends_marks_nothing_received(
    db_uri, monkeypatch, claim_state
):
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation().id
    now = 0.0
    monkeypatch.setattr(pending_inputs, "_now", lambda: now)
    pending_id = pending_inputs.record(
        session_id, [{"type": "input_text", "text": "steer"}], runner_managed=True
    )
    attempt_id = pending_inputs.delivery_attributes_for(session_id, pending_id)[
        "delivery_attempt_id"
    ]
    assert pending_inputs.claim_runner_input(session_id, pending_id, "paste")
    if claim_state == "lapsed":
        # The lease lapsed; its cleanup has not settled the entry yet.
        now = 7201.0
        assert not pending_inputs.claim_runner_input(session_id, pending_id, "paste")
    else:
        pending_inputs.mark_interrupted(session_id, [pending_id])
    collector = await start_session_stream_collector(session_id)
    try:
        orchestration._record_native_input_lifecycle(
            session_id,
            {
                "type": "response.completed",
                "response": {"id": "resp_paste"},
                "pending_id": pending_id,
                "delivery_attempt_id": attempt_id,
                "input_received": True,
            },
        )
        await collector.assert_no_event(within=0.05)
    finally:
        await collector.stop()
    assert pending_inputs.agent_received(session_id, pending_id) is None
    assert all("received" not in entry for entry in pending_inputs.snapshot_for(session_id))
    pending_inputs.resolve(session_id, pending_id)


def _record_tagged_paste(session_id: str, *, stable_id: str | None = None) -> tuple[str, str]:
    pending_id = pending_inputs.record(
        session_id,
        [
            {"type": "input_text", "text": "steer"},
            {"type": "input_file", "file_id": "file_status", "filename": "status.txt"},
        ],
        stable_id=stable_id,
        runner_managed=True,
    )
    attempt_id = pending_inputs.delivery_attributes_for(session_id, pending_id)[
        "delivery_attempt_id"
    ]
    path = f"/opt/work/attachments/{'b' * 32}/omnigent-upload-{attempt_id}-0-status.txt"
    return pending_id, f"steer\n[Attached: {path}]"


def _confirm_paste(session_id: str, pending_id: str) -> None:
    attempt_id = pending_inputs.delivery_attributes_for(session_id, pending_id)[
        "delivery_attempt_id"
    ]
    orchestration._record_native_input_lifecycle(
        session_id,
        {
            "type": "response.completed",
            "response": {"id": "resp_paste"},
            "pending_id": pending_id,
            "delivery_attempt_id": attempt_id,
            "input_received": True,
        },
    )


@pytest.mark.asyncio
async def test_stop_hides_claimed_input_and_keeps_its_proof_deadline(db_uri, monkeypatch):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(labels={WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE})
    session_id = conv.id
    stable_id = "d" * 32
    now = 0.0
    monkeypatch.setattr(pending_inputs, "_now", lambda: now)
    published: list[dict] = []
    monkeypatch.setattr(
        session_stream, "publish", lambda _conversation_id, event: published.append(event)
    )
    waiting, mirror_text = _record_tagged_paste(session_id, stable_id=stable_id)
    pending_inputs.mark_delivery_stage(session_id, waiting, "forward_accepted")
    pending_inputs.enable_runner_lease(session_id, waiting)
    stopped: str | None = None
    try:
        assert pending_inputs.claim_runner_input(session_id, waiting, "paste")
        _confirm_paste(session_id, waiting)
        orchestration._session_status_cache[session_id] = "waiting"
        for now in range(60, 1501, 60):
            # A reconnect at 780 s does not settle a claimed paste as lost.
            previous = pending_inputs.runner_accepted_ids(session_id) if now == 780 else set()
            await orchestration._reconcile_runner_input_leases(
                store,
                session_id,
                {"active_pending_ids": [], "queued_pending_ids": []},
                previous,
            )
        assert pending_inputs.runner_lease_remaining(session_id, waiting) == 5700
        assert not pending_inputs.claim_runner_input(session_id, waiting, "second")
        item_id, replay = await orchestration._persist_external_conversation_item(
            session_id,
            conv,
            SessionEventInput.model_validate(
                {
                    "type": "external_conversation_item",
                    "data": {
                        "item_type": "message",
                        "item_data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": mirror_text}],
                        },
                        "response_id": "resp_mirror",
                    },
                }
            ),
            store,
        )
        assert item_id == stable_id and not replay
        assert any(
            event["type"] == "session.input.consumed"
            and event["data"].get("cleared_pending_id") == waiting
            for event in published
        )
        assert not [item for item in store.list_items(session_id).data if item.type == "error"]

        # Stop hides the entry immediately. Heartbeats cannot extend its deadline.
        stopped = pending_inputs.record(
            session_id, [{"type": "input_text", "text": "stopped"}], runner_managed=True
        )
        assert pending_inputs.claim_runner_input(session_id, stopped, "paste-stopped")
        _confirm_paste(session_id, stopped)
        orchestration._session_status_cache[session_id] = "running"
        now = 1560
        await orchestration._reconcile_runner_input_leases(
            store,
            session_id,
            {"active_pending_ids": [], "queued_pending_ids": []},
            set(),
        )
        pending_inputs.mark_interrupted(session_id, [stopped])
        assert not pending_inputs.snapshot_for(session_id)
        assert not pending_inputs.claim_runner_input(session_id, stopped, "paste-stopped")
        for now in (1620, 2340, 8699):
            await orchestration._reconcile_runner_input_leases(
                store,
                session_id,
                {"active_pending_ids": [], "queued_pending_ids": []},
                set(),
            )
        assert pending_inputs.runner_lease_remaining(session_id, stopped) == 1
        now = 8700
        await orchestration._watch_runner_input_lease(store, session_id, stopped)
        errors = [item for item in store.list_items(session_id).data if item.type == "error"]
        assert [error.data.model_dump()["code"] for error in errors] == [
            "native_input_delivery_uncertain"
        ]
    finally:
        orchestration._session_status_cache.pop(session_id, None)
        pending_inputs.resolve(session_id, waiting)
        if stopped is not None:
            pending_inputs.resolve(session_id, stopped)


@pytest.mark.asyncio
async def test_replaying_a_settled_stable_id_cannot_gain_another_paste_claim(db_uri, monkeypatch):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(harness_override="claude-native")
    stable_id = "a" * 32
    content = [{"type": "input_text", "text": "same text"}]
    now = 0.0
    monkeypatch.setattr(pending_inputs, "_now", lambda: now)
    pending_id = pending_inputs.record(conv.id, content, stable_id=stable_id, runner_managed=True)
    attempt_id = pending_inputs.delivery_attributes_for(conv.id, pending_id)["delivery_attempt_id"]
    assert pending_inputs.claim_runner_input(conv.id, pending_id, "original")
    now = 7201.0
    await orchestration._watch_runner_input_lease(store, conv.id, pending_id)

    async def forward(_client, session_id, _conv, _body, *, input_delivery, **_kwargs):
        assert pending_inputs.claim_runner_input(
            session_id,
            pending_id,
            "replay",
            expected_attempt_id=input_delivery["delivery_attempt_id"],
        )
        return True

    runner_forward = AsyncMock(side_effect=forward)
    monkeypatch.setattr(orchestration, "_forward_native_terminal_message", runner_forward)
    try:
        result = await orchestration._dispatch_session_event_to_runner_impl(
            conv.id,
            conv,
            SessionEventInput(
                type="message", data={"role": "user", "content": content, "stable_id": stable_id}
            ),
            store,
            AsyncMock(),
            agent_name=None,
            file_store=None,
            artifact_store=None,
            native_terminal_ready=True,
        )
        late_mirror = pending_inputs.resolve_matching_text(conv.id, "same text")
        assert late_mirror.matched is None
        assert not pending_inputs.claim_runner_input(
            conv.id, pending_id, "late-original", expected_attempt_id=attempt_id
        )
        assert result.item_id is not None and result.pending_id is None
        item = store.get_item(conv.id, result.item_id)
        assert (
            item is not None
            and item.data.model_dump()["code"] == "native_input_delivery_uncertain"
        )
        runner_forward.assert_not_awaited()
    finally:
        pending_inputs.resolve(conv.id, pending_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "harness", ["claude-native", "codex-native", "pi-native", "antigravity-native"]
)
@pytest.mark.parametrize("claim_capable", [True, False])
async def test_buffered_peer_copy_stays_claimable_while_the_runner_holds_it(
    db_uri, monkeypatch, claim_capable, harness
):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(harness_override=harness)
    peer_id = "c" * 32
    now = 0.0
    monkeypatch.setattr(pending_inputs, "_now", lambda: now)
    monkeypatch.setattr(
        orchestration, "_native_terminal_runtime", lambda _: ("Native", None, harness)
    )
    real_forward = orchestration._forward_native_terminal_message
    headers = {"X-Omnigent-Pending-Input-Claim": "1"} if claim_capable else {}
    runner = httpx.AsyncClient(
        base_url="http://runner",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(202, json={"status": "buffered"}, headers=headers)
        ),
    )

    async def forward(_client, session_id, conversation, body, *, input_delivery, **_kwargs):
        return await real_forward(
            runner, session_id, conversation, body, input_delivery=input_delivery
        )

    monkeypatch.setattr(orchestration, "_forward_native_terminal_message", forward)
    try:
        with pytest.raises(HTTPException, match="buffered"):
            await orchestration._dispatch_session_event_to_runner_impl(
                conv.id,
                conv,
                SessionEventInput(
                    type="message",
                    data={
                        "role": "user",
                        "content": [{"type": "input_text", "text": "peer report"}],
                        "peer_message_id": peer_id,
                    },
                ),
                store,
                AsyncMock(),
                agent_name=None,
                file_store=None,
                artifact_store=None,
                native_terminal_ready=True,
            )
        # Only a Claude runner claims its buffered copy; other harnesses keep base cleanup.
        holds = claim_capable and harness == "claude-native"
        assert pending_inputs.holds_peer_message(conv.id, peer_id) is holds
        if not holds:
            assert pending_inputs.snapshot_for(conv.id) == []
            return
        (entry,) = pending_inputs.snapshot_for(conv.id)
        pending_id = entry["pending_id"]
        attempt_id = pending_inputs.delivery_attributes_for(conv.id, pending_id)[
            "delivery_attempt_id"
        ]
        for now in range(60, 1201, 60):
            await orchestration._reconcile_runner_input_leases(
                store,
                conv.id,
                {"active_pending_ids": [], "queued_pending_ids": [pending_id]},
                set(),
            )
        assert pending_inputs.claim_runner_input(
            conv.id, pending_id, "drained", expected_attempt_id=attempt_id
        )
        assert not store.list_items(conv.id).data
    finally:
        await runner.aclose()
        for task in list(orchestration._runner_input_lease_tasks):
            task.cancel()
        for pending_id in pending_inputs.pending_ids(conv.id):
            pending_inputs.resolve(conv.id, pending_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "confirmed",
        "refused",
        "reply-lost",
        "unconfirmed",
        "no-terminal-edge",
        "mirror-before-reply-loss",
    ],
)
async def test_system_delivery_waits_for_the_runner_outcome(db_uri, monkeypatch, outcome):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(harness_override="claude-native")
    posted: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.query == b"stream=true"
        posted.append(json.loads(request.content))
        pending_id = posted[-1]["pending_id"]
        assert pending_inputs.claim_runner_input(conv.id, pending_id, "original")
        if outcome == "mirror-before-reply-loss":
            assert pending_inputs.resolve_matching_text(conv.id, "Answer to the card").matched
        if outcome in {"reply-lost", "mirror-before-reply-loss"}:
            raise httpx.ReadError("reply lost")
        payload = {"type": "response.completed"}
        if outcome == "no-terminal-edge":
            payload = {"type": "response.in_progress"}
        if outcome == "refused":
            payload = {
                "type": "response.failed",
                "error": {"message": "paste refused", "undelivered": True},
            }
        if outcome == "unconfirmed":
            payload = {
                "type": "response.failed",
                "error": {
                    "code": "native_input_delivery_uncertain",
                    "message": "may have arrived",
                },
            }
        return httpx.Response(
            200,
            text=f"data: {json.dumps(payload)}\n\n",
            headers={"content-type": "text/event-stream", "X-Omnigent-Pending-Input-Claim": "1"},
        )

    monkeypatch.setattr(orchestration, "_ensure_runner_relay", lambda *_a, **_k: None)
    monkeypatch.setattr(
        orchestration,
        "_ensure_native_terminal_ready",
        AsyncMock(return_value=orchestration._NativeTerminalEnsureOutcome(error=None)),
    )
    monkeypatch.setattr(orchestration, "_watch_runner_input_lease", AsyncMock())
    monkeypatch.setattr(orchestration, "_DELIVER_RETRY_MAX_S", 0.0)
    timeouts: list[float] = []

    async def record_timeout(request: httpx.Request) -> None:
        timeouts.append(request.extensions["timeout"]["read"])

    async with httpx.AsyncClient(
        base_url="http://runner",
        transport=httpx.MockTransport(respond),
        event_hooks={"request": [record_timeout]},
    ) as client:
        monkeypatch.setattr(orchestration, "_get_runner_client", AsyncMock(return_value=client))
        try:
            delivered = await orchestration._deliver_with_retry(
                conv.id,
                "Answer to the card",
                conversation_store=store,
                runner_router=None,
                persist_failure=False,
            )
            assert timeouts == [pytest.approx(605.0)]
            assert delivered is {"confirmed": True, "refused": False}.get(outcome)
            notices = store.list_items(conv.id).data
            if delivered is None:
                assert [item.data.model_dump().get("code") for item in notices] == [
                    "answer_delivery_uncertain"
                ]
            else:
                assert not notices
        finally:
            for body in posted:
                pending_inputs.resolve(conv.id, body["pending_id"])


@pytest.fixture
async def replaced_runner_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    registry = TunnelRegistry()
    runner_id = "runner-test"
    hello = HelloFrame(runner_version="0.1.0", frame_protocol_version=1, harnesses=[], envs=[])
    original = registry.register(runner_id, AsyncMock(), hello)
    send = registry.send_text
    replaced: list[RunnerSession] = []

    async def replace_before_send(session: RunnerSession, data: str) -> None:
        (request,) = session.in_flight.values()
        replacement = registry.register(runner_id, AsyncMock(), hello)
        replaced.append(replacement)
        try:
            await send(session, data)
        finally:
            # The registry also aborts the unused response waiter on replacement.
            if request.head_future.done() and not request.head_future.cancelled():
                request.head_future.exception()

    monkeypatch.setattr(registry, "send_text", replace_before_send)
    monkeypatch.setattr(orchestration, "_ensure_runner_relay", lambda *_a, **_k: None)
    monkeypatch.setattr(
        orchestration,
        "_ensure_native_terminal_ready",
        AsyncMock(return_value=orchestration._NativeTerminalEnsureOutcome(error=None)),
    )
    async with httpx.AsyncClient(
        base_url="http://runner", transport=WSTunnelTransport(registry, runner_id)
    ) as client:
        monkeypatch.setattr(orchestration, "_get_runner_client", AsyncMock(return_value=client))
        yield client

    assert len(replaced) == 1
    assert original.outbound_queue.get_nowait() is None  # Writer retirement marker.
    assert original.outbound_queue.empty()
    assert replaced[0].outbound_queue.empty()


@pytest.mark.asyncio
async def test_answered_card_keeps_retrying_after_tunnel_replacement_before_enqueue(
    db_uri, monkeypatch: pytest.MonkeyPatch, replaced_runner_client: httpx.AsyncClient
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation(harness_override="claude-native").id
    text = "[System: answers to your question card #q1]"
    now = int(time.time())
    card = DetachedCard(
        elicitation_id="answered-card",
        session_id=session_id,
        kind="question",
        state="answered",
        mirror=False,
        params={},
        payload={"qid": "q1"},
        grant_key=None,
        verdict={"action": "accept", "content": {"Question": "Answer"}},
        delivery_text=text,
        created_at=now,
        expires_at=now + 60,
    )
    store.add_detached_card(card)
    backoff = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(orchestration, "_delivery_retry_sleep", backoff)

    try:
        with pytest.raises(asyncio.CancelledError):
            await routes_hooks._deliver_card_verdict(
                card, text, granted=False, conversation_store=store, runner_router=None
            )
        backoff.assert_awaited_once_with(orchestration._DELIVER_RETRY_BACKOFFS_S[0])
        (retained,) = store.list_detached_cards()
        assert retained.elicitation_id == card.elicitation_id
        assert retained.state == "answered"
        assert retained.delivery_text == text
        assert not pending_inputs.snapshot_for(session_id)
    finally:
        for entry in pending_inputs.snapshot_for(session_id):
            pending_inputs.resolve(session_id, entry["pending_id"])


@pytest.mark.asyncio
async def test_confirmed_forward_treats_tunnel_replacement_before_enqueue_as_unsent(
    db_uri, monkeypatch: pytest.MonkeyPatch, replaced_runner_client: httpx.AsyncClient
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation(harness_override="claude-native").id
    monkeypatch.setattr(orchestration, "_DELIVER_RETRY_MAX_S", 0.0)

    try:
        delivered = await orchestration._deliver_with_retry(
            session_id,
            "Answer to the card",
            conversation_store=store,
            runner_router=None,
            persist_failure=False,
        )
        assert delivered is False
        assert not store.list_items(session_id).data
        assert not pending_inputs.snapshot_for(session_id)
    finally:
        for entry in pending_inputs.snapshot_for(session_id):
            pending_inputs.resolve(session_id, entry["pending_id"])


@pytest.mark.asyncio
async def test_interrupted_claim_preserves_uncertainty_report(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation(agent_id="b" * 32).id
    input_stable_id = "a" * 32
    pending_id = pending_inputs.record(
        session_id,
        [{"type": "input_text", "text": "in flight"}],
        stable_id=input_stable_id,
        runner_managed=True,
    )
    attempt_id = pending_inputs.delivery_attributes_for(session_id, pending_id)[
        "delivery_attempt_id"
    ]
    assert pending_inputs.claim_runner_input(session_id, pending_id, "original")
    pending_inputs.mark_interrupted(session_id, [pending_id])
    collector = await start_session_stream_collector(session_id)
    try:
        status = {
            "type": "session.status",
            "status": "failed",
            "response_id": "resp_terminal",
            "pending_id": pending_id,
            "delivery_attempt_id": attempt_id,
            "input_stable_id": input_stable_id,
            "error": {"code": "native_input_delivery_uncertain", "message": "may have arrived"},
        }
        failure = {
            "type": "response.failed",
            "response": {"id": "resp_terminal"},
            "pending_id": pending_id,
            "delivery_attempt_id": attempt_id,
            "input_stable_id": input_stable_id,
            "error": {"code": "native_input_delivery_uncertain", "message": "may have arrived"},
        }
        async with httpx.AsyncClient(
            base_url="http://runner",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    text=(
                        f"data: {json.dumps(status)}\n\n"
                        f"data: {json.dumps(failure)}\n\n"
                        "data: [DONE]\n\n"
                    ),
                )
            ),
        ) as client:
            await asyncio.wait_for(
                orchestration._relay_runner_stream(session_id, client, store), timeout=3
            )
        assert not pending_inputs.runner_input_is_claimed(session_id, pending_id)
        assert [
            item.data.model_dump().get("code")
            for item in store.list_items(session_id).data
            if item.type == "error"
        ] == ["native_input_delivery_uncertain"]
        attempts: dict[str, str] = {}
        while len(attempts) < 2:
            event = await collector.next_event()
            if event["type"] in {"session.status", "response.error"} and event.get("error"):
                attempts[event["type"]] = event["error"]["delivery_attempt_id"]
        assert attempts == {"session.status": attempt_id, "response.error": attempt_id}
        snapshot = await orchestration._get_session_snapshot(
            store, session_id, include_items=False
        )
        assert snapshot.status == "failed"
        assert snapshot.last_task_error is not None
        assert snapshot.last_task_error["delivery_attempt_id"] == attempt_id
        assert snapshot.last_task_error["input_stable_id"] == input_stable_id
    finally:
        await collector.stop()
        pending_inputs.resolve(session_id, pending_id)
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.parametrize("runner_managed", [False, True])
@pytest.mark.asyncio
async def test_early_native_refusal_keeps_its_reason_in_history(db_uri, runner_managed):
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation().id
    stable_id = "a" * 32
    pending_id = pending_inputs.record(
        session_id,
        [{"type": "input_text", "text": "read the latest report"}],
        stable_id=stable_id,
        runner_managed=runner_managed,
    )
    identity = pending_inputs.delivery_attributes_for(session_id, pending_id)
    failure = {
        "type": "response.failed",
        "source": "harness",
        **identity,
        "response": {
            "status": "failed",
            "error": {
                "code": "runner_error",
                "message": "The terminal rejected the input while initializing.",
                "undelivered": True,
            },
        },
    }
    running = {"type": "session.status", "status": "running"}
    try:
        async with httpx.AsyncClient(
            base_url="http://runner",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    text=(
                        f"data: {json.dumps(failure)}\n\n"
                        f"data: {json.dumps(running)}\n\n"
                        "data: [DONE]\n\n"
                    ),
                )
            ),
        ) as client:
            await asyncio.wait_for(
                orchestration._relay_runner_stream(session_id, client, store), timeout=3
            )
        assert orchestration._session_status_cache[session_id] == "running"
        user, error = store.list_items(session_id, order="asc").data
        assert user.id == stable_id and user.status == "failed"
        assert error.type == "error" and error.response_id == user.response_id
        assert error.data.model_dump()["message"] == failure["response"]["error"]["message"]
        assert error.data.model_dump()["delivery_attempt_id"] == identity["delivery_attempt_id"]
    finally:
        pending_inputs.resolve(session_id, pending_id)
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_lapsed_annotation_keeps_explicit_retry_and_records_error(db_uri, monkeypatch):
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation().id
    now = 0.0
    monkeypatch.setattr(pending_inputs, "_now", lambda: now)
    stable_id = "a" * 32
    deferred: list[tuple[str, str]] = []
    monkeypatch.setattr(store, "annotation_receipt_for_item", lambda *args: object())
    monkeypatch.setattr(
        store,
        "defer_unsubmitted_annotation",
        lambda session, item, *, runner_reported: deferred.append((session, item)),
    )
    pending_id = pending_inputs.record(
        session_id,
        [{"type": "input_text", "text": "annotation"}],
        stable_id=stable_id,
        runner_managed=True,
    )
    now = 601.0
    await orchestration._watch_runner_input_lease(store, session_id, pending_id)

    assert deferred == [(session_id, stable_id)]
    assert not pending_inputs.snapshot_for(session_id)
    (error,) = store.list_items(session_id).data
    assert error.type == "error"
    assert "not delivered" in error.data.model_dump()["message"]


@pytest.fixture
def relay_clock(monkeypatch: pytest.MonkeyPatch) -> _RelayClock:
    clock = _RelayClock()
    relay_asyncio = SimpleNamespace(**vars(asyncio))
    relay_asyncio.get_running_loop = lambda: clock
    relay_asyncio.sleep = clock.sleep
    monkeypatch.setattr(orchestration, "asyncio", relay_asyncio)
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 10.0)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 1.0)
    return clock


@pytest.mark.asyncio
async def test_natural_eof_reconnects_and_recovers_lifecycle(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An abrupt successful HTTP EOF must use the relay recovery window."""
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation()
    session_id = conversation.id
    orchestration._session_status_cache.pop(session_id, None)
    attempts = 0
    collector = await start_session_stream_collector(session_id)

    def respond(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        heartbeat = b'data: {"type":"session.heartbeat"}\n\n'
        if attempts == 1:
            # The runner reported a live turn, then the HTTP body ended before
            # its terminal lifecycle event or the stream's [DONE] sentinel.
            return httpx.Response(
                200,
                stream=_NaturalEofStream(
                    heartbeat,
                    b'data: {"type":"session.status","status":"running"}\n\n',
                ),
            )
        return httpx.Response(
            200,
            stream=_NaturalEofStream(
                heartbeat,
                b'data: {"type":"session.status","status":"idle"}\n\n',
                b"data: [DONE]\n\n",
            ),
        )

    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 10.0)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.0)
    try:
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            task = asyncio.create_task(
                orchestration._relay_runner_stream(session_id, client, store)
            )
            try:
                statuses: list[str] = []
                while len(statuses) < 2:
                    event = await collector.next_event(timeout=10.0)
                    if event.get("type") == "session.status":
                        statuses.append(event["status"])
                await asyncio.wait_for(task, timeout=10.0)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        assert attempts == 2
        assert statuses == ["running", "idle"]
        assert orchestration._session_status_cache[session_id] == "idle"
    finally:
        await collector.stop()
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_persistent_natural_eof_exhausts_disconnect_grace(
    db_uri: str, relay_clock: _RelayClock
) -> None:
    """Repeated EOFs fail through the existing bounded grace decision."""
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation()
    session_id = conversation.id
    orchestration._session_status_cache[session_id] = "running"
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        relay_clock.now += 4.0
        return httpx.Response(
            200,
            stream=_NaturalEofStream(b'data: {"type":"session.status","status":"running"}\n\n'),
        )

    try:
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            await asyncio.wait_for(
                orchestration._relay_runner_stream(session_id, client, store), timeout=10
            )
        assert attempts == 3
        assert orchestration._session_status_cache[session_id] == "failed"
        persisted = store.get_conversation(session_id)
        assert persisted is not None
        assert persisted.labels["omnigent.last_task_error_code"] == "runner_disconnected"
    finally:
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped_runner", [None, "runner-current", "runner-old"])
async def test_natural_eof_matches_intent_to_the_relay_runner(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, stopped_runner: str | None
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    runner_id = "runner-current"
    conversation = store.create_conversation(runner_id=runner_id)
    session_id = conversation.id
    store.set_session_live_status(session_id, "running")
    orchestration._session_status_cache[session_id] = "running"
    if stopped_runner is not None:
        orchestration._intentional_stop_sessions[session_id] = stopped_runner
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.0)

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=_NaturalEofStream(b'data: {"type":"session.heartbeat"}\n\n')
        )

    try:
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            await asyncio.wait_for(
                orchestration._relay_runner_stream(session_id, client, store, runner_id=runner_id),
                timeout=10,
            )
        intentional = stopped_runner == runner_id
        assert orchestration._session_status_cache[session_id] == (
            "idle" if intentional else "failed"
        )
        persisted = store.get_conversation(session_id)
        assert persisted is not None
        error = orchestration._last_task_error_from_labels(persisted.labels)
        if intentional:
            assert error is None
            assert session_id not in orchestration._intentional_stop_sessions
        else:
            assert error is not None and error["code"] == "runner_disconnected"
    finally:
        orchestration._intentional_stop_sessions.pop(session_id, None)
        orchestration._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_short_recovered_streams_do_not_share_a_disconnect_deadline(
    db_uri: str, relay_clock: _RelayClock
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation()
    session_id = conversation.id
    orchestration._session_status_cache[session_id] = "running"
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        relay_clock.now += 4.0
        if attempts <= 4:
            return httpx.Response(200, stream=_InterruptedStream(ready=True))
        return httpx.Response(
            200,
            text=(
                'data: {"type":"session.heartbeat"}\n\n'
                'data: {"type":"session.status","status":"idle"}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    try:
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            await asyncio.wait_for(
                orchestration._relay_runner_stream(session_id, client, store), timeout=10
            )
        assert attempts == 5, "the relay gave up despite receiving fresh runner heartbeats"
        assert orchestration._session_status_cache[session_id] == "idle"
        persisted = store.get_conversation(session_id)
        assert persisted is not None
        assert not persisted.labels.get("omnigent.last_task_error_code")
    finally:
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_repeated_recovery_keeps_backoff_and_remains_cancellable(
    db_uri: str, relay_clock: _RelayClock
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation()
    session_id = conversation.id
    orchestration._session_status_cache[session_id] = "running"
    recovered = asyncio.Event()
    pause = asyncio.Event()
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        relay_clock.now += 4.0
        if attempts == 20:
            recovered.set()
        return httpx.Response(
            200, stream=_InterruptedStream(ready=True, pause=pause if attempts == 20 else None)
        )

    try:
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            task = asyncio.create_task(
                orchestration._relay_runner_stream(session_id, client, store)
            )
            try:
                await asyncio.wait_for(recovered.wait(), timeout=10)
                assert not task.done()
                assert relay_clock.delays == [1.0] * 19
                assert orchestration._session_status_cache[session_id] == "running"
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert attempts == 20
    finally:
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy_attempts", [0, 2])
async def test_an_unrecovered_stream_still_exhausts_its_grace(
    db_uri: str, relay_clock: _RelayClock, healthy_attempts: int
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation()
    session_id = conversation.id
    orchestration._session_status_cache[session_id] = "running"
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        relay_clock.now += 4.0
        return httpx.Response(200, stream=_InterruptedStream(ready=attempts <= healthy_attempts))

    try:
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            await asyncio.wait_for(
                orchestration._relay_runner_stream(session_id, client, store), timeout=10
            )
        assert attempts == max(1, healthy_attempts) + 2
        assert orchestration._session_status_cache[session_id] == "failed"
        persisted = store.get_conversation(session_id)
        assert persisted is not None
        assert persisted.labels["omnigent.last_task_error_code"] == "runner_disconnected"
    finally:
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)
