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
def test_usage_ranges_pools_and_navigation(
    page: Page, appearance_url: str, browser_contract: BrowserContract, width: int
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
    # 50 accounts, 365 daily points and four quota windows per account.
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
        "/v1/me/usage/analysis",
        {
            "generated_at": now,
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
    page.set_viewport_size({"width": width, "height": 900})
    started = time.perf_counter()
    page.goto(appearance_url + "/frame/usage/board")
    expect(page.get_by_test_id("usage-tile")).to_have_count(50)
    board_geometry = {
        '[data-testid="usage-tile"]': 50,
        '[data-testid="usage-ring"]': 198,
        "[data-usage-label]": 99,
        "[data-usage-amount]": 0,
    }
    assert_usage_geometry(page, board_geometry)
    summary = page.get_by_role("region", name="Usage summary")
    expect(summary.get_by_text("$617.00")).to_have_count(4)
    # The target question glyph must not inherit a full-height button row.
    assert summary.locator(".usage-summary-tip").first.bounding_box()["height"] == 15
    usable_ms = (time.perf_counter() - started) * 1000
    assert set(ranges) == {"period", "d30", "today", "h24", "d7"}
    assert len(ranges) == 5
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.get_by_role("status")).to_contain_text("API valuation unavailable")
    expect(page.get_by_role("status")).to_be_visible()
    expect(page.get_by_test_id("usage-tile").nth(1)).to_have_attribute("data-tone", "ok")
    expect(
        page.get_by_test_id("usage-tile")
        .nth(1)
        .locator('.usage-satellite [data-testid="usage-ring"]')
    ).to_have_attribute("data-tone", "crit")
    geometry = page.evaluate("""() => {
      const box=s=>document.querySelector(s).getBoundingClientRect().toJSON();
      const style=s=>getComputedStyle(document.querySelector(s));
      return {head:box('.usage-top h1'),nav:box('.usage-tabs'),age:box('.usage-data-age'),
        regular:box('[data-account="plan-1"]'),wide:box('[data-account="plan-0"]'),
        tilesDisplay:style('.usage-tiles').display,gap:style('.usage-tiles').gap,
        minHeight:style('[data-account="plan-1"]').minHeight,
        padding:style('.usage-board').paddingLeft,
        columns:style('.usage-tiles').gridTemplateColumns};
    }""")
    if width < 600:
        assert geometry["tilesDisplay"] == "grid"
        assert geometry["gap"] == "6px"
        assert geometry["minHeight"] == "0px"
        assert geometry["padding"] == "10px"
        assert len(geometry["columns"].split()) == 2
        assert geometry["nav"]["top"] >= geometry["head"]["bottom"]
        assert geometry["age"]["top"] >= geometry["nav"]["bottom"]
        assert geometry["wide"]["width"] > geometry["regular"]["width"] * 1.9
    else:
        assert geometry["regular"]["width"] == 150
        assert geometry["wide"]["width"] == 300
    # Large figure, blank summary padding and native keyboard activation all open Analysis.
    for activation in ["figure", "whitespace", "keyboard"]:
        summary = page.get_by_role("region", name="Usage summary")
        link = summary.get_by_role("link", name="Open usage analysis")
        if activation == "figure":
            summary.scroll_into_view_if_needed()
            figure = summary.locator(".usage-summary-big").bounding_box()
            anchor = link.bounding_box()
            link.click(
                position={
                    "x": figure["x"] + figure["width"] / 2 - anchor["x"],
                    "y": figure["y"] + figure["height"] / 2 - anchor["y"],
                }
            )
        elif activation == "whitespace":
            summary.scroll_into_view_if_needed()
            rect = summary.bounding_box()
            anchor = link.bounding_box()
            link.tap(
                position={
                    "x": rect["x"] + rect["width"] / 2 - anchor["x"],
                    "y": rect["y"] + rect["height"] - 4 - anchor["y"],
                }
            )
        else:
            link.focus()
            page.keyboard.press("Enter")
        expect(page).to_have_url(appearance_url + "/frame/usage/board/cost")
        expect(page.locator(".ua-kpi")).to_have_count(4)
        assert_usage_geometry(
            page, {'[data-testid="usage-ring"]': 0, ".ua-kpi": 4, '[role="table"]': 1}
        )
        page.get_by_role("link", name="Usage", exact=True).click()
        expect(page.get_by_test_id("usage-tile")).to_have_count(50)
        assert_usage_geometry(page, board_geometry)
    summary.get_by_role("button", name="All accounts · 30-day reported cost details").click()
    expect(page.get_by_test_id("usage-tip-card")).to_be_visible()
    page.keyboard.press("Escape")
    first = page.get_by_test_id("usage-tile").first
    expect(first.get_by_role("link", name="Example plan 0 Pool B", exact=True)).to_have_attribute(
        "href", "/frame/usage/board/plan-0?focus=Pool%20B"
    )
    first.get_by_role("link", name="Example plan 0 Pool B", exact=True).focus()
    page.keyboard.press("Enter")
    expect(page).to_have_url(appearance_url + "/frame/usage/board/plan-0?focus=Pool%20B")
    page.get_by_role("link", name="Usage", exact=True).click(timeout=5000)
    expect(page.get_by_test_id("usage-tile")).to_have_count(50)
    page.get_by_text("Board tools", exact=True).click()
    expect(page.get_by_role("button", name="Compact view")).to_be_visible()
    page.get_by_role("link", name="Analysis", exact=True).click()
    expect(page).to_have_url(appearance_url + "/frame/usage/board/cost")
    expect(page.get_by_role("link", name="Analysis", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    print(
        json.dumps(
            {
                "width": width,
                "accounts": 50,
                "daily_points": 18250,
                "usable_ms": round(usable_ms),
                "navigation": page.evaluate(
                    "performance.getEntriesByType('navigation')[0].toJSON()"
                ),
            }
        )
    )
