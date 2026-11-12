"""The built task board's settings, sidebar, link safety and narrow-screen list."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_features as appearance_features
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url


@pytest.mark.parametrize(
    "width,theme", [(1440, "dark"), (1440, "light"), (390, "dark"), (390, "light")]
)
def test_task_board_display_switch_and_source_list(
    page: Page,
    appearance_url: str,
    browser_contract: BrowserContract,
    width: int,
    theme: str,
    request: pytest.FixtureRequest,
) -> None:
    browser_contract.json(
        "/v1/info",
        {
            "single_user": True,
            "accounts_enabled": False,
            "features": {},
            "task_board_enabled": True,
        },
    )
    settings: dict[str, object] = {"showTaskBoard": False}

    def preferences(_: Request) -> dict[str, object]:
        return {
            "user_id": "local",
            "is_admin": True,
            "preferences": {"version": 1, "settings": {"session_navigation": settings}},
        }

    def save(request: Request) -> dict[str, bool]:
        settings.clear()
        settings.update(request.post_data_json["value"] or {})
        return {"ok": True}

    browser_contract.json("/v1/me", preferences)
    browser_contract.json("/v1/me/preferences", preferences)
    browser_contract.json("/v1/me/preferences/session_navigation", save, method="PATCH")
    browser_contract.json(
        "/v1/agents/with-sessions", {"agent_ids": [], "template_by_agent_id": {}}
    )
    browser_contract.json(
        "/v1/me/preferences/keep_warm/convert", {"layers": {}, "already": True}, method="POST"
    )
    browser_contract.json(
        "/v1/system/settings",
        {"health_check": {"project_id": None, "host_id": None, "prompt": None}},
    )
    native_sessions = [
        {
            "id": "parent",
            "title": "Transfer reliability",
            "status": "running",
            "host_id": "workstation",
            "updated_at": time.time(),
        },
        {
            "id": "child",
            "title": "Check retry behaviour",
            "status": "waiting",
            "parent_session_id": "parent",
            "host_id": "workstation",
            "pending_elicitations_count": 1,
            "updated_at": time.time(),
        },
        {
            "id": "archived",
            "title": "Document recovery",
            "status": "idle",
            "archived": True,
            "archived_at": time.time(),
            "host_id": "workstation",
            "updated_at": time.time(),
        },
    ]
    board_session_queries: list[dict[str, list[str]]] = []

    def session_list(request: Request) -> dict[str, object]:
        query = parse_qs(urlparse(request.url).query)
        if query.get("kind") == ["any"]:
            board_session_queries.append(query)
            archived = "archived_only" in query
            rows = [row for row in native_sessions if bool(row.get("archived")) == archived]
        else:
            rows = []
        return {"data": rows, "has_more": False, "last_id": None, "first_id": None}

    browser_contract.json("/v1/sessions", session_list)
    for session in native_sessions:
        browser_contract.json(
            f"/v1/sessions/{session['id']}",
            {
                **session,
                "total_cost_usd": 0.75,
                "todos": [{"content": "Verify resumed chunks", "status": "in_progress"}],
            },
        )
    watch_frames: list[dict[str, object]] = []

    def socket(ws):
        def message(body):
            if not urlparse(ws.url).query:
                watch_frames.append(json.loads(body))

        ws.on_message(message)

    browser_contract.websocket("**/v1/sessions/updates*", socket)
    browser_contract.json("/v1/me/usage", {"accounts": []})
    browser_contract.json("/v1/scheduled-tasks", {"scheduled_tasks": []})
    source_name = "Example tracker"
    browser_contract.json(
        "/v1/me/tasks/sources",
        {
            "sources": [
                {
                    "id": "example",
                    "name": source_name,
                    "source": "https://tracker.example.com",
                    "created": "2026-11-06T06:30:00Z",
                    "last_write": None,
                    "counts": {"open": 1, "closed": 1, "gone": 0},
                    "issues": [],
                }
            ]
        },
    )
    browser_contract.json(
        "/v1/me/tasks",
        {
            "@type": "Group",
            "uid": "omnigent:tasks",
            "updated": "2026-11-06T06:30:00Z",
            "entries": [
                {
                    "@type": "Task",
                    "uid": "retry",
                    "updated": "2026-11-06T06:30:00Z",
                    "title": "Retry failed uploads",
                    "progress": "in-process",
                    "omnigent.ai:source": "example",
                    "omnigent.ai:key": "ENG-1",
                    "omnigent.ai:waiting": {"on": "user", "note": "Approve the retry budget"},
                    "omnigent.ai:next": "Check a slow connection",
                    "omnigent.ai:workedBy": [
                        {"session": "parent", "from": "2026-11-06T05:00:00Z"}
                    ],
                    "omnigent.ai:group": {"id": "v2", "name": "Version two"},
                    "relatedTo": {"drop": {"relation": {"next": True}}},
                    "links": {
                        "page": {
                            "@type": "Link",
                            "rel": "alternate",
                            "href": "https://tracker.example.com/retry",
                        }
                    },
                },
                {
                    "@type": "Task",
                    "uid": "drop",
                    "updated": datetime.now(UTC).isoformat(),
                    "title": "Drop the legacy endpoint",
                    "progress": "cancelled",
                    "omnigent.ai:source": "example",
                    "links": {
                        "page": {
                            "@type": "Link",
                            "rel": "alternate",
                            "href": "javascript:alert(1)",
                        }
                    },
                },
                {
                    "uid": "past",
                    "title": "Earlier recovery check",
                    "progress": "completed",
                    "omnigent.ai:source": "example",
                    "omnigent.ai:group": {"id": "past", "name": "Earlier checks"},
                    "progressUpdated": (datetime.now(UTC) - timedelta(days=180)).isoformat(),
                },
            ],
        },
    )
    browser_contract.json(
        "/v1/me/tasks/history",
        {
            "entries": [
                {
                    "id": "event-one",
                    "time": "2026-11-06T06:30:00Z",
                    "kind": "commit",
                    "note": "Keep confirmed chunks",
                    "source": "example",
                    "uid": "retry",
                }
            ]
        },
    )
    browser_contract.json(
        "/v1/me/tasks/closed-counts",
        {"days": [{"day": "2020-01-01", "source": "example", "closed": 2}]},
    )
    browser_contract.json(
        "/v1/me/tasks/item",
        lambda request: {
            "uid": "retry"
            if parse_qs(urlparse(request.url).query).get("uid") == ["retry"]
            else "drop",
            "title": "Retry failed uploads"
            if parse_qs(urlparse(request.url).query).get("uid") == ["retry"]
            else "Drop the legacy endpoint",
            "omnigent.ai:source": "example",
            "progress": "in-process"
            if parse_qs(urlparse(request.url).query).get("uid") == ["retry"]
            else "cancelled",
            **(
                {
                    "omnigent.ai:next": "Check a slow connection",
                    "omnigent.ai:waiting": {"on": "user", "note": "Approve the retry budget"},
                    "omnigent.ai:workedBy": [
                        {"session": "parent", "from": "2026-11-06T05:00:00Z"}
                    ],
                    "links": {
                        "page": {"rel": "alternate", "href": "https://tracker.example.com/retry"}
                    },
                    "omnigent.ai:history": [
                        {
                            "id": "commit",
                            "time": "2026-11-06T06:30:00Z",
                            "kind": "commit",
                            "note": "Keep confirmed chunks",
                        }
                    ],
                }
                if parse_qs(urlparse(request.url).query).get("uid") == ["retry"]
                else {}
            ),
        },
    )
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('web-theme', {json.dumps(theme)})")
    page.goto(appearance_url + "/task-board")
    page.get_by_role("link", name="Turn it on in Settings").click()
    toggle = page.get_by_role("switch", name="Show Task board")
    expect(toggle).not_to_be_checked()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH" and response.url.endswith("/session_navigation")
        )
    ):
        toggle.click()
    calls: list[Request] = []
    page.on(
        "request",
        lambda request: (
            calls.append(request) if urlparse(request.url).path.startswith("/v1/") else None
        ),
    )
    if width < 768:
        page.get_by_role("button", name="Back to settings menu", exact=True).click()
    page.get_by_role("link", name="Back", exact=True).click()
    expect(page).to_have_url(appearance_url + "/task-board")
    if width < 768:
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    page.get_by_role("link", name="Task board", exact=True).click()
    native = page.get_by_role("region", name="Sessions", exact=True)
    expect(native.get_by_text("Transfer reliability", exact=True)).to_be_visible()
    expect(native.locator('a[href="/c/child"]')).to_be_visible()
    if width >= 621:
        native.get_by_role(
            "button", name="Collapse child sessions: Transfer reliability", exact=True
        ).click()
        expect(native.locator('a[href="/c/child"]')).to_have_count(0)
        native.get_by_role(
            "button", name="Expand child sessions: Transfer reliability", exact=True
        ).click()
        expect(native.locator('a[href="/c/child"]')).to_be_visible()
    native.get_by_role("button", name="1 archived today", exact=True).click()
    expect(native.locator('a[href="/c/archived"]')).to_be_visible()
    native.get_by_role("button", name="Session details: Transfer reliability", exact=True).click()
    session_detail = native.get_by_role(
        "region", name="Session details: Transfer reliability", exact=True
    )
    expect(
        session_detail.get_by_text("No linked task was supplied for this session.")
    ).to_be_visible()
    session_detail.get_by_role("button", name="Close", exact=True).click()
    expect(native.get_by_text("Verify resumed chunks").first).to_be_visible()
    page.get_by_role("button", name="2 Waiting on me", exact=True).click()
    waiting = page.get_by_role("region", name="Waiting on me", exact=True)
    expect(waiting.get_by_text("Approve the retry budget")).to_be_visible()
    expect(waiting.locator('a[href="/c/child"]')).to_be_visible()
    waiting.get_by_role("button", name=re.compile("Approve the retry budget")).click()
    waiting_detail = waiting.get_by_role(
        "region", name="Details: Retry failed uploads", exact=True
    )
    expect(waiting_detail).to_be_visible()
    waiting_detail.get_by_role("button", name="Close", exact=True).click()
    waiting.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("navigation", name="Board views").get_by_role(
        "button", name="Routes", exact=True
    ).click()
    region = page.get_by_role("region", name="Routes", exact=True)
    expect(region.get_by_role("heading", name="Version two")).to_be_visible()
    region.get_by_role(
        "button", name=re.compile(r"^Retry failed uploads(?:\s+Waiting on you.*)?$")
    ).first.click()
    detail = page.get_by_role("region", name="Details: Retry failed uploads", exact=True)
    expect(detail.get_by_text("Check a slow connection")).to_be_visible()
    expect(detail.locator('a[href="https://tracker.example.com/retry"]')).to_be_visible()
    expect(detail.locator('a[href="/c/parent"]')).to_be_visible()
    expect(detail.locator(".tb-dc-tiles")).to_be_visible()
    expect(detail.locator('a[href="/c/parent"]')).to_have_attribute("target", "_blank")
    if width >= 621:
        expect(detail.locator(".tb-dc-body")).to_have_css(
            "grid-template-columns", re.compile(r".+ .+")
        )
    detail.get_by_role("button", name="Close", exact=True).click()
    region.get_by_role(
        "button", name=re.compile(r"^Drop the legacy endpoint(?:\s+Completed.*)?$")
    ).first.click()
    no_page = page.get_by_role("region", name="Details: Drop the legacy endpoint", exact=True)
    expect(no_page.locator("a[href^=javascript]")).to_have_count(0)
    expect(page.locator('a[href^="javascript:"]')).to_have_count(0)
    no_page.get_by_role("button", name="Close", exact=True).click()
    if width == 1440:
        edge = region.locator(".rt-edge-hit").first
        hit = edge.evaluate(
            """el => {
              const p = el.getPointAtLength(el.getTotalLength()/2), m = el.getScreenCTM();
              return {x:p.x*m.a+p.y*m.c+m.e, y:p.x*m.b+p.y*m.d+m.f};
            }"""
        )
        page.mouse.move(hit["x"], hit["y"])
        expect(page.get_by_role("tooltip")).to_contain_text("No relationship date supplied")
        page.mouse.click(hit["x"], hit["y"])
        relation = page.get_by_role("region", name="Related tasks", exact=True)
        expect(relation).to_contain_text("Predecessor / successor")
        relation.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="By time", exact=True).click()
    expect(region.locator(".rt-axis")).to_be_visible()
    retry_stop = region.locator('.rt-stop[data-route-task="retry"]')
    retry_stop.click()
    expect(detail).to_be_visible()
    expect(detail.locator(".tb-dc-tiles")).to_be_visible()
    assert detail.evaluate(
        "el => {const panel=el.closest('.rt-panel'), scene=el.closest('.rt-scene');"
        "const next=[...scene.querySelectorAll('.rt-lane')].find(lane=>"
        "lane.getBoundingClientRect().top>=panel.getBoundingClientRect().top);"
        "return !next || next.getBoundingClientRect().top>=el.getBoundingClientRect().bottom;}"
    )
    detail.get_by_role("button", name="Close", exact=True).click()
    expect(retry_stop).to_be_focused()
    overview = page.get_by_role(
        "region", name="All-history overview: drag the window or its edges"
    )
    expect(overview).to_be_visible()
    original = [region.get_attribute("data-window-from"), region.get_attribute("data-window-to")]
    page.get_by_role("button", name="Zoom out", exact=True).click()
    assert int(region.get_attribute("data-window-to")) == int(original[1])
    assert (
        int(region.get_attribute("data-window-to")) - int(region.get_attribute("data-window-from"))
        == 4 * 24 * 3600000
    )
    window_box = overview.locator(".rt-overview-window").bounding_box()
    assert window_box
    page.mouse.move(
        window_box["x"] + window_box["width"] / 2, window_box["y"] + window_box["height"] / 2
    )
    page.mouse.down()
    page.mouse.move(window_box["x"] - 40, window_box["y"] + window_box["height"] / 2, steps=5)
    page.mouse.up()
    expect(region).not_to_have_attribute(
        "data-window-from", str(int(original[0]) - 2 * 24 * 3600000)
    )
    page.locator(".rt-map").focus()
    page.locator(".rt-map").press("0")
    assert abs(int(region.get_attribute("data-window-from")) - int(original[0])) < 10000
    assert abs(int(region.get_attribute("data-window-to")) - int(original[1])) < 10000
    page.get_by_role("button", name="How to read this map", exact=True).click()
    expect(page.get_by_role("region", name="How to read this map")).to_contain_text(
        "Plain vertical scrolling"
    )
    page.get_by_role("button", name="How to read this map", exact=True).click()
    if width == 390:
        page.get_by_role("button", name="7D", exact=True).click()
        before_pinch = int(region.get_attribute("data-window-to")) - int(
            region.get_attribute("data-window-from")
        )
        region.scroll_into_view_if_needed()
        box = region.bounding_box()
        assert box
        cx = box["x"] + box["width"] / 2
        cy = max(80, min(760, box["y"] + 80))
        cdp = page.context.new_cdp_session(page)
        try:
            cdp.send(
                "Input.dispatchTouchEvent",
                {
                    "type": "touchStart",
                    "touchPoints": [
                        {"x": cx - 30, "y": cy, "id": 1},
                        {"x": cx + 30, "y": cy, "id": 2},
                    ],
                },
            )
            cdp.send(
                "Input.dispatchTouchEvent",
                {
                    "type": "touchMove",
                    "touchPoints": [
                        {"x": cx - 55, "y": cy, "id": 1},
                        {"x": cx + 55, "y": cy, "id": 2},
                    ],
                },
            )
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        finally:
            cdp.detach()
        assert (
            int(region.get_attribute("data-window-to"))
            - int(region.get_attribute("data-window-from"))
            < before_pinch
        )
        page.locator(".rt-map").focus()
        page.locator(".rt-map").press("0")
    graph = region.locator("svg").first
    assert graph.bounding_box()
    assert page.locator("main").last.evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )
    page.locator(".tb-page-header").scroll_into_view_if_needed()
    title = page.locator(".tb-page-header").bounding_box()
    header = page.get_by_role("banner").bounding_box()
    assert title and header and title["y"] >= header["y"] + header["height"]
    assert board_session_queries and all(
        query["kind"] == ["any"] for query in board_session_queries
    )
    assert watch_frames and all(frame["type"] == "watch" for frame in watch_frames)
    page.get_by_role("button", name="Log", exact=True).click()
    expect(
        page.get_by_role("region", name="Log").get_by_text("Keep confirmed chunks")
    ).to_be_visible()
    page.get_by_role("region", name="Log", exact=True).get_by_role(
        "button", name="By root session", exact=True
    ).click()
    expect(
        page.get_by_role("region", name="Log").get_by_role("heading", name="Transfer reliability")
    ).to_be_visible()
    log = page.get_by_role("region", name="Log", exact=True)
    log.get_by_role("button", name="Retry failed uploads", exact=True).click()
    log_detail = log.get_by_role("region", name="Details: Retry failed uploads", exact=True)
    expect(log_detail).to_be_visible()
    expect(log.get_by_text("Keep confirmed chunks")).to_be_visible()
    log_detail.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="History", exact=True).click()
    history = page.get_by_role("region", name="History", exact=True)
    history.get_by_role("button", name="1 year", exact=True).click()
    history.get_by_role("button", name=re.compile(r"\bexample\b")).click()
    history.get_by_role("button", name=re.compile("Earlier checks")).click()
    expect(history.get_by_text("Earlier recovery check", exact=True)).to_be_visible()
    old_requests: list[dict[str, list[str]]] = []

    def archived_page(route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        if "closed_before" not in query:
            route.fallback()
            return
        old_requests.append(query)
        second = "cursor" in query
        route.fulfill(
            json={
                "entries": [
                    {
                        "uid": "archived-second" if second else "archived-first",
                        "title": "Archived second check" if second else "Archived first check",
                        "omnigent.ai:source": "example",
                        "omnigent.ai:group": {"id": "archive", "name": "Archive checks"},
                        "progress": "completed",
                        "created": "2020-01-01T00:00:00Z",
                        "progressUpdated": "2020-01-01T02:00:00Z",
                    }
                ],
                **({} if second else {"omnigent.ai:cursor": "second-page"}),
            }
        )

    browser_contract.route("/v1/me/tasks", archived_page)
    history.get_by_role("button", name="Find by month", exact=True).click()
    history.get_by_label("Find month", exact=True).fill("2020-01")
    history.get_by_role("button", name="Load month", exact=True).click()
    expect(history.locator(".h-stat>b").first).to_have_text("2")
    history.get_by_role("button", name=re.compile(r"\bexample\b")).click()
    history.get_by_role("button", name=re.compile("Archive checks")).click()
    expect(history.get_by_text("Archived first check", exact=True)).to_be_visible()
    expect(history.get_by_text("Archived second check", exact=True)).to_be_visible()
    assert len(old_requests) == 2 and old_requests[1]["cursor"] == ["second-page"]
    history.get_by_role("button", name="Back to recent", exact=True).click()
    expect(history.get_by_text("Archived first check", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Today", exact=True).click()
    today = page.get_by_role("region", name="Today", exact=True)
    expect(today.get_by_role("heading", name=re.compile("Who worked today"))).to_be_visible()
    expect(today.get_by_text("Closed today", exact=True)).to_be_visible()
    expect(today.get_by_text("Task-linked commits", exact=True)).to_be_visible()
    page.get_by_role("button", name="Settings", exact=True).click()
    display = page.get_by_role("region", name="Settings", exact=True)
    display.get_by_role("button", name="Dark" if theme == "dark" else "Light", exact=True).click()
    display.get_by_role("button", name="Off", exact=True).click()
    expect(display.get_by_role("button", name="Off", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    assert display.get_by_role("button", name="Off", exact=True).evaluate(
        "el => getComputedStyle(el).backgroundColor"
    ) == ("rgb(24, 31, 37)" if theme == "dark" else "rgb(255, 255, 255)")
    assert display.locator(".tb-project-bullet").evaluate(
        "el => getComputedStyle(el).borderRadius === '50%' && "
        "getComputedStyle(el).backgroundColor === 'rgb(238, 53, 46)'"
    )
    display.get_by_label("Station level up to", exact=True).select_option("3")
    display.get_by_label("Board window", exact=True).select_option("72")
    display.get_by_role("button", name=source_name, exact=True).click()
    page.get_by_role("navigation", name="Board views").get_by_role(
        "button", name="Routes", exact=True
    ).click()
    expect(page.get_by_role("heading", name="Version two")).to_have_count(0)
    page.get_by_role("button", name="Settings", exact=True).click()
    display.get_by_role("button", name="Restore defaults", exact=True).click()
    expect(display.get_by_role("status")).to_contain_text("Saved in this browser")
    expect(page.locator("main").last).to_be_visible()
    assert page.locator("main").last.evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )
    assert calls and all(request.method == "GET" for request in calls)
    assert not errors
    output = Path(request.config.getoption("--output"))
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"task-board-{width}-{theme}.png"), full_page=True)
    if width == 390:
        browser_contract.json(
            "/v1/me/tasks",
            {"entries": [], "updated": "2026-11-06T06:30:00Z"},
        )
        browser_contract.json("/v1/me/tasks/history", {"entries": []})
        page.evaluate(
            "localStorage.setItem('omnigent.taskBoard.display', "
            "JSON.stringify({theme:'dark', defaultView:'log'}))"
        )
        for route in ("/task-board", "/frame/task-board"):
            page.goto(appearance_url + route)
            expect(page.get_by_text("No activity to show.", exact=True)).to_be_visible()
            measured = page.locator(".tb-page").evaluate(
                """board => {
                  const tabs = board.querySelector('.tb-view-tabs');
                  const b = board.getBoundingClientRect(), n = tabs.getBoundingClientRect();
                  return { boardBottom:b.bottom, navTop:n.top };
                }"""
            )
            page.screenshot(
                path=str(output / f"empty-{route.strip('/').replace('/', '-')}-dark.png")
            )
            assert measured["boardBottom"] >= measured["navTop"], measured
            navigation = page.get_by_role("navigation", name="Board views")
            expect(navigation).to_have_css("background-color", "rgb(24, 31, 37)")
            navigation.get_by_role("button", name="Sessions", exact=True).click()
            expect(page.get_by_role("region", name="Sessions", exact=True)).to_be_visible()
    observed = datetime.now(UTC).isoformat()
    ref = {"source": "example", "uid": "observations"}
    checks = [
        {"id": verdict, "host": f"host-{verdict}", "verdict": verdict, "count": 7, "at": observed}
        for verdict in ("passed", "failed", "flaky")
    ]
    browser_contract.json(
        "/v1/me/tasks",
        {
            "entries": [
                {
                    "uid": "observations",
                    "title": "Recovery observations",
                    "omnigent.ai:source": "example",
                    "omnigent.ai:sourceDisplay": {
                        "version": 1,
                        "state": "blocked",
                        "current": "Verify the recovery path",
                        "identity": {
                            "key": "ENG-2",
                            "label": "Recovery observations",
                            "groupId": "v2",
                            "groupLabel": "Version two",
                        },
                        "actors": [{"id": "owner", "agentId": "codex", "label": "Recovery owner"}],
                        "sessions": [
                            {
                                "id": "source-stale",
                                "task": ref,
                                "role": "root",
                                "agentId": "codex",
                                "label": "Source stale",
                                "title": "Source stale",
                                "status": "running",
                                "createdAt": observed,
                                "lastActivityAt": observed,
                                "idleSeconds": 1300,
                                "context": 0.9,
                                "steps": [
                                    {
                                        "id": "step",
                                        "kind": "you",
                                        "label": "Review recovered output",
                                        "state": "current",
                                    }
                                ],
                            },
                            {
                                "id": "source-archive",
                                "task": ref,
                                "role": "root",
                                "agentId": "codex",
                                "label": "Source archive",
                                "title": "Source archive",
                                "status": "archived",
                                "createdAt": observed,
                                "lastActivityAt": observed,
                                "archivedAt": observed,
                                "context": 0.85,
                            },
                        ],
                        "checks": checks,
                        "events": [
                            {
                                "id": check["id"],
                                "at": observed,
                                "kind": "check",
                                "task": ref,
                                "checkId": check["id"],
                            }
                            for check in checks
                        ],
                    },
                }
            ],
            "updated": observed,
        },
    )
    page.evaluate(
        "localStorage.setItem('omnigent.taskBoard.display', "
        f"JSON.stringify({{theme:{json.dumps(theme)}, defaultView:'sessions'}}))"
    )
    page.goto(appearance_url + "/task-board")
    marker = page.locator('[data-session-id="source-stale"] .tb-session-station.current')
    danger = "rgb(231, 98, 125)" if theme == "dark" else "rgb(193, 49, 74)"
    expect(marker).to_have_css("background-color", danger)
    expect(marker).to_have_css("border-top-color", danger)
    expect(marker).to_have_css("color", "rgb(255, 255, 255)")
    failed = page.get_by_role("button", name=re.compile("host-failed failed"))
    expect(failed.locator(".tb-recent-kind")).to_have_css("color", danger)
    flaky = page.get_by_role("button", name=re.compile("host-flaky flaky"))
    expect(flaky.locator(".tb-recent-kind")).to_have_css(
        "color", "rgb(212, 151, 42)" if theme == "dark" else "rgb(129, 95, 34)"
    )
    if width >= 621:
        page.get_by_role("button", name="Session details: Source stale", exact=True).hover()
        expect(page.get_by_role("tooltip").locator(".tb-session-context b.hot")).to_have_css(
            "background-color", danger
        )
        page.get_by_role("button", name="2 archived today", exact=True).click()
        page.get_by_role("button", name="Session details: Source archive", exact=True).hover()
        expect(page.get_by_role("tooltip")).to_contain_text("Archived")
        expect(page.get_by_role("tooltip")).to_contain_text(str(datetime.now(UTC).year))
        expect(page.get_by_role("tooltip").locator(".tb-session-context b.hot")).to_have_count(0)
        failed.focus()
        expect(page.get_by_role("tooltip")).to_contain_text("Recovery owner")
        expect(page.get_by_role("tooltip")).to_contain_text("Verify the recovery path")
        expect(page.get_by_role("tooltip")).to_contain_text("host-failed failed")
        page.keyboard.press("Escape")
        expect(page.get_by_role("tooltip")).to_have_count(0)
    navigation_checks = []
    for boundary in ["source", "group", "focus"] + (["phone"] if width == 390 else []):
        count = {"source": 13, "group": 11, "focus": 2, "phone": 9}[boundary]
        navigation_tasks = [
            {
                "uid": f"nav-{index}",
                "title": f"Navigation task {index + 1}",
                "created": observed,
                "updated": observed,
                "progress": "in-process",
                "omnigent.ai:source": f"source-{index}" if boundary == "source" else "example",
                "omnigent.ai:group": {
                    "id": "shared" if boundary == "phone" else f"group-{index}",
                    "name": "Shared group" if boundary == "phone" else f"Group {index + 1}",
                },
                "omnigent.ai:sourceDisplay": {
                    "version": 1,
                    "state": "running",
                    "lastActivity": observed,
                },
            }
            for index in range(count)
        ]
        first, last = navigation_tasks[0], navigation_tasks[-1]
        first["omnigent.ai:sourceDisplay"]["relations"] = [
            {
                "id": "navigation",
                "from": {"source": first["omnigent.ai:source"], "uid": first["uid"]},
                "to": {"source": last["omnigent.ai:source"], "uid": last["uid"]},
                "kind": "dependency",
                "origin": "manual",
                "provenance": "Example dependency",
            }
        ]
        source_ids = list(dict.fromkeys(task["omnigent.ai:source"] for task in navigation_tasks))
        browser_contract.json(
            "/v1/me/tasks/sources",
            {
                "sources": [
                    {
                        "id": source,
                        "name": source,
                        "source": None,
                        "created": observed,
                        "issues": [],
                    }
                    for source in source_ids
                ]
            },
        )
        browser_contract.json("/v1/me/tasks", {"entries": navigation_tasks, "updated": observed})
        browser_contract.json("/v1/me/tasks/history", {"entries": []})
        browser_contract.json(
            "/v1/me/tasks/item",
            lambda incoming, items=navigation_tasks: next(
                task
                for task in items
                if task["uid"] == parse_qs(urlparse(incoming.url).query)["uid"][0]
            ),
        )
        preferences = {
            "theme": theme,
            "defaultView": "routes",
            "defaultMode": "order",
            "refresh": 0,
        }
        if boundary == "focus":
            preferences["focus"] = [json.dumps(["example", "group-0"], separators=(",", ":"))]
        page.evaluate(
            "value => localStorage.setItem('omnigent.taskBoard.display', JSON.stringify(value))",
            preferences,
        )
        page.goto(appearance_url + "/task-board")
        origin, target = (last, first) if boundary == "source" else (first, last)
        trigger = page.locator(f'button[data-route-task="{origin["uid"]}"]')
        expect(trigger).to_be_visible()
        expect(page.locator(f'button[data-route-task="{target["uid"]}"]')).to_have_count(0)
        trigger.click()
        current_detail = page.get_by_role("region", name=f"Details: {origin['title']}", exact=True)
        expect(current_detail).to_be_visible()
        anchor_top = current_detail.evaluate("el => el.closest('.rt-panel')?.style.top ?? null")
        current_detail.get_by_role("button", name=re.compile(re.escape(target["title"]))).click()
        destination = page.get_by_role("region", name=f"Details: {target['title']}", exact=True)
        expect(destination).to_be_visible()
        if width == 390:
            assert (
                destination.evaluate(
                    "el => el.closest('li').querySelector(':scope > button').dataset.routeTask"
                )
                == origin["uid"]
            )
        else:
            assert anchor_top != "0px"
            assert destination.evaluate("el => el.closest('.rt-panel').style.top") == anchor_top
        expect(page.locator(f'button[data-route-task="{target["uid"]}"]')).to_have_count(0)
        page.screenshot(
            path=str(output / f"route-related-{boundary}-{width}-{theme}.png"), full_page=True
        )
        destination.get_by_role("button", name="Close", exact=True).click()
        expect(trigger).to_be_focused()
        navigation_checks.append(boundary)
    print("ROUTE_RELATED_NAVIGATION", width, theme, navigation_checks, flush=True)
    source_ids = [f"project-{index:02d}" for index in range(1, 11)]
    browser_contract.json(
        "/v1/me/tasks/sources",
        {
            "sources": [
                {"id": source, "name": source, "source": None, "created": observed, "issues": []}
                for source in source_ids
            ]
        },
    )
    closed_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    browser_contract.json(
        "/v1/me/tasks",
        {
            "entries": [
                {
                    "uid": "closed-check",
                    "title": f"Closed check in {source}",
                    "omnigent.ai:source": source,
                    "omnigent.ai:group": {"id": "checks", "name": "Recovery checks"},
                    "progress": "completed",
                    "created": closed_at,
                    "progressUpdated": closed_at,
                }
                for source in source_ids
            ],
            "updated": observed,
        },
    )
    browser_contract.json("/v1/me/tasks/closed-counts", {"days": []})
    page.evaluate(
        "value => localStorage.setItem('omnigent.taskBoard.display', JSON.stringify(value))",
        {"theme": theme, "defaultView": "history", "refresh": 0},
    )
    page.goto(appearance_url + "/task-board")
    names = page.locator(".h-p .h-name b")
    expect(names).to_have_text(source_ids)
    if width == 390:
        for row in page.locator(".h-p").all():
            row.evaluate("el => el.scrollIntoView({block: 'center'})")
            assert row.evaluate(
                "el => { const r = el.getBoundingClientRect(); return el.contains("
                "document.elementFromPoint(r.x + r.width/2, r.y + r.height/2)); }"
            )
        last = page.locator(".h-p").last.bounding_box()
        navigation = page.locator(".tb-view-tabs").bounding_box()
        assert last and navigation and last["y"] + last["height"] <= navigation["y"]
        print(
            "HISTORY_SOURCE_SCROLL",
            theme,
            json.dumps({"last": last, "navigation": navigation}),
            flush=True,
        )
        page.screenshot(path=str(output / f"history-source-scroll-{width}-{theme}.png"))
    assert not errors
