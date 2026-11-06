"""Vendor response shapes and the account import boundary."""

import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, UnifiedAuthProvider
from omnigent.server.routes.agent_quota import create_agent_quota_router
from omnigent.server.server_config import usage_import_body_limit
from omnigent.stores.usage_hours_store import UsageHoursStore
from omnigent.usage.imports import ImportFormatError, ImportOptions, read_import
from tests.server.test_agent_quota import service_with_store

FIXTURES = Path(__file__).parent / "fixtures" / "usage_imports"
HOUR = 1_788_393_600  # 2026-09-03 00:00 UTC
NOW = HOUR + 86400 * 3


@pytest.mark.parametrize("limit", [1, 8192, 5 * 1024 * 1024])
def test_import_body_limit_uses_server_config(limit):
    assert usage_import_body_limit({}) == 5 * 1024 * 1024
    assert (
        usage_import_body_limit({"usage": {"ingest": {"max_body_bytes": {"import": limit}}}})
        == limit
    )


@pytest.mark.parametrize("limit", [0, -1, True, "1024", None])
def test_invalid_import_body_limit_refused(limit):
    with pytest.raises(ValueError, match=r"usage\.ingest\.max_body_bytes\.import"):
        usage_import_body_limit({"usage": {"ingest": {"max_body_bytes": {"import": limit}}}})


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize(
    ("name", "section", "expected"),
    [
        ("openai-usage", "activity", 1),
        ("openai-costs", "activity", 1),
        ("anthropic-usage", "activity", 1),
        ("anthropic-cost", "activity", 1),
        ("anthropic-analytics-usage", "activity", 1),
        ("anthropic-analytics-cost", "activity", 1),
        ("openrouter-key", "windows", 5),
        ("openrouter-credits", "balances", 1),
        ("newapi-user", "balances", 1),
        ("litellm-key", "windows", 1),
    ],
)
def test_official_shapes(name, section, expected):
    payload = fixture(name)
    original = deepcopy(payload)
    report = read_import(name.replace("-analytics", ""), payload, ImportOptions(), NOW)
    assert len(getattr(report, section)) == expected
    assert payload == original
    if section == "activity":
        assert report.activity[0].coverage == "complete"
    if name == "openai-usage":
        entry = report.activity[0]
        assert (
            entry.uncached_input_tokens,
            entry.cache_read_input_tokens,
            entry.cache_creation_input_tokens,
            entry.output_tokens,
        ) == (800, 150, 50, 200)
        assert entry.cost_usd is None and entry.requests == 10
    if name == "anthropic-usage":
        assert report.activity[0].cache_creation_input_tokens == 50
        assert report.activity[0].requests is None
    if name == "openai-costs":
        assert report.activity[0].cost_usd == 1.25
    if name in {"anthropic-cost", "anthropic-analytics-cost"}:
        assert report.activity[0].cost_usd == 1.2345
    if name == "newapi-user":
        assert report.balances[0].remaining == 2
        assert report.windows[0].used_value == 1
    if name == "openrouter-credits":
        assert report.balances[0].remaining == 74.75


def test_observation_and_bucket_intervals_determine_coverage():
    payload = fixture("openai-usage")
    bucket = payload["data"][0]
    payload["data"] += [
        {**deepcopy(bucket), "start_time": HOUR + 3600, "end_time": HOUR + 7200},
        {**deepcopy(bucket), "start_time": HOUR + 7200, "end_time": HOUR + 10740},
    ]
    options = ImportOptions(observed_at=HOUR + 7199)
    first = read_import("openai-usage", payload, options, HOUR + 8100)
    retry = read_import("openai-usage", payload, options, NOW)
    assert [e.coverage for e in first.activity] == ["complete", "partial", "partial"]
    assert first == retry
    late = read_import("openai-usage", payload, ImportOptions(), NOW)
    assert late.activity[-1].coverage == "partial"


@pytest.mark.parametrize("width,offset", [(60, 0), (86400, 0), (3600, 1)])
def test_usage_refuses_incompatible_grain(width, offset):
    payload = fixture("openai-usage")
    bucket = payload["data"][0]
    bucket.update(start_time=HOUR + offset, end_time=HOUR + offset + width)
    if width == 60:
        payload["data"].append({**bucket, "start_time": HOUR + 60, "end_time": HOUR + 120})
    with pytest.raises(ImportFormatError, match="hour"):
        read_import("openai-usage", payload, ImportOptions(), NOW)


def test_duplicate_buckets_refused_but_grouped_results_sum():
    payload = fixture("openai-usage")
    payload["data"][0]["results"] *= 2
    report = read_import("openai-usage", payload, ImportOptions(), NOW)
    assert len(report.activity) == 1 and report.activity[0].requests == 20
    payload["data"] *= 2
    with pytest.raises(ImportFormatError, match="hour"):
        read_import("openai-usage", payload, ImportOptions(), NOW)


def test_analytics_refresh_time_and_cache_field_aliases():
    payload = fixture("anthropic-analytics-usage")
    payload["data_refreshed_at"] = "2026-09-03T00:59:00Z"
    report = read_import("anthropic-usage", payload, ImportOptions(), NOW)
    assert report.observed_at == HOUR + 3540
    assert report.activity[0].coverage == "partial"
    assert report.activity[0].cache_creation_input_tokens == 50
    payload["data"].append(deepcopy(payload["data"][0]))
    report = read_import("anthropic-usage", payload, ImportOptions(), NOW)
    assert len(report.activity) == 1 and report.activity[0].requests == 20


@pytest.mark.parametrize("name", ["anthropic-usage", "anthropic-cost"])
@pytest.mark.parametrize(
    "dimension,value",
    [
        ("workspace_id", "workspace-a"),
        ("api_key_id", "key-a"),
        ("service_tier", "standard"),
        ("fast_mode", False),
    ],
)
def test_analytics_refuses_additional_grouping(name, dimension, value):
    payload = fixture(name.replace("anthropic-", "anthropic-analytics-"))
    payload["data"][0]["group"][dimension] = value
    with pytest.raises(ImportFormatError) as failure:
        read_import(name, payload, ImportOptions(), NOW)
    assert dimension in str(failure.value)
    assert "drop that grouping" in str(failure.value)
    assert "push all pages in one request" in str(failure.value)


@pytest.mark.parametrize(
    "group", [{}, {"model": None}, {"model": "model-a", "workspace_id": None, "api_key_id": ""}]
)
def test_analytics_accepts_ungrouped_or_model_only(group):
    payload = fixture("anthropic-analytics-usage")
    payload["data"][0]["group"] = group
    report = read_import("anthropic-usage", payload, ImportOptions(), NOW)
    assert len(report.activity) == 1
    assert report.activity[0].model == (group.get("model") or "")


def test_explicit_uncached_and_missing_fields_remain_distinct():
    payload = fixture("openai-usage")
    row = payload["data"][0]["results"][0]
    row["input_uncached_tokens"] = 700
    assert (
        read_import("openai-usage", payload, ImportOptions(), NOW)
        .activity[0]
        .uncached_input_tokens
        == 700
    )
    for name in ("input_uncached_tokens", "input_cached_tokens", "input_cache_write_tokens"):
        row.pop(name)
    entry = read_import("openai-usage", payload, ImportOptions(), NOW).activity[0]
    assert entry.cache_read_input_tokens is None and entry.cache_creation_input_tokens is None
    assert entry.uncached_input_tokens is None
    assert entry.tokens == 1200


@pytest.mark.parametrize("missing", ["input_cached_tokens", "input_cache_write_tokens"])
def test_unknown_cache_component_keeps_uncached_unknown(missing):
    payload = fixture("openai-usage")
    payload["data"][0]["results"][0].pop(missing)
    entry = read_import("openai-usage", payload, ImportOptions(), NOW).activity[0]
    assert entry.uncached_input_tokens is None
    assert entry.tokens == 1200


@pytest.mark.parametrize("name,currency", [("openai-costs", "eur"), ("anthropic-cost", "CNY")])
def test_non_usd_costs_refused(name, currency):
    payload = fixture(name)
    row = payload["data"][0]["results"][0]
    if name == "openai-costs":
        row["amount"]["currency"] = currency
    else:
        row["currency"] = currency
    with pytest.raises(ImportFormatError, match="currency"):
        read_import(name, payload, ImportOptions(), NOW)


def test_reading_variants():
    payload = fixture("openrouter-key")
    data = payload["data"]
    data.update(limit=None, budgets=[])
    report = read_import("openrouter-key", payload, ImportOptions(), NOW)
    assert [w.id for w in report.windows] == ["spent-day", "spent-week", "spent-month"]
    data.update(limit=100, limit_remaining=20, limit_reset=None)
    report = read_import("openrouter-key", payload, ImportOptions(), NOW)
    assert report.windows[0].kind == "term" and report.windows[0].window_minutes is None
    payload = fixture("litellm-key")
    payload["info"]["max_budget"] = None
    report = read_import("litellm-key", payload, ImportOptions(), NOW)
    assert report.windows[0].id == "spend" and report.windows[0].limit_value is None
    report = read_import(
        "newapi-user", fixture("newapi-user"), ImportOptions(units_per_usd=100000), NOW
    )
    assert report.balances[0].remaining == 10 and report.windows[0].used_value == 5


def test_pages_and_empty_buckets():
    payload = fixture("anthropic-usage")
    payload["data"][0]["results"] = []
    assert read_import("anthropic-usage", payload, ImportOptions(), NOW).activity == []
    payload = fixture("openai-costs")
    bucket = payload["data"][0]
    bucket["end_time"] -= 60
    report = read_import("openai-costs", payload, ImportOptions(correction=True), NOW)
    assert report.activity[0].coverage == "partial" and report.activity[0].correction is False


@pytest.mark.parametrize(
    "name", ["openai-usage", "anthropic-usage", "openai-costs", "anthropic-cost"]
)
def test_formats_refuse_bad_rows_atomically(name):
    payload = fixture(name)
    payload["data"].append({})
    with pytest.raises(ImportFormatError):
        read_import(name, payload, ImportOptions(), NOW)


@pytest.mark.parametrize("value", [True, -1, "12", float("inf"), float("nan")])
def test_numeric_validation(value):
    payload = fixture("openai-usage")
    payload["data"][0]["results"][0]["input_tokens"] = value
    with pytest.raises(ImportFormatError):
        read_import("openai-usage", payload, ImportOptions(), NOW)


@pytest.mark.parametrize(
    "name,age,valid",
    [
        ("openai-usage", 86400 * 2, True),
        ("openai-usage", 86400 * 401, False),
        ("openrouter-key", 7200, False),
        ("openai-usage", -601, False),
        ("openrouter-key", -601, False),
    ],
)
def test_observed_at_bounds(name, age, valid):
    options = ImportOptions(observed_at=NOW - age)
    if valid:
        read_import(name, fixture(name), options, NOW)
    else:
        with pytest.raises(ImportFormatError, match="observed_at"):
            read_import(name, fixture(name), options, NOW)


@pytest.mark.asyncio
async def test_import_route_outcomes_limits_and_rejections(db_uri, monkeypatch):
    service = service_with_store(db_uri)
    service.hours_store = UsageHoursStore(db_uri)
    monkeypatch.setattr("omnigent.server.routes.agent_quota.time.time", lambda: NOW)
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=None, host_store=None), prefix="/v1"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/v1/me/usage/accounts/vendor/import"
        payload = fixture("openai-usage")
        response = await client.post(path + "?format=openai-usage", json=payload)
        assert response.status_code == 200, response.text
        assert response.json() == {"applied": 1, "held": [], "skipped": [], "older_sections": []}
        payload["data"][0]["results"][0]["output_tokens"] = 1
        reply = await client.post(path + "?format=openai-usage", json=payload)
        assert reply.json()["held"][0]["reason"] == "lower_reread"
        reply = await client.post(path + "?format=openai-usage&correction=1", json=payload)
        assert reply.json()["applied"] == 1
        reply = await client.post(
            path + f"?format=openai-usage&observed_at={HOUR + 3599}", json=payload
        )
        assert reply.json()["skipped"][0]["reason"] == "partial_over_complete"
        for query, body, status in [
            ("format=unknown", {}, 404),
            ("format=openrouter-key", fixture("newapi-user"), 422),
            ("format=openai-usage&observed_at=nan", payload, 422),
            ("format=newapi-user&units_per_usd=0", fixture("newapi-user"), 422),
        ]:
            reply = await client.post(path + "?" + query, json=body)
            assert reply.status_code == status, reply.text
        huge_amount = fixture("anthropic-cost")
        huge_amount["data"][0]["results"][0]["amount"] = "1e1000002"
        reply = await client.post(path + "?format=anthropic-cost", json=huge_amount)
        assert reply.status_code == 422, reply.text
        service.flush(force=True)
        record = service.snapshot_store.read(RESERVED_USER_LOCAL)["usage.account.vendor"]
        assert record["source"] == "openai-usage"
        rejected = [e for e in record["report_log"] if e["kind"] == "import_rejected"]
        assert len(rejected) == 4 and rejected[0]["format"] == "openrouter-key"
        assert rejected[-1]["format"] == "anthropic-cost"
        assert "quota" not in json.dumps(rejected)
        reply = await client.post(
            path + "?format=openai-usage",
            content="{",
            headers={"Content-Type": "application/json"},
        )
        assert reply.status_code == 422
        reply = await client.post(
            path + "?format=openai-usage",
            content=b" " * (5 * 1024 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )
        assert reply.status_code == 413
        service.usage_enabled = False
        assert (await client.post(path + "?format=openai-usage", json=payload)).status_code == 404


@pytest.mark.asyncio
async def test_import_over_500_rows_and_section_ordering(db_uri, monkeypatch):
    service = service_with_store(db_uri)
    service.hours_store = UsageHoursStore(db_uri)
    monkeypatch.setattr("omnigent.server.routes.agent_quota.time.time", lambda: NOW)
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=None, host_store=None), prefix="/v1"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        payload = fixture("openai-usage")
        row = payload["data"][0]["results"][0]
        payload["data"][0]["results"] = [{**row, "model": f"model-{n}"} for n in range(501)]
        reply = await client.post(
            "/v1/me/usage/accounts/vendor/import?format=openai-usage", json=payload
        )
        assert reply.status_code == 200 and reply.json()["applied"] == 501, reply.text
        path = "/v1/me/usage/accounts/wallet/import?format=newapi-user"
        assert (await client.post(path, json=fixture("newapi-user"))).status_code == 200
        reply = await client.post(path + f"&observed_at={NOW - 60}", json=fixture("newapi-user"))
        assert reply.json()["older_sections"] == ["windows", "balances"]


@pytest.mark.asyncio
async def test_import_owner_scope_and_account_limit(db_uri):
    service = service_with_store(db_uri)
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(
            service,
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
            host_store=None,
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def application_error(_request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": error.message}, status_code=error.http_status)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/v1/me/usage/accounts/vendor/import?format=openrouter-credits"
        assert (await client.post(path, json=fixture("openrouter-credits"))).status_code == 401
        client.headers["X-Forwarded-Email"] = "alice"
        assert (await client.post(path, json=fixture("openrouter-credits"))).status_code == 200
        client.headers["X-Forwarded-Email"] = "bob"
        assert all(
            a["id"] != "vendor" for a in (await client.get("/v1/me/usage")).json()["accounts"]
        )
        for index in range(32):
            reply = await client.post(
                f"/v1/me/usage/accounts/a{index}/import?format=openrouter-credits",
                json=fixture("openrouter-credits"),
            )
            assert reply.status_code == 200
        assert (await client.post(path, json=fixture("openrouter-credits"))).status_code == 409
        assert (await client.post(path, json={})).status_code == 409


@pytest.mark.asyncio
async def test_import_invalid_page_does_not_write_and_source_kept(db_uri, monkeypatch):
    service = service_with_store(db_uri)
    service.hours_store = UsageHoursStore(db_uri)
    monkeypatch.setattr("omnigent.server.routes.agent_quota.time.time", lambda: NOW)
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=None, host_store=None), prefix="/v1"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/v1/me/usage/accounts/vendor"
        reply = await client.put(
            path, json={"schema_version": 2, "observed_at": NOW, "source": "my-feeder"}
        )
        assert reply.status_code == 200
        payload = fixture("openai-usage")
        payload["data"].append({})
        reply = await client.post(path + "/import?format=openai-usage", json=payload)
        assert reply.status_code == 422
        assert service.hours_store.read(RESERVED_USER_LOCAL, HOUR, NOW) == []
        payload["data"].pop()
        assert (
            await client.post(path + "/import?format=openai-usage", json=payload)
        ).status_code == 200
        reply = await client.put(
            path,
            json={
                "schema_version": 2,
                "observed_at": NOW,
                "activity": [{"hour": HOUR, "model": "model-a", "tokens": 2000}],
            },
        )
        assert reply.json()["applied"] == 1
        rows = service.hours_store.read(RESERVED_USER_LOCAL, HOUR, NOW)
        assert len(rows) == 1 and rows[0].total_tokens == 2000
        account = next(
            a for a in (await client.get("/v1/me/usage")).json()["accounts"] if a["id"] == "vendor"
        )
        assert account["source"] == "my-feeder"


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_analytics_pages_sum_within_request_only(db_uri, monkeypatch, reverse):
    service = service_with_store(db_uri)
    service.hours_store = UsageHoursStore(db_uri)
    monkeypatch.setattr("omnigent.server.routes.agent_quota.time.time", lambda: NOW)
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=None, host_store=None), prefix="/v1"
    )
    first = fixture("anthropic-analytics-usage")
    second = deepcopy(first)
    second["data"][0]["usage"]["uncached_input_tokens"] = 600
    first.update(has_more=True, next_page="next-page")
    pages = [second, first] if reverse else [first, second]
    combined = {**first, "data": [row for page in pages for row in page["data"]]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/v1/me/usage/accounts/combined/import?format=anthropic-usage"
        for _ in range(2):
            reply = await client.post(path, json=combined)
            assert reply.status_code == 200 and reply.json()["applied"] == 1
        rows = service.hours_store.read(RESERVED_USER_LOCAL, HOUR, NOW, "combined")
        assert len(rows) == 1 and rows[0].total_tokens == 2200
        for page in [*pages, pages[0]]:
            reply = await client.post(path.replace("combined", "separate"), json=page)
            assert reply.status_code == 200
        rows = service.hours_store.read(RESERVED_USER_LOCAL, HOUR, NOW, "separate")
        assert len(rows) == 1 and rows[0].total_tokens == 1200
        invalid = deepcopy(combined)
        invalid["data"][1]["group"]["workspace_id"] = "private-workspace"
        reply = await client.post(path.replace("combined", "invalid"), json=invalid)
        assert reply.status_code == 422
        assert "workspace_id" in reply.json()["detail"]
        assert "private-workspace" not in reply.text
        assert "push all pages in one request" in reply.json()["detail"]
        assert service.hours_store.read(RESERVED_USER_LOCAL, HOUR, NOW, "invalid") == []
        service.flush(force=True)
        record = service.snapshot_store.read(RESERVED_USER_LOCAL)["usage.account.invalid"]
        assert record["report_log"][-1]["kind"] == "import_rejected"
