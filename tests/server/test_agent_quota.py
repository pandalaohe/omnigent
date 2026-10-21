"""Quota reports stay owner/host/tenant scoped and use current deciding periods."""

import time
from types import SimpleNamespace

import httpx
import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.server.agent_quota import AgentQuotaService, QuotaContext
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
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
