"""Documented exporter payloads reach the hourly usage projection over OTLP/HTTP."""

from __future__ import annotations

import base64
import gzip
import json
import time
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict, ParseDict
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from omnigent.server.agent_quota import AgentQuotaService
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes.agent_quota import create_agent_quota_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.usage_hours_store import UsageHoursStore
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "usage_otlp"
HOUR = 1_793_923_200
OWNER = RESERVED_USER_LOCAL
BASE = "/v1/me/usage/otlp/v1/"


def documented(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())["body"]


def attribute(key, value):
    return {"key": key, "value": {"stringValue": value}}


def encode(payload, signal, media):
    if media == "application/json":
        return json.dumps(payload).encode()
    payload = deepcopy(payload)
    if signal == "traces":
        for resource in payload["resourceSpans"]:
            for scope in resource["scopeSpans"]:
                for span in scope["spans"]:
                    for key in ("traceId", "spanId"):
                        span[key] = base64.b64encode(bytes.fromhex(span[key])).decode()
        message = ExportTraceServiceRequest()
    else:
        message = ExportMetricsServiceRequest()
    return ParseDict(payload, message).SerializeToString()


@pytest.fixture
def receiver(db_uri):
    hours = UsageHoursStore(db_uri)
    service = AgentQuotaService(
        SqlAlchemyConversationStore(db_uri),
        None,
        None,
        None,
        None,
        UsageSnapshotStore(db_uri),
        hours_store=hours,
    )
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=None, host_store=None), prefix="/v1"
    )
    return app, service, hours


@pytest.mark.parametrize("media", ["application/json", "application/x-protobuf"])
@pytest.mark.parametrize("compressed", [False, True])
async def test_claude_four_buckets_cost_and_retry(receiver, media, compressed):
    app, service, hours = receiver
    payload = documented("claude")
    payload["resourceMetrics"][0]["resource"]["attributes"].append(
        attribute("user.email", "discard-me")
    )
    body = encode(payload, "metrics", media)
    headers = {"content-type": media}
    if compressed:
        body = gzip.compress(body)
        headers["content-encoding"] = "gzip"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.post(BASE + "metrics", content=body, headers=headers)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(media)
            assert response.content == (b"{}" if media == "application/json" else b"")
    [row] = hours.read(OWNER, HOUR, HOUR + 3600)
    assert (row.account_id, row.host_id, row.basis, row.priced) == (
        "claude-code",
        "otel:test-host",
        "list",
        True,
    )
    assert (
        row.input_tokens,
        row.cached_tokens,
        row.cache_write_tokens,
        row.output_tokens,
        row.total_tokens,
    ) == (40, 60, 10, 20, 130)
    assert row.cost_usd == 0.125
    service.flush(force=True)
    stored = service.snapshot_store.read(OWNER)
    assert "discard-me" not in json.dumps(stored)


@pytest.mark.parametrize(
    "name,expected,basis,priced",
    [
        ("codex", (40, 60, 0, 20, 5, 120), "metered", True),
        ("gemini", (43, 60, 0, 25, 5, 128), "list", False),
        ("litellm-cache", (30, 60, 10, 20, None, 120), "metered", True),
        ("litellm-default", (None, None, None, 20, None, 120), "metered", True),
    ],
)
@pytest.mark.parametrize("media", ["application/json", "application/x-protobuf"])
async def test_documented_emitter_normalization(receiver, name, expected, basis, priced, media):
    app, _, hours = receiver
    signal = "traces" if name.startswith("litellm") else "metrics"
    body = encode(documented(name), signal, media)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.post(
                BASE + signal, content=body, headers={"content-type": media}
            )
            assert response.status_code == 200, response.text
    [row] = hours.read(OWNER, HOUR, HOUR + 3600)
    assert (
        row.input_tokens,
        row.cached_tokens,
        row.cache_write_tokens,
        row.output_tokens,
        row.reasoning_tokens,
        row.total_tokens,
    ) == expected
    assert (row.basis, row.priced) == (basis, priced)
    if signal == "traces":
        assert row.requests == 1
        assert row.failed == (1 if name == "litellm-cache" else 0)


@pytest.mark.parametrize("media", ["application/json", "application/x-protobuf"])
async def test_cumulative_partial_reply_and_late_cost_share_row(receiver, media):
    app, _, hours = receiver
    payload = documented("codex")
    metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    metrics.append(
        {
            "name": "codex.turn.cost_microusd",
            "histogram": {
                "aggregationTemporality": 2,
                "dataPoints": [{"timeUnixNano": str(HOUR * 10**9), "count": "1", "sum": 500000}],
            },
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            BASE + "metrics",
            content=encode(payload, "metrics", media),
            headers={"content-type": media},
        )
        assert response.status_code == 200
        data = (
            response.json()
            if media == "application/json"
            else MessageToDict(ExportMetricsServiceResponse.FromString(response.content))
        )
        partial = data["partialSuccess"]
        assert int(partial["rejectedDataPoints"]) == 1
        assert "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta" in partial["errorMessage"]
        cost = deepcopy(metrics[-1])
        cost["histogram"]["aggregationTemporality"] = 1
        cost["histogram"]["dataPoints"][0].update(
            attributes=[attribute("model", "model-a"), attribute("auth_mode", "api")],
            timeUnixNano=str((HOUR + 60) * 10**9),
        )
        metrics[:] = [cost]
        assert (await client.post(BASE + "metrics", json=payload)).status_code == 200
    [row] = hours.read(OWNER, HOUR, HOUR + 3600)
    assert row.input_tokens == 40
    assert row.cost_usd == 0.5


@pytest.mark.parametrize("name", ["claude", "codex", "gemini", "litellm-cache"])
async def test_managed_resource_is_excluded(receiver, name):
    app, _, hours = receiver
    payload = documented(name)
    signal = "traces" if name.startswith("litellm") else "metrics"
    resources = payload["resourceSpans" if signal == "traces" else "resourceMetrics"]
    resources[0]["resource"]["attributes"].append(attribute("launch_mode", "omni"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(BASE + signal, json=payload)
    assert response.status_code == 200
    assert response.json() == {}
    assert hours.read(OWNER, HOUR, HOUR + 3600) == []


async def test_owned_claude_external_session_is_excluded(receiver):
    app, service, hours = receiver
    conv = service.conversations.create_conversation()
    service.conversations.set_external_session_id(conv.id, "external-test-session")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(BASE + "metrics", json=documented("claude"))
    assert response.status_code == 200
    assert hours.read(OWNER, HOUR, HOUR + 3600) == []


@pytest.mark.parametrize("v2", [False, True])
async def test_activity_writer_conflict_both_directions(receiver, v2):
    app, _, hours = receiver
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(BASE + "metrics", json=documented("claude"))).status_code == 200
        report = {
            "schema_version": 2 if v2 else 1,
            "observed_at": time.time(),
            "status": "available",
            "activity": [],
            "windows": [],
        }
        conflict = await client.put("/v1/me/usage/accounts/claude-code", json=report)
        assert conflict.status_code == 409
        assert "writer_conflict" in conflict.text
        assert (await client.put("/v1/me/usage/accounts/pushed", json=report)).status_code in (
            200,
            204,
        )
        payload = documented("claude")
        payload["resourceMetrics"][0]["resource"]["attributes"].append(
            attribute("omnigent.usage.account", "pushed")
        )
        response = await client.post(BASE + "metrics", json=payload)
        assert int(response.json()["partialSuccess"]["rejectedDataPoints"]) == 5
        assert "writer_conflict" in response.text
    assert len(hours.read(OWNER, HOUR, HOUR + 3600)) == 1


async def test_product_account_rejected_and_logs_body_unread(receiver):
    app, _, hours = receiver
    payload = documented("claude")
    payload["resourceMetrics"][0]["resource"]["attributes"].append(
        attribute("omnigent.usage.account", "claude")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(BASE + "metrics", json=payload)
        assert response.status_code == 200
        assert int(response.json()["partialSuccess"]["rejectedDataPoints"]) == 5
        response = await client.post(
            BASE + "logs",
            content=b"not valid JSON or protobuf",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json() == {}
    assert hours.read(OWNER, HOUR, HOUR + 3600) == []


async def test_malformed_and_compression_limits(receiver, monkeypatch):
    app, service, hours = receiver
    monkeypatch.setattr(
        "omnigent.server.routes.agent_quota.load_server_config",
        lambda: {"usage": {"ingest": {"max_body_bytes": {"otlp": 1024}}}},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                BASE + "metrics", content=b"{", headers={"content-type": "application/json"}
            )
        ).status_code == 400
        assert (
            await client.post(
                BASE + "metrics",
                content=b"\xff",
                headers={"content-type": "application/x-protobuf"},
            )
        ).status_code == 400
        response = await client.post(
            BASE + "metrics",
            content=gzip.compress(b" " * 2048),
            headers={"content-type": "application/json", "content-encoding": "gzip"},
        )
        assert response.status_code == 413
        service.usage_enabled = False
        assert (await client.post(BASE + "logs", content=b"ignored")).status_code == 404
    assert hours.read(OWNER, HOUR, HOUR + 3600) == []


async def test_generic_fallback_per_resource_and_unmapped_diagnostic(receiver):
    app, service, hours = receiver
    payload = documented("generic")
    metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    unknown = deepcopy(metrics[0])
    unknown["name"] = "foo.token.count"
    metrics.append(unknown)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(2):
            assert (await client.post(BASE + "metrics", json=payload)).json() == {}
    rows = {row.account_id: row for row in hours.read(OWNER, HOUR, HOUR + 3600)}
    assert (
        rows["generic"].input_tokens,
        rows["generic"].cached_tokens,
        rows["generic"].output_tokens,
    ) == (40, 60, 20)
    assert (rows["fallback"].input_tokens, rows["fallback"].output_tokens) == (10, 2)
    service.flush(force=True)
    log = service.snapshot_store.read(OWNER)["usage.account.generic"]["report_log"]
    assert [(item["kind"], item.get("metric")) for item in log] == [
        ("unmapped_metric", "foo.token.count")
    ]


async def test_retries_after_storage_failure_keep_only_successful_groups(receiver, monkeypatch):
    app, _, hours = receiver
    payload = documented("codex")
    metric = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
    second = deepcopy(metric["histogram"]["dataPoints"][0])
    second["attributes"][1] = attribute("model", "model-b")
    metric["histogram"]["dataPoints"].append(second)
    original = hours.add_activity

    def fail_second(*args, **kwargs):
        if args[3] == "model-b":
            raise RuntimeError("storage unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(hours, "add_activity", fail_second)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(BASE + "metrics", json=payload)).status_code == 503
        monkeypatch.setattr(hours, "add_activity", original)
        assert (await client.post(BASE + "metrics", json=payload)).json() == {}
    rows = hours.read(OWNER, HOUR, HOUR + 3600)
    assert [(row.model, row.input_tokens) for row in rows] == [("model-a", 40), ("model-b", 100)]


async def test_account_limit_rejects_new_account_but_accepts_existing(receiver):
    app, service, hours = receiver
    from omnigent.usage.contract import UsageReportV2

    for index in range(32):
        service.report_account(
            OWNER, f"account-{index}", UsageReportV2(schema_version=2, observed_at=time.time())
        )
    payload = documented("claude")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(BASE + "metrics", json=payload)
        assert response.status_code == 200
        assert int(response.json()["partialSuccess"]["rejectedDataPoints"]) == 5
        payload["resourceMetrics"][0]["resource"]["attributes"].append(
            attribute("omnigent.usage.account", "account-0")
        )
        assert (await client.post(BASE + "metrics", json=payload)).json() == {}
    [row] = hours.read(OWNER, HOUR, HOUR + 3600)
    assert row.account_id == "account-0"


async def test_metadata_settings_and_writer_survive_reload(receiver):
    app, service, hours = receiver
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(BASE + "metrics", json=documented("codex"))).json() == {}
        response = await client.put(
            "/v1/me/usage/accounts/codex-cli",
            json={"schema_version": 2, "observed_at": time.time(), "source_type": "subscription"},
        )
        assert response.status_code == 200
        payload = documented("codex")
        points = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["histogram"][
            "dataPoints"
        ]
        for point in points:
            point["timeUnixNano"] = str((HOUR + 60) * 10**9)
        assert (await client.post(BASE + "metrics", json=payload)).json() == {}
    rows = hours.read(OWNER, HOUR, HOUR + 3600)
    assert {row.basis for row in rows} == {"metered", "list"}
    reloaded = AgentQuotaService(
        service.conversations, None, None, None, None, service.snapshot_store, hours_store=hours
    )
    from omnigent.server.agent_quota import UsageWriterConflictError
    from omnigent.usage.contract import UsageReportV2

    with pytest.raises(UsageWriterConflictError):
        reloaded.report_account(
            OWNER,
            "codex-cli",
            UsageReportV2(schema_version=2, observed_at=time.time(), activity=[]),
        )


def test_retry_and_claude_session_lookup_are_owner_scoped(receiver, db_uri):
    _, service, hours = receiver
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from omnigent.usage.otlp import OtlpReceiver, decode_export

    service.permissions = SqlAlchemyPermissionStore(db_uri)
    conv = service.conversations.create_conversation()
    service.permissions.grant("alice@example.test", conv.id, 4)
    service.conversations.set_external_session_id(conv.id, "external-test-session")
    receiver = OtlpReceiver(service)
    message = decode_export(
        json.dumps(documented("claude")).encode(), "application/json", "metrics"
    )
    receiver.receive("alice@example.test", message)
    receiver.receive("bob@example.test", message)
    assert hours.read("alice@example.test", HOUR, HOUR + 3600) == []
    [row] = hours.read("bob@example.test", HOUR, HOUR + 3600)
    assert row.input_tokens == 40


def test_retry_expires_after_two_hours_and_resource_attribute_order_is_irrelevant(
    receiver, monkeypatch
):
    from types import SimpleNamespace

    from omnigent.usage.otlp import OtlpReceiver, decode_export

    _, service, hours = receiver
    clock = [0.0]
    monkeypatch.setattr("omnigent.usage.otlp.time", SimpleNamespace(monotonic=lambda: clock[0]))
    receiver = OtlpReceiver(service)
    payload = documented("claude")

    def send():
        return receiver.receive(
            OWNER, decode_export(json.dumps(payload).encode(), "application/json", "metrics")
        )

    send()
    payload["resourceMetrics"][0]["resource"]["attributes"].reverse()
    clock[0] = 7199
    send()
    [row] = hours.read(OWNER, HOUR, HOUR + 3600)
    assert row.input_tokens == 40
    clock[0] = 7200
    send()
    [row] = hours.read(OWNER, HOUR, HOUR + 3600)
    assert row.input_tokens == 80


async def test_logs_sink_does_not_read_the_request_stream(receiver):
    app, _, _ = receiver

    async def unread():
        raise AssertionError("logs body was read")
        yield b"unreachable"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            BASE + "logs", content=unread(), headers={"content-type": "application/x-protobuf"}
        )
    assert response.status_code == 200
    assert response.content == b""


async def test_writer_snapshot_failure_rolls_back_the_counter(receiver, monkeypatch):
    app, _, hours = receiver

    def fail(*_args):
        raise RuntimeError("snapshot unavailable")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with monkeypatch.context() as patch:
            patch.setattr(UsageSnapshotStore, "write_in_session", staticmethod(fail))
            assert (
                await client.post(BASE + "metrics", json=documented("claude"))
            ).status_code == 503
        assert hours.read(OWNER, HOUR, HOUR + 3600) == []
        assert (await client.post(BASE + "metrics", json=documented("claude"))).json() == {}
    [row] = hours.read(OWNER, HOUR, HOUR + 3600)
    assert row.input_tokens == 40
