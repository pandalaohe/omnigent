"""Startup states and exact release confirmation in the built SPA with mocked transport."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "narrow"])
def test_startup_release_keeps_unknown_work_and_advances_the_queue(
    page: Page, chat_session_contract: ChatSessionContract, output_path: str, width: int
) -> None:
    chat = chat_session_contract
    artifacts = Path(output_path)
    page.set_viewport_size({"width": width, "height": 900})
    startup = {
        "id": "a" * 32,
        "host_id": chat.host_id,
        "attempt_id": None,
        "input_id": "e" * 32,
        "state": "queued",
        "reason": None,
        "elapsed_seconds": 240,
        "admitted_elapsed_seconds": None,
        "input_elapsed_seconds": None,
        "unconfirmed_startups": 2,
        "queue_position_estimate": 1,
        "can_cancel": True,
        "evidence": {
            "runtime_ready": False,
            "tools_ready": False,
            "input_accepted": False,
            "work_started": False,
        },
    }
    chat.update_session(startup=startup)
    targets = [
        {
            "target": {
                "startup_id": sid * 32,
                "session_id": session * 32,
                "attempt_id": attempt * 32,
                "input_id": None,
                "runtime_id": None,
                "host_generation": "old-generation",
                "runner_id": None,
            },
            "title": title,
            "work_hint": hint,
        }
        for sid, session, attempt, title, hint in (
            ("b", "c", "d", "Draft project notes", "working"),
            ("f", "1", "2", "Review the launch checklist", "unknown"),
        )
    ]
    release_path = f"/v1/hosts/{chat.host_id}/startup-releases"
    chat.contract.json(
        release_path, {"current_host_generation": "current-generation", "targets": targets}
    )
    releases = []

    def release(request):
        releases.append(request.post_data_json)
        chat.update_session(
            startup={
                **startup,
                "state": "starting",
                "unconfirmed_startups": 0,
                "admitted_elapsed_seconds": 0,
                "queue_position_estimate": None,
            }
        )
        return {"released_at": 123}

    chat.contract.json(release_path, release, method="POST")
    page.goto(chat.url)
    chat.wait_for_stream()
    expect(page.get_by_text("Waiting to start on this host", exact=True)).to_be_visible()
    page.get_by_role("button", name="Review stuck starts", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Draft project notes", exact=True)).to_be_visible()
    expect(dialog).to_contain_text("does not stop old work")
    expect(dialog).to_contain_text("more concurrent starts than the limit")
    expect(dialog).to_contain_text("does not resend saved input")
    expect(dialog).to_contain_text("Work was observed")
    expect(dialog).to_contain_text("Work status unknown")
    for item in targets:
        session_id = item["target"]["session_id"]
        expect(dialog.get_by_text(session_id[:8], exact=True)).to_have_attribute(
            "title", session_id
        )
    confirm = dialog.get_by_role("button", name="Release startup slots", exact=True)
    expect(confirm).to_be_enabled()
    page.screenshot(path=str(artifacts / "release-dialog.png"), full_page=True)
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
    confirm.click()
    expect(dialog).not_to_be_visible()
    expect(page.get_by_text("Opening session", exact=True)).to_be_visible()
    assert releases == [
        {
            "current_host_generation": "current-generation",
            "targets": [item["target"] for item in targets],
        }
    ]
    assert chat.event_posts == [], "Release must never resend the saved message"

    for state, reason, label in (
        ("uncertain", "opening_unconfirmed", "Startup is stuck — opening not confirmed"),
        ("operator_released", "delivery_unconfirmed", "Startup slot released"),
        ("waiting_input", None, "Waiting for your first message"),
        ("opened", None, "Session opened"),
    ):
        current = {
            **startup,
            "state": state,
            "reason": reason,
            "queue_position_estimate": None,
            "admitted_elapsed_seconds": 180,
            "can_cancel": state == "uncertain",
        }
        if state == "waiting_input":
            current["input_id"] = None
        chat.update_session(startup=current)
        chat.emit(
            {
                "event": "session.startup",
                "data": {"conversation_id": chat.session_id, "startup": current},
            }
        )
        expect(page.get_by_text(label, exact=True)).to_be_visible()
        expect(page.get_by_test_id("working-indicator")).to_have_count(0)
        if state == "operator_released" and width == 390:
            # The existing control is movable; keep startup layout unchanged.
            assistant = page.get_by_role("button", name="Open floating assistant", exact=True)
            box = assistant.bounding_box()
            assert box is not None
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.down()
            page.mouse.move(335, 450, steps=8)
            page.mouse.up()
            expect(assistant).to_have_attribute("aria-expanded", "false")
            page.screenshot(
                path=str(artifacts / "operator_released-moved-control.png"), full_page=True
            )
    page.reload()
    chat.wait_for_stream()
    expect(page.get_by_text("Session opened", exact=True)).to_be_visible()
    if width == 390:
        restored = page.get_by_role(
            "button", name="Open floating assistant", exact=True
        ).bounding_box()
        assert restored is not None
        assert abs(restored["y"] + restored["height"] / 2 - 450) < 5
    assert chat.event_posts == []
