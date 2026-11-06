"""The built task board's settings, sidebar, link safety and narrow-screen list."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url


@pytest.mark.parametrize("width,theme", [(1440, "dark"), (1440, "light"), (390, "dark")])
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
    settings: dict[str, object] = {}

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
                    "updated": "2026-11-06T06:30:00Z",
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
            ],
        },
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
    expect(native.locator('a[href="/c/child"]')).to_have_count(0)
    native.get_by_role(
        "button", name="Expand child sessions: Transfer reliability", exact=True
    ).click()
    expect(native.locator('a[href="/c/child"]')).to_be_visible()
    expect(native.locator('a[href="/c/archived"]')).to_be_visible()
    expect(native.get_by_text("Verify resumed chunks").first).to_be_visible()
    page.get_by_role("button", name="2 Waiting on me", exact=True).click()
    waiting = page.get_by_role("region", name="Waiting on me", exact=True)
    expect(waiting.get_by_text("Approve the retry budget")).to_be_visible()
    expect(waiting.locator('a[href="/c/child"]')).to_be_visible()
    waiting.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="Routes", exact=True).click()
    region = page.get_by_role("region", name="Routes", exact=True)
    expect(region.get_by_role("heading", name="Version two")).to_be_visible()
    region.get_by_role(
        "button", name=re.compile(r"Retry failed uploads(?:\s*ENG-1)?$")
    ).first.click()
    detail = page.get_by_role("region", name="Details: Retry failed uploads", exact=True)
    expect(detail.get_by_text("Check a slow connection")).to_be_visible()
    expect(detail.locator('a[href="https://tracker.example.com/retry"]')).to_be_visible()
    expect(detail.locator('a[href="/c/parent"]')).to_be_visible()
    detail.get_by_text("History and commits", exact=True).click()
    expect(detail.get_by_text("Keep confirmed chunks")).to_be_visible()
    detail.get_by_role("button", name="Close", exact=True).click()
    region.get_by_role("button", name=re.compile(r"Drop the legacy endpoint")).first.click()
    no_page = page.get_by_role("region", name="Details: Drop the legacy endpoint", exact=True)
    expect(no_page.get_by_text("The source gives no page")).to_be_visible()
    expect(page.locator('a[href^="javascript:"]')).to_have_count(0)
    no_page.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="By time", exact=True).click()
    expect(region.locator(".tb-axis")).to_be_visible()
    graph = region.locator("svg").first
    assert graph.bounding_box()
    assert page.locator("main").last.evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )
    title = page.get_by_role("heading", name="Task board", exact=True).bounding_box()
    header = page.get_by_role("banner").bounding_box()
    assert title and header and title["y"] >= header["y"] + header["height"]
    assert board_session_queries and all(
        query["kind"] == ["any"] for query in board_session_queries
    )
    assert watch_frames and all(frame["type"] == "watch" for frame in watch_frames)
    assert calls and all(request.method == "GET" for request in calls)
    assert not errors
    output = Path(request.config.getoption("--output"))
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"task-board-{width}-{theme}.png"), full_page=True)
