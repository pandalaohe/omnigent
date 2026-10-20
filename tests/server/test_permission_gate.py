"""Mandatory execution-host confirmation and one-shot approval bindings."""

import json
from dataclasses import dataclass

import httpx
import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.entities import Conversation
from omnigent.permission_tiers import Verdict
from omnigent.server import permission_gate as gate
from omnigent.server.permission_tiers import (
    PERMISSION_CAPABILITY_LABEL_KEY,
    PERMISSION_CAPABILITY_VALUE,
    BoundToolCall,
    ConfirmedPermissionState,
)
from omnigent.server.routes import sessions
from omnigent.server.schemas import ElicitationResult


@dataclass
class Store:
    conversation: Conversation

    def get_conversation(self, session_id):
        assert session_id == self.conversation.id
        return self.conversation


@pytest.fixture
def store():
    state = ConfirmedPermissionState("ask", 2, "ready")
    return Store(
        Conversation(
            id="session",
            created_at=1,
            updated_at=1,
            root_conversation_id="session",
            host_id="host",
            workspace="/opt/work/project",
            labels={
                **state.labels("pi"),
                PERMISSION_CAPABILITY_LABEL_KEY: PERMISSION_CAPABILITY_VALUE,
            },
        )
    )


@pytest.fixture
def arguments():
    return {"path": "/opt/work/project/a.py", "content": "value"}


def response_body(arguments, **changes):
    binding = BoundToolCall.bind(
        session_id="session",
        host_id="host",
        call_id="call",
        name="write",
        arguments=arguments,
        tier_revision=2,
    )
    return {
        "session_id": "session",
        "host_id": "host",
        "call_id": "call",
        "name": "write",
        "arguments_digest": binding.arguments_digest,
        "tier_revision": 2,
        "permission_tiers_capability": PERMISSION_CAPABILITY_VALUE,
        "verdict": "ASK",
        "reason": "Approve this write once",
        "authorized": True,
        **changes,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"host_id": "other"},
        {"session_id": "other"},
        {"call_id": "other"},
        {"arguments_digest": "wrong"},
        {"tier_revision": 3},
        {"tier_revision": True},
        {"permission_tiers_capability": None},
        {"verdict": "unknown"},
    ],
)
async def test_mismatched_execution_host_verdict_denies(store, arguments, monkeypatch, changed):
    async with httpx.AsyncClient(
        base_url="http://runner",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response_body(arguments, **changed))
        ),
    ) as client:

        async def get_client(*_, **__):
            return client

        monkeypatch.setattr(sessions, "_get_runner_client", get_client)
        result = await gate.inspect_permission_call(
            store.conversation,
            harness="pi",
            name="write",
            arguments=arguments,
            call_id="call",
            runner_router=None,
        )
        assert result.decision.action == Verdict.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["arguments", "revision", "read-only", "decline", "accept"])
async def test_approval_is_bound_and_consumed_once(store, arguments, monkeypatch, change):
    from omnigent.server.routes._sessions import orchestration

    requests, cards = [], []
    async with httpx.AsyncClient(
        base_url="http://runner",
        transport=httpx.MockTransport(
            lambda req: (
                requests.append(json.loads(req.content))
                or httpx.Response(200, json=response_body(arguments))
            )
        ),
    ) as client:

        async def get_client(*_, **__):
            return client

        monkeypatch.setattr(sessions, "_get_runner_client", get_client)
        inspection = await gate.inspect_permission_call(
            store.conversation,
            harness="pi",
            name="write",
            arguments=arguments,
            call_id="call",
            runner_router=None,
        )
        gate._active_approvals.clear()
        gate._consumed_approvals.clear()

        async def approve(*_, **kwargs):
            cards.append(kwargs)
            if change == "revision":
                store.conversation.labels.update(
                    ConfirmedPermissionState("ask", 3, "ready").labels("pi")
                )
            return ElicitationResult(action="decline" if change == "decline" else "accept")

        monkeypatch.setattr(orchestration, "_publish_and_wait_for_harness_elicitation", approve)
        actual = {**arguments, "content": "different"} if change == "arguments" else arguments
        decision = await gate.authorize_permission_call(
            inspection,
            request=None,
            harness="pi",
            arguments=actual,
            conversation_store=store,
            runner_router=None,
            can_approve=change != "read-only",
        )
        assert decision.action == (Verdict.ALLOW if change == "accept" else Verdict.DENY)
        if change == "accept":
            replay = await gate.authorize_permission_call(
                inspection,
                request=None,
                harness="pi",
                arguments=actual,
                conversation_store=store,
                runner_router=None,
            )
            assert replay.action == Verdict.DENY
            assert len(cards) == 1
            assert cards[0]["elicitation_id"] == inspection.binding.approval_id()
            assert cards[0]["params"].phase == "permission_tier"
            assert requests[-1]["type"] == "permission_tier_authorize"
            with workspace_scope(123):
                other_workspace = await gate.authorize_permission_call(
                    inspection,
                    request=None,
                    harness="pi",
                    arguments=actual,
                    conversation_store=store,
                    runner_router=None,
                )
            assert other_workspace.action == Verdict.ALLOW
            assert len(cards) == 2
        else:
            assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "switching"])
async def test_full_does_not_override_unconfirmed_state(store, arguments, status):
    store.conversation.labels.update(
        ConfirmedPermissionState("full", 2, status, "tx").labels("pi")
    )
    result = await gate.inspect_permission_call(
        store.conversation,
        harness="pi",
        name="write",
        arguments=arguments,
        call_id="call",
        runner_router=None,
    )
    assert result.decision.action == Verdict.DENY


@pytest.mark.asyncio
async def test_unresolved_managed_harness_is_denied(store, arguments):
    result = await gate.inspect_permission_call(
        store.conversation,
        harness=None,
        name="write",
        arguments=arguments,
        call_id="call",
        runner_router=None,
    )
    assert result.decision.action == Verdict.DENY
    assert result.decision.reason == "Permission harness unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("proof,malformed", [(False, False), (True, False), (True, True)])
async def test_result_endpoint_releases_only_verified_valid_completion(
    store, monkeypatch, proof, malformed
):
    from fastapi import APIRouter, FastAPI

    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.server.auth import LEVEL_READ
    from omnigent.server.routes._auth_helpers import SessionAccess
    from omnigent.server.routes.sessions import routes_hooks

    conv = store.conversation
    conv.harness_override = "pi"
    conv.runner_id = token_bound_runner_id("test-binding")
    calls = []

    async def access(*_, **__):
        return SessionAccess(level=LEVEL_READ, conversation=conv)

    async def finish(conversation, **kwargs):
        calls.append((conversation.id, kwargs))
        return True

    monkeypatch.setattr(routes_hooks, "_require_access_and_level", access)
    monkeypatch.setattr(gate, "finish_permission_call", finish)
    router = APIRouter()
    routes_hooks.register_hooks_routes(router, conversation_store=store, agent_store=None)
    app = FastAPI()
    app.include_router(router)
    headers = {RUNNER_TUNNEL_TOKEN_HEADER: "test-binding"} if proof else {}
    data = {
        "name": "write",
        "arguments": {},
        "result": "done",
        "call_id": "call",
        "permission_tier_revision": 2,
    }
    if malformed:
        data["name"] = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=not malformed),
        base_url="http://server",
    ) as client:
        response = await client.post(
            "/sessions/session/policies/evaluate",
            headers=headers,
            json={"event": {"type": "PHASE_TOOL_RESULT", "target": data["name"], "data": data}},
        )
    assert len(calls) == (1 if proof and not malformed else 0)
    if not malformed:
        assert response.status_code == 200, response.text
        assert response.json()["permission_tier_finish"] == {
            "call_id": "call",
            "revision": 2,
            "finished": proof,
        }
