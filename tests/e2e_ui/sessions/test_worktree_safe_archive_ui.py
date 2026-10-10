"""Real settings/archive requests, with injected host status for UI state coverage.

Host Git removal and server policy are covered separately by integration tests.
This journey verifies the built UI's markers, Past aggregation and warning.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, Route, expect


def _mode(base_url: str, mode: str) -> None:
    httpx.patch(
        f"{base_url}/v1/me/preferences/worktree_archive",
        json={"value": {"mode": mode}},
        timeout=10,
    ).raise_for_status()


def _shot(page: Page, request: pytest.FixtureRequest, name: str) -> None:
    output = Path(request.config.getoption("output")) / "worktree-safety"
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"{name}.png"), full_page=True)


def test_archive_preference_is_server_side(
    page: Page, live_server: str, request: pytest.FixtureRequest
) -> None:
    _mode(live_server, "never")
    page.goto(f"{live_server}/settings/git")
    never = page.get_by_role("radio", name="Never delete", exact=True)
    safe = page.get_by_role("radio", name="Delete safe worktrees on archive", exact=True)
    expect(never).to_be_checked(timeout=30_000)
    safe.click()
    expect(safe).to_be_checked()
    page.reload()
    expect(safe).to_be_checked(timeout=30_000)
    assert httpx.get(f"{live_server}/v1/me", timeout=10).json()["preferences"]["settings"][
        "worktree_archive"
    ] == {"mode": "delete_safe"}
    assert page.evaluate("localStorage.getItem('omnigent:delete-worktrees-on-archive')") is None
    _shot(page, request, "preference-light")
    never.click()
    expect(never).to_be_checked()


def test_worktree_marks_past_aggregate_and_archive_warning(
    page: Page, seeded_session: tuple[str, str], request: pytest.FixtureRequest
) -> None:
    base_url, parent_id = seeded_session
    _mode(base_url, "delete_safe")
    httpx.patch(
        f"{base_url}/v1/sessions/{parent_id}", json={"title": "Planning notes"}, timeout=10
    ).raise_for_status()
    agent_id = httpx.get(f"{base_url}/v1/sessions/{parent_id}/agent", timeout=10).json()["id"]
    child = httpx.post(
        f"{base_url}/v1/sessions",
        json={"agent_id": agent_id, "parent_session_id": parent_id, "title": "Research notes"},
        timeout=10,
    )
    child.raise_for_status()
    child_id = child.json()["id"]
    state = {"value": "clean", "blocked": False}

    def status(route: Route) -> None:
        is_parent = f"/{parent_id}/" in route.request.url
        own_state = "clean" if is_parent and state["blocked"] else state["value"]
        own = {
            "state": own_state,
            "reason": "Uncommitted files" if own_state == "dirty" else "Worktree checked",
            "path": "/opt/work/project/task",
            "branch": "feature/research",
            "merged": False,
            "merge_target": "main",
            "files": [{"path": "draft.txt", "status": "??"}] if own_state == "dirty" else [],
        }
        aggregate = "dirty" if is_parent and state["blocked"] else own_state
        blockers = (
            [
                {
                    "session_id": child_id,
                    "title": "Research notes",
                    "state": "dirty",
                    "reason": "Untracked draft.txt in archived child",
                }
            ]
            if is_parent and state["blocked"]
            else []
        )
        route.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "own": own,
                    "aggregate": {"state": aggregate, "reason": own["reason"]},
                    "blockers": blockers,
                    "session_count": 2 if is_parent else 1,
                }
            ),
        )

    page.route("**/v1/sessions/*/worktree-status*", status)
    page.set_viewport_size({"width": 1440, "height": 900})
    try:
        for theme in ("light", "dark"):
            page.emulate_media(color_scheme=theme)
            for value in ("clean", "dirty", "unknown", "protected", "shared", "removed", "none"):
                state["value"] = value
                page.goto(f"{base_url}/c/{parent_id}")
                header = page.get_by_role("navigation", name="Conversation")
                expect(header).to_be_visible(timeout=30_000)
                if value == "none":
                    expect(header.locator("[data-worktree-state]")).to_have_count(0)
                else:
                    expect(header.locator(f"[data-worktree-state='{value}']")).to_be_visible(
                        timeout=15_000
                    )
                    sidebar = page.locator("a").filter(has_text="Planning notes").first
                    expect(sidebar.locator(f"[data-worktree-state='{value}']")).to_be_visible()
                    page.locator("[data-workspace-tab='subagents']").click()
                    expect(
                        page.get_by_test_id("subagent-main-row").locator(
                            f"[data-worktree-state='{value}']"
                        )
                    ).to_be_visible(timeout=15_000)
                _shot(page, request, f"marks-{theme}-{value}")

        httpx.patch(
            f"{base_url}/v1/sessions/{child_id}", json={"archived": True}, timeout=10
        ).raise_for_status()
        state.update(value="dirty", blocked=True)
        page.goto(f"{base_url}/c/{parent_id}")
        page.locator("[data-workspace-tab='subagents']").click()
        page.get_by_test_id("subagent-past-zone").click()
        expect(page.locator(f"[data-child-session-id='{child_id}']")).to_be_visible(timeout=15_000)
        mark = page.get_by_role("navigation", name="Conversation").locator(
            "[data-worktree-state='dirty']"
        )
        mark.hover()
        expect(page.get_by_role("tooltip").filter(has_text="Research notes")).to_be_visible()
        _shot(page, request, "mother-past-blocker")
        page.mouse.move(700, 800)
        state["blocked"] = False
        page.get_by_test_id("header-conversation-actions").click()
        page.get_by_role("menuitem", name="Archive", exact=True).click()
        dialog = page.get_by_test_id("archive-worktree-dialog")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_text("?? draft.txt", exact=True)).to_be_visible()
        expect(dialog.get_by_role("button", name="Archive only", exact=True)).to_be_focused()
        _shot(page, request, "unsafe-archive-warning")
        with page.expect_request(
            lambda req: req.method == "PATCH" and f"/v1/sessions/{parent_id}" in req.url
        ) as archived:
            dialog.get_by_role("button", name="Archive only", exact=True).click()
        assert archived.value.post_data_json == {"archived": True, "keep_worktree": True}
        expect(page).to_have_url(f"{base_url}/", timeout=15_000)
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10).raise_for_status()
        _mode(base_url, "never")
