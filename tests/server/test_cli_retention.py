"""Server coordination for Host-scoped idle CLI pools."""

from types import SimpleNamespace

import pytest

from omnigent.cli_retention import CliRetentionPolicy
from omnigent.entities.pagination import PagedList
from omnigent.server.cli_retention import (
    CliRetentionCoordinator,
    IdleCliSnapshot,
    select_idle_cli_overflow,
)
from omnigent.server.routes._sessions.orchestration import _archive_close_intents
from omnigent.server.routes.sessions.routes_events import _archive_blocks_external_user_work


def _snapshot(session_id: str, family: str, idle_seconds: float) -> IdleCliSnapshot:
    return IdleCliSnapshot(
        session_id=session_id,
        runner_id=f"runner-{session_id}",
        host_id="host-a",
        family=family,
        idle_seconds=idle_seconds,
        idle_since_monotonic=1_000 - idle_seconds,
        activity_token=f"token-{session_id}",
        runtime_generation=f"boot:{session_id}",
        policy_revision=0,
    )


def test_overflow_is_counted_separately_per_family_and_oldest_first() -> None:
    snapshots = [
        _snapshot("claude-old", "claude", 500),
        _snapshot("claude-mid", "claude", 400),
        _snapshot("claude-new", "claude", 300),
        _snapshot("codex-old", "codex", 800),
        _snapshot("codex-new", "codex", 700),
    ]

    selected = select_idle_cli_overflow(snapshots, max_idle_clis=2)

    assert [snapshot.session_id for snapshot in selected] == ["claude-old"]


def test_zero_retains_no_qualified_idle_clis() -> None:
    snapshots = [
        _snapshot("new", "claude", 60),
        _snapshot("old", "claude", 120),
    ]

    selected = select_idle_cli_overflow(snapshots, max_idle_clis=0)

    assert [snapshot.session_id for snapshot in selected] == ["old", "new"]


def test_oldest_selection_normalizes_different_observation_times() -> None:
    snapshots = [
        IdleCliSnapshot(
            session_id="actually-oldest",
            runner_id="runner-a",
            host_id="host-a",
            family="codex",
            idle_seconds=100,
            idle_since_monotonic=0,
            activity_token="a",
            runtime_generation="boot:1",
            policy_revision=0,
        ),
        IdleCliSnapshot(
            session_id="observed-later",
            runner_id="runner-b",
            host_id="host-a",
            family="codex",
            idle_seconds=150,
            idle_since_monotonic=50,
            activity_token="b",
            runtime_generation="boot:2",
            policy_revision=0,
        ),
    ]

    selected = select_idle_cli_overflow(snapshots, max_idle_clis=1)

    assert [snapshot.session_id for snapshot in selected] == ["actually-oldest"]


@pytest.mark.asyncio
async def test_coordinator_releases_only_oldest_excess_idle_family_member() -> None:
    conversations = [
        SimpleNamespace(id="claude-old", runner_id="runner-old", host_id="host-a"),
        SimpleNamespace(id="claude-new", runner_id="runner-new", host_id="host-a"),
        SimpleNamespace(id="claude-active", runner_id="runner-active", host_id="host-a"),
        SimpleNamespace(id="claude-recent", runner_id="runner-recent", host_id="host-a"),
        SimpleNamespace(id="codex-only", runner_id="runner-codex", host_id="host-a"),
    ]

    class _HostStore:
        def get_host(self, host_id):
            assert host_id == "host-a"
            return SimpleNamespace(
                cli_retention_revision=0,
                cli_retention_policy=CliRetentionPolicy(
                    idle_threshold_minutes=60,
                    max_idle_clis=1,
                    close_on_archive=True,
                ),
            )

    class _ConversationStore:
        def list_conversations(self, **kwargs):
            assert kwargs["host_id"] == "host-a"
            assert kwargs["include_archived"] is False
            return PagedList(data=conversations)

        def list_child_conversation_ids_by_parent(self, parent_ids):
            return {parent_id: [] for parent_id in parent_ids}

        def get_conversations(self, conversation_ids):
            assert conversation_ids == []
            return {}

    class _Response:
        def __init__(self, data, status_code=200):
            self._data = data
            self.status_code = status_code
            self.text = ""

        def json(self):
            return self._data

    class _Client:
        def __init__(self, session_id):
            self.session_id = session_id
            self.posts = []

        async def get(self, _url, *, params, timeout):
            del params, timeout
            family = "codex" if self.session_id == "codex-only" else "claude"
            idle = {
                "claude-old": 500.0,
                "claude-new": 300.0,
                "claude-active": 0.0,
                "claude-recent": 30.0,
                "codex-only": 700.0,
            }[self.session_id]
            busy = self.session_id == "claude-active"
            eligible = not busy and self.session_id != "claude-recent"
            return _Response(
                {
                    "session_id": self.session_id,
                    "present": True,
                    "supported": True,
                    "family": family,
                    "busy": busy,
                    "eligible": eligible,
                    "idle_seconds": idle,
                    "activity_token": f"token-{self.session_id}",
                    "runtime_generation": f"boot:{self.session_id}",
                    "host_id": "host-a",
                    "policy_revision": 0,
                }
            )

        async def post(self, url, *, json, timeout):
            del timeout
            self.posts.append((url, json))
            return _Response({"status": "released"})

    clients = {conversation.id: _Client(conversation.id) for conversation in conversations}

    class _Router:
        def client_for_session_resources(self, session_id, *, conversation):
            assert conversation.id == session_id
            return SimpleNamespace(client=clients[session_id], runner_id=conversation.runner_id)

    coordinator = CliRetentionCoordinator(
        host_store=_HostStore(),
        conversation_store=_ConversationStore(),
        runner_router=_Router(),
        scan_interval_seconds=60,
    )

    result = await coordinator.reconcile_host_once("host-a")

    assert result["families"] == {
        "claude": {"idle": 2, "active": 1, "below_threshold": 1, "total": 4},
        "codex": {"idle": 1, "active": 0, "below_threshold": 0, "total": 1},
    }
    assert result["released"] == ["claude-old"]
    assert len(clients["claude-old"].posts) == 1
    assert clients["claude-new"].posts == []
    assert clients["claude-active"].posts == []
    assert clients["claude-recent"].posts == []
    assert clients["codex-only"].posts == []


@pytest.mark.asyncio
async def test_archive_close_disabled_keeps_archived_clis_in_pool_scan() -> None:
    class _HostStore:
        def get_host(self, host_id):
            assert host_id == "host-a"
            return SimpleNamespace(
                cli_retention_revision=0,
                cli_retention_policy=CliRetentionPolicy(close_on_archive=False),
            )

    class _ConversationStore:
        def list_conversations(self, **kwargs):
            assert kwargs["include_archived"] is True
            return PagedList(data=[])

    coordinator = CliRetentionCoordinator(
        host_store=_HostStore(),
        conversation_store=_ConversationStore(),
        runner_router=SimpleNamespace(),
    )

    result = await coordinator.reconcile_host_once("host-a")

    assert result["configured"] is True
    assert result["families"] == {}
    assert result["released"] == []
    assert result["application"]["status"] == "applied"


@pytest.mark.asyncio
async def test_persisted_active_descendant_protects_idle_root_after_runner_restart() -> None:
    root = SimpleNamespace(id="root", runner_id="runner-root", host_id="host-a")
    child = SimpleNamespace(
        id="child",
        live_status="running",
        labels={},
    )

    class _HostStore:
        def get_host(self, host_id):
            assert host_id == "host-a"
            return SimpleNamespace(
                cli_retention_revision=3,
                cli_retention_policy=CliRetentionPolicy(max_idle_clis=0),
            )

    class _ConversationStore:
        def list_conversations(self, **kwargs):
            return PagedList(data=[root])

        def list_child_conversation_ids_by_parent(self, parent_ids):
            return {
                parent_id: ["child"] if parent_id == "root" else [] for parent_id in parent_ids
            }

        def get_conversations(self, conversation_ids):
            assert conversation_ids == ["child"]
            return {"child": child}

    class _Response:
        status_code = 200

        def json(self):
            return {
                "session_id": "root",
                "present": True,
                "supported": True,
                "family": "claude",
                "busy": False,
                "eligible": True,
                "idle_seconds": 7200.0,
                "activity_token": "root-idle",
                "runtime_generation": "boot:root",
                "host_id": "host-a",
                "policy_revision": 3,
            }

    class _Client:
        posts = []

        async def get(self, _url, *, params, timeout):
            del params, timeout
            return _Response()

        async def post(self, url, *, json, timeout):
            self.posts.append((url, json, timeout))
            return _Response()

    client = _Client()

    class _Router:
        def client_for_session_resources(self, session_id, *, conversation):
            assert session_id == conversation.id == "root"
            return SimpleNamespace(client=client, runner_id="runner-root")

    coordinator = CliRetentionCoordinator(
        host_store=_HostStore(),
        conversation_store=_ConversationStore(),
        runner_router=_Router(),
    )

    result = await coordinator.reconcile_host_once("host-a")

    assert result["families"] == {
        "claude": {"idle": 0, "active": 1, "below_threshold": 0, "total": 1}
    }
    assert result["released"] == []
    assert client.posts == []


@pytest.mark.asyncio
async def test_reset_host_reports_reachable_and_unavailable_sessions() -> None:
    conversations = [
        SimpleNamespace(id="reset-ok", runner_id="runner-ok", host_id="host-a"),
        SimpleNamespace(id="reset-unavailable", runner_id="runner-offline", host_id="host-a"),
    ]

    class _ConversationStore:
        def list_conversations(self, **kwargs):
            assert kwargs["include_archived"] is True
            return PagedList(data=conversations)

    class _Client:
        def __init__(self, session_id):
            self.session_id = session_id

        async def post(self, url, *, json, timeout):
            assert url.endswith(f"/{self.session_id}/cli-retention/reset")
            assert json == {"host_id": "host-a", "policy_revision": 8}
            assert timeout == 10.0
            return SimpleNamespace(status_code=200 if self.session_id == "reset-ok" else 503)

    class _Router:
        def client_for_session_resources(self, session_id, *, conversation):
            return SimpleNamespace(client=_Client(session_id), runner_id=conversation.runner_id)

    coordinator = CliRetentionCoordinator(
        host_store=SimpleNamespace(),
        conversation_store=_ConversationStore(),
        runner_router=_Router(),
    )

    result = await coordinator.reset_host_under_lease("host-a", policy_revision=8)

    assert result["configured"] is False
    assert result["policy_revision"] == 8
    assert result["reset"] == ["reset-ok"]
    assert result["unavailable"] == ["reset-unavailable"]
    assert coordinator.last_result("host-a") == result


@pytest.mark.asyncio
async def test_malformed_or_stale_runner_snapshot_is_unknown_not_family_runtime() -> None:
    conversations = [
        SimpleNamespace(id="stale", runner_id="runner-stale", host_id="host-a"),
        SimpleNamespace(id="malformed", runner_id="runner-malformed", host_id="host-a"),
        SimpleNamespace(id="stale-absent", runner_id="runner-stale-absent", host_id="host-a"),
        SimpleNamespace(
            id="wrong-session-absent",
            runner_id="runner-wrong-session-absent",
            host_id="host-a",
        ),
    ]

    class _HostStore:
        def get_host(self, host_id):
            assert host_id == "host-a"
            return SimpleNamespace(
                cli_retention_revision=4,
                cli_retention_policy=CliRetentionPolicy(),
            )

    class _ConversationStore:
        def list_conversations(self, **kwargs):
            return PagedList(data=conversations)

        def list_child_conversation_ids_by_parent(self, parent_ids):
            return {parent_id: [] for parent_id in parent_ids}

        def get_conversations(self, conversation_ids):
            assert conversation_ids == []
            return {}

    class _Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, session_id):
            self.session_id = session_id

        async def get(self, _url, *, params, timeout):
            del params, timeout
            payload = {
                "session_id": self.session_id,
                "present": True,
                "supported": True,
                "family": "claude",
                "busy": False,
                "eligible": True,
                "idle_seconds": 120.0,
                "activity_token": "activity",
                "runtime_generation": "boot:1",
                "host_id": "host-a",
                "policy_revision": 4,
            }
            if self.session_id == "stale":
                payload["policy_revision"] = 3
            elif self.session_id == "malformed":
                payload.pop("runtime_generation")
            elif self.session_id == "stale-absent":
                payload.update({"present": False, "policy_revision": 3})
            else:
                payload.update({"present": False, "session_id": "different-session"})
            return _Response(payload)

    class _Router:
        def client_for_session_resources(self, session_id, *, conversation):
            return SimpleNamespace(
                client=_Client(session_id),
                runner_id=conversation.runner_id,
            )

    coordinator = CliRetentionCoordinator(
        host_store=_HostStore(),
        conversation_store=_ConversationStore(),
        runner_router=_Router(),
    )

    result = await coordinator.reconcile_host_once("host-a")

    assert result["families"] == {}
    assert result["application"] == {
        "status": "unknown",
        "bound": 4,
        "supported": 0,
        "absent": 0,
        "unsupported": 0,
        "unknown": 4,
    }


@pytest.mark.asyncio
async def test_coordinator_loop_survives_one_transient_failure() -> None:
    coordinator = CliRetentionCoordinator(
        host_store=SimpleNamespace(),
        conversation_store=SimpleNamespace(),
        runner_router=SimpleNamespace(),
        scan_interval_seconds=0,
    )
    calls = 0

    async def _reconcile(_host_id: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database outage")
        return {"configured": False}

    coordinator.reconcile_host_once = _reconcile  # type: ignore[method-assign]

    await coordinator._run_host((0, "host-a"))

    assert calls == 2


@pytest.mark.asyncio
async def test_archive_gate_allows_inflight_spawn_when_archive_close_is_disabled() -> None:
    conv = SimpleNamespace(
        id="session-a",
        root_conversation_id="session-a",
        archived=True,
        host_id="host-a",
    )

    class _HostStore:
        def get_host(self, host_id):
            assert host_id == "host-a"
            return SimpleNamespace(cli_retention_policy=CliRetentionPolicy(close_on_archive=False))

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host_store=_HostStore())))

    assert (
        await _archive_blocks_external_user_work(
            request,
            conv,
            SimpleNamespace(),
            allow_inflight_spawn_without_close=True,
        )
        is False
    )


@pytest.mark.asyncio
async def test_archived_session_rejects_new_user_work_when_close_is_disabled() -> None:
    conv = SimpleNamespace(
        id="session-a",
        root_conversation_id="session-a",
        archived=True,
        host_id="host-a",
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host_store=None)))

    assert await _archive_blocks_external_user_work(request, conv, SimpleNamespace()) is True


@pytest.mark.asyncio
async def test_independently_archived_child_rejects_new_user_work() -> None:
    conv = SimpleNamespace(
        id="child",
        root_conversation_id="root",
        archived=True,
        archive_revision=1,
        archive_close_requested_revision=None,
        archive_close_completed_revision=None,
        archive_close_claimed=False,
        host_id="host-a",
    )
    root = SimpleNamespace(
        id="root",
        root_conversation_id="root",
        archived=False,
        archive_revision=0,
        archive_close_requested_revision=None,
        archive_close_completed_revision=None,
        archive_close_claimed=False,
        host_id="host-a",
    )

    class _ConversationStore:
        def get_conversation(self, conversation_id):
            assert conversation_id == "root"
            return root

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host_store=None)))
    assert (
        await _archive_blocks_external_user_work(
            request,
            conv,
            _ConversationStore(),
        )
        is True
    )


@pytest.mark.asyncio
async def test_completed_archive_close_still_blocks_inflight_respawn() -> None:
    conv = SimpleNamespace(
        id="session-a",
        root_conversation_id="session-a",
        parent_conversation_id=None,
        archived=True,
        archive_revision=1,
        archive_close_requested_revision=1,
        archive_close_completed_revision=1,
        archive_close_claimed=False,
        host_id="host-a",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                host_store=SimpleNamespace(
                    get_host=lambda _host_id: SimpleNamespace(
                        cli_retention_policy=CliRetentionPolicy(close_on_archive=False)
                    )
                )
            )
        )
    )

    assert (
        await _archive_blocks_external_user_work(
            request,
            conv,
            SimpleNamespace(),
            allow_inflight_spawn_without_close=True,
        )
        is True
    )


@pytest.mark.asyncio
async def test_active_archive_close_intent_blocks_work_after_quick_unarchive() -> None:
    conv = SimpleNamespace(
        id="session-a",
        root_conversation_id="session-a",
        archived=False,
        host_id="host-a",
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host_store=None)))
    _archive_close_intents.add(conv.id)
    try:
        assert await _archive_blocks_external_user_work(request, conv, SimpleNamespace()) is True
    finally:
        _archive_close_intents.discard(conv.id)
