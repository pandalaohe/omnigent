"""
Integration tests for ASK policies declared in agent YAML.

Covers both INPUT (``request``) and TOOL_CALL phases with approve/refuse
outcomes, exercising the full path from agent bundle upload through
policy engine construction, elicitation parking, and resolution.

Uses ``create_test_agent`` with inline ``guardrails`` dicts — the same
spec shape the YAML ``guardrails:`` block produces — so no fixture
agent directories are needed.

The ``make_fixed_action_callable`` builtin generates policy callables
that unconditionally return the configured action (here ``ask``),
which is the standard pattern for always-ASK gates in agent YAML.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from omnigent.runtime import pending_elicitations, session_stream
from tests._helpers.polling import poll_until_async
from tests.server.helpers import create_test_agent
from tests.server.helpers import policy_tool_call_request as _tool_call_request

pytestmark = pytest.mark.asyncio


POLICY_REGISTRY = [
    {
        "handler": "omnigent.policies.function.make_fixed_action_callable",
        "kind": "factory",
    }
]


@pytest.fixture(autouse=True)
def _register_test_policy_handlers() -> Iterator[None]:
    from omnigent.policies.registry import load_registry

    # HTTP uploads accept custom policies only when the server registers them.
    load_registry([__name__])
    yield
    load_registry()


# ── Guardrails specs ─────────────────────────────────────

_INPUT_ASK_GUARDRAILS: dict[str, Any] = {
    "policies": {
        "always_ask_on_input": {
            "type": "function",
            "on": ["request"],
            "function": {
                "path": "omnigent.policies.function.make_fixed_action_callable",
                "arguments": {
                    "action": "ask",
                    "reason": "Confirm this message before processing.",
                    "on_phases": ["request"],
                },
            },
        },
    },
}

_TOOL_CALL_ASK_GUARDRAILS: dict[str, Any] = {
    "policies": {
        "ask_before_any_tool": {
            "type": "function",
            "on": ["tool_call"],
            "function": {
                "path": "omnigent.policies.function.make_fixed_action_callable",
                "arguments": {
                    "action": "ask",
                    "reason": "Approve this tool call.",
                    "on_phases": ["tool_call"],
                },
            },
        },
    },
}


# ── Helpers ──────────────────────────────────────────────


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
) -> str:
    """
    Create a session bound to the given agent and return its id.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :returns: New session id.
    """
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent_id},
    )
    assert resp.status_code == 201, f"create session failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


async def _drain_elicitation_id(
    session_id: str,
    *,
    subscribed: asyncio.Event | None = None,
    timeout_s: float = 5.0,
) -> str:
    """
    Subscribe to the session stream and return the first elicitation id.

    :param session_id: Session to subscribe to.
    :param subscribed: Optional event set once the subscriber is
        registered, so the caller can safely publish events after
        this point without racing.
    :param timeout_s: Max seconds to wait.
    :returns: The ``elicitation_id`` from the event.
    """

    async def _on_subscribed() -> tuple[dict[str, Any], ...]:
        if subscribed is not None:
            subscribed.set()
        return ()

    async with asyncio.timeout(timeout_s):
        async for event in session_stream.subscribe(
            session_id,
            on_subscribed=_on_subscribed,
        ):
            if event.get("type") == "response.elicitation_request":
                eid = event.get("elicitation_id")
                assert isinstance(eid, str) and eid, f"missing id: {event!r}"
                return eid
    raise AssertionError("subscribe loop ended without an elicitation event")


# ── Test 1: ASK on INPUT from YAML -> approve ───────────


async def test_input_ask_yaml_approve(
    client: httpx.AsyncClient,
) -> None:
    """
    An agent YAML declaring an always-ASK policy on the ``request``
    phase parks the user message until approved. Accepting the
    elicitation allows the message through (the events endpoint
    returns without a deny verdict).

    Proves the full bundle-upload -> spec parse -> engine build ->
    INPUT ASK gate -> elicitation publish -> resolve -> ALLOW path.
    """
    agent = await create_test_agent(
        client,
        name="test-input-ask-approve",
        guardrails=_INPUT_ASK_GUARDRAILS,
    )
    session_id = await _create_session(client, agent["id"])

    # Subscribe to the stream BEFORE posting the message so we
    # don't miss the elicitation event.
    subscribed = asyncio.Event()
    drain_task = asyncio.create_task(_drain_elicitation_id(session_id, subscribed=subscribed))
    try:
        await subscribed.wait()

        # POST the user message — this parks inside _evaluate_input_policy
        # until the ASK is resolved.
        message_task = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "_input_request_id": "request-approval-test",
                        "content": [{"type": "input_text", "text": "hello approve test"}],
                    },
                },
            )
        )

        # Wait for the elicitation to appear on the stream.
        elicitation_id = await drain_task

        # Also verify the elicitation appears in the session snapshot.
        snap = await client.get(f"/v1/sessions/{session_id}")
        assert snap.status_code == 200
        pending = snap.json().get("pending_elicitations", [])
        assert any(p["elicitation_id"] == elicitation_id for p in pending), (
            f"elicitation {elicitation_id} not in session snapshot: {pending}"
        )

        own = next(p for p in pending if p["elicitation_id"] == elicitation_id)
        assert own["params"]["input_request_id"] == "request-approval-test"
        assert own["params"]["input_approval_deadline"] > 0

        # Approve the elicitation.
        verdict = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
        )
        assert verdict.status_code == 202, verdict.text

        # The message POST should now return — ALLOW means the message
        # was forwarded past the policy layer (not denied synchronously).
        # It may then fail with 503 (no runner bound) — that still proves
        # the ASK gate approved the message through.
        resp = await asyncio.wait_for(message_task, timeout=5.0)
        assert float(resp.headers["X-Omnigent-Approval-Wait-Seconds"]) > 0
        body = resp.json()
        assert body.get("denied") is not True, f"approved INPUT ASK should not deny; got {body}"
    finally:
        for task in [drain_task, message_task if "message_task" in dir() else None]:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        pending_elicitations.reset_for_tests()


# ── Test 2: ASK on INPUT from YAML -> refuse ────────────


async def test_input_ask_yaml_refuse(
    client: httpx.AsyncClient,
) -> None:
    """
    Declining an INPUT ASK elicitation produces the DENY sentinel.

    The events endpoint returns a synchronous deny verdict (``denied:
    true``) when the human refuses the ASK gate — fail-closed. The
    deny reason carries the policy's configured reason text.
    """
    agent = await create_test_agent(
        client,
        name="test-input-ask-refuse",
        guardrails=_INPUT_ASK_GUARDRAILS,
    )
    session_id = await _create_session(client, agent["id"])

    subscribed = asyncio.Event()
    drain_task = asyncio.create_task(_drain_elicitation_id(session_id, subscribed=subscribed))
    try:
        await subscribed.wait()

        message_task = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello refuse test"}],
                    },
                },
            )
        )

        elicitation_id = await drain_task

        # Decline the elicitation.
        verdict = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "decline"},
        )
        assert verdict.status_code == 202, verdict.text

        # The message POST returns a deny verdict.
        resp = await asyncio.wait_for(message_task, timeout=5.0)
        body = resp.json()
        assert body.get("denied") is True, f"declined INPUT ASK should deny; got {body}"
        assert "reason" in body, f"deny verdict missing reason: {body}"
        assert "Confirm this message before processing" in body["reason"], (
            f"expected exact ASK reason in deny verdict; got {body['reason']!r}"
        )
    finally:
        for task in [drain_task, message_task if "message_task" in dir() else None]:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        pending_elicitations.reset_for_tests()


# ── Test 3: ASK on TOOL_CALL from YAML -> approve ───────


async def test_tool_call_ask_yaml_approve(
    client: httpx.AsyncClient,
) -> None:
    """
    An agent YAML declaring an always-ASK policy on the ``tool_call``
    phase parks the evaluate endpoint until approved. Accepting the
    elicitation collapses the ASK to ``POLICY_ACTION_ALLOW``.

    Proves the agent-level guardrails (not ``default_policies``) flow
    through the ``/policies/evaluate`` endpoint: bundle upload -> spec
    parse -> engine build with agent-declared policies -> TOOL_CALL
    ASK gate -> elicitation -> resolve -> ALLOW.
    """
    agent = await create_test_agent(
        client,
        name="test-tool-ask-approve",
        guardrails=_TOOL_CALL_ASK_GUARDRAILS,
    )
    session_id = await _create_session(client, agent["id"])

    subscribed = asyncio.Event()
    drain_task = asyncio.create_task(_drain_elicitation_id(session_id, subscribed=subscribed))
    try:
        await subscribed.wait()

        evaluate_task = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/policies/evaluate",
                json=_tool_call_request("Bash", {"command": "echo hello"}),
            )
        )

        elicitation_id = await drain_task

        # Accept the tool call.
        verdict = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
        )
        assert verdict.status_code == 202, verdict.text

        resp = await asyncio.wait_for(evaluate_task, timeout=5.0)
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == "POLICY_ACTION_ALLOW"
    finally:
        for task in [drain_task, evaluate_task if "evaluate_task" in dir() else None]:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        pending_elicitations.reset_for_tests()


@pytest.mark.parametrize("stop_case", ["live_runner", "no_runner", "failed_stop"])
async def test_receiver_stop_settles_pending_request_and_resumes_sender(
    client, monkeypatch, stop_case
):
    import json

    from omnigent.harnesses.claude_native import bridge
    from omnigent.runner import tool_dispatch
    from omnigent.runtime import set_runner_client
    from omnigent.runtime.tool_call_deadline import ToolCallDeadline
    from tests.server.integration.test_sessions_endpoints import _runtime_runner

    monkeypatch.setattr(tool_dispatch, "_REMOTE_CHILD_READY_TIMEOUT_S", 1.5)
    monkeypatch.setattr(tool_dispatch, "_CHILD_APPROVAL_POLL_S", 0.01)
    monkeypatch.setattr(bridge, "_TOOL_CALL_TIMEOUT_S", 2.0)
    budgets, forwarded, messages = [], [], []

    def budget(seconds):
        value = ToolCallDeadline(seconds)
        budgets.append(value)
        return value

    monkeypatch.setattr(bridge, "ToolCallDeadline", budget)

    def runner(request):
        if request.method == "POST" and request.url.path.endswith("/events"):
            body = json.loads(request.content)
            forwarded.append(body)
            if body.get("type") == "stop_session" and stop_case == "failed_stop":
                return httpx.Response(503)
        return httpx.Response(204)

    async def record(request):
        if request.method == "POST" and request.url.path.endswith("/events"):
            body = json.loads(request.content)
            if body.get("type") == "message":
                messages.append(body)

    async with _runtime_runner(runner):
        agent = await create_test_agent(
            client, name="receiver-stop-policy", guardrails=_INPUT_ASK_GUARDRAILS
        )
        session_id = await _create_session(client, agent["id"])
        if stop_case == "no_runner":
            set_runner_client(None)
        subscribed = asyncio.Event()
        drain = asyncio.create_task(_drain_elicitation_id(session_id, subscribed=subscribed))
        await subscribed.wait()
        client.event_hooks["request"].append(record)

        async def executor(_name, _arguments):
            response = await tool_dispatch._post_child_message_recovering(
                client,
                session_id,
                content=[{"type": "input_text", "text": "wait for approval"}],
                created_by=None,
            )
            return response.json()

        call = asyncio.create_task(
            asyncio.to_thread(
                bridge._run_relay_tool,
                executor,
                asyncio.get_running_loop(),
                "sys_session_send",
                {},
            )
        )
        elicitation_id = None
        try:
            elicitation_id = await drain
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(call), 2.1)
            stopped = await client.post(
                f"/v1/sessions/{session_id}/events", json={"type": "stop_session", "data": {}}
            )
            if stop_case == "failed_stop":
                assert stopped.status_code == 503
                assert not call.done()
                assert any(
                    p["elicitation_id"] == elicitation_id
                    for p in pending_elicitations.snapshot_for(session_id)
                )
            else:
                assert stopped.status_code == 202, stopped.text
                result = await asyncio.wait_for(call, 3)
                assert not result.get("isError"), result
                outcome = json.loads(result["content"][0]["text"])
                assert outcome["denied"] is True and outcome["queued"] is False
                assert not pending_elicitations.snapshot_for(session_id)
                before = budgets[0].remaining()
                assert before > 0
                await poll_until_async(lambda: budgets[0].remaining() < before, timeout_s=3)
                assert len(messages) == 1
            assert not any(body.get("type") == "message" for body in forwarded)
            assert sum(body.get("type") == "stop_session" for body in forwarded) == (
                0 if stop_case == "no_runner" else 1
            )
        finally:
            client.event_hooks["request"].remove(record)
            if elicitation_id is not None and not call.done():
                await client.post(
                    f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
                    json={"action": "cancel"},
                )
            await asyncio.wait_for(call, 3)
            if not drain.done():
                drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)


async def test_receiver_stop_preserves_request_approval_opened_after_stop_fence(client):
    import json

    from tests.server.integration.test_sessions_endpoints import _runtime_runner

    stop_entered, finish_stop = asyncio.Event(), asyncio.Event()

    async def runner(request):
        if request.method == "POST" and request.url.path.endswith("/events"):
            if json.loads(request.content).get("type") == "stop_session":
                stop_entered.set()
                await finish_stop.wait()
        return httpx.Response(204)

    async with _runtime_runner(runner):
        agent = await create_test_agent(
            client, name="receiver-stop-fence", guardrails=_INPUT_ASK_GUARDRAILS
        )
        session_id = await _create_session(client, agent["id"])
        tasks, ids = [], []

        async def parked_message(text):
            subscribed = asyncio.Event()
            drain = asyncio.create_task(_drain_elicitation_id(session_id, subscribed=subscribed))
            await subscribed.wait()
            message = asyncio.create_task(
                client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                        },
                    },
                )
            )
            tasks.append(message)
            ids.append(await drain)
            return message

        stop = None
        try:
            original = await parked_message("before stop")
            stop = asyncio.create_task(
                client.post(
                    f"/v1/sessions/{session_id}/events", json={"type": "stop_session", "data": {}}
                )
            )
            await asyncio.wait_for(stop_entered.wait(), 3)
            newer = await parked_message("after stop began")
            finish_stop.set()
            assert (await asyncio.wait_for(stop, 3)).status_code == 202
            assert (await asyncio.wait_for(original, 3)).json()["denied"] is True
            assert not newer.done()
            assert [
                p["elicitation_id"] for p in pending_elicitations.snapshot_for(session_id)
            ] == [ids[1]]
        finally:
            finish_stop.set()
            for elicitation_id in ids:
                if any(
                    p["elicitation_id"] == elicitation_id
                    for p in pending_elicitations.snapshot_for(session_id)
                ):
                    await client.post(
                        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
                        json={"action": "cancel"},
                    )
            if stop is not None:
                await asyncio.wait_for(stop, 3)
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
