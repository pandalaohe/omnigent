"""Hourly usage through reports, ranges, attribution and owner-scoped routes."""

from dataclasses import replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.agent_quota import AgentQuotaService
from omnigent.server.routes.agent_quota import create_agent_quota_router
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.usage_hours_store import HourRow, UsageHoursStore
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore
from omnigent.usage.activity import MarkSettings, attributed_rows, error_mark, summarize
from omnigent.usage.contract import UsagePeriodView, UsageReport
from omnigent.usage.ranges import Span, range_spans
from tests.server.helpers import create_test_agent
from tests.server.integration.test_global_instructions_routes import (
    _headers,
    _make_user,
    auth_app,  # noqa: F401
)

NOW = datetime(2026, 11, 5, 12, 40, tzinfo=UTC).timestamp()
HOUR = int(NOW // 3600) * 3600
OWNER = "owner@example.test"


def row(**changes):
    return replace(
        HourRow(
            0,
            OWNER,
            "native:pi",
            HOUR,
            "kimi",
            "host",
            "metered",
            True,
            80,
            0,
            0,
            20,
            100,
            None,
            None,
            None,
            1.0,
            HOUR,
        ),
        **changes,
    )


@pytest.fixture
def service(db_uri, monkeypatch):
    monkeypatch.setattr("omnigent.stores.usage_hours_store.now_epoch", lambda: int(NOW))
    return AgentQuotaService(
        SqlAlchemyConversationStore(db_uri),
        None,
        SqlAlchemyUserPreferencesStore(db_uri),
        None,
        None,
        UsageSnapshotStore(db_uri),
        hours_store=UsageHoursStore(db_uri),
    )


def report(service, account="gateway", owner=OWNER, **changes):
    service.report_account(
        owner,
        account,
        UsageReport.model_validate(
            {
                "observed_at": NOW,
                "windows": [],
                **changes,
            }
        ),
        now=NOW,
    )


@pytest.mark.parametrize("key,days", [("today", 0), ("d7", 6), ("d30", 29), ("d90", 89)])
def test_calendar_ranges_start_at_local_midnight(key, days):
    current, previous = range_spans(key, NOW, ZoneInfo("UTC"))
    assert current.requested_from == datetime(2026, 11, 5, tzinfo=UTC).timestamp() - days * 86400
    assert not current.approx
    if key == "today":
        assert previous.until == NOW - 86400
    else:
        assert previous.until == current.counted_from


def test_cutoff_reset_custom_and_half_hour_zone():
    span, previous = range_spans("h24", NOW, ZoneInfo("UTC"))
    assert span.requested_from == NOW - 86400
    assert span.counted_from == HOUR - 86400
    assert previous.until == span.counted_from
    assert previous.counted_from == span.counted_from - 25 * 3600
    assert span.approx
    period = UsagePeriodView(start=HOUR - 7200 + 2400, days=7)
    span, previous = range_spans("period", NOW, ZoneInfo("UTC"), period=period)
    assert span.counted_from == HOUR - 7200
    assert previous.until == HOUR - 7200
    half, _ = range_spans("today", NOW, ZoneInfo("Asia/Kolkata"))
    assert half.approx and half.requested_from - half.counted_from == 1800
    custom, _ = range_spans(
        "custom", NOW, ZoneInfo("UTC"), from_date=date(2026, 11, 3), to_date=date(2026, 11, 4)
    )
    assert custom.until == datetime(2026, 11, 5, tzinfo=UTC).timestamp()


def test_dst_day_and_local_clock_previous():
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 1, 23, tzinfo=zone).timestamp()
    span, previous = range_spans("today", now, zone)
    assert span.until - span.counted_from == 24 * 3600
    assert datetime.fromtimestamp(previous.until, zone).hour == 23
    assert previous.until - previous.counted_from == 23 * 3600


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"from_date": date(2026, 11, 6), "to_date": date(2026, 11, 6)},
        {"from_date": date(2026, 11, 5), "to_date": date(2026, 11, 4)},
    ],
)
def test_custom_rejects_invalid_pairs(changes):
    with pytest.raises(ValueError):
        range_spans("custom", NOW, ZoneInfo("UTC"), **changes)


def test_attribution_requires_all_links_to_agree_and_own_activity_wins():
    accounts = {"kimi-code": {}, "linked": {"product": "pi"}}
    source = row()
    assert attributed_rows([source], accounts, {}) == {"linked": [source]}
    meters = {("pi", "kimi"): {"kimi-code"}}
    assert attributed_rows([source], accounts, meters) == {"kimi-code": [source]}
    for conflict in [{"kimi-code", None}, {"kimi-code", "linked"}]:
        assert attributed_rows([source], accounts, {("pi", "kimi"): conflict}) == {"pi": [source]}
    accounts["kimi-code"]["has_activity"] = True
    assert attributed_rows([source], accounts, meters) == {}


@pytest.mark.asyncio
async def test_real_choice_links_attribute_once_and_keep_conflicting_hours_whole(
    client, app, db_uri, monkeypatch
):
    monkeypatch.setattr("omnigent.stores.usage_hours_store.now_epoch", lambda: int(NOW))
    agent = await create_test_agent(
        client, executor={"type": "omnigent", "config": {"harness": "pi"}}
    )
    service = app.state.agent_quota_service
    service.preferences = SqlAlchemyUserPreferencesStore(db_uri)
    owner = "local"
    report(service, "linked", owner=owner, product="pi")
    report(service, "kimi-code", owner=owner)
    link = {"agent": agent["id"], "model": "kimi", "meter": "kimi-code"}
    settings = {
        "workTypes": [{"id": "work", "label": "Work"}],
        "presets": [{"id": "kimi", "name": "Kimi", "links": [link]}],
        "hosts": {"host": {"work": [[{"links": [dict(link)]}]]}},
    }

    def save_choice():
        service.preferences.replace_session_choice(
            owner, settings, service.preferences.session_choice(owner)["revision"]
        )

    save_choice()
    service.hours_store.add_turn(
        owner, "native:pi", HOUR, "kimi", "host", "metered", True, 80, 0, 0, 20, 100, 1
    )
    amounts = {item["account"]: item for item in service.activity(owner, now=NOW)["accounts"]}
    assert amounts["kimi-code"]["totals"]["cost_usd"] == 1
    assert amounts["linked"]["totals"]["cost_usd"] is None
    assert "pi" not in amounts
    settings["hosts"]["host"]["work"][0][0]["links"][0]["meter"] = None
    save_choice()
    amounts = {item["account"]: item for item in service.activity(owner, now=NOW)["accounts"]}
    assert amounts["pi"]["totals"]["cost_usd"] == 1
    assert amounts["kimi-code"]["totals"]["cost_usd"] is None
    settings["hosts"]["host"]["work"][0][0]["links"][0]["meter"] = "kimi-code"
    save_choice()
    report(service, "kimi-code", owner=owner, activity=[])
    report(service, "kimi-code", owner=owner, observed_at=NOW + 1)
    amounts = service.activity(owner, now=NOW + 1)["accounts"]
    assert all(item["totals"]["cost_usd"] is None for item in amounts)


def test_marks_coverage_and_hour_boundaries():
    span = Span.starting(HOUR - 3600 + 30, NOW)
    rows = [row(), row(total_tokens=0, cost_usd=3), row(priced=False, cost_usd=0)]
    value = summarize(rows, span, ZoneInfo("UTC"))
    assert value["totals"]["cost_usd"] == 4
    assert value["totals"]["tokens"] == 200
    assert value["totals"]["per_million"] == 10000
    assert value["totals"]["cost_coverage_pct"] == 25
    assert value["totals"]["covered_tokens"] == 100
    assert value["totals"]["covered_cost_usd"] == 1
    assert value["mark"] == "≥"
    assert summarize([row(basis="subscription")], span, ZoneInfo("UTC"))["mark"] == "~"
    assert summarize([row()], span, ZoneInfo("UTC"))["mark"] == "≈"


def test_metered_projection_keeps_coverage_and_estimates_separate():
    rows = [
        row(total_tokens=1000, cost_usd=2),
        row(basis="subscription", total_tokens=9000, cost_usd=90),
        row(total_tokens=0, cost_usd=3),
        row(total_tokens=500, cost_usd=0, priced=False),
    ]
    result = summarize(rows, Span.starting(HOUR, NOW), ZoneInfo("UTC"))
    assert result["mark"] == "~"
    assert result["totals"]["cost_usd"] == 95
    metered = result["totals"]["metered"]
    assert metered["cost_usd"] == 5
    assert metered["covered_cost_usd"] == 2
    assert metered["covered_tokens"] == 1000
    assert metered["per_million"] == 2000
    assert metered["cost_coverage_pct"] == 40
    assert metered["tokens"] == 1500
    assert metered["unpriced"] == 1
    assert result["series"][0]["metered"] == metered


@pytest.mark.parametrize("failed,level", [(1, "notice"), (4, "serious"), (10, "down"), (0, None)])
def test_error_thresholds_and_expiry(failed, level):
    rows = [row(requests=20, failed=failed)]
    mark = error_mark(rows, NOW, True, MarkSettings())
    assert (mark["level"] if mark else None) == level
    assert error_mark(rows, NOW, False, MarkSettings()) is None
    assert error_mark(rows, HOUR + 7200, True, MarkSettings()) is None
    assert error_mark([row(requests=19, failed=10)], NOW, True, MarkSettings()) is None
    assert error_mark([row()], NOW, True, MarkSettings()) is None


def test_report_replaces_activity_keeps_ownership_and_reports_issues(service):
    activity = [
        {
            "hour": HOUR,
            "model": "m",
            "requests": 20,
            "failed": 4,
            "reasons": {"timeout": 4},
            "tokens": 100,
            "cost_usd": 2,
        }
    ]
    report(service, activity=activity)
    activity[0]["cost_usd"] = 3
    report(service, activity=activity)
    report(service, owner="other@example.test", activity=[{**activity[0], "cost_usd": 99}])
    item = next(item for item in service.activity(OWNER, account="gateway", now=NOW)["accounts"])
    assert item["totals"]["cost_usd"] == 3
    assert item["totals"]["requests"] == 20
    assert item["error_mark"]["level"] == "serious"
    issues = service.issues(OWNER, now=NOW, include_models=True)
    assert issues["failures"][0]["reasons"] == {"timeout": 4}
    assert service.usage_issues(OWNER, now=NOW)[0]["what"] == "failures"
    assert service.usage_issues(OWNER, now=HOUR + 7200)[0]["what"] == "silent"
    service.flush(now=NOW, force=True)
    restored = AgentQuotaService(
        service.conversations,
        None,
        service.preferences,
        None,
        None,
        service.snapshot_store,
        hours_store=service.hours_store,
    )
    assert (
        restored.activity(OWNER, now=NOW, account="gateway")["accounts"][0]["totals"]["cost_usd"]
        == 3
    )


def test_unavailable_and_rejected_source_log_is_bounded_and_resolved(service):
    report(service)
    report(service, observed_at=NOW + 1, status="unavailable")
    assert service.usage_issues(OWNER, now=NOW + 2)[0]["what"] == "read_failed"
    for index in range(201):
        service.note_rejected(OWNER, "gateway", now=NOW + 2 + index)
    sources = service.issues(OWNER, now=NOW + 300)["source_reports"]
    assert len(sources) == 200
    assert all(source["kind"] == "rejected" for source in sources)
    report(service, observed_at=NOW + 400)
    assert service.usage_issues(OWNER, now=NOW + 401) == []


def test_silent_source_follows_the_requested_custom_range(service):
    read_at = NOW - 86400
    report(service, observed_at=read_at)
    past = service.issues(
        OWNER,
        now=NOW,
        range_key="custom",
        from_date=date(2026, 10, 29),
        to_date=date(2026, 10, 29),
    )
    assert past["source_reports"] == []
    reported_day = service.issues(
        OWNER,
        now=NOW,
        range_key="custom",
        from_date=date(2026, 11, 4),
        to_date=date(2026, 11, 4),
    )
    assert reported_day["source_reports"] == [
        {"account": "gateway", "kind": "silent", "at": read_at}
    ]


@pytest.mark.parametrize("range_key", ["d7", "custom"])
def test_current_silence_older_than_the_range_remains_visible(service, range_key):
    read_at = datetime(2026, 10, 28, 12, 40, tzinfo=UTC).timestamp()
    report(service, observed_at=read_at)
    options = (
        {"from_date": date(2026, 11, 5), "to_date": date(2026, 11, 5)}
        if range_key == "custom"
        else {}
    )
    current = service.issues(OWNER, now=NOW, range_key=range_key, **options)
    assert current["source_reports"] == [{"account": "gateway", "kind": "silent", "at": read_at}]
    assert service.usage_issues(OWNER, now=NOW) == [
        {"account": "gateway", "level": "notice", "what": "silent", "last_at": read_at}
    ]


def test_store_failure_keeps_readings_and_advisory_payload_available(service, monkeypatch):
    report(service, product="codex")
    conversation = service.conversations.create_conversation()

    def failed(*args, **kwargs):
        raise RuntimeError("unavailable test store")

    monkeypatch.setattr(service.hours_store, "read", failed)
    assert service.overview(OWNER, now=NOW).accounts
    assert "usage_issues" not in service.payload(conversation, requester_id="local")


def test_future_old_rows_are_dropped_and_hours_are_normalized(service):
    report(
        service,
        activity=[
            {"hour": HOUR + 1, "tokens": 100},
            {"hour": int(NOW) + 1, "tokens": 200},
            {"hour": HOUR - 401 * 86400, "tokens": 300},
        ],
    )
    value = service.activity(OWNER, account="gateway", range_key="h24", now=NOW)["accounts"][0]
    assert value["totals"]["tokens"] == 100
    assert value["series"][0]["at"] == HOUR


@pytest.mark.asyncio
async def test_authenticated_routes_isolate_amounts_and_open_issues(request, db_uri, monkeypatch):
    app = request.getfixturevalue("auth_app")
    service = app.state.agent_quota_service
    _make_user(db_uri, OWNER, is_admin=False)
    _make_user(db_uri, "other@example.test", is_admin=False)
    monkeypatch.setattr("omnigent.server.agent_quota.time.time", lambda: NOW)
    monkeypatch.setattr("omnigent.stores.usage_hours_store.now_epoch", lambda: int(NOW))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {
            "observed_at": NOW,
            "windows": [],
            "activity": [
                {"hour": HOUR, "model": "unpriced", "tokens": 100},
            ],
        }
        pushed = await client.put(
            "/v1/me/usage/accounts/gateway", json=body, headers=_headers(OWNER)
        )
        assert pushed.status_code == 204
        for path in ("/v1/me/usage/activity", "/v1/me/usage/issues"):
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers=_headers(OWNER))).status_code == 200
        mine = await client.get("/v1/me/usage/activity", headers=_headers(OWNER))
        assert (
            next(item for item in mine.json()["accounts"] if item["account"] == "gateway")[
                "totals"
            ]["tokens"]
            == 100
        )
        other = await client.get("/v1/me/usage/activity", headers=_headers("other@example.test"))
        assert all(item["account"] != "gateway" for item in other.json()["accounts"])
        issues = await client.get("/v1/me/usage/issues", headers=_headers(OWNER))
        assert issues.json()["unpriced_models"][0]["model"] == "unpriced"
        other_issues = await client.get(
            "/v1/me/usage/issues", headers=_headers("other@example.test")
        )
        assert other_issues.json() == {"failures": [], "unpriced_models": [], "source_reports": []}
        root = service.conversations.create_conversation()
        service.permissions.grant(OWNER, root.id, 4)
        service.permissions.grant("other@example.test", root.id, 1)
        quota = await client.get(f"/v1/sessions/{root.id}/agent-quota", headers=_headers(OWNER))
        assert quota.json()["usage_issues"][0]["what"] == "unpriced"
        shared_quota = await client.get(
            f"/v1/sessions/{root.id}/agent-quota", headers=_headers("other@example.test")
        )
        assert shared_quota.status_code == 200
        assert "usage_issues" not in shared_quota.json()
        service.usage_enabled = False
        quota = await client.get(f"/v1/sessions/{root.id}/agent-quota", headers=_headers(OWNER))
        assert "usage_issues" not in quota.json()


@pytest.mark.asyncio
async def test_routes_validate_dates_zones_scope_and_deployment_gate(service, monkeypatch):
    monkeypatch.setattr("omnigent.server.agent_quota.time.time", lambda: NOW)
    report(service, owner="local", activity=[{"hour": HOUR, "tokens": 100, "cost_usd": 1}])
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=None, host_store=None), prefix="/v1"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/me/usage/activity", params={"account": "gateway"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json()["accounts"][0]["totals"]["cost_usd"] == 1
        for params in [
            {"tz": "unknown"},
            {"range": "wrong"},
            {"range": "custom"},
            {"range": "custom", "from": "2026-11-06", "to": "2026-11-06"},
        ]:
            assert (await client.get("/v1/me/usage/activity", params=params)).status_code == 422
        assert (await client.get("/v1/me/usage/issues")).status_code == 200
        service.usage_enabled = False
        assert (await client.get("/v1/me/usage/activity")).status_code == 404
        assert (await client.get("/v1/me/usage/issues")).status_code == 404
