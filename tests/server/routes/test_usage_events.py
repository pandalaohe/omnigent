"""Exercise producer admission, durable ACKs and the owner feed over ASGI."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from omnigent.entities import Conversation
from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.routes.usage_cutover import create_usage_cutover_router
from omnigent.server.routes.usage_events import create_usage_events_router
from omnigent.server.usage_cutover import admit_turn, refuse_if_held
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.usage_export.outbox import UsageOutbox


def _app(conversations: SqlAlchemyConversationStore, harness: str) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_usage_events_router(
            conversations,
            lambda _: harness,
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def application_error(_request: object, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.http_status)

    return app


@pytest.mark.asyncio
async def test_spool_survives_home_and_session_delete_and_lost_ack(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'server.db'}"
    conversations = SqlAlchemyConversationStore(uri)
    conv = conversations.create_conversation(
        agent_id="a" * 32, host_id="b" * 32, workspace="/opt/work"
    )
    SqlAlchemyPermissionStore(uri).grant("alice", conv.id, LEVEL_OWNER)
    app = _app(conversations, "codex-native")
    spool = UsageOutbox(tmp_path / "usage")
    calls = 0

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://omni.example.test",
        headers={"X-Forwarded-Email": "alice"},
    ) as server:
        await spool.prepare(server, conv.id, "codex-native")
        record = {
            "type": "token_usage_record",
            "timestamp": "2026-10-20T00:00:00Z",
            "payload": {
                "response_id": "response-1",
                "thread_id": "physical-1",
                "turn_id": "turn-1",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 30,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                },
            },
        }
        source = tmp_path / "rollout.jsonl"
        source.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "physical-1"}})
            + "\n"
            + json.dumps(record)
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": "2026-10-20T00:00:05Z",
                    "payload": {"type": "task_complete", "turn_id": "turn-1"},
                }
            )
            + "\n"
        )
        spool.register_source(
            source,
            conv.id,
            "codex-native",
            {
                "harness_family": "codex",
                "identity_namespace": "codex/rollout/v1",
                "physical_transcript_path": "/opt/work/rollout.jsonl",
                "thread_kind": "main",
            },
        )
        spool.scan(source)
        source.unlink()
        assert await conversations.delete_conversation(conv.id)
        assert conversations.get_conversation(conv.id) is None

        async def lose_ack(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            response = await server.request(
                request.method,
                str(request.url),
                content=request.content,
                headers={"Content-Type": request.headers["Content-Type"]},
            )
            if request.url.path == "/v1/usage-observations":
                calls += 1
                if calls == 1:
                    assert response.status_code == 200, response.text
                    raise httpx.ReadError("ACK lost", request=request)
            return response

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lose_ack), base_url="https://omni.example.test"
        ) as transport:
            with pytest.raises(httpx.ReadError):
                await spool.drain(transport)
            await UsageOutbox(tmp_path / "usage").drain(transport)
        response = await server.get("/v1/usage-events")
        assert response.status_code == 200, response.text
        events = response.json()["events"]
        assert len(events) == 2
        event = events[-1]
        assert events[0]["kind"] == "usage"
        assert event["kind"] == "correction"
        assert event["original_event_id"] == events[0]["event_id"]
        assert event["input_tokens"] == 70
        assert event["output_tokens"] == 20
        assert event["reasoning_tokens"] == 5
        assert event["request_count"] == 1
        # ended_at is the server-clock bound of the revision that carried the
        # physical thread's terminal evidence, never the host timestamp.
        assert events[0]["ended_at"] is None
        assert "terminal" not in event
        assert event["ended_at"] == event["recorded_at"]
        assert response.json()["host_progress"][0]["last_sealed_turn_at"] == event["ended_at"]
        assert event["account_hint"] is None
        assert (await server.get("/v1/usage-events?limit=0")).status_code == 422
        server.headers["X-Forwarded-Email"] = "bob"
        assert (await server.get("/v1/usage-events")).json()["events"] == []
        assert (
            await server.post(
                "/v1/usage-observations",
                json={"producer_id": uuid.uuid4().hex, "observation": event},
            )
        ).status_code == 422


def _cutover_app(
    conversations: SqlAlchemyConversationStore,
    *,
    owner: str = "alice",
    harness: str = "codex-native",
) -> FastAPI:
    """The cutover control plane plus a probe standing in for the §3 gates."""
    app = FastAPI()
    app.include_router(
        create_usage_cutover_router(
            conversations.storage_location,
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
            host_owner=lambda host_id, _workspace=None: owner,
        ),
        prefix="/v1",
    )
    resolve = lambda _conv: harness  # noqa: E731 - test-local resolver

    @app.post("/probe/{host_id}")
    async def probe(host_id: str, source: str = "web") -> dict[str, object]:
        conv = Conversation(
            id="conv_test",
            created_at=0,
            updated_at=0,
            root_conversation_id="conv_test",
            title="probe",
            host_id=host_id,
        )
        admission_id = admit_turn(
            conversations.storage_location,
            conv,
            source=source,
            resolve_harness=resolve,
            owner_id="alice",
        )
        return {"admission_id": admission_id}

    @app.post("/probe-control/{host_id}")
    async def probe_control(host_id: str) -> dict[str, bool]:
        conv = Conversation(
            id="conv_test",
            created_at=0,
            updated_at=0,
            root_conversation_id="conv_test",
            title="probe",
            host_id=host_id,
        )
        refuse_if_held(conversations.storage_location, conv, resolve_harness=resolve)
        return {"ok": True}

    @app.exception_handler(OmnigentError)
    async def application_error(_request: object, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.http_status)

    return app


@pytest.mark.asyncio
async def test_native_hook_and_plan_followup_cannot_bypass_a_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from omnigent.harnesses.codex_native.forwarder import _plan_implementation_request_event
    from omnigent.runtime import pending_inputs
    from omnigent.server.routes.sessions import create_sessions_router
    from omnigent.server.schemas import ElicitationResult
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.usage_export.cutover import UsageCutoverStore

    uri = f"sqlite:///{tmp_path / 'gates.db'}"
    conversations = SqlAlchemyConversationStore(uri)
    host = "a" * 32
    conv = conversations.create_conversation(host_id=host, workspace="/opt/work")
    conversations.update_conversation(conv.id, harness_override="claude-native")
    SqlAlchemyPermissionStore(uri).grant("alice", conv.id, LEVEL_OWNER)
    app = _cutover_app(conversations)
    app.include_router(
        create_sessions_router(conversations, SqlAlchemyAgentStore(uri)), prefix="/v1"
    )
    store = UsageCutoverStore(uri)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://omni.test"
    ) as client:
        request = {"event": {"type": "PHASE_REQUEST", "data": {}, "context": {}}}
        admitted = await client.post(f"/v1/sessions/{conv.id}/policies/evaluate", json=request)
        assert admitted.status_code == 200, admitted.text
        admission_id = admitted.json()["admission_id"]
        pending = pending_inputs.record(conv.id, [], admission_id=admission_id)
        store.create_hold("alice", host, "claude-native", 1)
        interleaved = await client.post(f"/v1/sessions/{conv.id}/policies/evaluate", json=request)
        assert interleaved.json()["admission_id"] == admission_id
        pending_inputs.resolve(conv.id, pending)
        denied = await client.post(f"/v1/sessions/{conv.id}/policies/evaluate", json=request)
        assert denied.json()["result"] == "POLICY_ACTION_DENY"
        assert denied.json()["reason"] == "usage_cutover_hold"

        conversations.update_conversation(conv.id, harness_override="codex-native")
        store.create_hold("alice", host, "codex-native", 1)
        monkeypatch.setattr(
            "omnigent.server.routes.sessions.routes_hooks._publish_and_wait_for_harness_elicitation",
            AsyncMock(
                return_value=ElicitationResult(
                    action="accept", content={"plan_implementation": "Yes, implement this plan"}
                )
            ),
        )
        plan = await client.post(
            f"/v1/sessions/{conv.id}/hooks/codex-elicitation-request",
            json=_plan_implementation_request_event("thread", "turn"),
        )
        assert plan.status_code == 200, plan.text
        assert plan.json() == {"answers": {}}


def _drain_body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "detection": {
            "checkpoint_digest": "0" * 64,
            "clock_offset_ms": 0,
            "threshold_at": "2026-01-01T00:00:00Z",
        },
        "admissions": [],
        "calls": [],
        "counts": {},
        "failure_codes": [],
    }
    body.update(changes)
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex-native", "pi-native", "pi", "antigravity-native"])
async def test_cutover_gate_refuses_a_held_domain_and_records_nothing(
    tmp_path: Path,
    harness: str,
) -> None:
    uri = f"sqlite:///{tmp_path / 'server.db'}"
    conversations = SqlAlchemyConversationStore(uri)
    app = _cutover_app(conversations, harness=harness)
    held = "a" * 32
    other = "b" * 32
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://omni.example.test",
        headers={"X-Forwarded-Email": "alice"},
    ) as server:
        created = await server.post(
            "/v1/usage-cutover/holds",
            json={"host_id": held, "harness": harness, "generation": 1},
        )
        assert created.status_code == 200
        refused = await server.post(f"/probe/{held}")
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "usage_cutover_hold"
        control = await server.post(f"/probe-control/{held}")
        assert control.status_code == 409
        assert control.json()["error"]["code"] == "usage_cutover_hold"
        # A refused turn writes no admission.
        status = await server.get(f"/v1/usage-cutover/holds/{held}/{harness}")
        assert status.json()["open_admissions"] == 0
        # Another domain is unaffected.
        passed = await server.post(f"/probe/{other}")
        assert passed.status_code == 200
        assert bool(passed.json()["admission_id"]) is (harness == "codex-native")
        # Abandoning the hold lifts it.
        from omnigent.usage_export.store import UsageEventStore

        pins = UsageEventStore(uri).page("alice")
        abandoned = await server.post(
            f"/v1/usage-cutover/holds/{held}/{harness}/1/release",
            json={
                "abandon": True,
                "reason": "clock",
                "consumer_receipt": {
                    "kind": "abandon",
                    **{key: pins[key] for key in ("owner_scope_id", "feed_id", "feed_epoch")},
                    "domain": {"host_id": held, "harness": harness},
                    "generation": 1,
                    "producer_receipt_digest": "2" * 64,
                    "acked_through_sequence": None,
                    "usage_read_grant_id": None,
                    "legacy_frozen": False,
                    "cutover_at": None,
                    "authority_revision": None,
                    "authority_digest": None,
                    "authority_published": False,
                    "restored_authority_revision": None,
                    "restored_authority_digest": None,
                },
            },
        )
        assert abandoned.status_code == 200
        assert abandoned.json()["result"] == "abandoned"
        after = await server.post(f"/probe/{held}")
        assert after.status_code == 200


@pytest.mark.asyncio
async def test_receipts_route_serves_the_r4_page_shape_and_errors(
    tmp_path: Path,
) -> None:
    uri = f"sqlite:///{tmp_path / 'server.db'}"
    conversations = SqlAlchemyConversationStore(uri)
    app = _cutover_app(conversations)
    host = "c" * 32
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://omni.example.test",
        headers={"X-Forwarded-Email": "alice"},
    ) as server:
        missing = await server.get("/v1/usage-cutover/receipts")
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == "usage_cutover_bad_request"
        assert missing.headers["cache-control"] == "no-store"
        unknown = await server.get(
            "/v1/usage-cutover/receipts",
            params={"host_id": host, "harness": "codex-native", "generation": 1},
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "usage_cutover_domain_not_found"
        assert (
            await server.post(
                "/v1/usage-cutover/holds",
                json={"host_id": host, "harness": "codex-native", "generation": 2},
            )
        ).status_code == 200
        empty = await server.get(
            "/v1/usage-cutover/receipts",
            params={"host_id": host, "harness": "codex-native", "generation": 2},
        )
        assert empty.status_code == 200
        assert empty.json() == {
            "receipt_page_schema_version": 1,
            "domain": {"harness": "codex-native", "host_id": host},
            "generation": 2,
            "receipts": [],
            "has_more": False,
            "next_after_seq": 0,
        }
        wrong_generation = await server.get(
            "/v1/usage-cutover/receipts",
            params={"host_id": host, "harness": "codex-native", "generation": 1},
        )
        assert wrong_generation.status_code == 404
        assert wrong_generation.json()["error"]["code"] == "usage_cutover_generation_not_found"
        for params in (
            {"host_id": host, "harness": "codex-native", "generation": 0},
            {"host_id": host, "harness": "codex-native", "generation": 2, "limit": 0},
            {"host_id": host, "harness": "codex-native", "generation": 2, "limit": 51},
            {"host_id": host, "harness": "codex-native", "generation": 2, "after_seq": -1},
            {"host_id": host, "harness": "opencode", "generation": 2},
        ):
            bad = await server.get("/v1/usage-cutover/receipts", params=params)
            assert bad.status_code == 400, params
            assert bad.json()["error"]["code"] == "usage_cutover_bad_request"
        drained = await server.post(
            f"/v1/usage-cutover/holds/{host}/codex-native/2/receipts",
            json=_drain_body(),
        )
        assert drained.status_code == 200, drained.text
        assert drained.json()["result"] == "drained"
        page = await server.get(
            "/v1/usage-cutover/receipts",
            params={"host_id": host, "harness": "codex-native", "generation": 2},
        )
        assert page.status_code == 200
        body = page.json()
        assert body["receipts"][0]["digest"] == drained.json()["digest"]
        assert body["receipts"][0]["receipt_seq"] == 1
        assert body["next_after_seq"] == 1
        assert body["has_more"] is False
        assert page.headers["cache-control"] == "no-store"
        # A second receipt (late violation) makes the first page report more.
        from omnigent.usage_export.cutover import UsageCutoverStore

        UsageCutoverStore(uri).abandon(
            "alice",
            host,
            "codex-native",
            2,
            {
                "kind": "abandon",
                **{
                    key: drained.json()[key] for key in ("owner_scope_id", "feed_id", "feed_epoch")
                },
                "domain": {"host_id": host, "harness": "codex-native"},
                "generation": 2,
                "authority_published": False,
            },
            "clock",
        )
        first = await server.get(
            "/v1/usage-cutover/receipts",
            params={
                "host_id": host,
                "harness": "codex-native",
                "generation": 2,
                "limit": 1,
            },
        )
        assert first.status_code == 200
        assert len(first.json()["receipts"]) == 1
        assert first.json()["has_more"] is True
        assert first.json()["next_after_seq"] == 1
        tail = await server.get(
            "/v1/usage-cutover/receipts",
            params={
                "host_id": host,
                "harness": "codex-native",
                "generation": 2,
                "after_seq": 1,
            },
        )
        assert tail.status_code == 200
        assert [item["kind"] for item in tail.json()["receipts"]] == ["abandon"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actual_harness", "registered_harness"),
    [
        ("claude-code-native", "claude-native"),
        ("pi-native", "pi-native"),
        ("pi", "pi"),
        ("antigravity-native", "antigravity-native"),
    ],
)
async def test_registration_requires_exact_owner_and_harness(
    tmp_path: Path, actual_harness: str, registered_harness: str
) -> None:
    uri = f"sqlite:///{tmp_path / 'server.db'}"
    store = SqlAlchemyConversationStore(uri)
    conv = store.create_conversation(agent_id="a" * 32, host_id="b" * 32, workspace="/opt/work")
    SqlAlchemyPermissionStore(uri).grant("alice", conv.id, LEVEL_OWNER)
    app = _app(store, actual_harness)
    registration = {
        "producer_id": uuid.uuid4().hex,
        "session_id": conv.id,
        "harness": registered_harness,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://omni.example.test"
    ) as client:
        assert (await client.get("/v1/usage-events")).status_code == 401
        client.headers["X-Forwarded-Email"] = "bob"
        assert (await client.post("/v1/usage-producers", json=registration)).status_code == 403
        client.headers["X-Forwarded-Email"] = "alice"
        assert (
            await client.post(
                "/v1/usage-producers", json={**registration, "harness": "codex-native"}
            )
        ).status_code == 409
        assert (await client.post("/v1/usage-producers", json=registration)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capture_enabled", "id_field", "namespace", "step_name", "fail_append"),
    [
        (False, "responseId", "antigravity/response-id/v1", "planner_response_text", None),
        (True, "responseId", "antigravity/response-id/v1", "planner_response_text", None),
        (
            True,
            "providerAssignedMessageId",
            "antigravity/provider-message-id/v1",
            "planner_response_text",
            None,
        ),
        (True, "messageId", "antigravity/message-id/v1", "planner_response_text", None),
        (True, "responseId", "antigravity/response-id/v1", "checkpoint", None),
        (True, "responseId", "antigravity/response-id/v1", "planner_response_text", 1),
        (True, "responseId", "antigravity/response-id/v1", "planner_response_text", 2),
    ],
)
async def test_agy_reader_usage_replay_exports_linked_native_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_enabled: bool,
    id_field: str,
    namespace: str,
    step_name: str,
    fail_append: int | None,
) -> None:
    from omnigent.harnesses.antigravity_native import reader
    from omnigent.usage_export import runtime

    uri = f"sqlite:///{tmp_path / 'server.db'}"
    conversations = SqlAlchemyConversationStore(uri)
    conv = conversations.create_conversation(
        agent_id="a" * 32, host_id="b" * 32, workspace="/opt/work"
    )
    SqlAlchemyPermissionStore(uri).grant("alice", conv.id, LEVEL_OWNER)
    spool = UsageOutbox(tmp_path / "usage")
    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1" if capture_enabled else "0")
    monkeypatch.setattr(runtime, "outbox", lambda: spool)

    message_roles = []

    async def accepted_native_event(**kwargs: object) -> httpx.Response:
        payload = kwargs["payload"]
        if (
            payload["type"] == "external_conversation_item"
            and payload["data"]["item_type"] == "message"
        ):
            message_roles.append(payload["data"]["item_data"]["role"])
        return httpx.Response(200)

    monkeypatch.setattr(reader, "post_session_event_with_retry", accepted_native_event)
    fixtures = Path(__file__).parents[2] / "fixtures/antigravity/steps"
    step = json.loads((fixtures / f"{step_name}.json").read_text())
    usage = step["metadata"]["modelUsage"]
    usage["responseHeader"] = {"x-source": "must-not-be-retained"}
    if step_name == "planner_response_text":
        usage.pop("responseId", None)
        usage.update(
            inputTokens="1000",
            outputTokens="100",
            thinkingOutputTokens="40",
            cacheReadTokens="200",
            cacheWriteTokens="0",
        )
        usage[id_field] = "agy-response-1"
        step["plannerResponse"]["modifiedResponse"] = "must-not-be-retained"
    if fail_append is not None:
        step["metadata"]["retryInfos"] = [{"usage": {**usage, "responseId": "agy-retry"}}]
        append = spool.append
        attempts = 0

        def interrupted_append(*args: object, **kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == fail_append:
                raise OSError("temporary spool failure")
            append(*args, **kwargs)

        async def no_delay(_seconds: float) -> None:
            await asyncio.sleep(0)

        monkeypatch.setattr(spool, "append", interrupted_append)
        monkeypatch.setattr(reader, "_sleep", no_delay)
        monkeypatch.setattr(reader, "get_available_models", lambda _port: {})
        user = json.loads((fixtures / "user_input.json").read_text())
        monkeypatch.setattr(reader, "get_trajectory_steps", lambda _port, _cascade: [user, step])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(conversations, "antigravity-native")),
        base_url="https://omni.example.test",
        headers={"X-Forwarded-Email": "alice"},
    ) as server:
        await runtime.prepare_capture(server, conv.id, "antigravity-native")
        spool.begin_admission(conv.id, "antigravity-native", {"admission_id": "not-proof"})
        if fail_append is not None:
            await asyncio.wait_for(
                reader._mirror_subagent_cascade(
                    port=4242,
                    child_cascade_id="native-cascade",
                    client=server,
                    child_session_id=conv.id,
                ),
                timeout=60,
            )
            assert attempts > fail_append
            assert message_roles == ["user", "assistant"]
        else:
            for _ in range(2):
                await reader._process_committed_step(
                    step,
                    client=server,
                    session_id=conv.id,
                    cascade_id="native-cascade",
                    state=reader._ReaderState(seen=set(), interacted=set(), model_catalog={}),
                    on_pending_interaction=accepted_native_event,
                )
        assert (
            spool.pending_admission(conv.id, "antigravity-native")["admission_id"] == "not-proof"
        )
        spool.finish_admission(conv.id, "antigravity-native")
        await spool.drain(server)
        await spool.drain(server)
        page = (await server.get("/v1/usage-events")).json()
    if not capture_enabled:
        assert page["events"] == []
        return
    assert len(page["events"]) == (2 if fail_append is not None else 1)
    event = page["events"][0]
    assert (event["harness"], event["harness_family"]) == ("antigravity-native", "antigravity")
    assert (event["session_id"], event["physical_session_id"]) == (conv.id, "native-cascade")
    assert event["turn_id"] == step["metadata"]["executionId"]
    assert (
        event["input_tokens"],
        event["cached_input_tokens"],
        event["output_tokens"],
        event["reasoning_tokens"],
    ) == ((None, None, 3, None) if step_name == "checkpoint" else (800, 200, 100, 40))
    assert event["logical_call_id"] == usage[id_field] and event["request_count"] == 1
    assert event["source_refs"] == [
        {"kind": "response", "namespace": namespace, "id": usage[id_field]}
    ]
    if fail_append is not None:
        assert page["events"][1]["logical_call_id"] == "agy-retry"
        assert page["events"][1]["request_count"] == 1
        assert all(row["thread_kind"] == "subagent" for row in page["events"])
    assert event["account_hint"] is None and event["call_started_not_before"] is None
    assert "must-not-be-retained" not in json.dumps(page)
    assert b"must-not-be-retained" not in spool.path.read_bytes()


@pytest.mark.asyncio
async def test_read_only_policy_evaluation_cannot_mutate_admissions(tmp_path: Path) -> None:
    from sqlalchemy import text

    from omnigent.db.utils import get_or_create_engine
    from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
    from omnigent.server.routes.sessions import create_sessions_router
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    uri = f"sqlite:///{tmp_path / 'read-only.db'}"
    conversations = SqlAlchemyConversationStore(uri)
    host = "a" * 32
    conv = conversations.create_conversation(host_id=host, workspace="/opt/work")
    conversations.update_conversation(conv.id, harness_override="claude-native")
    permissions = SqlAlchemyPermissionStore(uri)
    permissions.grant("owner", conv.id, LEVEL_OWNER)
    permissions.grant("reader", conv.id, LEVEL_READ)
    permissions.grant("editor", conv.id, LEVEL_EDIT)
    app = _cutover_app(conversations)
    app.include_router(
        create_sessions_router(
            conversations,
            SqlAlchemyAgentStore(uri),
            permission_store=permissions,
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        ),
        prefix="/v1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://omni.test"
    ) as client:
        request = {"event": {"type": "PHASE_REQUEST", "data": {}, "context": {}}}
        response = await client.post(
            f"/v1/sessions/{conv.id}/policies/evaluate",
            json=request,
            headers={"X-Forwarded-Email": "reader"},
        )
        assert response.status_code == 200, response.text
        assert "admission_id" not in response.json()
        with get_or_create_engine(uri).connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM usage_turn_admissions")).scalar_one()
                == 0
            )
        response = await client.post(
            f"/v1/sessions/{conv.id}/policies/evaluate",
            json=request,
            headers={"X-Forwarded-Email": "editor"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["admission_id"]


@pytest.mark.parametrize("initial", [None, "active", "paused"])
@pytest.mark.asyncio
async def test_external_goal_completion_closes_goal_admissions(
    tmp_path: Path, initial: str | None
) -> None:
    from omnigent.server.routes._sessions.helpers import _persist_external_goal_state
    from omnigent.server.schemas import SessionEventInput
    from omnigent.usage_export.cutover import UsageCutoverStore

    uri = f"sqlite:///{tmp_path / 'goal.db'}"
    conversations = SqlAlchemyConversationStore(uri)
    host = "a" * 32
    conv = conversations.create_conversation(host_id=host, workspace="/opt/work")
    SqlAlchemyPermissionStore(uri).grant("alice", conv.id, LEVEL_OWNER)
    if initial is not None:
        conversations.set_labels(conv.id, {"omnigent.goal_state": initial})
    store = UsageCutoverStore(uri)
    store.admit_turn(
        "alice",
        admission_id="goal",
        session_id=conv.id,
        host_id=host,
        harness="codex-native",
        source="goal",
    )
    from omnigent.usage_export.store import UsageExportError

    with pytest.raises(UsageExportError, match="codex_goal_active"):
        store.create_hold("alice", host, "codex-native", 1)
    await _persist_external_goal_state(
        conv.id,
        conversations.get_conversation(conv.id),
        SessionEventInput(type="external_goal_state", data={"state": None}),
        conversations,
    )
    assert store.create_hold("alice", host, "codex-native", 1)["state"] == "held"
