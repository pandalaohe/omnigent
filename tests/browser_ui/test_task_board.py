"""The built task board's settings, sidebar, link safety and narrow-screen list."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url


@pytest.mark.parametrize("width", [1440, 390])
def test_task_board_display_switch_and_source_list(
    page: Page,
    appearance_url: str,
    browser_contract: BrowserContract,
    width: int,
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
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 900})
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
    if width < 768:
        page.get_by_role("button", name="Back to settings menu", exact=True).click()
    page.get_by_role("link", name="Back", exact=True).click()
    expect(page).to_have_url(appearance_url + "/task-board")
    if width < 768:
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    page.get_by_role("link", name="Task board", exact=True).click()
    region = page.get_by_role("region", name=source_name)
    expect(region.get_by_text("Retry failed uploads")).to_be_visible()
    expect(region.get_by_role("heading", name="Version two")).to_be_visible()
    expect(region.get_by_role("img", name="In progress")).to_be_visible()
    expect(region.get_by_role("img", name="Cancelled")).to_be_visible()
    expect(region.get_by_text("The source gives no page")).to_be_visible()
    expect(region.locator('a[href="https://tracker.example.com/retry"]')).to_have_count(1)
    expect(region.locator('a[href^="javascript:"]')).to_have_count(0)
    assert page.locator("main").last.evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )
    title = page.get_by_role("heading", name="Task board", exact=True).bounding_box()
    header = page.get_by_role("banner").bounding_box()
    assert title and header and title["y"] >= header["y"] + header["height"]
    assert not errors
    output = Path(request.config.getoption("--output"))
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"task-board-{width}.png"), full_page=True)
