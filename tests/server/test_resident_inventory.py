"""Incomplete owner evidence cannot become startup or resident permission."""

import asyncio
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import SqlUser
from omnigent.db.session_startup import SqlStartupBudget
from omnigent.entities.session_startup import StartupReceipt
from omnigent.host.frames import HostHelloFrame
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame, ResponseHeadFrame
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.host_registry import HostRegistry
from omnigent.server.resident_inventory import retain_unknown_owner_inventory
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.server.server_config import StartupAdmissionPolicy
from omnigent.server.startup_admission import StartupAdmission
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient
from tests.server.test_host_registry import FakeWebSocket


@pytest.mark.asyncio
async def test_admission_cannot_bypass_missing_owner_inventory_capability(db_uri):
    conversations, startups = SqlAlchemyConversationStore(db_uri), SessionStartupStore(db_uri)
    hosts, host = HostRegistry(), uuid4().hex
    conn = hosts.register(
        host,
        FakeWebSocket(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="sample-host",
            capabilities=["startup_admission_v1", "startup_receipts:codex"],
        ),
        owner=None,
    )
    startups.register_host_generation(host, conn.startup_generation)
    conversation = conversations.create_conversation()
    conversations.set_host_id(conversation.id, host, "/opt/work/project")
    admission = StartupAdmission(startups, conversations, hosts, StartupAdmissionPolicy(4, 4, 2))
    admission.bind(SimpleNamespace(state=SimpleNamespace(startup_inputs=None)))
    admission.resolve_harness = AsyncMock(return_value="codex")
    queued = await admission.queue_session(conn, conversation.id)
    try:
        assert not await admission._eligible(queued)
        await admission.coordinator.advance(host)
        await admission.coordinator.drain()
        current = startups.get(queued.id)
        assert current.state == "queued" and not current.resident and not current.occupied
    finally:
        await admission.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminals", [None, []])
async def test_partial_reader_veto_survives_reorder_reload_and_reconnect(
    db_uri, monkeypatch, terminals
):
    conversations, startups = SqlAlchemyConversationStore(db_uri), SessionStartupStore(db_uri)
    hosts, host = HostRegistry(), uuid4().hex
    conn = hosts.register(
        host,
        FakeWebSocket(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="sample-host",
            capabilities=["startup_admission_v1", "resident_inventory_v1"],
        ),
        owner=None,
    )
    startups.configure(host, host_resident_limit=10, deployment_resident_limit=10)
    startups.register_host_generation(host, conn.startup_generation)
    conversation = conversations.create_conversation()
    conversations.set_host_id(conversation.id, host, "/opt/work/project")
    conversations.set_runner_id(conversation.id, "runner")
    queued = startups.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=conversation.id,
        operation_key="existing",
        input_id=None,
    )
    attempt = startups.reserve(host)
    assert startups.claim_boundary(
        queued.id,
        attempt.attempt_id,
        runtime_id="existing",
        host_generation=conn.startup_generation,
        boundary="runner_prepare",
        claim_id="owned",
    )
    requests = []
    first_read, release_first = asyncio.Event(), asyncio.Event()

    async def read(request):
        requests.append(request.url.params["read_id"])
        if len(requests) == 1:
            first_read.set()
            await release_first.wait()

    monkeypatch.setattr("omnigent.runner.native.orchestration._AUTO_CODEX_APP_SERVERS", {})
    monkeypatch.setattr("omnigent.runner.native.orchestration._AUTO_OPENCODE_SERVERS", {})
    monkeypatch.setattr("omnigent.runner.native.orchestration._CLOSING_NATIVE_BACKENDS", {})
    resources = SessionResourceRegistry(TerminalRegistry() if terminals is not None else None)
    runner_app = create_runner_app(server_client=NullServerClient(), resource_registry=resources)

    tunnels = TunnelRegistry()
    tunnels.register("runner", FakeWebSocket(), HelloFrame("test", 1))
    initializer = RunnerSessionInitializer(tunnels, server_version="test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runner_app),
        base_url="http://runner",
        event_hooks={"request": [read]},
    ) as runner:
        router = Mock()
        router.runner_has_capability.return_value = True
        router.client_for_existing_conversation.return_value = SimpleNamespace(
            runner_id="runner", client=runner
        )
        app = SimpleNamespace(
            state=SimpleNamespace(
                runner_router=router, runner_session_initializer=initializer, startup_inputs=None
            )
        )
        first = asyncio.create_task(
            retain_unknown_owner_inventory(
                app, conversations, hosts, startups, conn, conversation.id
            )
        )
        await asyncio.wait_for(first_read.wait(), 2)
        try:
            assert await retain_unknown_owner_inventory(
                app, conversations, hosts, startups, conn, conversation.id
            )
        finally:
            release_first.set()
            assert await asyncio.wait_for(first, 2)
        # A duplicate read is monotonic too; neither delivery order releases usage.
        assert await retain_unknown_owner_inventory(
            app, conversations, hosts, startups, conn, conversation.id
        )
        admission = StartupAdmission(
            startups, conversations, hosts, StartupAdmissionPolicy(10, 10, 2)
        )
        admission.bind(app)
        try:
            assert not await admission._eligible(queued)
        finally:
            await admission.shutdown()
    assert requests[0] != requests[1]
    retained = startups.get(queued.id)
    assert retained.resident and retained.occupied
    assert retained.attempt_id == attempt.attempt_id and retained.runtime_id == "existing"
    assert not startups.claim(queued.id, attempt.attempt_id, runtime_id="existing")
    assert not startups.claim_boundary(
        queued.id,
        attempt.attempt_id,
        runtime_id="existing",
        host_generation=conn.startup_generation,
        boundary="executor_prepare",
        claim_id="late",
    )
    # Positive owner observations and an owned stop still work under the veto.
    for phase in ("runtime_ready", "tools_ready"):
        assert startups.observe(
            StartupReceipt(
                startup_id=queued.id,
                attempt_id=attempt.attempt_id,
                runtime_id="existing",
                source="runner",
                phase=phase,
            )
        )
    assert startups.get(queued.id).state == "ready_empty"
    startups.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="new",
        input_id=None,
    )
    replacement = SessionStartupStore(db_uri)
    assert replacement.reserve(host) is None
    replacement.register_host_generation(host, "reconnected")
    replacement.configure(host, host_resident_limit=20, deployment_resident_limit=20)
    assert replacement.reserve(host) is None
    assert replacement.confirm_stopped(queued.id, attempt.attempt_id)
    assert not replacement.get(queued.id).resident
    assert replacement.reserve(host) is None
    # An unknown host also leaves the deployment budget unknown.
    other = uuid4().hex
    replacement.configure(other, host_resident_limit=20, deployment_resident_limit=20)
    replacement.enqueue(
        owner_user_id="owner",
        host_id=other,
        session_id=uuid4().hex,
        operation_key="other-host",
        input_id=None,
    )
    assert replacement.reserve(other) is None
    with replacement._session("inspect_unknown_veto") as db:
        budget = db.get(SqlStartupBudget, f"0:{host}")
        assert budget.resident_inventory_unknown and not budget.fenced and budget.recovery is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "race",
    [
        "host",
        "runner",
        "binding",
        "account",
        "read_id",
        "queued_write",
        "commit_retry",
    ],
)
async def test_stale_owner_response_cannot_write_inventory_veto(db_uri, monkeypatch, race):
    conversations, startups = SqlAlchemyConversationStore(db_uri), SessionStartupStore(db_uri)
    hosts, host = HostRegistry(), uuid4().hex
    with startups._session("seed_inventory_owner") as db:
        db.add(SqlUser(id="alice", account_generation="account-one"))
    with account_authority_scope("alice", "account-one"):
        conn = hosts.register(
            host,
            FakeWebSocket(),
            HostHelloFrame(
                version="test",
                frame_protocol_version=1,
                name="sample-host",
                capabilities=["resident_inventory_v1"],
            ),
            owner="alice",
        )
    startups.configure(host, host_resident_limit=10, deployment_resident_limit=10)
    startups.register_host_generation(host, conn.startup_generation)
    conversation = conversations.create_conversation()
    conversations.set_host_id(conversation.id, host, "/opt/work/project")
    conversations.set_runner_id(conversation.id, "runner")
    tunnels = TunnelRegistry()
    tunnels.register("runner", FakeWebSocket(), HelloFrame("test", 1))
    initializer = RunnerSessionInitializer(tunnels, server_version="test")
    write_waiting, release_write = threading.Event(), threading.Event()
    if race == "queued_write":
        budgets = startups._budgets

        def paused_budgets(db, host_id):
            locked = budgets(db, host_id)
            write_waiting.set()
            assert release_write.wait(5), "test did not release the locked transaction"
            return locked

        monkeypatch.setattr(startups, "_budgets", paused_budgets)
    elif race == "commit_retry":
        commit = Session.commit
        failure_pending = True

        class SerializationFailure(Exception):
            sqlstate = "40001"

        def fail_first_inventory_commit(db):
            nonlocal failure_pending
            if failure_pending and any(
                isinstance(row, SqlStartupBudget) and row.resident_inventory_unknown
                for row in db.dirty
            ):
                failure_pending = False
                raise DBAPIError("commit", None, SerializationFailure())
            return commit(db)

        def pause_before_retry(_operation, outcome):
            assert outcome == "scheduled"
            write_waiting.set()
            assert release_write.wait(5), "test did not release the transaction retry"

        monkeypatch.setattr(Session, "commit", fail_first_inventory_commit)
        monkeypatch.setattr("omnigent.db.utils.is_cockroachdb", lambda _dialect: True)
        monkeypatch.setattr("omnigent.db.utils.record_transaction_retry", pause_before_retry)

    async def read(request):
        if race == "host":
            new = hosts.register(host, FakeWebSocket(), conn.hello, owner="alice")
            startups.register_host_generation(host, new.startup_generation)
        elif race == "runner":
            tunnels.register("runner", FakeWebSocket(), HelloFrame("test", 1))
        elif race == "binding":
            conversations.replace_runner_id(conversation.id, "replacement")
        elif race == "account":
            with startups._session("replace_inventory_owner") as db:
                db.execute(
                    update(SqlUser)
                    .where(SqlUser.id == "alice")
                    .values(account_generation="account-two")
                )
        return httpx.Response(
            200,
            json={
                "read_id": "older-read" if race == "read_id" else request.url.params["read_id"],
                "native_backends": {"owners": []},
                "terminals": [],
                "executor_contexts_complete": False,
                "external_owners_complete": False,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(read), base_url="http://runner"
    ) as runner:
        router = Mock()
        router.runner_has_capability.return_value = True
        router.client_for_existing_conversation.return_value = SimpleNamespace(
            runner_id="runner", client=runner
        )
        app = SimpleNamespace(
            state=SimpleNamespace(runner_router=router, runner_session_initializer=initializer)
        )
        pending = asyncio.create_task(
            retain_unknown_owner_inventory(
                app, conversations, hosts, startups, conn, conversation.id
            )
        )
        if race in ("queued_write", "commit_retry"):

            async def reconnect():
                tunnels.register("runner", FakeWebSocket(), HelloFrame("test", 1))

            try:
                assert await asyncio.to_thread(write_waiting.wait, 5)
                # A registry lock held while waiting for DB locks must not block reconnect.
                await asyncio.wait_for(asyncio.to_thread(asyncio.run, reconnect()), 2)
            finally:
                release_write.set()
        assert not await asyncio.wait_for(pending, 5)
    with startups._session("inspect_rejected_inventory") as db:
        assert not db.get(SqlStartupBudget, f"0:{host}").resident_inventory_unknown


@pytest.mark.asyncio
async def test_unknown_inventory_preserves_reserved_resident_reuse_dispatch(db_uri):
    store, conversations = SessionStartupStore(db_uri), SqlAlchemyConversationStore(db_uri)
    host = uuid4().hex
    store.configure(host, host_resident_limit=2, deployment_resident_limit=2)
    store.register_host_generation(host, "host-generation")
    conversation = conversations.create_conversation()
    conversations.set_host_id(conversation.id, host, "/opt/work/project")
    conversations.set_runner_id(conversation.id, "runner")
    empty = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=conversation.id,
        operation_key="empty",
        input_id=None,
    )
    reserved = store.reserve(host)
    assert store.claim_boundary(
        empty.id,
        reserved.attempt_id,
        runtime_id="warm-context",
        host_generation="host-generation",
        boundary="runner_prepare",
        claim_id="prepare",
    )
    for phase in ("runtime_ready", "tools_ready"):
        assert store.observe(
            StartupReceipt(
                startup_id=empty.id,
                attempt_id=reserved.attempt_id,
                runtime_id="warm-context",
                source="runner",
                phase=phase,
            )
        )
    reused = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=conversation.id,
        operation_key="first-input",
        input_id=uuid4().hex,
        runtime_generation=2,
        reuse_startup_id=empty.id,
    )
    dispatch = store.reserve(host)
    assert dispatch.id == reused.id
    cold = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="independent-cold",
        input_id=None,
    )
    cold_attempt = store.reserve(host)
    assert cold_attempt.id == cold.id
    tunnels = TunnelRegistry()
    runner = tunnels.register("runner", FakeWebSocket(), HelloFrame("test", 1))
    # The veto arrives after this resident reuse was already reserved.
    assert store.note_unknown_resident_inventory(
        host,
        host_generation="host-generation",
        session_id=conversation.id,
        expected_runner_id="runner",
        account=None,
        runner_generation=tunnels.generation_guard("runner", runner.generation),
    )
    assert not store.claim_boundary(
        cold.id,
        cold_attempt.attempt_id,
        runtime_id="cold-context",
        host_generation="host-generation",
        boundary="executor_prepare",
        claim_id="new-cold",
    )
    assert not store.claim_boundary(
        reused.id,
        dispatch.attempt_id,
        runtime_id="warm-context",
        host_generation="host-generation",
        boundary="executor_prepare",
        claim_id="cold",
    )
    assert store.claim_boundary(
        reused.id,
        dispatch.attempt_id,
        runtime_id="warm-context",
        host_generation="host-generation",
        boundary="input_dispatch",
        claim_id="warm",
    )
    assert store.get(empty.id).resident and store.get(reused.id).resident


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", [None, "register", "deregister", "close", "cancel"])
async def test_inventory_commit_keeps_unrelated_traffic_and_eventloop_moving(
    db_uri, monkeypatch, lifecycle
):
    store, conversations = SessionStartupStore(db_uri), SqlAlchemyConversationStore(db_uri)
    host = uuid4().hex
    store.configure(host, host_resident_limit=2, deployment_resident_limit=2)
    store.register_host_generation(host, "generation")
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, host, "/opt/work/project")
    conversations.set_runner_id(conv.id, "target")
    tunnels = TunnelRegistry()
    target = tunnels.register("target", FakeWebSocket(), HelloFrame("test", 1))
    other = tunnels.register("other", FakeWebSocket(), HelloFrame("test", 1))
    entered, release, ticked, progressed = (threading.Event() for _ in range(4))
    observations = []
    commit = Session.commit

    def paused_commit(db):
        if any(
            isinstance(row, SqlStartupBudget) and row.resident_inventory_unknown
            for row in db.dirty
        ):
            entered.set()
            assert release.wait(5), "test did not release the real commit"
        return commit(db)

    def watchdog():
        observations.append((progressed.wait(2), ticked.is_set()))
        release.set()

    monkeypatch.setattr(Session, "commit", paused_commit)
    write = asyncio.create_task(
        asyncio.to_thread(
            store.note_unknown_resident_inventory,
            host,
            host_generation="generation",
            session_id=conv.id,
            expected_runner_id="target",
            account=None,
            runner_generation=tunnels.generation_guard("target", target.generation),
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    watcher = threading.Thread(target=watchdog, daemon=True)
    watcher.start()
    waiter = None
    try:
        asyncio.get_running_loop().call_soon(ticked.set)
        if lifecycle in ("register", "cancel"):
            waiter = asyncio.create_task(
                tunnels.register_async("target", FakeWebSocket(), HelloFrame("test", 1))
            )
        elif lifecycle == "deregister":
            waiter = asyncio.create_task(tunnels.deregister_async("target", target))
        elif lifecycle == "close":
            waiter = asyncio.create_task(
                tunnels.record_close_async(target, code=4003, reason="test-close")
            )
        if waiter is not None:
            await asyncio.sleep(0)
            assert not waiter.done(), "target lifecycle bypassed the pending commit"
            if lifecycle == "cancel":
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
        assert tunnels.mark_frame_seen(other)
        assert tunnels.get("other") is other
        await tunnels.send_text(other, "warm-control")
        request = tunnels.open_request("other", "response")
        assert tunnels.route_response_frame("other", ResponseHeadFrame("response", 200, []))
        assert (await asyncio.wait_for(request.head_future, 1)).status == 200
        tunnels.close_request("other", "response")
        independent = await tunnels.register_async(
            "independent", FakeWebSocket(), HelloFrame("test", 1)
        )
        assert independent.loop is asyncio.get_running_loop()
        progressed.set()
    finally:
        release.set()
        await asyncio.to_thread(watcher.join, 3)
        assert await asyncio.wait_for(write, 5)
    assert observations == [(True, True)], (
        "unrelated receive/send/response routing or loop tick waited for commit"
    )
    if waiter is not None and lifecycle != "cancel":
        result = await asyncio.wait_for(waiter, 2)
        if lifecycle == "register":
            assert result.generation != target.generation
            assert result.loop is asyncio.get_running_loop()
        elif lifecycle == "deregister":
            assert result is target and tunnels.get("target") is None
        else:
            assert target.close_code == 4003
    if lifecycle == "cancel":
        assert (
            await tunnels.register_async("target", FakeWebSocket(), HelloFrame("test", 1))
        ).generation != target.generation
    assert not tunnels._lifecycle_locks


@pytest.fixture
async def host_owner_control(monkeypatch):
    from omnigent.host.connect import HostProcess, _RunnerHandle
    from omnigent.server.routes.host_tunnel import _receive_loop

    class ServerSocket(FakeWebSocket):
        def __init__(self):
            super().__init__()
            self.inbox = asyncio.Queue()

        async def receive(self):
            return await self.inbox.get()

    server_socket = ServerSocket()
    hosts = HostRegistry()
    hello = HostHelloFrame(
        "test",
        1,
        "sample-host",
        capabilities=["resident_inventory_v1"],
        connection_id="connection-one",
    )
    conn = hosts.register(
        uuid4().hex, server_socket, hello, owner=None, startup_generation="server-generation"
    )
    sent = []
    fault = SimpleNamespace(mutate=None)

    class HostSocket:
        async def send(self, raw):
            sent.append(raw)
            if fault.mutate is not None:
                raw = fault.mutate(raw)
            await server_socket.inbox.put({"type": "websocket.receive", "text": raw})

    socket = HostSocket()
    host = object.__new__(HostProcess)
    host._ws = socket
    host._startup_generation = "connection-one"
    host._runner_lifecycle_lock = asyncio.Lock()
    host._runner_stop_tasks = set()
    host._pending_runner_launches = {}
    process = Mock()
    process.poll.side_effect = AssertionError("must not probe")
    process.kill.side_effect = AssertionError("must not stop")
    handle = _RunnerHandle(process, Path("/opt/work/runner.log"), session_id="session-one")
    handle.incarnation_id = "handle-one"
    host._runners = {"runner-one": handle}

    async def send(ws, raw):
        await ws.send(raw)

    host._send_text = send
    for name in (
        "_alive_runner_ids",
        "_trigger_maintenance",
        "_cleanup_runners",
        "_handle_launch",
    ):
        monkeypatch.setattr(host, name, Mock(side_effect=AssertionError("read must be pure")))
    receiver = asyncio.create_task(
        _receive_loop(
            server_socket,
            conn,
            conn.host_id,
            Mock(),
            hosts,
            None,
            None,
            None,
            None,
            None,
            None,
        )
    )
    try:
        yield hosts, conn, host, socket, sent, fault, process
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        hosts.deregister(conn.host_id, conn=conn)


async def test_host_owner_inventory_control_round_trip_is_pure(host_owner_control):
    from dataclasses import asdict

    from omnigent.server.resident_inventory import read_host_owner_inventory

    hosts, conn, host, socket, sent, _fault, process = host_owner_control
    read = asyncio.create_task(read_host_owner_inventory(hosts, conn))
    try:
        outbound = await asyncio.wait_for(conn.outbound_queue.get(), timeout=2)
        await host._handle_raw_message(socket, outbound.data)
        result = await asyncio.wait_for(read, timeout=2)
        assert result.connection_id == "connection-one"
        assert result.host_generation == "server-generation"
        inventory = asdict(result.inventory)
        token = inventory.pop("registration_token")
        assert len(token) == 64
        assert inventory == {
            "runners": (
                {
                    "runner_id": "runner-one",
                    "incarnation_id": "handle-one",
                    "session_id": "session-one",
                    "stop_requested": False,
                },
            ),
            "pending_runner_ids": (),
            "lifecycle_busy": False,
            "cleanup_pending": False,
            "aliases_complete": False,
            "context_coverage_complete": False,
        }
        assert len(sent) == 1
        assert not conn.pending_owner_inventory
        assert host._runners["runner-one"].incarnation_id == "handle-one"
        process.poll.assert_not_called()
        process.kill.assert_not_called()
    finally:
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)


async def _exchange_host_owner_control(control, **kwargs):
    from omnigent.server.resident_inventory import read_host_owner_inventory

    hosts, conn, host, socket, _sent, _fault, _process = control
    read = asyncio.create_task(read_host_owner_inventory(hosts, conn, **kwargs))
    try:
        outbound = await asyncio.wait_for(conn.outbound_queue.get(), timeout=2)
        await host._handle_raw_message(socket, outbound.data)
        return await asyncio.wait_for(read, timeout=2)
    finally:
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)


async def test_host_owner_inventory_revalidates_registration_token_and_handle_lifetime(
    host_owner_control,
):
    from dataclasses import FrozenInstanceError

    from omnigent.host.connect import _RunnerHandle

    _hosts, _conn, host, _socket, _sent, _fault, process = host_owner_control
    first = await _exchange_host_owner_control(host_owner_control)
    token = first.inventory.registration_token
    same = await _exchange_host_owner_control(
        host_owner_control, expected_registration_token=token
    )
    assert same.inventory == first.inventory
    with pytest.raises(FrozenInstanceError):
        first.inventory.cleanup_pending = True
    replacement = _RunnerHandle(process, Path("/opt/work/runner.log"), session_id="session-one")
    another = _RunnerHandle(process, Path("/opt/work/runner.log"), session_id="session-one")
    assert replacement.incarnation_id != another.incarnation_id
    assert replacement.incarnation_id != "handle-one"
    host._runners["runner-one"] = replacement
    assert (
        await _exchange_host_owner_control(host_owner_control, expected_registration_token=token)
        is None
    )
    current = await _exchange_host_owner_control(host_owner_control)
    assert current.inventory.registration_token != token
    assert current.inventory.runners[0].incarnation_id == replacement.incarnation_id
    assert first.inventory.runners[0].incarnation_id == "handle-one"
    host._runners.clear()
    empty = await _exchange_host_owner_control(host_owner_control)
    assert empty.inventory.runners == ()
    assert not empty.inventory.aliases_complete and not empty.inventory.context_coverage_complete


async def test_host_owner_inventory_keeps_pending_cleanup_unknown_without_waiting(
    host_owner_control,
):
    _hosts, _conn, host, _socket, _sent, _fault, process = host_owner_control
    pending = asyncio.get_running_loop().create_future()
    host._pending_runner_launches["runner-pending"] = {pending}
    cleanup = asyncio.create_task(asyncio.Event().wait())
    host._runner_stop_tasks.add(cleanup)
    await host._runner_lifecycle_lock.acquire()
    try:
        result = await _exchange_host_owner_control(host_owner_control)
        assert result.inventory.pending_runner_ids == ("runner-pending",)
        assert result.inventory.cleanup_pending and result.inventory.lifecycle_busy
        assert not result.inventory.context_coverage_complete
        assert not cleanup.done() and not pending.done()
        assert host._runner_lifecycle_lock.locked()
        process.poll.assert_not_called()
    finally:
        host._runner_lifecycle_lock.release()
        pending.cancel()
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)


@pytest.mark.parametrize(
    "race",
    [
        "disconnect",
        "replacement",
        "account",
        "generation",
        "connection",
        "socket",
        "host-socket",
        "host-generation",
    ],
)
async def test_host_owner_inventory_rejects_stale_control_replies(host_owner_control, race):
    from omnigent.server.resident_inventory import read_host_owner_inventory

    hosts, conn, host, socket, _sent, _fault, _process = host_owner_control
    read = asyncio.create_task(read_host_owner_inventory(hosts, conn))
    try:
        outbound = await asyncio.wait_for(conn.outbound_queue.get(), timeout=2)
        if race == "disconnect":
            hosts.deregister(conn.host_id, conn=conn)
        elif race == "replacement":
            hosts.register(conn.host_id, FakeWebSocket(), conn.hello, owner=None)
        elif race == "account":
            conn.account_generation = "new-account"
        elif race == "generation":
            conn.startup_generation = "new-host-generation"
        elif race == "connection":
            conn.hello.connection_id = "new-connection"
        elif race == "socket":
            conn.ws = FakeWebSocket()
        elif race == "host-socket":
            host._ws = object()
        else:
            host._startup_generation = "new-connection"
        await host._handle_raw_message(socket, outbound.data)
        assert await asyncio.wait_for(read, timeout=2) is None
        assert not conn.pending_owner_inventory
    finally:
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)


@pytest.mark.parametrize(
    "reason", ["unsupported", "missing-connection", "missing-host", "invalid-timeout"]
)
async def test_host_owner_inventory_compatibility_unknown_never_enqueues(
    host_owner_control, reason
):
    from omnigent.server.resident_inventory import read_host_owner_inventory

    hosts, conn, _host, _socket, sent, _fault, _process = host_owner_control
    if reason == "unsupported":
        conn.hello.capabilities.clear()
    elif reason == "missing-connection":
        conn.hello.connection_id = None
    elif reason == "missing-host":
        hosts.deregister(conn.host_id, conn=conn)
        assert conn.outbound_queue.get_nowait() is None
    result = await read_host_owner_inventory(
        hosts, conn, timeout_s=0 if reason == "invalid-timeout" else 0.05
    )
    assert result is None and sent == []
    assert conn.outbound_queue.empty()
    assert not conn.pending_owner_inventory


@pytest.mark.parametrize(
    "fault_kind",
    [
        "bool-string",
        "missing-field",
        "extra-field",
        "duplicate-owner",
        "empty-pending",
        "duplicate-pending",
        "complete-coverage",
        "wrong-generation",
        "wrong-connection",
        "wrong-read-id",
    ],
)
async def test_host_owner_inventory_invalid_reply_is_unknown(host_owner_control, fault_kind):
    import json

    _hosts, conn, _host, _socket, _sent, fault, _process = host_owner_control

    def mutate(raw):
        body = json.loads(raw)
        if fault_kind == "bool-string":
            body["inventory"]["runners"][0]["stop_requested"] = "false"
        elif fault_kind == "missing-field":
            body["inventory"].pop("cleanup_pending")
        elif fault_kind == "extra-field":
            body["inventory"]["runners"][0]["unexpected"] = "field"
        elif fault_kind == "duplicate-owner":
            body["inventory"]["runners"] *= 2
        elif fault_kind == "empty-pending":
            body["inventory"]["pending_runner_ids"] = [""]
        elif fault_kind == "duplicate-pending":
            body["inventory"]["pending_runner_ids"] = ["runner-pending", "runner-pending"]
        elif fault_kind == "complete-coverage":
            body["inventory"]["context_coverage_complete"] = True
        elif fault_kind == "wrong-generation":
            body["host_generation"] = "different-generation"
        elif fault_kind == "wrong-connection":
            body["connection_id"] = "different-connection"
        else:
            body["request_id"] = "different-read"
        return json.dumps(body)

    fault.mutate = mutate
    assert await _exchange_host_owner_control(host_owner_control, timeout_s=0.05) is None
    assert not conn.pending_owner_inventory


@pytest.mark.parametrize("end", ["timeout", "cancel"])
async def test_host_owner_inventory_wait_is_bounded_and_late_reply_is_ignored(
    host_owner_control, end
):
    from omnigent.server.resident_inventory import read_host_owner_inventory

    hosts, conn, host, socket, _sent, _fault, _process = host_owner_control
    read = asyncio.create_task(read_host_owner_inventory(hosts, conn, timeout_s=0.05))
    try:
        outbound = await asyncio.wait_for(conn.outbound_queue.get(), timeout=2)
        if end == "cancel":
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(read, timeout=2)
        else:
            assert await asyncio.wait_for(read, timeout=2) is None
        assert not conn.pending_owner_inventory
        await host._handle_raw_message(socket, outbound.data)
        await asyncio.sleep(0)
        assert not conn.pending_owner_inventory
        assert list(host._runners) == ["runner-one"]
    finally:
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)


async def test_host_membership_union_preserves_both_sources(
    host_owner_control, db_uri, monkeypatch
):
    from omnigent.server import resident_inventory

    monkeypatch.setattr(resident_inventory, "_HOST_BINDING_PAGE_SIZE", 2)

    hosts, conn, host, socket, _sent, _fault, process = host_owner_control
    conversations = SqlAlchemyConversationStore(db_uri)
    main = conversations.create_conversation()
    side = conversations.create_conversation(parent_conversation_id=main.id)
    other = conversations.create_conversation()
    unbound = conversations.create_conversation()
    for conv in (main, side, other, unbound):
        conversations.set_host_id(conv.id, conn.host_id, "/opt/work/project")
    for conv in (main, side):
        conversations.set_runner_id(conv.id, "runner-one")
    conversations.set_runner_id(other.id, "server-only")
    conversations.update_conversation(side.id, archived=True)
    host._pending_runner_launches["pending-only"] = set()
    read = asyncio.create_task(
        resident_inventory.read_host_membership_inventory(hosts, conn, conversations)
    )
    try:
        for _ in range(2):
            outbound = await asyncio.wait_for(conn.outbound_queue.get(), 2)
            await host._handle_raw_message(socket, outbound.data)
        result = await asyncio.wait_for(read, 2)
        assert result is not None
        assert result.connection_id == "connection-one"
        assert result.host_generation == "server-generation"
        assert result.unbound_session_ids == (unbound.id,)
        assert [(r.runner_id, r.session_ids) for r in result.runners] == [
            ("pending-only", ()),
            ("runner-one", tuple(sorted((main.id, side.id)))),
            ("server-only", (other.id,)),
        ]
        assert result.runners[0].host_owner is None
        assert result.runners[1].host_owner.incarnation_id == "handle-one"
        assert result.runners[2].host_owner is None
        assert not result.aliases_complete and not result.context_coverage_complete
        assert not conn.pending_owner_inventory
        process.poll.assert_not_called()
    finally:
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)


@pytest.mark.parametrize("race", ["binding", "host", "account", "connection"])
async def test_host_membership_union_rejects_changes(host_owner_control, db_uri, race):
    from omnigent.host.connect import _RunnerHandle
    from omnigent.server.resident_inventory import read_host_membership_inventory

    hosts, conn, host, socket, _sent, _fault, process = host_owner_control
    conversations = SqlAlchemyConversationStore(db_uri)
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, conn.host_id, "/opt/work/project")
    conversations.set_runner_id(conv.id, "runner-one")
    read = asyncio.create_task(read_host_membership_inventory(hosts, conn, conversations))
    try:
        outbound = await asyncio.wait_for(conn.outbound_queue.get(), 2)
        await host._handle_raw_message(socket, outbound.data)
        outbound = await asyncio.wait_for(conn.outbound_queue.get(), 2)
        if race == "binding":
            conversations.replace_runner_id(conv.id, "replacement")
        elif race == "host":
            host._runners["runner-one"] = _RunnerHandle(process, Path("/opt/work/runner.log"))
        elif race == "account":
            conn.account_generation = "replacement"
        else:
            hosts.register(conn.host_id, FakeWebSocket(), conn.hello, owner=None)
        await host._handle_raw_message(socket, outbound.data)
        assert await asyncio.wait_for(read, 2) is None
        assert not conn.pending_owner_inventory
    finally:
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)


@pytest.mark.parametrize("fault", ["page-cap", "missing", "reordered", "timeout", "cancel"])
async def test_host_membership_union_incomplete_is_unknown(
    host_owner_control, db_uri, monkeypatch, fault
):
    from omnigent.server import resident_inventory

    hosts, conn, host, socket, _sent, _fault, _process = host_owner_control
    conversations = SqlAlchemyConversationStore(db_uri)
    ids = sorted(conversations.create_conversation().id for _ in range(2))
    for session_id in ids:
        conversations.set_host_id(session_id, conn.host_id, "/opt/work/project")
    if fault == "page-cap":
        monkeypatch.setattr(resident_inventory, "_HOST_BINDING_PAGE_SIZE", 1)
        monkeypatch.setattr(resident_inventory, "_MAX_HOST_BINDING_PAGES", 1)
    elif fault == "missing":
        monkeypatch.setattr(conversations, "get_runner_ids", lambda _ids: {})
    elif fault == "reordered":
        monkeypatch.setattr(conversations, "list_host_conversation_ids", lambda *a, **k: ids[::-1])
    read = asyncio.create_task(
        resident_inventory.read_host_membership_inventory(
            hosts, conn, conversations, timeout_s=0.1
        )
    )
    try:
        outbound = await asyncio.wait_for(conn.outbound_queue.get(), 2)
        if fault in {"timeout", "cancel"}:
            if fault == "cancel":
                read.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await read
            else:
                assert await asyncio.wait_for(read, 2) is None
            await host._handle_raw_message(socket, outbound.data)
            await asyncio.sleep(0)
        else:
            await host._handle_raw_message(socket, outbound.data)
            assert await asyncio.wait_for(read, 2) is None
        assert not conn.pending_owner_inventory
    finally:
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)


async def test_host_membership_union_workspace_mismatch_never_enqueues(host_owner_control, db_uri):
    from omnigent.db.db_models import workspace_scope
    from omnigent.server.resident_inventory import read_host_membership_inventory

    hosts, conn, _host, _socket, sent, _fault, _process = host_owner_control
    conversations = SqlAlchemyConversationStore(db_uri)
    with workspace_scope(17):
        assert await read_host_membership_inventory(hosts, conn, conversations) is None
    assert conn.outbound_queue.empty() and not sent


@pytest.mark.parametrize("end", ["timeout", "cancel"])
async def test_host_membership_union_bounds_database_wait(
    host_owner_control, db_uri, monkeypatch, end
):
    from omnigent.server.resident_inventory import read_host_membership_inventory

    hosts, conn, host, socket, _sent, _fault, _process = host_owner_control
    conversations = SqlAlchemyConversationStore(db_uri)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def paused_page(*_args, **_kwargs):
        entered.set()
        try:
            assert release.wait(3), "test did not release read-only query"
            return []
        finally:
            finished.set()

    monkeypatch.setattr(conversations, "list_host_conversation_ids", paused_page)
    read = asyncio.create_task(
        read_host_membership_inventory(hosts, conn, conversations, timeout_s=0.5)
    )
    try:
        outbound = await asyncio.wait_for(conn.outbound_queue.get(), 2)
        await host._handle_raw_message(socket, outbound.data)
        assert await asyncio.to_thread(entered.wait, 2)
        if end == "cancel":
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await read
        else:
            assert await asyncio.wait_for(read, 2) is None
        assert not conn.pending_owner_inventory and conn.outbound_queue.empty()
    finally:
        release.set()
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 2)


@pytest.fixture
async def host_context_reader(host_owner_control, db_uri, monkeypatch):
    from omnigent.inner.executor import ExecutorResidentInventory, ExecutorResidentOwner
    from omnigent.runner.native.orchestration import NativeBackendInventorySnapshot
    from omnigent.runner.resident_inventory import OwnerRegistrationSnapshot
    from omnigent.runner.routing import RunnerRouter
    from omnigent.runtime.harnesses.process_manager import (
        HarnessContextInventorySnapshot,
        HarnessInventorySnapshot,
        HarnessResidentSnapshot,
    )

    hosts, conn, _host, _socket, _sent, _fault, _process = host_owner_control
    conversations = SqlAlchemyConversationStore(db_uri)
    main = conversations.create_conversation()
    side = conversations.create_conversation(parent_conversation_id=main.id)
    for conv in (main, side):
        conversations.set_host_id(conv.id, conn.host_id, "/opt/work/project")
        conversations.set_runner_id(conv.id, "runner-one")
    owner = HarnessResidentSnapshot("harness-one", main.id, "codex", False, False, False)
    inventory = OwnerRegistrationSnapshot(
        NativeBackendInventorySnapshot((), True),
        None,
        HarnessInventorySnapshot(
            (owner,),
            True,
            (
                HarnessContextInventorySnapshot(
                    owner,
                    0,
                    ExecutorResidentInventory(
                        (
                            ExecutorResidentOwner(
                                "context-one", ("main", "ui"), "thread-one", True, False, False
                            ),
                            ExecutorResidentOwner(
                                "context-side", ("side",), "thread-side", True, False, False
                            ),
                        )
                    ),
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        "omnigent.runner.resident_inventory.owner_registration_snapshot",
        AsyncMock(return_value=inventory),
    )
    runner_app = create_runner_app(server_client=NullServerClient(), auth_token="runner-auth")
    tunnels = TunnelRegistry()
    hello = HelloFrame("test", 1, capabilities=["resident_inventory_v1"])
    tunnels.register("runner-one", FakeWebSocket(), hello)
    router = RunnerRouter(registry=tunnels, conversation_store=conversations, host_registry=hosts)
    initializer = RunnerSessionInitializer(tunnels, server_version="test")
    requests = []

    async def record(request):
        requests.append(request.url.path)

    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=runner_app),
                base_url="http://runner",
                headers={"Authorization": "Bearer runner-auth"},
                event_hooks={"request": [record]},
            )
        )
        router._clients["runner-one"] = client
        yield host_owner_control, conversations, router, initializer, tunnels, client, requests


async def _exchange_host_context_inventory(setup, **kwargs):
    from omnigent.server.resident_inventory import read_host_context_inventory

    control, conversations, router, initializer, _tunnels, _client, _requests = setup
    hosts, conn, host, socket, _sent, _fault, _process = control
    read = asyncio.create_task(
        read_host_context_inventory(hosts, conn, conversations, router, initializer, **kwargs)
    )
    try:
        for _ in range(4):
            outbound = await asyncio.wait_for(conn.outbound_queue.get(), 2)
            await host._handle_raw_message(socket, outbound.data)
        return await asyncio.wait_for(read, 2)
    finally:
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)


async def test_host_context_inventory_preserves_contexts_and_aliases(host_context_reader):
    result = await _exchange_host_context_inventory(host_context_reader)
    assert result is not None
    assert len(result.runners) == 1
    read = result.runners[0]
    assert read.membership.runner_id == "runner-one"
    contexts = read.inventory.harnesses.contexts[0].inventory
    assert [(o.incarnation_id, o.session_keys, o.vendor_context_id) for o in contexts.owners] == [
        ("context-one", ("main", "ui"), "thread-one"),
        ("context-side", ("side",), "thread-side"),
    ]
    assert not result.aliases_complete and not result.context_coverage_complete
    assert host_context_reader[-1] == ["/v1/startup/owner-inventory"]


def _add_host_context_runner(setup, runner_id):
    from omnigent.host.connect import _RunnerHandle

    control, conversations, router, _initializer, tunnels, client, _requests = setup
    _hosts, conn, host, _socket, _sent, _fault, process = control
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, conn.host_id, "/opt/work/project")
    conversations.set_runner_id(conv.id, runner_id)
    host._runners[runner_id] = _RunnerHandle(
        process, Path("/opt/work/runner.log"), session_id=conv.id
    )
    tunnels.register(
        runner_id, FakeWebSocket(), HelloFrame("test", 1, capabilities=["resident_inventory_v1"])
    )
    router._clients[runner_id] = client
    return conv


@pytest.mark.parametrize("change", ["none", "reconnect", "disconnect"])
async def test_host_context_inventory_rechecks_early_runner(
    host_context_reader, monkeypatch, change
):
    _add_host_context_runner(host_context_reader, "runner-two")
    _control, _conversations, _router, initializer, tunnels, client, _requests = (
        host_context_reader
    )
    checked = asyncio.Event()
    require = initializer.require_generation

    def observe(runner_id, runner_client, generation):
        require(runner_id, runner_client, generation)
        if runner_id == "runner-one":
            checked.set()

    monkeypatch.setattr(initializer, "require_generation", observe)
    calls = 0

    async def pause_second(response):
        nonlocal calls
        calls += 1
        if calls == 2:
            await asyncio.wait_for(checked.wait(), 2)
            if change == "reconnect":
                tunnels.register(
                    "runner-one",
                    FakeWebSocket(),
                    HelloFrame("test", 1, capabilities=["resident_inventory_v1"]),
                )
            elif change == "disconnect":
                tunnels.deregister("runner-one")

    client.event_hooks["response"].append(pause_second)
    result = await _exchange_host_context_inventory(host_context_reader)
    assert result is not None
    assert (
        result.runners[1].inventory.harnesses.contexts[0].inventory.owners[0].vendor_context_id
        == "thread-one"
    )
    if change == "none":
        assert result.runners[0].inventory is not None
        assert result.runners[0].inventory == result.runners[1].inventory
        assert result.runners[0].membership.runner_id != result.runners[1].membership.runner_id
    else:
        assert result.runners[0].inventory is None
        assert result.runners[0].runner_generation is None
    assert not result.aliases_complete and not result.context_coverage_complete


@pytest.mark.parametrize(
    "reason", ["unsupported", "closing", "pending", "host-only", "server-only", "bad-read-id"]
)
async def test_host_context_inventory_preserves_unknown_members(host_context_reader, reason):
    control, conversations, _router, _initializer, tunnels, client, requests = host_context_reader
    _hosts, _conn, host, _socket, _sent, _fault, _process = control
    if reason == "unsupported":
        tunnels.get("runner-one").hello.capabilities.clear()
    elif reason == "closing":
        host._runners["runner-one"].stop_requested = True
    elif reason == "pending":
        host._pending_runner_launches["runner-one"] = set()
    elif reason == "host-only":
        for conv in conversations.list_conversations_by_runner_id("runner-one"):
            conversations.clear_runner_id(conv.id)
    elif reason == "server-only":
        host._runners.clear()
    else:

        async def corrupt(response):
            import json

            await response.aread()
            payload = response.json()
            payload["read_id"] = "old-read"
            response._content = json.dumps(payload).encode()

        client.event_hooks["response"].append(corrupt)
    result = await _exchange_host_context_inventory(host_context_reader)
    assert result is not None
    assert len(result.runners) == 1 and result.runners[0].inventory is None
    assert requests == (["/v1/startup/owner-inventory"] if reason == "bad-read-id" else [])


@pytest.mark.parametrize("change", ["host-owner", "binding", "account"])
async def test_host_context_inventory_rejects_final_membership_changes(
    host_context_reader, change
):
    control, conversations, _router, _initializer, _tunnels, client, _requests = (
        host_context_reader
    )
    _hosts, conn, host, _socket, _sent, _fault, process = control

    async def mutate(_response):
        from omnigent.host.connect import _RunnerHandle

        if change == "host-owner":
            host._runners["runner-one"] = _RunnerHandle(process, Path("/opt/work/runner.log"))
        elif change == "binding":
            conv = conversations.list_conversations_by_runner_id("runner-one")[0]
            conversations.replace_runner_id(conv.id, "replacement")
        else:
            conn.account_generation = "replacement"

    client.event_hooks["response"].append(mutate)
    assert await _exchange_host_context_inventory(host_context_reader) is None


@pytest.mark.parametrize("end", ["complete", "timeout", "cancel"])
async def test_host_context_inventory_bounds_and_drains_reads(host_context_reader, end):
    from omnigent.server.resident_inventory import read_host_context_inventory

    for index in range(5):
        _add_host_context_runner(host_context_reader, f"runner-extra-{index}")
    control, conversations, router, initializer, _tunnels, client, requests = host_context_reader
    hosts, conn, host, socket, _sent, _fault, _process = control
    active = set()
    four_started, resume = asyncio.Event(), asyncio.Event()
    maximum = 0

    async def pause(response):
        nonlocal maximum
        key = response.request.url.params["read_id"]
        active.add(key)
        maximum = max(maximum, len(active))
        if len(active) == 4:
            four_started.set()
        try:
            await asyncio.wait_for(resume.wait(), 2)
        finally:
            active.remove(key)

    client.event_hooks["response"].append(pause)
    read = asyncio.create_task(
        read_host_context_inventory(
            hosts,
            conn,
            conversations,
            router,
            initializer,
            timeout_s=0.5 if end == "timeout" else 5,
        )
    )
    try:
        for _ in range(2):
            outbound = await asyncio.wait_for(conn.outbound_queue.get(), 2)
            await host._handle_raw_message(socket, outbound.data)
        await asyncio.wait_for(four_started.wait(), 2)
        assert maximum == 4
        if end == "cancel":
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await read
        elif end == "timeout":
            assert await asyncio.wait_for(read, 2) is None
        else:
            resume.set()
            for _ in range(2):
                outbound = await asyncio.wait_for(conn.outbound_queue.get(), 2)
                await host._handle_raw_message(socket, outbound.data)
            result = await asyncio.wait_for(read, 2)
            assert len(result.runners) == len(requests) == 6
            assert all(r.inventory is not None for r in result.runners)
        assert not active and maximum == 4 and not conn.pending_owner_inventory
        assert not client.is_closed
    finally:
        resume.set()
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "empty",
        "unknown",
        "ambiguous",
        "bool-string",
        "extra",
        "missing",
        "complete",
        "context-complete",
        "owner-mismatch",
        "duplicate-harness",
        "duplicate-context",
        "unaligned",
        "closing-harness",
    ],
)
def test_runner_context_inventory_strict_wire_decode(case):
    from omnigent.server.resident_inventory import _decode_runner_context_inventory

    owner = {
        "incarnation_id": "harness-one",
        "session_id": "session-one",
        "harness": "codex",
        "closing": False,
        "wrapper_exited": False,
        "exit_confirmed": False,
    }
    contexts = {
        "owners": [
            {
                "incarnation_id": "context-one",
                "session_keys": ["main", "ui"],
                "vendor_context_id": "thread-one",
                "started": True,
                "closing": False,
                "cleanup_complete": False,
            }
        ],
        "ambiguous_context_ids": [],
        "aliases_complete": False,
        "context_coverage_complete": False,
    }
    payload = {
        "native_backends": {
            "owners": [],
            "exit_coverage_complete": True,
            "aliases_complete": False,
        },
        "terminals": None,
        "harnesses": {
            "owners": [owner],
            "exit_coverage_complete": True,
            "contexts": [{"owner": dict(owner), "release_generation": 0, "inventory": contexts}],
        },
        "executor_contexts_complete": False,
        "external_owners_complete": False,
    }
    if case == "empty":
        contexts["owners"] = []
    elif case == "unknown":
        payload["harnesses"]["contexts"] = [None]
    elif case == "ambiguous":
        contexts["ambiguous_context_ids"] = ["thread-one"]
    elif case == "bool-string":
        contexts["owners"][0]["started"] = "false"
    elif case == "extra":
        contexts["owners"][0]["unexpected"] = "value"
    elif case == "missing":
        payload.pop("external_owners_complete")
    elif case == "complete":
        payload["executor_contexts_complete"] = True
    elif case == "context-complete":
        contexts["context_coverage_complete"] = True
    elif case == "owner-mismatch":
        payload["harnesses"]["contexts"][0]["owner"]["session_id"] = "other"
    elif case == "duplicate-harness":
        payload["harnesses"]["owners"] *= 2
        payload["harnesses"]["contexts"] *= 2
    elif case == "duplicate-context":
        contexts["owners"] *= 2
    elif case == "unaligned":
        payload["harnesses"]["contexts"].append(None)
    elif case == "closing-harness":
        owner["closing"] = True
        payload["harnesses"]["contexts"][0]["owner"]["closing"] = True
    result = _decode_runner_context_inventory(payload)
    if case not in {"valid", "empty", "unknown", "ambiguous"}:
        assert result is None
    else:
        assert result is not None
        assert result.external_owners_complete is False
        read = result.harnesses.contexts[0]
        if case == "unknown":
            assert read is None
        elif case == "empty":
            assert read.inventory.owners == ()
        elif case == "ambiguous":
            assert read.inventory.ambiguous_context_ids == ("thread-one",)
            assert read.inventory.aliases_complete is False
        else:
            assert read.inventory.owners[0].session_keys == ("main", "ui")
