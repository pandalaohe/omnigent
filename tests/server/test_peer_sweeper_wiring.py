"""App wiring: ``app.state.peer_sweeper`` and its lifespan start/stop.

Builds the real app (flag on/off, store present/absent) and runs its
actual lifespan via ``TestClient``'s context manager, rather than faking
anything — this is the one place that exercises the production
``register_peer_routes`` → ``PeerSweeper`` → app.py lifespan wiring
end to end.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnigent.host.frames import CAP_KEEP_WARM, HostHelloFrame
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL, UnifiedAuthProvider
from omnigent.server.child_keep_warm import KEEP_WARM_LABEL, ChildKeepWarmSweeper
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.peer_sweeper import PeerSweeper
from omnigent.server.session_live_state import RUNNING_SINCE_LABEL_KEY
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.peer_message_store.sqlalchemy_store import SqlAlchemyPeerMessageStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


def _build_app(db_uri: str, tmp_path: Path, *, flag_on: bool, with_store: bool) -> Any:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    flags = resolve_feature_flags(
        {"OMNIGENT_FEATURES": "session_peer_messaging"} if flag_on else {}
    )
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
        feature_flags=flags,
        peer_message_store=SqlAlchemyPeerMessageStore(db_uri) if with_store else None,
        user_preferences_store=SqlAlchemyUserPreferencesStore(db_uri),
    )


def test_flag_on_with_store_starts_and_stops_sweeper(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    app = _build_app(db_uri, tmp_path, flag_on=True, with_store=True)
    sweeper = app.state.peer_sweeper
    assert isinstance(sweeper, PeerSweeper)
    assert sweeper._task is None
    with TestClient(app):
        assert sweeper._task is not None
        assert not sweeper._task.done()
    assert sweeper._task is None


def test_flag_off_no_sweeper(runtime_init: None, db_uri: str, tmp_path: Path) -> None:
    app = _build_app(db_uri, tmp_path, flag_on=False, with_store=True)
    assert app.state.peer_sweeper is None
    with TestClient(app):
        pass
    assert app.state.peer_sweeper is None


def test_flag_on_no_store_no_sweeper(runtime_init: None, db_uri: str, tmp_path: Path) -> None:
    app = _build_app(db_uri, tmp_path, flag_on=True, with_store=False)
    assert app.state.peer_sweeper is None
    with TestClient(app):
        pass


def test_flag_off_still_starts_the_keep_warm_sweeper(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    """SCC28 A1: keep-warm runs whether or not peer messaging is on."""
    app = _build_app(db_uri, tmp_path, flag_on=False, with_store=True)
    keep_warm = app.state.child_keep_warm
    assert isinstance(keep_warm, ChildKeepWarmSweeper)
    assert keep_warm._notify_line is None
    assert keep_warm._task is None
    with TestClient(app):
        assert keep_warm._task is not None
        assert not keep_warm._task.done()
    assert keep_warm._task is None


_HOST_ID = "6f6e1d2c3b4a5968778899aabbccddee"


class _FakeHostWebSocket:
    """Minimal host WebSocket stand-in (the registry only enqueues)."""

    async def send_text(self, data: str) -> None:
        del data


def _register_host(app: Any, *, capabilities: list[str] | None = None) -> None:
    app.state.host_registry.register(
        host_id=_HOST_ID,
        ws=_FakeHostWebSocket(),
        hello=HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name="keep-warm-host",
            capabilities=capabilities or [],
        ),
        owner=RESERVED_USER_LOCAL,
    )


@pytest.mark.asyncio
async def test_keep_warm_host_gate_follows_the_inherited_placement(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    """A child with no host_id of its own is gated on its INHERITED host.

    The due ping stays blocked (stop reason ``host``) while the parent's
    host lacks ``keep_warm_v1``, and goes out once the host advertises the
    capability — a gate reading the child row's own empty host_id would
    have counted the child hostless and pinged an incapable host.
    """
    app = _build_app(db_uri, tmp_path, flag_on=False, with_store=True)
    keep_warm = app.state.child_keep_warm
    keep_warm._app = app
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = "c0ffee" * 5 + "c0"
    app.state.user_preferences_store.patch_namespace(
        RESERVED_USER_LOCAL,
        "keep_warm",
        {"agents": {agent_id: {"main": True, "child": True}}},
    )
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "keep-warm-host", RESERVED_USER_LOCAL)

    now = int(time.time())
    u = now - 55 * 60
    parent = store.create_conversation(
        title="parent", host_id=_HOST_ID, workspace="/opt/work/keep-warm"
    )
    child = store.create_conversation(
        kind="sub_agent",
        title="researcher:task",
        parent_conversation_id=parent.id,
        agent_id=agent_id,
        harness_override="claude-native",
    )
    store.set_session_live_status(child.id, "idle")
    warm = json.dumps({"s": "w", "t": u, "c": u, "u": u, "w": u + 3600}, separators=(",", ":"))
    store.set_labels(child.id, {RUNNING_SINCE_LABEL_KEY: str(u), KEEP_WARM_LABEL: warm})

    async def _drive() -> dict[str, Any]:
        await keep_warm._process_session(child.id, now, {}, {})
        if keep_warm._ping_tasks:
            await asyncio.gather(*list(keep_warm._ping_tasks))
        conv = store.get_conversation(child.id)
        assert conv is not None
        return json.loads(conv.labels[KEEP_WARM_LABEL])

    _register_host(app)
    state = await _drive()
    assert state.get("a") is None and state.get("k") == "host"

    _register_host(app, capabilities=[CAP_KEEP_WARM])
    state = await _drive()
    assert state.get("a") is not None and "k" not in state
