"""Storage work in native policy hooks must leave other requests responsive."""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar
from typing import Any

import httpx
import pytest

from omnigent.policies.types import PolicyAction, PolicyResult
from omnigent.runtime import get_conversation_store
from omnigent.server.routes import sessions as sessions_routes
from omnigent.server.routes.sessions import routes_hooks
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent, policy_tool_call_request

pytestmark = pytest.mark.asyncio
_request_context: ContextVar[str | None] = ContextVar("policy_hook_test_context", default=None)


def _allow(event: dict[str, Any]) -> PolicyResult:
    return PolicyResult(action=PolicyAction.ALLOW)


def _write_label(event: dict[str, Any]) -> PolicyResult:
    return PolicyResult(action=PolicyAction.ALLOW, set_labels={"checked": "yes"})


def _deny_with_label(event: dict[str, Any]) -> PolicyResult:
    return PolicyResult(action=PolicyAction.DENY, set_labels={"checked": "yes"})


@pytest.mark.parametrize(
    "stage",
    ["agent", "presence", "initial_tree", "ask_tree", "allow_label", "deny_label", "route_label"],
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
    expected = "POLICY_ACTION_DENY" if stage == "deny_label" else "POLICY_ACTION_ALLOW"
    assert response.json()["result"] == expected
    if stage.endswith("label"):
        conv = await asyncio.to_thread(store.get_conversation, session_id)
        assert conv is not None and conv.labels["checked"] == "yes"
