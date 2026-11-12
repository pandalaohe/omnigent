"""Native pool intake and reads preserve actual usage and transaction boundaries."""

from datetime import date

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.auth import scope_allows
from omnigent.server.routes.agent_quota import create_agent_quota_router
from omnigent.stores.usage_pool_store import UsagePoolConflictError, UsagePoolStore
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore
from omnigent.usage.analysis import bounded_span
from omnigent.usage.contract import UsageReportV2
from omnigent.usage.pricing import PricingBaseline
from omnigent.usage.pricing_projection import value_usage
from tests.server.integration.test_global_instructions_routes import auth_app as authenticated_app  # noqa: F401
from tests.server.test_usage_activity import HOUR, NOW, OWNER
from tests.server.test_usage_activity import service as activity_service  # noqa: F401


@pytest.fixture
def pools(request, db_uri):
    service = request.getfixturevalue("activity_service")
    service.pool_store = UsagePoolStore(db_uri)
    push(service, "parent")
    return service


def push(service, account, relation=None, activity=None, **fields):
    payload = {"schema_version": 2, "observed_at": NOW, **fields}
    if relation is not None:
        payload["pool_relation"] = {
            "parent_account_id": "parent",
            "pool_id": "source:pool-a",
            "channel": "usage",
            "effective_from_hour": HOUR,
            **relation,
        }
    if activity is not None:
        payload["activity"] = activity
    return service.report_account(OWNER, account, UsageReportV2.model_validate(payload), now=NOW)


def entry(tokens=100, **fields):
    return {
        "hour": HOUR,
        "host": "host",
        "model": "model",
        "tokens": tokens,
        "requests": 10,
        "failed": 0,
        **fields,
    }


def test_two_pools_boundary_quota_and_cost_channels(pools):
    push(pools, "a", {}, [entry(0), entry(10, hour=HOUR - 3600)], display_name="First")
    push(pools, "b", {"pool_id": "source:pool-b"}, [entry(20)])
    push(pools, "q", {"pool_id": "source:quota", "channel": "quota"}, [entry(900)])
    push(pools, "paid", {"channel": "metered_cost"}, [entry(999, cost_usd=2)])
    push(pools, "list", {"channel": "list_cost"}, [entry(999, cost_usd=5, cost_basis="list")])
    push(pools, "a", {}, display_name="Renamed")
    view = pools.analysis(OWNER, now=NOW)
    catalog = {item.pool_id: item for item in view.pool_catalog}
    assert set(catalog) == {"source:pool-a", "source:pool-b", "source:quota"}
    assert catalog["source:quota"].usage_available is False
    assert catalog["source:pool-b"].usage_available is True
    parent = next(item.totals for item in view.parents if item.parent_account_id == "parent")
    assert (parent.tokens, parent.cost_usd) == (30, 2)
    actual = next(item.totals for item in view.pools if item.account_id == "a")
    assert actual.tokens == 0
    assert (
        next(row for row in view.rows if row.account_id == "a" and row.hour < HOUR).pool_id is None
    )
    assert all(
        row.tokens is None and row.requests is None
        for row in view.rows
        if row.channel in {"quota", "metered_cost", "list_cost"}
    )


def test_pricing_consumes_separate_source_list_and_paid_channels(pools):
    push(pools, "a", {}, [entry(100)])
    push(pools, "list", {"channel": "list_cost"}, [entry(999, cost_usd=5, cost_basis="list")])
    push(pools, "paid", {"channel": "metered_cost"}, [entry(999, cost_usd=2)])
    sample = pools.analysis(OWNER, now=NOW + 3600)
    listed = next(row for row in sample.rows if row.account_id == "list")
    assert not listed.contributes_money
    for pool in (None, "source:pool-a"):
        result = value_usage(sample, PricingBaseline(), pool=pool)
        assert result["api_value"].state == "exact"
        assert result["api_value"].low == "5"
        assert result["paid"].low == "2"
        assert result["payback"].low == "2.5"
        assert result["tokens"].low == "100"
    assert sample.parents[0].totals.cost_usd == 2


def test_conflict_and_snapshot_failure_rollback_every_write(pools, monkeypatch):
    push(pools, "a", {}, [entry(100)])
    before = pools.snapshot_store.read(OWNER)["usage.account.a"]
    with pytest.raises(UsagePoolConflictError):
        push(pools, "a", {"pool_id": "different"}, [entry(200)])
    assert pools.hours_store.read(OWNER, HOUR, HOUR + 3600, "a")[0].total_tokens == 100
    original = UsageSnapshotStore.write_in_session

    def fail_snapshot(session, owner, key, record):
        if key == "usage.account.b":
            raise RuntimeError("injected persistence failure")
        original(session, owner, key, record)

    monkeypatch.setattr(UsageSnapshotStore, "write_in_session", staticmethod(fail_snapshot))
    with pytest.raises(RuntimeError, match="injected persistence failure"):
        push(pools, "b", {"pool_id": "source:pool-b"}, [entry(200)])
    assert pools.pool_store.get(OWNER, "b") is None
    assert pools.hours_store.read(OWNER, HOUR, HOUR + 3600, "b") == []
    assert pools.snapshot_store.read(OWNER)["usage.account.a"] == before
    assert all(item.id != "b" for item in pools.overview(OWNER, now=NOW).accounts)


def test_old_reporter_does_not_erase_relation_or_corrections(pools):
    push(pools, "a", {}, [entry(100)])
    push(pools, "a", activity=[entry(80, correction=True)], pool_relation=None)
    assert pools.pool_store.get(OWNER, "a").pool_id == "source:pool-a"
    assert (
        pools.snapshot_store.read(OWNER)["usage.account.a"]["report"]["pool_relation"]["pool_id"]
        == "source:pool-a"
    )
    push(pools, "a", activity=[{"hour": HOUR, "host": "host", "model": "model", "removed": True}])
    assert pools.hours_store.read(OWNER, HOUR, HOUR + 3600, "a") == []


def test_parent_overlap_is_held_and_native_relay_is_counted_once(pools):
    push(pools, "parent", activity=[entry(100)], product="claude")
    push(pools, "a", {}, [entry(100)])
    pools.hours_store.add_turn(
        OWNER, "native:claude", HOUR, "model", "host", "metered", True, 100, 0, 0, 0, 100, 0
    )
    view = pools.analysis(OWNER, now=NOW)
    assert (
        next(item.totals.tokens for item in view.parents if item.parent_account_id == "parent")
        == 100
    )
    assert view.unmapped.reasons["unproven_overlap"] == 1
    assert view.partial


def test_alternative_prices_and_counts_agree_at_every_aggregation_level(pools):
    push(pools, "a", {}, [entry(100, cost_usd=2)])
    push(pools, "a", activity=[entry(100, cost_usd=3, cost_basis="billed")])
    view = pools.analysis(OWNER, now=NOW)
    totals = [view.parents[0].totals, view.pools[0].totals, view.accounts[0].totals]
    assert [(item.cost_usd, item.tokens, item.requests) for item in totals] == [(3, 100, 10)] * 3
    assert sum(row.contributes_usage for row in view.rows) == 1
    assert sum(row.contributes_money for row in view.rows) == 1


@pytest.mark.parametrize("basis,priced,cost", [("metered", False, 0), ("subscription", True, 2)])
def test_incremental_native_buckets_remain_additive(pools, basis, priced, cost):
    pools.hours_store.add_turn(
        OWNER, "native:codex", HOUR, "model", "host", "metered", True, 10, 0, 0, 1, 11, 0.5
    )
    pools.hours_store.add_turn(
        OWNER,
        "native:codex",
        HOUR,
        "model",
        "host",
        basis,
        priced,
        1_000_000,
        0,
        0,
        0,
        1_000_000,
        cost,
    )
    view = pools.analysis(OWNER, account="codex", now=NOW)
    assert view.parents[0].totals.tokens == 1_000_011
    assert view.parents[0].totals.cost_usd == cost + 0.5
    assert sum(row.contributes_usage for row in view.rows) == 2


@pytest.mark.parametrize("basis", ["metered", "list"])
def test_unit_cost_joins_chosen_bill_to_the_richer_usage_partitions(pools, basis):
    push(
        pools,
        "a",
        {},
        [
            entry(
                100,
                cost_usd=2,
                cost_basis=basis,
                uncached_input_tokens=80,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                output_tokens=20,
            )
        ],
    )
    push(pools, "a", activity=[entry(100, cost_usd=3, cost_basis="billed")])
    view = pools.analysis(OWNER, now=NOW)
    for value in (view.parents[0].totals, view.pools[0].totals, view.accounts[0].totals):
        assert value.cost_usd == 3
        assert value.per_million == 30_000
        assert value.covered_tokens == 100
        assert value.cost_coverage_pct == 100
        assert value.metered.per_million == 30_000
        assert value.metered.covered_tokens == 100


def test_cost_coverage_retains_unmatched_money_in_its_denominator(pools):
    push(
        pools,
        "a",
        {"effective_from_hour": HOUR - 3600},
        [entry(100, cost_usd=2), entry(0, cost_usd=3, hour=HOUR - 3600)],
    )
    view = pools.analysis(OWNER, now=NOW)
    for value in (view.parents[0].totals, view.pools[0].totals, view.accounts[0].totals):
        assert value.cost_usd == 5
        assert value.covered_cost_usd == 2
        assert value.cost_coverage_pct == 40
        assert value.metered.cost_coverage_pct == 40


def test_before_boundary_parent_overlap_stays_unattributed_without_double_count(pools):
    push(pools, "parent", activity=[entry(100)])
    push(pools, "a", {"effective_from_hour": HOUR + 3600}, [entry(100)])
    view = pools.analysis(OWNER, now=NOW)
    assert view.parents[0].totals.tokens == 100
    child = next(row for row in view.rows if row.account_id == "a")
    assert child.pool_id is None and not child.contributes_usage
    assert child.reason == "unproven_overlap"


def test_period_uses_selected_account_and_multi_account_keeps_own_bounds(pools):
    week_start = HOUR - 3 * 86400
    month_start = HOUR - 15 * 86400
    push(
        pools,
        "parent",
        windows=[
            {
                "id": "week",
                "kind": "weekly",
                "percent_used": 20,
                "window_minutes": 10080,
                "resets_at": week_start + 7 * 86400,
            }
        ],
        activity=[entry(3, hour=HOUR - 86400), entry(50, hour=HOUR - 10 * 86400)],
    )
    push(
        pools,
        "newer",
        observed_at=NOW + 1,
        windows=[
            {
                "id": "month",
                "kind": "monthly",
                "percent_used": 20,
                "window_minutes": 43200,
                "resets_at": month_start + 30 * 86400,
            }
        ],
        activity=[entry(7, hour=HOUR - 10 * 86400)],
    )
    selected = pools.analysis(OWNER, account="parent", range_key="period", now=NOW)
    assert selected.span.requested_from == week_start
    assert selected.parents[0].totals.tokens == 3
    combined = pools.analysis(OWNER, range_key="period", now=NOW)
    assert {item.parent_account_id: item.totals.tokens for item in combined.parents} == {
        "parent": 3,
        "newer": 7,
    }
    assert combined.account_spans["parent"].requested_from == week_start


@pytest.mark.asyncio
async def test_analysis_http_auth_and_real_app_pool_intake(request):
    import time

    app = request.getfixturevalue("authenticated_app")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://omni.example.test"
    ) as client:
        path = "/v1/me/usage/analysis"
        assert (await client.get(path)).status_code == 401
        headers = {"X-Forwarded-Email": OWNER}
        now = time.time()
        payload = {"schema_version": 2, "observed_at": now}
        assert (
            await client.put("/v1/me/usage/accounts/parent", headers=headers, json=payload)
        ).status_code == 200
        payload["pool_relation"] = {
            "parent_account_id": "parent",
            "pool_id": "source:pool-a",
            "channel": "usage",
            "effective_from_hour": int(now // 3600) * 3600,
        }
        response = await client.put("/v1/me/usage/accounts/a", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        response = await client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.json()["pool_catalog"][0]["pool_id"] == "source:pool-a"
        other = await client.get(path, headers={"X-Forwarded-Email": "other@example.test"})
        assert other.status_code == 200 and other.json()["pool_catalog"] == []


def test_query_bound_filters_cohort_before_limit_and_reports_partial(pools, monkeypatch):
    push(pools, "a", {}, [entry(3, model="a"), entry(4, model="b")])
    push(pools, "unrelated", activity=[entry(999, model="0")])
    monkeypatch.setattr("omnigent.server.agent_quota.MAX_ANALYSIS_ROWS", 2)
    view = pools.analysis(OWNER, account="parent", now=NOW)
    assert {row.account_id for row in view.rows} == {"a"}
    assert view.parents[0].totals.tokens == 7
    assert not view.truncated
    push(pools, "a", activity=[entry(5, model="c")])
    view = pools.analysis(OWNER, account="parent", now=NOW)
    assert len(view.rows) == 2 and view.truncated and view.partial
    assert pools.analysis("other@example.test", now=NOW).rows == []


def test_date_boundary_is_exclusive_and_year_span_is_supported(pools):
    assert bounded_span(HOUR - 365 * 86400, HOUR) == (
        HOUR - 365 * 86400,
        HOUR,
        HOUR - 365 * 86400,
        False,
    )
    push(pools, "a", {}, [entry(3, hour=HOUR - 86400), entry(4)])
    # HOUR is 2026-11-05 12:00 UTC, so an earlier day excludes both rows.
    view = pools.analysis(
        OWNER, range_key="custom", from_date=date(2026, 11, 3), to_date=date(2026, 11, 3), now=NOW
    )
    assert view.rows == []


@pytest.mark.asyncio
async def test_analysis_route_range_validation_and_usage_scope(pools):
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(pools, auth_provider=None, host_store=None), prefix="/v1"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://omni.example.test"
    ) as client:
        for query in ["range=unknown", "range=custom", "tz=invalid"]:
            assert (await client.get("/v1/me/usage/analysis?" + query)).status_code == 422
        response = await client.get("/v1/me/usage/analysis")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
    assert scope_allows("usage", "GET", "/v1/me/usage/analysis")
    assert not scope_allows("usage:read", "GET", "/v1/me/usage/analysis")
