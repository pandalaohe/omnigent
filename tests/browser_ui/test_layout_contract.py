"""Sweep Settings and the session UI for text and control geometry regressions."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui._layout_helpers import assert_layout
from tests.browser_ui.chat._geometry_helpers import open_live
from tests.browser_ui.chat.conftest import chat_session_contract as chat_session_contract
from tests.browser_ui.chat.session_contract import ChatSessionContract, list_payload
from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_features as appearance_features
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url

_VIEWPORTS = [(1280, 800), (390, 844)]
_SETTINGS = {
    "account": "Account",
    "admin": "Admin settings",
    "agents": "Agents",
    "api-keys": "API keys",
    "appearance": "Appearance",
    "archived": "Archived sessions",
    "browser": "Browser",
    "cli": "Local CLI",
    "general": "General",
    "git": "Git",
    "import": "Import sessions",
    "instructions": "Startup prompts",
    "integrations": "Sandbox Integrations",
    "mcp": "MCP",
    "members": "Members",
    "model-providers": "Model providers",
    "policies": "Global Policies",
    "sharing": "Session sharing",
    "shortcuts": "Keyboard shortcuts",
    "updates": "Updates",
}


def test_settings_layout_covers_current_routes() -> None:
    nav = Path(__file__).resolve().parents[2] / "web/src/shell/settingsNav.tsx"
    section_list = nav.read_text().split("const SECTION_IDS:", 1)[1].split("];", 1)[0]
    assert {line.split('"')[1] for line in section_list.splitlines() if '"' in line} == set(
        _SETTINGS
    )


def test_layout_probe_reports_geometry_and_respects_intentional_overlap(
    page: Page, output_path: str
) -> None:
    page.set_content("""
        <style>
          section { position: relative; width: 200px; height: 110px }
          section button { position: absolute; top: 10px; left: 5px; width: 82px; height: 22px;
                           white-space: nowrap }
          section a { position: absolute; top: 20px; left: 77px }
          section p { position: absolute; top: 46px; margin: 0; width: 70px;
                      overflow: hidden; white-space: nowrap }
        </style>
        <section id="fixture">
          <button>First control</button>
          <a href="#">Second control</a>
          <p>Label with long text</p>
          <span style="position:absolute;left:178px;top:75px">A long word</span>
        </section>
        """)
    scope = page.locator("#fixture")
    with pytest.raises(AssertionError) as outcome:
        assert_layout(scope, "probe", Path(output_path))
    assert 'overlap: button "First control" ↔ a "Second control"' in str(outcome.value)
    assert 'clipped: text "Label with long text"' in str(outcome.value)
    assert "horizontal overflow:" in str(outcome.value)
    assert list(Path(output_path).glob("probe-*.png"))

    scope.get_by_role("link").evaluate("el => el.setAttribute('data-layout-overlay', '')")
    scope.locator("p").evaluate("el => el.style.textOverflow = 'ellipsis'")
    scope.locator("span").evaluate("el => el.style.left = '110px'")
    assert_layout(scope, "probe", Path(output_path))


@pytest.mark.parametrize(("width", "height"), _VIEWPORTS)
@pytest.mark.parametrize("section", _SETTINGS)
def test_settings_layout(
    page: Page,
    appearance_url: str,
    browser_contract: BrowserContract,
    output_path: str,
    width: int,
    height: int,
    section: str,
) -> None:
    browser_contract.json(
        "/v1/info",
        {
            "accounts_enabled": True,
            "single_user": False,
            "login_url": "/login",
            "enabled_connections": ["mcp", "github"],
            "api_keys_available": True,
            "sibling_sessions": True,
            "features": {},
        },
    )
    browser_contract.json(
        "/v1/me", {"user_id": "example", "is_admin": True, "email": "user@example.test"}
    )
    browser_contract.json("/auth/users", {"users": []})
    browser_contract.json(
        "/v1/me/api-keys", {"keys": [], "read_only": [], "available_scopes": ["usage"]}
    )
    browser_contract.json(
        "/v1/sharing",
        {
            "object": "sharing",
            "sharing_mode": "off",
            "editable": True,
            "options": ["off"],
            "public_sharing_enabled": False,
            "public_sharing_editable": True,
            "default_public_sessions": "off",
            "default_public_sessions_options": ["off"],
            "sibling_session_destinations": "any_host",
            "sibling_session_destinations_options": ["any_host"],
        },
    )
    browser_contract.json(
        "/v1/sessions/archived-facets", {"projects": [], "host_ids": [], "agent_names": []}
    )
    browser_contract.json("/v1/model-connections", {"connections": [], "can_manage": True})
    browser_contract.json("/v1/connections/github/status", {"enabled": True, "connected": False})
    browser_contract.json("/v1/mcp-registry/services", {"data": [], "next_cursor": None})
    browser_contract.json("/v1/policies", list_payload([]))
    browser_contract.json("/v1/policy-registry", list_payload([]))
    if section in {"browser", "cli", "updates"}:
        page.add_init_script(
            "window.omnigentDesktop = {kind: 'electron', browserOpenOrNavigate() {}}"
        )
    page.set_viewport_size({"width": width, "height": height})
    page.goto(f"{appearance_url}/settings/{section}")
    heading = page.get_by_role("heading", name=_SETTINGS[section], exact=True).first
    expect(heading).to_be_visible(timeout=5_000)
    main = page.get_by_role("main").filter(has=heading).last
    assert_layout(main, f"settings-{section}", Path(output_path))


@pytest.mark.parametrize(("width", "height"), _VIEWPORTS)
def test_session_chat_layout(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
    width: int,
    height: int,
) -> None:
    chat = chat_session_contract
    chat.seed_transcript(1)
    chat.seed_sidebar_sessions(["Example follow-up", "Review a draft"])
    chat.contract.json(f"/v1/sessions/{chat.session_id}/policies", list_payload([]))
    chat.contract.json("/v1/policy-registry", list_payload([]))
    chat.contract.json(f"/v1/sessions/{chat.session_id}/owner", {"owner": None})
    open_live(page, chat, {"width": width, "height": height})
    shell = page.locator(".app-shell")
    if shell.get_attribute("data-sidebar-open") != "true":
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    expect(shell).to_have_attribute("data-sidebar-open", "true")
    sidebar = page.get_by_role("complementary", name="Conversations")
    expect(sidebar.get_by_text("Example follow-up", exact=True)).to_be_visible()
    sidebar.evaluate("el => Promise.all(el.getAnimations().map(animation => animation.finished))")
    if width < 768:
        # On phones the drawer covers the chat, which can be read after dismissal.
        assert_layout(sidebar, "session-sidebar", Path(output_path))
        page.get_by_test_id("sidebar-scrim").click(
            position={"x": width - 12, "y": height // 2}, timeout=5_000
        )
        expect(shell).not_to_have_attribute("data-sidebar-open", "true")
        assert_layout(page.locator("[data-chat-surface]"), "session-chat", Path(output_path))
    else:
        assert_layout(shell, "session-chat", Path(output_path))
        page.get_by_role("button", name="Expand right panel", exact=True).click()
        rail = page.locator('aside[aria-label="Workspace"]')
        expect(rail).to_have_attribute("data-state", "open")
        page.get_by_role("button", name="Collapse right panel", exact=True).click()
        expect(rail).to_have_attribute("data-state", "closed")
        expect(
            page.locator('aside[aria-label="Workspace"][data-animate-visibility]')
        ).to_have_count(0)
        assert_layout(shell, "session-chat-workspace-closed", Path(output_path))


@pytest.mark.parametrize(
    "width",
    [
        pytest.param(
            width,
            id=f"{width}x{height}",
            marks=pytest.mark.browser_context_args(
                has_touch=True,
                viewport={"width": width, "height": height},
                screen={"width": 390, "height": 844},
            ),
        )
        for width, height in _VIEWPORTS
    ],
)
def test_session_terminal_layout(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
    width: int,
) -> None:
    chat = chat_session_contract
    assert page.evaluate("window.innerWidth") == width
    chat.harness = "codex-native"
    chat.update_session(labels={"omnigent.ui": "terminal", "omnigent.wrapper": "codex-native-ui"})
    session = f"/v1/sessions/{chat.session_id}"
    terminal_id = "terminal_codex_main"
    chat.contract.json(f"{session}/codex_goal", {"goal": None})
    chat.contract.json(
        f"{session}/resources/terminals",
        list_payload(
            [
                {
                    "id": terminal_id,
                    "object": "terminal",
                    "name": "codex",
                    "metadata": {"running": True, "terminal_name": "codex", "session_key": "main"},
                }
            ]
        ),
    )
    chat.contract.websocket(
        f"**{session}/resources/terminals/{terminal_id}/attach*",
        lambda socket: socket.send(b"$ "),
    )
    page.goto(chat.url)
    if width < 768:
        page.get_by_role("button", name="Conversation actions", exact=True).click()
        page.get_by_role("menuitem", name="Terminal", exact=True).click()
    else:
        page.get_by_role("radio", name="Terminal view", exact=True).click()
    terminal = page.get_by_test_id("terminal-view").filter(visible=True)
    expect(terminal).to_have_attribute("data-state", "connected", timeout=5_000)
    expect(terminal.get_by_test_id("terminal-key-row")).to_be_visible(timeout=5_000)
    assert_layout(terminal, "session-terminal", Path(output_path))


@pytest.mark.parametrize(("width", "height"), _VIEWPORTS)
def test_session_approval_layout(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
    width: int,
    height: int,
) -> None:
    chat = chat_session_contract
    open_live(page, chat, {"width": width, "height": height})
    chat.emit(
        {
            "event": "response.elicitation_request",
            "data": {
                "elicitation_id": "layout-approval",
                "params": {
                    "mode": "form",
                    "message": "Approve this operation?",
                    "phase": "pre_tool_use",
                    "policy_name": "permission",
                    "requestedSchema": {},
                },
            },
        }
    )
    card = page.get_by_test_id("approval-card")
    expect(card.get_by_role("button", name="Approve", exact=True)).to_be_visible()
    assert_layout(card, "session-approval", Path(output_path))
