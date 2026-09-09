"""Durable CLI release intent expansion, leasing, and retry tests."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event

from omnigent.cli_retention import CliRetentionPolicy
from omnigent.db.db_models import SqlConversation, current_workspace_id
from omnigent.server.archive_close import ArchiveCloseCoordinator
from omnigent.server.cli_release_store import CliReleaseIntentStore
from omnigent.server.cli_retention import (
    CliRetentionCoordinator,
    CliRetentionHostLeaseBusy,
    CliRetentionHostLeaseLost,
)
from omnigent.stores.conversation_store import ConversationArchiveClosingError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore


class _Registry:
    def get(self, _host_id: str):
        return None


class _Response:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.status_code = 200
        self.text = ""
        self._payload = payload or {"status": "released"}

    def json(self):
        return self._payload


class _RunnerClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []

    async def post(self, url: str, *, json: dict[str, object], timeout: float):
        del timeout
        self.posts.append((url, json))
        return _Response()


class _RunnerRouter:
    def __init__(self, client: _RunnerClient, *, online: bool) -> None:
        self.client = client
        self.online = online

    def runner_is_online(self, _runner_id: str) -> bool:
        return self.online

    def client_for_session_resources(self, _session_id: str, *, conversation=None):
        if not self.online:
            raise RuntimeError("runner offline")
        return SimpleNamespace(runner_id=conversation.runner_id, client=self.client)


def test_archive_transition_and_close_request_are_atomic(
    db_uri: str,
) -> None:
    conversation_store = SqlAlchemyConversationStore(db_uri)
    conversation = conversation_store.create_conversation()

    archived = conversation_store.update_conversation(
        conversation.id,
        archived=True,
        close_cli_on_archive=True,
    )
    assert archived is not None
    assert archived.archived is True
    assert archived.archive_revision == 1
    assert archived.archive_close_requested_revision == 1
    assert [row.id for row in conversation_store.list_pending_archive_closes()] == [
        conversation.id
    ]

    unarchived = conversation_store.update_conversation(conversation.id, archived=False)
    assert unarchived is not None
    assert unarchived.archive_revision == 2
    assert unarchived.archive_close_requested_revision is None
    assert conversation_store.list_pending_archive_closes() == []


def test_archived_root_atomically_rejects_new_child(db_uri: str) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation()
    conversations.update_conversation(root.id, archived=True, close_cli_on_archive=True)

    with pytest.raises(ConversationArchiveClosingError):
        conversations.create_conversation(parent_conversation_id=root.id)


def test_completed_archive_still_rejects_new_child(db_uri: str) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation()
    archived = conversations.update_conversation(root.id, archived=True, close_cli_on_archive=True)
    assert archived is not None
    assert (
        conversations.claim_archive_close(
            root.id,
            archived.archive_revision,
            "worker",
            claimed_at=100,
            stale_before=0,
        )
        == "claimed"
    )
    assert conversations.complete_archive_close(
        root.id,
        archived.archive_revision,
        "worker",
    )

    with pytest.raises(ConversationArchiveClosingError):
        conversations.create_conversation(parent_conversation_id=root.id)


def test_archived_intermediate_session_rejects_new_descendant(db_uri: str) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation()
    child = conversations.create_conversation(parent_conversation_id=root.id)
    conversations.update_conversation(
        child.id,
        archived=True,
        close_cli_on_archive=True,
    )

    with pytest.raises(ConversationArchiveClosingError):
        conversations.create_conversation(parent_conversation_id=child.id)


@pytest.mark.databricks
def test_postgres_child_create_waits_for_archived_ancestor_lock(db_uri: str) -> None:
    """A child INSERT waits for an ancestor archive commit, then rejects it."""
    archive_store = SqlAlchemyConversationStore(db_uri)
    if archive_store._conv_engine.dialect.name != "postgresql":
        pytest.skip("row-lock ordering requires PostgreSQL")

    create_store = SqlAlchemyConversationStore(db_uri)
    root = archive_store.create_conversation()
    middle = archive_store.create_conversation(parent_conversation_id=root.id)
    archive_locked = threading.Event()
    allow_archive_commit = threading.Event()
    root_lock_attempted = threading.Event()
    attempts_lock = threading.Lock()
    for_update_attempts = 0

    def _observe_for_update(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        nonlocal for_update_attempts
        if "FOR UPDATE" not in statement.upper():
            return
        with attempts_lock:
            for_update_attempts += 1
            # The new descendant locks its direct parent first and then the
            # archived root. The second statement is the one that must wait.
            if for_update_attempts >= 2:
                root_lock_attempted.set()

    def _hold_archived_root_until_released() -> None:
        with archive_store._conv_session_immediate("test_postgres_archive_lock") as session:
            row = session.get(
                SqlConversation,
                (current_workspace_id(), root.id),
            )
            assert row is not None
            row.archived = True
            row.archive_revision += 1
            row.archive_close_requested_revision = row.archive_revision
            session.flush()
            archive_locked.set()
            assert allow_archive_commit.wait(5), "archive commit was never released"

    event.listen(create_store._conv_engine, "before_cursor_execute", _observe_for_update)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        archive_future = pool.submit(_hold_archived_root_until_released)
        assert archive_locked.wait(5), "archive transaction did not acquire its row lock"

        create_future = pool.submit(
            create_store.create_conversation,
            parent_conversation_id=middle.id,
        )
        assert root_lock_attempted.wait(5), "child create never reached the root row lock"
        assert not create_future.done(), "child create bypassed the uncommitted archive lock"

        allow_archive_commit.set()
        archive_future.result(timeout=5)
        with pytest.raises(ConversationArchiveClosingError):
            create_future.result(timeout=5)
    finally:
        allow_archive_commit.set()
        pool.shutdown(wait=True, cancel_futures=True)
        event.remove(
            create_store._conv_engine,
            "before_cursor_execute",
            _observe_for_update,
        )


def test_archive_without_close_allows_inflight_child_creation(db_uri: str) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation()
    conversations.update_conversation(
        root.id,
        archived=True,
        close_cli_on_archive=False,
    )

    child = conversations.create_conversation(parent_conversation_id=root.id)

    assert child.parent_conversation_id == root.id


def test_unarchive_keeps_inflight_close_lease_until_worker_releases(
    db_uri: str,
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation()
    archived = conversations.update_conversation(root.id, archived=True, close_cli_on_archive=True)
    assert archived is not None
    assert (
        conversations.claim_archive_close(
            root.id,
            archived.archive_revision,
            "worker",
            claimed_at=100,
            stale_before=0,
        )
        == "claimed"
    )

    unarchived = conversations.update_conversation(root.id, archived=False)
    assert unarchived is not None
    assert unarchived.archive_close_claimed is True
    assert conversations.release_archive_close_claim(root.id, archived.archive_revision, "worker")
    settled = conversations.get_conversation(root.id)
    assert settled is not None
    assert settled.archive_close_claimed is False


def test_target_intent_claim_is_single_owner_and_stale_reclaimable(db_uri: str) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    target = conversations.create_conversation()
    store_a = CliReleaseIntentStore(db_uri)
    store_b = CliReleaseIntentStore(db_uri)
    intent = store_a.ensure_archive_targets(target.id, 1, [target])[0]

    assert store_a.claim(intent.id, "owner-a", claimed_at=100, stale_before=0)
    assert not store_b.claim(intent.id, "owner-b", claimed_at=101, stale_before=99)
    assert store_b.claim(intent.id, "owner-b", claimed_at=1000, stale_before=500)
    assert not store_a.complete(intent.id, "owner-a")
    assert store_b.complete(intent.id, "owner-b")


def test_archive_root_claim_renewal_prevents_stale_takeover(db_uri: str) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation()
    archived = conversations.update_conversation(root.id, archived=True, close_cli_on_archive=True)
    assert archived is not None
    assert (
        conversations.claim_archive_close(
            root.id,
            archived.archive_revision,
            "owner-a",
            claimed_at=100,
            stale_before=0,
        )
        == "claimed"
    )

    assert conversations.renew_archive_close_claim(
        root.id,
        "owner-a",
        claimed_at=900,
    )
    assert (
        conversations.claim_archive_close(
            root.id,
            archived.archive_revision,
            "owner-b",
            claimed_at=1000,
            stale_before=500,
        )
        == "busy"
    )


def test_only_last_archive_target_on_shared_runner_is_ready_to_stop_it(
    db_uri: str,
) -> None:
    host_id = "a1b2c3d4e5f61234567890abcdef0123"
    runner_id = "b1b2c3d4e5f61234567890abcdef0123"
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation()
    child = conversations.create_conversation(parent_conversation_id=root.id)
    conversations.set_host_id(root.id, host_id, workspace="C:\\root")
    conversations.set_runner_id(root.id, runner_id)
    conversations.set_host_id(child.id, host_id, workspace="C:\\child")
    conversations.set_runner_id(child.id, runner_id)
    bound_root = conversations.get_conversation(root.id)
    bound_child = conversations.get_conversation(child.id)
    assert bound_root is not None and bound_child is not None
    intents = CliReleaseIntentStore(db_uri)
    first, second = intents.ensure_archive_targets(root.id, 1, [bound_root, bound_child])
    assert not intents.archive_binding_ready_to_stop(
        root_session_id=root.id,
        revision=1,
        current_intent_id=first.id,
        host_id=host_id,
        runner_id=runner_id,
    )
    assert intents.claim(first.id, "first", claimed_at=100, stale_before=0)
    assert intents.complete(first.id, "first")
    assert intents.archive_binding_ready_to_stop(
        root_session_id=root.id,
        revision=1,
        current_intent_id=second.id,
        host_id=host_id,
        runner_id=runner_id,
    )


@pytest.mark.asyncio
async def test_shared_runner_stops_only_after_every_target_cli_released(
    db_uri: str,
) -> None:
    host_id = "c1b2c3d4e5f61234567890abcdef0123"
    runner_id = "d1b2c3d4e5f61234567890abcdef0123"
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation()
    child = conversations.create_conversation(parent_conversation_id=root.id)
    for conversation, workspace in ((root, "C:\\root"), (child, "C:\\child")):
        conversations.set_host_id(conversation.id, host_id, workspace=workspace)
        conversations.set_runner_id(conversation.id, runner_id)
    bound_root = conversations.get_conversation(root.id)
    bound_child = conversations.get_conversation(child.id)
    assert bound_root is not None and bound_child is not None

    intents = CliReleaseIntentStore(db_uri)
    first, second = intents.ensure_archive_targets(root.id, 1, [bound_root, bound_child])
    client = _RunnerClient()
    coordinator = ArchiveCloseCoordinator(
        conversation_store=conversations,
        host_store=None,
        host_registry=_Registry(),
        runner_router=_RunnerRouter(client, online=True),
        intent_store=intents,
        scan_interval_seconds=3600,
    )
    stop_runner = AsyncMock(return_value=True)

    from omnigent.server.routes import sessions as sessions_facade

    with patch.object(sessions_facade, "_stop_session_host_runner", stop_runner):
        assert await coordinator._execute_intent(first) == "completed"
        stop_runner.assert_not_awaited()
        assert intents.claim(first.id, "first", claimed_at=100, stale_before=0)
        assert intents.complete(first.id, "first")

        assert await coordinator._execute_intent(second) == "completed"

    stop_runner.assert_awaited_once()
    assert [url for url, _payload in client.posts] == [
        f"/v1/sessions/{root.id}/cli-retention/release",
        f"/v1/sessions/{child.id}/cli-retention/release",
    ]


def test_host_pool_selection_lease_is_single_owner_without_touching_liveness(
    db_uri: str,
) -> None:
    host_id = "d1b2c3d4e5f61234567890abcdef0123"
    first = HostStore(db_uri)
    second = HostStore(db_uri)
    host = first.upsert_on_connect(host_id, "host", "local")

    assert first.claim_cli_retention(host_id, "owner-a", claimed_at=100, stale_before=0)
    assert not second.claim_cli_retention(host_id, "owner-b", claimed_at=101, stale_before=99)
    assert first.get_host(host_id).updated_at == host.updated_at  # type: ignore[union-attr]
    assert not second.release_cli_retention(host_id, "owner-b")
    assert first.release_cli_retention(host_id, "owner-a")
    assert second.claim_cli_retention(host_id, "owner-b", claimed_at=102, stale_before=0)


@pytest.mark.asyncio
async def test_coordinator_host_lease_serializes_across_server_replicas(
    db_uri: str,
) -> None:
    host_id = "e1b2c3d4e5f61234567890abcdef0123"
    first_store = HostStore(db_uri)
    second_store = HostStore(db_uri)
    first_store.upsert_on_connect(host_id, "host", "local")
    first = CliRetentionCoordinator(
        host_store=first_store,
        conversation_store=SimpleNamespace(),
        runner_router=SimpleNamespace(),
    )
    second = CliRetentionCoordinator(
        host_store=second_store,
        conversation_store=SimpleNamespace(),
        runner_router=SimpleNamespace(),
    )

    async with first.lease_for_host(host_id, wait_timeout_s=0):
        with pytest.raises(CliRetentionHostLeaseBusy):
            async with second.lease_for_host(host_id, wait_timeout_s=0):
                raise AssertionError("second replica unexpectedly acquired the Host lease")

    async with second.lease_for_host(host_id, wait_timeout_s=0):
        pass


@pytest.mark.asyncio
async def test_host_lease_heartbeat_cancels_owner_when_renewal_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server import cli_retention as retention_module

    real_sleep = asyncio.sleep

    async def _yield_once(_seconds: float) -> None:
        await real_sleep(0)

    class _HostStore:
        released = False

        def claim_cli_retention(self, *_args, **_kwargs):
            return True

        def renew_cli_retention(self, *_args, **_kwargs):
            return False

        def release_cli_retention(self, *_args, **_kwargs):
            self.released = True
            return False

    hosts = _HostStore()
    coordinator = CliRetentionCoordinator(
        host_store=hosts,
        conversation_store=SimpleNamespace(),
        runner_router=SimpleNamespace(),
    )
    monkeypatch.setattr(retention_module.asyncio, "sleep", _yield_once)

    async def _own_lease() -> None:
        async with coordinator.lease_for_host("host-a", wait_timeout_s=0):
            await asyncio.Event().wait()

    owner = asyncio.create_task(_own_lease())
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert hosts.released is True


@pytest.mark.asyncio
async def test_lost_host_lease_blocks_idle_release_command() -> None:
    class _HostStore:
        def get_host(self, host_id):
            assert host_id == "host-a"
            return SimpleNamespace(
                cli_retention_policy=CliRetentionPolicy(),
                cli_retention_revision=4,
            )

    class _Lease:
        async def ensure_owned(self):
            raise CliRetentionHostLeaseLost("host-a")

    class _NeverRouter:
        def client_for_session_resources(self, *_args, **_kwargs):
            raise AssertionError("lost lease must not issue a Runner release")

    coordinator = ArchiveCloseCoordinator(
        conversation_store=SimpleNamespace(),
        host_store=_HostStore(),
        host_registry=_Registry(),
        runner_router=_NeverRouter(),
        intent_store=SimpleNamespace(),  # type: ignore[arg-type]
    )
    intent = SimpleNamespace(
        host_id="host-a",
        policy_revision=4,
        target_session_id="session-a",
        idle_threshold_seconds=60,
        activity_token="activity",
        runtime_generation="boot:1",
    )

    with pytest.raises(CliRetentionHostLeaseLost):
        await coordinator._execute_idle_intent_locked(
            intent,
            SimpleNamespace(id="session-a", runner_id="runner-a"),
            lease=_Lease(),
        )


@pytest.mark.asyncio
async def test_lost_intent_lease_blocks_archive_release_command() -> None:
    root = SimpleNamespace(
        id="root",
        archived=True,
        archive_revision=2,
        archive_close_requested_revision=2,
    )

    class _ConversationStore:
        def get_conversation(self, conversation_id):
            assert conversation_id == "root"
            return root

        def claim_archive_close(self, *_args, **_kwargs):
            return "claimed"

        def renew_archive_close_claim(self, *_args, **_kwargs):
            return True

        def release_archive_close_claim(self, *_args, **_kwargs):
            return True

    class _IntentStore:
        def claim(self, *_args, **_kwargs):
            return True

        def renew(self, *_args, **_kwargs):
            return False

        def retry(self, *_args, **_kwargs):
            return False

    class _NeverRouter:
        def client_for_session_resources(self, *_args, **_kwargs):
            raise AssertionError("lost intent lease must not issue an archive release")

    coordinator = ArchiveCloseCoordinator(
        conversation_store=_ConversationStore(),
        host_store=None,
        host_registry=_Registry(),
        runner_router=_NeverRouter(),
        intent_store=_IntentStore(),  # type: ignore[arg-type]
    )
    intent = SimpleNamespace(
        id="intent-a",
        reason="archive",
        root_session_id="root",
        target_session_id="root",
        host_id=None,
        runner_id="runner-a",
        archive_revision=2,
    )

    with pytest.raises(asyncio.CancelledError):
        await coordinator._process_intent((0, "intent-a"), intent)


@pytest.mark.asyncio
async def test_reconnect_reset_rechecks_policy_after_acquiring_host_lease(
    db_uri: str,
) -> None:
    host_id = "f1b2c3d4e5f61234567890abcdef0123"
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(host_id, "host", "local")
    hosts.replace_cli_retention_policy(
        host_id,
        CliRetentionPolicy(
            idle_threshold_minutes=30,
            max_idle_clis=5,
            close_on_archive=True,
        ),
        expected_revision=0,
    )

    class _NeverRouter:
        def client_for_session_resources(self, *_args, **_kwargs):
            raise AssertionError("stale reset must not reach a Runner")

    coordinator = CliRetentionCoordinator(
        host_store=hosts,
        conversation_store=SimpleNamespace(),
        runner_router=_NeverRouter(),
    )

    assert await coordinator.reset_host(host_id, policy_revision=0) is None


@pytest.mark.asyncio
async def test_archive_intent_survives_failed_worker_and_new_coordinator(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server import archive_close as archive_module

    monkeypatch.setattr(archive_module, "_RETRY_DELAY_S", 0)
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation(runner_id="b1b2c3d4e5f61234567890abcdef0123")
    archived = conversations.update_conversation(root.id, archived=True, close_cli_on_archive=True)
    assert archived is not None
    intents = CliReleaseIntentStore(db_uri)
    client = _RunnerClient()

    first = ArchiveCloseCoordinator(
        conversation_store=conversations,
        host_store=None,
        host_registry=_Registry(),
        runner_router=_RunnerRouter(client, online=False),
        intent_store=intents,
        scan_interval_seconds=3600,
    )
    first.trigger(root.id)
    await first.wait_for_idle()
    still_pending = conversations.get_conversation(root.id)
    assert still_pending is not None
    assert still_pending.archive_close_completed_revision is None

    second = ArchiveCloseCoordinator(
        conversation_store=conversations,
        host_store=None,
        host_registry=_Registry(),
        runner_router=_RunnerRouter(client, online=True),
        intent_store=intents,
        scan_interval_seconds=3600,
    )
    second.trigger_pending()
    await second.wait_for_idle()

    completed = conversations.get_conversation(root.id)
    assert completed is not None
    assert completed.archive_close_completed_revision == completed.archive_revision == 1
    assert client.posts == [
        (
            f"/v1/sessions/{root.id}/cli-retention/release",
            {
                "reason": "archive",
                "archive_scope_id": root.id,
                "archive_revision": 1,
            },
        )
    ]


@pytest.mark.asyncio
async def test_parent_archive_uses_parent_scope_after_child_revision_advanced(
    db_uri: str,
) -> None:
    runner_id = "e2b2c3d4e5f61234567890abcdef0123"
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation(runner_id=runner_id)
    child = conversations.create_conversation(
        parent_conversation_id=root.id,
        runner_id=runner_id,
    )
    conversations.update_conversation(child.id, archived=True, close_cli_on_archive=True)
    advanced_child = conversations.update_conversation(child.id, archived=False)
    assert advanced_child is not None and advanced_child.archive_revision == 2
    archived_root = conversations.update_conversation(
        root.id, archived=True, close_cli_on_archive=True
    )
    assert archived_root is not None and archived_root.archive_revision == 1

    client = _RunnerClient()
    coordinator = ArchiveCloseCoordinator(
        conversation_store=conversations,
        host_store=None,
        host_registry=_Registry(),
        runner_router=_RunnerRouter(client, online=True),
        intent_store=CliReleaseIntentStore(db_uri),
        scan_interval_seconds=3600,
    )
    coordinator.trigger(root.id)
    await coordinator.wait_for_idle()

    assert {url for url, _payload in client.posts} == {
        f"/v1/sessions/{root.id}/cli-retention/release",
        f"/v1/sessions/{child.id}/cli-retention/release",
    }
    assert all(
        payload
        == {
            "reason": "archive",
            "archive_scope_id": root.id,
            "archive_revision": 1,
        }
        for _url, payload in client.posts
    )


@pytest.mark.asyncio
async def test_periodic_release_scan_survives_transient_database_failure() -> None:
    coordinator = ArchiveCloseCoordinator(
        conversation_store=SimpleNamespace(),
        host_store=None,
        host_registry=_Registry(),
        runner_router=SimpleNamespace(),
        intent_store=SimpleNamespace(),  # type: ignore[arg-type]
        scan_interval_seconds=0.01,
    )
    calls = 0
    recovered = asyncio.Event()

    def _scan() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database outage")
        recovered.set()

    coordinator._trigger_all_workspaces = _scan  # type: ignore[method-assign]
    task = asyncio.create_task(coordinator._scan_loop())
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_idle_pool_reserves_and_executes_only_oldest_excess_intent(
    db_uri: str,
) -> None:
    host_id = "a1b2c3d4e5f61234567890abcdef0123"
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(host_id, "host", "local")
    hosts.replace_cli_retention_policy(
        host_id,
        CliRetentionPolicy(
            idle_threshold_minutes=1,
            max_idle_clis=1,
            close_on_archive=True,
        ),
        expected_revision=0,
    )
    conversations = SqlAlchemyConversationStore(db_uri)
    old = conversations.create_conversation(
        host_id=host_id,
        runner_id="b1b2c3d4e5f61234567890abcdef0123",
        workspace="C:\\workspace-old",
    )
    new = conversations.create_conversation(
        host_id=host_id,
        runner_id="c1b2c3d4e5f61234567890abcdef0123",
        workspace="C:\\workspace-new",
    )
    posts: list[str] = []

    class _Client:
        def __init__(self, session_id: str) -> None:
            self.session_id = session_id

        async def get(self, _url: str, *, params, timeout: float):
            del timeout
            idle = 300.0 if self.session_id == old.id else 120.0
            return _Response(
                {
                    "session_id": self.session_id,
                    "present": True,
                    "supported": True,
                    "busy": False,
                    "eligible": True,
                    "family": "claude",
                    "idle_seconds": idle,
                    "activity_token": f"activity:{self.session_id}",
                    "runtime_generation": f"boot:{self.session_id}",
                    "host_id": params["host_id"],
                    "policy_revision": params["policy_revision"],
                }
            )

        async def post(self, url: str, *, json, timeout: float):
            del json, timeout
            posts.append(url)
            return _Response()

    clients = {old.id: _Client(old.id), new.id: _Client(new.id)}

    class _Router:
        def runner_is_online(self, _runner_id: str) -> bool:
            return True

        def client_for_session_resources(self, session_id: str, *, conversation=None):
            return SimpleNamespace(
                runner_id=conversation.runner_id,
                client=clients[session_id],
            )

    class _HostRegistry:
        connection = object()

        def get(self, _host_id: str):
            return self.connection

    registry = _HostRegistry()
    intents = CliReleaseIntentStore(db_uri)
    releases = ArchiveCloseCoordinator(
        conversation_store=conversations,
        host_store=hosts,
        host_registry=registry,
        runner_router=_Router(),
        intent_store=intents,
        scan_interval_seconds=3600,
    )
    pools = CliRetentionCoordinator(
        host_store=hosts,
        conversation_store=conversations,
        runner_router=_Router(),
        intent_store=intents,
        release_coordinator=releases,
        host_registry=registry,
        scan_interval_seconds=3600,
    )

    result = await pools.reconcile_host_once(host_id)
    await releases.wait_for_idle()

    assert result["scheduled"] == [old.id]
    assert posts == [f"/v1/sessions/{old.id}/cli-retention/release"]
    assert intents.list_due(now=2_147_483_647) == []


@pytest.mark.asyncio
async def test_conversation_delete_removes_release_intents_for_its_subtree(
    db_uri: str,
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation()
    child = conversations.create_conversation(parent_conversation_id=root.id)
    intents = CliReleaseIntentStore(db_uri)
    intents.ensure_archive_targets(root.id, 1, [root, child])

    assert len(intents.list_due(now=2_147_483_647)) == 2
    assert await conversations.delete_conversation(root.id)
    assert intents.list_due(now=2_147_483_647) == []
