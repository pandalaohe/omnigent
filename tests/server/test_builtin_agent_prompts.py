"""Starting prompts remain owner scoped across save, restore and session init."""

import hashlib
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.db.utils import builtin_agent_id
from omnigent.server.builtin_agent_prompts import NAMESPACE, session_instructions_override
from omnigent.server.user_preferences_store import (
    SqlAlchemyUserPreferencesStore,
    UserPreferencesValidationError,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import build_agent_bundle, create_test_agent


@pytest.mark.asyncio
async def test_session_prompt_create_fork_and_utf8_limit(client, db_uri):
    agent = await create_test_agent(client)
    response = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "session_prompt": "本次会话\n追加"}
    )
    assert response.status_code == 201, response.text
    store = SqlAlchemyConversationStore(db_uri)
    source = store.get_conversation(response.json()["id"])
    assert source.session_prompt == "本次会话\n追加"
    fork = store.fork_conversation(source.id)
    assert fork.session_prompt == source.session_prompt
    too_big = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "session_prompt": "中" * 5462}
    )
    assert too_big.status_code == 422


@pytest.mark.asyncio
async def test_runner_rebind_carries_current_owner_prompt(db_uri, tmp_path, monkeypatch):
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.server.routes import sessions
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from tests.server.test_user_preferences import _preferences_app

    permissions = SqlAlchemyPermissionStore(db_uri)
    app = _preferences_app(db_uri, tmp_path, permission_store=permissions)
    agent_id = builtin_agent_id("claude-sdk")
    bundle = build_agent_bundle(
        "claude-sdk", executor={"type": "omnigent", "config": {"harness": "claude-sdk"}}
    )
    location = f"{agent_id}/bundle"
    app.state.agent_cache._artifact_store.put(location, bundle)
    app.state.agent_store.create(agent_id, "claude-sdk", location)
    conversation = app.state.conversation_store.create_conversation(agent_id=agent_id)
    permissions.ensure_user("alice")
    permissions.grant("alice", conversation.id, LEVEL_OWNER)
    captured = []

    def receive(request):
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(409, json={"error": "stop after init snapshot"})

    monkeypatch.setattr(sessions, "_registered_runner_id", lambda _router, raw, **_: raw)
    monkeypatch.setattr(app.state.runner_router, "require_mcp_registry_support", lambda _: None)
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(receive), base_url="http://runner"
        ) as runner,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            headers={"X-Test-User": "alice"},
        ) as client,
    ):
        monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=runner))
        monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", AsyncMock())
        session_id = conversation.id
        original = (await client.get(f"/v1/agents/{agent_id}/prompt")).json()
        for text in ("Saved owner prompt", None):
            saved = await client.patch(
                f"/v1/agents/{agent_id}/prompt",
                json={"text": text, "base_sha256": original["base_sha256"]},
            )
            assert saved.status_code == 200, saved.text
            rebound = await client.patch(
                f"/v1/sessions/{session_id}", json={"runner_id": "runner-current"}
            )
            assert rebound.status_code == 200, rebound.text
            assert captured[-1]["session_init"]["snapshot"]["instructions_override"] == text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,harness",
    [
        ("claude-sdk", "claude-sdk"),
        ("codex-sdk", "codex"),
        ("pi-rpc", "pi"),
        ("polly", "claude-sdk"),
        ("deleted-account", "claude-sdk"),
    ],
)
async def test_builtin_prompt_save_restore_and_owner_scope(db_uri, tmp_path, name, harness):
    from tests.server.test_user_preferences import _deleted_account_app, _preferences_app

    app = (_deleted_account_app if name == "deleted-account" else _preferences_app)(
        db_uri, tmp_path
    )
    agent_store = app.state.agent_store
    cache = app.state.agent_cache
    artifact_store = cache._artifact_store
    agent_id = builtin_agent_id(name)
    bundle = build_agent_bundle(
        name, executor={"type": "omnigent", "config": {"harness": harness}}
    )
    location = f"{agent_id}/{hashlib.sha256(bundle).hexdigest()}"
    artifact_store.put(location, bundle)
    agent_store.create(agent_id, name, location)
    preferences = app.state.user_preferences_store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"X-Test-User": "alice"},
    ) as client:
        route = f"/v1/agents/{agent_id}/prompt"
        initial_response = await client.get(route)
        assert initial_response.status_code == 200, initial_response.text
        original = initial_response.json()
        saved = await client.patch(
            route, json={"text": "Owner brief", "base_sha256": original["base_sha256"]}
        )
        if name == "deleted-account":
            assert saved.status_code == 401, saved.text
            assert preferences.get("alice") is None
            return
        assert saved.status_code == 200, saved.text
        assert saved.json()["text"] == "Owner brief"
        assert saved.json()["customized"] is True
        other = await client.get(route, headers={"X-Test-User": "bob"})
        assert other.json()["customized"] is False
        assert other.json()["text"] == original["text"]
        conv = SqlAlchemyConversationStore(db_uri).create_conversation(agent_id=agent_id)
        assert (
            session_instructions_override(
                conv,
                owner="alice",
                agent_store=agent_store,
                agent_cache=cache,
                preferences=preferences,
            )
            == "Owner brief"
        )
        assert (cache.load(agent_id, location).spec.instructions or "") == original["text"]
        moved = await client.patch(route, json={"text": "Old version", "base_sha256": "0" * 64})
        assert moved.json()["builtin_changed"] is True
        restored = await client.patch(
            route, json={"text": None, "base_sha256": original["base_sha256"]}
        )
        assert restored.json()["customized"] is False
        assert agent_id not in preferences.get("alice")["settings"][NAMESPACE]
        assert (
            session_instructions_override(
                conv,
                owner="alice",
                agent_store=agent_store,
                agent_cache=cache,
                preferences=preferences,
            )
            is None
        )


def test_prompt_preferences_allow_large_prompts_without_expanding_other_namespaces(db_uri):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    agent_id = builtin_agent_id("claude-sdk")
    saved = {"text": "中" * 262144, "base_sha256": "a" * 64, "saved_at": 1}
    store.patch_namespace("alice", NAMESPACE, {agent_id: saved})
    assert store.get("alice")["settings"][NAMESPACE][agent_id] == saved
    with pytest.raises(UserPreferencesValidationError):
        store.patch_namespace("alice", "agent_badges", "x" * 65536)
    with pytest.raises(UserPreferencesValidationError):
        store.patch_namespace("alice", NAMESPACE, {agent_id: {**saved, "text": "x" * 262145}})


def test_mysql_prompt_storage_keeps_the_compression_codec():
    from sqlalchemy.dialects import mysql

    from omnigent.db.compression import decode
    from omnigent.db.db_models import SqlPreference

    dialect = mysql.dialect()
    column = SqlPreference.__table__.c.value
    storage = column.type.dialect_impl(dialect)
    assert column.type.compile(dialect=dialect) == "MEDIUMBLOB"
    encoded = storage.bind_processor(dialect)("Starting prompt")
    assert isinstance(encoded, bytes)
    assert decode(encoded) == "Starting prompt"


def test_session_prompt_migration_preserves_existing_rows(tmp_path):
    from importlib import import_module

    import sqlalchemy as sa

    from tests.db.test_migration_preferences import _run

    migration = import_module("omnigent.db.migrations.versions.a54c20261106_add_session_prompt")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'session-prompts.db'}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE conversations (id VARCHAR PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO conversations (id) VALUES ('existing')"))
    _run(engine, migration)
    with engine.begin() as connection:
        assert (
            connection.execute(sa.text("SELECT session_prompt FROM conversations")).scalar()
            is None
        )
        connection.execute(sa.text("UPDATE conversations SET session_prompt = 'Only this chat'"))
    _run(engine, migration, "downgrade")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT id FROM conversations")).scalar() == "existing"
    assert {column["name"] for column in sa.inspect(engine).get_columns("conversations")} == {"id"}
    engine.dispose()
