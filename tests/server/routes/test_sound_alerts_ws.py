"""Tests for cross-device sound-alert claiming over the updates stream.

A client that detects an alert transition posts
``POST /v1/me/sound-alerts/claim``; the server pushes the alert to exactly
one of the user's registered session-updates connections. These tests drive
two WebSocket clients of the same user plus one of another user against the
real router and stores, and assert the active device is the only one that
receives the frame.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import omnigent.server.routes.sessions as sessions_routes
from omnigent.server import sound_alerts
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.routes.sessions import create_sessions_router
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


def test_claim_delivers_to_the_active_connection_only_once(
    app: FastAPI, stores, fast_rescan: None
) -> None:
    """The device used last receives the alert; repeats and other users don't."""
    session_id = _seed_session(stores, owner=ALICE, title="live")
    with (
        TestClient(app) as client,
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
        # Register each connection before marking one active.
        for ws in (ws_active, ws_idle, ws_bob):
            ws.send_text(json.dumps({"type": "watch", "session_ids": [session_id]}))
            _recv_until(ws, {"snapshot"})
        ws_active.send_text(json.dumps({"type": "activity"}))
        # This snapshot proves the active connection processed the activity.
        ws_active.send_text(json.dumps({"type": "watch", "session_ids": [session_id]}))
        _recv_until(ws_active, {"snapshot"})

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
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
        ) as ws_active,
        client.websocket_connect(
            "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
        ) as ws_idle,
    ):
        _hello(ws_active, "dev_a")
        _hello(ws_idle, "dev_b")
        ws_active.send_text(json.dumps({"type": "watch", "session_ids": []}))
        _recv_until(ws_active, {"snapshot"})
        ws_idle.send_text(json.dumps({"type": "watch", "session_ids": [session_id]}))
        _recv_until(ws_idle, {"snapshot"})
        ws_active.send_text(json.dumps({"type": "activity"}))
        # A follow-up snapshot proves the active connection processed it.
        ws_active.send_text(json.dumps({"type": "watch", "session_ids": []}))
        _recv_until(ws_active, {"snapshot"})

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
