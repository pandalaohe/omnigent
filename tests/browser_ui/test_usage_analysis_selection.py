"""Analysis selection through the real built SPA and browser transport."""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect

from omnigent.usage.analysis_presentation import present_analysis
from omnigent.usage.contract import UsageAnalysisView
from omnigent.usage.pricing import PricingBaseline, PurchasedCard
from omnigent.usage.pricing_projection import value_usage
from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_features as appearance_features
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url
from tests.browser_ui.usage_geometry import assert_usage_geometry
from tests.browser_ui.usage_long_names import LONG_NAMES


def test_cost_class_table_tracks_and_narrow_layout(
    page: Page, appearance_url: str, browser_contract: BrowserContract, tmp_path
):
    from fastapi.encoders import jsonable_encoder

    from tests.server.test_usage_pricing_math import view

    browser_contract.json("/v1/info", {"usage_board_enabled": True, "features": {}})
    browser_contract.json(
        "/v1/me/usage",
        {
            "generated_at": 1794268800,
            "accounts": [
                {
                    "id": "account-a",
                    "kind": "account",
                    "display_name": "Alpha",
                    "windows": [],
                    "hosts": [],
                    "freshness": "fresh",
                }
            ],
        },
    )
    browser_contract.json("/v1/me/usage/activity", {"accounts": []})
    source = view()
    current = {"kind": "subscription"}

    def analysis(request):
        params = parse_qs(urlparse(request.url).query)
        source.account_cost_classes = {"account-a": current["kind"]}
        baseline = PricingBaseline()
        payload = source.model_dump(mode="json")
        payload["pricing"] = jsonable_encoder(value_usage(source, baseline))
        if "dimension" in params:
            payload["presentation"] = jsonable_encoder(
                present_analysis(
                    source,
                    None,
                    baseline,
                    dimension=params["dimension"][0],
                    keys=json.loads(params["selection"][0]),
                    excluded=set(),
                )
            )
        return payload

    browser_contract.json("/v1/me/usage/analysis", analysis)
    page.add_init_script(
        "localStorage.setItem('omnigent:usage-context-preferences',JSON.stringify({version:1,board:{enabled:true}}))"
    )
    for kind, count in [("subscription", 7), ("prepaid", 7), ("metered", 6), ("free", 4)]:
        current["kind"] = kind
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(appearance_url + "/frame/usage/board/cost")
        table = page.get_by_role("table", name="Each item", exact=True)
        row = table.get_by_role("row").nth(1)
        expect(row.get_by_role("cell")).to_have_count(count)
        geometry = row.evaluate(
            "el => ({tracks:getComputedStyle(el).gridTemplateColumns.split(' ').length, "
            "tops:[...el.children].map(child=>child.getBoundingClientRect().top)})"
        )
        assert geometry["tracks"] == count
        assert max(geometry["tops"]) - min(geometry["tops"]) < 20
        page.screenshot(path=str(tmp_path / f"{kind}-wide.png"))
        page.set_viewport_size({"width": 390, "height": 900})
        expect(row).to_be_visible()
        page.wait_for_function(
            "getComputedStyle(document.querySelector('.ua-table-row'))"
            ".gridTemplateColumns.split(' ').length === 2"
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"{kind}-narrow.png"))


@pytest.mark.parametrize("width", [1280, 900, 390])
def test_analysis_selection(
    page: Page, appearance_url: str, browser_contract: BrowserContract, width: int, record_property
):
    now = 1794268800
    long_account, long_pool, long_model, long_card = (
        LONG_NAMES[key] for key in ["account", "pool", "model", "card"]
    )
    accounts = [
        {
            "id": key,
            "kind": "account",
            "display_name": name,
            "windows": [],
            "hosts": [],
            "freshness": "fresh",
            "age_s": 0,
            "read_at": now,
        }
        for key, name in [("a", "Alpha"), ("b", long_account)]
    ]
    rows = [
        {
            "account_id": key,
            "parent_account_id": key,
            "pool_id": "p",
            "hour": now - 3600,
            "model": model,
            "basis": "metered",
            "tokens": tokens,
            "requests": 1000,
            "failed": failed,
            "cost_usd": 1,
            "uncached_input_tokens": tokens,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "contributes_usage": True,
            "contributes_money": True,
            "attributed": True,
            "coverage": "complete",
            "reason": None,
        }
        for key, model, tokens, failed in [
            ("a", "Claude", 1000000, 0),
            ("b", long_model, 3000000, 30),
        ]
    ]
    browser_contract.json("/v1/info", {"usage_board_enabled": True, "features": {}})
    browser_contract.json("/v1/me/usage", {"generated_at": now, "accounts": accounts})
    browser_contract.json("/v1/me/usage/activity", {"generated_at": now, "accounts": []})
    span = {
        "requested_from": now - 86400,
        "counted_from": now - 86400,
        "until": now,
        "approx": False,
    }
    source = UsageAnalysisView.model_validate(
        {
            "generated_at": now,
            "range": "today",
            "tz": "UTC",
            "span": span,
            "account_spans": {"a": span, "b": span},
            "pool_catalog": [
                {
                    "parent_account_id": key,
                    "pool_id": "p",
                    "display_name": f"{name} pool" if key == "a" else long_pool,
                    "usage_available": True,
                }
                for key, name in [("a", "Alpha"), ("b", "Beta")]
            ]
            + [
                {
                    "parent_account_id": "a",
                    "pool_id": "q",
                    "display_name": "Quota only",
                    "usage_available": False,
                }
            ],
            "rows": rows,
        }
    )
    previous = source.model_copy(deep=True)
    previous.span = source.span.model_copy(
        update={
            "counted_from": now - 2 * 86400,
            "requested_from": now - 2 * 86400,
            "until": now - 86400,
        }
    )
    previous.account_spans = {"a": previous.span, "b": previous.span}
    for row in previous.rows:
        row.hour -= 86400
        row.tokens //= 2
        row.uncached_input_tokens = row.tokens
    baseline = PricingBaseline(
        cards=[
            PurchasedCard.model_validate(
                {
                    "id": f"card-{parent}-{start}",
                    "name": long_card
                    if parent == "b" and start == "2026-11-09"
                    else f"{label} card",
                    "retail_amount": retail,
                    "purchase_currency": "USD",
                    "allowance_usd": "100",
                    "discount_override": "1",
                    "period_kind": "week",
                    "starts_on": start,
                    "target_kind": "pool",
                    "parent_account_id": parent,
                    "target_id": "p",
                    "rates_usd_per_million": {
                        "model": model,
                        "read": "2",
                        "write": "0",
                        "cache_read": "0",
                        "cache_write": "0",
                    },
                }
            )
            for parent, label, model, retail, start in [
                ("a", "Alpha", "Claude", "70", "2026-11-09"),
                ("b", "Beta", long_model, "140", "2026-11-09"),
                ("a", "Previous Alpha", "Claude", "140", "2026-11-02"),
                ("b", "Previous Beta", long_model, "280", "2026-11-02"),
            ]
        ]
    )
    source.quota_closings = [
        {
            "account_id": "a",
            "parent_account_id": "a",
            "pool_id": "p",
            "window_id": "week",
            "observed_at": now - 86400 - 60,
            "resets_at": now - 86400,
            "remaining": {
                "state": "range",
                "low": "0",
                "high": "30",
                "unit": "percent",
                "cohort_id": "closing",
                "as_of": now,
            },
        }
    ]
    source.quota_closings += [
        {**source.quota_closings[0], "account_id": "b", "parent_account_id": "b"}
    ]

    def analysis(request):
        params = parse_qs(urlparse(request.url).query)
        payload = source.model_dump(mode="json")
        from fastapi.encoders import jsonable_encoder

        payload["pricing"] = jsonable_encoder(value_usage(source, baseline))
        if "dimension" in params:
            payload["presentation"] = jsonable_encoder(
                present_analysis(
                    source,
                    None if params.get("range") == ["d90"] else previous,
                    baseline,
                    dimension=params["dimension"][0],
                    keys=json.loads(params["selection"][0]),
                    excluded=set(params.get("exclude", [])),
                )
            )
        return payload

    browser_contract.json("/v1/me/usage/analysis", analysis)
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(
        "localStorage.setItem('omnigent:usage-context-preferences',JSON.stringify({version:1,board:{enabled:true}}))"
    )
    page.goto(appearance_url + "/frame/usage/board/cost")
    picks = page.get_by_role("group", name="Select items")
    expect(picks.get_by_role("button", name="Alpha", exact=True)).to_be_visible()
    expect(picks.get_by_role("button", name=long_account, exact=True)).to_have_attribute(
        "title", long_account
    )
    analysis_geometry = {
        '[data-testid="usage-ring"]': 0,
        ".ua-kpi": 5,
        '[role="table"]': 1,
        ".usage-tabs a": 2,
        '[aria-label="Group by"] button': 4,
        '[aria-label="Time range"] button': 4,
    }
    assert_usage_geometry(page, analysis_geometry)
    table = page.get_by_role("table", name="Each item")
    expect(table.get_by_title(long_account, exact=True)).to_be_visible()
    expect(table.get_by_text("3.00%", exact=True)).to_be_visible()
    for section in ["Subscription payback", "Failure rate", "Unused last period"]:
        expect(
            page.get_by_role("region", name=section, exact=True).get_by_title(
                long_account, exact=True
            )
        ).to_be_visible()
    page.get_by_role("button", name=long_account + " failure rate 3.00%", exact=True).click()
    tip = page.get_by_test_id("usage-tip-card")
    expect(tip.get_by_title(long_account, exact=True)).to_be_visible()
    expect(tip.get_by_text("3.00%", exact=True)).to_be_visible()
    assert_usage_geometry(page, analysis_geometry)
    page.keyboard.press("Escape")
    expect(tip).to_have_count(0)
    account_pick = picks.get_by_role("button", name=long_account, exact=True)
    account_pick.click()
    expect(account_pick).to_have_attribute("aria-pressed", "true")
    expect(table.get_by_title(long_account, exact=True)).to_be_visible()
    expect(table).not_to_contain_text("Alpha")
    assert_usage_geometry(page, analysis_geometry)
    picks.get_by_role("button", name="All", exact=True).click()
    picks.get_by_role("button", name="Alpha", exact=True).click()
    expect(table).to_contain_text("Alpha")
    expect(table).not_to_contain_text(long_account)
    expect(table).to_contain_text("$10.00")
    expect(page.get_by_text("Less spent $30.00", exact=True)).to_be_visible()
    expect(page.get_by_text("≤30.00%", exact=True)).to_be_visible()
    expect(page.get_by_text("More used 300.00%", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Mon 23:00 1000000 tokens", exact=True)).to_be_visible()
    expect(
        page.get_by_role("button", name="Claude token share 100.00%", exact=True)
    ).to_be_visible()
    expect(page.get_by_text("100.00% / —", exact=True)).to_be_visible()
    page.get_by_role("button", name="Model", exact=True).click()
    model = picks.get_by_role("button", name=long_model, exact=True)
    expect(model).to_have_attribute("title", long_model)
    model.click()
    expect(table).to_contain_text(long_model)
    assert_usage_geometry(page, analysis_geometry)
    expect(table).not_to_contain_text("Claude")
    expect(table).to_contain_text("Unknown")
    assert_usage_geometry(page, analysis_geometry)
    expect(
        page.get_by_role("figure", name="Cumulative selected tokens and paid")
    ).not_to_contain_text("Previous selected hourly series not available yet")
    page.get_by_role("button", name="Pool", exact=True).click()
    expect(picks.get_by_role("button", name=long_pool, exact=True)).to_have_attribute(
        "title", long_pool
    )
    expect(table.get_by_title(long_pool, exact=True)).to_be_visible()
    assert_usage_geometry(page, analysis_geometry)
    picks.get_by_role("button", name="Quota only", exact=True).click()
    expect(table).to_contain_text("Unknown")
    expect(page.get_by_role("button", name="GPT token share 100.00%", exact=True)).to_have_count(0)
    assert_usage_geometry(page, analysis_geometry)
    page.get_by_role("button", name="90 days", exact=True).click()
    expect(page.get_by_text("90 days has no previous period", exact=True)).to_have_count(2)
    assert_usage_geometry(page, analysis_geometry)
    page.get_by_role("button", name="Card", exact=True).click()
    analysis_geometry[".ua-kpi"] = 4
    expect(picks.get_by_role("button", name=long_card, exact=True)).to_have_attribute(
        "title", long_card
    )
    cards = page.get_by_role("table", name="Purchased cards")
    expect(cards).to_contain_text("Alpha card")
    expect(cards).to_contain_text("$10.00")
    expect(cards).to_contain_text("$100.00")
    expect(cards.get_by_title(long_card, exact=True)).to_be_visible()
    assert_usage_geometry(page, analysis_geometry)
    picks.get_by_role("button", name=long_card, exact=True).click()
    expect(cards.get_by_title(long_card, exact=True)).to_be_visible()
    expect(cards).not_to_contain_text("Alpha card")
    assert_usage_geometry(page, analysis_geometry)
    expect(page.get_by_role("region", name="Failure rate")).to_have_count(0)
    expect(page.get_by_role("group", name="Selected weekday and hour tokens")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    record_property(
        "usage_name_views",
        json.dumps(
            {
                "width": width,
                "views": [
                    "account",
                    "account-selected",
                    "account-tooltip",
                    "model",
                    "pool",
                    "card",
                    "card-selected",
                ],
                "names": [long_account, long_pool, long_model, long_card],
            }
        ),
    )
