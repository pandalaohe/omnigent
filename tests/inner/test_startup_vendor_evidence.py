"""Only exact vendor turn observations can qualify first-input startup proof."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.inner.codex_executor import CodexExecutor, _CodexAppServerSession
from omnigent.inner.executor import ExecutorConfig
from omnigent.runner.app import _relay_startup_claim, _relay_startup_proof, _wrap_as_message_event
from omnigent.runtime.harnesses._scaffold import MessageEvent
from omnigent.server.schemas import (
    CreateResponseRequest,
    StartupClaimRequestEvent,
    StartupProofEvent,
    is_known_event,
)
from omnigent.startup_evidence import (
    CodexTurnEvidence,
    claim_startup_boundary,
    startup_evidence_scope,
)
from omnigent.startup_protocol import StartupAttemptRef, StartupObservation
from tests.inner.test_codex_executor import _FakeProcess


@pytest.mark.parametrize("terminal_only", [False, True])
def test_codex_receipts_require_exact_committed_input_and_real_same_turn_work(terminal_only):
    emitted = []
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="context",
        host_generation="generation",
    )
    with startup_evidence_scope(ref, emitted.append):
        evidence = CodexTurnEvidence("thread", "turn", source="codex")
        base = {"threadId": "thread", "turnId": "turn"}
        evidence.notification("turn/started", {**base, "turn": {"id": "turn"}})
        evidence.notification("item/agentMessage/delta", {**base, "turnId": "old", "delta": "old"})
        evidence.notification(
            "item/agentMessage/delta", {**base, "threadId": "child", "delta": "child"}
        )
        evidence.notification("item/agentMessage/delta", {"delta": "unbound"})
        evidence.notification("item/agentMessage/delta", {**base, "delta": ""})
        assert emitted == []
        if terminal_only:
            evidence.notification(
                "item/completed",
                {**base, "item": {"type": "agentMessage", "id": "assistant", "text": "work"}},
            )
        else:
            evidence.notification("item/reasoning/textDelta", {**base, "delta": "work"})
        evidence.notification(
            "item/completed", {**base, "item": {"type": "userMessage", "id": "user"}}
        )
        evidence.notification("item/agentMessage/delta", {**base, "delta": "more"})
        evidence.notification(
            "item/completed", {**base, "item": {"type": "userMessage", "id": "user"}}
        )
    assert [item.phase for item in emitted] == ["work_started", "input_accepted"]
    assert all(item.attempt == ref and item.vendor_turn_id == "turn" for item in emitted)
    evidence.notification("item/agentMessage/delta", {**base, "delta": "outside"})
    assert len(emitted) == 2


def test_codex_hook_or_completion_without_vendor_execution_is_not_work():
    emitted = []
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="context",
        host_generation="generation",
    )
    with startup_evidence_scope(ref, emitted.append):
        evidence = CodexTurnEvidence("thread", "turn", source="codex")
        for method in (
            "hook/started",
            "hook/completed",
            "turn/completed",
            "thread/status/changed",
        ):
            evidence.notification(method, {"threadId": "thread", "turnId": "turn"})
    assert emitted == []


def test_codex_dispatch_receipt_requires_owned_turn_and_input():
    emitted = []
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="context",
        host_generation="generation",
    )
    with startup_evidence_scope(ref, emitted.append):
        CodexTurnEvidence("thread", "turn", source="codex").ready(tool_bridge_installed=True)
    assert [item.phase for item in emitted] == [
        "input_dispatched",
        "runtime_ready",
        "tools_ready",
    ]
    assert all(
        item.vendor_session_id == "thread" and item.vendor_turn_id == "turn" for item in emitted
    )

    without_input = ref.model_copy(update={"input_id": None})
    emitted.clear()
    with startup_evidence_scope(without_input, emitted.append):
        CodexTurnEvidence("thread", "turn", source="codex").ready(tool_bridge_installed=False)
    assert [item.phase for item in emitted] == ["runtime_ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work",
    [
        "reasoning",
        "command",
        "file_change",
        "approval",
        "tail",
        "imageView",
        "imageGeneration",
        "webSearch",
        "dynamicToolCall",
    ],
)
@pytest.mark.parametrize("queue_mode", [False, True])
async def test_actual_codex_executor_reports_only_its_own_vendor_turn(work, queue_mode):
    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd="/opt/work/project",
        env={},
        tool_executor=None,
    )
    session.start = AsyncMock()
    session._proc = _FakeProcess()
    session.thread_id = "thread"
    session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn"}}})
    emitted = []
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="context",
        host_generation="generation",
    )
    if queue_mode:
        ref = ref.model_copy(update={"mode": "startup_only", "policy_revision": 1})
    base = {"threadId": "thread", "turnId": "turn"}
    activities = {
        "reasoning": [("item/reasoning/textDelta", {**base, "delta": "work"})],
        "command": [
            (
                "item/started",
                {**base, "item": {"type": "commandExecution", "id": "command", "command": "true"}},
            )
        ],
        "file_change": [
            (
                "item/completed",
                {
                    **base,
                    "item": {
                        "type": "fileChange",
                        "id": "change",
                        "changes": [{"path": "sample.py"}],
                    },
                },
            )
        ],
        "approval": [
            (
                "item/commandExecution/requestApproval",
                {**base, "itemId": "command", "command": "true"},
            )
        ],
        "tail": [
            ("turn/completed", {**base, "turn": {"id": "turn", "status": "completed"}}),
            (
                "item/completed",
                {
                    **base,
                    "item": {
                        "type": "agentMessage",
                        "id": "answer",
                        "phase": "final_answer",
                        "text": "done",
                    },
                },
            ),
        ],
    }
    if work in ("imageView", "imageGeneration", "webSearch", "dynamicToolCall"):
        activities[work] = [("item/started", {**base, "item": {"type": work, "id": "tool"}})]
    events = [
        ("turn/started", {**base, "turn": {"id": "turn"}}),
        ("item/agentMessage/delta", {**base, "threadId": "child", "delta": "child"}),
        ("item/completed", {**base, "item": {"type": "userMessage", "id": "user"}}),
        *activities[work],
        ("turn/failed", {**base, "turn": {"id": "turn", "error": "ended"}}),
    ]
    for method, params in events:
        await session._events.put({"method": method, "params": params})
    with startup_evidence_scope(ref, emitted.append):
        async for _event in session.run_turn(
            messages=[{"role": "user", "content": "work"}],
            tools=[],
            system_prompt="",
            model=None,
            cwd="/opt/work/project",
            sandbox="workspace-write",
        ):
            pass
    assert [item.phase for item in emitted] == [
        "input_dispatched",
        "runtime_ready",
        "tools_ready",
        *(["opening_settled"] if queue_mode else []),
        "input_accepted",
        "work_started",
    ]


@pytest.mark.asyncio
async def test_startup_claim_scope_requires_trusted_callback():
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="context",
        host_generation="generation",
    )
    called = []

    async def claim(attempt, boundary):
        called.append((attempt, boundary))
        return True

    await claim_startup_boundary("executor_prepare")
    with startup_evidence_scope(ref, lambda _: None, claim):
        await claim_startup_boundary("executor_prepare")
    assert called == [(ref, "executor_prepare")]
    with startup_evidence_scope(ref, lambda _: None):
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            await claim_startup_boundary("executor_prepare")

    async def denied(attempt, boundary):
        return False

    with startup_evidence_scope(ref, lambda _: None, denied):
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            await claim_startup_boundary("executor_prepare")


@pytest.mark.asyncio
async def test_startup_control_frames_roundtrip_only_over_internal_relay():
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="context",
        host_generation="generation",
    )
    body = {
        "model": "agent",
        "input": "work",
        "startup_attempt": ref.model_dump(),
        "startup_required": True,
    }
    request = MessageEvent.model_validate(_wrap_as_message_event(body)).to_create_request()
    assert request.startup_attempt == ref
    assert request.startup_required is True
    assert "startup_attempt" not in CreateResponseRequest.model_json_schema()["properties"]
    assert "startup_required" not in CreateResponseRequest.model_json_schema()["properties"]
    assert not is_known_event("startup.proof")
    assert not is_known_event("startup.claim.requested")

    posted = []

    def server_handler(request: httpx.Request) -> httpx.Response:
        posted.append((request.url.path, request.content))
        return httpx.Response(200, json={"accepted": True, "granted": True})

    replies = []

    def harness_handler(request: httpx.Request) -> httpx.Response:
        replies.append((request.url.path, request.content))
        return httpx.Response(200, json={})

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(server_handler), base_url="http://server.invalid"
        ) as server,
        httpx.AsyncClient(
            transport=httpx.MockTransport(harness_handler), base_url="http://harness.invalid"
        ) as harness,
    ):
        claim = StartupClaimRequestEvent(
            claim_id="claim", attempt=ref, boundary="executor_prepare"
        )
        await _relay_startup_claim(server, harness, "session", claim.model_dump(), ref)
        proof = StartupProofEvent(
            observation=StartupObservation(
                attempt=ref,
                phase="input_accepted",
                source="codex",
                vendor_session_id="thread",
                vendor_turn_id="turn",
            )
        )
        assert await _relay_startup_proof(server, "session", proof.model_dump(), ref)
        ready = StartupProofEvent(
            observation=StartupObservation(
                attempt=ref,
                phase="runtime_ready",
                source="codex",
                vendor_session_id="thread",
                vendor_turn_id="turn",
            )
        )
        assert await _relay_startup_proof(server, "session", ready.model_dump(), ref)
        wrong = ref.model_copy(update={"attempt_id": "d" * 32})
        assert not await _relay_startup_proof(server, "session", proof.model_dump(), wrong)
        await _relay_startup_claim(server, harness, "session", claim.model_dump(), wrong)
    assert len(posted) == 3
    assert posted[0][0] == "/v1/sessions/session/startup/claims"
    assert posted[1][0] == "/v1/sessions/session/startup/receipts"
    assert json.loads(posted[1][1])["source"] == "codex"
    assert json.loads(posted[2][1])["source"] == "runner"
    assert len(replies) == 2
    assert b'"granted":true' in replies[0][1]
    assert b'"granted":false' in replies[1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statuses", "accepted", "expected_calls"),
    [
        ([503, 200], True, 2),
        ([400, 200], False, 1),
        ([200, 200], False, 1),
    ],
)
async def test_startup_receipt_retries_only_transient_responses(
    statuses, accepted, expected_calls
):
    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="context",
        host_generation="generation",
    )
    event = StartupProofEvent(
        observation=StartupObservation(
            attempt=ref,
            phase="input_dispatched",
            source="codex",
            vendor_session_id="thread",
            vendor_turn_id="turn",
        )
    )
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        status = statuses[calls]
        calls += 1
        return httpx.Response(status, json={"accepted": status == 200 and accepted})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server.invalid"
    ) as server:
        result = await _relay_startup_proof(server, "session", event.model_dump(), ref)
    assert result is accepted
    assert calls == expected_calls


@pytest.mark.asyncio
async def test_codex_cold_executor_claim_precedes_creation_and_warm_turn_skips_it(db_uri):
    class FakeAppSession:
        _started = False
        _closing = False
        _transport_error = None
        cleaned = True

        async def run_turn(self, **kwargs):
            self._started = True
            if False:
                yield None

        async def close(self):
            self._started = False

    created = []

    def factory(**kwargs):
        session = FakeAppSession()
        created.append(session)
        return session

    executor = CodexExecutor(
        codex_path="/bin/echo",
        cwd="/opt/work/project",
        model="gpt-5.4",
        app_session_factory=factory,
    )
    from uuid import uuid4

    from omnigent.stores.session_startup_store import SessionStartupStore

    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, host_resident_limit=4, deployment_resident_limit=8)
    queued = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="codex-cold",
        input_id=uuid4().hex,
    )
    reserved = store.reserve(host)
    assert store.claim(queued.id, reserved.attempt_id, runtime_id="runtime")
    ref = StartupAttemptRef(
        startup_id=queued.id,
        attempt_id=reserved.attempt_id,
        input_id=queued.input_id,
        runtime_id="runtime",
        host_generation="generation",
    )
    messages = [{"role": "user", "content": "work", "session_id": "session"}]
    claims = []

    async def deny(attempt, boundary):
        claims.append(boundary)
        return False

    with startup_evidence_scope(ref, lambda _: None, deny):
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            async for _ in executor.run_turn(messages, [], "", ExecutorConfig()):
                pass
    assert created == []

    async def grant(attempt, boundary):
        claims.append(boundary)
        return store.claim_boundary(
            attempt.startup_id,
            attempt.attempt_id,
            runtime_id=attempt.runtime_id,
            host_generation=attempt.host_generation,
            boundary=boundary,
            claim_id=uuid4().hex,
        )

    with startup_evidence_scope(ref, lambda _: None, grant):
        async for _ in executor.run_turn(messages, [], "", ExecutorConfig()):
            pass
        # One owned cold operation covers package preparation and session construction.
        assert claims == ["executor_prepare"] * 2
        assert len(created) == 1
        async for _ in executor.run_turn(messages, [], "", ExecutorConfig()):
            pass
        assert claims == ["executor_prepare"] * 2
        assert len(created) == 1
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            async for _ in executor.run_turn(messages, [], "", ExecutorConfig(model="gpt-5.5")):
                pass
    assert claims == ["executor_prepare"] * 3
    assert len(created) == 1

    # A warm follow-up has no new attempt; a later signature change or crash
    # must discover cold work and refuse it before making a replacement client.
    with startup_evidence_scope(None, lambda _: None, required=True):
        async for _ in executor.run_turn(messages, [], "", ExecutorConfig(model="gpt-5.4")):
            pass
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            async for _ in executor.run_turn(messages, [], "", ExecutorConfig(model="gpt-5.6")):
                pass
    assert len(created) == 1


@pytest.mark.asyncio
async def test_required_executor_scope_without_attempt_refuses_cold_claim():
    with startup_evidence_scope(None, lambda _: None, required=True):
        with pytest.raises(RuntimeError, match="Startup permission unavailable"):
            await claim_startup_boundary("executor_prepare")
