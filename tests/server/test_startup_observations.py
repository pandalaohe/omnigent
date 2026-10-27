"""Runner cache reconciliation accepts only current, owned startup observations."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update
from starlette.datastructures import Headers

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import SqlUser, current_workspace_id
from omnigent.db.session_startup import SqlSessionStartup
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.host.frames import HostHelloFrame
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.server_config import StartupAdmissionPolicy
from omnigent.server.startup_admission import StartupAdmission
from omnigent.server.startup_inputs import StartupInputContext
from omnigent.server.startup_observations import reconcile_startup_observations
from omnigent.startup_protocol import StartupAttemptRef, StartupObservation
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore
from tests.server.test_host_registry import FakeWebSocket


def _startup(app, db_uri, *, owner=None, source="codex", mode="guarded", empty=False):
    conversations = SqlAlchemyConversationStore(db_uri)
    startups = SessionStartupStore(db_uri)
    hosts = app.state.host_registry
    host_id = uuid4().hex
    if owner is not None:
        with startups._session("seed_observation_owner") as db:
            db.add(SqlUser(id=owner, account_generation="old-generation"))
    with account_authority_scope(owner, "old-generation" if owner else None):
        conn = hosts.register(
            host_id,
            FakeWebSocket(),
            HostHelloFrame(
                version="test",
                frame_protocol_version=1,
                name="sample-host",
                connection_id="generation",
                capabilities=(
                    ["startup_queue_v1"]
                    if mode == "startup_only"
                    else ["startup_admission_v1", f"startup_receipts:{source}"]
                ),
            ),
            owner=owner,
        )
    conversation = conversations.create_conversation()
    conversations.set_host_id(conversation.id, host_id, "/opt/work/project")
    runner_id = token_bound_runner_id("sample-binding")
    conversations.set_runner_id(conversation.id, runner_id)
    item = None
    if not empty:
        [item] = conversations.append(
            conversation.id,
            [
                NewConversationItem(
                    type="message",
                    response_id=PENDING_INPUT_RESPONSE_ID,
                    status="in_progress",
                    data=MessageData(
                        role="user", content=[{"type": "input_text", "text": "work"}]
                    ),
                )
            ],
        )
    startups.configure(
        host_id,
        mode=mode,
        host_resident_limit=2 if mode == "guarded" else None,
        deployment_resident_limit=2 if mode == "guarded" else None,
    )
    startups.register_host_generation(host_id, conn.startup_generation)
    queued = startups.enqueue(
        owner_user_id="owner",
        host_id=host_id,
        session_id=conversation.id,
        operation_key="create",
        input_id=item.id if item is not None else None,
    )
    attempt = startups.reserve(host_id)
    assert startups.claim_boundary(
        queued.id,
        attempt.attempt_id,
        runtime_id="runtime",
        host_generation=conn.startup_generation,
        boundary="runner_prepare",
        claim_id="claim",
    )
    ref = StartupAttemptRef(
        startup_id=queued.id,
        attempt_id=attempt.attempt_id,
        input_id=item.id if item is not None else None,
        runtime_id="runtime",
        host_generation=conn.startup_generation,
        mode=mode,
        policy_revision=attempt.policy_revision if mode == "startup_only" else 0,
    )
    app.state.startup_store = startups
    return conversations, startups, hosts, conn, conversation, item, ref, runner_id


def _observation(ref, phase, *, source="codex"):
    return StartupObservation(
        attempt=ref,
        phase=phase,
        source=source,
        vendor_session_id="thread" if source in ("codex", "claude-sdk") else None,
        vendor_turn_id="turn" if source in ("codex", "claude-sdk") else None,
    )


@pytest.mark.asyncio
async def test_exemption_keeps_authenticated_runner_during_rebinding(
    app, client, db_uri, monkeypatch
):
    from omnigent.server.startup_observations import accept_observation

    conversations, startups, hosts, _, conv, _, ref, runner_id = _startup(
        app, db_uri, mode="startup_only"
    )
    admission = StartupAdmission(
        startups, conversations, hosts, StartupAdmissionPolicy(None, None, 2, "startup_only")
    )
    admission.bind(app)
    assert await accept_observation(
        app,
        conversations,
        conv.id,
        runner_id,
        StartupObservation(attempt=ref, phase="opening_settled", source="runner"),
    )
    original = admission.recreate_exempt

    async def rebind(conn, session_id, **kwargs):
        conversations.replace_runner_id(
            session_id, "different-runner", expected_runner_id=runner_id
        )
        return await original(conn, session_id, **kwargs)

    monkeypatch.setattr(admission, "recreate_exempt", rebind)
    response = await client.post(
        f"/v1/sessions/{conv.id}/startup/recreate-exemption",
        headers={RUNNER_TUNNEL_TOKEN_HEADER: "sample-binding"},
    )
    assert response.json() == {"exempt": False}
    await admission.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "policy", "mode", "connection", "cancel"])
async def test_queue_completion_requires_exact_authority_without_vendor_work(
    app, client, db_uri, fault
):
    from omnigent.server.startup_observations import accept_observation

    conversations, startups, hosts, conn, conv, item, ref, runner_id = _startup(
        app, db_uri, mode="startup_only"
    )
    if fault == "policy":
        ref = ref.model_copy(update={"policy_revision": ref.policy_revision + 1})
    if fault == "mode":
        ref = ref.model_copy(update={"mode": "guarded"})
        assert not await accept_observation(
            app,
            conversations,
            conv.id,
            runner_id,
            _observation(ref, "runtime_ready", source="runner"),
        )
        assert startups.get(ref.startup_id).occupied
        return
    if fault == "connection":
        replacement = hosts.register(conn.host_id, FakeWebSocket(), conn.hello, owner=None)
        startups.register_host_generation(conn.host_id, replacement.startup_generation)
    if fault == "cancel":
        startups.cancel(ref.startup_id)
    observation = StartupObservation(attempt=ref, phase="opening_settled", source="runner")
    assert await accept_observation(app, conversations, conv.id, runner_id, observation) == (
        fault is None
    )
    current = startups.get(ref.startup_id)
    assert current.occupied == (fault is not None)
    assert not current.input_accepted and current.proof_source is None
    assert conversations.get_item(conv.id, item.id).status == "in_progress"
    if fault is None:
        assert current.state == "opened"
        assert await accept_observation(app, conversations, conv.id, runner_id, observation)

        from omnigent.server.startup_inputs import ManagedStartupInputs

        inputs = ManagedStartupInputs(conversations, hosts)
        app.state.startup_inputs = inputs
        admission = StartupAdmission(
            startups, conversations, hosts, StartupAdmissionPolicy(None, None, 2, "startup_only")
        )
        admission.bind(app)
        inputs._deliver = AsyncMock(return_value="submitted")
        live = conversations.get_conversation(conv.id)
        assert await inputs.accepts(live, app)
        assert not await inputs._recover_session(app, conv.id, conn)
        inputs._deliver.assert_not_awaited()
        conversations.append(
            conv.id,
            [
                NewConversationItem(
                    type=item.type,
                    response_id="turn",
                    status="completed",
                    stable_id=item.id,
                    replaces_pending_id=item.id,
                    data=item.data,
                )
            ],
        )
        assert not await inputs.accepts(conversations.get_conversation(conv.id), app)
        conversations.append(
            conv.id,
            [
                NewConversationItem(
                    type="message",
                    response_id=PENDING_INPUT_RESPONSE_ID,
                    status="in_progress",
                    data=MessageData(
                        role="user", content=[{"type": "input_text", "text": "next"}]
                    ),
                )
            ],
        )
        assert await inputs._recover_session(app, conv.id, conn)
        await asyncio.sleep(0)
        inputs._deliver.assert_awaited_once()
        assert inputs._deliver.await_args.kwargs == {}
        await admission.shutdown()
        await inputs.shutdown()


@pytest.mark.asyncio
async def test_lost_lazy_empty_completion_reconciles_waiting_input_from_exact_runner_cache(
    app, client, db_uri, monkeypatch
):
    from omnigent.runner.startup_admission import StartupAdmission as RunnerAdmission

    conversations, startups, hosts, _conn, conv, _, ref, runner_id = _startup(
        app, db_uri, mode="startup_only", empty=True
    )
    async with httpx.AsyncClient(
        base_url="http://server.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
    ) as server:
        runner = RunnerAdmission(server, root_session_id=conv.id, root_attempt=ref)
        assert not await runner.opening_settled(conv.id, ref, waiting_input=True)
        cached = runner.observations_for(conv.id, ref)
        assert cached[0].reason == "waiting_input"
    async with httpx.AsyncClient(
        base_url="http://runner.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"observations": [x.model_dump() for x in cached]}
            )
        ),
    ) as routed:
        monkeypatch.setattr(
            app.state.runner_router,
            "client_for_existing_conversation",
            lambda _: SimpleNamespace(runner_id=runner_id, client=routed),
        )
        assert await reconcile_startup_observations(
            app, conversations, hosts, startups, startups.get(ref.startup_id)
        )
    current = startups.get(ref.startup_id)
    assert current.state == "waiting_input" and not current.occupied and not current.resident
    admission = StartupAdmission(
        startups, conversations, hosts, StartupAdmissionPolicy(None, None, 2, "startup_only")
    )
    assert not await admission.recreate_exempt(_conn, conv.id)
    await admission.shutdown()


@pytest.mark.asyncio
async def test_recreate_is_exempt_only_for_latest_settled_without_mutating_its_row(
    app, client, db_uri
):
    from omnigent.server.startup_observations import accept_observation

    conversations, startups, _hosts, conn, conv, _, ref, runner_id = _startup(
        app, db_uri, mode="startup_only"
    )
    admission = StartupAdmission(
        startups,
        conversations,
        app.state.host_registry,
        StartupAdmissionPolicy(None, None, 2, "startup_only"),
    )
    admission.bind(app)
    assert not await admission.recreate_exempt(conn, conv.id)
    assert await accept_observation(
        app,
        conversations,
        conv.id,
        runner_id,
        StartupObservation(attempt=ref, phase="opening_settled", source="runner"),
    )
    settled = startups.get(ref.startup_id)
    route = f"/v1/sessions/{conv.id}/startup/recreate-exemption"
    assert (await client.post(route)).status_code == 403
    headers = {RUNNER_TUNNEL_TOKEN_HEADER: "sample-binding"}
    assert (await client.post(route, headers=headers)).json() == {"exempt": True}
    assert await admission.recreate_exempt(conn, conv.id)
    assert await admission.admit_launch(conn, conv.id) is None
    assert not startups.claim_boundary(
        ref.startup_id,
        ref.attempt_id,
        runtime_id=ref.runtime_id,
        host_generation=ref.host_generation,
        boundary="native_prepare",
        claim_id="old-permit",
    )
    assert startups.get(ref.startup_id) == settled
    for _ in range(3):
        startups.enqueue(
            owner_user_id="owner",
            host_id=conn.host_id,
            session_id=uuid4().hex,
            operation_key=uuid4().hex,
            input_id=None,
        )
    assert startups.reserve(conn.host_id) is not None
    assert startups.reserve(conn.host_id) is not None
    assert startups.reserve(conn.host_id) is None
    assert await admission.recreate_exempt(conn, conv.id)
    assert startups.get(ref.startup_id) == settled
    startups.enqueue(
        owner_user_id="owner",
        host_id=conn.host_id,
        session_id=conv.id,
        operation_key="new-first-input",
        input_id=None,
        runtime_generation=2,
    )
    assert not await admission.recreate_exempt(conn, conv.id)
    assert startups.get(ref.startup_id) == settled
    await admission.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["codex", "claude-sdk"])
async def test_lost_receipts_reconcile_from_exact_runner_cache_once(
    app, client, db_uri, monkeypatch, source
):
    conversations, startups, hosts, conn, conv, item, ref, runner_id = _startup(
        app, db_uri, source=source
    )
    phases = (
        "input_dispatched",
        "runtime_ready",
        "tools_ready",
        "input_accepted",
        "work_started",
    )
    observations = [_observation(ref, phase, source=source).model_dump() for phase in phases]
    requests = []

    def runner_handler(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"observations": observations})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_handler), base_url="http://runner"
    ) as runner_client:
        monkeypatch.setattr(
            app.state.runner_router,
            "client_for_existing_conversation",
            lambda _session_id: SimpleNamespace(runner_id=runner_id, client=runner_client),
        )
        admission = StartupAdmission(
            startups, conversations, hosts, StartupAdmissionPolicy(2, 2, 2)
        )
        admission.bind(app)
        assert not await admission.recover_session(conv.id, conn)
        first_dispatch = startups.get(ref.startup_id).first_dispatch_at
        assert first_dispatch is not None
        assert startups.get(ref.startup_id).state == "working"
        consumed = conversations.get_item(conv.id, item.id)
        assert consumed.response_id == f"turn_{ref.attempt_id}"
        assert not conversations.list_pending_inputs(conv.id)
        assert requests == [(f"/v1/sessions/{conv.id}/startup/observations", ref.model_dump())]
        assert await reconcile_startup_observations(
            app, conversations, hosts, startups, startups.get(ref.startup_id)
        )
        assert startups.get(ref.startup_id).first_dispatch_at == first_dispatch
        assert conversations.get_item(conv.id, item.id).response_id == consumed.response_id
        await admission.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["forged_source", "stale_connection", "wrong_attempt"])
async def test_reconciliation_rejects_unowned_observations(
    app, client, db_uri, monkeypatch, fault
):
    conversations, startups, hosts, conn, conv, item, ref, runner_id = _startup(app, db_uri)
    observation = _observation(ref, "input_dispatched")

    def runner_handler(_request):
        if fault == "stale_connection":
            replacement = hosts.register(conn.host_id, FakeWebSocket(), conn.hello, owner=None)
            startups.register_host_generation(conn.host_id, replacement.startup_generation)
        if fault == "forged_source":
            body = observation.model_copy(update={"source": "codex-native"}).model_dump()
        elif fault == "wrong_attempt":
            wrong = ref.model_copy(update={"attempt_id": "f" * 32})
            body = observation.model_copy(update={"attempt": wrong}).model_dump()
        else:
            body = observation.model_dump()
        return httpx.Response(200, json={"observations": [body]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_handler), base_url="http://runner"
    ) as runner_client:
        monkeypatch.setattr(
            app.state.runner_router,
            "client_for_existing_conversation",
            lambda _session_id: SimpleNamespace(runner_id=runner_id, client=runner_client),
        )
        assert not await reconcile_startup_observations(
            app, conversations, hosts, startups, startups.get(ref.startup_id)
        )
    assert startups.get(ref.startup_id).first_dispatch_at is None
    assert conversations.get_item(conv.id, item.id).response_id == PENDING_INPUT_RESPONSE_ID


@pytest.mark.asyncio
async def test_post_receipt_binds_first_dispatch_and_rejects_other_runner(app, client, db_uri):
    _conversations, startups, _hosts, _conn, conv, _item, ref, _runner_id = _startup(app, db_uri)
    url = f"/v1/sessions/{conv.id}/startup/receipts"
    body = _observation(ref, "input_dispatched").model_dump()
    assert (await client.post(url, json=body)).status_code == 403
    assert (
        await client.post(url, json=body, headers={RUNNER_TUNNEL_TOKEN_HEADER: "wrong"})
    ).status_code == 403
    assert (
        await client.post(url, json=body, headers={RUNNER_TUNNEL_TOKEN_HEADER: "sample-binding"})
    ).json() == {"accepted": True}
    assert startups.get(ref.startup_id).first_dispatch_at is not None


@pytest.mark.asyncio
async def test_reconciliation_rejects_rotated_host_account_after_query(
    app, client, db_uri, monkeypatch
):
    conversations, startups, hosts, conn, conv, item, ref, runner_id = _startup(
        app, db_uri, owner="alice"
    )
    assert conn.account_generation == "old-generation"

    def runner_handler(_request):
        with startups._session("rotate_observation_owner") as db:
            db.execute(
                update(SqlUser)
                .where(SqlUser.workspace_id == current_workspace_id(), SqlUser.id == "alice")
                .values(account_generation="new-generation")
            )
        return httpx.Response(
            200,
            json={"observations": [_observation(ref, "input_dispatched").model_dump()]},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_handler), base_url="http://runner"
    ) as runner_client:
        monkeypatch.setattr(
            app.state.runner_router,
            "client_for_existing_conversation",
            lambda _session_id: SimpleNamespace(runner_id=runner_id, client=runner_client),
        )
        assert not await reconcile_startup_observations(
            app, conversations, hosts, startups, startups.get(ref.startup_id)
        )
    snapshot = startups.get(ref.startup_id)
    assert snapshot.first_dispatch_at is None
    assert snapshot.proof_source is None
    assert snapshot.occupied
    assert conversations.get_item(conv.id, item.id).response_id == PENDING_INPUT_RESPONSE_ID


@pytest.mark.asyncio
async def test_direct_receipts_reject_rotated_host_account(app, client, db_uri):
    conversations, startups, _hosts, _conn, conv, item, ref, _runner_id = _startup(
        app, db_uri, owner="alice"
    )
    with startups._session("rotate_direct_receipt_owner") as db:
        db.execute(
            update(SqlUser)
            .where(SqlUser.workspace_id == current_workspace_id(), SqlUser.id == "alice")
            .values(account_generation="new-generation")
        )
    url = f"/v1/sessions/{conv.id}/startup/receipts"
    for phase in ("input_dispatched", "runtime_ready"):
        response = await client.post(
            url,
            json=_observation(
                ref, phase, source="codex" if phase == "input_dispatched" else "runner"
            ).model_dump(),
            headers={RUNNER_TUNNEL_TOKEN_HEADER: "sample-binding"},
        )
        assert response.json() == {"accepted": False}
    snapshot = startups.get(ref.startup_id)
    assert snapshot.proof_source is None
    assert snapshot.first_dispatch_at is None
    assert snapshot.occupied
    assert conversations.get_item(conv.id, item.id).response_id == PENDING_INPUT_RESPONSE_ID


@pytest.mark.asyncio
async def test_runner_claim_rejects_rotated_host_account(app, client, db_uri):
    _conversations, startups, hosts, _conn, conv, _item, ref, _runner_id = _startup(
        app, db_uri, owner="alice"
    )
    hosts.launch_authorizer = Mock()
    with startups._session("rotate_runner_claim_owner") as db:
        db.execute(
            update(SqlUser)
            .where(SqlUser.workspace_id == current_workspace_id(), SqlUser.id == "alice")
            .values(account_generation="new-generation")
        )
    response = await client.post(
        f"/v1/sessions/{conv.id}/startup/claims",
        json={"attempt": ref.model_dump(), "boundary": "executor_prepare", "claim_id": "cold"},
        headers={RUNNER_TUNNEL_TOKEN_HEADER: "sample-binding"},
    )
    assert response.json() == {"granted": False}
    with startups._session("check_rejected_runner_claim") as db:
        row = db.get(SqlSessionStartup, (current_workspace_id(), ref.startup_id))
        assert "executor_prepare" not in row.cold_claims


@pytest.mark.asyncio
async def test_saved_input_claim_rejects_revoked_host_account(app, client, db_uri):
    conversations, startups, hosts, _conn, conv, item, ref, _runner = _startup(
        app, db_uri, owner="alice"
    )
    app.state.startup_admission = SimpleNamespace(hosts=hosts)
    with startups._session("rotate_saved_input_claim_owner") as db:
        db.execute(
            update(SqlUser)
            .where(SqlUser.workspace_id == current_workspace_id(), SqlUser.id == "alice")
            .values(account_generation="new-generation")
        )
    context = StartupInputContext(app, Headers(), "alice", item.id, startup=ref)
    with pytest.raises(OmnigentError, match="account authority has been revoked"):
        await context.claim(conversations, conv.id)
    assert conversations.get_item(conv.id, item.id).status == "in_progress"
    with startups._session("read_rejected_saved_input_claim") as db:
        row = db.get(SqlSessionStartup, (current_workspace_id(), ref.startup_id))
        assert "input_dispatch" not in row.cold_claims
