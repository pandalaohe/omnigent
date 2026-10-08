"""Tests for per-owner session-collaboration settings enforcement (SCC15).

The peer-route cases ride on ``test_peer_messages.py``'s app fixture with a
real preferences store attached; the initializer cases drive a fake runner
client counting session-init POSTs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.server.session_collab import (
    COLLAB_DISABLED_MESSAGE,
    collab_owner_for,
    require_collab_enabled,
    session_peer_enabled,
)
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore

# Imported so pytest registers the fixture; collab_env requests it by name.
from tests.server.routes.test_peer_messages import (
    ALICE,
    _seed_trigger_depth,
    peer_env,  # noqa: F401
)

SESSION_COLLAB = "session_collab"


@pytest.fixture()
def collab_env(request: pytest.FixtureRequest, db_uri: str) -> dict[str, Any]:
    """Peer-messaging app whose routes read a real preferences store."""
    env: dict[str, Any] = request.getfixturevalue("peer_env")
    store = SqlAlchemyUserPreferencesStore(db_uri)
    env["app"].state.user_preferences_store = store
    env["prefs_store"] = store
    return env


def _conversation(**overrides: Any) -> Conversation:
    values: dict[str, Any] = {
        "id": "conv_collab",
        "created_at": 1,
        "updated_at": 2,
        "root_conversation_id": "conv_collab",
        "agent_id": "agent_collab",
        "runner_id": "runner_collab",
    }
    values.update(overrides)
    return Conversation(**values)


# ── peer route: rows 0 and 2–6 per owner ────────────────────────────


@pytest.mark.asyncio
async def test_relay_depth_max_is_per_owner(collab_env: dict[str, Any]) -> None:
    """A stored depth 3 holds the 4th hop; the default would deliver it."""
    sender, receiver = collab_env["sender"], collab_env["receiver"]
    collab_env["prefs_store"].patch_namespace(ALICE, SESSION_COLLAB, {"relayDepthMax": 3})
    _seed_trigger_depth(collab_env, 3)

    result = await collab_env["app"].state.peer_send(
        sender=sender,
        receiver_id=receiver.id,
        text=f"relay hop {uuid.uuid4().hex}",
        correlation_id=None,
    )

    assert result["disposition"] == "held"
    assert result["reason"] == "relay_limit"
    held = collab_env["peer_store"].get(result["peer_id"])
    assert held is not None and held.state == "held" and held.relay_depth == 4


@pytest.mark.asyncio
async def test_defaults_apply_without_a_namespace(collab_env: dict[str, Any]) -> None:
    """An empty store equals today's constants: depth 30 delivers, 31 holds."""
    sender, receiver = collab_env["sender"], collab_env["receiver"]
    _seed_trigger_depth(collab_env, 29)
    delivered = await collab_env["app"].state.peer_send(
        sender=sender,
        receiver_id=receiver.id,
        text=f"at limit {uuid.uuid4().hex}",
        correlation_id=None,
    )
    assert delivered["disposition"] == "delivered"

    _seed_trigger_depth(collab_env, 30)
    held = await collab_env["app"].state.peer_send(
        sender=sender,
        receiver_id=receiver.id,
        text=f"over limit {uuid.uuid4().hex}",
        correlation_id=None,
    )
    assert held["disposition"] == "held"
    assert held["reason"] == "relay_limit"


@pytest.mark.asyncio
async def test_pair_rate_count_is_per_owner(collab_env: dict[str, Any]) -> None:
    """A stored pair budget of 2 queues the third send in the window."""
    sender, receiver = collab_env["sender"], collab_env["receiver"]
    collab_env["prefs_store"].patch_namespace(ALICE, SESSION_COLLAB, {"pairRateCount": 2})

    dispositions = []
    for _ in range(3):
        result = await collab_env["app"].state.peer_send(
            sender=sender,
            receiver_id=receiver.id,
            text=f"pair {uuid.uuid4().hex}",
            correlation_id=None,
        )
        dispositions.append((result["disposition"], result["reason"]))

    assert dispositions[:2] == [("delivered", None), ("delivered", None)]
    assert dispositions[2] == ("queued", "rate_delay")


@pytest.mark.asyncio
async def test_undelivered_ttl_is_per_owner(collab_env: dict[str, Any]) -> None:
    """A busy receiver's queued record expires on the owner's stamp."""
    sender, receiver = collab_env["sender"], collab_env["receiver"]
    collab_env["prefs_store"].patch_namespace(
        ALICE, SESSION_COLLAB, {"undeliveredTtlSeconds": 3600}
    )
    sessions_module._session_status_cache[receiver.id] = "running"
    try:
        result = await collab_env["app"].state.peer_send(
            sender=sender,
            receiver_id=receiver.id,
            text=f"busy ttl {uuid.uuid4().hex}",
            correlation_id=None,
        )
    finally:
        sessions_module._session_status_cache.pop(receiver.id, None)

    assert result["disposition"] == "queued"
    record = collab_env["peer_store"].get(result["peer_id"])
    assert record is not None
    assert record.expires_at - record.created_at == 3600


@pytest.mark.asyncio
async def test_master_off_refuses_sends(collab_env: dict[str, Any]) -> None:
    """A disabled owner gets collab_disabled."""
    sender, receiver = collab_env["sender"], collab_env["receiver"]
    collab_env["prefs_store"].patch_namespace(ALICE, SESSION_COLLAB, {"enabled": False})

    refused = await collab_env["app"].state.peer_send(
        sender=sender,
        receiver_id=receiver.id,
        text=f"never sent {uuid.uuid4().hex}",
        correlation_id=None,
    )
    assert refused["disposition"] == "refused"
    assert refused["reason"] == "collab_disabled"
    assert refused["peer_id"] is None
    assert refused["receiver"]["id"] == receiver.id


# ── session_collab helpers ──────────────────────────────────────────


def test_collab_owner_for_defaults_to_the_local_user() -> None:
    """Without a permission store the reserved local user wins."""
    conversation = _conversation()

    assert collab_owner_for(conversation, None, None) == RESERVED_USER_LOCAL  # type: ignore[arg-type]


def test_session_peer_enabled_reads_the_owner_switch(db_uri: str) -> None:
    """The stored master switch governs the snapshot for the local owner."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    conversation = _conversation()

    assert (
        session_peer_enabled(
            conversation,
            flag_on=True,
            conversation_store=None,  # type: ignore[arg-type]
            permission_store=None,
            prefs_store=store,
        )
        is True
    )

    store.patch_namespace(RESERVED_USER_LOCAL, SESSION_COLLAB, {"enabled": False})
    assert (
        session_peer_enabled(
            conversation,
            flag_on=True,
            conversation_store=None,  # type: ignore[arg-type]
            permission_store=None,
            prefs_store=store,
        )
        is False
    )
    # The deployment flag off always wins, whatever the store says.
    assert (
        session_peer_enabled(
            conversation,
            flag_on=False,
            conversation_store=None,  # type: ignore[arg-type]
            permission_store=None,
            prefs_store=store,
        )
        is False
    )


def test_session_peer_enabled_fails_open_to_the_flag() -> None:
    """A store error keeps today's behaviour instead of dropping collaboration."""
    conversation = _conversation()

    class _RaisingStore:
        def get(self, user_id: str) -> None:
            raise RuntimeError("preferences backend down")

    assert (
        session_peer_enabled(
            conversation,
            flag_on=True,
            conversation_store=None,  # type: ignore[arg-type]
            permission_store=None,
            prefs_store=_RaisingStore(),  # type: ignore[arg-type]
        )
        is True
    )
    assert (
        session_peer_enabled(
            conversation,
            flag_on=False,
            conversation_store=None,  # type: ignore[arg-type]
            permission_store=None,
            prefs_store=_RaisingStore(),  # type: ignore[arg-type]
        )
        is False
    )


def test_require_collab_enabled_refuses_with_the_settings_message(db_uri: str) -> None:
    """The master switch off raises FORBIDDEN naming Settings."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace(ALICE, SESSION_COLLAB, {"enabled": False})

    with pytest.raises(OmnigentError) as error:
        require_collab_enabled(store, ALICE)

    assert error.value.code == ErrorCode.FORBIDDEN
    assert error.value.message == COLLAB_DISABLED_MESSAGE

    store.patch_namespace(ALICE, SESSION_COLLAB, {"enabled": True})
    assert require_collab_enabled(store, ALICE).enabled is True
    assert require_collab_enabled(None, ALICE).enabled is True


# ── forward path: a flipped switch re-inits before dispatch ─────────


@pytest.mark.asyncio
async def test_message_forward_reinits_when_peer_flag_is_stale(
    client: httpx.AsyncClient, app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale snapshot re-sends the init envelope ahead of the forward."""
    from omnigent.server.routes._sessions.helpers import _SessionEventDispatchResult
    from omnigent.server.routes.sessions import routes_events as routes_events_module
    from tests.server.helpers import create_test_agent

    agent = await create_test_agent(client)
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    order: list[str] = []

    class _Initializer:
        async def peer_flag_stale(self, conversation: Any, runner_client: Any) -> bool:
            order.append("stale")
            return True

    app.state.runner_session_initializer = _Initializer()

    async def _fake_runner_client(*_args: Any, **_kwargs: Any) -> Any:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(202, json={}))
        )

    async def _fake_ensure_initialized(*_args: Any, **_kwargs: Any) -> bool:
        order.append("init")
        return True

    async def _fake_dispatch(*_args: Any, **_kwargs: Any) -> Any:
        order.append("dispatch")
        return _SessionEventDispatchResult(item_id=None, pending_id=None)

    monkeypatch.setattr(routes_events_module, "_get_runner_client", _fake_runner_client)
    monkeypatch.setattr(
        routes_events_module, "_ensure_runner_session_initialized", _fake_ensure_initialized
    )
    monkeypatch.setattr(routes_events_module, "_dispatch_session_event_to_runner", _fake_dispatch)

    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        },
    )

    assert response.status_code == 202, response.text
    assert order == ["stale", "init", "dispatch"]


# ── initializer: applied-flag tracking and stale check ──────────────


class _Registry:
    def __init__(self) -> None:
        self.connection: Any = SimpleNamespace(generation=1)

    def get(self, _runner_id: str) -> Any:
        return self.connection


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, _path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(kwargs["json"])
        return httpx.Response(201, json={"status": "initialized"})


def _peer_flag(payload: dict[str, Any]) -> bool:
    return bool(payload["session_init"]["snapshot"]["peer_messaging_enabled"])


@pytest.mark.asyncio
async def test_resolve_peer_messaging_defaults_to_the_static_flag() -> None:
    """Embedded initializers without a resolver keep the constructed value."""
    conversation = _conversation()
    on = RunnerSessionInitializer(_Registry(), server_version="test", peer_messaging_enabled=True)  # type: ignore[arg-type]
    off = RunnerSessionInitializer(_Registry(), server_version="test")  # type: ignore[arg-type]

    assert await on.resolve_peer_messaging(conversation) is True
    assert await off.resolve_peer_messaging(conversation) is False


@pytest.mark.asyncio
async def test_initializer_reposts_on_each_peer_flag_flip() -> None:
    """on → off → on re-posts each time; a matching state hits the memo."""
    state = {"enabled": True}
    initializer = RunnerSessionInitializer(
        _Registry(),  # type: ignore[arg-type]
        server_version="test",
        peer_messaging_resolver=lambda _conv: state["enabled"],
    )
    conversation = _conversation()
    client = _RecordingClient()

    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert len(client.calls) == 1
    assert _peer_flag(client.calls[0]) is True
    assert await initializer.peer_flag_stale(conversation, client) is False  # type: ignore[arg-type]

    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert len(client.calls) == 1, "a matching state must reuse the memo"

    state["enabled"] = False
    assert await initializer.peer_flag_stale(conversation, client) is True  # type: ignore[arg-type]
    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert len(client.calls) == 2
    assert _peer_flag(client.calls[1]) is False

    state["enabled"] = True
    assert await initializer.peer_flag_stale(conversation, client) is True  # type: ignore[arg-type]
    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert len(client.calls) == 3
    assert _peer_flag(client.calls[2]) is True
    assert await initializer.peer_flag_stale(conversation, client) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_peer_flag_stale_needs_an_applied_value() -> None:
    """No successful post in this generation — or no runner — is never stale."""
    initializer = RunnerSessionInitializer(
        _Registry(),  # type: ignore[arg-type]
        server_version="test",
        peer_messaging_resolver=lambda _conv: False,
    )
    conversation = _conversation()
    client = _RecordingClient()

    assert await initializer.peer_flag_stale(conversation, client) is False  # type: ignore[arg-type]
    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert await initializer.peer_flag_stale(conversation, client) is False  # type: ignore[arg-type]

    initializer.invalidate_session(conversation.id)
    assert await initializer.peer_flag_stale(conversation, client) is False  # type: ignore[arg-type]

    unbound = dataclasses.replace(conversation, runner_id=None)
    assert await initializer.peer_flag_stale(unbound, client) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_invalidation_forgets_the_applied_peer_value() -> None:
    """Dropped readiness also drops the applied snapshot for that scope."""
    state = {"enabled": True}
    initializer = RunnerSessionInitializer(
        _Registry(),  # type: ignore[arg-type]
        server_version="test",
        peer_messaging_resolver=lambda _conv: state["enabled"],
    )
    conversation = _conversation()
    client = _RecordingClient()

    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert initializer._applied_peer

    initializer.invalidate_runner(conversation.runner_id or "")
    assert not initializer._applied_peer

    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    state["enabled"] = False
    initializer.invalidate_session(conversation.id)
    assert not initializer._applied_peer
    assert await initializer.peer_flag_stale(conversation, client) is False  # type: ignore[arg-type]


class _HoldableClient:
    """Fake runner client whose POST blocks until released, recording bodies."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def post(self, _path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(kwargs["json"])
        self.entered.set()
        await self.release.wait()
        return httpx.Response(201, json={"status": "initialized"})


@pytest.mark.asyncio
async def test_initialize_retries_when_a_joined_post_carries_a_stale_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on → in-flight off → on: the joined off post must not win."""
    state = {"enabled": True}
    registry = _Registry()
    initializer = RunnerSessionInitializer(
        registry,  # type: ignore[arg-type]
        server_version="test",
        peer_messaging_resolver=lambda _conv: state["enabled"],
    )
    conversation = _conversation()
    client = _HoldableClient()

    joined: list[asyncio.Task[httpx.Response]] = []
    real_shield = asyncio.shield

    def _spy_shield(awaitable: Any) -> Any:
        task = asyncio.ensure_future(awaitable)
        joined.append(task)
        return real_shield(task)

    monkeypatch.setattr(asyncio, "shield", _spy_shield)

    client.release.set()
    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert len(client.calls) == 1
    assert _peer_flag(client.calls[0]) is True

    # The switch flips off and its post is still in flight.
    state["enabled"] = False
    client.entered.clear()
    client.release.clear()
    off = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await client.entered.wait()
    assert _peer_flag(client.calls[-1]) is False

    # The switch flips back on while the off post is pending: the generation
    # will carry the pending value, so the resolved flag is stale.
    state["enabled"] = True
    assert await initializer.peer_flag_stale(conversation, client) is True  # type: ignore[arg-type]

    # A new call resolves the on value and joins the in-flight off post.
    joined.clear()
    on = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]

    async def _joined_off_post() -> None:
        while not joined:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(_joined_off_post(), timeout=5)
    assert len(client.calls) == 2, "the second call must join, not post a third time"

    client.release.set()
    await asyncio.gather(off, on)

    # The joined off post applied False; the on call posts once more with the
    # latest resolved value and the applied snapshot follows it.
    assert len(client.calls) == 3
    assert _peer_flag(client.calls[-1]) is True
    pkey = (conversation.runner_id or "", registry.connection.generation, conversation.id)
    assert initializer._applied_peer[pkey] is True
    assert not initializer._pending_peer
    assert await initializer.peer_flag_stale(conversation, client) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_initialize_retry_is_single_flight_across_joined_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two on callers joining one in-flight off post start exactly one retry."""
    state = {"enabled": True}
    registry = _Registry()
    initializer = RunnerSessionInitializer(
        registry,  # type: ignore[arg-type]
        server_version="test",
        peer_messaging_resolver=lambda _conv: state["enabled"],
    )
    conversation = _conversation()
    client = _HoldableClient()

    client.release.set()
    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert len(client.calls) == 1

    # The switch flips off and its post is still in flight.
    state["enabled"] = False
    client.entered.clear()
    client.release.clear()
    off = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await client.entered.wait()

    # Two callers resolve the flipped-on value and join the held off post.
    joined = 0
    both_joined = asyncio.Event()
    real_shield = asyncio.shield

    def _spy_shield(awaitable: Any) -> Any:
        nonlocal joined
        task = asyncio.ensure_future(awaitable)
        joined += 1
        if joined == 2:
            both_joined.set()
        return real_shield(task)

    monkeypatch.setattr(asyncio, "shield", _spy_shield)

    state["enabled"] = True
    on_first = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    on_second = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await asyncio.wait_for(both_joined.wait(), timeout=5)
    assert len(client.calls) == 2, "both callers must join, not post again"

    client.release.set()
    await asyncio.gather(off, on_first, on_second)

    assert [_peer_flag(body) for body in client.calls] == [True, False, True]
    pkey = (conversation.runner_id or "", registry.connection.generation, conversation.id)
    assert initializer._applied_peer[pkey] is True
    assert not initializer._pending_peer
    assert await initializer.peer_flag_stale(conversation, client) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["session", "runner"])
async def test_initialize_does_not_retry_when_invalidated_before_the_waiter_resumes(
    scope: str,
) -> None:
    """A spurious retry must not restore readiness an invalidation cleared."""
    initializer = RunnerSessionInitializer(
        _Registry(),  # type: ignore[arg-type]
        server_version="test",
        peer_messaging_resolver=lambda _conv: True,
    )
    conversation = _conversation()
    client = _HoldableClient()
    client.release.clear()

    caller = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await client.entered.wait()
    assert len(client.calls) == 1

    task = next(iter(initializer._tasks.values()))

    def _invalidate(_done: asyncio.Task[httpx.Response]) -> None:
        if scope == "session":
            initializer.invalidate_session(conversation.id)
        else:
            initializer.invalidate_runner(conversation.runner_id or "")

    task.add_done_callback(_invalidate)
    client.release.set()
    response = await caller

    assert response.status_code == 201
    assert len(client.calls) == 1, "the invalidated post must not trigger a retry"
    assert not initializer._applied_peer
    assert not initializer._pending_peer
