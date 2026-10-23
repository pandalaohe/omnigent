"""Owner evidence reads never probe or create vendor/terminal resources."""

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.inner.executor import Executor, ExecutorResidentInventory, ExecutorResidentOwner
from omnigent.runner import create_runner_app
from omnigent.runner.native import orchestration
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses.process_manager import (
    HarnessProcessManager,
    _HarnessEndpoint,
    _SubprocessEntry,
)
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import _runner_client
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_owner_reader_preserves_missing_registry_and_incomplete_aliases(
    monkeypatch, configured
):
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", {})
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    resources = SessionResourceRegistry(
        terminal_registry=TerminalRegistry() if configured else None
    )
    resources.list_resources = Mock(side_effect=AssertionError("must read registrations only"))
    app = create_runner_app(
        server_client=NullServerClient(), resource_registry=resources, auth_token="test-auth"
    )
    async with _runner_client(app) as client:
        url = "/v1/startup/owner-inventory?read_id=read-one"
        assert (await client.get(url)).status_code == 401
        response = await client.get(url, headers={"Authorization": "Bearer test-auth"})
        assert response.status_code == 200
        assert response.json() == {
            "read_id": "read-one",
            "native_backends": {
                "owners": [],
                "exit_coverage_complete": True,
                "aliases_complete": False,
            },
            "terminals": [] if configured else None,
            "harnesses": None,
            "executor_contexts_complete": False,
            "external_owners_complete": False,
            "native_thread_ids": [],
            "harness_thread_ids": None,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [None, "backend", "harness"])
async def test_reader_keeps_shared_backend_aliases_as_evidence_without_probing(
    monkeypatch, conflict
):
    backend = SimpleNamespace(
        resident_incarnation_id="backend-one",
        process_exit_confirmed=False,
        exit_confirmed=False,
        close=Mock(side_effect=AssertionError("read must not close")),
        is_alive=Mock(side_effect=AssertionError("read must not probe")),
    )
    alias = SimpleNamespace(**vars(backend)) if conflict == "backend" else backend
    monkeypatch.setattr(
        orchestration,
        "_AUTO_CODEX_APP_SERVERS",
        {
            "session-one": backend,
            **({"ui-alias": alias} if conflict != "harness" else {}),
        },
    )
    monkeypatch.setattr(
        orchestration,
        "_AUTO_OPENCODE_SERVERS",
        {"ui-alias": alias} if conflict == "harness" else {},
    )
    app = create_runner_app(server_client=NullServerClient())
    async with _runner_client(app) as client:
        response = await client.get("/v1/startup/owner-inventory?read_id=owners")
    assert response.status_code == 200
    expected = {
        "owners": [
            {
                "incarnation_id": "backend-one",
                "harness": "codex-native",
                "session_ids": ["session-one", "ui-alias"],
                "registered": True,
                "closing": False,
                "process_exit_confirmed": False,
                "exit_confirmed": False,
                "aliases_complete": False,
                "transport_generation": None,
            }
        ],
        "exit_coverage_complete": True,
        "aliases_complete": False,
    }
    if conflict:
        expected = {"owners": [], "exit_coverage_complete": False, "aliases_complete": False}
    assert response.json()["native_backends"] == expected
    assert response.json()["executor_contexts_complete"] is False
    backend.close.assert_not_called()
    backend.is_alive.assert_not_called()


@pytest.mark.parametrize(
    "state",
    [
        "bound",
        "unretained",
        "reconnected",
        "closed",
        "replaced",
        "incarnation",
        "closing",
        "restart-before-binding",
    ],
)
async def test_native_inventory_keeps_only_same_backend_transport_binding(monkeypatch, state):
    backend = SimpleNamespace(
        resident_incarnation_id="backend-bound",
        process_exit_confirmed=False,
        exit_confirmed=False,
        close=AsyncMock(),
    )
    client = SimpleNamespace(resident_connection_generation=3)
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", {"main": backend, "ui": backend})
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    monkeypatch.setattr(orchestration, "_STARTING_NATIVE_BACKENDS", {})
    monkeypatch.setattr(orchestration, "_CLOSING_NATIVE_BACKENDS", {})
    monkeypatch.setattr(orchestration, "_CODEX_INVENTORY_CLIENTS", {}, raising=False)
    if state == "restart-before-binding":
        backend.resident_incarnation_id = "replacement-incarnation"
    if state != "unretained":
        orchestration._bind_codex_inventory_client(
            "main", backend, client, incarnation_id="backend-bound"
        )
    if state == "reconnected":
        client.resident_connection_generation = 4
    elif state == "closed":
        client.resident_connection_generation = None
    elif state == "replaced":
        backend = SimpleNamespace(**vars(backend))
        orchestration._AUTO_CODEX_APP_SERVERS.update(main=backend, ui=backend)
    elif state == "incarnation":
        backend.resident_incarnation_id = "replacement-incarnation"
    elif state == "closing":
        orchestration._CLOSING_NATIVE_BACKENDS[object()] = orchestration._ClosingNativeBackend(
            backend.resident_incarnation_id, "main", "codex-native", backend
        )
    inventory = orchestration.native_backend_resident_inventory()
    (owner,) = inventory.owners
    assert owner.session_ids == ("main", "ui")
    assert owner.transport_generation == (3 if state == "bound" else None)
    assert not owner.aliases_complete and not inventory.aliases_complete
    assert orchestration.native_backend_resident_inventory() == inventory
    backend.close.assert_not_called()
    if state == "bound":
        await orchestration._close_native_backend("main", "codex-native", backend)
        assert not orchestration._CODEX_INVENTORY_CLIENTS


@pytest.mark.parametrize(
    "phases",
    [
        ("registered", "registered"),
        ("registered", "starting"),
        ("registered", "closing"),
        ("starting", "starting"),
        ("starting", "closing"),
        ("closing", "closing"),
    ],
)
@pytest.mark.parametrize("conflict", [None, "backend", "harness"])
def test_native_owner_union_checks_identity_across_lifecycle_maps(monkeypatch, phases, conflict):
    backend = SimpleNamespace(
        resident_incarnation_id="shared-incarnation",
        process_exit_confirmed=False,
        exit_confirmed=False,
    )
    other = SimpleNamespace(**vars(backend)) if conflict == "backend" else backend
    codex, opencode, starting, closing = {}, {}, {}, {}
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", codex)
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", opencode)
    monkeypatch.setattr(orchestration, "_STARTING_NATIVE_BACKENDS", starting)
    monkeypatch.setattr(orchestration, "_CLOSING_NATIVE_BACKENDS", closing)
    for session, phase, owner, harness in zip(
        ("one", "two"),
        phases,
        (backend, other),
        ("codex-native", "opencode-native" if conflict == "harness" else "codex-native"),
        strict=True,
    ):
        if phase == "registered":
            (codex if harness == "codex-native" else opencode)[session] = owner
        elif phase == "starting":
            starting[object()] = (session, harness, owner)
        else:
            closing[object()] = orchestration._ClosingNativeBackend(
                owner.resident_incarnation_id, session, harness, owner
            )

    snapshot = orchestration.native_backend_resident_inventory()
    assert orchestration.native_backend_resident_inventory() == snapshot
    assert snapshot.aliases_complete is False
    if conflict:
        assert snapshot.owners == ()
        assert snapshot.exit_coverage_complete is False
    else:
        assert snapshot.exit_coverage_complete is True
        assert snapshot.owners == (
            orchestration.NativeBackendResidentSnapshot(
                "shared-incarnation",
                "codex-native",
                ("one", "two"),
                "registered" in phases,
                "closing" in phases,
                False,
                False,
            ),
        )


@pytest.fixture
def native_thread_inventory(monkeypatch):
    for name in (
        "_AUTO_CODEX_APP_SERVERS",
        "_AUTO_OPENCODE_SERVERS",
        "_STARTING_NATIVE_BACKENDS",
        "_CLOSING_NATIVE_BACKENDS",
        "_CODEX_INVENTORY_CLIENTS",
    ):
        monkeypatch.setattr(orchestration, name, {})
    monkeypatch.setattr(orchestration, "_native_backend_exit_coverage_complete", True)

    def add(key, *, ids=("thread-one",), bind=True):
        backend = SimpleNamespace(
            resident_incarnation_id=f"backend-{key}",
            process_exit_confirmed=False,
            exit_confirmed=False,
            close=Mock(side_effect=AssertionError("read must not close")),
            start=Mock(side_effect=AssertionError("read must not start")),
        )
        client = SimpleNamespace(
            resident_connection_generation=3,
            resident_thread_observation_revision=0,
            read_loaded_thread_ids=AsyncMock(return_value=ids),
            connect=Mock(side_effect=AssertionError("read must not connect")),
            close=Mock(side_effect=AssertionError("read must not close")),
        )
        orchestration._AUTO_CODEX_APP_SERVERS[key] = backend
        if bind:
            orchestration._bind_codex_inventory_client(
                key, backend, client, incarnation_id=backend.resident_incarnation_id
            )
        return backend, client

    return add


@pytest.mark.parametrize(
    "state",
    [
        "loaded",
        "empty",
        "unknown",
        "unretained",
        "closing",
        "exited",
        "unsupported",
        "revision-none",
        "revision-bool",
        "revision-string",
        "revision-float",
        "revision-negative",
        "revision-missing",
        "revision-later",
    ],
)
async def test_native_thread_inventory_preserves_owner_evidence(native_thread_inventory, state):
    backend, client = native_thread_inventory(
        "main",
        ids={"empty": (), "unknown": None}.get(state, ("thread-one",)),
        bind=state != "unretained",
    )
    orchestration._AUTO_CODEX_APP_SERVERS["ui"] = backend
    if state == "revision-missing":
        del client.resident_thread_observation_revision
    elif state.startswith("revision-"):
        client.resident_thread_observation_revision = {
            "revision-none": None,
            "revision-bool": False,
            "revision-string": "0",
            "revision-float": 0.0,
            "revision-negative": -1,
            "revision-later": 7,
        }[state]
    if state == "closing":
        orchestration._CLOSING_NATIVE_BACKENDS[object()] = orchestration._ClosingNativeBackend(
            "backend-main", "main", "codex-native", backend
        )
    elif state == "exited":
        backend.process_exit_confirmed = True
    elif state == "unsupported":
        orchestration._AUTO_CODEX_APP_SERVERS.clear()
        orchestration._AUTO_OPENCODE_SERVERS["main"] = backend
    result = await orchestration.read_native_backend_thread_inventory()
    assert result.thread_ids == (
        ("thread-one",)
        if state in {"loaded", "revision-later"}
        else ()
        if state == "empty"
        else None,
    )
    assert result.inventory == orchestration.native_backend_resident_inventory()
    assert not result.context_coverage_complete and not result.inventory.aliases_complete
    if state in {"loaded", "empty", "unknown"} or state.startswith("revision-"):
        client.read_loaded_thread_ids.assert_awaited_once_with(timeout_s=5.0)
        assert result.inventory.owners[0].session_ids == ("main", "ui")
    else:
        client.read_loaded_thread_ids.assert_not_called()
    client.connect.assert_not_called()
    client.close.assert_not_called()
    backend.start.assert_not_called()
    backend.close.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "rebind",
        "client",
        "backend",
        "generation",
        "observation",
        "observation-bool",
        "observation-none",
        "observation-string",
        "observation-float",
        "observation-negative",
        "observation-missing",
        "incarnation",
        "alias",
        "closing",
        "release",
        "new-owner",
    ],
)
async def test_native_thread_inventory_reconciles_after_later_owner_waits(
    native_thread_inventory, change
):
    first, client = native_thread_inventory("first")
    _, second = native_thread_inventory("second", ids=())
    waiting, resume = asyncio.Event(), asyncio.Event()

    async def pause(**_kwargs):
        waiting.set()
        await resume.wait()
        return ()

    second.read_loaded_thread_ids.side_effect = pause
    read = asyncio.create_task(orchestration.read_native_backend_thread_inventory())
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        assert client.read_loaded_thread_ids.await_count == 1
        if change in {"client", "backend", "rebind"}:
            backend = SimpleNamespace(**vars(first)) if change == "backend" else first
            new_client = SimpleNamespace(**vars(client)) if change == "client" else client
            orchestration._AUTO_CODEX_APP_SERVERS["first"] = backend
            orchestration._bind_codex_inventory_client(
                "first", backend, new_client, incarnation_id="backend-first"
            )
        elif change == "generation":
            client.resident_connection_generation = 4
        elif change == "observation-missing":
            del client.resident_thread_observation_revision
        elif change.startswith("observation"):
            client.resident_thread_observation_revision = {
                "observation": 1,
                "observation-bool": False,
                "observation-none": None,
                "observation-string": "0",
                "observation-float": 0.0,
                "observation-negative": -1,
            }[change]
        elif change == "incarnation":
            first.resident_incarnation_id = "restarted-first"
        elif change == "alias":
            orchestration._AUTO_CODEX_APP_SERVERS["ui"] = first
        elif change == "closing":
            orchestration._CLOSING_NATIVE_BACKENDS[object()] = orchestration._ClosingNativeBackend(
                "backend-first", "first", "codex-native", first
            )
        elif change == "release":
            orchestration._AUTO_CODEX_APP_SERVERS.pop("first")
        elif change == "new-owner":
            native_thread_inventory("new")
        resume.set()
        result = await asyncio.wait_for(read, timeout=2)
        by_session = {
            owner.session_ids[0]: ids
            for owner, ids in zip(result.inventory.owners, result.thread_ids, strict=True)
        }
        assert by_session["second"] == ()
        if change == "release":
            assert "first" not in by_session
        else:
            assert by_session["first"] == (
                ("thread-one",) if change in {"none", "new-owner"} else None
            )
        if change == "new-owner":
            assert by_session["new"] is None
    finally:
        resume.set()
        read.cancel()
        await asyncio.gather(read, return_exceptions=True)


@pytest.mark.parametrize("end", ["complete", "timeout", "cancel"])
async def test_native_thread_inventory_bounds_workers_and_drains(native_thread_inventory, end):
    active, entered, resume = set(), asyncio.Event(), asyncio.Event()
    clients = []

    async def pause(key, **_kwargs):
        active.add(key)
        if len(active) == 4:
            entered.set()
        try:
            await resume.wait()
            return ()
        finally:
            active.remove(key)

    for index in range(6):
        _, client = native_thread_inventory(str(index))

        async def bound_pause(key=index, **kwargs):
            return await pause(key, **kwargs)

        client.read_loaded_thread_ids.side_effect = bound_pause
        clients.append(client)
    read = asyncio.create_task(orchestration.read_native_backend_thread_inventory(timeout_s=0.2))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert len(active) == 4
        if end == "cancel":
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await read
        else:
            if end == "complete":
                resume.set()
            result = await asyncio.wait_for(read, timeout=2)
            assert result.thread_ids == (((),) * 6 if end == "complete" else (None,) * 6)
        assert not active
        assert all(client.close.call_count == 0 for client in clients)
    finally:
        resume.set()
        read.cancel()
        await asyncio.gather(read, return_exceptions=True)


@pytest.mark.parametrize("timeout_s", [0, -1, 5.1, float("inf"), float("nan")])
async def test_native_thread_inventory_invalid_deadline_does_not_read(
    native_thread_inventory, timeout_s
):
    _, client = native_thread_inventory("one")
    result = await orchestration.read_native_backend_thread_inventory(timeout_s=timeout_s)
    assert result.thread_ids == (None,)
    client.read_loaded_thread_ids.assert_not_called()


@pytest.mark.parametrize(
    "state", ["no-owners", "collision", "read-error", "same-vendor-id", "partial-timeout"]
)
async def test_native_thread_inventory_keeps_unknown_sources_separate(
    native_thread_inventory, state
):
    if state == "no-owners":
        result = await orchestration.read_native_backend_thread_inventory()
        assert result.thread_ids == () and result.inventory.owners == ()
    else:
        _, first = native_thread_inventory("first")
        backend, second = native_thread_inventory("second")
        if state == "collision":
            backend.resident_incarnation_id = "backend-first"
        elif state == "read-error":
            first.read_loaded_thread_ids.side_effect = RuntimeError("channel unavailable")
        elif state == "partial-timeout":

            async def pause(**_kwargs):
                await asyncio.Event().wait()

            second.read_loaded_thread_ids.side_effect = pause
        result = await orchestration.read_native_backend_thread_inventory(timeout_s=0.1)
        if state == "collision":
            assert result.thread_ids == () and result.inventory.owners == ()
            assert not result.inventory.exit_coverage_complete
            first.read_loaded_thread_ids.assert_not_called()
            second.read_loaded_thread_ids.assert_not_called()
        else:
            assert result.thread_ids == (
                None if state == "read-error" else ("thread-one",),
                None if state == "partial-timeout" else ("thread-one",),
            )
    assert not result.context_coverage_complete and not result.inventory.aliases_complete


@pytest.mark.parametrize("replace_connection", [False, True])
async def test_native_thread_inventory_uses_established_client_pages(
    native_thread_inventory, replace_connection
):
    import json

    from websockets.asyncio.client import ClientConnection

    from omnigent.harnesses.codex_native.app_server import CodexAppServerClient

    backend, _ = native_thread_inventory("main", bind=False)
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:1")
    websocket = AsyncMock(spec=ClientConnection)
    reader = asyncio.create_task(asyncio.Event().wait())
    client._ws = websocket
    client._reader_task = reader
    client._established_connection = (0, websocket, reader)
    client.connect = AsyncMock(side_effect=AssertionError("must not connect"))
    requests = []

    async def respond(raw):
        message = json.loads(raw)
        requests.append((message["method"], message["params"]))
        first = len(requests) == 1
        if not first and replace_connection:
            client._connection_generation += 1
        client._pending_requests[message["id"]].set_result(
            {
                "id": message["id"],
                "result": {
                    "data": ["main" if first else "side"],
                    "nextCursor": "second" if first else None,
                },
            }
        )

    websocket.send.side_effect = respond
    orchestration._bind_codex_inventory_client(
        "main", backend, client, incarnation_id="backend-main"
    )
    try:
        result = await orchestration.read_native_backend_thread_inventory()
        assert result.thread_ids == (None if replace_connection else ("main", "side"),)
        assert requests == [
            ("thread/list/loaded", {"limit": 100}),
            ("thread/list/loaded", {"limit": 100, "cursor": "second"}),
        ]
        assert client._pending_requests == {}
        client.connect.assert_not_called()
        websocket.close.assert_not_called()
    finally:
        await client.close()


@pytest.fixture
async def harness_inventory_reader(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", {})
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    monkeypatch.setattr(orchestration, "_CLOSING_NATIVE_BACKENDS", {})
    monkeypatch.setattr(orchestration, "_native_backend_exit_coverage_complete", True)
    manager = HarnessProcessManager(tmp_parent=tmp_path)
    manager._started = True
    requests = []

    def forbidden(*_args, **_kwargs):
        pytest.fail("inventory must not spawn, probe, or alter retention")

    for name in ("start", "get_client", "_spawn_entry", "_retention_expiry", "note_activity"):
        monkeypatch.setattr(manager, name, forbidden)

    class Process:
        pid = None
        returncode = None

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    class Owner(Executor):
        def __init__(self, inventory):
            self.inventory = inventory

        def resident_inventory(self):
            if isinstance(self.inventory, Exception):
                raise self.inventory
            return self.inventory

    async with AsyncExitStack() as stack:

        async def add(session_id, inventory):
            owner = Owner(inventory)
            adapter = ExecutorAdapter(forbidden)
            adapter._executor = owner
            harness = adapter.build()
            harness.state.conversation_id = session_id
            harness.state.harness_auth_token = "harness-auth"

            async def record(request):
                requests.append((session_id, request.url.path))

            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=harness, raise_app_exceptions=False),
                    base_url="http://harness",
                    headers={"Authorization": "Bearer harness-auth"},
                    event_hooks={"request": [record]},
                )
            )
            entry = _SubprocessEntry(Process(), client, _HarnessEndpoint(), "codex")
            entry.incarnation_id = f"harness-{session_id}"
            manager._entries[session_id] = entry
            manager._retention_managed[session_id] = 0.0
            return entry, owner

        app = create_runner_app(
            server_client=NullServerClient(), process_manager=manager, auth_token="runner-auth"
        )
        client = await stack.enter_async_context(_runner_client(app))
        client.headers["Authorization"] = "Bearer runner-auth"
        yield manager, add, client, requests


@pytest.mark.parametrize("generation", [None, 0, 7])
async def test_runner_owner_inventory_composes_registered_harness_contexts(
    harness_inventory_reader,
    generation,
):
    manager, add, client, requests = harness_inventory_reader
    entry, _owner = await add(
        "one",
        ExecutorResidentInventory(
            owners=(
                ExecutorResidentOwner(
                    "context-one",
                    ("main", "ui"),
                    "thread-one",
                    True,
                    generation is None,
                    False,
                    transport_generation=generation,
                ),
            )
        ),
    )
    response = await client.get("/v1/startup/owner-inventory?read_id=composed")
    assert response.status_code == 200
    expected_owner = {
        "incarnation_id": "harness-one",
        "session_id": "one",
        "harness": "codex",
        "closing": False,
        "wrapper_exited": False,
        "exit_confirmed": False,
    }
    assert response.json()["harnesses"] == {
        "owners": [expected_owner],
        "exit_coverage_complete": True,
        "contexts": [
            {
                "owner": expected_owner,
                "release_generation": 0,
                "inventory": {
                    "owners": [
                        {
                            "incarnation_id": "context-one",
                            "session_keys": ["main", "ui"],
                            "vendor_context_id": "thread-one",
                            "started": True,
                            "closing": generation is None,
                            "cleanup_complete": False,
                            "transport_generation": generation,
                        }
                    ],
                    "ambiguous_context_ids": [],
                    "aliases_complete": False,
                    "context_coverage_complete": False,
                },
            }
        ],
    }
    assert response.json()["executor_contexts_complete"] is False
    assert response.json()["external_owners_complete"] is False
    assert requests == [("one", "/v1/sessions/one/resident-thread-inventory")]
    assert entry.last_used_at == 0.0
    assert manager._retention_managed == {"one": 0.0}
    assert manager._entries["one"] is entry


async def test_runner_owner_inventory_auth_precedes_harness_reads(harness_inventory_reader):
    _manager, add, client, requests = harness_inventory_reader
    await add("one", ExecutorResidentInventory(owners=()))
    client.headers.clear()
    assert (await client.get("/v1/startup/owner-inventory?read_id=auth")).status_code == 401
    assert requests == []


@pytest.mark.parametrize("ids", [("main", "side"), (), None, (" main ", "main")])
@pytest.mark.parametrize("generation", [None, 0, 7])
async def test_runner_inventory_transports_sdk_threads_after_auth(
    harness_inventory_reader, ids, generation
):
    from omnigent.inner.executor import ExecutorThreadInventorySnapshot

    manager, add, client, requests = harness_inventory_reader
    entry, owner = await add(
        "one",
        ExecutorResidentInventory(
            (
                ExecutorResidentOwner(
                    "context-one",
                    ("main", "ui"),
                    "main",
                    True,
                    False,
                    False,
                    transport_generation=generation,
                ),
            )
        ),
    )
    snapshot = ExecutorThreadInventorySnapshot(owner.inventory, (ids,))
    owner.prepare_resident_thread_inventory = AsyncMock(return_value=lambda: snapshot)
    client.headers.clear()
    assert (await client.get("/v1/startup/owner-inventory?read_id=sdk")).status_code == 401
    owner.prepare_resident_thread_inventory.assert_not_called()
    assert requests == []
    client.headers["Authorization"] = "Bearer runner-auth"
    body = (await client.get("/v1/startup/owner-inventory?read_id=sdk")).json()
    if generation is None and ids is not None:
        assert body["harness_thread_ids"] == body["harnesses"]["contexts"] == [None]
    else:
        assert body["harness_thread_ids"] == [[list(ids) if ids is not None else None]]
        observed = body["harnesses"]["contexts"][0]["inventory"]["owners"][0]
        assert observed["session_keys"] == ["main", "ui"]
        assert observed["transport_generation"] == generation
    assert body["executor_contexts_complete"] is body["external_owners_complete"] is False
    owner.prepare_resident_thread_inventory.assert_awaited_once_with(timeout_s=5.0)
    assert requests == [("one", "/v1/sessions/one/resident-thread-inventory")]
    assert manager._retention_managed == {"one": 0.0} and entry.last_used_at == 0.0


@pytest.mark.parametrize(
    "state",
    ["empty-manager", "spawning", "empty-owner", "unsupported", "failed", "closing", "legacy"],
)
async def test_runner_owner_inventory_keeps_unknown_distinct_from_empty(
    harness_inventory_reader, monkeypatch, state
):
    manager, add, client, requests = harness_inventory_reader
    if state == "spawning":
        monkeypatch.setattr(manager, "_spawning_sessions", {"preparing"}, raising=False)
    if state not in {"empty-manager", "spawning"}:
        inventory = ExecutorResidentInventory(owners=())
        if state == "unsupported":
            inventory = None
        elif state == "failed":
            inventory = RuntimeError("owner read failed")
        entry, _owner = await add("one", inventory)
        if state == "closing":
            manager._entries.pop("one")
            manager._closing_entries[entry.incarnation_id] = ("one", entry)
        elif state == "legacy":

            async def old_harness(response):
                response.status_code = 404

            entry.client.event_hooks["response"].append(old_harness)
    response = await client.get("/v1/startup/owner-inventory?read_id=unknown")
    assert response.status_code == 200
    body = response.json()
    assert body["executor_contexts_complete"] is False
    assert body["external_owners_complete"] is False
    if state in {"empty-manager", "spawning"}:
        assert body["harnesses"] == {
            "owners": [],
            "exit_coverage_complete": state == "empty-manager",
            "contexts": [],
        }
    elif state == "empty-owner":
        assert body["harnesses"]["contexts"][0]["inventory"] == {
            "owners": [],
            "ambiguous_context_ids": [],
            "aliases_complete": False,
            "context_coverage_complete": False,
        }
    else:
        assert body["harnesses"]["contexts"] == [None]
        assert body["harnesses"]["owners"][0]["closing"] is (state == "closing")
    if state in {"closing", "empty-manager", "spawning"}:
        assert requests == []
    else:
        assert requests == [("one", "/v1/sessions/one/resident-thread-inventory")]
    assert body["harness_thread_ids"] == ([] if state in {"empty-manager", "spawning"} else [None])


@pytest.mark.parametrize(
    "change", ["release", "replacement", "closing", "generation", "failed-release"]
)
async def test_runner_owner_inventory_reconciles_earlier_read_after_later_owner_waits(
    harness_inventory_reader, monkeypatch, change
):
    manager, add, client, _requests = harness_inventory_reader
    first, _ = await add("first", ExecutorResidentInventory(owners=()))
    second, _ = await add("second", ExecutorResidentInventory(owners=()))
    first_read, second_waiting, resume = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_read = manager.prepare_resident_thread_inventory

    async def observed_read(owner, **kwargs):
        result = await original_read(owner, **kwargs)
        if owner.session_id == "first":
            assert result() is not None
            first_read.set()
        return result

    async def wait_for_second(_response):
        second_waiting.set()
        await asyncio.wait_for(resume.wait(), timeout=2)

    monkeypatch.setattr(manager, "prepare_resident_thread_inventory", observed_read)
    second.client.event_hooks["response"].append(wait_for_second)
    read = asyncio.create_task(client.get("/v1/startup/owner-inventory?read_id=race"))
    release = None
    finish_close = asyncio.Event()
    try:
        await asyncio.wait_for(first_read.wait(), timeout=2)
        await asyncio.wait_for(second_waiting.wait(), timeout=2)
        if change == "generation":
            manager._release_generations["first"] = 1
        else:
            monkeypatch.setattr(manager, "_retention_expiry", lambda _session: None)
            if change == "failed-release":
                monkeypatch.setattr(first.client, "aclose", AsyncMock())
                monkeypatch.setattr(first.process, "terminate", lambda: None)
                monkeypatch.setattr(first.process, "kill", lambda: None)
                monkeypatch.setattr(
                    first.process, "wait", AsyncMock(side_effect=RuntimeError("still alive"))
                )
                assert await manager.release("first") is False
                assert manager._entries["first"] is first
                assert manager._release_generations["first"] == 0
                assert not first.client.is_closed
            elif change == "closing":
                close_started = asyncio.Event()
                original_close = first.client.aclose

                async def pause_close():
                    close_started.set()
                    await asyncio.wait_for(finish_close.wait(), timeout=2)
                    await original_close()

                monkeypatch.setattr(first.client, "aclose", pause_close)
                release = asyncio.create_task(manager.release("first"))
                await asyncio.wait_for(close_started.wait(), timeout=2)
            else:
                assert await manager.release("first") is True
                if change == "replacement":
                    replacement, _ = await add("first", ExecutorResidentInventory(owners=()))
                    replacement.incarnation_id = "replacement-first"
        resume.set()
        response = await asyncio.wait_for(read, timeout=2)
        assert response.status_code == 200
        harnesses = response.json()["harnesses"]
        owners = {
            owner["session_id"]: (owner, context)
            for owner, context in zip(harnesses["owners"], harnesses["contexts"], strict=True)
        }
        assert owners["second"][1]["inventory"]["owners"] == []
        if change == "release":
            assert "first" not in owners
        else:
            assert owners["first"][1] is None
            if change == "replacement":
                assert owners["first"][0]["incarnation_id"] == "replacement-first"
            elif change == "closing":
                assert owners["first"][0]["closing"] is True
    finally:
        resume.set()
        finish_close.set()
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)
        if release is not None:
            assert await asyncio.wait_for(release, timeout=2) is True


@pytest.mark.parametrize("end", ["complete", "timeout", "cancel"])
async def test_runner_owner_inventory_bounds_workers_and_drains_on_exit(
    harness_inventory_reader, monkeypatch, end
):
    from omnigent.runner import resident_inventory

    manager, add, client, _requests = harness_inventory_reader
    active = set()
    maximum = 0
    four_started, resume = asyncio.Event(), asyncio.Event()
    for index in range(6):
        entry, _ = await add(str(index), ExecutorResidentInventory(owners=()))

        async def pause(_response, key=str(index)):
            nonlocal maximum
            active.add(key)
            maximum = max(maximum, len(active))
            if len(active) == 4:
                four_started.set()
            try:
                await asyncio.wait_for(resume.wait(), timeout=2)
            finally:
                active.remove(key)

        entry.client.event_hooks["response"].append(pause)
    if end == "timeout":
        monkeypatch.setattr(resident_inventory, "_OWNER_INVENTORY_TIMEOUT_S", 0.1)
    read = asyncio.create_task(client.get("/v1/startup/owner-inventory?read_id=bounded"))
    try:
        await asyncio.wait_for(four_started.wait(), timeout=2)
        assert maximum == 4
        if end == "cancel":
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(read, timeout=2)
        else:
            if end == "complete":
                resume.set()
            response = await asyncio.wait_for(read, timeout=2)
            assert response.status_code == 200
            harnesses = response.json()["harnesses"]
            if end == "timeout":
                assert harnesses is None
            else:
                assert len(harnesses["owners"]) == 6
                assert all(
                    context["inventory"]["owners"] == [] for context in harnesses["contexts"]
                )
        assert active == set()
        assert maximum == 4
        assert all(not entry.client.is_closed for entry in manager._entries.values())
    finally:
        resume.set()
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)


@pytest.mark.parametrize("phase", ["selection", "reconciliation"])
async def test_runner_owner_inventory_deadline_includes_registry_waits(
    harness_inventory_reader, monkeypatch, phase
):
    from omnigent.runner import resident_inventory

    manager, add, client, requests = harness_inventory_reader
    await add("one", ExecutorResidentInventory(owners=()))
    monkeypatch.setattr(resident_inventory, "_OWNER_INVENTORY_TIMEOUT_S", 0.05)
    locked = asyncio.Event()
    if phase == "selection":
        await manager._registry_lock.acquire()
        locked.set()
    else:
        original_read = manager.prepare_resident_thread_inventory

        async def block_reconciliation(owner, **kwargs):
            result = await original_read(owner, **kwargs)
            assert result() is not None
            await manager._registry_lock.acquire()
            locked.set()
            return result

        monkeypatch.setattr(manager, "prepare_resident_thread_inventory", block_reconciliation)
    read = asyncio.create_task(client.get("/v1/startup/owner-inventory?read_id=registry-wait"))
    try:
        await asyncio.wait_for(locked.wait(), timeout=2)
        response = await asyncio.wait_for(read, timeout=2)
        assert response.status_code == 200
        assert response.json()["harnesses"] is None
        assert requests == (
            [] if phase == "selection" else [("one", "/v1/sessions/one/resident-thread-inventory")]
        )
    finally:
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)
        if locked.is_set():
            manager._registry_lock.release()


@pytest.mark.parametrize("bound", [False, True])
async def test_runner_inventory_transports_native_threads_after_auth(
    harness_inventory_reader, native_thread_inventory, bound
):
    _, _, client, _ = harness_inventory_reader
    backend, native = native_thread_inventory("main", bind=bound)
    orchestration._AUTO_CODEX_APP_SERVERS["ui"] = backend
    client.headers.clear()
    assert (await client.get("/v1/startup/owner-inventory?read_id=native")).status_code == 401
    native.read_loaded_thread_ids.assert_not_called()
    client.headers["Authorization"] = "Bearer runner-auth"
    response = await client.get("/v1/startup/owner-inventory?read_id=native")
    assert response.status_code == 200
    assert response.json().get("native_thread_ids") == ([["thread-one"]] if bound else [None])
    assert response.json()["native_backends"]["owners"][0]["session_ids"] == ["main", "ui"]
    assert response.json()["executor_contexts_complete"] is False
    assert response.json()["external_owners_complete"] is False
    assert native.read_loaded_thread_ids.await_count == int(bound)


@pytest.mark.parametrize(
    "change", ["none", "rebind", "backend", "generation", "incarnation", "closing", "observation"]
)
async def test_runner_inventory_rechecks_native_after_harness_reconciliation_wait(
    harness_inventory_reader, native_thread_inventory, monkeypatch, change
):
    manager, add, client, _ = harness_inventory_reader
    await add("harness", ExecutorResidentInventory(owners=()))
    backend, native = native_thread_inventory("native")
    entered, resume = asyncio.Event(), asyncio.Event()
    original = manager.resident_inventory

    async def paused(**kwargs):
        if "context_reads" in kwargs:
            entered.set()
            await resume.wait()
        return await original(**kwargs)

    monkeypatch.setattr(manager, "resident_inventory", paused)
    read = asyncio.create_task(client.get("/v1/startup/owner-inventory?read_id=late-native"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert native.read_loaded_thread_ids.await_count == 1
        if change in {"rebind", "backend"}:
            if change == "backend":
                backend = SimpleNamespace(**vars(backend))
                orchestration._AUTO_CODEX_APP_SERVERS["native"] = backend
            orchestration._bind_codex_inventory_client(
                "native", backend, native, incarnation_id="backend-native"
            )
        elif change == "generation":
            native.resident_connection_generation = 4
        elif change == "observation":
            native.resident_thread_observation_revision = 1
        elif change == "incarnation":
            backend.resident_incarnation_id = "restarted-native"
        elif change == "closing":
            orchestration._CLOSING_NATIVE_BACKENDS[object()] = orchestration._ClosingNativeBackend(
                "backend-native", "native", "codex-native", backend
            )
        resume.set()
        body = (await asyncio.wait_for(read, 2)).json()
        assert body["native_thread_ids"] == ([["thread-one"]] if change == "none" else [None])
        assert body["harnesses"]["contexts"][0]["inventory"]["owners"] == []
    finally:
        resume.set()
        read.cancel()
        await asyncio.gather(read, return_exceptions=True)


@pytest.mark.parametrize("change", ["none", "generation", "teardown", "client", "process"])
@pytest.mark.parametrize("phase", ["native", "registry"])
async def test_runner_inventory_rechecks_harness_after_native_wait(
    harness_inventory_reader, native_thread_inventory, monkeypatch, change, phase
):
    from omnigent.inner.executor import ExecutorThreadInventorySnapshot

    manager, add, client, _ = harness_inventory_reader
    entry, owner = await add(
        "harness",
        ExecutorResidentInventory(
            (
                ExecutorResidentOwner(
                    "sdk-one", ("main",), "sdk", True, False, False, transport_generation=0
                ),
            )
        ),
    )
    snapshot = ExecutorThreadInventorySnapshot(owner.inventory, (("sdk",),))
    owner.prepare_resident_thread_inventory = AsyncMock(return_value=lambda: snapshot)
    _, native = native_thread_inventory("native")
    entered, resume = asyncio.Event(), asyncio.Event()

    async def paused(**kwargs):
        entered.set()
        await resume.wait()
        return ("thread-one",)

    if phase == "native":
        native.read_loaded_thread_ids.side_effect = paused
    else:
        original = manager.resident_inventory

        async def wait_for_registry(**kwargs):
            if "context_reads" in kwargs:
                entered.set()
                await resume.wait()
            return await original(**kwargs)

        monkeypatch.setattr(manager, "resident_inventory", wait_for_registry)
    read = asyncio.create_task(client.get("/v1/startup/owner-inventory?read_id=late-harness"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if change == "generation":
            manager._release_generations["harness"] = 1
        elif change == "teardown":
            entry.teardown_started = True
        elif change == "client":
            entry.client = SimpleNamespace(is_closed=False)
        elif change == "process":
            entry.process = SimpleNamespace(returncode=None)
        resume.set()
        body = (await asyncio.wait_for(read, 2)).json()
        assert body["native_thread_ids"] == [["thread-one"]]
        context = body["harnesses"]["contexts"][0]
        assert (context is None) is (change != "none")
        assert body["harness_thread_ids"] == ([[["sdk"]]] if change == "none" else [None])
    finally:
        resume.set()
        read.cancel()
        await asyncio.gather(read, return_exceptions=True)


async def test_runner_native_inventory_total_deadline_drains_all_sources(
    harness_inventory_reader, native_thread_inventory, monkeypatch
):
    from omnigent.runner import resident_inventory

    _, add, client, _ = harness_inventory_reader
    await add("harness", ExecutorResidentInventory(owners=()))
    _, native = native_thread_inventory("native")
    drained = asyncio.Event()

    async def paused(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    native.read_loaded_thread_ids.side_effect = paused
    monkeypatch.setattr(resident_inventory, "_OWNER_INVENTORY_TIMEOUT_S", 0.05)
    body = (
        await asyncio.wait_for(client.get("/v1/startup/owner-inventory?read_id=deadline"), 2)
    ).json()
    assert body["native_thread_ids"] is None and body["harnesses"] is None
    assert drained.is_set()
