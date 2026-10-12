"""Real settings/archive requests, with injected host status for UI state coverage.

Host Git removal and server policy are covered separately by integration tests.
This journey verifies the built UI's markers, details and first-choice prompt.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, Response, Route, expect


def _mode(base_url: str, mode: str, *, ask: bool = False) -> None:
    httpx.patch(
        f"{base_url}/v1/me/preferences/worktree_archive",
        json={"value": {"mode": mode, "ask_before_archive": ask, "rebuildable_paths": ["dist/"]}},
        timeout=10,
    ).raise_for_status()


def _shot(page: Page, request: pytest.FixtureRequest, name: str) -> None:
    output = Path(request.config.getoption("output")) / "worktree-safety"
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"{name}.png"), full_page=True)


def _preference_saved(response: Response) -> bool:
    return response.request.method == "PATCH" and response.url.endswith(
        "/v1/me/preferences/worktree_archive"
    )


def test_archive_preference_is_server_side(
    page: Page, live_server: str, request: pytest.FixtureRequest
) -> None:
    _mode(live_server, "never")
    page.goto(f"{live_server}/settings/git")
    never = page.get_by_role("radio", name="Never delete worktrees")
    safe = page.get_by_role("radio", name="Delete green worktrees")
    expect(never).to_be_checked(timeout=30_000)
    with page.expect_response(_preference_saved) as saved:
        safe.click()
    assert saved.value.ok
    expect(safe).to_be_checked()
    page.reload()
    expect(safe).to_be_checked(timeout=30_000)
    assert httpx.get(f"{live_server}/v1/me", timeout=10).json()["preferences"]["settings"][
        "worktree_archive"
    ] == {"mode": "green_only", "ask_before_archive": False, "rebuildable_paths": ["dist/"]}
    with page.expect_response(_preference_saved) as saved:
        page.get_by_role("switch", name="Ask before archiving").click()
    assert saved.value.ok
    page.get_by_label("Rebuildable ignored directories").fill("dist/\ncoverage/")
    with page.expect_response(_preference_saved) as saved:
        page.get_by_role("button", name="Save list", exact=True).click()
    assert saved.value.ok
    page.reload()
    expect(page.get_by_role("switch", name="Ask before archiving")).to_be_checked(timeout=30_000)
    expect(page.get_by_label("Rebuildable ignored directories")).to_have_value("dist/\ncoverage/")
    assert page.evaluate("localStorage.getItem('omnigent:delete-worktrees-on-archive')") is None
    _shot(page, request, "preference-light")
    with page.expect_response(_preference_saved) as saved:
        never.click()
    assert saved.value.ok
    expect(never).to_be_checked()


def test_archive_confirmation_lists_child_worktrees_without_a_parent_worktree(
    page: Page, seeded_session: tuple[str, str], request: pytest.FixtureRequest
) -> None:
    base_url, parent_id = seeded_session
    _mode(base_url, "green_only", ask=True)
    parent = httpx.get(f"{base_url}/v1/sessions/{parent_id}", timeout=10).json()
    agent_id = httpx.get(f"{base_url}/v1/sessions/{parent_id}/agent", timeout=10).json()["id"]
    entries = []
    child_ids = []
    for title, state, reason in (
        ("Clean implementation", "clean", "clean worktree"),
        ("Local research files", "local", "other ignored local files"),
        ("Uncommitted edits", "dirty", "uncommitted source files"),
    ):
        response = httpx.post(
            f"{base_url}/v1/sessions",
            json={"agent_id": agent_id, "parent_session_id": parent_id, "title": title},
            timeout=10,
        )
        response.raise_for_status()
        child_id = response.json()["id"]
        child_ids.append(child_id)
        entries.append(
            {
                "session_id": child_id,
                "title": title,
                "state": state,
                "reason": reason,
                "path": f"/opt/work/sample-app/{state}",
                "kept": False,
            }
        )
    assert not parent.get("git_branch") and not parent.get("worktree")

    def status(route: Route) -> None:
        route.fulfill(
            json={
                "own": {
                    "state": "none",
                    "reason": "no bound worktree",
                    "path": None,
                    "branch": None,
                    "merged": None,
                    "merge_target": None,
                    "files": [],
                },
                "aggregate": {"state": "dirty", "reason": "descendant worktree needs attention"},
                "blockers": [
                    {key: entry[key] for key in ("session_id", "title", "state", "reason")}
                    for entry in entries
                    if entry["state"] != "clean"
                ],
                "session_count": 4,
                "worktrees": entries,
            }
        )

    page.route("**/v1/sessions/*/worktree-status*", status)
    try:
        page.goto(f"{base_url}/c/{parent_id}")
        page.get_by_test_id("header-conversation-actions").click(timeout=30_000)
        page.get_by_role("menuitem", name="Archive", exact=True).click()
        dialog = page.get_by_test_id("archive-worktree-dialog")
        expect(dialog).to_be_visible()
        for entry in entries:
            expect(dialog.get_by_text(entry["title"], exact=False)).to_be_visible()
            expect(dialog.get_by_text(entry["path"], exact=True)).to_be_visible()
        expect(dialog.get_by_text("Remove after safety checks", exact=True)).to_have_count(1)
        expect(
            dialog.get_by_text("Keep: selected tier keeps local files", exact=True)
        ).to_be_visible()
        expect(dialog.get_by_text("Keep: uncommitted source files", exact=True)).to_be_visible()
        _shot(page, request, "archive-child-worktrees")
        # Keep all directories in this UI drive; the server/host suites prove deletion.
        dialog.get_by_role("radio", name="Never delete worktrees").click()
        dialog.get_by_role("button", name="Archive", exact=True).click()
        expect(page).to_have_url(f"{base_url}/", timeout=15_000)
        for child_id in child_ids:
            assert httpx.get(f"{base_url}/v1/sessions/{child_id}", timeout=10).json()["archived"]
    finally:
        for child_id in child_ids:
            httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10).raise_for_status()
        _mode(base_url, "never")


def test_worktree_marks_past_aggregate_and_archive_prompt(
    page: Page, seeded_session: tuple[str, str], request: pytest.FixtureRequest
) -> None:
    base_url, parent_id = seeded_session
    _mode(base_url, "never", ask=True)
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

    def bound_session_metadata(route: Route) -> None:
        if route.request.method != "GET" or route.request.url.split("?", 1)[0].endswith("/stream"):
            route.fallback()
            return
        response = route.fetch()
        if "application/json" not in response.headers.get("content-type", ""):
            route.fulfill(response=response)
            return
        body = response.json()

        def bind(value: object) -> None:
            if isinstance(value, dict):
                if value.get("id") == parent_id or value.get("session_id") == parent_id:
                    value["git_branch"] = "feature/research"
                for item in value.values():
                    bind(item)
            elif isinstance(value, list):
                for item in value:
                    bind(item)

        bind(body)
        route.fulfill(response=response, json=body)

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
            "files": (
                [{"path": "draft.txt", "status": "??", "kind": "source"}]
                if own_state == "dirty"
                else [{"path": "local-notes.md", "status": "!!", "kind": "local"}]
                if own_state == "local"
                else []
            ),
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

    # This journey isolates UI states; host binding/removal has separate real-Git tests.
    page.route("**/v1/sessions**", bound_session_metadata)
    page.route("**/v1/sessions/*/worktree-status*", status)

    def bound_updates(socket) -> None:
        server = socket.connect_to_server()

        def forward(message) -> None:
            try:
                value = json.loads(message)
            except (TypeError, ValueError):
                socket.send(message)
                return

            def bind(value: object) -> None:
                if isinstance(value, dict):
                    if value.get("id") == parent_id or value.get("session_id") == parent_id:
                        value["git_branch"] = "feature/research"
                    for item in value.values():
                        bind(item)
                elif isinstance(value, list):
                    for item in value:
                        bind(item)

            bind(value)
            socket.send(json.dumps(value))

        server.on_message(forward)

    page.route_web_socket("**/v1/sessions/updates**", bound_updates)
    page.set_viewport_size({"width": 1440, "height": 900})
    try:
        for theme in ("light", "dark"):
            page.emulate_media(color_scheme=theme)
            for value in (
                "clean",
                "local",
                "dirty",
                "unknown",
                "protected",
                "shared",
                "removed",
                "none",
            ):
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
            "[data-worktree-state='clean']"
        )
        expect(mark).to_have_text("")
        mark.hover()
        expect(page.get_by_role("tooltip").filter(has_text="Research notes")).to_be_visible()
        _shot(page, request, "mother-past-blocker")
        page.mouse.move(700, 800)
        state["blocked"] = False
        state["value"] = "local"
        page.goto(f"{base_url}/c/{parent_id}")
        page.get_by_test_id("header-conversation-actions").click()
        page.get_by_role("menuitem", name="Worktree details", exact=True).click()
        details = page.get_by_test_id("worktree-details-dialog")
        expect(details).to_be_visible()
        expect(details.get_by_text("!! local-notes.md", exact=True)).to_be_visible()
        _shot(page, request, "worktree-details-desktop")
        page.keyboard.press("Escape")
        page.get_by_test_id("header-conversation-actions").click()
        page.get_by_role("menuitem", name="Archive", exact=True).click()
        dialog = page.get_by_test_id("archive-worktree-dialog")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("radio", name="Never delete worktrees")).to_be_checked()
        _shot(page, request, "first-archive-desktop")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(page).to_have_url(f"{base_url}/c/{parent_id}")
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("button", name="Close sidebar", exact=True).click(
            position={"x": 375, "y": 400}
        )
        page.mouse.move(350, 700)
        page.get_by_test_id("header-conversation-actions").click()
        page.get_by_role("menuitem", name="Archive", exact=True).click()
        dialog = page.get_by_test_id("archive-worktree-dialog")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("radio", name="Never delete worktrees")).to_be_checked()
        expect(dialog.get_by_role("button", name="Archive", exact=True)).to_be_focused()
        dialog.get_by_role("radio", name="Delete green and yellow worktrees").click()
        dialog.get_by_role("checkbox", name="Don't ask again", exact=True).check()
        _shot(page, request, "first-archive-mobile")
        with page.expect_request(
            lambda req: req.method == "PATCH" and f"/v1/sessions/{parent_id}" in req.url
        ) as archived:
            dialog.get_by_role("button", name="Archive", exact=True).click()
        assert archived.value.post_data_json == {
            "archived": True,
            "worktree_archive_mode": "yellow_too",
        }
        saved = httpx.get(f"{base_url}/v1/me", timeout=10).json()["preferences"]["settings"][
            "worktree_archive"
        ]
        assert saved["mode"] == "yellow_too"
        assert saved["ask_before_archive"] is False
        expect(page).to_have_url(f"{base_url}/", timeout=15_000)
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10).raise_for_status()
        _mode(base_url, "never")
