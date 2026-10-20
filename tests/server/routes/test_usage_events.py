"""Exercise producer admission, durable ACKs and the owner feed over ASGI."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.routes.usage_events import create_usage_events_router
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
        assert events[0]["ended_at"] is None
        assert event["ended_at"] == "2026-10-20T00:00:05Z"
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


@pytest.mark.asyncio
async def test_registration_requires_exact_owner_and_harness(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'server.db'}"
    store = SqlAlchemyConversationStore(uri)
    conv = store.create_conversation(agent_id="a" * 32, host_id="b" * 32, workspace="/opt/work")
    SqlAlchemyPermissionStore(uri).grant("alice", conv.id, LEVEL_OWNER)
    app = _app(store, "claude-code-native")
    registration = {
        "producer_id": uuid.uuid4().hex,
        "session_id": conv.id,
        "harness": "claude-native",
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
