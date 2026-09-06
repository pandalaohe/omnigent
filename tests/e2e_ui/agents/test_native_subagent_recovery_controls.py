"""Synthetic native child reports exercise the real recovery controls.

Status checks use the real Server old-Host error path; no native TUI is launched.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail


def test_native_child_owner_recheck_and_stop(page: Page, seeded_session: tuple[str, str]) -> None:
    base_url, parent_id = seeded_session
    httpx.patch(
        f"{base_url}/v1/sessions/{parent_id}",
        json={
            "title": "Review release checklist",
            "labels": {"omnigent.wrapper": "claude-code-native-ui"},
        },
    ).raise_for_status()
    started = httpx.post(
        f"{base_url}/v1/sessions/{parent_id}/events",
        json={
            "type": "external_subagent_start",
            "data": {
                "subagent_id": "review-worker",
                "agent_type": "Explore",
                "description": "Check release documentation",
                "tool_use_id": "tool-review",
            },
        },
    )
    started.raise_for_status()
    child_id = started.json()["child_session_id"]
    httpx.post(
        f"{base_url}/v1/sessions/{child_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": "running"},
        },
    ).raise_for_status()
    screenshots = os.environ.get("OMNIGENT_RECOVERY_SCREENSHOTS")

    def capture(name: str) -> None:
        if screenshots:
            target = Path(screenshots)
            target.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target / f"{name}.png"), full_page=True)

    page.set_viewport_size({"width": 1440, "height": 960})
    page.goto(f"{base_url}/c/{parent_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="Agents", exact=False).click()
    recheck = rail.get_by_role("button", name="Recheck agent status", exact=True)
    expect(recheck).to_be_visible()
    with page.expect_response(f"**/v1/sessions/{parent_id}/child_sessions/reconcile") as checked:
        recheck.click()
    assert checked.value.status == 503
    expect(
        page.get_by_text(
            "Could not verify agent status. Check that the Host is online and updated."
        )
    ).to_be_visible()
    stop = rail.get_by_test_id("stop-subagent")
    expect(stop).to_be_visible()
    stop.click()
    expect(page.get_by_role("dialog")).to_contain_text("Its conversation and history are kept.")
    capture("native-child-stop-desktop")
    page.get_by_role("button", name="Cancel", exact=True).click()
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    page.get_by_role("button", name="Conversation actions").click()
    page.get_by_role("menuitem", name="Agents", exact=False).click()
    drawer = page.get_by_test_id("subagents-panel-drawer")
    expect(drawer.get_by_role("button", name="Recheck agent status")).to_be_visible()
    drawer.get_by_test_id("stop-subagent").click()
    capture("native-child-stop-mobile")
    with page.expect_response(f"**/v1/sessions/{child_id}/events") as stopped:
        page.get_by_test_id("stop-subagent-confirm").click()
    assert stopped.value.status == 202
    expect(page.get_by_role("dialog").filter(has_text="Stop sub-agent?")).to_have_count(0)
    kept = httpx.get(f"{base_url}/v1/sessions/{child_id}")
    kept.raise_for_status()
    assert kept.json()["id"] == child_id
