"""E2E: "Create custom agent" dialog on the new-session landing page.

Covers the user journey of creating a custom agent from the agent picker
dropdown, configuring it (name, description, MCP tools), and submitting the
form to create a session with the bundled agent. Form and target-state variants
are covered in ``web/src/shell/NewChatDialog.test.tsx``.

Uses the same route-stubbing approach as ``test_start_session.py``: the
server's ``/v1/hosts``, ``/v1/agents``, and ``POST /v1/sessions`` are faked
so the tests don't need a real host. The create POST is intercepted to
capture the multipart request body for assertion.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop as _run_in_fresh_loop
from tests.e2e_ui.start_session.helpers import stub_empty_host_picker_data

# Stubbed host the composer auto-selects.
_HOST_ID = "host_e2e"
# Bare create endpoint — intercepts POST but lets GET through.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")


async def _wait_until(predicate, *, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _agents_body() -> str:
    """Single Claude Code agent for the stub.

    ``harness: "claude-native"`` matches the built-in catalog shape: without a
    harness id the picker treats the row as unrunnable and disables it.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_claude_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": "claude-native",
                    "skills": [],
                }
            ]
        }
    )


def _hosts_body() -> str:
    """One online host the composer picks, with the Claude harness ready.

    ``configured_harnesses`` is the wire shape the picker's readiness gate
    reads; without it the harness reads as unavailable.
    """
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": "e2e-host",
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": {"claude-native": True},
                }
            ]
        }
    )


def _managed_info_body() -> str:
    """Stub body for ``GET /v1/info``: a managed deployment offering a sandbox.

    ``managed_sandboxes_enabled: true`` + ``sandbox_provider: "lakebox"`` makes
    the picker offer (and default to) the "Databricks Sandbox" target, which is
    the shape that gates the "Create custom agent" affordance.
    """
    return json.dumps(
        {
            "accounts_enabled": False,
            "login_url": None,
            "needs_setup": False,
            "databricks_features": True,
            "managed_sandboxes_enabled": True,
            "sandbox_provider": "lakebox",
            "server_version": "0.0.0-e2e",
            "smart_routing_enabled": False,
        }
    )


async def _register_routes(
    page,
    *,
    created_session_id: str,
    create_requests: list[dict[str, Any]],
    custom_agent_name: str = "custom-agent",
    managed: bool = False,
) -> None:
    """Install stubs for hosts, agents, session create, and events.

    When ``managed`` is set, also stub ``GET /v1/info`` so the picker enters
    managed mode and offers the sandbox target.
    """

    custom_agents: list[dict[str, Any]] = []

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_events(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
        )

    async def handle_sessions(route: Route) -> None:
        if route.request.method == "POST":
            # Capture multipart or JSON create requests.
            content_type = route.request.headers.get("content-type", "")
            if "multipart" in content_type:
                # For multipart, we can't easily parse the binary body in
                # Playwright, so just record that a multipart POST happened.
                create_requests.append({"__multipart__": True})
            else:
                create_requests.append(route.request.post_data_json)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": created_session_id, "session_id": created_session_id}),
            )
        else:
            await route.continue_()

    async def handle_agent_scan(route: Route) -> None:
        # Neutralize agent discovery so only the stubbed Claude agent feeds the
        # picker. On the shared e2e_ui server, sessions other tests left behind
        # would otherwise leak in as discovered custom agents and change the
        # picker's "Other..." group contents while these tests exercise a fixed
        # Agent roster.
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": []}),
        )

    async def handle_custom_agents(route: Route) -> None:
        if route.request.url.endswith("/contents"):
            await route.fulfill(status=200, content_type="application/gzip", body=b"agent-bundle")
            return
        if route.request.method == "POST":
            created = {
                "id": "ca_e2e_created",
                "name": custom_agent_name,
                "description": None,
                "harness": "claude-sdk",
                "model": "claude-sonnet-4-20250514",
                "version": 1,
                "created_at": 1,
                "updated_at": 1,
                "instructions": None,
            }
            custom_agents[:] = [created]
            await route.fulfill(
                status=201, content_type="application/json", body=json.dumps(created)
            )
            return
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": custom_agents, "has_more": False}),
        )

    if managed:

        async def handle_info(route: Route) -> None:
            await route.fulfill(
                status=200, content_type="application/json", body=_managed_info_body()
            )

        await page.route("**/v1/info", handle_info)
    await page.route("**/v1/hosts", handle_hosts)
    await stub_empty_host_picker_data(page, _HOST_ID)
    await page.route("**/v1/agents", handle_agents)
    await page.route(re.compile(r"/v1/custom-agents(?:/.*)?(?:\?.*)?$"), handle_custom_agents)
    await page.route("**/v1/sessions/*/events", handle_events)
    await page.route(_SESSIONS_RE, handle_sessions)
    # Registered after the broad sessions glob so it wins the visibility=mine discovery
    # scan; the bare conversation-list GET still falls through to handle_sessions.
    await page.route(
        re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
    )


async def _seed_workspace(page) -> None:
    """Seed a recent workspace so the composer can enable Send."""
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


async def _open_create_agent(page) -> None:
    """Open the agent picker and click "Create custom agent".

    The create action lives in the "Other..." submenu even on a fresh
    server, so open that submenu before choosing the create item.
    """
    await page.get_by_test_id("new-chat-landing-agent-select").click()
    await page.get_by_test_id("new-chat-landing-custom-agents").click()
    await page.get_by_test_id("new-chat-landing-create-agent").click()


async def _wait_for_host_menu_closed(page) -> None:
    """Wait out the host dropdown's exit animation after a target switch.

    The host menu stays mounted while it animates out and returns focus to its
    trigger when it unmounts; opening the agent picker before that point gets
    the fresh menu dismissed by that close-refocus.
    """
    await expect(page.get_by_test_id("new-chat-landing-host-menu")).to_have_count(0)


async def _choose_model(page, model_id: str) -> None:
    """Pick *model_id* through the lead member's Model submenu.

    The host stub answers an empty model catalog, so the row list can't offer
    this pinned id — select the "Other model…" entry, type the id, and commit.
    """
    await page.get_by_test_id("agent-member-trigger").click()
    await page.get_by_test_id("agent-member-model").click()
    await page.get_by_test_id("agent-member-model-other").click()
    field = page.get_by_test_id("agent-member-model-input")
    await field.fill(model_id)
    await field.press("Enter")
    await expect(page.get_by_test_id("agent-member-agent-model-value")).to_have_text(model_id)
    # Enter leaves the picker open; Escape dismisses it so the dialog footer
    # stays clickable.
    await page.keyboard.press("Escape")
    await expect(field).to_be_hidden()


# ── Tests ──────────────────────────────────────────────────────────


def test_create_agent_dialog_opens_from_dropdown(
    seeded_session: tuple[str, str],
) -> None:
    """The agent dropdown shows a "Create custom agent" item that opens the dialog."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_dialog_opens(base_url, session_id))


async def _drive_dialog_opens(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_requests: list[dict[str, Any]] = []
            await _register_routes(
                page,
                created_session_id=session_id,
                create_requests=create_requests,
                custom_agent_name="test-agent",
            )
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the agent dropdown, then the Other submenu.
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-custom-agents").click()

            # "Create custom agent" item should be visible.
            create_item = page.get_by_test_id("new-chat-landing-create-agent")
            await expect(create_item).to_be_visible()

            # Click it — dialog should open.
            await create_item.click()
            dialog = page.get_by_test_id("create-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)

            # Verify form fields are present.
            await expect(page.get_by_test_id("create-agent-name")).to_be_visible()
            await expect(page.get_by_test_id("create-agent-description")).to_be_visible()
            await expect(page.get_by_test_id("agent-member-trigger")).to_be_visible()
            await expect(page.get_by_test_id("create-agent-instructions")).to_be_visible()
            await expect(page.get_by_test_id("create-agent-add-mcp")).to_be_visible()
        finally:
            await browser.close()


def test_create_agent_submits_multipart_bundle(
    seeded_session: tuple[str, str],
) -> None:
    """Creating a custom agent and sending produces a multipart POST."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_create_and_submit(base_url, session_id))


async def _drive_create_and_submit(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_requests: list[dict[str, Any]] = []
            await _register_routes(
                page,
                created_session_id=session_id,
                create_requests=create_requests,
                custom_agent_name="test-agent",
            )
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open dropdown → Create custom agent.
            await _open_create_agent(page)

            dialog = page.get_by_test_id("create-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)

            # Fill in agent details.
            await page.get_by_test_id("create-agent-name").fill("test-agent")
            await page.get_by_test_id("create-agent-description").fill("A test agent")
            await _choose_model(page, "claude-sonnet-4-20250514")
            await page.get_by_test_id("create-agent-instructions").fill(
                "You are a test assistant."
            )

            # Submit the dialog.
            await page.get_by_test_id("create-agent-submit").click()

            # Dialog should close.
            await expect(dialog).to_be_hidden(timeout=5_000)

            # The agent chip should now show the custom agent name.
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "test-agent"
            )

            # Type a message and submit the session.
            await page.get_by_test_id("new-chat-landing-input").fill("hello world")
            await page.get_by_test_id("new-chat-landing-submit").click()

            # The create POST should have been a multipart request (the bundle).
            await _wait_until(lambda: len(create_requests) == 1)
            assert create_requests[0].get("__multipart__") is True, (
                f"Expected multipart POST, got: {create_requests[0]}"
            )
        finally:
            await browser.close()


def test_create_agent_with_mcp_server(
    seeded_session: tuple[str, str],
) -> None:
    """Adding an MCP server in the dialog includes it in the bundle."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_mcp_server(base_url, session_id))


async def _drive_mcp_server(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_requests: list[dict[str, Any]] = []
            await _register_routes(
                page,
                created_session_id=session_id,
                create_requests=create_requests,
                custom_agent_name="mcp-agent",
            )
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open dropdown → Create custom agent.
            await _open_create_agent(page)

            dialog = page.get_by_test_id("create-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)

            # Fill in agent name and model (both required).
            await page.get_by_test_id("create-agent-name").fill("mcp-agent")
            await _choose_model(page, "claude-sonnet-4-20250514")

            # Add an MCP server.
            await page.get_by_test_id("create-agent-add-mcp").click()

            # An MCP entry card should appear.
            mcp_entry = page.get_by_test_id("create-agent-mcp-entry")
            await expect(mcp_entry).to_be_visible()

            # Fill in MCP server details (stdio transport is default).
            await page.get_by_test_id("create-agent-mcp-name").fill("github")
            await page.get_by_test_id("create-agent-mcp-command").fill("npx")
            await page.get_by_test_id("create-agent-mcp-args").fill(
                "-y @modelcontextprotocol/server-github"
            )
            await page.get_by_test_id("create-agent-mcp-env").fill("GITHUB_TOKEN=ghp_test123")

            # Submit the dialog.
            await page.get_by_test_id("create-agent-submit").click()
            await expect(dialog).to_be_hidden(timeout=5_000)

            # Submit session.
            await page.get_by_test_id("new-chat-landing-input").fill("list repos")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_requests) == 1)
            assert create_requests[0].get("__multipart__") is True
        finally:
            await browser.close()


def test_create_agent_cancel_closes_dialog(
    seeded_session: tuple[str, str],
) -> None:
    """Cancelling the dialog closes it without creating an agent."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_cancel(base_url, session_id))


async def _drive_cancel(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_requests: list[dict[str, Any]] = []
            await _register_routes(
                page, created_session_id=session_id, create_requests=create_requests
            )
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open dropdown → Create custom agent.
            await _open_create_agent(page)

            dialog = page.get_by_test_id("create-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)

            # Fill some fields.
            await page.get_by_test_id("create-agent-name").fill("should-not-persist")

            # Cancel.
            cancel_btn = dialog.get_by_role("button", name="Cancel")
            await cancel_btn.click()

            # Dialog should close.
            await expect(dialog).to_be_hidden(timeout=5_000)

            # The agent chip should still show the original agent (Claude Code).
            # Agent identity rides the accessible name: the visible text is the
            # resolved model, which is empty on this stub host.
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_have_attribute(
                "aria-label", re.compile("Claude Code")
            )
        finally:
            await browser.close()


def test_create_agent_hidden_on_sandbox(
    seeded_session: tuple[str, str],
) -> None:
    """On a managed sandbox target, "Create custom agent" is hidden.

    A sandbox provisions its runner from a baked image with no create path for
    an uploaded bundle, so the affordance is omitted from the picker. Switching
    to a connected host brings it back.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_hidden_on_sandbox(base_url, session_id))


async def _drive_hidden_on_sandbox(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_requests: list[dict[str, Any]] = []
            # Managed mode: the picker defaults to the "Databricks Sandbox"
            # target, alongside the one connected host.
            await _register_routes(
                page,
                created_session_id=session_id,
                create_requests=create_requests,
                custom_agent_name="pending-agent",
                managed=True,
            )
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # Sanity: the sandbox is the default managed target.
            await expect(page.get_by_test_id("new-chat-landing-host-chip")).to_have_attribute(
                "aria-label",
                re.compile("Databricks Sandbox"),
            )

            # On the sandbox, "Create custom agent" is not offered (a managed
            # sandbox has no create path for an uploaded bundle), so it's never
            # in the DOM.
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await expect(page.get_by_test_id("new-chat-landing-create-agent")).to_have_count(0)

            # Switch to the connected host: the Other submenu offers
            # creation even when the private catalog is still empty.
            await page.keyboard.press("Escape")
            await page.get_by_test_id("new-chat-landing-host-chip").click()
            await page.get_by_test_id(f"new-chat-landing-host-{_HOST_ID}").click()
            await _wait_for_host_menu_closed(page)
            await expect(page.get_by_test_id("new-chat-landing-host-chip")).not_to_have_attribute(
                "aria-label", re.compile("Databricks Sandbox")
            )
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-custom-agents").click()
            create_item = page.get_by_test_id("new-chat-landing-create-agent")
            await expect(create_item).to_be_visible()
            await create_item.click()
            await expect(page.get_by_test_id("create-agent-dialog")).to_be_visible(timeout=5_000)
        finally:
            await browser.close()


def test_saved_agent_is_blocked_when_switching_to_sandbox(
    seeded_session: tuple[str, str],
) -> None:
    """A saved custom Agent stays selected but cannot run on a sandbox.

    Creation now persists the Agent in the private library. Switching targets
    keeps that deliberate selection visible, while disabling both the row and
    Send until the user chooses a connected computer or another Agent.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_saved_agent_blocked_on_sandbox(base_url, session_id))


async def _drive_saved_agent_blocked_on_sandbox(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_requests: list[dict[str, Any]] = []
            await _register_routes(
                page,
                created_session_id=session_id,
                create_requests=create_requests,
                custom_agent_name="pending-agent",
                managed=True,
            )
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Switch to the connected host, then create and persist an Agent.
            await page.get_by_test_id("new-chat-landing-host-chip").click()
            await page.get_by_test_id(f"new-chat-landing-host-{_HOST_ID}").click()
            await _wait_for_host_menu_closed(page)
            await _open_create_agent(page)
            await expect(page.get_by_test_id("create-agent-dialog")).to_be_visible(timeout=5_000)
            await page.get_by_test_id("create-agent-name").fill("pending-agent")
            await _choose_model(page, "claude-sonnet-4-20250514")
            await page.get_by_test_id("create-agent-submit").click()
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "pending-agent"
            )

            # Switch back to the sandbox: the saved pick stays visible but cannot run.
            await page.get_by_test_id("new-chat-landing-host-chip").click()
            await page.get_by_test_id("new-chat-landing-sandbox-option").click()
            await _wait_for_host_menu_closed(page)
            await expect(page.get_by_test_id("new-chat-landing-host-chip")).to_have_attribute(
                "aria-label", re.compile("Databricks Sandbox")
            )
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "pending-agent"
            )
            await page.get_by_test_id("new-chat-landing-input").fill("review this")
            await expect(page.get_by_test_id("new-chat-landing-submit")).to_be_disabled()
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-custom-agents").click()
            saved_agent = page.get_by_test_id("new-chat-landing-agent-ca_e2e_created")
            await expect(saved_agent).to_be_disabled()
        finally:
            await browser.close()
