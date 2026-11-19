"""Usage composition and navigation through the built SPA boundary."""

import re
import time
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_features as appearance_features
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url
from tests.browser_ui.test_usage_analysis_selection import analysis_payload
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
    browser_contract.json("/v1/model-connections", [])
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

    def analysis_response(request):
        params = parse_qs(urlparse(request.url).query, keep_blank_values=True)
        if params.get("include") == ["previous"]:
            return analysis_payload(params, int(now))
        return {
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
        }

    browser_contract.json("/v1/me/usage/analysis", analysis_response)
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
    browser_contract.json(
        "/v1/me/usage/issues", {"failures": [], "unpriced_models": [], "source_reports": []}
    )
    extension_path = "/extensions/example.pools/main"
    proxy_path = "/v1/extensions/example.pools/proxy/main/"
    pool_fragment = "#/models/pools"
    browser_contract.json(
        "/v1/me",
        {
            "user_id": "local",
            "is_admin": True,
            "preferences": {
                "version": 1,
                "settings": {
                    "usage_context": {
                        "version": 6,
                        "board": {"modelPoolsPage": extension_path + pool_fragment},
                    }
                },
            },
        },
    )
    browser_contract.json(
        "/v1/extensions",
        {
            "object": "list",
            "data": [
                {
                    "object": "extension",
                    "id": "example.pools",
                    "display_name": "Example pool explorer",
                    "distribution": "example-pools",
                    "version": "1.0.0",
                    "extension_api": 1,
                    "status": "enabled",
                    "permissions": ["pages.external", "pages.proxy"],
                    "pages": [
                        {
                            "id": "model-pools",
                            "title": "Pool explorer",
                            "route": "main",
                            "kind": "proxied",
                            "view": None,
                            "url": proxy_path,
                        }
                    ],
                    "primary_navigation": [],
                    "browser": {
                        "declared": False,
                        "has_styles": False,
                        "digest": None,
                        "script_url": None,
                        "style_url": None,
                    },
                }
            ],
        },
    )
    browser_contract.route(
        appearance_url + proxy_path,
        lambda route: route.fulfill(
            content_type="text/html",
            body="""<output></output><script>
              const show = () => { document.querySelector('output').textContent = location.hash; };
              show(); addEventListener('hashchange', show);
            </script>""",
        ),
    )
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
        appearance_url + prefix + "/usage/board/analysis?range=today&account=plan-0"
    )
    expect(page.get_by_role("link", name="Analysis", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    page.go_back()
    host = overview.get_by_role("region", name="Hosts").get_by_role(
        "link", name=re.compile("^example-host")
    )
    host.press("Enter")
    expect(page).to_have_url(
        appearance_url
        + prefix
        + "/usage/board/analysis?range=today&account=plan-0&host=example-host"
    )
    expect(
        page.get_by_role("button", name="Remove Host: example-host", exact=True)
    ).to_be_visible()

    page.goto(overview_url + "/cost?range=d7&extra=keep#summary")
    expect(page).to_have_url(overview_url + "/analysis?range=d7&extra=keep#summary")
    page.goto(overview_url + "?theme=invalid")
    surface = page.locator(".usage-surface")
    expect(surface).to_be_visible()
    expect(page.locator("[data-usage-theme]")).to_have_count(0)
    for theme, preference, background, ink in [
        ("light", "dark", "rgb(245, 246, 248)", "rgb(15, 20, 28)"),
        ("dark", "light", "rgb(16, 20, 27)", "rgb(234, 238, 244)"),
    ]:
        page.evaluate("theme => localStorage.setItem('web-theme', theme)", preference)
        page.goto(overview_url + f"?theme={theme}")
        expect(page.locator(".usage-board-scroll")).to_have_css("background-color", background)
        expect(surface).to_have_css("color", ink)

    scroll = page.locator(".usage-board-scroll")
    if prefix:
        expect(page.locator("header.chat-header")).to_have_count(0)
        assert scroll.evaluate("element => element.getBoundingClientRect().top") == 0
    else:
        clearance = scroll.evaluate(
            """element => element.getBoundingClientRect().top -
                document.querySelector('header.chat-header').getBoundingClientRect().bottom"""
        )
        assert clearance >= 0
    page.get_by_role("link", name="Analysis", exact=True).click()
    page.get_by_text("Analysis tools", exact=True).click()
    page.get_by_role("link", name="Issues", exact=True).click()
    expect(page).to_have_url(overview_url + "/issues?theme=dark")
    expect(surface).to_have_css("color", "rgb(234, 238, 244)")
    page.get_by_role("link", name="Cost").click()
    expect(page).to_have_url(overview_url + "/analysis?theme=dark")

    pools = page.get_by_role("link", name=re.compile(r"^Model pools"))
    expect(pools).to_have_attribute("href", extension_path + pool_fragment)
    if prefix:
        expect(pools).to_have_attribute("target", "_blank")
        expect(pools).to_have_attribute("rel", "noopener")
        with page.expect_popup() as popup:
            pools.click()
        extension_page = popup.value
    else:
        assert pools.get_attribute("target") is None
        pools.click()
        extension_page = page
    expect(extension_page).to_have_url(appearance_url + extension_path + pool_fragment)
    frame = extension_page.frame_locator('iframe[title="Pool explorer"]')
    expect(frame.locator("output")).to_have_text(pool_fragment)
    extension_page.evaluate("location.hash = '#/another-view'")
    expect(frame.locator("output")).to_have_text("#/another-view")
    if prefix:
        extension_page.close()
