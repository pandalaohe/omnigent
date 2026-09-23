"""E2E: a project root chooses the host and directory for a new session."""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.helpers import stub_empty_host_picker_data

_PROJECT_ID = "proj_e2e_root"
_PROJECT_NAME = "RootedProject"
_HOST_A = "host_e2e_root_a"
_HOST_B = "host_e2e_root_b"
_ROOT = "/work/project-a"
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


async def _wait_for_create(bodies: list[dict[str, Any]]) -> None:
    for _ in range(300):
        if bodies:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("session create was not posted within 15 seconds")


def test_project_root_placement_and_untouched_seed_omission(
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_project_root_placement(base_url, session_id))


async def _drive_project_root_placement(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    create_bodies.append(route.request.post_data_json)
                    await route.fulfill(json={"id": session_id})
                else:
                    await route.continue_()

            await page.route(
                "**/v1/hosts",
                lambda route: route.fulfill(
                    json={
                        "hosts": [
                            {
                                "host_id": _HOST_A,
                                "name": "host-a",
                                "owner": "e2e",
                                "status": "online",
                            },
                            {
                                "host_id": _HOST_B,
                                "name": "host-b",
                                "owner": "e2e",
                                "status": "online",
                            },
                        ]
                    }
                ),
            )
            await stub_empty_host_picker_data(page, _HOST_A)
            await stub_empty_host_picker_data(page, _HOST_B)
            await page.route(
                "**/v1/agents",
                lambda route: route.fulfill(
                    json={
                        "data": [
                            {
                                "id": "ag_claude_e2e",
                                "name": "claude-native-ui",
                                "display_name": "Claude Code",
                                "description": "Coding agent",
                                "harness": None,
                                "skills": [],
                            }
                        ]
                    }
                ),
            )
            await page.route(
                "**/v1/sessions/projects",
                lambda route: route.fulfill(json=[{"id": _PROJECT_ID, "name": _PROJECT_NAME}]),
            )
            await page.route(
                f"**/v1/projects/{_PROJECT_ID}",
                lambda route: route.fulfill(
                    json={
                        "id": _PROJECT_ID,
                        "name": _PROJECT_NAME,
                        "config": {"host_id": _HOST_A, "workspace": _ROOT},
                    }
                ),
            )
            await page.route(
                f"**/v1/projects/{_PROJECT_ID}/host-roots",
                lambda route: route.fulfill(
                    json={
                        "roots": [{"host_id": _HOST_A, "workspace": _ROOT, "source": "config"}],
                        "default_host_id": _HOST_A,
                        "default_host_reason": "config",
                    }
                ),
            )
            await page.route(
                "**/v1/sessions/*/events",
                lambda route: route.fulfill(json={"queued": True, "item_id": "ci_e2e"}),
            )
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"),
                lambda route: route.fulfill(json={"data": []}),
            )
            await page.add_init_script(
                """window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({host_e2e_root_b: ["/work/unrelated"]})
                );"""
            )

            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await expect(page.get_by_test_id("new-chat-landing-project-line")).to_have_text(
                f"Project: {_PROJECT_NAME}"
            )
            host_chip = page.get_by_test_id("new-chat-landing-host-chip")
            directory_chip = page.get_by_test_id("new-chat-landing-workspace-chip")
            await expect(host_chip).to_have_attribute("aria-label", re.compile("host-a"))
            await expect(directory_chip).to_have_attribute(
                "aria-label", f"Working directory: {_ROOT}"
            )

            await host_chip.click()
            await page.get_by_test_id(f"new-chat-landing-host-{_HOST_B}").click()
            await expect(host_chip).to_have_attribute("aria-label", re.compile("host-b"))
            await expect(directory_chip).to_have_attribute(
                "aria-label", "Working directory: Not selected"
            )
            await page.get_by_test_id("new-chat-landing-input").fill("start here")
            submit = page.get_by_test_id("new-chat-landing-submit")
            await expect(submit).to_be_disabled()
            await submit.locator("..").hover()
            tooltip = page.get_by_test_id("new-chat-landing-submit-error-tooltip")
            await expect(tooltip).to_have_text(
                "This project has no directory on host-b. "
                "Choose a folder, or set one in project settings."
            )
            assert create_bodies == []

            await host_chip.click()
            await page.get_by_test_id(f"new-chat-landing-host-{_HOST_A}").click()
            await expect(directory_chip).to_have_attribute(
                "aria-label", f"Working directory: {_ROOT}"
            )
            await expect(submit).to_be_enabled()
            await submit.click()
            await _wait_for_create(create_bodies)
            body = create_bodies[0]
            assert body["project_id"] == _PROJECT_ID, body
            assert body["host_id"] == _HOST_A, body
            assert "workspace" not in body, body
        finally:
            await browser.close()
