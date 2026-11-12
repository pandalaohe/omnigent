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


@pytest.mark.parametrize("width", [1280, 900, 390])
def test_analysis_selection(
    page: Page, appearance_url: str, browser_contract: BrowserContract, width: int
):
    now = 1794268800
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
        for key, name in [("a", "Alpha"), ("b", "Beta")]
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
        for key, model, tokens, failed in [("a", "Claude", 1000000, 0), ("b", "GPT", 3000000, 30)]
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
                    "display_name": f"{name} pool",
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
                    "name": f"{label} card",
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
                ("b", "Beta", "GPT", "140", "2026-11-09"),
                ("a", "Previous Alpha", "Claude", "140", "2026-11-02"),
                ("b", "Previous Beta", "GPT", "280", "2026-11-02"),
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
    analysis_geometry = {'[data-testid="usage-ring"]': 0, ".ua-kpi": 4, '[role="table"]': 1}
    assert_usage_geometry(page, analysis_geometry)
    picks.get_by_role("button", name="Alpha", exact=True).click()
    table = page.get_by_role("table", name="Each item")
    expect(table).to_contain_text("Alpha")
    expect(table).not_to_contain_text("Beta")
    expect(table).to_contain_text("$10.00")
    expect(page.get_by_text("Less spent $30.00", exact=True)).to_be_visible()
    expect(page.get_by_text("≤30%", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Mon 23:00 1000000 tokens", exact=True)).to_be_visible()
    expect(
        page.get_by_role("button", name="Claude token share 100.0%", exact=True)
    ).to_be_visible()
    page.get_by_role("button", name="Model", exact=True).click()
    picks.get_by_role("button", name="GPT", exact=True).click()
    expect(table).to_contain_text("GPT")
    assert_usage_geometry(page, analysis_geometry)
    expect(table).not_to_contain_text("Claude")
    expect(table).to_contain_text("Unknown")
    assert_usage_geometry(page, analysis_geometry)
    expect(
        page.get_by_role("figure", name="Cumulative selected tokens and paid")
    ).not_to_contain_text("Previous selected hourly series not available yet")
    page.get_by_role("button", name="Pool", exact=True).click()
    picks.get_by_role("button", name="Quota only", exact=True).click()
    expect(table).to_contain_text("Unknown")
    expect(page.get_by_role("button", name="GPT token share 100.0%", exact=True)).to_have_count(0)
    assert_usage_geometry(page, analysis_geometry)
    page.get_by_role("button", name="90 days", exact=True).click()
    expect(page.get_by_text("90 days has no previous period", exact=True)).to_have_count(2)
    assert_usage_geometry(page, analysis_geometry)
    page.get_by_role("button", name="Card", exact=True).click()
    cards = page.get_by_role("table", name="Purchased cards")
    expect(cards).to_contain_text("Alpha card")
    expect(cards).to_contain_text("$10.00")
    expect(cards).to_contain_text("$100.00")
    assert_usage_geometry(page, analysis_geometry)
    expect(page.get_by_role("region", name="Failure rate")).to_have_count(0)
    expect(page.get_by_role("group", name="Selected weekday and hour tokens")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
