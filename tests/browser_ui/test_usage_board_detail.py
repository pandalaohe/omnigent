"""Usage composition and navigation through the built SPA boundary."""

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_features as appearance_features
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url
from tests.browser_ui.usage_geometry import assert_usage_geometry


@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "has_touch": True}


@pytest.mark.parametrize("width", [1280, 900, 390])
def test_account_detail_keyboard_ranges_and_return(
    page: Page, appearance_url: str, browser_contract: BrowserContract, width: int, record_property
):
    now = time.time()
    window = {
        "id": "week",
        "label": "7d",
        "group": "Pool A",
        "kind": "weekly",
        "percent_used": 90.61,
        "remaining_pct": 9.39,
        "window_minutes": 10080,
        "time_left_s": 86400,
        "resets_at": now + 86400,
        "state": "low",
        "pace_pct": 18.78,
        "ahead_pts": 10,
        "used_value": None,
        "limit_value": None,
        "unit": None,
        "past_reset": False,
    }
    account = {
        "id": "plan",
        "kind": "account",
        "display_name": "Example plan",
        "plan": "Monthly",
        "band": None,
        "product": None,
        "details_url": None,
        "renews_at": now + 86400,
        "state": "low",
        "reason": None,
        "deciding_window": "week",
        "pace_pct": 18.78,
        "read_at": now,
        "age_s": 0,
        "freshness": "fresh",
        "mark": None,
        "period": {"start": now - 86400 * 30, "days": 30, "moved_from": None},
        "windows": [window, {**window, "id": "other", "group": "Pool B", "percent_used": 22.25}],
        "hosts": [],
    }
    # Distinct multi-pool, extreme-reading and unreported accounts, with a year of activity.
    accounts = [
        {
            **account,
            "id": f"plan-{i}",
            "display_name": f"Example plan {i}",
            "windows": [
                window,
                {**window, "id": "day", "label": "1d"},
                {**window, "id": "other", "group": "Pool B", "percent_used": 22.25},
                {**window, "id": "short", "group": "Pool B", "label": "5h"},
            ],
        }
        for i in range(2)
    ]
    accounts[1]["windows"] = [
        {**window, "id": "short", "group": None, "state": "low", "percent_used": 100},
        {**window, "id": "month", "group": None, "state": "ok", "percent_used": 1.09},
        {
            **window,
            "id": "amount",
            "group": "Amount only",
            "percent_used": None,
            "used_value": 1234567890123.45,
            "unit": "USD",
        },
    ]
    accounts[1]["display_name"] = "Long account name with multiple segments and meaningful suffix"
    accounts.append(
        {
            **account,
            "id": "plan-2",
            "display_name": "Unreported plan",
            "windows": [],
            "read_at": None,
        }
    )
    accounts[0]["balances"] = [
        {"id": "balance", "remaining": 298.24, "unit": "CNY", "label": "Balance"},
        {"id": "large", "remaining": 1234567890123.45, "unit": "USD", "label": "Large balance"},
    ]
    browser_contract.json("/v1/info", {"usage_board_enabled": True, "features": {}})
    browser_contract.json("/v1/me/usage", {"generated_at": now, "accounts": accounts})
    ranges = []
    totals = {
        "cost_usd": 12.34,
        "counted_cost_usd": 12.34,
        "tokens": 1000000,
        "unpriced": 0,
        "per_million": 12.34,
        "covered_cost_usd": 12.34,
        "covered_tokens": 1000000,
        "cost_coverage_pct": 100,
        "tokens_complete": True,
        "requests": 10,
        "failed": 0,
        "failure_pct": 0,
        "reasons": {},
    }

    def activity(request):
        params = parse_qs(urlparse(request.url).query)
        ranges.append(params["range"][0])
        return {
            "range": params["range"][0],
            "generated_at": now,
            "tz": "UTC",
            "accounts": [
                {
                    "account": item["id"],
                    "span": {
                        "requested_from": now - 365 * 86400,
                        "counted_from": now - 365 * 86400,
                        "until": now,
                        "approx": False,
                    },
                    "mark": None,
                    "totals": totals,
                    "previous": None,
                    "series": [{"at": now - day * 86400, **totals} for day in range(365)],
                }
                for item in accounts
            ],
        }

    browser_contract.json("/v1/me/usage/activity", activity)
    browser_contract.json(
        "/v1/me/usage/analysis", {"generated_at": now, "pool_catalog": [], "pools": [], "rows": []}
    )
    pricing = {
        "revision": 1,
        "defaults": {
            "schema_version": 1,
            "default_discount": "0.8",
            "display_currency": "USD",
            "fx_to_display": {"USD": "1", "HKD": "0.128205128205128205"},
        },
        "cards": [
            {
                "id": "card-a",
                "name": "Used card",
                "retail_amount": "100.000000000000000001",
                "purchase_currency": "HKD",
                "allowance_usd": "100",
                "period_kind": "week",
                "starts_on": "2026-11-02",
                "timezone": "UTC",
                "target_kind": "account",
                "target_id": "plan-0",
                "parent_account_id": None,
                "discount_override": None,
                "rates_usd_per_million": {
                    "read": "2",
                    "write": "4",
                    "cache_read": "0.2",
                    "cache_write": "2.5",
                },
            }
        ],
    }
    browser_contract.json("/v1/me/usage/pricing-baseline", pricing)
    browser_contract.json(
        "/v1/me/usage/pricing-baseline",
        lambda request: {**request.post_data_json, "revision": 2},
        method="PUT",
    )
    browser_contract.json("/v1/me/usage/lines", {"schema": 1, "tiles": {}})
    browser_contract.json("/v1/me/session-choice", {"settings": None, "revision": "sample"})
    browser_contract.json(
        "/v1/me/session-choice/meters", {"meters": [], "running": {}, "floors": {}}
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(appearance_url + "/frame/usage/board")
    first = page.get_by_test_id("usage-tile").first
    first.get_by_role("link", name="Example plan 0 Pool B", exact=True).focus()
    page.keyboard.press("Enter")
    expect(page.locator("[data-pool]").first).to_have_attribute("data-pool", "Pool B")
    assert_usage_geometry(
        page,
        {
            '[data-testid="usage-ring"]': 4,
            ".usage-balance-ring": 2,
            "[data-usage-amount]": 2,
            "[data-usage-label]": 6,
        },
    )
    expect(
        page.locator(".usage-detail-cost").get_by_text(
            "Not reported · No proven usage for this pool."
        )
    ).to_be_visible()
    tools = page.get_by_text("Usage tools", exact=True).locator("..")
    tools.locator("summary").click()
    expect(tools.get_by_text("Not reported · No proven usage for this pool.")).to_be_visible()
    expect(tools).not_to_contain_text("$12.34")
    # The relation read is real, but this fixture proves no activity channel.
    expect(page.get_by_role("group", name="Cost time range")).not_to_contain_text("$12.34")
    page.get_by_role("link", name="Usage board", exact=True).click()
    expect(page.get_by_test_id("usage-tile")).to_have_count(3)
    assert_usage_geometry(
        page,
        {
            '[data-testid="usage-tile"]': 3,
            '[data-testid="usage-ring"]': 8,
            ".usage-balance-ring": 2,
            "[data-usage-amount]": 3,
            "[data-usage-label]": 6,
        },
    )
    page.get_by_text("Board tools", exact=True).click()
    page.get_by_role("button", name="Compact view", exact=True).click()
    assert_usage_geometry(
        page,
        {
            '[data-testid="usage-tile"]': 3,
            '[data-testid="usage-ring"]': 3,
            ".usage-balance-ring": 0,
            "[data-usage-amount]": 2,
            "[data-usage-label]": 2,
        },
    )
    page.get_by_role("button", name="Rings view", exact=True).click()
    visited = []
    for item in accounts:
        page.goto(appearance_url + f"/frame/usage/board/{item['id']}")
        expect(page.get_by_role("heading", name=item["display_name"], exact=True)).to_be_visible()
        cells = page.get_by_role("group", name="Cost time range")
        expect(cells.get_by_text("$12.34", exact=True)).to_have_count(3)
        for label in ["Today", "This period", "Last 7 days"]:
            button = cells.get_by_role("button", name=label + " $12.34")
            button.focus()
            page.keyboard.press("Enter")
            expect(button).to_have_attribute("aria-pressed", "true")
        balances = len(item.get("balances", []))
        amount_windows = sum(window["percent_used"] is None for window in item["windows"])
        expected = {
            '[data-testid="usage-ring"]': max(1, len(item["windows"])),
            ".usage-balance-ring": balances,
            "[data-usage-amount]": balances + amount_windows,
            "[data-usage-label]": len(item["windows"]) + balances,
        }
        expect(page.get_by_text("Hosts", exact=True)).to_be_visible()
        assert_usage_geometry(page, expected)
        visited.append({"account": item["id"], "view": "detail", "width": width})
        if item["id"] == "plan-1":
            expect(page.locator("[data-usage-percent]").filter(has_text="100.00%")).to_be_visible()
            expect(
                page.locator("[data-usage-amount]").filter(has_text="$1,234,567,890,123.45")
            ).to_be_visible()
        page.get_by_role("link", name="Account settings").click()
        expect(page.get_by_test_id("usage-tile")).to_have_count(1)
        expect(page.get_by_test_id("usage-tile")).to_have_attribute("data-account", item["id"])
        expected['[data-testid="usage-tile"]'] = 1
        expected["[data-usage-label]"] = (
            len({window["group"] for window in item["windows"]}) + balances
        )
        assert_usage_geometry(page, expected)
        visited.append({"account": item["id"], "view": "settings", "width": width})
        back = page.get_by_role("link", name=item["display_name"], exact=True).and_(
            page.locator('[data-slot="button"]')
        )
        expect(back).to_have_attribute("title", item["display_name"])
    page.goto(appearance_url + "/frame/usage/board/plan-0/settings")
    expect(page.get_by_role("link", name="Models", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Issues", exact=True)).to_be_visible()
    expect(page.get_by_text("Connect a source", exact=True)).to_be_visible()
    settings_geometry = {
        '[data-testid="usage-tile"]': 1,
        '[data-testid="usage-ring"]': 4,
        ".usage-balance-ring": 2,
        "[data-usage-amount]": 2,
        "[data-usage-label]": 4,
    }
    assert_usage_geometry(page, settings_geometry)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    record_property("usage_views", json.dumps(visited))

    page.get_by_text("Pricing", exact=True).click()
    expect(page.get_by_label("Card payment preview")).to_contain_text("HKD 80.00 · USD 10.26")
    field = page.get_by_label("Card discount", exact=True)
    field.fill("0.6")
    field.blur()
    expect(page.get_by_label("Card payment preview")).to_contain_text("HKD 60.00 · USD 7.69")
    with page.expect_request(
        lambda req: req.method == "PUT" and req.url.endswith("/usage/pricing-baseline")
    ) as saved_request:
        page.get_by_role("button", name="Save pricing", exact=True).click()
    assert saved_request.value.post_data_json["cards"][0]["discount_override"] == "0.6"
    assert (
        saved_request.value.post_data_json["cards"][0]["retail_amount"] == "100.000000000000000001"
    )
    expect(page.get_by_text("Pricing saved. History recalculated.")).to_be_visible()
    assert_usage_geometry(page, settings_geometry)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
