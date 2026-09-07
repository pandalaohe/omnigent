"""Focused tests for owner-triggered native sub-agent status reconciliation."""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from omnigent.entities.conversation import (
    MessageData,
    NewConversationItem,
    ResourceEventData,
)
from omnigent.runtime import pending_elicitations
from omnigent.server import session_live_state
from omnigent.server.routes._sessions import (
    helpers as helpers_module,
)
from omnigent.server.routes._sessions import (
    subagent_reconciliation as reconciliation_module,
)
from omnigent.server.routes._sessions.subagent_reconciliation import (
    _FINGERPRINT_LABEL_KEYS,
    _PARENT_RUNTIME_LABEL_KEYS,
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
_GENERATION_KEY = "omnigent.subagent.status_generation"
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
        "bridge_id": parent_id,
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


async def _seed_running_native_pair(
    client: httpx.AsyncClient,
    db_uri: str,
    name: str,
    *,
    parent_runner: str = "runner-parent",
    child_runner: str = "runner-parent",
) -> tuple[SqlAlchemyConversationStore, dict[str, Any], Any, Any]:
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, name)
    store.replace_runner_id(parent["id"], parent_runner)
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.replace_runner_id(child.id, child_runner)
    store.set_session_live_status(child.id, "running")
    reconciliation_module._session_status_cache[child.id] = "running"
    parent_row = store.get_conversation(parent["id"])
    assert parent_row is not None
    return store, parent, child, parent_row


def _runner_client(payload: dict[str, Any], status_code: int = 200) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(status_code, json=payload)),
        base_url="http://runner.test",
    )


@pytest.mark.asyncio
async def test_first_complete_child_list_repairs_stale_native_terminal_state(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening Agents repairs restart-stale activity from the existing bridge."""
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, "lazy-reconcile-parent")
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.set_session_live_status(child.id, "running")
    reconciliation_module._session_status_cache[child.id] = "running"
    runner = _runner_client(_probe_payload(parent["id"], child.id))

    async def _existing_runner(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(routes_items_module, "_get_runner_client", _existing_runner)
    try:
        response = await client.get(f"/v1/sessions/{parent['id']}/child_sessions?limit=1000")
    finally:
        await runner.aclose()
        reconciliation_module._session_status_cache.pop(child.id, None)

    assert response.status_code == 200, response.text
    row = next(value for value in response.json()["data"] if value["id"] == child.id)
    assert row["busy"] is False
    assert row["current_task_status"] == "completed"
    assert parent["id"] not in routes_items_module._subagent_reconcile_locks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_runner", "child_runner", "observed_runner"),
    [
        ("runner-parent", "runner-child", "runner-parent"),
        ("runner-new", "runner-new", "runner-old"),
    ],
    ids=["child-rebound", "stale-relay"],
)
async def test_missing_parent_terminal_rejects_mismatched_runner_binding(
    client: httpx.AsyncClient,
    db_uri: str,
    parent_runner: str,
    child_runner: str,
    observed_runner: str,
) -> None:
    store, parent, child, parent_row = await _seed_running_native_pair(
        client,
        db_uri,
        "runner-mismatch-parent",
        parent_runner=parent_runner,
        child_runner=child_runner,
    )
    try:
        changed = (
            await reconciliation_module.invalidate_native_subagents_for_missing_parent_terminal(
                parent_session_id=parent["id"],
                parent=parent_row,
                conversation_store=store,
                observed_runner_id=observed_runner,
            )
        )
        current = store.get_conversation(child.id)
        assert current is not None
        assert changed == 0
        assert current.labels.get(_UNVERIFIED_KEY) != "true"
        assert reconciliation_module._session_status_cache[child.id] == "running"
    finally:
        reconciliation_module._session_status_cache.pop(child.id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "race",
    [
        "child_rebind",
        "parent_rebind",
        "terminal_recreated_during_cas",
    ],
)
async def test_missing_parent_terminal_cas_rejects_changed_runtime_evidence(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    store, parent, child, parent_row = await _seed_running_native_pair(
        client, db_uri, f"missing-terminal-{race}"
    )
    changed_evidence = False
    original_fingerprint = SqlAlchemyConversationStore.get_native_subagent_reconcile_fingerprint
    original_list = SqlAlchemyConversationStore.list_conversations

    def _recreate_terminal(target: SqlAlchemyConversationStore) -> None:
        nonlocal changed_evidence
        changed_evidence = True
        target.append(
            parent["id"],
            [
                NewConversationItem(
                    type="resource_event",
                    response_id="terminal-recreated",
                    data=ResourceEventData(
                        event_type="session.resource.created",
                        resource_id="terminal_claude_main",
                        resource_type="terminal",
                        resource={"id": "terminal_claude_main", "type": "terminal"},
                    ),
                )
            ],
        )

    def _fingerprint_after_child_rebind(
        self: SqlAlchemyConversationStore,
        conversation_id: str,
        label_keys: tuple[str, ...],
    ) -> Any:
        nonlocal changed_evidence
        if conversation_id == child.id and not changed_evidence:
            changed_evidence = True
            self.replace_runner_id(child.id, "runner-other")
        return original_fingerprint(self, conversation_id, label_keys)

    def _list_then_change_parent(
        self: SqlAlchemyConversationStore, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal changed_evidence
        page = original_list(self, *args, **kwargs)
        if kwargs.get("parent_conversation_id") != parent["id"] or changed_evidence:
            return page
        changed_evidence = True
        if race == "parent_rebind":
            self.replace_runner_id(parent["id"], "runner-new")
        else:
            _recreate_terminal(self)
        return page

    monkeypatch.setattr(
        SqlAlchemyConversationStore,
        (
            "get_native_subagent_reconcile_fingerprint"
            if race == "child_rebind"
            else "list_conversations"
        ),
        (_fingerprint_after_child_rebind if race == "child_rebind" else _list_then_change_parent),
    )
    try:
        invalidated = (
            await reconciliation_module.invalidate_native_subagents_for_missing_parent_terminal(
                parent_session_id=parent["id"],
                parent=parent_row,
                conversation_store=store,
                observed_runner_id="runner-parent",
            )
        )
        current = store.get_conversation(child.id)
        assert changed_evidence
        assert invalidated == 0
        assert current is not None
        if race == "child_rebind":
            assert current.runner_id == "runner-other"
        assert current.labels.get(_UNVERIFIED_KEY) != "true"
    finally:
        reconciliation_module._session_status_cache.pop(child.id, None)


def test_latest_child_fanout_prefers_durable_terminal_over_quarantine(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = _seed_native_child(store, parent_id=parent.id)
    store.set_session_live_status(child.id, "running")
    store.set_labels(
        child.id,
        {_TERMINAL_KEY: "completed", _UNVERIFIED_KEY: "true"},
    )
    reconciliation_module._session_status_cache[child.id] = "running"
    published: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(session_live_state, "_store", store)
    monkeypatch.setattr(
        session_live_state,
        "submit",
        lambda _description, fn, *args, **_kwargs: fn(*args),
    )
    monkeypatch.setattr(
        helpers_module.session_stream,
        "publish",
        lambda session_id, payload: published.append((session_id, payload)),
    )
    try:
        helpers_module._publish_child_status_to_parent(child.id, None)
        event = next(payload for session_id, payload in published if session_id == parent.id)
        assert event["child"]["current_task_status"] == "completed"
        assert event["child"]["busy"] is False
        assert event["child"]["activity_unverified"] is False
    finally:
        reconciliation_module._session_status_cache.pop(child.id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_activity", ["response", "prompt"])
async def test_missing_parent_terminal_preserves_new_activity_during_cas(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    new_activity: str,
) -> None:
    store, parent, child, parent_row = await _seed_running_native_pair(
        client, db_uri, f"missing-terminal-{new_activity}-race"
    )
    old_prompt = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit-reused",
        "params": {"message": "Old prompt"},
    }
    new_prompt = {**old_prompt, "params": {"message": "New turn prompt"}}
    if new_activity == "response":
        reconciliation_module._session_active_response_cache[child.id] = "response-old"
    else:
        pending_elicitations.record_publish(child.id, old_prompt)
    original_reconcile = SqlAlchemyConversationStore.reconcile_native_subagent_status

    def _reconcile_then_start_new_activity(
        self: SqlAlchemyConversationStore, *args: Any, **kwargs: Any
    ) -> Any:
        result = original_reconcile(self, *args, **kwargs)
        if new_activity == "response":
            reconciliation_module._session_active_response_cache[child.id] = "response-new"
        else:
            pending_elicitations.record_publish(child.id, new_prompt)
        return result

    monkeypatch.setattr(
        SqlAlchemyConversationStore,
        "reconcile_native_subagent_status",
        _reconcile_then_start_new_activity,
    )
    try:
        changed = (
            await reconciliation_module.invalidate_native_subagents_for_missing_parent_terminal(
                parent_session_id=parent["id"],
                parent=parent_row,
                conversation_store=store,
                observed_runner_id="runner-parent",
            )
        )
        current = store.get_conversation(child.id)
        assert current is not None
        assert changed == 0
        assert current.labels.get(_UNVERIFIED_KEY) != "true"
        if new_activity == "response":
            assert reconciliation_module._session_active_response_cache[child.id] == "response-new"
        else:
            assert pending_elicitations.snapshot_for(child.id) == [new_prompt]
    finally:
        pending_elicitations.resolve(child.id, "elicit-reused")
        reconciliation_module._session_status_cache.pop(child.id, None)
        reconciliation_module._session_active_response_cache.pop(child.id, None)


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
    stale_prompt = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit-stale-terminal",
        "params": {"message": "Approve stale work?"},
    }
    pending_elicitations.record_publish(child.id, stale_prompt)
    pending_elicitations.record_publish(
        parent["id"],
        {
            **stale_prompt,
            "params": {
                **stale_prompt["params"],
                "target_session_id": child.id,
            },
        },
    )
    published: list[tuple[str, dict[str, Any]]] = []
    parent_updates: list[tuple[str, str]] = []
    monkeypatch.setattr(
        reconciliation_module.session_stream,
        "publish",
        lambda session_id, payload, **_kwargs: published.append((session_id, payload)),
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
    assert pending_elicitations.count_for(child.id) == 0
    assert pending_elicitations.count_for(parent["id"]) == 0
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
async def test_reconcile_route_preserves_child_on_another_runner(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent transcript evidence cannot settle an independently rebound child."""
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, "reconcile-rebound-parent")
    store.replace_runner_id(parent["id"], "runner-parent")
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.replace_runner_id(child.id, "runner-child")
    store.set_session_live_status(child.id, "running")
    runner = _runner_client(_probe_payload(parent["id"], child.id))

    async def _existing_runner(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(routes_items_module, "_get_runner_client", _existing_runner)
    try:
        response = await client.post(f"/v1/sessions/{parent['id']}/child_sessions/reconcile")
    finally:
        await runner.aclose()

    assert response.status_code == 200, response.text
    assert response.json()["details"] == [
        {
            "session_id": child.id,
            "outcome": "unverified",
            "reason": "child_on_another_runner",
        }
    ]
    current = store.get_conversation(child.id)
    assert current is not None
    assert current.live_status == "running"
    assert _TERMINAL_KEY not in current.labels


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
    assert current.labels.get(_TERMINAL_KEY) in {None, ""}


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


@pytest.mark.asyncio
async def test_reconcile_route_reports_missing_parent_transcript_without_upgrade_advice(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = await _seed_parent_via_api(client, store, "reconcile-missing-transcript")
    child = _seed_native_child(store, parent_id=parent["id"], agent_id=parent["agent_id"])
    store.set_session_live_status(child.id, "running")
    runner = _runner_client(
        {"error": "native_parent_not_found", "detail": "bridge unavailable"},
        status_code=404,
    )

    async def _existing_runner(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(routes_items_module, "_get_runner_client", _existing_runner)
    try:
        response = await client.post(f"/v1/sessions/{parent['id']}/child_sessions/reconcile")
    finally:
        await runner.aclose()

    assert response.status_code == 503
    assert "transcript evidence" in response.text
    assert "Update the custom Host" not in response.text


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


def test_reconcile_cas_rejects_same_second_terminal_running_aba(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = _seed_native_child(store, parent_id=parent.id)
    store.set_session_live_status(child.id, "running")
    store.set_labels(
        child.id,
        {_TERMINAL_KEY: "", _UNVERIFIED_KEY: "", _GENERATION_KEY: "turn-a"},
        updated_at=100,
    )
    frozen = store.get_native_subagent_reconcile_fingerprint(child.id, _FINGERPRINT_LABEL_KEYS)
    assert frozen is not None

    store.set_session_live_status(child.id, "idle")
    store.set_labels(
        child.id,
        {_TERMINAL_KEY: "completed", _UNVERIFIED_KEY: "", _GENERATION_KEY: "turn-b"},
        updated_at=100,
    )
    store.set_session_live_status(child.id, "running")
    store.set_labels(
        child.id,
        {_TERMINAL_KEY: "", _UNVERIFIED_KEY: "", _GENERATION_KEY: "turn-c"},
        updated_at=100,
    )

    result = store.reconcile_native_subagent_status(
        frozen,
        live_status="running",
        label_updates={_UNVERIFIED_KEY: "true", _GENERATION_KEY: "repair"},
    )
    assert result == "stale"
    current = store.get_conversation(child.id)
    assert current is not None and current.live_status == "running"
    assert current.labels[_GENERATION_KEY] == "turn-c"
    assert current.labels[_UNVERIFIED_KEY] == ""


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


@pytest.mark.parametrize("conflict_owner", ["child", "parent"])
def test_reconcile_cas_rejects_missing_label_insert_during_apply(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, conflict_owner: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    parent_frozen = None
    if conflict_owner == "parent":
        store.set_labels(parent.id, {"omnigent.wrapper": _PARENT_WRAPPER})
        store.replace_runner_id(parent.id, "runner-parent")
    child = _seed_native_child(store, parent_id=parent.id)
    if conflict_owner == "parent":
        store.replace_runner_id(child.id, "runner-parent")
        parent_frozen = store.get_native_subagent_reconcile_fingerprint(
            parent.id, _PARENT_RUNTIME_LABEL_KEYS
        )
        assert parent_frozen is not None
    store.set_session_live_status(child.id, "running")
    frozen = store.get_native_subagent_reconcile_fingerprint(child.id, _FINGERPRINT_LABEL_KEYS)
    assert frozen is not None
    conflict_id = parent.id if conflict_owner == "parent" else child.id
    conflict_key = (
        "omnigent.claude_native.bridge_id" if conflict_owner == "parent" else _TERMINAL_KEY
    )

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
                    conversation_id=conflict_id,
                    key=conflict_key,
                    value="bridge-new" if conflict_owner == "parent" else "",
                    updated_at=1,
                )
            )
            self.flush()
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", _insert_conflict_before_guard)
    result = store.reconcile_native_subagent_status(
        frozen,
        expected_parent=parent_frozen,
        live_status="running",
        label_updates={
            _TERMINAL_KEY: "completed",
            _UNVERIFIED_KEY: "",
            _GENERATION_KEY: "repair",
        },
    )

    assert inserted
    assert result == "stale"
    current = store.get_conversation(child.id)
    assert current is not None and current.live_status == "running"
    assert _TERMINAL_KEY not in current.labels
