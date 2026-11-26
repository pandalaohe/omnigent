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
_LONG_MEMBER = "example-member-with-an-uncommonly-long-account-name-for-testing"
_SETTINGS = {
    "account": ("Account", "Sign out"),
    "admin": ("Admin settings", "Nested agent destinations"),
    "agents": ("Agents", "No custom agents yet."),
    "api-keys": ("API keys", "No API keys yet."),
    "appearance": ("Appearance", "Interface typography"),
    "archived": ("Archived sessions", "No archived sessions match."),
    "browser": ("Browser", "Your recently visited pages will appear here."),
    "cli": ("Local CLI", "CLI status is unavailable."),
    "general": ("General", "Approvals & Questions"),
    "git": ("Git", "Worktrees"),
    "import": ("Import sessions", "Import from a machine"),
    "instructions": ("Startup prompts", "Route total"),
    "integrations": ("Sandbox Integrations", "Connect GitHub"),
    "mcp": ("MCP", "No managed services are available for your account."),
    "members": ("Members", "No members yet."),
    "model-providers": (
        "Model providers",
        "No model providers yet. Add a provider to get started.",
    ),
    "policies": (
        "Global Policies",
        "No global policies configured. Add one to apply it to all sessions.",
    ),
    "sharing": ("Session sharing", "Sharing mode"),
    "shortcuts": ("Keyboard shortcuts", "Show key bar on this page"),
    "system": ("System", "Usage telemetry"),
    "updates": ("Updates", "Update settings are unavailable."),
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


def test_layout_probe_detects_fully_overlapping_controls(page: Page, output_path: str) -> None:
    page.set_content("""
        <section id="fixture" style="display:grid;width:120px">
          <button aria-label="First action" style="grid-area:1/1;height:40px">
            <svg width="16" height="16" aria-hidden="true"><circle cx="8" cy="8" r="7"/></svg>
          </button>
          <button aria-label="Second action" style="grid-area:1/1;height:40px">
            <svg width="16" height="16" aria-hidden="true"><circle cx="8" cy="8" r="7"/></svg>
          </button>
        </section>
        """)
    with pytest.raises(AssertionError) as outcome:
        assert_layout(page.locator("#fixture"), "fully-overlapping-controls", Path(output_path))
    assert 'button "First action" ↔ button "Second action"' in str(outcome.value)


def test_layout_probe_uses_visible_control_area_in_horizontal_scroller(
    page: Page, output_path: str
) -> None:
    page.set_content("""
        <section id="fixture" style="display:flex;width:145px">
          <div style="width:100px;flex:none;overflow-x:auto">
            <button aria-label="Scrollable action"
                    style="position:relative;width:130px;height:32px">
              <svg width="16" height="16" aria-hidden="true"
                   style="position:absolute;top:8px;right:3px">
                <circle cx="8" cy="8" r="7"/>
              </svg>
            </button>
          </div>
          <button aria-label="Fixed action" style="width:45px;height:32px"></button>
        </section>
        """)
    scope = page.locator("#fixture")
    assert_layout(scope, "clipped-control", Path(output_path))

    scope.get_by_role("button", name="Fixed action").evaluate(
        "el => el.style.transform = 'translateX(-10px)'"
    )
    with pytest.raises(AssertionError) as outcome:
        assert_layout(scope, "visible-control-overlap", Path(output_path))
    assert 'button "Scrollable action" ↔ button "Fixed action"' in str(outcome.value)


def test_layout_probe_checks_text_on_an_intentional_hit_area(page: Page, output_path: str) -> None:
    page.set_content("""
        <section id="fixture" style="position:relative;width:240px">
          <button data-layout-overlay="hit-area"
                  style="width:100%;height:38px;text-align:left;padding-left:10px">
            Open a session
          </button>
          <button aria-label="More actions"
                  style="position:absolute;top:4px;right:4px;width:32px;height:30px"></button>
        </section>
        """)
    scope = page.locator("#fixture")
    assert_layout(scope, "intentional-hit-area", Path(output_path))

    scope.get_by_role("button", name="More actions").evaluate("el => el.style.left = '35px'")
    with pytest.raises(AssertionError) as outcome:
        assert_layout(scope, "covered-hit-area-label", Path(output_path))
    assert 'button "More actions" ↔ text "Open a session"' in str(outcome.value)


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
            "telemetry": {"enabled": False, "locked": False, "source": "default"},
            "features": {},
        },
    )
    browser_contract.json(
        "/v1/me", {"user_id": "example", "is_admin": True, "email": "user@example.test"}
    )
    populated_members = section == "members" and width == 390
    browser_contract.json(
        "/auth/users",
        {
            "users": [
                {
                    "id": _LONG_MEMBER,
                    "is_admin": False,
                    "has_password": True,
                    "created_at": 1_704_067_200,
                    "last_login_at": 1_704_067_200,
                }
            ]
            if populated_members
            else []
        },
    )
    if populated_members:
        page.add_init_script("""
            const fetchUsers = window.fetch.bind(window);
            window.fetch = async (input, options) => {
                const response = await fetchUsers(input, options);
                if (new URL(response.url, location.href).pathname === '/auth/users') {
                    await new Promise(resolve => setTimeout(resolve, 500));
                }
                return response;
            };
            """)
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
    if section == "agents":
        browser_contract.json(
            "/v1/agents",
            list_payload(
                [
                    {
                        "id": "agent-native",
                        "name": "codex-native-ui",
                        "harness": "codex-native",
                        "product_of": "codex-native",
                        "builtin": True,
                    },
                    {
                        "id": "agent-sdk",
                        "name": "codex-sdk",
                        "harness": "codex",
                        "product_of": "codex",
                        "builtin": True,
                    },
                ]
            ),
        )
        browser_contract.json(
            "/v1/hosts",
            {
                "hosts": [
                    {
                        "host_id": "example-host",
                        "name": "Example host",
                        "owner": "example",
                        "status": "online",
                        "configured_harnesses": {"codex-native": True, "codex": True},
                    }
                ]
            },
        )
        browser_contract.json(
            "/v1/hosts/example-host/agent-settings/codex",
            {
                "family": "codex",
                "root": "/opt/work/agent-home",
                "choosable": False,
                "exists": False,
                "read_only_reason": None,
                "revision": None,
                "fields": [],
                "account": None,
            },
        )
        browser_contract.json(
            "/v1/hosts/example-host/harnesses/codex/model-options", {"models": []}
        )
        browser_contract.json(
            "/v1/hosts/example-host/harnesses/codex/startup", {"environment": None}
        )
        page.add_init_script("""
            localStorage.setItem("omnigent:agent-badge-preferences", JSON.stringify({
              version: 1,
              enabled: true,
              entries: {"agent-sdk": {label: "XS", borderColor: "#ca8a04", textColor: "theme"}},
            }));
        """)
    if section in {"browser", "cli", "updates"}:
        page.add_init_script(
            "window.omnigentDesktop = {kind: 'electron', browserOpenOrNavigate() {}}"
        )
    page.set_viewport_size({"width": width, "height": height})
    page.goto(f"{appearance_url}/settings/{section}")
    title, ready_text = _SETTINGS[section]
    heading = page.get_by_role("heading", name=title, exact=True).first
    expect(heading).to_be_visible(timeout=5_000)
    main = page.get_by_role("main").filter(has=heading).last
    marker = _LONG_MEMBER if populated_members else ready_text
    expect(main.get_by_text(marker, exact=True)).to_be_visible(timeout=5_000)
    if section == "agents":
        table = main.get_by_role("table", name="Model defaults")
        native = table.get_by_text("Codex", exact=True)
        sdk = table.get_by_text("Codex SDK", exact=True)
        sdk_row = table.get_by_role("row").filter(has=page.get_by_text("Codex SDK", exact=True))
        native_row = table.get_by_role("row").filter(has=page.get_by_text("Codex", exact=True))
        badge = sdk_row.get_by_test_id("agent-name-badge")
        kind = sdk_row.get_by_test_id("agent-name-mark")
        expect(badge).to_have_text("XS")
        expect(kind).to_have_text("SDK")
        expect(native_row.get_by_test_id("agent-name-mark")).to_have_count(0)
        for target in (sdk, badge, kind, sdk_row.get_by_role("link", name="Codex SDK details")):
            expect(target).to_be_in_viewport(ratio=1)
        native_box, sdk_box = native.bounding_box(), sdk.bounding_box()
        assert native_box is not None and sdk_box is not None
        assert abs(native_box["x"] - sdk_box["x"]) < 1
    assert_layout(main, f"settings-{section}", Path(output_path))
    if section == "agents":
        main.get_by_role("link", name="Codex SDK details").click()
        edit_badge = page.get_by_role("button", name="Edit badge for Codex SDK")
        expect(edit_badge).to_be_visible(timeout=5_000)
        edit_badge.click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_test_id("agent-badge-editor")).to_be_visible(timeout=5_000)
        if width < 768:
            trigger = dialog.get_by_test_id("agent-badge-color-trigger")
            expect(trigger).to_be_visible()
            assert_layout(dialog, "settings-agents-badge-editor", Path(output_path))
            trigger.click()
            sheet = page.get_by_role("dialog", name="Badge color")
            expect(sheet).to_be_visible(timeout=5_000)
            palette_root = sheet
            min_swatch_width = 34
        else:
            palette_root = dialog
            min_swatch_width = 24
        swatches = palette_root.get_by_role("group", name="Badge color").get_by_role("button")
        expect(swatches).to_have_count(40)
        geometry = swatches.evaluate_all(
            """buttons => {
                const boxes = buttons.map(button => button.getBoundingClientRect());
                const cols = new Set(boxes.map(box => Math.round(box.x)));
                const rows = new Set(boxes.map(box => Math.round(box.y)));
                const minWidth = Math.min(...boxes.map(box => box.width));
                return { cols: cols.size, rows: rows.size, minWidth };
            }"""
        )
        assert geometry["cols"] == 8 and geometry["rows"] == 5
        assert geometry["minWidth"] >= min_swatch_width
        expect(palette_root.get_by_test_id("agent-badge-color-spectrum")).to_be_in_viewport(
            ratio=1
        )
        expect(palette_root.get_by_test_id("agent-badge-color-hue")).to_be_in_viewport(ratio=1)
        expect(palette_root.get_by_label("Outline color hex")).to_be_in_viewport(ratio=1)
        assert_layout(
            palette_root,
            "settings-agents-badge-palette-sheet"
            if width < 768
            else "settings-agents-badge-editor",
            Path(output_path),
        )
    if populated_members:
        expect(main.get_by_role("button", name="Remove user")).to_be_in_viewport(
            ratio=1, timeout=2_000
        )


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
