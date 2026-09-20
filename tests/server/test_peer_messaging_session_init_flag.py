"""Flag transport for session peer messaging, server side.

The ``Feature.SESSION_PEER_MESSAGING`` state reaches the runner inside
the session-init snapshot: the payload builder writes the field, the
``RunnerSessionInitializer`` sends the value it was constructed with,
and the session-create route passes the app's resolved flags.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities import Conversation
from omnigent.runner.session_init_protocol import (
    build_runner_session_init_payload,
    parse_runner_session_init_envelope,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.assignment_store.sqlalchemy_store import SqlAlchemyAssignmentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_host_binding_store.sqlalchemy_store import (
    SqlAlchemyProjectHostBindingStore,
)
from omnigent.stores.project_repository_store.sqlalchemy_store import (
    SqlAlchemyProjectRepositoryStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

AGENT_ID = "187b7cb7ac30abf4debfaa578d052ec6"


def _conversation() -> Conversation:
    return Conversation(
        id="conv_peer_flag",
        created_at=10,
        updated_at=11,
        root_conversation_id="conv_peer_flag",
        agent_id="agent_peer_flag",
        runner_id="runner_peer_flag",
    )


def test_builder_writes_flag_on() -> None:
    payload = build_runner_session_init_payload(
        _conversation(), server_version="0.6.0.dev0", peer_messaging_enabled=True
    )
    envelope = parse_runner_session_init_envelope(payload)
    assert envelope is not None
    assert envelope.snapshot.peer_messaging_enabled is True


def test_builder_defaults_flag_off() -> None:
    payload = build_runner_session_init_payload(_conversation(), server_version="0.6.0.dev0")
    envelope = parse_runner_session_init_envelope(payload)
    assert envelope is not None
    assert envelope.snapshot.peer_messaging_enabled is False


class _Registry:
    def __init__(self) -> None:
        self.connection: object | None = object()

    def get(self, _runner_id: str) -> object | None:
        return self.connection


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, _path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(kwargs["json"])
        return httpx.Response(201, json={"status": "initialized"})


@pytest.mark.asyncio
async def test_initializer_sends_constructed_flag() -> None:
    conversation = _conversation()
    on = RunnerSessionInitializer(
        _Registry(),  # type: ignore[arg-type]
        server_version="0.6.0.dev0",
        peer_messaging_enabled=True,
    )
    off = RunnerSessionInitializer(
        _Registry(),  # type: ignore[arg-type]
        server_version="0.6.0.dev0",
    )
    on_client, off_client = _Client(), _Client()
    await on.initialize(conversation, on_client, timeout=10)  # type: ignore[arg-type]
    await off.initialize(conversation, off_client, timeout=10)  # type: ignore[arg-type]
    assert on_client.calls[0]["session_init"]["snapshot"]["peer_messaging_enabled"] is True
    assert off_client.calls[0]["session_init"]["snapshot"]["peer_messaging_enabled"] is False


def _build_app(db_uri: str, tmp_path: Path, *, enabled: bool) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    flags = resolve_feature_flags({"OMNIGENT_FEATURES": "session_peer_messaging"} if enabled else {})
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        host_store=HostStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        project_repository_store=SqlAlchemyProjectRepositoryStore(db_uri),
        project_host_binding_store=SqlAlchemyProjectHostBindingStore(db_uri),
        assignment_store=SqlAlchemyAssignmentStore(db_uri),
        feature_flags=flags,
    )


class _RecordingRunnerClient:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    async def post(self, _path: str, **kwargs: Any) -> httpx.Response:
        self.bodies.append(kwargs["json"])
        return httpx.Response(200, json={})


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False], ids=["flag-on", "flag-off"])
async def test_session_create_passes_resolved_flag(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """The create route seeds the runner from the app's flags, not the env."""
    from omnigent.server.routes import sessions as sessions_routes

    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(
            agent_id=AGENT_ID, name="test-agent", bundle_location=f"{AGENT_ID}/bundle"
        )
    app = _build_app(db_uri, tmp_path, enabled=enabled)
    recording = _RecordingRunnerClient()

    async def _fake_runner_client(*_args: Any, **_kwargs: Any) -> Any:
        return recording

    monkeypatch.setattr(sessions_routes, "_get_runner_client", _fake_runner_client)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/sessions", json={"agent_id": AGENT_ID})
    assert resp.status_code == 201, resp.text
    assert len(recording.bodies) == 1
    snapshot = recording.bodies[0]["session_init"]["snapshot"]
    assert snapshot["peer_messaging_enabled"] is enabled
