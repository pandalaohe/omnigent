"""E2E: a project visit seeds New Chat from the per-host calling defaults.

The composer resolves a project's per-host calling defaults through
``GET /v1/calling-defaults/resolve`` (``web/src/lib/callingDefaultsSeed.ts``)
once the host is known: untouched pickers take the resolved agent / model /
effort, and the create posts the values the picker shows. Switching to another
host re-resolves for that host, so the untouched fields follow its set.

The tunneled e2e_ui runner registers no host, so ``/v1/hosts``, the host picker
data, ``/v1/agents``, the project routes, the resolve / catalogs routes, and the
create ``POST`` are faked — the POST handler *captures the body*, the thing
under test, and returns a seeded session id so post-send navigation lands
somewhere real. The host catalog stub carries one ``claude-native`` row only so
the picker can label the resolved native model.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Coroutine
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page, Route, async_playwright, expect

from tests.e2e_ui.start_session.helpers import stub_empty_host_picker_data

_PROJECT_ID = "proj_e2e_calling_defaults"
_PROJECT_NAME = "CallingDefaultsProject"
_HOST_HDS = "host_e2e_calling_hds"
_HOST_TMB = "host_e2e_calling_tmb"
_ROOT = "/work/calling-defaults-repo"
_AGENT_CODEX = "ag_codex_sdk_e2e"
_AGENT_CLAUDE = "ag_claude_native_e2e"
_CODEX_MODEL = "gpt-6-sol"
_CODEX_EFFORT = "high"
_CLAUDE_MODEL = "opus-5-5"
_CLAUDE_EFFORT = "xhigh"
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own loop (see test_start_session)."""
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


async def _wait_for_host_menu_closed(page: Page) -> None:
    """Wait out the host dropdown's exit animation after a target switch.

    The menu stays mounted while it animates out and returns focus to its
    trigger when it unmounts; reopening before that gets the fresh menu
    dismissed by that close-refocus.
    """
    await expect(page.get_by_test_id("new-chat-landing-host-menu")).to_have_count(0)


def test_project_new_chat_seeds_calling_defaults_per_host(
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_calling_defaults_prefill(base_url, session_id))


async def _drive_calling_defaults_prefill(base_url: str, session_id: str) -> None:
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

            async def handle_resolve(route: Route) -> None:
                query = parse_qs(urlparse(route.request.url).query)
                host_id = (query.get("host_id") or [""])[0]
                if host_id == _HOST_TMB:
                    body = {
                        "agent_id": _AGENT_CLAUDE,
                        "harness": "claude-native",
                        "model": _CLAUDE_MODEL,
                        "effort": _CLAUDE_EFFORT,
                        "sources": {"agent": "project", "model": "project", "effort": "project"},
                        "problems": [],
                    }
                else:
                    body = {
                        "agent_id": _AGENT_CODEX,
                        "harness": "codex",
                        "model": _CODEX_MODEL,
                        "effort": _CODEX_EFFORT,
                        "sources": {"agent": "project", "model": "project", "effort": "project"},
                        "problems": [],
                    }
                await route.fulfill(json=body)

            await page.route(
                "**/v1/hosts",
                lambda route: route.fulfill(
                    json={
                        "hosts": [
                            {
                                "host_id": _HOST_HDS,
                                "name": "host-hds",
                                "owner": "e2e",
                                "status": "online",
                            },
                            {
                                "host_id": _HOST_TMB,
                                "name": "host-tmb",
                                "owner": "e2e",
                                "status": "online",
                            },
                        ]
                    }
                ),
            )
            await stub_empty_host_picker_data(page, _HOST_HDS)
            await stub_empty_host_picker_data(page, _HOST_TMB)
            # The SDK picker labels an unknown model by its id, but the native
            # Claude picker only labels catalog rows — give the TMB stub the one
            # row the resolve names so the picker can show it.
            await page.route(
                f"**/v1/hosts/{_HOST_TMB}/harnesses/claude-native/model-options",
                lambda route: route.fulfill(
                    json={"models": [{"id": _CLAUDE_MODEL, "displayName": _CLAUDE_MODEL}]}
                ),
            )
            await page.route(
                "**/v1/agents",
                lambda route: route.fulfill(
                    json={
                        "data": [
                            {
                                "id": _AGENT_CODEX,
                                "name": "codex-sdk",
                                "display_name": "Codex SDK",
                                "description": "OpenAI's coding agent",
                                "harness": "codex",
                                "skills": [],
                            },
                            {
                                "id": _AGENT_CLAUDE,
                                "name": "claude-native-ui",
                                "display_name": "Claude Code",
                                "description": "Anthropic's coding agent",
                                "harness": "claude-native",
                                "skills": [],
                            },
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
                        "config": {"host_id": _HOST_HDS, "workspace": _ROOT},
                    }
                ),
            )
            await page.route(
                f"**/v1/projects/{_PROJECT_ID}/host-roots",
                lambda route: route.fulfill(
                    json={
                        "roots": [{"host_id": _HOST_HDS, "workspace": _ROOT, "source": "config"}],
                        "default_host_id": _HOST_HDS,
                        "default_host_reason": "config",
                    }
                ),
            )
            await page.route("**/v1/calling-defaults/resolve*", handle_resolve)
            await page.route(
                "**/v1/calling-defaults/catalogs*",
                lambda route: route.fulfill(json={"rows": []}),
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

            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Untouched: the HDS resolve (Codex SDK · gpt-6-sol · high) owns the
            # pickers with no interaction at all.
            trigger = page.get_by_test_id("new-chat-landing-agent-select")
            await expect(trigger).to_have_attribute(
                "aria-label", re.compile("Codex SDK"), timeout=15_000
            )
            await expect(trigger).to_contain_text(_CODEX_MODEL)
            await expect(trigger).to_contain_text("High")
            host_chip = page.get_by_test_id("new-chat-landing-host-chip")
            await expect(host_chip).to_have_attribute("aria-label", re.compile("host-hds"))

            # A host switch the user did not touch: the untouched agent / model /
            # effort follow the TMB resolve (Claude Code · opus-5-5 · xhigh).
            await host_chip.click()
            await page.get_by_test_id(f"new-chat-landing-host-{_HOST_TMB}").click()
            await _wait_for_host_menu_closed(page)
            await expect(host_chip).to_have_attribute("aria-label", re.compile("host-tmb"))
            await expect(trigger).to_have_attribute(
                "aria-label", re.compile("Claude Code"), timeout=15_000
            )
            await expect(trigger).to_contain_text(_CLAUDE_MODEL)
            await expect(trigger).to_contain_text("xHigh")

            # Back on HDS the seed re-resolves its set; the create carries the
            # values the picker shows.
            await host_chip.click()
            await page.get_by_test_id(f"new-chat-landing-host-{_HOST_HDS}").click()
            await _wait_for_host_menu_closed(page)
            await expect(host_chip).to_have_attribute("aria-label", re.compile("host-hds"))
            await expect(trigger).to_have_attribute(
                "aria-label", re.compile("Codex SDK"), timeout=15_000
            )
            await expect(trigger).to_contain_text(_CODEX_MODEL)
            await expect(trigger).to_contain_text("High")

            await page.get_by_test_id("new-chat-landing-input").fill("start here")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_for_create(create_bodies)
            body = create_bodies[0]
            assert body["project_id"] == _PROJECT_ID, body
            assert body["host_id"] == _HOST_HDS, body
            assert body["agent_id"] == _AGENT_CODEX, body
            assert body["model_override"] == _CODEX_MODEL, body
            assert body["reasoning_effort"] == _CODEX_EFFORT, body
        finally:
            await browser.close()
