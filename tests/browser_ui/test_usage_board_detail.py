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
from tests.browser_ui.usage_long_names import LONG_NAMES


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
    accounts[1]["display_name"] = LONG_NAMES["account"]
    long_window = LONG_NAMES["window"]
    accounts[1]["windows"][1]["label"] = long_window
    accounts[1]["windows"][2]["group"] = LONG_NAMES["pool"]
    accounts[1].update(
        plan=LONG_NAMES["plan"],
        band=LONG_NAMES["band"],
        source=LONG_NAMES["source"],
        models=[{"id": LONG_NAMES["model"]}],
        hosts=[
            {"host_id": "host-1", "name": LONG_NAMES["host"], "read_at": now, "windows": [window]}
        ],
        balances=[
            {
                "id": "large",
                "remaining": 1234567890123.45,
                "unit": "USD",
                "label": LONG_NAMES["balance"],
                "group": LONG_NAMES["pool"],
                "note": LONG_NAMES["note"],
            }
        ],
    )
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
                    "models": [{"model": LONG_NAMES["model"], **totals}],
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
                "name": LONG_NAMES["card"],
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
        "price_overrides": [],
        "current_prices": {
            "pricing-model": {
                "imported": {"read": "2", "write": "4", "cache_read": "0.2", "cache_write": "2.5"},
                "builtin": None,
            }
        },
    }
    browser_contract.json("/v1/me/usage/pricing-baseline", pricing)
    browser_contract.json(
        "/v1/me/usage/pricing-baseline/model-price", {"imported": None, "builtin": None}
    )
    browser_contract.json(
        "/v1/me/usage/pricing-baseline",
        lambda request: {
            **request.post_data_json,
            "revision": 2,
            "current_prices": pricing["current_prices"],
        },
        method="PUT",
    )
    browser_contract.json("/v1/me/usage/lines", {"schema": 1, "tiles": {}})
    browser_contract.json("/v1/me/session-choice", {"settings": None, "revision": "sample"})
    browser_contract.json(
        "/v1/me/session-choice/meters", {"meters": [], "running": {}, "floors": {}}
    )
    browser_contract.json(
        "/v1/me/usage/models",
        {
            "display_name": LONG_NAMES["account"],
            "span": None,
            "models": [
                {
                    "id": LONG_NAMES["model"],
                    "pricing": None,
                    "usage": None,
                    "sources": [
                        {
                            "kind": "host",
                            "label": LONG_NAMES["host"],
                            "configuration": {
                                "label": LONG_NAMES["pool"],
                                "name": LONG_NAMES["source"],
                            },
                        }
                    ],
                }
            ],
        },
    )
    browser_contract.json(
        "/v1/me/usage/issues",
        {
            "failures": [{"account": "plan-1", **totals}],
            "unpriced_models": [{"account": "plan-1", "model": LONG_NAMES["model"], **totals}],
            "source_reports": [{"account": "plan-1", "kind": "read_failed", "at": now}],
        },
    )
    browser_contract.json(
        "/v1/me/usage/price-source",
        {
            "source": "builtin",
            "can_edit": False,
            "model_count": 0,
            "fetched_at": None,
            "error": None,
        },
    )
    browser_contract.json("/v1/me/preferences", {"preferences": {"version": 1, "settings": {}}})
    browser_contract.json("/v1/me/preferences/usage_context", {"ok": True}, method="PATCH")
    browser_contract.json(
        "/v1/extensions",
        {
            "object": "list",
            "data": [
                {
                    "id": "example.pools",
                    "status": "enabled",
                    "display_name": LONG_NAMES["source"],
                    "pages": [
                        {"id": "example.pools.main", "title": LONG_NAMES["pool"], "route": "main"}
                    ],
                }
            ],
        },
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(
        "localStorage.setItem('omnigent:usage-context-preferences',"
        + json.dumps(
            json.dumps(
                {
                    "version": 1,
                    "board": {
                        "enabled": True,
                        "bands": [
                            {
                                "id": "long-band",
                                "name": LONG_NAMES["band"],
                                "accounts": [item["id"] for item in accounts],
                                "open": True,
                            }
                        ],
                    },
                    "shownModels": {"plan-1": LONG_NAMES["model"]},
                }
            )
        )
        + ")"
    )
    name_views = []

    def names_in_view(view, names):
        for name in names:
            own = page.get_by_text(name, exact=False).filter(visible=True)
            expect(own.first).to_be_visible()
        name_views.append({"view": view, "width": width, "names": list(names)})

    page.goto(appearance_url + "/frame/usage/board")
    first = page.get_by_test_id("usage-tile").first
    first.get_by_role("link", name="Example plan 0 Pool B", exact=True).focus()
    page.keyboard.press("Enter")
    expect(page.locator("[data-pool]").first).to_have_attribute("data-pool", "Pool B")
    assert_usage_geometry(
        page,
        {
            '[data-testid="usage-ring"]': 4,
            '[data-testid="usage-figure"]': 2,
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
            '[data-testid="usage-ring"]': 7,
            '[data-testid="usage-figure"]': 4,
            "[data-usage-amount]": 4,
            "[data-usage-label]": 7,
        },
    )
    page.get_by_text("Board tools", exact=True).click()
    page.get_by_role("button", name="Compact view", exact=True).click()
    # The density change persists the product-normalized group name.
    shown_names = {
        **LONG_NAMES,
        "band": page.evaluate(
            "JSON.parse(localStorage.getItem('omnigent:usage-context-preferences'))"
            ".board.bands.find(band => band.id === 'long-band').name"
        ),
    }
    assert_usage_geometry(
        page,
        {
            '[data-testid="usage-tile"]': 3,
            '[data-testid="usage-ring"]': 3,
            '[data-testid="usage-figure"]': 0,
            "[data-usage-amount]": 3,
            # Three balance labels plus the window name inside each read compact ring.
            "[data-usage-label]": 5,
        },
    )
    names_in_view(
        "compact", [shown_names[key] for key in ["account", "band", "plan", "model", "balance"]]
    )
    page.get_by_role("button", name="Rings view", exact=True).click()
    names_in_view(
        "rings",
        [shown_names[key] for key in ["account", "band", "pool", "plan", "window", "balance"]],
    )
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
            '[data-testid="usage-ring"]': max(1, len(item["windows"]) - amount_windows),
            '[data-testid="usage-figure"]': balances + amount_windows,
            "[data-usage-amount]": balances + amount_windows,
            "[data-usage-label]": len(item["windows"]) + balances,
        }
        expect(page.get_by_text("Hosts", exact=True)).to_be_visible()
        assert_usage_geometry(page, expected)
        visited.append({"account": item["id"], "view": "detail", "width": width})
        if item["id"] == "plan-0":
            names_in_view("detail-card", [LONG_NAMES["card"]])
        if item["id"] == "plan-1":
            expect(page.locator("[data-usage-percent]").filter(has_text="100.00%")).to_be_visible()
            expect(
                page.locator("[data-usage-amount]").filter(has_text="$1,234,567,890,123.45")
            ).to_have_count(balances)
            page.get_by_text("Hosts", exact=True).click()
            page.get_by_text("Usage tools", exact=True).click()
            page.get_by_text("By model", exact=True).click()
            page.get_by_text("Cost breakdown", exact=True).click()
            names_in_view(
                "detail",
                [
                    LONG_NAMES[key]
                    for key in [
                        "account",
                        "pool",
                        "window",
                        "host",
                        "model",
                        "balance",
                        "note",
                    ]
                ],
            )
            window_tip = (
                page.locator("[data-usage-window]")
                .filter(has=page.get_by_title(long_window, exact=True))
                .locator(".usage-ring-trigger")
            )
            window_tip.click()
            tip = page.get_by_test_id("usage-tip-card")
            expect(tip.get_by_title(long_window, exact=True)).to_be_visible()
            assert_usage_geometry(page, expected)
            names_in_view("window-tooltip", [long_window])
            page.keyboard.press("Escape")
            expect(tip).to_have_count(0)
        page.get_by_role("link", name="Account settings").click()
        expect(page.get_by_test_id("usage-tile")).to_have_count(1)
        expect(page.get_by_test_id("usage-tile")).to_have_attribute("data-account", item["id"])
        expected['[data-testid="usage-tile"]'] = 1
        expected["[data-usage-label]"] = (
            len({window["group"] for window in item["windows"]}) + balances
        )
        for choice in [long_window, "Auto"] if item["id"] == "plan-1" else [None]:
            if choice is not None:
                radio = page.get_by_role("radio", name=choice, exact=True)
                expect(radio).to_have_attribute("title", choice)
                radio.click()
                expect(radio).to_have_attribute("aria-checked", "true")
            selected = page.get_by_role("radio", checked=True)
            selected_label = selected.get_attribute("title") if selected.count() else None
            selected_id = next(
                (
                    window["id"]
                    for window in item["windows"]
                    if (window.get("label") or window["id"]) == selected_label
                ),
                None,
            )
            groups = {}
            for window in item["windows"]:
                groups.setdefault(window.get("group") or "", []).append(window)
            expected['[data-testid="usage-figure"]'] = balances + amount_windows
            expected['[data-testid="usage-ring"]'] = max(
                1,
                sum(
                    int(
                        next(window for window in windows if window["id"] == selected_id)[
                            "percent_used"
                        ]
                        is not None
                    )
                    if any(window["id"] == selected_id for window in windows)
                    else sum(window["percent_used"] is not None for window in windows)
                    for windows in groups.values()
                ),
            )
            assert_usage_geometry(page, expected)
            visited.append(
                {
                    "account": item["id"],
                    "view": "settings",
                    "width": width,
                    "selected_window": selected_id,
                    "expected_rings": expected['[data-testid="usage-ring"]'],
                }
            )
            if item["id"] == "plan-1":
                names_in_view(
                    "settings-" + choice,
                    [
                        LONG_NAMES[key]
                        for key in [
                            "account",
                            "window",
                            "source",
                            "balance",
                        ]
                    ]
                    + (["Pinned " + long_window] if selected_id else []),
                )
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
        '[data-testid="usage-figure"]': 2,
        "[data-usage-amount]": 2,
        "[data-usage-label]": 4,
    }
    assert_usage_geometry(page, settings_geometry)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    record_property("usage_views", json.dumps(visited))

    page.get_by_text("Pricing", exact=True).click()
    expect(page.get_by_label("Purchased card", exact=True)).to_have_attribute(
        "title", LONG_NAMES["card"] + " · 2026-11-02"
    )
    expect(page.get_by_label("Card name", exact=True)).to_have_attribute(
        "title", LONG_NAMES["card"]
    )
    name_views.append(
        {"view": "pricing", "width": width, "names": [LONG_NAMES["card"]], "native_control": True}
    )
    expect(page.get_by_label("Card payment preview")).to_contain_text("HKD 80.00 · USD 10.26")
    page.get_by_label("Model id", exact=True).fill("pricing-model")
    page.get_by_role("button", name="Add model price", exact=True).click()
    account_read = page.get_by_label("pricing-model · Read", exact=True)
    expect(account_read).to_have_attribute("placeholder", "2.00")
    account_read.fill("3.000000000000000001")
    account_read.blur()
    expect(account_read).to_have_value("3.00")
    page.get_by_label("Rate model (optional)", exact=True).fill("pricing-model")
    card_read = page.get_by_label("Read", exact=True)
    card_read.focus()
    expect(card_read).to_have_value("2")
    card_read.fill("")
    expect(card_read).to_have_value("")
    expect(card_read).to_have_attribute("placeholder", "3.00")
    card_write = page.get_by_label("Write", exact=True)
    card_write.focus()
    expect(card_write).to_have_value("4")
    card_write.fill("0")
    expect(card_write).to_have_value("0")
    field = page.get_by_label("Card discount", exact=True)
    field.fill("0.6")
    field.blur()
    expect(page.get_by_label("Card payment preview")).to_contain_text("HKD 60.00 · USD 7.69")
    with page.expect_request(
        lambda req: req.method == "PUT" and req.url.endswith("/usage/pricing-baseline")
    ) as saved_request:
        page.get_by_role("button", name="Save pricing", exact=True).click()
    assert saved_request.value.post_data_json["cards"][0]["discount_override"] == "0.6"
    assert saved_request.value.post_data_json["cards"][0]["rates_usd_per_million"]["read"] is None
    assert saved_request.value.post_data_json["cards"][0]["rates_usd_per_million"]["write"] == "0"
    assert (
        saved_request.value.post_data_json["price_overrides"][0]["read"] == "3.000000000000000001"
    )
    assert (
        saved_request.value.post_data_json["cards"][0]["retail_amount"] == "100.000000000000000001"
    )
    expect(page.get_by_text("Pricing saved. History recalculated.")).to_be_visible()
    assert_usage_geometry(page, settings_geometry)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.goto(appearance_url + "/frame/usage/board/plan-1/models")
    expect(page.get_by_role("table")).to_be_visible()
    names_in_view(
        "models", [LONG_NAMES[key] for key in ["account", "model", "host", "pool", "source"]]
    )
    assert_usage_geometry(page, {"table": 1})
    page.goto(appearance_url + "/frame/usage/board/plan-1/issues")
    expect(page.get_by_role("region", name="Failures", exact=True)).to_be_visible()
    names_in_view("issues", [LONG_NAMES[key] for key in ["account", "model"]])
    assert_usage_geometry(page, {".usage-surface section": 3})
    page.goto(appearance_url + "/settings/general#usage-settings")
    usage_settings = page.get_by_test_id("settings-group-usage-settings")
    expect(usage_settings).to_be_visible()
    expect(
        usage_settings.get_by_role(
            "switch", name="Show " + LONG_NAMES["account"] + " on the board", exact=True
        )
    ).to_be_visible()
    pools_page = usage_settings.get_by_label("Model pools page", exact=True)
    pools_page.select_option("/extensions/example.pools/main")
    expect(pools_page).to_have_attribute(
        "title", LONG_NAMES["source"] + " · " + LONG_NAMES["pool"]
    )
    names_in_view("board-settings", [LONG_NAMES["account"]])
    assert_usage_geometry(page, {'[data-testid="settings-group-usage-settings"]': 1})
    record_property("usage_name_views", json.dumps(name_views))
