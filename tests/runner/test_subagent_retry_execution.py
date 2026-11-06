"""Exercise final input receipts through the runner's real SSE relay."""

import asyncio
import contextlib
import json
import threading
import time
from uuid import uuid4

import httpx
import pytest
import uvicorn

from omnigent.entities.subagent_retry import InputExecutionClaim, InputExecutionReceipt, RetryInput
from omnigent.runner import create_runner_app
from omnigent.server.schemas import InputExecutionClaimEvent
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient
from tests.runner.test_runner_dispatch import _runner_test_client, _sse_text_turn


@contextlib.asynccontextmanager
async def _receiver(journal, production_http):
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.server.subagent_retry_admission import SubagentRetryAdmission
    from tests.server.routes.test_session_event_batches import _make_client

    token = "test-child-receiver"
    receiver = token_bound_runner_id(token)
    journal.conversations.set_runner_id(journal.child, receiver)
    service = SubagentRetryAdmission(
        journal.store, journal.conversations, host_store=None, host_registry=None
    )
    server = _make_client(journal.conversations)
    server.app.state.subagent_retry_admission = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app),
        base_url="http://server",
        headers=dict(server.headers),
    ) as client:

        async def post(claim):
            if production_http:
                response = await client.post(
                    f"/sessions/{journal.child}/input-admission",
                    json=claim.model_dump(mode="json"),
                    headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
                )
                response.raise_for_status()
                return InputExecutionReceipt.model_validate(response.json())
            return await service.admit_execution(journal.child, claim, runner_id=receiver)

        yield post


@pytest.mark.asyncio
@pytest.mark.parametrize("production_http", [False, True], ids=["component", "http"])
@pytest.mark.parametrize("failure", ["rpc", "connection"])
async def test_native_definite_refusal_finalizes_once_without_attempt(
    db_uri, tmp_path, monkeypatch, production_http, failure
):
    from omnigent.harnesses.codex_native.app_server import CodexAppServerResponseError
    from omnigent.harnesses.codex_native.bridge import (
        CODEX_APP_SERVER_STOPPED,
        CodexNativeBridgeState,
        read_bridge_state,
        write_bridge_state,
    )
    from omnigent.inner.codex_native_executor import CodexNativeExecutor
    from omnigent.inner.executor import ExecutorError
    from omnigent.native.auto_retry import (
        AutoRetryGuard,
        auto_retry_scope,
        get_snapshot,
        observe_turn,
        read_receipt,
    )
    from omnigent.native.input_diagnostics import input_delivery_scope
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from tests.inner.test_codex_native_executor import _FakeCodexNativeClient
    from tests.stores.test_subagent_retry_execution import _prepared
    from tests.stores.test_subagent_retry_store import _journal

    journal = _journal(db_uri, SqlAlchemyConversationStore(db_uri))
    journal.conversations.update_conversation(journal.child, harness_override="codex-native")
    chain, claim = _prepared(journal)
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id=journal.child,
            thread_id="native-thread",
            socket_path=str(tmp_path / "unused.sock"),
            codex_home=str(tmp_path / "codex-home"),
        ),
    )
    snapshot = observe_turn(tmp_path, journal.child, "root-turn", idle=True)
    guard = AutoRetryGuard(
        session_id=journal.child,
        chain_id=chain.chain_id,
        input_id=claim.input_id,
        expected_turn_id="root-turn",
        expected_input_generation=1,
        expected_native_generation=snapshot.generation,
        admission_deadline=time.time() + 30,
        attempt=1,
        of=3,
    )
    requests, receipts = [], []

    class Refusing(_FakeCodexNativeClient):
        async def connect(self):
            if failure == "connection":
                raise ConnectionError("Native app-server is not listening")
            return await super().connect()

        async def request(self, method, params):
            requests.append(method)
            if method == "turn/start":
                raise CodexAppServerResponseError({"code": -32600, "message": "invalid turn id"})
            return await super().request(method, params)

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.CodexAppServerClient", Refusing
    )
    async with _receiver(journal, production_http) as receive:

        async def reserve():
            result = await receive(claim.model_copy(update={"boundary": "native_reserve"}))
            receipts.append(result)
            assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0
            return result.state == "reserved"

        async def settle(outcome, turn_id, reason, detail):
            assert outcome == "refused" and turn_id is None
            refusal = claim.model_copy(
                update={
                    "boundary": "native_refuse",
                    "native_reason": reason,
                    "native_detail": detail,
                }
            )
            result = await receive(refusal)
            repeated = await receive(refusal)
            assert repeated == result
            receipts.append(result)
            return result.accepted

        with (
            auto_retry_scope(guard, reserve, settle),
            input_delivery_scope({"input_stable_id": claim.input_id}),
        ):
            events = [
                event
                async for event in CodexNativeExecutor(tmp_path).run_turn(
                    [{"role": "user", "content": [{"type": "input_text", "text": "retry"}]}],
                    [],
                    "",
                )
            ]
    [error] = [event for event in events if isinstance(event, ExecutorError)]
    expected_code = (
        CODEX_APP_SERVER_STOPPED.code if failure == "connection" else "codex_native_input_refused"
    )
    assert error.undelivered and error.code == expected_code
    assert requests.count("turn/start") == int(failure == "rpc")
    assert [receipt.state for receipt in receipts] == (
        ["reserved", "refused"] if failure == "rpc" else ["refused"]
    )
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 0
    closed = journal.store.get_input(claim.input_id)
    assert closed.state == "closed" and closed.reason["code"] == error.code
    assert read_bridge_state(tmp_path).active_turn_id is None
    assert get_snapshot(tmp_path, journal.child).generation == snapshot.generation
    local = read_receipt(tmp_path, claim.input_id)
    if failure == "rpc":
        assert local.reason == error.code
    else:
        assert local is None
    [final] = journal.store.list_outbox(owner=journal.owner)
    assert final.attempts == 0 and final.outcome.reason == error.code
    assert final.outcome.output == error.message and final.outcome.error == journal.terminal.error
    assert journal.store.get_chain(chain.chain_id).phase == "finalized"
    current = journal.store.get_chain(chain.chain_id)
    assert (
        journal.store.prepare_attempt(
            chain.chain_id, owner=journal.owner, expected_version=current.version, now=1000
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("production_http", [False, True], ids=["component", "http"])
@pytest.mark.parametrize("queued_seconds", [0, 120])
async def test_human_buffered_during_retry_admission_executes_on_own_turn(
    db_uri, production_http, queued_seconds, monkeypatch
):
    from omnigent.runtime.harnesses._scaffold import HarnessApp
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from tests._helpers.live_server import reserve_port
    from tests._helpers.polling import poll_until_async
    from tests.stores.test_subagent_retry_execution import _prepared
    from tests.stores.test_subagent_retry_store import _journal

    journal = _journal(db_uri, SqlAlchemyConversationStore(db_uri))
    chain, claim = _prepared(journal)
    human_id = uuid4().hex
    entered, release, human_ran = asyncio.Event(), asyncio.Event(), asyncio.Event()
    executed, admissions, markers = [], [], []
    wall_time = time.time

    class Harness(HarnessApp):
        async def run_turn(self, request, ctx):
            executed.append((request.input_id, ctx.response_id))
            markers.append(request.auto_retry)
            if request.input_id == human_id:
                human_ran.set()

    harness = Harness()
    app = harness.build()
    app.state.conversation_id = journal.child
    app.state.harness_auth_token = "test-harness"
    with reserve_port() as sock:
        sock.listen()
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            await poll_until_async(lambda: server.started, timeout_s=5)
            async with _receiver(journal, production_http) as receive:

                class Server(NullServerClient):
                    async def post(self, url, **kwargs):
                        if url.endswith("/input-admission"):
                            body = InputExecutionClaim.model_validate(kwargs["json"])
                            admissions.append(body)
                            if body.input_id == claim.input_id:
                                entered.set()
                                await release.wait()
                            result = await receive(body)
                            if body.input_id == claim.input_id and queued_seconds:
                                monkeypatch.setattr(
                                    "omnigent.runner.app.time.time",
                                    lambda: wall_time() + queued_seconds,
                                )
                            return httpx.Response(
                                200,
                                json=result.model_dump(),
                                request=httpx.Request("POST", "http://server" + url),
                            )
                        return await super().post(url, **kwargs)

                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}",
                    headers={"Authorization": "Bearer test-harness"},
                ) as hc:
                    runner = create_runner_app(
                        process_manager=_FakeProcessManager(hc), server_client=Server()
                    )
                    monkeypatch.setattr(
                        "omnigent.runner.app.pending_approvals.has_pending", lambda _: True
                    )
                    async with _runner_test_client(runner) as client:
                        body = {
                            "type": "message",
                            "role": "user",
                            "model": "test",
                            "content": [{"type": "input_text", "text": "retry"}],
                            "input_id": claim.input_id,
                            "retry_guard": claim.retry.model_dump(),
                            "auto_retry": claim.retry.metadata.model_dump(),
                            "input_admission_required": True,
                            "input_admission_deadline": time.time() + 30,
                        }
                        task = asyncio.create_task(
                            client.post(
                                f"/v1/sessions/{journal.child}/events?stream=true", json=body
                            )
                        )
                        try:
                            await asyncio.wait_for(entered.wait(), 5)
                            response = await client.post(
                                f"/v1/sessions/{journal.child}/events",
                                json={
                                    **body,
                                    "input_id": human_id,
                                    "retry_guard": None,
                                    "auto_retry": None,
                                    "content": [
                                        {"type": "input_text", "text": "new human request"}
                                    ],
                                },
                            )
                            assert response.status_code == 202
                            assert journal.store.get_input(human_id) is None
                            assert executed == []
                        finally:
                            release.set()
                        assert (await asyncio.wait_for(task, 8)).status_code == 200
                        await asyncio.wait_for(human_ran.wait(), 8)
                        await poll_until_async(lambda: len(admissions) == 2, timeout_s=3)
        finally:
            release.set()
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
    assert len(executed) == 2 and executed[0][0] == claim.input_id and executed[1][0] == human_id
    assert executed[0][1] != executed[1][1]
    assert markers == [claim.retry.metadata, None]
    human = journal.store.get_input(human_id)
    assert human.state == "accepted" and human.turn_id == executed[1][1]
    assert [admission.turn_id for admission in admissions] == [turn_id for _, turn_id in executed]
    assert journal.store.get_chain(chain.chain_id).accepted_attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "lost_ack", "replacement"])
async def test_admission_relay_reconciles_one_id_and_fences_late_replacement(outcome):
    entered, release = asyncio.Event(), asyncio.Event()
    deadline = time.time() + 30
    input_id, session_id, turn_id = "a" * 32, "child", "resp_multiturn"
    claim = InputExecutionClaim(input_id=input_id, turn_id=turn_id, admission_deadline=deadline)
    accepted = InputExecutionReceipt(
        state="accepted",
        reason="accepted",
        receipt=RetryInput(
            input_id=input_id,
            chain_id=None,
            target_id=session_id,
            kind="human",
            attempt=0,
            state="accepted",
            payload={},
            version=1,
            turn_id=turn_id,
            input_generation=2,
            reason=None,
        ),
    )
    admissions, lookups = [], []

    class Server(NullServerClient):
        async def post(self, url, **kwargs):
            if url.endswith("/input-admission"):
                admissions.append(kwargs["json"])
                entered.set()
                await release.wait()
                if outcome == "lost_ack":
                    raise httpx.ReadTimeout("Accepted input response lost")
                return httpx.Response(
                    200,
                    json=accepted.model_dump(),
                    request=httpx.Request("POST", "http://server" + url),
                )
            if url.endswith("/input-admission/resolve"):
                lookups.append(kwargs["json"])
                return httpx.Response(
                    200,
                    json=accepted.model_dump(),
                    request=httpx.Request("POST", "http://server" + url),
                )
            return await super().post(url, **kwargs)

    frames = _sse_text_turn("done")
    event = InputExecutionClaimEvent(claim_id="claim", claim=claim)
    frames.insert(
        1, "event: input.execution.claim\ndata: " + json.dumps(event.model_dump()) + "\n\n"
    )
    harness = _ScriptedHarnessClient(frames)
    app = create_runner_app(process_manager=_FakeProcessManager(harness), server_client=Server())
    replacement = {"turn_id": "replacement", "input_id": "replacement", "input_generation": 3}
    async with _runner_test_client(app) as client:
        task = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test",
                    "content": [{"type": "input_text", "text": "retry"}],
                    "input_id": input_id,
                    "input_admission_required": True,
                    "input_admission_deadline": deadline,
                },
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            if outcome == "replacement":
                app.state.turn_bind_epoch[session_id] += 1
                app.state.live_response_id[session_id] = "replacement"
                app.state.turn_identities[session_id] = dict(replacement)
        finally:
            release.set()
            response = await asyncio.wait_for(task, 5)
    assert response.status_code == 200
    assert len(admissions) == 1 and admissions[0]["input_id"] == input_id
    assert len(lookups) == int(outcome == "lost_ack")
    if lookups:
        assert lookups[0] == admissions[0]
    results = [
        event for event in harness.patched_events if event.get("type") == "input_execution_result"
    ]
    assert len(results) == 1
    assert results[0]["result"]["accepted"] is (outcome != "replacement")
    if outcome == "replacement":
        assert results[0]["result"]["reason"] == "superseded"
        assert app.state.turn_identities[session_id] == replacement


@pytest.mark.asyncio
async def test_runner_posts_same_control_receipt_before_reply_and_recovers_lost_receipt_ack():
    control_id = "c" * 32
    state = {"state": "prepared", "version": 0}
    writes, reads = [], []

    class Server(NullServerClient):
        async def get(self, url, **kwargs):
            if url.endswith(f"/input-controls/{control_id}"):
                reads.append(url)
                return httpx.Response(
                    200, json=state, request=httpx.Request("GET", "http://server" + url)
                )
            return await super().get(url, **kwargs)

        async def post(self, url, **kwargs):
            if url.endswith(f"/input-controls/{control_id}/resolve"):
                writes.append(kwargs["json"])
                state.update(state="accepted", version=1)
                raise httpx.ReadTimeout("Control receipt committed; acknowledgement lost")
            return await super().post(url, **kwargs)

    harness = _ScriptedHarnessClient(_sse_text_turn("next input"))
    manager = _FakeProcessManager(harness)
    app = create_runner_app(process_manager=manager, server_client=Server())
    async with _runner_test_client(app) as client:
        interrupted = await client.post(
            "/v1/sessions/child/events",
            json={
                "type": "interrupt",
                "input_control": {"session_id": "child", "input_id": control_id},
            },
        )
        assert interrupted.status_code == 204
        assert writes == [{"outcome": "accepted", "expected_version": 0}]
        response = await client.post(
            "/v1/sessions/child/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "model": "test",
                "content": [{"type": "input_text", "text": "human input"}],
            },
        )
        assert response.status_code == 200
    assert len(reads) == 2 and len(writes) == 1


@pytest.mark.asyncio
async def test_cancelled_admission_is_closed_before_next_human_can_start(db_uri):
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from tests.stores.test_subagent_retry_store import _journal

    journal = _journal(db_uri, SqlAlchemyConversationStore(db_uri))
    journal.conversations.set_runner_id(journal.child, "receiver")
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    old_id, new_id = uuid4().hex, uuid4().hex
    deadline = time.time() + 30
    resolves = []

    class Server(NullServerClient):
        async def post(self, url, **kwargs):
            if url.endswith("/input-admission"):
                claim = InputExecutionClaim.model_validate(kwargs["json"])

                def admit():
                    if claim.input_id == old_id:
                        entered.set()
                        assert release.wait(5)
                    try:
                        return journal.store.admit_execution(
                            journal.child, claim, runner_id="receiver"
                        )
                    finally:
                        if claim.input_id == old_id:
                            done.set()

                result = await asyncio.to_thread(admit)
            elif url.endswith("/input-admission/resolve"):
                claim = InputExecutionClaim.model_validate(kwargs["json"])
                resolves.append(claim.input_id)
                result = await asyncio.to_thread(
                    journal.store.resolve_execution,
                    journal.child,
                    claim,
                    runner_id="receiver",
                    close=True,
                )
            else:
                return await super().post(url, **kwargs)
            return httpx.Response(
                200, json=result.model_dump(), request=httpx.Request("POST", "http://server" + url)
            )

    def frames(input_id, turn_id):
        output = [part.replace("resp_multiturn", turn_id) for part in _sse_text_turn("done")]
        claim = InputExecutionClaimEvent(
            claim_id=input_id,
            claim=InputExecutionClaim(
                input_id=input_id,
                turn_id=turn_id,
                admission_deadline=deadline,
            ),
        )
        output.insert(
            1, "event: input.execution.claim\ndata: " + json.dumps(claim.model_dump()) + "\n\n"
        )
        return output

    harness = _ScriptedHarnessClient(frames(old_id, "old-turn"))
    app = create_runner_app(process_manager=_FakeProcessManager(harness), server_client=Server())

    def body(input_id):
        return {
            "type": "message",
            "role": "user",
            "model": "test",
            "content": [{"type": "input_text", "text": "human"}],
            "input_id": input_id,
            "input_admission_required": True,
            "input_admission_deadline": deadline,
        }

    async with _runner_test_client(app) as client:
        task = asyncio.create_task(
            client.post(f"/v1/sessions/{journal.child}/events?stream=true", json=body(old_id))
        )
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (
                await client.post(
                    f"/v1/sessions/{journal.child}/events", json={"type": "interrupt"}
                )
            ).status_code == 204
            harness._sse_frames = frames(new_id, "new-human-turn")
            accepted = await client.post(
                f"/v1/sessions/{journal.child}/events?stream=true", json=body(new_id)
            )
            assert accepted.status_code == 200
        finally:
            release.set()
        assert await asyncio.to_thread(done.wait, 3)
    control = journal.store.get_control(journal.child)
    assert resolves == [old_id]
    assert (
        control.input_id == new_id
        and control.turn_id == "new-human-turn"
        and control.generation == 2
    )
    assert journal.store.get_input(old_id).state == "closed"
