"""Focused tests for owner-triggered native sub-agent status reconciliation."""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.server import session_live_state
from omnigent.server.routes._sessions import (
    subagent_reconciliation as reconciliation_module,
)
from omnigent.server.routes._sessions.subagent_reconciliation import (
    _FINGERPRINT_LABEL_KEYS,
)
from omnigent.server.routes.sessions import routes_items as routes_items_module
from omnigent.stores.conversation_store import sqlalchemy_store as sqlalchemy_store_module
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_agent

_PARENT_WRAPPER = "claude-code-native-ui"
_CHILD_WRAPPER = "claude-code-native-ui-subagent"
_SUBAGENT_ID_KEY = "omnigent.claude_native.subagent_id"
_TOOL_USE_ID_KEY = "omnigent.claude_native.tool_use_id"
_TERMINAL_KEY = "omnigent.subagent.terminal_status"
_UNVERIFIED_KEY = "omnigent.subagent.activity_unverified"
_ERROR_CODE_KEY = "omnigent.last_task_error_code"
_ERROR_MESSAGE_KEY = "omnigent.last_task_error_message"


def _seed_native_child(
    store: SqlAlchemyConversationStore,
    *,
    parent_id: str,
    agent_id: str | None = None,
) -> Any:
    child = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent_id,
        title="Explore:repair-me",
        agent_id=agent_id,
    )
    store.set_labels(
        child.id,
        {
            "omnigent.wrapper": _CHILD_WRAPPER,
            _SUBAGENT_ID_KEY: "agent-a",
            _TOOL_USE_ID_KEY: "tool-a",
        },
    )
    return child


def _probe_payload(
    parent_id: str,
    child_id: str,
    *,
    status: str = "terminal",
    terminal_status: str | None = "completed",
    reason: str = "structured_parent_terminal_evidence",
) -> dict[str, Any]:
    evidence_key = hashlib.sha256(
        "\0".join(
            (
                parent_id,
                "claude-session-a",
                "123",
                "agent-a",
                "tool-a",
                terminal_status or "",
            )
        ).encode()
    ).hexdigest()
    return {
        "parent_session_id": parent_id,
        "claude_session_id": "claude-session-a",
        "parent_complete_byte_offset": 123,
        "children": [
            {
                "server_session_id": child_id,
                "subagent_id": "agent-a",
                "tool_use_id": "tool-a",
                "status": status,
                "terminal_status": terminal_status,
                "reason": reason,
                "evidence": {
                    "claude_session_id": "claude-session-a",
                    "parent_complete_byte_offset": 123,
                    "subagent_id": "agent-a",
                    "meta_tool_use_id": "tool-a",
                    "evidence_key": evidence_key,
                },
            }
        ],
    }


async def _seed_parent_via_api(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    name: str,
) -> dict[str, Any]:
    agent = await create_test_agent(client, name=name)
    response = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert response.status_code == 201, response.text
    parent = response.json()
    store.set_labels(parent["id"], {"omnigent.wrapper": _PARENT_WRAPPER})
    assert store.set_external_session_id(parent["id"], "claude-session-a")
    return parent


def _runner_client(payload: dict[str, Any], status_code: int = 200) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(status_code, json=payload)),
        base_url="http://runner.test",
    )


@pytest.mark.asyncio
async def test_reconcile_route_corrects_only_reliable_terminal_metadata(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, "reconcile-terminal-parent")
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.set_session_live_status(child.id, "failed")
    store.set_labels(
        child.id,
        {
            _UNVERIFIED_KEY: "true",
            _ERROR_CODE_KEY: "stale_transport_error",
            _ERROR_MESSAGE_KEY: "stale failure",
        },
    )
    reconciliation_module._session_status_cache[child.id] = "activity_unverified"
    reconciliation_module._session_active_response_cache[child.id] = "old-response"
    reconciliation_module._session_background_task_count_cache[child.id] = 1
    reconciliation_module._session_background_tasks_cache[child.id] = []
    published: list[tuple[str, dict[str, Any]]] = []
    parent_updates: list[tuple[str, str]] = []
    monkeypatch.setattr(
        reconciliation_module.session_stream,
        "publish",
        lambda session_id, payload: published.append((session_id, payload)),
    )
    monkeypatch.setattr(
        reconciliation_module,
        "_publish_child_status_to_parent",
        lambda session_id, status: parent_updates.append((session_id, status)),
    )
    runner = _runner_client(_probe_payload(parent["id"], child.id))

    async def _existing_runner(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(routes_items_module, "_get_runner_client", _existing_runner)
    try:
        response = await client.post(f"/v1/sessions/{parent['id']}/child_sessions/reconcile")
    finally:
        await runner.aclose()

    assert response.status_code == 200, response.text
    body = response.json()
    assert {key: body[key] for key in ("corrected", "unchanged", "unverified")} == {
        "corrected": 1,
        "unchanged": 0,
        "unverified": 0,
    }
    repaired = store.get_conversation(child.id)
    assert repaired is not None
    assert repaired.live_status == "idle"
    assert repaired.labels[_TERMINAL_KEY] == "completed"
    assert repaired.labels[_UNVERIFIED_KEY] == ""
    assert repaired.labels[_ERROR_CODE_KEY] == ""
    assert repaired.labels[_ERROR_MESSAGE_KEY] == ""
    assert reconciliation_module._session_status_cache[child.id] == "idle"
    assert child.id not in reconciliation_module._session_active_response_cache
    assert child.id not in reconciliation_module._session_background_task_count_cache
    assert child.id not in reconciliation_module._session_background_tasks_cache
    assert parent_updates == [(child.id, "idle")]
    assert any(
        session_id == child.id
        and payload["type"] == "session.status"
        and payload["status"] == "idle"
        and payload["background_task_count"] == 0
        for session_id, payload in published
    )


@pytest.mark.asyncio
async def test_reconcile_route_preserves_unverified_child(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, "reconcile-unverified-parent")
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.set_session_live_status(child.id, "running")
    runner = _runner_client(
        _probe_payload(
            parent["id"],
            child.id,
            status="unverified",
            terminal_status=None,
            reason="independent_child_runtime_present",
        )
    )

    async def _existing_runner(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(routes_items_module, "_get_runner_client", _existing_runner)
    try:
        response = await client.post(f"/v1/sessions/{parent['id']}/child_sessions/reconcile")
    finally:
        await runner.aclose()

    assert response.status_code == 200, response.text
    assert response.json()["unverified"] == 1
    unchanged = store.get_conversation(child.id)
    assert unchanged is not None
    assert unchanged.live_status == "running"
    assert _TERMINAL_KEY not in unchanged.labels


@pytest.mark.asyncio
async def test_reconcile_route_keeps_a_reliable_failure_failed(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, "reconcile-failed-parent")
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.set_session_live_status(child.id, "running")
    store.set_labels(
        child.id,
        {_ERROR_CODE_KEY: "real_failure", _ERROR_MESSAGE_KEY: "real failure"},
    )
    runner = _runner_client(_probe_payload(parent["id"], child.id, terminal_status="failed"))

    async def _existing_runner(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(routes_items_module, "_get_runner_client", _existing_runner)
    try:
        response = await client.post(f"/v1/sessions/{parent['id']}/child_sessions/reconcile")
    finally:
        await runner.aclose()

    assert response.status_code == 200, response.text
    assert response.json()["corrected"] == 1
    failed = store.get_conversation(child.id)
    assert failed is not None
    assert failed.live_status == "failed"
    assert failed.labels[_TERMINAL_KEY] == "failed"
    assert failed.labels[_ERROR_CODE_KEY] == "real_failure"
    assert failed.labels[_ERROR_MESSAGE_KEY] == "real failure"


@pytest.mark.asyncio
async def test_reconcile_route_repairs_stale_display_cache_when_db_is_terminal(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, "reconcile-cache-parent")
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.set_session_live_status(child.id, "idle")
    store.set_labels(
        child.id,
        {_TERMINAL_KEY: "completed", _UNVERIFIED_KEY: ""},
    )
    reconciliation_module._session_status_cache[child.id] = "running"
    reconciliation_module._session_active_response_cache[child.id] = "stale-response"
    reconciliation_module._session_background_task_count_cache[child.id] = 1
    session_live_state._last_status[child.id] = "running"
    persisted: list[tuple[str, str]] = []

    def _submit_now(_description: str, fn: Any, *args: Any, **_kwargs: Any) -> None:
        if _description == "live_status":
            persisted.append((args[0], args[1]))
        fn(*args)

    monkeypatch.setattr(
        session_live_state,
        "submit",
        _submit_now,
    )
    monkeypatch.setattr(session_live_state, "_store", store)
    runner = _runner_client(_probe_payload(parent["id"], child.id))

    async def _existing_runner(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(routes_items_module, "_get_runner_client", _existing_runner)
    try:
        response = await client.post(f"/v1/sessions/{parent['id']}/child_sessions/reconcile")
    finally:
        await runner.aclose()

    assert response.status_code == 200, response.text
    assert response.json()["corrected"] == 1
    assert response.json()["unchanged"] == 0
    assert reconciliation_module._session_status_cache[child.id] == "idle"
    assert child.id not in reconciliation_module._session_active_response_cache
    assert child.id not in reconciliation_module._session_background_task_count_cache
    assert session_live_state._last_status[child.id] == "idle"
    session_live_state.persist_live_status(child.id, "running")
    assert persisted[-1] == (child.id, "running")
    current = store.get_conversation(child.id)
    assert current is not None and current.live_status == "running"


@pytest.mark.asyncio
async def test_reconcile_route_counts_late_server_activity_as_unverified(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, "reconcile-race-parent")
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.set_session_live_status(child.id, "idle")
    payload = _probe_payload(parent["id"], child.id)

    def _probe_after_new_activity(_request: httpx.Request) -> httpx.Response:
        store.set_session_live_status(child.id, "running")
        return httpx.Response(200, json=payload)

    runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_probe_after_new_activity),
        base_url="http://runner.test",
    )

    async def _existing_runner(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(routes_items_module, "_get_runner_client", _existing_runner)
    try:
        response = await client.post(f"/v1/sessions/{parent['id']}/child_sessions/reconcile")
    finally:
        await runner.aclose()

    assert response.status_code == 200, response.text
    assert response.json()["corrected"] == 0
    assert response.json()["unverified"] == 1
    current = store.get_conversation(child.id)
    assert current is not None and current.live_status == "running"
    assert _TERMINAL_KEY not in current.labels


@pytest.mark.asyncio
async def test_reconcile_route_rejects_parent_binding_change_during_probe(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, "reconcile-parent-race")
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.set_session_live_status(child.id, "running")
    payload = _probe_payload(parent["id"], child.id)

    def _probe_after_rebind(_request: httpx.Request) -> httpx.Response:
        assert store.set_runner_id(parent["id"], "runner-new")
        return httpx.Response(200, json=payload)

    runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_probe_after_rebind),
        base_url="http://runner.test",
    )

    async def _existing_runner(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(routes_items_module, "_get_runner_client", _existing_runner)
    try:
        response = await client.post(f"/v1/sessions/{parent['id']}/child_sessions/reconcile")
    finally:
        await runner.aclose()

    assert response.status_code == 409
    current = store.get_conversation(child.id)
    assert current is not None and current.live_status == "running"
    assert _TERMINAL_KEY not in current.labels


@pytest.mark.asyncio
async def test_reconcile_route_counts_child_binding_change_as_unverified(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, "reconcile-child-binding-race")
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.set_session_live_status(child.id, "running")
    payload = _probe_payload(parent["id"], child.id)

    def _probe_after_child_rebind(_request: httpx.Request) -> httpx.Response:
        assert store.set_external_session_id(child.id, "independent-child-session")
        return httpx.Response(200, json=payload)

    runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_probe_after_child_rebind),
        base_url="http://runner.test",
    )

    async def _existing_runner(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(routes_items_module, "_get_runner_client", _existing_runner)
    try:
        response = await client.post(f"/v1/sessions/{parent['id']}/child_sessions/reconcile")
    finally:
        await runner.aclose()

    assert response.status_code == 200
    assert response.json()["unverified"] == 1
    current = store.get_conversation(child.id)
    assert current is not None and current.live_status == "running"
    assert _TERMINAL_KEY not in current.labels


@pytest.mark.asyncio
async def test_reconcile_route_preserves_same_status_new_turn_during_cas(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, "reconcile-display-race")
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.set_session_live_status(child.id, "running")
    reconciliation_module._session_status_cache[child.id] = "running"
    reconciliation_module._session_active_response_cache[child.id] = "response-old"
    session_live_state._last_status[child.id] = "running"
    persisted: list[tuple[str, str]] = []

    def _submit_now(_description: str, fn: Any, *args: Any, **_kwargs: Any) -> None:
        if _description == "live_status":
            persisted.append((args[0], args[1]))
        fn(*args)

    monkeypatch.setattr(session_live_state, "submit", _submit_now)
    monkeypatch.setattr(session_live_state, "_store", store)
    original_reconcile = SqlAlchemyConversationStore.reconcile_native_subagent_status

    def _reconcile_then_start_new_turn(
        self: SqlAlchemyConversationStore, *args: Any, **kwargs: Any
    ) -> Any:
        result = original_reconcile(self, *args, **kwargs)
        reconciliation_module._session_active_response_cache[child.id] = "response-new"
        return result

    monkeypatch.setattr(
        SqlAlchemyConversationStore,
        "reconcile_native_subagent_status",
        _reconcile_then_start_new_turn,
    )
    runner = _runner_client(_probe_payload(parent["id"], child.id))

    async def _existing_runner(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(routes_items_module, "_get_runner_client", _existing_runner)
    try:
        response = await client.post(f"/v1/sessions/{parent['id']}/child_sessions/reconcile")
    finally:
        await runner.aclose()

    assert response.status_code == 200
    assert response.json()["corrected"] == 0
    assert response.json()["unverified"] == 1
    assert reconciliation_module._session_status_cache[child.id] == "running"
    assert reconciliation_module._session_active_response_cache[child.id] == "response-new"
    assert persisted[-2:] == [(child.id, "idle"), (child.id, "running")]
    current = store.get_conversation(child.id)
    assert current is not None and current.live_status == "running"


@pytest.mark.asyncio
async def test_reconcile_route_reports_old_host_without_changing_state(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, "reconcile-old-host-parent")
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.set_session_live_status(child.id, "running")
    runner = _runner_client({"detail": "not found"}, status_code=404)

    async def _existing_runner(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(routes_items_module, "_get_runner_client", _existing_runner)
    try:
        response = await client.post(f"/v1/sessions/{parent['id']}/child_sessions/reconcile")
    finally:
        await runner.aclose()

    assert response.status_code == 503
    assert "Update the custom Host" in response.text
    unchanged = store.get_conversation(child.id)
    assert unchanged is not None and unchanged.live_status == "running"


def test_reconcile_cas_rejects_new_running_edge_without_new_item(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = _seed_native_child(store, parent_id=parent.id)
    store.set_session_live_status(child.id, "idle")
    frozen = store.get_native_subagent_reconcile_fingerprint(child.id, _FINGERPRINT_LABEL_KEYS)
    assert frozen is not None

    store.set_session_live_status(child.id, "running")
    result = store.reconcile_native_subagent_status(
        frozen,
        live_status="idle",
        label_updates={_TERMINAL_KEY: "completed", _UNVERIFIED_KEY: ""},
    )

    assert result == "stale"
    current = store.get_conversation(child.id)
    assert current is not None and current.live_status == "running"


def test_reconcile_cas_rejects_failure_label_change_at_same_live_status(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = _seed_native_child(store, parent_id=parent.id)
    store.set_session_live_status(child.id, "failed")
    store.set_labels(
        child.id,
        {_ERROR_CODE_KEY: "old", _ERROR_MESSAGE_KEY: "old failure"},
    )
    frozen = store.get_native_subagent_reconcile_fingerprint(child.id, _FINGERPRINT_LABEL_KEYS)
    assert frozen is not None

    store.set_labels(
        child.id,
        {_ERROR_CODE_KEY: "new", _ERROR_MESSAGE_KEY: "new failure"},
    )
    result = store.reconcile_native_subagent_status(
        frozen,
        live_status="idle",
        label_updates={
            _TERMINAL_KEY: "completed",
            _UNVERIFIED_KEY: "",
            _ERROR_CODE_KEY: "",
            _ERROR_MESSAGE_KEY: "",
        },
    )

    assert result == "stale"
    current = store.get_conversation(child.id)
    assert current is not None
    assert current.labels[_ERROR_CODE_KEY] == "new"
    assert current.labels[_ERROR_MESSAGE_KEY] == "new failure"


def test_reconcile_cas_rejects_new_latest_item(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = _seed_native_child(store, parent_id=parent.id)
    store.set_session_live_status(child.id, "running")
    frozen = store.get_native_subagent_reconcile_fingerprint(child.id, _FINGERPRINT_LABEL_KEYS)
    assert frozen is not None

    store.append(
        child.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp-new",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "new activity"}],
                    agent="test-agent",
                ),
            )
        ],
    )
    result = store.reconcile_native_subagent_status(
        frozen,
        live_status="idle",
        label_updates={_TERMINAL_KEY: "completed", _UNVERIFIED_KEY: ""},
    )

    assert result == "stale"
    current = store.get_conversation(child.id)
    assert current is not None and current.live_status == "running"
    assert _TERMINAL_KEY not in current.labels


def test_reconcile_cas_rejects_missing_label_insert_during_apply(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = _seed_native_child(store, parent_id=parent.id)
    store.set_session_live_status(child.id, "running")
    frozen = store.get_native_subagent_reconcile_fingerprint(child.id, _FINGERPRINT_LABEL_KEYS)
    assert frozen is not None

    original_execute = Session.execute
    inserted = False

    def _insert_conflict_before_guard(
        self: Session, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal inserted
        if not inserted and "DO NOTHING" in str(statement):
            inserted = True
            self.add(
                sqlalchemy_store_module.SqlConversationLabel(
                    conversation_id=child.id,
                    key=_TERMINAL_KEY,
                    value="",
                    updated_at=1,
                )
            )
            self.flush()
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", _insert_conflict_before_guard)
    result = store.reconcile_native_subagent_status(
        frozen,
        live_status="idle",
        label_updates={_TERMINAL_KEY: "completed", _UNVERIFIED_KEY: ""},
    )

    assert inserted
    assert result == "stale"
    current = store.get_conversation(child.id)
    assert current is not None and current.live_status == "running"
    assert _TERMINAL_KEY not in current.labels
