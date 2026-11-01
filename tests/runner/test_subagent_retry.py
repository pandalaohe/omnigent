"""Scheduling traverses the HTTP journal; receiver outcomes are deterministic."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities.subagent_retry import retry_identity
from omnigent.runner import subagent_work
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runner.subagent_retry import SubagentRetryRuntime, retry_attempt_limit
from omnigent.server.subagent_retry_admission import SubagentRetryAdmission
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.routes.test_session_event_batches import _make_client
from tests.stores.test_subagent_retry_store import _journal


@asynccontextmanager
async def runtime_case(db_uri, *, limit=3, mode="accepted", early=False):
    journal = _journal(db_uri, SqlAlchemyConversationStore(db_uri))
    token = "test-parent-retry-runtime"
    journal.conversations.clear_runner_id(journal.parent)
    assert journal.conversations.set_runner_id(journal.parent, token_bound_runner_id(token))
    now, sent = [100.0], []

    def clock():
        return now[0]

    service = SubagentRetryAdmission(
        journal.store, journal.conversations, host_store=None, host_registry=None, clock=clock
    )
    server = _make_client(journal.conversations)
    server.app.state.subagent_retry_admission = service
    app = FastAPI()
    app.mount("/v1", server.app)
    transport = httpx.ASGITransport(app=app)
    pending = asyncio.Event()

    async def dispatch(request):
        if request.url.path.endswith("/dispatch"):
            input_id = request.url.path.split("/")[-2]
            receipt = journal.store.get_input(input_id)
            chain = journal.store.get_chain(receipt.chain_id)
            sent.append(input_id)
            claimed = journal.store.claim_input(
                input_id, owner=chain.owner, admission_deadline=clock() + 60
            )
            assert claimed.claimed
            if mode in {"pending", "lost_ack"}:
                await runtime.accept_terminal(journal.terminal, "duplicate root failure")
                assert not runtime.chains[chain.chain_id].accepted.is_set()
                pending.set()
                if mode == "lost_ack":
                    raise httpx.ReadError("lost dispatch acknowledgement", request=request)
                return httpx.Response(
                    200,
                    json={
                        "state": "pending",
                        "receipt": claimed.receipt.model_dump(mode="json"),
                        "admission_deadline": claimed.receipt.payload["admission_deadline"],
                    },
                )
            if mode == "accepted":
                if early:
                    await runtime.accept_terminal(
                        journal.terminal.model_copy(
                            update={
                                "turn_id": f"turn-{len(sent)}",
                                "input_id": input_id,
                                "input_generation": claimed.receipt.expected_input_generation + 1,
                            }
                        ),
                        "same error",
                    )
                    receipt = journal.store.get_input(input_id)
                else:
                    receipt = journal.store.record_acceptance(
                        input_id, owner=chain.owner, turn_id=f"turn-{len(sent)}"
                    )
                return httpx.Response(
                    200, json={"state": "accepted", "receipt": receipt.model_dump(mode="json")}
                )
            receipt = journal.store.close_input(
                input_id,
                owner=chain.owner,
                expected_version=journal.store.get_input(input_id).version,
                reason={"code": mode},
            )
            return httpx.Response(
                200, json={"state": "closed", "receipt": receipt.model_dump(mode="json")}
            )
        return await transport.handle_async_request(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(dispatch),
        base_url="http://server",
        headers={**dict(server.headers), RUNNER_TUNNEL_TOKEN_HEADER: token},
    ) as client:
        runtime = SubagentRetryRuntime(client, clock=clock, limit=limit)
        inbox = asyncio.Queue()
        subagent_work._session_inboxes_ref[journal.parent] = inbox
        entry = subagent_work.register_subagent_work(
            parent_session_id=journal.parent,
            child_session_id=journal.child,
            agent="worker",
            title="retry task",
            registered_by="sys_session_send",
        )
        try:
            yield SimpleNamespace(
                runtime=runtime,
                journal=journal,
                inbox=inbox,
                entry=entry,
                now=now,
                sent=sent,
                client=client,
                service=service,
                pending=pending,
            )
        finally:
            await runtime.close()
            subagent_work.unregister_subagent_work_for_session(journal.child)
            subagent_work._session_inboxes_ref.pop(journal.parent, None)


@pytest.mark.parametrize(
    "raw,expected", [(None, 3), ("0", 0), ("1", 1), ("5", 5), ("abc", 3), ("-1", 3)]
)
def test_deployment_retry_limit(raw, expected, monkeypatch, caplog):
    if raw is None:
        monkeypatch.delenv("OMNIGENT_SUBAGENT_AUTO_RETRY_ATTEMPTS", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_SUBAGENT_AUTO_RETRY_ATTEMPTS", raw)
    assert retry_attempt_limit() == expected
    assert ("using 3" in caplog.text) == (raw in {"abc", "-1"})


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 3, 5])
async def test_exact_attempts_duplicate_deadlines_and_final_tombstone(db_uri, limit):
    async with runtime_case(db_uri, limit=limit) as case:
        rt, j = case.runtime, case.journal
        ack = await rt.accept_terminal(j.terminal, "same error")
        assert ack.reason == "held" and not ack.delivered
        state = rt.chains[ack.chain_id]
        first_timer = state.timer
        for _ in range(3):
            case.now[0] += 2
            duplicate = await rt.accept_terminal(j.terminal, "same error")
            assert duplicate.reason == "held"
            assert state.chain.due_at == 160 and state.timer is first_timer
        assert case.inbox.empty() and not case.entry.delivered and not case.sent
        assert case.entry.status == "waiting" and case.entry.completed_at is None
        for attempt in range(1, limit + 1):
            case.now[0] = state.chain.due_at - 0.01
            await rt._run(ack.chain_id)
            assert len(case.sent) == attempt - 1
            case.now[0] += 0.01
            await rt._run(ack.chain_id)
            receipt = j.store.get_input(case.sent[-1])
            assert receipt.attempt == attempt and receipt.state == "accepted"
            terminal = j.terminal.model_copy(
                update={
                    "turn_id": receipt.turn_id,
                    "input_id": receipt.input_id,
                    "input_generation": receipt.input_generation,
                }
            )
            await rt.accept_terminal(terminal, "same error")
        final = j.store.get_final(ack.chain_id)
        assert (
            final.attempts == limit
            and final.limit == limit
            and final.outcome.reason == "exhausted"
        )
        assert final.notice_id == retry_identity("subagent-final", ack.chain_id)
        for replay in (j.terminal, terminal):
            await rt.accept_terminal(replay, "late same error")
        assert state.timer is None and len(case.sent) == limit
        assert j.store.get_final(ack.chain_id) == final
        assert case.inbox.empty() and not case.entry.delivered


@pytest.mark.asyncio
async def test_terminal_before_post_reply_accounts_one_attempt_and_one_next_deadline(db_uri):
    async with runtime_case(db_uri, early=True) as case:
        ack = await case.runtime.accept_terminal(case.journal.terminal, "failure")
        case.now[0] = 160
        await case.runtime._run(ack.chain_id)
        state = case.runtime.chains[ack.chain_id]
        assert state.chain.accepted_attempts == 1 and state.chain.due_at == 220
        assert len(case.sent) == 1 and case.inbox.empty()
        await case.runtime._run(ack.chain_id)
        assert len(case.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["admission_denied", "not_forwarded", "launch_refused", "routing_error"]
)
async def test_refusal_finalizes_without_an_invented_attempt(db_uri, reason):
    async with runtime_case(db_uri, mode=reason) as case:
        ack = await case.runtime.accept_terminal(case.journal.terminal, "original failure")
        case.now[0] = 160
        await case.runtime._run(ack.chain_id)
        final = case.journal.store.get_final(ack.chain_id)
        assert final.attempts == 0 and final.outcome.reason == reason
        assert "original failure" in final.outcome.output
        assert not case.entry.delivered and case.inbox.empty()


@pytest.mark.asyncio
async def test_missing_inbox_and_side_threads_cannot_hold(db_uri):
    async with runtime_case(db_uri) as case:
        queue = subagent_work._session_inboxes_ref.pop(case.journal.parent)
        assert await case.runtime.accept_terminal(case.journal.terminal, "failure") is None
        subagent_work._session_inboxes_ref[case.journal.parent] = queue
        case.entry.wrapper_label = "codex-native-subagent"
        case.entry.registered_by = None
        assert (
            await case.runtime.accept_terminal(
                case.journal.terminal, "failure", parent_harness="codex-native"
            )
            is None
        )
        assert not case.runtime.chains and not case.sent


@pytest.mark.asyncio
async def test_zero_keeps_legacy_inbox_path_and_no_chain(db_uri):
    async with runtime_case(db_uri, limit=0) as case:
        assert await case.runtime.accept_terminal(case.journal.terminal, "failure") is None
        ack = subagent_work.mark_subagent_work_terminal(
            case.journal.child, status="failed", output="failure", terminal=case.journal.terminal
        )
        assert ack.delivered and case.inbox.get_nowait()["output"] == "failure"
        assert not case.runtime.chains and not case.sent


@pytest.mark.asyncio
async def test_native_http_held_ack_does_not_deliver_or_settle(db_uri):
    from tests.runner.conftest import _runner_client
    from tests.runner.test_native_subagent_turn_outcome_delivery import _native_app

    async with runtime_case(db_uri) as case:
        app = _native_app("claude-native")
        app.state.subagent_retry_runtime = case.runtime
        async with _runner_client(app, permission_harness="claude-native") as client:
            response = await client.post(
                "/v1/sessions", json={"session_id": case.journal.child, "agent_id": uuid4().hex}
            )
            assert response.status_code == 201, response.text
            data = {**case.journal.terminal.model_dump(), "output": "native failure"}
            response = await client.post(
                f"/v1/sessions/{case.journal.child}/events",
                json={"type": "external_session_status", "data": data},
            )
            assert response.status_code == 200, response.text
            assert response.json()["disposition"] == "held"
            assert case.inbox.empty() and not case.entry.delivered
            chain = case.journal.store.get_chain(response.json()["chain_id"])
            assert chain.due_at == 160 and chain.accepted_attempts == 0


@pytest.mark.asyncio
async def test_owner_routes_require_bound_runner_not_edit_permission(db_uri):
    async with runtime_case(db_uri) as case:
        response = await case.client.post(
            f"/v1/sessions/{case.journal.parent}/retry-owner",
            headers={RUNNER_TUNNEL_TOKEN_HEADER: "wrong-runner"},
        )
        assert response.status_code == 403, response.text
        assert not case.runtime.chains


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["pending", "lost_ack"])
async def test_root_duplicate_cannot_end_a_live_admission_wait(db_uri, mode):
    async with runtime_case(db_uri, mode=mode) as case:
        rt, j = case.runtime, case.journal
        ack = await rt.accept_terminal(j.terminal, "initial failure")
        state = rt.chains[ack.chain_id]
        case.now[0] = state.chain.due_at
        task = asyncio.create_task(rt._run(ack.chain_id))
        try:
            await asyncio.wait_for(case.pending.wait(), 5)
            receipt = j.store.get_input(case.sent[0])
            # The authoritative future deadline prevents even an explicit early close.
            response = await case.client.post(
                f"/v1/sessions/{j.child}/retry-inputs/{receipt.input_id}/lookup",
                json={
                    "owner": state.chain.owner.model_dump(),
                    "expected_version": receipt.version,
                    "reason": {"code": "acceptance_unknown"},
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["state"] == "pending"
            assert response.json()["admission_deadline"] == receipt.payload["admission_deadline"]
            assert not task.done() and j.store.get_input(receipt.input_id).state == "dispatching"
            # The terminal itself proves acceptance; there is no prior acceptance write.
            await rt.accept_terminal(
                j.terminal.model_copy(
                    update={
                        "input_id": receipt.input_id,
                        "turn_id": "fast-failure",
                        "input_generation": receipt.expected_input_generation + 1,
                    }
                ),
                "next failure",
            )
            await asyncio.wait_for(task, 5)
            assert state.chain.accepted_attempts == 1 and len(case.sent) == 1
            assert state.chain.phase == "waiting" and state.chain.due_at == case.now[0] + 60
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_parent_owner_returns_forwarding_ack_with_local_inbox(db_uri):
    async with runtime_case(db_uri) as case:
        j = case.journal
        j.conversations.clear_runner_id(j.parent)
        assert j.conversations.set_runner_id(j.parent, "replacement-runner")
        ack = await case.runtime.accept_terminal(j.terminal, "failure")
        response = subagent_work._subagent_delivery_not_confirmed_response(
            ack, is_runner_known_subagent=True
        )
        assert response.status_code == 503
        assert b'"reason":"missing_parent_inbox"' in response.body
        assert case.inbox.empty() and not case.runtime.chains


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_moved", [False, True])
async def test_sdk_stream_end_preserves_identity_and_obligation(db_uri, owner_moved):
    from tests._helpers.polling import poll_until_async
    from tests.runner.helpers import NullServerClient
    from tests.runner.test_member_obligations import _build_app

    async with runtime_case(db_uri) as case:
        j, posted = case.journal, []

        class Server(NullServerClient):
            async def get(self, url, **kwargs):
                if url == f"/v1/sessions/{j.child}":
                    return httpx.Response(
                        200,
                        json={
                            "session_id": j.child,
                            "parent_session_id": j.parent,
                            "kind": "sub_agent",
                            "labels": {},
                        },
                        request=httpx.Request("GET", "http://server" + url),
                    )
                return await super().get(url, **kwargs)

            async def post(self, url, **kwargs):
                posted.append((url, kwargs.get("json")))
                return await super().post(url, **kwargs)

        app, _manager, _harness = _build_app(Server(), harness="claude-sdk")
        app.state.subagent_retry_runtime = case.runtime
        if owner_moved:
            j.conversations.clear_runner_id(j.parent)
            assert j.conversations.set_runner_id(j.parent, "replacement-runner")
        app.state.turn_identities[j.child] = j.terminal.model_dump()
        app.state.on_proxy_stream_end(j.child, error=j.terminal.error)
        app.state.turn_identities.pop(j.child, None)
        if owner_moved:
            await poll_until_async(
                lambda: any(
                    body and body.get("type") == "external_session_status" for _url, body in posted
                ),
                timeout_s=5,
            )
            status = next(
                body
                for _url, body in posted
                if body and body.get("type") == "external_session_status"
            )
            assert status["data"]["turn_id"] == "root-turn"
            assert not case.runtime.chains
        else:
            await poll_until_async(lambda: bool(case.runtime.chains), timeout_s=5)
            [state] = case.runtime.chains.values()
            assert state.chain.root_turn_id == "root-turn" and state.chain.due_at == 160
        assert case.inbox.empty() and not case.entry.delivered


@pytest.mark.asyncio
async def test_sdk_ack_outage_retains_fields_for_the_result_pinning_consumer(db_uri, monkeypatch):
    from tests._helpers.polling import poll_until_async
    from tests.runner.helpers import NullServerClient
    from tests.runner.test_member_obligations import _build_app

    async with runtime_case(db_uri) as case:
        posted = []

        class Server(NullServerClient):
            async def post(self, url, **kwargs):
                body = kwargs.get("json") or {}
                posted.append(body)
                if body.get("type") == "external_conversation_item":
                    return httpx.Response(
                        200,
                        json={"item_id": "saved-error-item"},
                        request=httpx.Request("POST", "http://server" + url),
                    )
                return await super().post(url, **kwargs)

        async def unavailable(*_args, **_kwargs):
            raise httpx.ConnectError("temporary acknowledgement outage")

        app, _manager, _harness = _build_app(Server(), harness="claude-sdk")
        app.state.subagent_retry_runtime = case.runtime
        terminal = case.journal.terminal
        with monkeypatch.context() as outage:
            outage.setattr(case.runtime.client, "post", unavailable)
            app.state.turn_identities[terminal.child_id] = terminal.model_dump()
            app.state.on_proxy_stream_end(terminal.child_id, error=terminal.error)
            await poll_until_async(lambda: case.entry.recovery_result is not None, timeout_s=5)
        text = "Error: sub-agent turn failed: same provider failure"
        assert case.entry.recovery_result == ("failed", None, text)
        assert case.entry.terminal.input_id == terminal.input_id
        assert case.inbox.empty() and not case.entry.delivered
        del app.state.subagent_retry_runtime
        ack = app.state.mark_subagent_terminal_and_wake(
            terminal.child_id, status="failed", output=text, terminal=terminal
        )
        assert ack.delivered and case.inbox.get_nowait()["output"] == text
        await poll_until_async(
            lambda: case.entry.delivered_result_key == "saved-error-item", timeout_s=5
        )
        [saved] = [body for body in posted if body.get("type") == "external_conversation_item"]
        assert saved["data"]["item_data"]["message"] == text


@pytest.mark.asyncio
@pytest.mark.parametrize("finalized", [False, True])
async def test_old_chain_replay_preserves_a_newly_registered_dispatch(db_uri, finalized):
    async with runtime_case(db_uri, limit=1) as case:
        j, rt = case.journal, case.runtime
        ack = await rt.accept_terminal(j.terminal, "original failure")
        if finalized:
            case.now[0] = 160
            await rt._run(ack.chain_id)
            receipt = j.store.get_input(case.sent[0])
            await rt.accept_terminal(
                j.terminal.model_copy(
                    update={
                        "input_id": receipt.input_id,
                        "turn_id": receipt.turn_id,
                        "input_generation": receipt.input_generation,
                    }
                ),
                "exhausted failure",
            )
            assert j.store.get_final(ack.chain_id) is not None
        replacement = subagent_work.register_subagent_work(
            parent_session_id=j.parent,
            child_session_id=j.child,
            agent="worker",
            title="new dispatch",
            registered_by="sys_session_send",
        )
        before = (
            replacement.status,
            replacement.terminal,
            replacement.output,
            replacement.delivered,
            replacement.work_id,
        )
        await rt.accept_terminal(j.terminal, "late original failure")
        assert (
            replacement.status,
            replacement.terminal,
            replacement.output,
            replacement.delivered,
            replacement.work_id,
        ) == before
        assert replacement.status == "launching" and case.inbox.empty()
