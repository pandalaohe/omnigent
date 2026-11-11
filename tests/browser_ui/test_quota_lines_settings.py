"""Quota rule edits and stop warnings in the built settings page."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_features as appearance_features
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url
from tests.browser_ui.test_settings_streamline import settings_contract as settings_contract


@pytest.mark.parametrize("width", [1440, 390])
def test_quota_lines_save_and_preview(
    page: Page,
    appearance_url: str,
    browser_contract: BrowserContract,
    width: int,
    request: pytest.FixtureRequest,
) -> None:
    settings = {
        "enabled": True,
        "strategy": "drain",
        "workTypes": [],
        "presets": [],
        "hosts": {},
        "abilityOrder": [],
        "limits": {"plan": {"reserve": {"mode": "daily", "window": "7d", "pct": 4, "stopPct": 1}}},
    }
    browser_contract.json(
        "/v1/hosts",
        {"hosts": [{"host_id": "example-host", "name": "Desktop", "status": "online"}]},
    )
    browser_contract.json(
        "/v1/me/session-choice", lambda _: {"settings": settings, "revision": "a"}
    )
    browser_contract.json(
        "/v1/me/session-choice/meters",
        {
            "meters": [
                {
                    "id": "plan",
                    "display_name": "Coding plan",
                    "freshness": "fresh",
                    "windows": [
                        {
                            "id": "7d",
                            "label": "Weekly",
                            "window_minutes": 10080,
                            "remaining_pct": 5,
                            "resets_at": time.time() + 2.5 * 86400,
                        }
                    ],
                }
            ],
            "running": {"plan": ["other"]},
            "floors": {},
        },
    )
    browser_contract.json(
        "/v1/me/usage/lines",
        {
            "schema": 1,
            "tiles": {
                "plan": {
                    "state": "warn",
                    "warn_line_pct": 12,
                    "stop_line_pct": 3,
                    "released": False,
                    "running": ["one", "two"],
                }
            },
        },
    )

    def save(incoming: Request) -> dict:
        settings.update(incoming.post_data_json["settings"])
        return {"settings": settings, "revision": "b"}

    browser_contract.json("/v1/me/session-choice", save, method="PUT")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{appearance_url}/settings/general#session-choice")
    section = page.get_by_role("region", name="Session choice settings")
    section.get_by_text("Limits", exact=True).click()
    expect(section.get_by_text("Warn: 12%", exact=True)).to_be_visible()
    stop = section.get_by_role("spinbutton", name="Coding plan stop percent")
    stop.fill("2")
    expect(section.get_by_text("Saving cuts 2 running sessions")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    output = Path(request.config.getoption("--output"))
    output.mkdir(parents=True, exist_ok=True)
    stop.scroll_into_view_if_needed()
    page.screenshot(path=str(output / f"quota-lines-{width}.png"), full_page=True)
    section.get_by_role("spinbutton", name="Coding plan warn percent").fill("")
    stop.fill("")
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT" and response.url.endswith("/v1/me/session-choice")
        )
    ):
        section.get_by_role("button", name="Save session choice").click()
    expect(section.get_by_text("Saved session choice.", exact=True)).to_be_visible()
    assert settings["limits"]["plan"]["reserve"] is None
