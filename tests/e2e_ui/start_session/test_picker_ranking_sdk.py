"""New-session picker ranking, SDK grouping, and Windows host availability."""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Locator, Page, Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _register_common_routes,
    _run_in_fresh_loop,
)


def _agents_body() -> str:
    rows = [
        ("ag_claude_e2e", "claude-native-ui", "Claude Code", "claude-native"),
        ("ag_cursor_e2e", "cursor-native-ui", "Cursor", "cursor-native"),
        ("ag_codex_e2e", "codex-native-ui", "Codex", "codex-native"),
        ("ag_opencode_e2e", "opencode-native-ui", "OpenCode", "opencode-native"),
        ("ag_codex_sdk_e2e", "codex-sdk", "Codex SDK", "codex"),
        ("ag_claude_sdk_e2e", "claude-sdk", "Claude SDK", "claude-sdk"),
        ("ag_polly_e2e", "polly", "Polly", "claude-sdk"),
        ("ag_debby_e2e", "debby", "Debby", "claude-sdk"),
    ]
    return json.dumps(
        {
            "data": [
                {
                    "id": agent_id,
                    "name": name,
                    "display_name": display_name,
                    "description": None,
                    "harness": harness,
                    "skills": [],
                }
                for agent_id, name, display_name, harness in rows
            ]
        }
    )


def _host_body(platform: str, configured_harnesses: dict[str, bool] | None) -> str:
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": "ranking-host",
                    "owner": "e2e",
                    "status": "online",
                    "platform": platform,
                    "configured_harnesses": configured_harnesses,
                }
            ]
        }
    )


async def _setup(
    page: Page,
    session_id: str,
    platform: str,
    configured_harnesses: dict[str, bool] | None,
) -> list[dict[str, Any]]:
    create_bodies: list[dict[str, Any]] = []
    await _register_common_routes(
        page,
        created_session_id=session_id,
        create_bodies=create_bodies,
        agents_body=_agents_body(),
    )
    await page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=_host_body(platform, configured_harnesses),
        ),
    )

    async def empty_scan(route: Route) -> None:
        await route.fulfill(json={"data": []})

    await page.route(re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), empty_scan)
    await page.add_init_script(
        f'localStorage.setItem("omnigent:recent-workspaces", '
        f'JSON.stringify({{{json.dumps(_HOST_ID)}: ["/work/repo"]}}));'
    )
    return create_bodies


async def _open_picker(page: Page) -> None:
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    await page.get_by_test_id("new-chat-landing-agent-select").click()
    await expect(page.get_by_text("Harnesses", exact=True)).to_be_visible()


async def _precedes(first: Locator, second: Locator) -> bool:
    return await first.evaluate(
        "(node, other) => Boolean(node.compareDocumentPosition(other)"
        " & Node.DOCUMENT_POSITION_FOLLOWING)",
        await second.element_handle(),
    )


def test_windows_native_rows_and_sdk_section(seeded_session: tuple[str, str]) -> None:
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_windows(base_url, session_id))


async def _drive_windows(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _setup(
                page,
                session_id,
                "win32",
                {
                    "claude-native": True,
                    "codex-native": True,
                    "cursor-native": True,
                    "opencode-native": True,
                    "codex": True,
                    "claude-sdk": True,
                },
            )
            await page.goto(f"{base_url}/")
            await _open_picker(page)
            sdk_heading = page.get_by_text("SDK", exact=True)
            agents_heading = page.get_by_text("Agents", exact=True)
            codex_sdk = page.get_by_test_id("new-chat-landing-agent-ag_codex_sdk_e2e")
            claude_sdk = page.get_by_test_id("new-chat-landing-agent-ag_claude_sdk_e2e")
            polly = page.get_by_test_id("new-chat-landing-agent-ag_polly_e2e")
            debby = page.get_by_test_id("new-chat-landing-agent-ag_debby_e2e")
            for first, second in (
                (sdk_heading, codex_sdk),
                (codex_sdk, claude_sdk),
                (claude_sdk, agents_heading),
                (agents_heading, polly),
                (polly, debby),
            ):
                assert await _precedes(first, second)
            await expect(codex_sdk).not_to_have_attribute("aria-disabled", "true")
            await expect(claude_sdk).not_to_have_attribute("aria-disabled", "true")

            await page.get_by_test_id("new-chat-landing-harness-more").click()
            for agent_id in ("ag_claude_e2e", "ag_codex_e2e", "ag_cursor_e2e"):
                row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
                await expect(row).to_have_attribute("aria-disabled", "true")
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-warning-{agent_id}")
                ).to_have_attribute("aria-label", "not on Windows")
            await page.get_by_test_id("new-chat-landing-agent-ag_claude_e2e").hover()
            await expect(
                page.get_by_test_id("new-chat-landing-agent-tooltip-ag_claude_e2e")
            ).to_contain_text("ranking-host (Windows) can't host")
        finally:
            await browser.close()


def test_recent_open_code_launch_leads_picker(seeded_session: tuple[str, str]) -> None:
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_recency(base_url, session_id))


async def _drive_recency(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies = await _setup(
                page,
                session_id,
                "darwin",
                {
                    "claude-native": True,
                    "codex-native": True,
                    "cursor-native": True,
                    "opencode-native": True,
                    "codex": True,
                    "claude-sdk": True,
                },
            )
            await page.goto(f"{base_url}/")
            await _open_picker(page)
            heading = page.get_by_text("Harnesses", exact=True)
            claude = page.get_by_test_id("new-chat-landing-agent-ag_claude_e2e")
            cursor = page.get_by_test_id("new-chat-landing-agent-ag_cursor_e2e")
            codex = page.get_by_test_id("new-chat-landing-agent-ag_codex_e2e")
            assert await _precedes(heading, claude)
            assert await _precedes(claude, cursor)
            assert await _precedes(cursor, codex)
            await expect(heading.locator("xpath=following-sibling::*[1]")).to_contain_text(
                "Claude Code"
            )
            await page.get_by_test_id("new-chat-landing-harness-more").click()
            await page.get_by_test_id("new-chat-landing-agent-ag_opencode_e2e").click()
            await page.get_by_test_id("new-chat-landing-input").fill("explore the repo")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_function(
                'JSON.parse(localStorage.getItem("omnigent:recent-harnesses") || "[]")[0]'
                ' === "opencode-native"'
            )
            assert create_bodies and create_bodies[0]["agent_id"] == "ag_opencode_e2e"

            await page.goto(f"{base_url}/")
            await _open_picker(page)
            await expect(
                page.get_by_text("Harnesses", exact=True).locator("xpath=following-sibling::*[1]")
            ).to_contain_text("OpenCode")
        finally:
            await browser.close()


def test_unknown_readiness_keeps_harnesses_under_other(seeded_session: tuple[str, str]) -> None:
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_unknown(base_url, session_id))


async def _drive_unknown(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _setup(page, session_id, "darwin", None)
            # A selected harness stays inline even when readiness is unknown.
            await page.add_init_script(
                'localStorage.setItem("omnigent:last-agent-id", "ag_codex_sdk_e2e");'
            )
            await page.goto(f"{base_url}/")
            await _open_picker(page)
            heading = page.get_by_text("Harnesses", exact=True)
            more = page.get_by_test_id("new-chat-landing-harness-more")
            assert await _precedes(heading, more)
            assert await heading.evaluate(
                "(node, other) => node.nextElementSibling === other", await more.element_handle()
            )
            for agent_id in ("ag_claude_e2e", "ag_codex_e2e", "ag_cursor_e2e", "ag_opencode_e2e"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
                ).to_have_count(0)
            await more.click()
            for agent_id in ("ag_claude_e2e", "ag_codex_e2e", "ag_cursor_e2e", "ag_opencode_e2e"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
                ).to_be_visible()
        finally:
            await browser.close()
