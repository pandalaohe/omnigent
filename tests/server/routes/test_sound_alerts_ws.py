"""Tests for cross-device sound-alert claiming over the updates stream.

A client that detects an alert transition posts
``POST /v1/me/sound-alerts/claim``; the server pushes the alert to exactly
one of the user's registered session-updates connections. These tests drive
two WebSocket clients of the same user plus one of another user against the
real router and stores, and assert the active device is the only one that
receives the frame.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import omnigent.server.routes.sessions as sessions_routes
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server import sound_alerts
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_pre_resolved_elicitations,
)
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.server.schemas import SessionEventInput
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture(autouse=True)
def _reset_sound_alerts_ws() -> None:
    """Isolate the module-global ringer registry per test."""
    sound_alerts.reset_for_tests()
    yield
    sound_alerts.reset_for_tests()


@pytest.fixture
def fast_rescan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the heartbeat cadence so silence checks fail fast."""
    monkeypatch.setattr(sessions_routes, "_SESSION_UPDATES_RESCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(sessions_routes, "_SESSION_UPDATES_HEARTBEAT_INTERVAL_S", 0.1)


@pytest.fixture
def stores(
    db_uri: str,
) -> tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore]:
    """Real file-backed stores so writes from the test thread are visible
    to the WS handler thread."""
    return (
        SqlAlchemyConversationStore(db_uri),
        SqlAlchemyAgentStore(db_uri),
        SqlAlchemyPermissionStore(db_uri),
    )


@pytest.fixture
def app(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
    db_uri: str,
) -> FastAPI:
    """Minimal app with header auth, a real permission store, and the
    preferences store the claim route reads the primary device from."""
    conversation_store, agent_store, permission_store = stores
    app = FastAPI()
    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=permission_store,
        ),
        prefix="/v1",
    )
    app.state.user_preferences_store = SqlAlchemyUserPreferencesStore(db_uri)
    return app


def _seed_session(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
    *,
    owner: str,
    title: str,
) -> str:
    """Create a session-shaped conversation owned by ``owner``."""
    conversation_store, agent_store, permission_store = stores
    if agent_store.get("087b7cb7ac30abf4debfaa578d052ec6") is None:
        agent_store.create(
            agent_id="087b7cb7ac30abf4debfaa578d052ec6",
            name="test-agent",
            bundle_location="087b7cb7ac30abf4debfaa578d052ec6/bundle",
        )
    conv = conversation_store.create_conversation(
        title=title, agent_id="087b7cb7ac30abf4debfaa578d052ec6"
    )
    permission_store.ensure_user(owner)
    permission_store.grant(owner, conv.id, LEVEL_OWNER)
    return conv.id


def _recv_until(ws: object, wanted: set[str], *, max_frames: int = 50) -> dict[str, object]:
    """Read frames until one whose ``type`` is in ``wanted`` arrives."""
    for _ in range(max_frames):
        frame = json.loads(ws.receive_text())  # type: ignore[attr-defined]
        if frame.get("type") in wanted:
            return frame
    raise AssertionError(f"no frame in {wanted} after {max_frames} frames")


def _assert_only_heartbeats(ws: object, *, frames: int = 5) -> None:
    """Assert the next frames carry no alert (the registry never picked this connection)."""
    for _ in range(frames):
        frame = json.loads(ws.receive_text())  # type: ignore[attr-defined]
        assert frame["type"] == "heartbeat", f"expected only heartbeats, got {frame['type']}"


def _hello(ws: object, device_id: str) -> None:
    ws.send_text(  # type: ignore[attr-defined]
        json.dumps(
            {
                "type": "hello",
                "device_id": device_id,
                "device_label": device_id,
                "can_ring": True,
            }
        )
    )


def _register_fake_ringer() -> list[dict[str, Any]]:
    """Register one in-process device and collect any delivered frames."""
    frames: list[dict[str, Any]] = []

    async def send(frame: dict[str, Any]) -> None:
        frames.append(frame)

    sound_alerts.register(
        ALICE,
        "fake_ringer",
        device_id="dev_fake",
        device_label="Fake device",
        can_ring=True,
        send=send,
    )
    return frames


async def _claim_done(session_id: str, alert_id: str) -> bool:
    return await sound_alerts.claim(
        ALICE,
        alert_id=alert_id,
        session_id=session_id,
        level="done",
        primary_device_id=None,
    )


def test_claim_delivers_to_the_active_connection_only_once(
    app: FastAPI, stores, fast_rescan: None
) -> None:
    """The device used last receives the alert; repeats and other users don't."""
    session_id = _seed_session(stores, owner=ALICE, title="live")
    client = TestClient(app)
    with (
        client.websocket_connect(
            "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
        ) as ws_active,
        client.websocket_connect(
            "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
        ) as ws_idle,
        client.websocket_connect(
            "/v1/sessions/updates", headers={"X-Forwarded-Email": BOB}
        ) as ws_bob,
    ):
        _hello(ws_active, "dev_a")
        _hello(ws_idle, "dev_b")
        _hello(ws_bob, "dev_bob")
        ws_active.send_text(json.dumps({"type": "activity"}))
        # The watch/snapshot round trip proves each connection's reader has
        # processed the hello (and activity) sent ahead of it.
        for ws in (ws_active, ws_idle, ws_bob):
            ws.send_text(json.dumps({"type": "watch", "session_ids": [session_id]}))
            _recv_until(ws, {"snapshot"})

        body = {
            "alert_id": f"{session_id}:needs_response:1",
            "session_id": session_id,
            "level": "needs_response",
        }
        response = client.post(
            "/v1/me/sound-alerts/claim", json=body, headers={"X-Forwarded-Email": ALICE}
        )
        assert response.status_code == 202
        assert response.json() == {"delivered": True}

        frame = _recv_until(ws_active, {"sound_alert"})
        assert frame == {
            "type": "sound_alert",
            "alert_id": body["alert_id"],
            "session_id": session_id,
            "level": "needs_response",
        }
        # The idle same-user device and the other user never see the frame.
        _assert_only_heartbeats(ws_idle)
        _assert_only_heartbeats(ws_bob)

        repeat = client.post(
            "/v1/me/sound-alerts/claim", json=body, headers={"X-Forwarded-Email": ALICE}
        )
        assert repeat.status_code == 202
        assert repeat.json() == {"delivered": False}
        # An already-claimed id delivers nothing to anyone.
        _assert_only_heartbeats(ws_active)

        # A malformed body is rejected outright (extra="forbid").
        bad = client.post(
            "/v1/me/sound-alerts/claim",
            json={**body, "unexpected": True},
            headers={"X-Forwarded-Email": ALICE},
        )
        assert bad.status_code == 422


def test_claim_delivers_to_the_active_ringer_even_without_watching_the_session(
    app: FastAPI, stores, fast_rescan: None
) -> None:
    """Claim routing follows activity, not which connection watched the session."""
    session_id = _seed_session(stores, owner=ALICE, title="live")
    client = TestClient(app)
    with (
        client.websocket_connect(
            "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
        ) as ws_active,
        client.websocket_connect(
            "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
        ) as ws_idle,
    ):
        _hello(ws_active, "dev_a")
        _hello(ws_idle, "dev_b")
        ws_active.send_text(json.dumps({"type": "activity"}))
        # The most recently active connection watches nothing; the idle one
        # watches the alert's session. The alert still lands on the active one.
        ws_active.send_text(json.dumps({"type": "watch", "session_ids": []}))
        _recv_until(ws_active, {"snapshot"})
        ws_idle.send_text(json.dumps({"type": "watch", "session_ids": [session_id]}))
        _recv_until(ws_idle, {"snapshot"})

        body = {
            "alert_id": f"{session_id}:needs_response:1",
            "session_id": session_id,
            "level": "needs_response",
        }
        response = client.post(
            "/v1/me/sound-alerts/claim", json=body, headers={"X-Forwarded-Email": ALICE}
        )
        assert response.status_code == 202
        assert response.json() == {"delivered": True}

        frame = _recv_until(ws_active, {"sound_alert"})
        assert frame == {
            "type": "sound_alert",
            "alert_id": body["alert_id"],
            "session_id": session_id,
            "level": "needs_response",
        }
        _assert_only_heartbeats(ws_idle)


@pytest.mark.asyncio
async def test_delivered_interrupt_is_noted_before_the_delivery_starts(
    app: FastAPI,
    stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal claim racing with runner delivery is already silenced."""
    from omnigent.server.routes.sessions import routes_events

    session_id = _seed_session(stores, owner=ALICE, title="interrupt")
    frames = _register_fake_ringer()
    claims_during_delivery: list[bool] = []

    async def _deliver(_session_id: str, _runner_router: Any) -> None:
        claims_during_delivery.append(
            await _claim_done(_session_id, f"{_session_id}:done:during_delivery")
        )

    monkeypatch.setattr(routes_events, "_deliver_interrupt_once", _deliver)
    try:
        response = TestClient(app).post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "interrupt", "data": {}},
            headers={"X-Forwarded-Email": ALICE},
        )

        assert response.status_code == 202, response.text
        assert claims_during_delivery == [False]
        assert await _claim_done(session_id, f"{session_id}:done:after_delivery") is False
        assert frames == []
    finally:
        routes_events._interrupt_fenced_sessions.discard(session_id)


@pytest.mark.asyncio
async def test_failed_interrupt_forgets_the_stop_note(
    app: FastAPI,
    stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected interrupt leaves real completion alerts enabled."""
    from omnigent.server.routes.sessions import routes_events

    session_id = _seed_session(stores, owner=ALICE, title="interrupt failure")
    frames = _register_fake_ringer()

    async def _fail_delivery(_session_id: str, _runner_router: Any) -> None:
        raise OmnigentError("runner unavailable", code=ErrorCode.RUNNER_UNAVAILABLE)

    monkeypatch.setattr(routes_events, "_deliver_interrupt_once", _fail_delivery)
    try:
        with pytest.raises(OmnigentError) as error:
            TestClient(app).post(
                f"/v1/sessions/{session_id}/events",
                json={"type": "interrupt", "data": {}},
                headers={"X-Forwarded-Email": ALICE},
            )
        assert error.value.code == ErrorCode.RUNNER_UNAVAILABLE
        assert await _claim_done(session_id, f"{session_id}:done:after_failure") is True
        assert [frame["level"] for frame in frames] == ["done"]
    finally:
        routes_events._interrupt_fenced_sessions.discard(session_id)


@pytest.mark.asyncio
async def test_failed_stop_forgets_the_stop_note(
    app: FastAPI,
    stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop that the runner rejects leaves completion alerts enabled."""
    from omnigent.server.routes.sessions import routes_events

    session_id = _seed_session(stores, owner=ALICE, title="stop failure")
    frames = _register_fake_ringer()

    async def _fail_stop(_session_id: str, _runner_router: Any) -> bool:
        raise OmnigentError("runner unavailable", code=ErrorCode.RUNNER_UNAVAILABLE)

    monkeypatch.setattr(routes_events, "_stop_session_via_runner", _fail_stop)
    with pytest.raises(OmnigentError) as error:
        TestClient(app).post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "stop_session", "data": {}},
            headers={"X-Forwarded-Email": ALICE},
        )
    assert error.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert await _claim_done(session_id, f"{session_id}:done:after_failure") is True
    assert [frame["level"] for frame in frames] == ["done"]


@pytest.mark.asyncio
async def test_cancelled_interrupt_request_keeps_the_stop_note(
    app: FastAPI,
    stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes.sessions import routes_events

    session_id = _seed_session(stores, owner=ALICE, title="cancelled interrupt")
    started, release = asyncio.Event(), asyncio.Event()

    async def _deliver(_session_id: str, _runner_router: Any) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(routes_events, "_deliver_interrupt_once", _deliver)
    endpoint = next(route.endpoint for route in app.routes if route.name == "post_event")
    request = Request({"type": "http", "headers": [(b"x-forwarded-email", ALICE.encode())]})
    task = asyncio.create_task(endpoint(request, session_id, SessionEventInput(type="interrupt")))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    delivery = routes_events._interrupt_delivery_tasks.get(session_id)
    assert delivery is not None
    release.set()
    await delivery
    assert await _claim_done(session_id, f"{session_id}:done:after_cancel") is False
    routes_events._interrupt_fenced_sessions.discard(session_id)


def _register_harness_future(session_id: str, elicitation_id: str) -> asyncio.Future[Any]:
    """Register a live, session-owned harness Future for ``elicitation_id``."""
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    _harness_elicitation_registry[elicitation_id] = future
    _harness_elicitation_owners[elicitation_id] = session_id
    _harness_pre_resolved_elicitations.pop(elicitation_id, None)
    return future


def _clear_harness_future(elicitation_id: str) -> None:
    """Drop the Future, owner, and tombstone planted for ``elicitation_id``."""
    _harness_elicitation_registry.pop(elicitation_id, None)
    _harness_elicitation_owners.pop(elicitation_id, None)
    _harness_pre_resolved_elicitations.pop(elicitation_id, None)


def _resolve_url(app: FastAPI, session_id: str, elicitation_id: str, body: dict[str, Any]):
    return TestClient(app).post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json=body,
        headers={"X-Forwarded-Email": ALICE},
    )


def _approval_event(app: FastAPI, session_id: str, data: dict[str, Any]):
    return TestClient(app).post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "approval", "data": data},
        headers={"X-Forwarded-Email": ALICE},
    )


@pytest.mark.asyncio
async def test_resolve_url_interrupting_cancel_on_live_future_silences_done(
    app: FastAPI, stores
) -> None:
    """A cancel-and-interrupt landing on the owned Future notes the session."""
    session_id = _seed_session(stores, owner=ALICE, title="resolve interrupt")
    frames = _register_fake_ringer()
    elicitation_id = "elicit_a"
    _register_harness_future(session_id, elicitation_id)
    try:
        response = _resolve_url(
            app,
            session_id,
            elicitation_id,
            {"action": "cancel", "_meta": {"interrupt": True}},
        )
        assert response.status_code == 202, response.text
        assert await _claim_done(session_id, f"{session_id}:done:after_resolve") is False
        assert frames == []
    finally:
        _clear_harness_future(elicitation_id)


@pytest.mark.asyncio
async def test_approval_event_interrupting_cancel_on_live_future_silences_done(
    app: FastAPI, stores
) -> None:
    """The approval event path notes the same owned-Future cancel."""
    session_id = _seed_session(stores, owner=ALICE, title="approval interrupt")
    frames = _register_fake_ringer()
    elicitation_id = "elicit_a"
    _register_harness_future(session_id, elicitation_id)
    try:
        response = _approval_event(
            app,
            session_id,
            {
                "elicitation_id": elicitation_id,
                "action": "cancel",
                "_meta": {"interrupt": True},
            },
        )
        assert response.status_code == 202, response.text
        assert await _claim_done(session_id, f"{session_id}:done:after_approval") is False
        assert frames == []
    finally:
        _clear_harness_future(elicitation_id)


@pytest.mark.asyncio
async def test_cancel_without_interrupt_on_live_future_keeps_done(app: FastAPI, stores) -> None:
    """A plain cancel is not the user's stop, so completion still rings."""
    session_id = _seed_session(stores, owner=ALICE, title="resolve plain cancel")
    frames = _register_fake_ringer()
    elicitation_id = "elicit_a"
    _register_harness_future(session_id, elicitation_id)
    try:
        response = _resolve_url(app, session_id, elicitation_id, {"action": "cancel"})
        assert response.status_code == 202, response.text
        assert await _claim_done(session_id, f"{session_id}:done:after_cancel") is True
        assert [frame["level"] for frame in frames] == ["done"]
    finally:
        _clear_harness_future(elicitation_id)


@pytest.mark.asyncio
async def test_interrupting_cancel_without_live_future_keeps_done(app: FastAPI, stores) -> None:
    """A runner-side verdict ignores the marker, so the note is not taken."""
    session_id = _seed_session(stores, owner=ALICE, title="runner-side interrupt")
    frames = _register_fake_ringer()
    elicitation_id = "elicit_a"
    _clear_harness_future(elicitation_id)
    try:
        response = _approval_event(
            app,
            session_id,
            {
                "elicitation_id": elicitation_id,
                "action": "cancel",
                "_meta": {"interrupt": True},
            },
        )
        assert response.status_code == 202, response.text
        assert await _claim_done(session_id, f"{session_id}:done:after_runner") is True
        assert [frame["level"] for frame in frames] == ["done"]
    finally:
        _clear_harness_future(elicitation_id)


@pytest.mark.asyncio
async def test_interrupting_cancel_note_survives_a_later_resolver_failure(
    app: FastAPI,
    stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop landed once the Future was set, so a later failure keeps the note."""
    session_id = _seed_session(stores, owner=ALICE, title="resolve late failure")
    frames = _register_fake_ringer()
    elicitation_id = "elicit_a"
    future = _register_harness_future(session_id, elicitation_id)

    def _explode(*_args: Any) -> None:
        raise RuntimeError("ancestor fan-out failed")

    monkeypatch.setattr(orchestration, "_publish_elicitation_resolved_to_ancestors", _explode)
    try:
        with pytest.raises(RuntimeError):
            _resolve_url(
                app,
                session_id,
                elicitation_id,
                {"action": "cancel", "_meta": {"interrupt": True}},
            )
        assert future.done()
        assert await _claim_done(session_id, f"{session_id}:done:after_late") is False
        assert frames == []
    finally:
        _clear_harness_future(elicitation_id)


@pytest.mark.asyncio
async def test_cancelled_interrupt_request_forgets_note_when_shared_delivery_fails(
    app: FastAPI,
    stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request cancelled mid-delivery still forgets a failed shared delivery."""
    from omnigent.server.routes.sessions import routes_events

    session_id = _seed_session(stores, owner=ALICE, title="cancelled interrupt failure")
    frames = _register_fake_ringer()
    started, release = asyncio.Event(), asyncio.Event()

    async def _deliver(_session_id: str, _runner_router: Any) -> None:
        started.set()
        await release.wait()
        raise OmnigentError("runner unavailable", code=ErrorCode.RUNNER_UNAVAILABLE)

    monkeypatch.setattr(routes_events, "_deliver_interrupt_once", _deliver)
    endpoint = next(route.endpoint for route in app.routes if route.name == "post_event")
    request = Request({"type": "http", "headers": [(b"x-forwarded-email", ALICE.encode())]})
    task = asyncio.create_task(endpoint(request, session_id, SessionEventInput(type="interrupt")))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    delivery = routes_events._interrupt_delivery_tasks.get(session_id)
    assert delivery is not None
    release.set()
    with pytest.raises(OmnigentError):
        await delivery
    assert await _claim_done(session_id, f"{session_id}:done:after_cancel") is True
    assert [frame["level"] for frame in frames] == ["done"]
    routes_events._interrupt_fenced_sessions.discard(session_id)
