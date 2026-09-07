"""Native Goal metadata -> persisted marker -> rendered sidebar and chat frame."""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect


def test_native_goal_projection_survives_reload_and_clear(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session

    def report(state: str | None) -> None:
        response = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={"type": "external_goal_state", "data": {"state": state}},
        )
        response.raise_for_status()

    report("active")
    page.goto(f"{base_url}/c/{session_id}")
    frame = page.get_by_test_id("session-goal-frame")
    expect(frame).to_have_attribute("data-goal-state", "active")
    opener = page.get_by_role("button", name="Open sidebar", exact=True)
    if opener.is_visible():
        opener.click()
    expect(page.get_by_role("img", name="Goal active", exact=True)).to_be_visible()

    page.reload()
    expect(frame).to_have_attribute("data-goal-state", "active")
    report(None)
    expect(frame).to_have_count(0, timeout=15_000)
    expect(page.get_by_role("img", name="Goal active", exact=True)).to_have_count(0)
