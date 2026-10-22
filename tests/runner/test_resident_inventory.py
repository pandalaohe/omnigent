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
        }


@pytest.mark.asyncio
async def test_reader_keeps_shared_backend_aliases_as_evidence_without_probing(monkeypatch):
    backend = SimpleNamespace(
        resident_incarnation_id="backend-one",
        process_exit_confirmed=False,
        exit_confirmed=False,
        close=Mock(side_effect=AssertionError("read must not close")),
        is_alive=Mock(side_effect=AssertionError("read must not probe")),
    )
    monkeypatch.setattr(
        orchestration,
        "_AUTO_CODEX_APP_SERVERS",
        {
            "session-one": backend,
            "ui-alias": backend,
        },
    )
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    app = create_runner_app(server_client=NullServerClient())
    async with _runner_client(app) as client:
        response = await client.get("/v1/startup/owner-inventory?read_id=owners")
    assert response.status_code == 200
    assert response.json()["native_backends"] == {
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
            }
        ],
        "exit_coverage_complete": True,
        "aliases_complete": False,
    }
    assert response.json()["executor_contexts_complete"] is False
    backend.close.assert_not_called()
    backend.is_alive.assert_not_called()


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


async def test_runner_owner_inventory_composes_registered_harness_contexts(
    harness_inventory_reader,
):
    manager, add, client, requests = harness_inventory_reader
    entry, _owner = await add(
        "one",
        ExecutorResidentInventory(
            owners=(
                ExecutorResidentOwner(
                    "context-one", ("main", "ui"), "thread-one", True, True, False
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
                            "closing": True,
                            "cleanup_complete": False,
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
    assert requests == [("one", "/v1/sessions/one/resident-inventory")]
    assert entry.last_used_at == 0.0
    assert manager._retention_managed == {"one": 0.0}
    assert manager._entries["one"] is entry


async def test_runner_owner_inventory_auth_precedes_harness_reads(harness_inventory_reader):
    _manager, add, client, requests = harness_inventory_reader
    await add("one", ExecutorResidentInventory(owners=()))
    client.headers.clear()
    assert (await client.get("/v1/startup/owner-inventory?read_id=auth")).status_code == 401
    assert requests == []


@pytest.mark.parametrize(
    "state", ["empty-manager", "empty-owner", "unsupported", "failed", "closing"]
)
async def test_runner_owner_inventory_keeps_unknown_distinct_from_empty(
    harness_inventory_reader, state
):
    manager, add, client, requests = harness_inventory_reader
    if state != "empty-manager":
        inventory = ExecutorResidentInventory(owners=())
        if state == "unsupported":
            inventory = None
        elif state == "failed":
            inventory = RuntimeError("owner read failed")
        entry, _owner = await add("one", inventory)
        if state == "closing":
            manager._entries.pop("one")
            manager._closing_entries[entry.incarnation_id] = ("one", entry)
    response = await client.get("/v1/startup/owner-inventory?read_id=unknown")
    assert response.status_code == 200
    body = response.json()
    assert body["executor_contexts_complete"] is False
    assert body["external_owners_complete"] is False
    if state == "empty-manager":
        assert body["harnesses"] == {"owners": [], "exit_coverage_complete": True, "contexts": []}
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
    if state in {"closing", "empty-manager"}:
        assert requests == []


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
    original_read = manager.read_resident_inventory

    async def observed_read(owner, **kwargs):
        result = await original_read(owner, **kwargs)
        if owner.session_id == "first":
            assert result is not None
            first_read.set()
        return result

    async def wait_for_second(_response):
        second_waiting.set()
        await asyncio.wait_for(resume.wait(), timeout=2)

    monkeypatch.setattr(manager, "read_resident_inventory", observed_read)
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
        original_read = manager.read_resident_inventory

        async def block_reconciliation(owner, **kwargs):
            result = await original_read(owner, **kwargs)
            assert result is not None
            await manager._registry_lock.acquire()
            locked.set()
            return result

        monkeypatch.setattr(manager, "read_resident_inventory", block_reconciliation)
    read = asyncio.create_task(client.get("/v1/startup/owner-inventory?read_id=registry-wait"))
    try:
        await asyncio.wait_for(locked.wait(), timeout=2)
        response = await asyncio.wait_for(read, timeout=2)
        assert response.status_code == 200
        assert response.json()["harnesses"] is None
        assert requests == (
            [] if phase == "selection" else [("one", "/v1/sessions/one/resident-inventory")]
        )
    finally:
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)
        if locked.is_set():
            manager._registry_lock.release()
