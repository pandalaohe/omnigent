"""App wiring: ``app.state.peer_sweeper`` and its lifespan start/stop.

Builds the real app (flag on/off, store present/absent) and runs its
actual lifespan via ``TestClient``'s context manager, rather than faking
anything — this is the one place that exercises the production
``register_peer_routes`` → ``PeerSweeper`` → app.py lifespan wiring
end to end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.peer_sweeper import PeerSweeper
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
