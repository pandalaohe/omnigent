"""Quota reports stay owner/host/tenant scoped and use current deciding periods."""

import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.server.agent_quota import AgentQuotaService, QuotaContext, UsageAccountLimitError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes.agent_quota import create_agent_quota_router
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore
from omnigent.usage.contract import UsageReport, UsageReportV2
from tests.server.integration.test_global_instructions_routes import (
    _headers,
    _make_user,
    auth_app,  # noqa: F401
)

NOW = 2_000_000
OWNER = "owner@example.test"
HOST = "a" * 32


def snapshot(now=NOW, used=85, period="5h"):
    return {
        "captured_at": now,
        "windows": [
            {"label": period, "used_percent": used, "duration_mins": 300, "resets_at": now + 10800}
        ],
    }


def usage_report(*, account_product=None, windows=None, **changes):
    payload = {
        "schema_version": 1,
        "observed_at": NOW,
        "status": "available",
        "windows": windows
        or [
            {
                "id": "weekly",
                "kind": "weekly",
                "percent_used": 40,
                "window_minutes": 10_080,
                "resets_at": NOW + 4 * 86400,
            }
        ],
        **changes,
    }
    if account_product is not None:
        payload["product"] = account_product
    return UsageReport.model_validate(payload)


def service_with_store(db_uri, store=None):
    return AgentQuotaService(
        SqlAlchemyConversationStore(db_uri),
        None,
        SqlAlchemyUserPreferencesStore(db_uri),
        None,
        HostStore(db_uri),
        store if store is not None else UsageSnapshotStore(db_uri),
    )


@pytest.mark.parametrize("usage_enabled", [True, False])
def test_pool_readings_use_tile_not_latest_product_projection(db_uri, usage_enabled):
    service = service_with_store(db_uri)
    service.usage_enabled = usage_enabled
    service.report_account(OWNER, "a", usage_report(account_product="codex"), now=NOW)
    service.report_account(
        OWNER,
        "b",
        usage_report(
            account_product="codex",
            windows=[
                {
                    "id": "weekly",
                    "kind": "weekly",
                    "percent_used": 100,
                    "window_minutes": 10080,
                    "resets_at": NOW + 86400,
                }
            ],
        ),
        now=NOW,
    )
    readings = service.pool_readings(QuotaContext(OWNER, HOST), now=NOW)
    assert readings["a"].windows[0].remaining_pct == 60
    assert readings["b"].windows[0].remaining_pct == 0
    assert readings["a"].windows[0].resets_at == NOW + 4 * 86400
    assert (
        service.pool_readings(QuotaContext("other", HOST), now=NOW).keys().isdisjoint({"a", "b"})
    )
    assert service.pool_readings(QuotaContext(OWNER, HOST), now=NOW + 3601)["a"].freshness == "old"


@pytest.mark.parametrize("report_host", [None, HOST])
@pytest.mark.parametrize("read_host", [None, HOST])
def test_pool_readings_with_board_disabled_use_reported_products(service, report_host, read_host):
    service.usage_enabled = False
    service.report(OWNER, report_host, "claude", snapshot(), now=NOW)
    readings = service.pool_readings(QuotaContext(OWNER, read_host), now=NOW)
    assert "claude" in readings
    assert readings["claude"].windows[0].remaining_pct == 15
    assert (
        service.pool_readings(QuotaContext("other", read_host), now=NOW)["claude"].freshness
        == "none"
    )


def test_discovery_lists_pools_for_exclusion_without_duplicate_or_extra_prompt(service):
    conv = service.conversations.create_conversation(host_id=HOST, workspace="/opt/work/project")
    owner = service.context(conv).owner_id
    settings = {
        "enabled": True,
        "workTypes": [{"id": "work", "label": "Work"}],
        "presets": [
            {"id": "code", "name": "Code", "links": [{"agent": "codex-native", "meter": "plan"}]}
        ],
        "hosts": {
            HOST: {
                "work": [
                    [
                        {
                            "links": [
                                {"agent": "codex-native", "meter": "plan"},
                                {"agent": "pi", "model": "kimi"},
                            ]
                        }
                    ]
                ]
            }
        },
    }
    service.preferences.replace_session_choice(
        owner, settings, service.preferences.session_choice(owner)["revision"]
    )
    assert service.payload(conv)["session_choice"] == {
        "enabled": True,
        "work_types": ["work"],
        "pools": ["plan", "pi:kimi"],
    }


@pytest.fixture
def service(db_uri):
    return AgentQuotaService(
        SqlAlchemyConversationStore(db_uri),
        None,
        SqlAlchemyUserPreferencesStore(db_uri),
        None,
        HostStore(db_uri),
    )


def test_newest_fresh_host_or_owner_report_and_recovery(service):
    context = QuotaContext(OWNER, HOST)
    service.report(OWNER, HOST, "opencode", snapshot(used=100), now=NOW)
    service.report(OWNER, None, "opencode", snapshot(NOW + 1), now=NOW)
    assert service.quotas(context, now=NOW)[1]["opencode"].state == "tight"
    assert not service.report(OWNER, HOST, "opencode", snapshot(NOW + 301), now=NOW)
    assert service.report(OWNER, HOST, "opencode", snapshot(NOW + 2, used=0), now=NOW)
    assert service.quotas(context, now=NOW)[1]["opencode"].state == "ok"
    assert service.quotas(QuotaContext("someone-else", HOST), now=NOW)[1] == {}
    assert service.quotas(context, now=NOW + 3603)[1]["opencode"].state == "unknown"
    service.report("another-owner", None, "claude", snapshot(NOW + 3604), now=NOW + 3604)
    assert service.quotas(context, now=NOW + 3604)[1]["opencode"].state == "unknown"


@pytest.mark.parametrize("product", ["opencode", "claude"])
def test_external_period_ids_are_preserved(service, product):
    report = {**snapshot(used=100, period="w"), "pinned_period": "w"}
    service.report(OWNER, None, product, report, now=NOW)
    quota = service.quotas(QuotaContext(OWNER, HOST), now=NOW)[1][product]
    assert quota.state == "exhausted"
    assert quota.deciding_window == "w"


def test_native_source_uses_authenticated_tunnel_owner(service):
    service.permissions = SqlAlchemyPermissionStore(service.conversations.storage_location)
    native = {
        "captured_at": NOW,
        "limits": [
            {
                "limit_id": "codex",
                "windows": [
                    {
                        "kind": "primary",
                        "used_percent": 100,
                        "window_duration_mins": 300,
                        "resets_at": NOW + 100,
                    }
                ],
            }
        ],
    }
    service.host_registry = SimpleNamespace(
        get=lambda _: SimpleNamespace(owner=OWNER, hello=SimpleNamespace(codex_rate_limits=native))
    )
    assert service.quotas(QuotaContext(OWNER, HOST), now=NOW)[1]["codex"].state == "exhausted"
    assert service.quotas(QuotaContext("another-owner", HOST), now=NOW)[1] == {}


def test_cache_does_not_cross_workspaces(service):
    service.report(OWNER, None, "opencode", snapshot(), now=NOW)
    with workspace_scope(42):
        assert service.quotas(QuotaContext(OWNER, HOST), now=NOW)[1] == {}


@pytest.mark.parametrize(
    ("report_kind", "reason"),
    [
        ("missing", "no_report"),
        ("fresh", None),
        ("stale", "stale_report"),
        ("empty", "empty_report"),
    ],
)
def test_external_quota_payload_explains_unknown_reports(
    service, monkeypatch, report_kind, reason
):
    now = time.time()
    conv = service.conversations.create_conversation(harness_override="antigravity-native")
    context = service.context(conv)
    if report_kind != "missing":
        report = snapshot(now)
        assert service.report(context.owner_id, None, "antigravity", report, now=now)
        if report_kind == "empty":
            # An explicit retraction replaces the previously usable report.
            assert service.report(
                context.owner_id,
                None,
                "antigravity",
                {"captured_at": now + 1, "windows": []},
                now=now,
            )
        elif report_kind == "stale":
            monkeypatch.setattr(time, "time", lambda: now + 3601)
    quota = service.payload(conv)["agents"]["antigravity"]
    assert quota["state"] == ("tight" if report_kind == "fresh" else "unknown")
    assert quota["reason"] == reason
    if report_kind != "fresh":
        assert quota["pace_pct"] is None
        assert quota["deciding_window"] is None
        assert not quota["windows"]
    if report_kind == "missing":
        assert service.context_line(context) == ""


@pytest.mark.parametrize("foreign_scope", ["owner", "host", "workspace"])
def test_missing_report_projection_does_not_expose_other_scopes(service, foreign_scope):
    now = time.time()
    conv = service.conversations.create_conversation(host_id=HOST, workspace="/opt/work/project")
    context = service.context(conv)
    owner = "another-owner" if foreign_scope == "owner" else context.owner_id
    host = "another-host" if foreign_scope == "host" else None
    with workspace_scope(42 if foreign_scope == "workspace" else 0):
        assert service.report(owner, host, "antigravity", snapshot(now, used=100), now=now)
    quota = service.payload(conv)["agents"]["antigravity"]
    assert quota["state"] == "unknown"
    assert quota["reason"] == "no_report"
    assert not quota["windows"]


def test_pinned_and_user_selected_period_keep_other_evidence(service):
    report = snapshot()
    report["windows"].append(
        {
            "label": "month",
            "used_percent": 0,
            "duration_mins": 43200,
            "resets_at": NOW + 30 * 86400,
        }
    )
    report["pinned_period"] = "month"
    service.report(OWNER, None, "opencode", report, now=NOW)
    context = QuotaContext(OWNER, HOST)
    assert service.quotas(context, now=NOW)[1]["opencode"].deciding_window == "month"
    service.preferences.patch_namespace(
        OWNER, "session_collab", {"quotaPeriods": {"opencode": "5h"}}
    )
    quota = service.quotas(context, now=NOW)[1]["opencode"]
    assert quota.state == "tight"
    assert quota.deciding_window == "5h"
    assert len(quota.windows) == 2


def test_product_exception_then_default_then_pinned_then_tightest(service):
    report = snapshot(used=85)
    report["windows"].append(
        {
            "label": "month",
            "used_percent": 0,
            "duration_mins": 43200,
            "resets_at": NOW + 30 * 86400,
        }
    )
    report["pinned_period"] = "month"
    context = QuotaContext(OWNER, HOST)
    for product in ("codex", "claude", "opencode"):
        assert service.report(OWNER, None, product, report, now=NOW)
    service.preferences.patch_namespace(
        OWNER,
        "session_collab",
        {"quotaPeriodDefault": "5h", "quotaPeriods": {"codex": "month"}},
    )
    quotas = service.quotas(context, now=NOW)[1]
    assert quotas["codex"].deciding_window == "month"
    assert quotas["claude"].deciding_window == "5h"

    service.preferences.patch_namespace(
        OWNER, "session_collab", {"quotaPeriodDefault": None, "quotaPeriods": {}}
    )
    assert service.quotas(context, now=NOW)[1]["opencode"].deciding_window == "month"
    unpinned = {key: value for key, value in report.items() if key != "pinned_period"}
    assert service.report(OWNER, None, "opencode", {**unpinned, "captured_at": NOW + 1}, now=NOW)
    assert service.quotas(context, now=NOW)[1]["opencode"].deciding_window == "5h"


def test_claude_capture_uses_ancestor_owner_and_inherited_host(service, db_uri):
    _make_user(db_uri, OWNER, is_admin=False)
    root = service.conversations.create_conversation(host_id=HOST, workspace="/opt/work/project")
    child = service.conversations.create_conversation(parent_conversation_id=root.id)
    service.permissions = SqlAlchemyPermissionStore(db_uri)
    service.permissions.grant(OWNER, root.id, 4)
    context = service.context(child)
    assert context == QuotaContext(OWNER, HOST)
    report = snapshot(int(time.time()))
    service.report_claude(child, {**report, "provider": "Claude"})
    assert service.quotas(context)[1]["claude"].state == "tight"
    newborn = service.conversations.create_conversation()
    assert service.context(newborn, OWNER).owner_id == OWNER


def test_native_codex_competes_with_external_report(service):
    service.host_store.upsert_on_connect(HOST, "Host", OWNER)
    native = {
        "captured_at": NOW,
        "limits": [
            {
                "limit_id": "codex",
                "windows": [
                    {
                        "kind": "primary",
                        "used_percent": 100,
                        "window_duration_mins": 300,
                        "resets_at": NOW + 100,
                    }
                ],
            }
        ],
    }
    service.host_registry = SimpleNamespace(
        get=lambda _: SimpleNamespace(hello=SimpleNamespace(codex_rate_limits=native))
    )
    context = QuotaContext(OWNER, HOST)
    assert service.quotas(context, now=NOW)[1]["codex"].state == "exhausted"
    service.report(OWNER, None, "codex", snapshot(NOW + 1, used=0), now=NOW)
    assert service.quotas(context, now=NOW)[1]["codex"].state == "ok"


def test_pi_limits_stay_separate_from_claude_and_codex(service):
    conv = service.conversations.create_conversation(harness_override="pi")
    report = {**snapshot(int(time.time()), used=100), "provider": "Anthropic API"}
    service.report_pi(conv, report)
    quotas = service.quotas(service.context(conv))[1]
    assert set(quotas) == {"pi"}
    assert quotas["pi"].state == "exhausted"


@pytest.mark.asyncio
async def test_report_and_session_read_auth_and_bounds(request, db_uri):
    app = request.getfixturevalue("auth_app")
    service = app.state.agent_quota_service
    _make_user(db_uri, OWNER, is_admin=False)
    other = "other@example.test"
    _make_user(db_uri, other, is_admin=False)
    service.host_store.upsert_on_connect(HOST, "Host", OWNER)
    root = service.conversations.create_conversation(host_id=HOST, workspace="/opt/work/project")
    service.permissions.grant(OWNER, root.id, 4)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/v1/me/agent-quota/opencode"
        missing = await client.get(f"/v1/sessions/{root.id}/agent-quota", headers=_headers(OWNER))
        assert missing.status_code == 200
        assert missing.json()["agents"]["antigravity"] == {
            "state": "unknown",
            "reason": "no_report",
            "pace_pct": None,
            "deciding_window": None,
            "windows": [],
        }
        report = {**snapshot(int(time.time())), "host_id": HOST}
        assert (await client.put(path, json=report)).status_code == 401
        assert (await client.put(path, json=report, headers=_headers(other))).status_code == 404
        assert (await client.put(path, json=report, headers=_headers(OWNER))).status_code == 204
        result = await client.get(f"/v1/sessions/{root.id}/agent-quota", headers=_headers(OWNER))
        assert result.status_code == 200
        quota = result.json()["agents"]["opencode"]
        assert quota["state"] == "tight"
        assert quota["windows"][0]["remaining_pct"] == 15
        assert (
            await client.get(f"/v1/sessions/{root.id}/agent-quota", headers=_headers(other))
        ).status_code == 404
        for bad in (
            {"captured_at": time.time() + 301},
            {"windows": report["windows"] * 17},
            {"captured_at": True},
            {"pinned_period": "missing"},
        ):
            response = await client.put(path, json={**report, **bad}, headers=_headers(OWNER))
            assert response.status_code == 422, response.text
        assert (
            await client.put(
                "/v1/me/agent-quota/unknown-product", json=report, headers=_headers(OWNER)
            )
        ).status_code == 422


def account(overview, account_id):
    return next(item for item in overview.accounts if item.id == account_id)


def test_reported_models_are_account_display_suggestions(service):
    service.report_account(
        OWNER, "plan", usage_report(models=[{"id": "model-a"}, {"id": "model-b"}]), now=NOW
    )
    tile = account(service.overview(OWNER, now=NOW), "plan")
    assert [model.id for model in tile.models] == ["model-a", "model-b"]
    assert "plan" not in {tile.id for tile in service.overview("other", now=NOW).accounts}


def test_push_value_and_percent_windows_stay_out_of_agents(service):
    report = usage_report(
        windows=[
            {
                "id": "spend",
                "percent_used": 41,
                "used_value": 41,
                "limit_value": 100,
                "unit": "USD",
            }
        ]
    )
    service.report_account(OWNER, "gw", report, now=NOW)

    overview = service.overview(OWNER, now=NOW)
    tile = account(overview, "gw")
    assert tile.windows[0].percent_used == 41
    assert tile.windows[0].used_value == 41
    assert tile.windows[0].limit_value == 100
    assert overview.agents["codex"]["reason"] == "no_report"


def test_linked_account_replaces_native_tile_and_projects_to_routing(service):
    service.report(OWNER, None, "codex", snapshot(), now=NOW)
    service.report_account(OWNER, "gw", usage_report(account_product="codex"), now=NOW)

    overview = service.overview(OWNER, now=NOW)
    assert "codex" not in {item.id for item in overview.accounts if item.kind == "native"}
    assert account(overview, "gw").product == "codex"
    assert overview.agents["codex"]["state"] == "ok"


def test_v2_pin_only_change_updates_routing_without_refreshing_windows(service):
    service.report_account(
        OWNER,
        "gw",
        UsageReportV2.model_validate(
            {
                "schema_version": 2,
                "observed_at": NOW,
                "fresh_until": NOW + 60,
                "product": "codex",
                "pinned_window": "short",
                "windows": [
                    {
                        "id": "short",
                        "kind": "session",
                        "percent_used": 85,
                        "window_minutes": 300,
                        "resets_at": NOW + 10800,
                    },
                    {
                        "id": "weekly",
                        "kind": "weekly",
                        "percent_used": 40,
                        "window_minutes": 10080,
                        "resets_at": NOW + 4 * 86400,
                    },
                ],
            }
        ),
        now=NOW,
    )
    service.report_account(
        OWNER,
        "gw",
        UsageReportV2.model_validate(
            {
                "schema_version": 2,
                "observed_at": NOW + 30,
                "pinned_window": "weekly",
            }
        ),
        now=NOW + 30,
    )
    overview = service.overview(OWNER, now=NOW + 30)
    assert account(overview, "gw").deciding_window == "weekly"
    assert account(overview, "gw").read_at == NOW
    assert overview.agents["codex"]["deciding_window"] == "weekly"
    assert account(service.overview(OWNER, now=NOW + 61), "gw").freshness == "old"
    assert service.overview(OWNER, now=NOW + 61).agents["codex"]["state"] == "unknown"


def test_successful_read_removes_missing_window_but_keeps_pin_for_its_return(service):
    windows = [
        {
            "id": "five_hour",
            "kind": "session",
            "percent_used": 85,
            "window_minutes": 300,
            "resets_at": NOW + 10800,
        },
        {
            "id": "weekly",
            "kind": "weekly",
            "percent_used": 40,
            "window_minutes": 10_080,
            "resets_at": NOW + 4 * 86400,
        },
    ]
    service.report_account(
        OWNER, "gw", usage_report(windows=windows, pinned_window="five_hour"), now=NOW
    )
    service.report_account(OWNER, "gw", usage_report(windows=windows[1:]), now=NOW + 1)

    tile = account(service.overview(OWNER, now=NOW + 1), "gw")
    assert [window.id for window in tile.windows] == ["weekly"]
    assert tile.state == "unknown"

    service.report_account(OWNER, "gw", usage_report(windows=windows), now=NOW + 2)
    tile = account(service.overview(OWNER, now=NOW + 2), "gw")
    assert {window.id for window in tile.windows} == {"five_hour", "weekly"}
    assert tile.deciding_window == "five_hour"


def test_failed_read_keeps_last_rings_dim_and_withdraws_linked_routing(service):
    service.report_account(OWNER, "codex", usage_report(), now=NOW)
    service.report_account(
        OWNER,
        "codex",
        usage_report(observed_at=NOW + 10, status="unavailable", fresh_until=NOW, windows=[]),
        now=NOW + 10,
    )

    overview = service.overview(OWNER, now=NOW + 10)
    tile = account(overview, "codex")
    assert tile.freshness == "old"
    assert tile.read_at == NOW
    assert tile.windows[0].percent_used == 40
    assert tile.windows[0].pace_pct is None
    assert overview.agents["codex"]["reason"] == "empty_report"


def test_host_linked_account_enters_session_less_overview(service):
    service.report_account(
        OWNER,
        "gw",
        usage_report(account_product="codex", host_id=HOST),
        now=NOW,
    )
    overview = service.overview(OWNER, now=NOW)
    assert overview.agents["codex"]["state"] == "ok"
    assert account(overview, "gw").hosts[0].host_id == HOST


def test_window_ids_drive_pins_and_duplicate_labels_keep_their_own_pace(service):
    windows = [
        {
            "id": "fast",
            "label": "days",
            "percent_used": 40,
            "window_minutes": 10_080,
            "resets_at": NOW + 4 * 86400,
        },
        {
            "id": "slow",
            "label": "days",
            "percent_used": 90,
            "window_minutes": 300,
            "resets_at": NOW + 1000,
        },
    ]
    service.report_account(
        OWNER,
        "gw",
        usage_report(account_product="codex", windows=windows, pinned_window="slow"),
        now=NOW,
    )
    tile = account(service.overview(OWNER, now=NOW), "gw")
    assert tile.deciding_window == "slow"
    assert service.quotas(QuotaContext(OWNER, None), now=NOW)[1]["codex"].deciding_window == "slow"
    views = {window.id: window for window in tile.windows}
    assert views["fast"].pace_pct != views["slow"].pace_pct
    assert views["fast"].label == views["slow"].label == "days"


def test_deleting_one_account_keeps_other_sources_projection(service):
    service.report_account(OWNER, "a", usage_report(account_product="codex"), now=NOW)
    service.report_account(
        OWNER, "b", usage_report(account_product="codex", observed_at=NOW + 1), now=NOW + 1
    )
    service.delete_account(OWNER, "a")
    assert service.quotas(QuotaContext(OWNER, None), now=NOW + 1)[1]["codex"].state == "ok"

    service.report_account(OWNER, "a", usage_report(account_product="codex"), now=NOW + 2)
    service.delete_account(OWNER, "b")
    quota = service.quotas(QuotaContext(OWNER, None), now=NOW + 2)[1]["codex"]
    assert quota.state == "ok"


def test_unavailable_without_identity_withdraws_its_previous_projection(service):
    service.report_account(
        OWNER, "gw", usage_report(account_product="codex", host_id=HOST), now=NOW
    )
    service.report_account(
        OWNER,
        "gw",
        usage_report(observed_at=NOW + 1, status="unavailable", windows=[]),
        now=NOW + 1,
    )
    quota = service.quotas(QuotaContext(OWNER, HOST), now=NOW + 1)[1]["codex"]
    assert quota.state == "unknown"
    assert quota.reason == "empty_report"


def test_linked_account_includes_native_host_readings(service):
    service.report(OWNER, HOST, "claude", snapshot(), now=NOW)
    service.report_account(OWNER, "gw", usage_report(account_product="claude"), now=NOW)
    tile = account(service.overview(OWNER, now=NOW), "gw")
    assert [host.host_id for host in tile.hosts] == [HOST]
    assert tile.hosts[0].windows[0].percent_used == 85


@pytest.mark.parametrize("native_host", [HOST, "b" * 32])
def test_linked_account_uses_the_newest_reading_per_host(service, native_host):
    report = usage_report(account_product="claude", host_id=HOST)
    service.report_account(OWNER, "claude", report, now=NOW)
    later = NOW + 3601
    service.report(OWNER, native_host, "claude", snapshot(later, used=58), now=later)

    tile = account(service.overview(OWNER, now=later), "claude")
    assert tile.windows[0].percent_used == 58
    assert tile.read_at == later
    assert tile.freshness == "fresh"
    hosts = {host.host_id: host for host in tile.hosts}
    assert len(tile.hosts) == len({HOST, native_host})
    assert hosts[native_host].windows[0].percent_used == 58
    assert hosts[native_host].read_at == later
    if native_host != HOST:
        assert hosts[HOST].windows[0].percent_used == 40
        assert hosts[HOST].read_at == NOW

    service.report_account(
        OWNER, "claude", report.model_copy(update={"observed_at": later + 1}), now=later + 1
    )
    tile = account(service.overview(OWNER, now=later + 1), "claude")
    assert tile.windows[0].percent_used == 40
    assert tile.read_at == later + 1
    hosts = {host.host_id: host for host in tile.hosts}
    assert len(tile.hosts) == len({HOST, native_host})
    assert hosts[HOST].windows[0].percent_used == 40
    assert hosts[HOST].read_at == later + 1
    if native_host != HOST:
        assert hosts[native_host].windows[0].percent_used == 58
        assert hosts[native_host].read_at == later


def test_overview_includes_live_native_codex_evidence(service):
    native = {
        "captured_at": NOW,
        "limits": [
            {
                "limit_id": "codex",
                "windows": [
                    {
                        "kind": "primary",
                        "used_percent": 25,
                        "window_duration_mins": 300,
                        "resets_at": NOW + 1000,
                    }
                ],
            }
        ],
    }
    conn = SimpleNamespace(owner=OWNER, hello=SimpleNamespace(codex_rate_limits=native))
    service.host_registry = SimpleNamespace(
        online_host_ids=lambda workspace_id: [HOST],
        get=lambda host_id, workspace_id=None: conn,
    )
    service.note_reading(OWNER, HOST, "codex", native, now=NOW)
    overview = service.overview(OWNER, now=NOW)
    assert overview.agents["codex"]["state"] == "ok"
    assert account(overview, "codex").windows[0].percent_used == 25


def test_old_empty_report_also_keeps_display_but_not_routing(service):
    service.report(OWNER, None, "claude", snapshot(), now=NOW)
    service.report(OWNER, None, "claude", {"captured_at": NOW + 1, "windows": []}, now=NOW + 1)

    tile = account(service.overview(OWNER, now=NOW + 1), "claude")
    assert tile.freshness == "old"
    assert tile.windows[0].percent_used == 85
    quota = service.quotas(QuotaContext(OWNER, None), now=NOW + 1)[1]["claude"]
    assert quota.reason == "empty_report"


def test_usage_off_allows_explicit_empty_claude_retraction(service):
    service.usage_enabled = False
    conversation = service.conversations.create_conversation()
    now = time.time()
    good = {**snapshot(now), "provider": "Claude"}
    service.report_claude(conversation, good)
    assert service.quotas(QuotaContext(RESERVED_USER_LOCAL, None))[1]["claude"].state == "tight"

    service.report_claude(
        conversation,
        {"provider": "Claude", "captured_at": now + 1, "windows": []},
    )
    quota = service.quotas(QuotaContext(RESERVED_USER_LOCAL, None), now=now + 1)[1]["claude"]
    assert quota.state == "unknown"
    assert quota.reason == "empty_report"


def test_older_attempt_cannot_replace_or_dim_a_newer_reading(service):
    service.report_account(
        OWNER, "codex", usage_report(observed_at=NOW + 20, pinned_window="weekly"), now=NOW + 20
    )
    service.report_account(
        OWNER,
        "codex",
        usage_report(observed_at=NOW + 10, status="unavailable", windows=[]),
        now=NOW + 21,
    )
    overview = service.overview(OWNER, now=NOW + 21)
    tile = account(overview, "codex")
    assert tile.freshness == "fresh"
    assert tile.read_at == NOW + 20
    assert tile.deciding_window == "weekly"
    assert overview.agents["codex"]["state"] == "ok"


def test_fresh_until_and_past_reset_are_distinct(service):
    service.report_account(OWNER, "codex", usage_report(fresh_until=NOW + 5), now=NOW)
    overview = service.overview(OWNER, now=NOW + 6)
    assert account(overview, "codex").freshness == "old"
    assert overview.agents["codex"]["reason"] == "stale_report"
    assert overview.agents["codex"]["state"] == "unknown"
    assert overview.agents["codex"]["pace_pct"] is None
    assert overview.agents["codex"]["deciding_window"] is None
    assert not overview.agents["codex"]["windows"]

    service.report_account(
        OWNER,
        "gw",
        usage_report(
            windows=[
                {
                    "id": "weekly",
                    "kind": "weekly",
                    "percent_used": 40,
                    "window_minutes": 10_080,
                    "resets_at": NOW - 1,
                }
            ]
        ),
        now=NOW,
    )
    window = account(service.overview(OWNER, now=NOW), "gw").windows[0]
    assert window.past_reset is True
    assert window.percent_used is None
    assert window.remaining_pct is None


@pytest.mark.parametrize("usage_enabled", [True, False])
def test_restart_hydrates_accounts_and_native_readings(db_uri, usage_enabled):
    store = UsageSnapshotStore(db_uri)
    first = service_with_store(db_uri, store)
    first.usage_enabled = usage_enabled
    first.report_account(OWNER, "gw", usage_report(), now=NOW)
    first.report(OWNER, None, "claude", snapshot(), now=NOW)
    native = {
        "captured_at": NOW,
        "limits": [
            {
                "limit_id": "codex",
                "windows": [
                    {
                        "kind": "primary",
                        "used_percent": 25,
                        "window_duration_mins": 300,
                        "resets_at": NOW + 1000,
                    }
                ],
            }
        ],
    }
    first.note_reading(OWNER, HOST, "codex", native, now=NOW)
    assert first.flush(now=NOW, force=True) == 3

    second = service_with_store(db_uri, store)
    second.usage_enabled = usage_enabled
    overview = second.overview(OWNER, now=NOW)
    assert account(overview, "gw").read_at == NOW
    assert account(overview, "claude").windows[0].percent_used == 85
    assert account(overview, "codex").hosts[0].host_id == HOST
    assert second.quotas(QuotaContext(OWNER, HOST), now=NOW)[1]["claude"].state == "tight"
    assert "codex" not in second.quotas(QuotaContext(OWNER, HOST), now=NOW)[1]


def test_linked_account_restart_restores_routing_projection(db_uri):
    store = UsageSnapshotStore(db_uri)
    first = service_with_store(db_uri, store)
    first.report_account(OWNER, "gw", usage_report(account_product="codex"), now=NOW)
    first.flush(now=NOW, force=True)

    second = service_with_store(db_uri, store)
    quota = second.quotas(QuotaContext(OWNER, None), now=NOW)[1]["codex"]
    assert quota.state == "ok"
    assert account(second.overview(OWNER, now=NOW), "gw").read_at == NOW


def test_legacy_failure_restart_keeps_latest_attempt_and_last_good_rings(db_uri):
    store = UsageSnapshotStore(db_uri)
    first = service_with_store(db_uri, store)
    first.report(OWNER, None, "claude", snapshot(), now=NOW)
    first.report(OWNER, None, "claude", {"captured_at": NOW + 1, "windows": []}, now=NOW + 1)
    first.flush(now=NOW + 1, force=True)

    second = service_with_store(db_uri, store)
    quota = second.quotas(QuotaContext(OWNER, None), now=NOW + 1)[1]["claude"]
    assert quota.reason == "empty_report"
    assert quota.state == "unknown"
    tile = account(second.overview(OWNER, now=NOW + 1), "claude")
    assert tile.windows[0].percent_used == 85


def test_empty_before_first_hydrate_still_loads_good_rings(db_uri):
    store = UsageSnapshotStore(db_uri)
    first = service_with_store(db_uri, store)
    first.report(OWNER, None, "claude", snapshot(), now=NOW)
    first.flush(now=NOW, force=True)

    second = service_with_store(db_uri, store)
    second.report(OWNER, None, "claude", {"captured_at": NOW + 1, "windows": []}, now=NOW + 1)
    quota = second.quotas(QuotaContext(OWNER, None), now=NOW + 1)[1]["claude"]
    assert quota.reason == "empty_report"
    tile = account(second.overview(OWNER, now=NOW + 1), "claude")
    assert tile.windows[0].percent_used == 85

    second.flush(now=NOW + 2, force=True)
    third = service_with_store(db_uri, store)
    tile = account(third.overview(OWNER, now=NOW + 2), "claude")
    assert tile.windows[0].percent_used == 85
    assert (
        third.quotas(QuotaContext(OWNER, None), now=NOW + 2)[1]["claude"].reason == "empty_report"
    )


class MemorySnapshotStore:
    def __init__(self):
        self.records = {}

    def read(self, owner):
        return dict(self.records.get((current_workspace_id(), owner), {}))

    def write(self, owner, key, record):
        self.records.setdefault((current_workspace_id(), owner), {})[key] = record

    def delete(self, owner, key):
        self.records.get((current_workspace_id(), owner), {}).pop(key, None)


def test_write_behind_waits_for_window_change_or_five_minutes(db_uri):
    store = MemorySnapshotStore()
    service = service_with_store(db_uri, store)
    service.report_account(OWNER, "gw", usage_report(), now=NOW)
    assert service.flush(now=NOW) == 1

    service.report_account(OWNER, "gw", usage_report(observed_at=NOW + 1), now=NOW + 1)
    assert service.flush(now=NOW + 2) == 0
    assert service.flush(now=NOW + 301) == 1

    changed = usage_report(
        observed_at=NOW + 302,
        windows=[
            {
                "id": "weekly",
                "kind": "weekly",
                "percent_used": 50,
                "window_minutes": 10_080,
                "resets_at": NOW + 4 * 86400,
            }
        ],
    )
    service.report_account(OWNER, "gw", changed, now=NOW + 302)
    assert service.flush(now=NOW + 302) == 1
    assert service.delete_account(OWNER, "gw") is True
    assert service.flush(now=NOW + 303) == 1
    assert store.read(OWNER) == {}


def test_flush_binds_each_dirty_records_workspace(db_uri):
    store = MemorySnapshotStore()
    service = service_with_store(db_uri, store)
    with workspace_scope(7):
        service.report_account(OWNER, "gw", usage_report(), now=NOW)
    with workspace_scope(0):
        assert service.flush(now=NOW, force=True) == 1
    assert set(store.records) == {(7, OWNER)}


class FailingSnapshotStore:
    def __init__(self):
        self.calls = 0

    def read(self, owner):
        self.calls += 1
        raise OSError("store down")

    def write(self, owner, key, record):
        self.calls += 1
        raise OSError("store down")

    def delete(self, owner, key):
        self.calls += 1
        raise OSError("store down")


def test_status_path_never_touches_store_and_hydrate_failure_is_fail_open(db_uri):
    store = FailingSnapshotStore()
    service = service_with_store(db_uri, store)
    conversation = service.conversations.create_conversation(
        host_id=HOST, workspace="/opt/work/project"
    )
    report = {**snapshot(int(time.time())), "provider": "Claude"}
    for _ in range(1000):
        service.report_claude(conversation, report)
    assert store.calls == 0
    assert service.flush(force=True) == 0
    assert store.calls > 0

    calls = store.calls
    quota = service.quotas(QuotaContext(RESERVED_USER_LOCAL, HOST))[1]["claude"]
    assert quota.state == "tight"
    assert store.calls == calls + 1
    tile = account(service.overview(RESERVED_USER_LOCAL), "claude")
    assert tile.read_at == report["captured_at"]


def test_account_limit_is_bounded(service):
    for index in range(32):
        service.report_account(OWNER, f"gw-{index}", usage_report(), now=NOW)
    with pytest.raises(UsageAccountLimitError):
        service.report_account(OWNER, "gw-33", usage_report(), now=NOW)


def test_rejected_reports_also_respect_account_bound(service):
    for index in range(32):
        service.note_rejected(OWNER, f"bad-{index}", now=NOW)
    with pytest.raises(UsageAccountLimitError):
        service.note_rejected(OWNER, "bad-33", now=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_enabled", [True, False])
async def test_usage_routes_auth_isolation_validation_and_delete(request, db_uri, usage_enabled):
    app = request.getfixturevalue("auth_app")
    service = app.state.agent_quota_service
    service.usage_enabled = usage_enabled
    other = "other@example.test"
    _make_user(db_uri, OWNER, is_admin=False)
    _make_user(db_uri, other, is_admin=False)
    body = {
        "observed_at": time.time(),
        "windows": [
            {
                "id": "spend",
                "used_value": 41,
                "limit_value": 100,
                "unit": "USD",
                "future": "dropped",
            }
        ],
        "future": "dropped",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/v1/me/usage/accounts/gw"
        assert (await client.put(path, json=body)).status_code == 401
        assert (await client.put(path, json=body, headers=_headers(OWNER))).status_code == 204
        readings = service.pool_readings(QuotaContext(OWNER, None))
        assert readings["gw"].windows[0].used_value == 41
        assert "future" not in readings["gw"].windows[0].model_dump()
        assert "gw" not in service.pool_readings(QuotaContext(other, None))
        if usage_enabled:
            owned = await client.get("/v1/me/usage", headers=_headers(OWNER))
            assert owned.status_code == 200
            assert any(item["id"] == "gw" for item in owned.json()["accounts"])
            foreign = await client.get("/v1/me/usage", headers=_headers(other))
            assert "gw" not in {item["id"] for item in foreign.json()["accounts"]}
        assert (await client.delete(path, headers=_headers(other))).status_code == 404

        rejected = await client.put(path, json={"bad": True}, headers=_headers(OWNER))
        assert rejected.status_code == 422
        service.flush(force=True)
        stored = service.snapshot_store.read(OWNER)["usage.account.gw"]
        assert stored["report_log"][-1]["kind"] == "rejected"
        assert "bad" not in str(stored)
        assert (await client.delete(path, headers=_headers(OWNER))).status_code == 204
        assert (await client.delete(path, headers=_headers(OWNER))).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_deployment_off_keeps_quota_ingest_and_hides_usage_display(db_uri, version):
    service = service_with_store(db_uri)
    service.usage_enabled = False
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=None, host_store=None), prefix="/v1"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/me/usage")).status_code == 404
        for route in ("activity", "issues", "models?account=gw", "price-source"):
            assert (await client.get(f"/v1/me/usage/{route}")).status_code == 404
        path = "/v1/me/usage/accounts/gw"
        now = time.time()
        body = usage_report(
            observed_at=now,
            windows=[
                {
                    "id": "weekly",
                    "kind": "weekly",
                    "percent_used": 40,
                    "window_minutes": 10080,
                    "resets_at": now + 4 * 86400,
                }
            ],
        ).model_dump(mode="json")
        body["schema_version"] = version
        response = await client.put(path, json=body)
        assert response.status_code == (204 if version == 1 else 200), response.text
        service.flush(force=True)
        restarted = service_with_store(db_uri)
        restarted.usage_enabled = False
        reading = restarted.pool_readings(QuotaContext(RESERVED_USER_LOCAL, None))["gw"]
        assert reading.windows[0].remaining_pct == 60
        assert restarted.delete_account(RESERVED_USER_LOCAL, "gw")
        restarted.flush(force=True)
        assert "usage.account.gw" not in restarted.snapshot_store.read(RESERVED_USER_LOCAL)
        assert (await client.delete(path)).status_code == 204
        old = {"captured_at": time.time(), "windows": []}
        assert (await client.put("/v1/me/agent-quota/codex", json=old)).status_code == 204


def test_usage_request_openapi_references_resolve(request):
    app = request.getfixturevalue("auth_app")
    document = app.openapi()
    schema = document["paths"]["/v1/me/usage/accounts/{account_id}"]["put"]["requestBody"]

    def check(node):
        if isinstance(node, dict):
            reference = node.get("$ref")
            if reference is not None:
                target = document
                for part in reference.removeprefix("#/").split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
                assert target is not None
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)


@pytest.mark.parametrize("mode", ["structured", "binary"])
async def test_cloud_event_v2_report_and_partial_updates(db_uri, mode):
    service = service_with_store(db_uri)
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=None, host_store=None), prefix="/v1"
    )
    body = {
        "schema_version": 2,
        "observed_at": time.time(),
        "balances": [{"id": "wallet", "remaining": 3, "unit": "USD"}],
    }
    envelope = {
        "specversion": "1.0",
        "id": "event-1",
        "source": "vendor-reader",
        "type": "dev.omnigent.usage.report",
        "time": "2026-11-06T05:00:00Z",
    }
    headers = (
        {"Content-Type": "application/cloudevents+json"}
        if mode == "structured"
        else {f"ce-{key}": value for key, value in envelope.items()}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/v1/me/usage/accounts/gw"
        reply = await client.put(
            path,
            json={**envelope, "data": body} if mode == "structured" else body,
            headers=headers,
        )
        assert reply.status_code == 200
        assert reply.json() == {"applied": 0, "held": [], "skipped": [], "older_sections": []}
        assert (
            await client.put(
                path,
                json={"schema_version": 2, "observed_at": time.time(), "display_name": "Wallet"},
            )
        ).status_code == 200
        tile = next(
            a for a in (await client.get("/v1/me/usage")).json()["accounts"] if a["id"] == "gw"
        )
        assert tile["balances"][0]["remaining"] == 3
        assert tile["source"] == "vendor-reader" and tile["read_at"] is None
        service.flush(force=True)
        stored = service.snapshot_store.read(RESERVED_USER_LOCAL)["usage.account.gw"]
        event = next(e for e in stored["report_log"] if e["kind"] == "cloudevent")
        assert event["id"] == "event-1" and event["time"] == envelope["time"]
        assert (
            await client.put(path, json=body, headers={"ce-specversion": "1.0"})
        ).status_code == 422
        assert (
            await client.put(
                path,
                json=[{**envelope, "data": body}],
                headers={"Content-Type": "application/cloudevents+json"},
            )
        ).status_code == 422
