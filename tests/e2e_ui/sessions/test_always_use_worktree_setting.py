"""E2E: the New Chat worktree default persists on the server across reloads."""

from __future__ import annotations

from playwright.sync_api import Page, expect

STORAGE_KEY = "omnigent:worktree-defaults"


def _server_default(page: Page, base_url: str) -> bool | None:
    response = page.request.get(f"{base_url}/v1/me")
    assert response.ok
    preferences = response.json().get("preferences") or {}
    return preferences.get("settings", {}).get("worktree_defaults", {}).get("alwaysUseWorktree")


def test_always_use_worktree_defaults_off_persists_and_clears(
    page: Page, live_server: str
) -> None:
    page.goto(f"{live_server}/settings/git")
    toggle = page.get_by_test_id("settings-always-use-worktree-toggle")
    expect(toggle).to_have_attribute("aria-checked", "false", timeout=30_000)
    assert _server_default(page, live_server) in (None, False)

    with page.expect_response(
        lambda response: (
            response.url.endswith("/v1/me/preferences/worktree_defaults")
            and response.request.method == "PATCH"
        )
    ) as saved:
        toggle.click()
    assert saved.value.ok
    assert _server_default(page, live_server) is True

    # Remove the local cache so reloading must hydrate the server's value.
    page.evaluate("key => localStorage.removeItem(key)", STORAGE_KEY)
    page.reload()
    toggle = page.get_by_test_id("settings-always-use-worktree-toggle")
    expect(toggle).to_have_attribute("aria-checked", "true", timeout=30_000)

    with page.expect_response(
        lambda response: (
            response.url.endswith("/v1/me/preferences/worktree_defaults")
            and response.request.method == "PATCH"
        )
    ) as saved:
        toggle.click()
    assert saved.value.ok
    assert _server_default(page, live_server) is False
    page.evaluate("key => localStorage.removeItem(key)", STORAGE_KEY)
    page.reload()
    expect(page.get_by_test_id("settings-always-use-worktree-toggle")).to_have_attribute(
        "aria-checked", "false", timeout=30_000
    )
