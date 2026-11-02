"""Routed page content must clear the shell's mobile navigation control."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from omnigent.server.child_idle import DEFAULT_CHECKIN_INSTRUCTIONS
from tests.browser_ui.chat.conftest import chat_session_contract as chat_session_contract
from tests.browser_ui.chat.session_contract import ChatSessionContract
from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url


@pytest.fixture
def pages_url(appearance_url: str, browser_contract: BrowserContract) -> str:
    browser_contract.json(
        "/v1/info",
        {
            "accounts_enabled": False,
            "single_user": True,
            "features": {"usage_page": True, "canvas": True, "automations_v2": True},
        },
    )
    browser_contract.json("/v1/usage", {"daily_costs": [], "sessions": [], "total_cost_usd": 0})
    browser_contract.json("/v1/scheduled-tasks", {"scheduled_tasks": []})
    browser_contract.json(
        "/v1/system/status",
        {
            "revision": 0,
            "level": "ok",
            "findings": [],
            "server": None,
            "hosts": [],
            "monitor_overhead": {
                "server": {"cpu_pct": 0, "mem_estimate_bytes": 0, "estimated": True},
                "hosts": {},
            },
        },
    )
    browser_contract.json(
        "/v1/system/settings",
        {
            "cpu_pct": 90,
            "cpu_sustain_min": 5,
            "mem_pct": 90,
            "disk_pct": 90,
            "server_5xx_pct": 5,
            "health_check": {"project_id": None, "host_id": None, "prompt": None},
            "default_health_check_prompt": "Example health check",
        },
    )
    browser_contract.json(
        "/v1/child-checkin/defaults", {"instructions": DEFAULT_CHECKIN_INSTRUCTIONS}
    )
    browser_contract.json("/v1/policies", {"data": []})
    browser_contract.json("/v1/policy-registry", {"data": []})
    browser_contract.json(
        "/v1/instruction-layers",
        {
            "data": [],
            "agents": [],
            "order": ["shared", "agent", "host"],
            "route_sizes": [],
            "size_reminder_chars": 10000,
        },
    )
    browser_contract.json(
        "/v1/sharing",
        {
            "sharing_mode": "off",
            "editable": False,
            "options": ["off"],
            "public_sharing_enabled": False,
            "public_sharing_editable": False,
            "default_public_sessions": "off",
            "default_public_sessions_editable": False,
            "default_public_sessions_options": ["off"],
        },
    )
    return appearance_url


@pytest.mark.parametrize("native", [False, True])
def test_usage_clears_mobile_sidebar_control(
    page: Page,
    pages_url: str,
    native: bool,
    request: pytest.FixtureRequest,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{pages_url}/usage")
    title = page.get_by_role("heading", name="Usage", exact=True)
    menu = page.get_by_role("button", name="Open sidebar", exact=True)
    expect(title).to_be_visible()
    expect(menu).to_be_visible()
    expect(page.get_by_text("Total cost", exact=True)).to_be_visible()
    if native:
        page.locator(".app-shell").evaluate(
            """shell => {
                shell.dataset.iosNative = 'true';
                document.documentElement.style.setProperty('--omnigent-safe-top', '47px');
            }"""
        )
    # Bring the first row to the top of its own scroll viewport, as a phone
    # swipe does. Header clearance must survive scrolling the initial padding.
    scroll_top = title.evaluate(
        """title => {
            let scroll = title.parentElement;
            while (scroll && getComputedStyle(scroll).overflowY !== 'auto') {
                scroll = scroll.parentElement;
            }
            if (!scroll) throw new Error('Page scroll viewport not found');
            const titleTop = title.getBoundingClientRect().top;
            scroll.scrollTop = titleTop - scroll.getBoundingClientRect().top;
            return scroll.scrollTop;
        }"""
    )
    assert scroll_top > 0, "Loaded Usage content must actually scroll before checking overlap"
    title_box = title.bounding_box()
    menu_box = menu.bounding_box()
    assert title_box is not None and menu_box is not None
    output = Path(request.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    geometry = {"title": title_box, "menu": menu_box, "scroll_top": scroll_top}
    suffix = "native" if native else "browser"
    (output / f"usage-geometry-{suffix}.json").write_text(json.dumps(geometry, indent=2))
    page.screenshot(path=str(output / f"usage-mobile-{suffix}.png"))
    assert title_box["y"] >= menu_box["y"] + menu_box["height"], geometry
    menu.click()
    expect(page.get_by_test_id("usage-nav")).to_be_visible()


@pytest.mark.parametrize(
    ("route", "heading"),
    [
        ("/tasks", "Automations"),
        ("/inbox", "Inbox"),
        ("/system", "System status"),
        ("/settings/shortcuts", "Keyboard shortcuts"),
        ("/settings/general", "General"),
        ("/settings/appearance", "Appearance"),
        ("/settings/git", "Git"),
        ("/settings/harnesses", "Harnesses"),
        ("/settings/custom-agents", "Custom agents"),
        ("/settings/import", "Import sessions"),
        ("/settings/instructions", "Instructions"),
        ("/settings/policies", "Global Policies"),
        ("/settings/system-status", "System status"),
    ],
)
def test_sidebar_page_scroll_viewport_clears_mobile_navigation(
    page: Page,
    pages_url: str,
    route: str,
    heading: str,
    request: pytest.FixtureRequest,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{pages_url}{route}")
    title = page.get_by_role("heading", name=heading, level=1, exact=True)
    expect(title).to_be_visible()
    if page.locator(".app-shell").get_attribute("data-sidebar-open") == "true":
        page.get_by_role("button", name="Close sidebar", exact=True).click()
    menu = page.locator(".chat-header-sidebar-toggle")
    expect(menu).to_be_visible()
    geometry = page.locator("main[data-shell-header] > div").first.evaluate(
        """scroll => {
            const menu = document.querySelector('.chat-header-sidebar-toggle');
            return {scroll: scroll.getBoundingClientRect().toJSON(),
                    menu: menu.getBoundingClientRect().toJSON()};
        }"""
    )
    output = Path(request.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    stem = route.strip("/").replace("/", "-")
    (output / f"{stem}-geometry.json").write_text(json.dumps(geometry, indent=2))
    page.screenshot(path=str(output / f"{stem}-mobile.png"))
    assert geometry["scroll"]["top"] >= geometry["menu"]["bottom"], geometry


@pytest.fixture
def extension_url(
    pages_url: str,
    browser_contract: BrowserContract,
    kind: str,
) -> str:
    board_url = (
        "/v1/extensions/example.board/proxy/board/"
        if kind == "proxied"
        else "https://board.example.test/"
    )
    browser_contract.json(
        "/v1/extensions",
        {
            "object": "list",
            "data": [
                {
                    "object": "extension",
                    "id": "example.board",
                    "display_name": "Example board",
                    "distribution": "example-board",
                    "version": "1",
                    "extension_api": 1,
                    "status": "enabled",
                    "permissions": ["pages.external", "pages.proxy"],
                    "pages": [
                        {
                            "id": "board",
                            "title": "Usage board",
                            "route": "board",
                            "kind": kind,
                            "view": None,
                            "url": board_url,
                        }
                    ],
                    "primary_navigation": [
                        {
                            "id": "nav",
                            "label": "Usage board",
                            "page": "board",
                            "icon": None,
                            "order": 0,
                            "when": None,
                        }
                    ],
                    "browser": {
                        "declared": False,
                        "has_styles": False,
                        "digest": None,
                        "script_url": None,
                        "style_url": None,
                    },
                }
            ],
        },
    )
    browser_contract.route(
        f"{pages_url}{board_url}" if kind == "proxied" else board_url,
        lambda route: route.fulfill(content_type="text/html", body="<h1>Example usage data</h1>"),
    )
    return f"{pages_url}/extensions/example.board/board"


@pytest.mark.parametrize("kind", ["external", "proxied"])
@pytest.mark.parametrize("native", [False, True])
def test_extension_page_clears_mobile_navigation(
    page: Page,
    extension_url: str,
    kind: str,
    native: bool,
    request: pytest.FixtureRequest,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(extension_url)
    title = page.get_by_role("heading", name="Usage board", exact=True)
    menu = page.get_by_role("button", name="Open sidebar", exact=True)
    expect(title).to_be_visible()
    expect(menu).to_be_visible()
    if native:
        page.locator(".app-shell").evaluate(
            """shell => {
                shell.dataset.iosNative = 'true';
                document.documentElement.style.setProperty('--omnigent-safe-top', '47px');
            }"""
        )
    title_box, menu_box = title.bounding_box(), menu.bounding_box()
    assert title_box is not None and menu_box is not None
    output = Path(request.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    geometry = {"title": title_box, "menu": menu_box}
    suffix = f"{kind}-{'native' if native else 'browser'}"
    (output / f"extension-geometry-{suffix}.json").write_text(json.dumps(geometry, indent=2))
    page.screenshot(path=str(output / f"extension-mobile-{suffix}.png"))
    assert title_box["y"] >= menu_box["y"] + menu_box["height"], geometry
    menu.click()
    expect(page.locator("main[data-shell-header]")).to_have_attribute(
        "data-shell-header", "hidden"
    )
    expect(page.locator('iframe[title="Usage board"]').locator("..")).to_have_css(
        "padding-top", "0px"
    )


def test_system_status_keeps_desktop_scroll_geometry(
    page: Page, pages_url: str, request: pytest.FixtureRequest
) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{pages_url}/system")
    expect(page.get_by_text("No hosts connected.", exact=True)).to_be_visible()
    main = page.locator("main[data-shell-header]")
    scroll = main.locator(":scope > div").first
    main_box, scroll_box = main.bounding_box(), scroll.bounding_box()
    assert main_box is not None and scroll_box is not None
    geometry = {
        "main": main_box,
        "scroll": scroll_box,
        "padding_top": scroll.evaluate("element => getComputedStyle(element).paddingTop"),
    }
    output = Path(request.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    (output / "system-desktop-geometry.json").write_text(json.dumps(geometry, indent=2))
    page.screenshot(path=str(output / "system-desktop.png"))
    assert scroll_box["y"] == main_box["y"], geometry
    assert geometry["padding_top"] == "80px", geometry


@pytest.mark.parametrize("kind", ["external", "proxied"])
def test_extension_page_keeps_desktop_header_padding(
    page: Page, extension_url: str, kind: str, request: pytest.FixtureRequest
) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(extension_url)
    title = page.get_by_role("heading", name="Usage board", exact=True)
    expect(title).to_be_visible()
    main = page.locator("main[data-shell-header]")
    expect(main).to_have_attribute("data-shell-header", "hidden")
    frame = page.locator('iframe[title="Usage board"]')
    host = frame.locator("..")
    geometry = {
        "title": title.bounding_box(),
        "frame": frame.bounding_box(),
        "padding_top": host.evaluate("element => getComputedStyle(element).paddingTop"),
    }
    output = Path(request.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{kind}-desktop-geometry.json").write_text(json.dumps(geometry, indent=2))
    page.screenshot(path=str(output / f"{kind}-desktop.png"))
    assert geometry["padding_top"] == "48px", geometry
    page.get_by_role("button", name="Close sidebar", exact=True).click()
    expect(main).to_have_attribute("data-shell-header", "visible")
    expect(host).to_have_css("padding-top", "48px")


def _settle_closed_sidebar(page: Page, width: int, height: int) -> None:
    page.set_viewport_size({"width": width, "height": height})
    if page.locator(".app-shell").get_attribute("data-sidebar-open") == "true":
        page.get_by_role("button", name="Close sidebar", exact=True).click()
    expect(page.get_by_role("button", name="Open sidebar", exact=True)).to_be_visible()
    expect(page.locator("main[data-shell-header]")).to_have_css("width", f"{width}px")
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )


def _page_header_geometry(page: Page, heading: str, *, scroll: bool = False) -> dict:
    title = page.get_by_role("heading", name=heading, level=1, exact=True)
    viewport = title.evaluate(
        """(title, shouldScroll) => {
            let viewport = title.parentElement;
            while (viewport && getComputedStyle(viewport).overflowY !== 'auto') {
                viewport = viewport.parentElement;
            }
            if (!viewport) throw new Error('Page scroll viewport not found');
            viewport.scrollTop = 0;
            if (shouldScroll) {
                viewport.scrollTop = title.getBoundingClientRect().top
                    - viewport.getBoundingClientRect().top;
            }
            return {rect: viewport.getBoundingClientRect().toJSON(),
                    scroll_top: viewport.scrollTop,
                    padding_top: getComputedStyle(viewport).paddingTop};
        }""",
        scroll,
    )
    menu = page.get_by_role("button", name="Open sidebar", exact=True)
    menu_box = menu.bounding_box() if menu.count() else None
    title_box = title.bounding_box()
    assert title_box is not None
    horizontal = (
        menu_box is not None
        and title_box["x"] < menu_box["x"] + menu_box["width"]
        and title_box["x"] + title_box["width"] > menu_box["x"]
    )
    overlap = (
        horizontal
        and menu_box is not None
        and (
            title_box["y"] < menu_box["y"] + menu_box["height"]
            and title_box["y"] + title_box["height"] > menu_box["y"]
        )
    )
    return {
        "title": title_box,
        "menu": menu_box,
        "viewport": viewport,
        "horizontal": horizontal,
        "overlap": overlap,
    }


@pytest.mark.parametrize(("width", "height"), [(768, 390), (844, 390), (1024, 768), (1440, 900)])
def test_scrolled_usage_clears_closed_navigation_at_landscape_widths(
    page: Page, pages_url: str, width: int, height: int, request: pytest.FixtureRequest
) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": height})
    page.goto(f"{pages_url}/usage")
    expect(page.get_by_text("Total cost", exact=True)).to_be_visible()
    _settle_closed_sidebar(page, width, height)
    geometry = _page_header_geometry(page, "Usage", scroll=True)
    assert geometry["viewport"]["scroll_top"] > 0, geometry
    output = Path(request.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"usage-closed-{width}x{height}.png"))
    (output / f"usage-closed-{width}x{height}.json").write_text(json.dumps(geometry, indent=2))
    assert not geometry["overlap"], geometry
    expected_top, expected_padding = (56, "24px") if width < 1040 else (0, "80px")
    violations = []
    if (
        geometry["viewport"]["rect"]["top"] != expected_top
        or geometry["viewport"]["padding_top"] != expected_padding
    ):
        violations.append(geometry["viewport"])
    page.get_by_role("button", name="Open sidebar", exact=True).click()
    viewport = page.locator("main[data-shell-header] > div").first
    geometry["sidebar_open"] = viewport.evaluate(
        """element => ({rect: element.getBoundingClientRect().toJSON(),
            padding_top: getComputedStyle(element).paddingTop,
            horizontal_overflow: element.scrollWidth > element.clientWidth})"""
    )
    if (
        geometry["sidebar_open"]["rect"]["top"] != expected_top
        or geometry["sidebar_open"]["padding_top"] != expected_padding
    ):
        violations.append(geometry["sidebar_open"])
    expect(page.get_by_role("heading", name="Usage", level=1, exact=True)).to_be_visible()
    assert not geometry["sidebar_open"]["horizontal_overflow"], geometry
    page.screenshot(path=str(output / f"usage-open-{width}x{height}.png"))
    _settle_closed_sidebar(page, width, height)
    geometry["after_sidebar_cycle"] = _page_header_geometry(page, "Usage", scroll=True)
    (output / f"usage-closed-{width}x{height}.json").write_text(json.dumps(geometry, indent=2))
    assert geometry["after_sidebar_cycle"]["viewport"]["scroll_top"] > 0, geometry
    assert not geometry["after_sidebar_cycle"]["overlap"], geometry
    assert not violations, violations
    assert not errors, errors


_PAGE_SCROLL_CALLERS = [
    ("/usage", "Usage", "UsagePage.tsx"),
    ("/tasks", "Automations", "TasksPage.tsx"),
    ("/tasks/layout-example", "Example automation", "TaskDetailPage.tsx"),
    ("/inbox", "Inbox", "InboxPage.tsx"),
    ("/system", "System status", "SystemStatusPage.tsx"),
    ("/settings/general", "General", "SettingsPage.tsx"),
    ("/settings/system-status", "System status", "SettingsPage.tsx"),
    ("/settings/instructions", "Instructions", "InstructionsPage.tsx"),
    ("/settings/policies", "Global Policies", "PoliciesPage.tsx"),
    ("/settings/members", "Members", "MembersPage.tsx"),
    ("/settings/sharing", "Session sharing", "SharingPage.tsx"),
]


@pytest.mark.parametrize(("route", "heading", "caller"), _PAGE_SCROLL_CALLERS)
def test_page_clearance_uses_shared_threshold(
    page: Page,
    pages_url: str,
    browser_contract: BrowserContract,
    route: str,
    heading: str,
    caller: str,
    request: pytest.FixtureRequest,
) -> None:
    # Keep the browser sweep complete when a new routed caller is introduced.
    source = Path(__file__).resolve().parents[2] / "web/src"
    callers = {
        path.name
        for path in source.rglob("*.tsx")
        if "<PageScroll" in path.read_text() and path.name != "PageScroll.tsx"
    }
    swept = {values[2] for values in _PAGE_SCROLL_CALLERS}
    assert callers == swept, "Add every new PageScroll caller to the layout sweep"
    if route in ("/settings/members", "/settings/sharing"):
        browser_contract.json(
            "/v1/info",
            {"accounts_enabled": False, "single_user": False, "features": {}},
        )
        browser_contract.json("/auth/users", {"users": []})
    elif route == "/tasks/layout-example":
        browser_contract.json(
            "/v1/scheduled-tasks/layout-example",
            {
                "id": "layout-example",
                "name": "Example automation",
                "prompt": "Example task",
                "rrule": "FREQ=DAILY",
                "timezone": "UTC",
                "created_at": 1,
                "updated_at": 1,
                "agent_id": "example-agent",
                "execution_target": "connected_host",
                "state": "active",
                "host_id": None,
                "workspace": None,
                "owner_user_id": None,
                "model_override": None,
                "reasoning_effort": None,
                "permission_mode": None,
                "last_run_at": None,
                "last_run_status": None,
                "last_run_conversation_id": None,
                "next_run_at": None,
            },
        )
        browser_contract.json("/v1/scheduled-tasks/layout-example/runs", {"runs": []})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": 768, "height": 390})
    page.goto(f"{pages_url}{route}")
    expect(page.get_by_role("heading", name=heading, level=1, exact=True)).to_be_visible()
    if route == "/usage":
        expect(page.get_by_text("Total cost", exact=True)).to_be_visible()
    elif route == "/system":
        expect(page.get_by_text("No hosts connected.", exact=True)).to_be_visible()
    pinned_settings_sidebar = route.startswith("/settings/")
    first_disjoint = 768
    if not pinned_settings_sidebar:
        lower, upper = 768, 1440
        while lower < upper:
            width = (lower + upper) // 2
            _settle_closed_sidebar(page, width, 390)
            if _page_header_geometry(page, heading)["horizontal"]:
                lower = width + 1
            else:
                upper = width
        first_disjoint = lower
    measurements = []
    violations = []
    for width in sorted(
        {768, 844, 1024, 1039, 1040, 1041, 1440, max(768, first_disjoint - 1), first_disjoint}
    ):
        if pinned_settings_sidebar:
            page.set_viewport_size({"width": width, "height": 390})
            expect(page.locator(".app-shell")).to_have_attribute("data-sidebar-open", "true")
        else:
            _settle_closed_sidebar(page, width, 390)
        geometry = _page_header_geometry(page, heading)
        geometry["width"] = width
        measurements.append(geometry)
        if width < 1040:
            if (
                geometry["viewport"]["rect"]["top"] != 56
                or geometry["viewport"]["padding_top"] != "24px"
            ):
                violations.append(geometry)
        elif (
            geometry["viewport"]["rect"]["top"] != 0
            or geometry["viewport"]["padding_top"] != "80px"
            or geometry["horizontal"]
        ):
            violations.append(geometry)
    output = Path(request.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{route.strip('/').replace('/', '-')}-horizontal-boundary.json").write_text(
        json.dumps(
            {
                "first_disjoint": None if pinned_settings_sidebar else first_disjoint,
                "sidebar_pinned_open": pinned_settings_sidebar,
                "caller": caller,
                "measurements": measurements,
            },
            indent=2,
        )
    )
    assert not violations, violations
    assert not errors, errors


@pytest.mark.parametrize("surface", ["chat", "canvas", "host-terminals", "archived"])
def test_sidebar_surfaces_with_their_own_headers(
    page: Page,
    chat_session_contract: ChatSessionContract,
    surface: str,
    request: pytest.FixtureRequest,
) -> None:
    chat = chat_session_contract
    contract = chat.contract
    page.set_viewport_size({"width": 390, "height": 844})
    if surface == "canvas":
        contract.json(
            "/v1/info",
            {"accounts_enabled": False, "single_user": True, "features": {"canvas": True}},
        )
        page.goto(f"{chat.contract.base_url}/canvas")
        first_row = page.get_by_role("tab", name="Main", exact=True)
    elif surface == "host-terminals":
        contract.json(
            "/v1/hosts",
            {
                "hosts": [
                    {
                        "host_id": chat.host_id,
                        "name": "Example host",
                        "owner": "local",
                        "status": "online",
                        "capabilities": ["host_terminals_v1"],
                    }
                ]
            },
        )
        contract.json(f"/v1/hosts/{chat.host_id}/terminals", {"terminals": []})
        page.goto(f"{chat.contract.base_url}/hosts/{chat.host_id}/terminals")
        first_row = page.get_by_role("heading", name="Example host · Terminals")
    elif surface == "archived":
        contract.json("/v1/sessions/archived-facets", {"hosts": [], "agents": [], "projects": []})
        page.goto(f"{chat.contract.base_url}/settings/archived")
        first_row = page.get_by_role("heading", name="Archived sessions", exact=True)
    else:
        page.goto(chat.url)
        first_row = page.get_by_role("navigation", name="Conversation", exact=True)
    expect(first_row).to_be_visible()
    menu = (
        page.get_by_role("link", name="Back to sessions")
        if surface == "host-terminals"
        else page.get_by_role(
            "button",
            name="Back to settings menu" if surface == "archived" else "Open sidebar",
            exact=True,
        )
    )
    expect(menu).to_be_visible()
    row_box, menu_box = first_row.bounding_box(), menu.bounding_box()
    assert row_box is not None and menu_box is not None
    assert (
        row_box["x"] >= menu_box["x"] + menu_box["width"]
        or row_box["y"] >= menu_box["y"] + menu_box["height"]
    ), (row_box, menu_box)
    output = Path(request.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"{surface}-mobile.png"))
