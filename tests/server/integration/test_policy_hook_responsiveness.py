"""Storage work in native policy hooks must leave other requests responsive."""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar
from dataclasses import replace
from typing import Any
from uuid import uuid4

import httpx
import pytest

from omnigent.policies.function import FunctionPolicy
from omnigent.policies.types import EvaluationContext, PolicyAction, PolicyResult
from omnigent.runtime import get_conversation_store
from omnigent.runtime.policies import builder
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.server.auth import LEVEL_READ
from omnigent.server.routes import sessions as sessions_routes
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.sessions import routes_hooks
from omnigent.spec.types import (
    FunctionPolicySpec,
    Phase,
    StateUpdate,
    StateUpdateAction,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
from tests.server.helpers import create_test_agent, policy_tool_call_request

pytestmark = pytest.mark.asyncio
_request_context: ContextVar[str | None] = ContextVar("policy_hook_test_context", default=None)


def _allow(event: dict[str, Any]) -> PolicyResult:
    return PolicyResult(action=PolicyAction.ALLOW)


def _write_label(event: dict[str, Any]) -> PolicyResult:
    return PolicyResult(action=PolicyAction.ALLOW, set_labels={"checked": "yes"})


def _deny_with_label(event: dict[str, Any]) -> PolicyResult:
    return PolicyResult(action=PolicyAction.DENY, set_labels={"checked": "yes"})


def _allow_with_state(event: dict[str, Any]) -> PolicyResult:
    return PolicyResult(
        action=PolicyAction.ALLOW,
        set_labels={"checked": "yes"},
        state_updates=[StateUpdate(key="checked", action=StateUpdateAction.SET, value="yes")],
    )


def _deny_with_state(event: dict[str, Any]) -> PolicyResult:
    return replace(_allow_with_state(event), action=PolicyAction.DENY)


def _ask_with_state(event: dict[str, Any]) -> PolicyResult:
    return replace(_allow_with_state(event), action=PolicyAction.ASK)


@pytest.mark.parametrize(
    "stage",
    [
        "agent",
        "presence",
        "initial_tree",
        "ask_tree",
        "allow_label",
        "deny_label",
        "route_label",
        "allow_state",
        "deny_state",
        "ask_label",
        "ask_state",
    ],
)
async def test_policy_hook_storage_waits_leave_health_responsive(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """Pause real store work, including the ASK rebuild, while serving health."""
    handler = "_deny_with_label" if stage == "deny_label" else "_write_label"
    if stage in {"agent", "presence", "initial_tree"}:
        handler = "_allow"
    if stage.endswith("state"):
        handler = "_deny_with_state" if stage == "deny_state" else "_allow_with_state"
    if stage in {"ask_label", "ask_state"}:
        handler = "_ask_with_state"

        async def accept(*args: Any, **kwargs: Any) -> orchestration.ElicitationResult:
            return orchestration.ElicitationResult(action="accept")

        monkeypatch.setattr(orchestration, "_publish_and_wait_for_harness_elicitation", accept)
        monkeypatch.setattr(
            orchestration, "_spawn_native_approval_popup_forward", lambda *args: None
        )
    function: dict[str, Any] = {"path": f"{__name__}.{handler}"}
    if stage == "ask_tree":
        function = {
            "path": "omnigent.policies.builtins.cost.cost_budget",
            "arguments": {"max_cost_usd": 1000.0, "ask_thresholds_usd": [0.10]},
        }
    agent = await create_test_agent(
        client,
        guardrails={"policies": {"storage_probe": {"type": "function", "function": function}}},
    )
    session_id = agent["_session_id"]
    store = get_conversation_store()
    if stage == "ask_tree":
        store.set_session_usage(session_id, {"total_cost_usd": 0.13})

        async def approve(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(sessions_routes, "_hold_native_ask_gate", approve)

    entered = threading.Event()
    release = threading.Event()
    timed_out = threading.Event()
    contexts: list[str | None] = []
    calls = 0
    block_call = 2 if stage in {"ask_tree", "route_label"} else 1
    if stage == "agent":
        owner, method = SqlAlchemyAgentStore, "get"
    elif stage == "presence":
        owner, method = routes_hooks, "any_policies_apply"
    elif stage.endswith("tree"):
        owner, method = SqlAlchemyConversationStore, "list_conversations"
    elif stage.endswith("state"):
        owner, method = SqlAlchemyConversationStore, "set_session_state"
    else:
        owner, method = SqlAlchemyConversationStore, "set_labels"
    original = getattr(owner, method)

    def paused(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == block_call:
            contexts.append(_request_context.get())
            entered.set()
            # The timeout rescues the pre-fix event loop so a negative control
            # fails an assertion instead of hanging the test process.
            if not release.wait(2):
                timed_out.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, method, paused)
    token = _request_context.set("request-scope")
    request = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=policy_tool_call_request("Read"),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5), f"{stage} did not reach its storage gate"
        health = await asyncio.wait_for(client.get("/health"), 1)
        assert health.status_code == 200
        assert not timed_out.is_set(), f"{stage} blocked the event loop until the storage timeout"
        assert not request.done(), f"{stage} finished before the storage gate was released"
        assert contexts == ["request-scope"]
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        _request_context.reset(token)

    response = request.result()
    assert response.status_code == 200, response.text
    expected = "POLICY_ACTION_DENY" if stage.startswith("deny") else "POLICY_ACTION_ALLOW"
    assert response.json()["result"] == expected
    if stage.endswith(("label", "state")):
        conv = await asyncio.to_thread(store.get_conversation, session_id)
        assert conv is not None and conv.labels["checked"] == "yes"
        if stage.endswith("state"):
            assert conv.session_state["checked"] == "yes"


@pytest.mark.parametrize("action", ["allow", "deny", "ask"])
async def test_read_only_hook_withholds_label_and_state_writes(
    auth_client: httpx.AsyncClient, db_uri: str, action: str
) -> None:
    agent = await create_test_agent(
        auth_client,
        user="owner@example.com",
        guardrails={
            "policies": {
                "probe": {
                    "type": "function",
                    "function": {
                        "path": f"{__name__}._{action}_with_state",
                    },
                }
            }
        },
    )
    session_id = agent["_session_id"]
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("reader@example.com")
    permissions.grant("reader@example.com", session_id, LEVEL_READ)
    store = get_conversation_store()
    before = await asyncio.to_thread(store.get_conversation, session_id)
    assert before is not None
    response = await auth_client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=policy_tool_call_request("Read"),
        headers={"X-Forwarded-Email": "reader@example.com"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"] == f"POLICY_ACTION_{action.upper()}"
    after = await asyncio.to_thread(store.get_conversation, session_id)
    assert after is not None
    assert after.labels == before.labels
    assert after.session_state == before.session_state


async def test_allow_cancellation_does_not_drop_state_after_label_write(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = await create_test_agent(
        client,
        guardrails={
            "policies": {
                "probe": {
                    "type": "function",
                    "function": {
                        "path": f"{__name__}._allow_with_state",
                    },
                }
            }
        },
    )
    session_id = agent["_session_id"]
    store = get_conversation_store()
    loop = asyncio.get_running_loop()
    cancelled = threading.Event()
    state_written = threading.Event()
    original_labels = SqlAlchemyConversationStore.set_labels
    original_state = SqlAlchemyConversationStore.set_session_state

    def cancel_after_labels(*args: Any, **kwargs: Any) -> Any:
        result = original_labels(*args, **kwargs)
        loop.call_soon_threadsafe(request.cancel)
        assert cancelled.wait(5), "request did not observe cancellation"
        return result

    def write_state(*args: Any, **kwargs: Any) -> Any:
        result = original_state(*args, **kwargs)
        state_written.set()
        return result

    monkeypatch.setattr(SqlAlchemyConversationStore, "set_labels", cancel_after_labels)
    monkeypatch.setattr(SqlAlchemyConversationStore, "set_session_state", write_state)
    request = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=policy_tool_call_request("Read"),
        )
    )
    request.add_done_callback(lambda _: cancelled.set())
    await asyncio.gather(request, return_exceptions=True)
    assert request.cancelled()
    assert await asyncio.to_thread(state_written.wait, 5), (
        "cancelled hook dropped its state update"
    )
    conv = await asyncio.to_thread(store.get_conversation, session_id)
    assert conv is not None and conv.labels["checked"] == "yes"
    assert conv.session_state["checked"] == "yes"


@pytest.mark.parametrize("scope", ["default", "session"])
async def test_policy_invalidation_during_read_does_not_cache_stale_specs(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    store = SqlAlchemyPolicyStore(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation()

    def create(name: str) -> None:
        kwargs = {
            "policy_id": uuid4().hex,
            "name": name,
            "type": "python",
            "handler": "probe.allow",
        }
        if scope == "default":
            store.create_default(**kwargs)
        else:
            store.create(session_id=conv.id, **kwargs)

    def load() -> list[Any]:
        if scope == "default":
            return builder._load_default_policy_specs(store)
        return builder._load_session_policy_specs(conv.id, store)

    def invalidate() -> None:
        if scope == "default":
            builder.invalidate_default_policy_specs_cache()
        else:
            builder.invalidate_session_policy_specs_cache(conv.id)

    create("before")
    invalidate()
    entered, release = threading.Event(), threading.Event()
    method = "list_defaults" if scope == "default" else "list_for_session"
    original = getattr(store, method)
    reads = 0

    def paused(*args: Any, **kwargs: Any) -> Any:
        nonlocal reads
        rows = original(*args, **kwargs)
        reads += 1
        if reads == 1:
            entered.set()
            assert release.wait(5), "in-flight policy read was not released"
        return rows

    monkeypatch.setattr(store, method, paused)
    pending = asyncio.create_task(asyncio.to_thread(load))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        create("after")
        invalidate()
    finally:
        release.set()
        stale = await pending
    try:
        assert {spec.name for spec in stale} == {"before"}
        fresh = await asyncio.to_thread(load)
        assert {spec.name for spec in fresh} == {"before", "after"}
        assert await asyncio.to_thread(load) is fresh
        assert reads == 2
    finally:
        invalidate()


@pytest.mark.parametrize("read_only, with_writes", [(False, False), (True, False), (True, True)])
async def test_deny_without_persistence_does_not_submit_thread_work(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, read_only: bool, with_writes: bool
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()

    async def deny(event: dict[str, Any]) -> PolicyResult:
        return _deny_with_state(event) if with_writes else PolicyResult(action=PolicyAction.DENY)

    engine = PolicyEngine(
        policies=[FunctionPolicy(FunctionPolicySpec(name="probe", on=None), deny)],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=store,
    )

    async def unexpected_thread(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("DENY without persistence submitted thread work")

    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "to_thread", unexpected_thread)
        result = await engine.evaluate(
            EvaluationContext(phase=Phase.REQUEST, content="probe"), read_only=read_only
        )
    assert result.action == PolicyAction.DENY
    persisted = store.get_conversation(conv.id)
    assert persisted is not None and not persisted.labels and not persisted.session_state
