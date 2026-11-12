"""Analysis selection through the real built SPA and browser transport."""

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_features as appearance_features
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url


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
            "contributes_usage": True,
            "contributes_money": True,
            "attributed": True,
            "coverage": "complete",
            "reason": None,
        }
        for key, model, tokens, failed in [("a", "M1", 1000000, 0), ("b", "M2", 3000000, 30)]
    ]
    browser_contract.json("/v1/info", {"usage_board_enabled": True, "features": {}})
    browser_contract.json("/v1/me/usage", {"generated_at": now, "accounts": accounts})
    browser_contract.json("/v1/me/usage/activity", {"generated_at": now, "accounts": []})
    browser_contract.json(
        "/v1/me/usage/analysis",
        {
            "generated_at": now,
            "span": {"requested_from": now - 86400, "counted_from": now - 86400, "until": now},
            "pool_catalog": [
                {
                    "parent_account_id": "a",
                    "pool_id": "q",
                    "display_name": "Quota only",
                    "usage_available": False,
                }
            ],
            "rows": rows,
            "partial": False,
            "truncated": False,
        },
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(
        "localStorage.setItem('omnigent:usage-context-preferences',JSON.stringify({version:1,board:{enabled:true}}))"
    )
    page.goto(appearance_url + "/frame/usage/board/cost")
    picks = page.get_by_role("group", name="Select items")
    expect(picks.get_by_role("button", name="Alpha", exact=True)).to_be_visible()
    picks.get_by_role("button", name="Alpha", exact=True).click()
    table = page.get_by_role("table", name="Each item")
    expect(table).to_contain_text("Alpha")
    expect(table).not_to_contain_text("Beta")
    expect(page.get_by_role("button", name="M1 token share 100.0%", exact=True)).to_be_visible()
    page.get_by_role("button", name="Model", exact=True).click()
    picks.get_by_role("button", name="M2", exact=True).click()
    expect(table).to_contain_text("M2")
    expect(table).not_to_contain_text("M1")
    page.get_by_role("button", name="Pool", exact=True).click()
    picks.get_by_role("button", name="Quota only", exact=True).click()
    expect(table).to_contain_text("Unknown")
    expect(page.get_by_role("button", name="M2 token share 100.0%", exact=True)).to_have_count(0)
    page.get_by_role("button", name="90 days", exact=True).click()
    expect(page.get_by_text("90 days has no previous period", exact=True)).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
