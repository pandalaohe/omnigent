"""Regression coverage for writer loading and resource-block normalization."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.agent_quota import AgentQuotaService, UsageWriterConflictError
from omnigent.server.routes.agent_quota import create_agent_quota_router
from omnigent.usage.contract import UsageReportV2
from tests.server.routes.test_usage_otlp import (
    BASE,
    HOUR,
    OWNER,
    documented,
)
from tests.server.routes.test_usage_otlp import (
    receiver as receiver,
)


def reload_service(service):
    return AgentQuotaService(
        service.conversations,
        None,
        None,
        None,
        None,
        service.snapshot_store,
        hours_store=service.hours_store,
    )


@pytest.mark.parametrize("schema", [1, 2])
async def test_hydration_failure_rejects_activity_and_next_attempt_retries(
    receiver, monkeypatch, schema
):
    app, service, hours = receiver
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(BASE + "metrics", json=documented("claude"))).status_code == 200
    reloaded = reload_service(service)
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(reloaded, auth_provider=None, host_store=None), prefix="/v1"
    )

    def fail(_owner):
        raise RuntimeError("snapshot temporarily unavailable")

    body = {"schema_version": schema, "observed_at": time.time(), "windows": [], "activity": []}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with monkeypatch.context() as patch:
            patch.setattr(reloaded.snapshot_store, "read", fail)
            response = await client.put("/v1/me/usage/accounts/claude-code", json=body)
            assert response.status_code == 503
        response = await client.put("/v1/me/usage/accounts/claude-code", json=body)
        assert response.status_code == 409
    [row] = hours.read(OWNER, HOUR, HOUR + 3600)
    assert row.input_tokens == 40


async def test_activity_write_waits_for_inflight_snapshot_read(receiver, monkeypatch):
    app, service, hours = receiver
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(BASE + "metrics", json=documented("claude"))).status_code == 200
    reloaded = reload_service(service)
    started, release, writing = threading.Event(), threading.Event(), threading.Event()
    read = reloaded.snapshot_store.read

    def blocking_read(owner):
        started.set()
        assert release.wait(5)
        return read(owner)

    def write():
        writing.set()
        return reloaded.report_account(
            OWNER,
            "claude-code",
            UsageReportV2(schema_version=2, observed_at=time.time(), activity=[]),
        )

    monkeypatch.setattr(reloaded.snapshot_store, "read", blocking_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        loading = pool.submit(reloaded.overview, OWNER)
        assert started.wait(5)
        writer = pool.submit(write)
        try:
            assert writing.wait(5)
            with pytest.raises(TimeoutError):
                writer.result(timeout=0.1)
        finally:
            release.set()
        loading.result(timeout=5)
        with pytest.raises(UsageWriterConflictError):
            writer.result(timeout=5)
    [row] = hours.read(OWNER, HOUR, HOUR + 3600)
    assert row.input_tokens == 40


async def test_rejection_during_failed_hydration_preserves_writer_after_flush(
    receiver, monkeypatch
):
    app, service, hours = receiver
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(BASE + "metrics", json=documented("claude"))).status_code == 200
    reloaded = reload_service(service)
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(reloaded, auth_provider=None, host_store=None), prefix="/v1"
    )

    def fail(_owner):
        raise RuntimeError("snapshot temporarily unavailable")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with monkeypatch.context() as patch:
            patch.setattr(reloaded.snapshot_store, "read", fail)
            rejected = await client.put("/v1/me/usage/accounts/claude-code", json={})
        reloaded.flush(force=True)
        restarted = reload_service(reloaded)
        body = UsageReportV2(schema_version=2, observed_at=time.time(), activity=[])
        with pytest.raises(UsageWriterConflictError):
            restarted.report_account(OWNER, "claude-code", body)
        assert rejected.status_code == 503
    [row] = hours.read(OWNER, HOUR, HOUR + 3600)
    assert row.input_tokens == 40


async def test_same_resource_blocks_normalize_together(receiver):
    app, _, hours = receiver
    payload = documented("codex")
    resource = payload["resourceMetrics"][0]
    second = deepcopy(resource)
    metric = resource["scopeMetrics"][0]["metrics"][0]
    points = metric["histogram"]["dataPoints"]
    metric["histogram"]["dataPoints"] = points[:1]
    second["scopeMetrics"][0]["metrics"][0]["histogram"]["dataPoints"] = points[1:2]
    payload["resourceMetrics"].append(second)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(BASE + "metrics", json=payload)).json() == {}
    [row] = hours.read(OWNER, HOUR, HOUR + 3600)
    assert (row.input_tokens, row.cached_tokens, row.total_tokens) == (40, 60, 100)
