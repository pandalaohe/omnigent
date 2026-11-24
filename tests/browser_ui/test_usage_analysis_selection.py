"""URL-cohort and polling contracts through the built SPA in both shells."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_features as appearance_features
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url
from tests.browser_ui.usage_long_names import LONG_NAMES

NOW = datetime(2026, 11, 19, 16, tzinfo=UTC)
DIMENSIONS = ("account", "model", "host", "agent")


@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "timezone_id": "UTC"}


def analysis_payload(params, now):
    echo = {
        name: params.get(name, [None])[0]
        for name in ("range", "group", "day", "slot", "from", "to", "tz")
    }
    echo.update({name: sorted(params.get(name, [])) for name in DIMENSIONS})
    rows = [
        {
            "account": "plan-0",
            "host": "example-host",
            "agent": "claude",
            "model": "Sonnet",
            "tokens": 100,
        },
        {
            "account": "plan-0",
            "host": "example-host",
            "agent": "codex",
            "model": LONG_NAMES["model"],
            "tokens": 300,
        },
        {"account": "plan-1", "host": "other-host", "agent": "", "model": "Sonnet", "tokens": 500},
    ]

    def cohort(ignore=None):
        return [
            row
            for row in rows
            if all(
                name == ignore or not echo[name] or row[name] in echo[name] for name in DIMENSIONS
            )
        ]

    def value(amount):
        return {
            "state": "exact",
            "low": str(amount),
            "high": str(amount),
            "currency": "USD",
            "reason": None,
            "formatted": {"low": f"{amount:.2f}", "high": f"{amount:.2f}"},
        }

    def amounts(selected):
        tokens = sum(row["tokens"] for row in selected)
        return {
            "tokens": tokens,
            "requests": tokens // 10,
            "failed": 0,
            "coverage": "complete",
            "last_at": now if tokens else None,
            "api_value": value(tokens / 100),
            "paid": value(3),
            "payback": value(2),
        }

    def facet(dimension):
        unfiltered = cohort(ignore=dimension)
        return [
            {
                "key": key,
                "selected": key in echo[dimension],
                **amounts([row for row in unfiltered if row[dimension] == key]),
            }
            for key in sorted({row[dimension] for row in unfiltered})
        ]

    totals = amounts(cohort())
    span = {"requested_from": now - 86400, "counted_from": now - 86400, "until": now}
    return {
        "filter": echo,
        "generated_at": now,
        "tz": echo["tz"],
        "span": span,
        "complete": True,
        "totals": totals,
        "series": [{"at": now - 3600, **totals}],
        "groups": facet(echo["group"]),
        "facets": {name: facet(name) for name in DIMENSIONS},
        "heat": [{"slot": "2-14", **totals}],
        "days": [],
        "classes": {
            "basis": "price_table_estimate",
            "items": [],
            "unpriced_tokens": {},
            "unsplit_tokens": 0,
            "unsplit_cost": {"low": "0", "high": "0"},
        },
        "cache_savings": {"state": "unknown"},
    }


@pytest.fixture
def analysis_reads(page: Page, browser_contract: BrowserContract):
    reads = []
    now = int(NOW.timestamp())
    browser_contract.json("/v1/info", {"usage_board_enabled": True, "features": {}})
    browser_contract.json("/v1/model-connections", [])
    browser_contract.json(
        "/v1/me/usage",
        {
            "generated_at": now,
            "accounts": [
                {
                    "id": "plan-0",
                    "display_name": "Example plan",
                    "hosts": [{"host_id": "example-host", "name": "Example Host", "read_at": now}],
                },
                {
                    "id": "plan-1",
                    "display_name": LONG_NAMES["account"],
                    "hosts": [{"host_id": "other-host", "name": "Other Host"}],
                },
            ],
        },
    )

    def analysis(request):
        params = parse_qs(urlparse(request.url).query, keep_blank_values=True)
        reads.append(params)
        if "group" not in params:
            return {"presentation": {"quota_closings": []}}
        return analysis_payload(params, now)

    browser_contract.json("/v1/me/usage/analysis", analysis)
    page.add_init_script(
        "localStorage.setItem('omnigent:usage-context-preferences',"
        "JSON.stringify({version:1,board:{enabled:true}}));"
        "window.analysisVisibility = 'visible';"
        "Object.defineProperty(document, 'visibilityState', "
        "{get: () => window.analysisVisibility});"
    )
    page.clock.install(time=NOW)
    return reads


@pytest.mark.parametrize(("prefix", "width"), [("", 1280), ("/frame", 390)])
def test_analysis_selection(page: Page, appearance_url: str, analysis_reads, prefix, width):
    page.set_viewport_size({"width": width, "height": 900})
    base = appearance_url + prefix + "/usage/board/analysis"
    canonical = (
        "range=d7&group=model&model=Sonnet&host=example-host&day=2026-11-17&slot=2-14&theme=dark"
    )
    page.goto(
        base + "?theme=dark&slot=2-14&host=example-host&model=Sonnet"
        "&day=2026-11-17&group=model&range=d7"
    )
    expect(page).to_have_url(base + "?" + canonical)
    results = page.get_by_test_id("analysis-results")
    totals = results.get_by_role("region", name="Totals")
    expect(totals.get_by_text("$1.00", exact=True)).to_be_visible()
    expect(totals.get_by_text("Unknown", exact=True)).to_have_count(2)
    expected_request = {
        "range": ["d7"],
        "group": ["model"],
        "model": ["Sonnet"],
        "host": ["example-host"],
        "day": ["2026-11-17"],
        "slot": ["2-14"],
        "tz": ["UTC"],
        "include": ["previous"],
    }
    assert analysis_reads == [expected_request]
    before = totals.inner_text()
    page.reload()
    expect(totals.get_by_text("$1.00", exact=True)).to_be_visible()
    assert totals.inner_text() == before
    assert analysis_reads == [expected_request, expected_request]
    table = results.get_by_role("table", name="Each item")
    sonnet = table.get_by_role("button", name="Sonnet", exact=True)
    other = table.get_by_role("button", name=LONG_NAMES["model"], exact=True)
    expect(sonnet).to_have_attribute("aria-pressed", "true")
    expect(other).to_be_visible()
    sonnet.click()
    expect(sonnet).to_have_attribute("aria-pressed", "false")
    expect(totals.get_by_text("$4.00", exact=True)).to_be_visible()
    sonnet.click()
    expect(sonnet).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Clear filters").click()
    expect(page).to_have_url(base + "?range=d7&group=model&theme=dark")
    expect(totals.get_by_text("$9.00", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("navigation", name="Analysis sections").get_by_role(
        "button", name="When usage is highest", exact=True
    ).click()
    slot = page.get_by_role("button", name="Tue 14:00 · 900 tokens", exact=True)
    slot.click()
    expect(slot).to_have_attribute("aria-pressed", "true")
    slot.click()
    expect(slot).to_have_attribute("aria-pressed", "false")


def test_analysis_polls_visible_and_keeps_folded_reads_idle(
    page: Page, appearance_url: str, analysis_reads
):
    page.goto(appearance_url + "/frame/usage/board/cost?range=d30&group=account")
    expect(page).to_have_url(
        appearance_url + "/frame/usage/board/analysis?range=d30&group=account"
    )
    expect(page.get_by_test_id("analysis-results")).to_be_visible()
    assert len(analysis_reads) == 1
    page.clock.fast_forward(298_000)
    assert len(analysis_reads) == 1
    with page.expect_response(lambda response: "/v1/me/usage/analysis?" in response.url):
        page.clock.fast_forward(4_000)
    expect(page.get_by_test_id("analysis-results")).to_be_visible()
    assert len(analysis_reads) == 2
    page.evaluate(
        "window.analysisVisibility = 'hidden'; "
        "document.dispatchEvent(new Event('visibilitychange'))"
    )
    page.clock.fast_forward(600_000)
    assert len(analysis_reads) == 2
    page.evaluate(
        "window.analysisVisibility = 'visible'; "
        "document.dispatchEvent(new Event('visibilitychange'))"
    )
    # Returning to a visible tab may read a stale cohort immediately.
    assert all(params.get("include") == ["previous"] for params in analysis_reads)
    with page.expect_response(lambda response: "dimension=account" in response.url):
        page.get_by_role("navigation", name="Analysis sections").get_by_role(
            "button", name="Unused last period", exact=True
        ).click()
    expect(page.get_by_text("No eligible closing observation in this range")).to_be_visible()
    assert sum("dimension" in params for params in analysis_reads) == 1
    page.locator("#analysis-remaining > summary").click()
    page.clock.fast_forward(301_000)
    expect(page.get_by_test_id("analysis-results")).to_be_visible()
    assert sum("dimension" in params for params in analysis_reads) == 1
