"""Standing approval terminology and inherited state in the built chat UI."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract
from tests.browser_ui.conftest import BrowserContract


@pytest.mark.parametrize("width", [1440, 390])
def test_standing_approval_chip_and_legacy_route(
    page: Page,
    chat_session_contract: ChatSessionContract,
    browser_contract: BrowserContract,
    width: int,
    request: pytest.FixtureRequest,
) -> None:
    chat = chat_session_contract
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(chat.url)
    chip = page.get_by_role("button", name="Standing approval: none", exact=True)
    expect(chip).to_be_visible()
    chip.click()
    expect(page.get_by_role("heading", name="Standing approval", exact=True)).to_be_visible()
    expect(page.get_by_text("Applies to this parent session and its children.")).to_be_visible()
    expect(page.get_by_text("No active kinds.", exact=True)).to_be_visible()
    records = page.evaluate(
        """async sessionId => Promise.all(
            ["standing-pass", "standing-approval"].map(async route => {
                const response = await fetch(`/v1/sessions/${sessionId}/${route}`);
                return {status: response.status, body: await response.json()};
            })
        )""",
        chat.session_id,
    )
    assert records[0] == records[1]
    assert records[0]["status"] == 200
    assert records[0]["body"]["root_id"] == chat.session_id
    page.keyboard.press("Escape")

    browser_contract.json(
        f"/v1/sessions/{chat.session_id}/standing-approval",
        {
            "root_id": "parent-session",
            "record": {"kinds": ["install"], "exceptions": [], "since": 0, "events": []},
        },
    )
    page.reload()
    page.get_by_role("button", name="Standing approval: 1 active", exact=True).click()
    expect(page.get_by_text("Inherited from the parent session. Read-only here.")).to_be_visible()
    expect(page.get_by_text("install", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Revoke install", exact=True)).to_have_count(0)
    output = Path(request.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"standing-approval-{width}.png"), full_page=True)
