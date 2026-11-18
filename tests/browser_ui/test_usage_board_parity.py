"""Usage composition and navigation through the built SPA boundary."""

import re
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


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("prefix", ["", "/frame"])
def test_usage_overview_geometry_and_navigation(
    page: Page, appearance_url: str, browser_contract: BrowserContract, width: int, prefix: str
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
    # Enough accounts to exercise the one-line account chooser at every width.
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
        for i in range(50)
    ]
    accounts[1]["windows"] = [
        {**window, "id": "short", "group": None, "state": "low", "percent_used": 96},
        {**window, "id": "month", "group": None, "state": "ok", "percent_used": 1.09},
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
        range_key = params["range"][0]
        ranges.append(range_key)
        account_id = params.get("account", [None])[0]
        bucket = 3600 if range_key == "today" else 86400
        return {
            "range": range_key,
            "generated_at": now,
            "tz": "UTC",
            "accounts": [
                {
                    "account": item["id"],
                    "span": {
                        "requested_from": now - 4 * bucket,
                        "counted_from": now - 4 * bucket,
                        "until": now,
                        "approx": False,
                    },
                    "mark": "~",
                    "totals": totals,
                    "previous": None,
                    "series": [{"at": now - bucket, **totals}],
                    "models": [{"model": "Example model", **totals}],
                    "hosts": [{"host_id": "example-host", **totals}],
                }
                for item in accounts
                if account_id is None or item["id"] == account_id
            ],
        }

    browser_contract.json("/v1/me/usage/activity", activity)
    browser_contract.json(
        "/v1/me/usage/analysis",
        {
            "generated_at": now,
            "complete": True,
            "classes": {
                "basis": "price_table_estimate",
                "items": [],
                "unpriced_tokens": {"input": 500},
                "unsplit_tokens": 1000,
                "unsplit_cost": {"low": "0.00", "high": None},
            },
            "rows": [],
            "pool_catalog": [],
            "pools": [],
            "span": {"requested_from": now - 86400, "counted_from": now - 86400, "until": now},
        },
    )
    browser_contract.json(
        "/v1/me/usage/pricing-baseline",
        {
            "revision": 0,
            "defaults": {
                "default_discount": "1",
                "display_currency": "USD",
                "fx_to_display": {"USD": "1"},
            },
            "cards": [],
        },
    )
    browser_contract.json("/v1/me/usage/events", {"events": [], "next_before": None})
    browser_contract.json("/v1/me/usage/sources", {"sources": []})
    page.set_viewport_size({"width": width, "height": 900})
    overview_url = appearance_url + prefix + "/usage/board"
    page.goto(overview_url)
    overview = page.get_by_test_id("usage-overview")
    chooser = page.get_by_role("navigation", name="Usage accounts")
    expect(chooser.get_by_role("link")).to_have_count(51)
    expect(chooser.get_by_role("link", name="All", exact=True)).to_have_attribute(
        "aria-current", "true"
    )
    expect(overview.get_by_text("$617.00", exact=True).first).to_be_visible()
    assert sorted(ranges) == ["d30", "d7", "period", "today"]
    assert_usage_geometry(
        page,
        {
            ".usage-overview-accounts a": 51,
            ".usage-overview-quota li": 50,
            ".usage-overview-cells button": 4,
            ".usage-chart-plot": 1,
            ".usage-tiles": 0,
        },
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    codex = chooser.get_by_role("link", name="Example plan 0", exact=True)
    codex.focus()
    page.keyboard.press("Enter")
    expect(page).to_have_url(overview_url + "?account=plan-0")
    expect(overview.get_by_role("heading", name="Example plan 0", exact=True)).to_be_visible()
    expect(overview.get_by_text("$12.34", exact=True).first).to_be_visible()
    expect(page.get_by_test_id("usage-chart-now").locator("b")).to_have_text("$12.34")
    overview.get_by_text("Cost composition", exact=True).click()
    expect(overview.get_by_text("Unpriced / Unsplit", exact=True)).to_be_visible()
    assert_usage_geometry(
        page,
        {
            "[data-usage-window]": 4,
            ".usage-overview-cells button": 4,
            ".usage-chart-plot": 1,
            ".usage-overview-stack": 1,
        },
    )
    cost = overview.get_by_role("region", name="Cost", exact=True)
    cost.get_by_role("button", name="Token", exact=True).click()
    expect(page.get_by_test_id("usage-chart-now").locator("b")).to_have_text("1.00M")
    cost.get_by_role("button", name=re.compile("^Today")).click()
    expect(cost.get_by_text("Per hour", exact=True)).to_be_visible()
    analysis = cost.get_by_role("link", name=re.compile("^View in Analysis"))
    analysis.press("Enter")
    expect(page).to_have_url(
        appearance_url + prefix + "/usage/board/cost?range=today&account=plan-0"
    )
    expect(page.get_by_role("link", name="Analysis", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
