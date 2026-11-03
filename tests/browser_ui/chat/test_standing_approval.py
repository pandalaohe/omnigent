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


@pytest.mark.parametrize("width", [1440, 390])
def test_parent_approval_handoff_and_attribution(
    page: Page,
    chat_session_contract: ChatSessionContract,
    browser_contract: BrowserContract,
    width: int,
    request: pytest.FixtureRequest,
) -> None:
    chat = chat_session_contract
    page.set_viewport_size({"width": width, "height": 900})
    browser_contract.json(
        "/v1/sessions/parent-session",
        {
            **chat.session_snapshot(),
            "id": "parent-session",
            "title": "Parent review",
        },
    )
    triage = {
        "offer_id": "offer",
        "root_session_id": "parent-session",
        "mode": "answer",
        "state": "offered",
    }
    event = {
        "elicitation_id": "child-offer",
        "params": {
            "message": "Run the workspace check?",
            "phase": "pre_tool_use",
            "policy_name": "claude_native_permission",
            "tool_name": "Bash",
            "content_preview": "echo ready",
            "requestedSchema": {},
            "triage": triage,
        },
    }
    chat.update_session(pending_elicitations=[event], pending_elicitations_count=0)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(chat.url)
    summary = page.locator("[data-testid=approval-offer] summary")
    chat.wait_for_stream()
    expect(summary).to_contain_text("Waiting for Parent review")
    expect(page.get_by_role("button", name="Approve", exact=True)).not_to_be_visible()
    summary.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_visible()
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Approve", exact=True)).not_to_be_visible()
    output = Path(request.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"parent-offered-{width}.png"), full_page=True)

    passed = {
        **event,
        "params": {
            **event["params"],
            "triage": {
                **triage,
                "state": "passed",
                "note": "子会话希望执行工作区检查，请确认是否允许这一次操作。",
                "recommend": "approve",
            },
        },
    }
    chat.update_session(pending_elicitations=[passed], pending_elicitations_count=1)
    chat.emit({"event": "response.elicitation_request", "data": passed})
    expect(page.get_by_text("Parent recommends: approve")).to_be_visible()
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_visible()
    page.reload()
    chat.wait_for_stream()
    expect(
        page.get_by_text("子会话希望执行工作区检查，请确认是否允许这一次操作。")
    ).to_be_visible()
    expect(page.get_by_text("Parent recommends: approve")).to_be_visible()
    page.screenshot(path=str(output / f"parent-passed-{width}.png"), full_page=True)
    page.goto(f"{browser_contract.base_url}/inbox")
    expect(
        page.get_by_text("子会话希望执行工作区检查，请确认是否允许这一次操作。")
    ).to_be_visible()
    expect(page.get_by_text("Parent recommends: approve")).to_be_visible()
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_visible()
    page.screenshot(path=str(output / f"parent-inbox-{width}.png"), full_page=True)
    page.goto(chat.url)
    chat.wait_for_stream()
    expect(page.get_by_text("Parent recommends: approve")).to_be_visible()
    chat.emit(
        {
            "event": "response.elicitation_resolved",
            "data": {
                "elicitation_id": "child-offer",
                "action": "accept",
                "reason": "Inside the assigned workspace",
                "answered_by": {"session_id": "parent-session", "title": "Parent review"},
            },
        }
    )
    expect(page.get_by_test_id("approval-card")).to_contain_text("Approved by Parent review")
    expect(page.get_by_text("Inside the assigned workspace", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Approve", exact=True)).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert errors == []
    page.screenshot(path=str(output / f"parent-answered-{width}.png"), full_page=True)
