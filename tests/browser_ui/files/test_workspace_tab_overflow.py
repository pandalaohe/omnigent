"""Workspace tab navigation in Chromium with a sealed mock backend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _mock_markdown_files,
)
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture


def _open_file_tabs(
    page: Page, seeded_session: BrowserSession, files: dict[str, str], width: int
) -> Locator:
    """Open every file as a tab in a rail *width* pixels wide; return the tab toolbar."""
    paths = list(files)
    _mock_markdown_files(page, seeded_session, files)
    environment_url = (
        f"{seeded_session.contract.base_url}/v1/sessions/{seeded_session.session_id}"
        "/resources/environments/default"
    )
    seeded_session.contract.route(
        f"{environment_url}/filesystem/missing.md",
        lambda route: route.fulfill(status=404, json={"error": "File not found"}),
    )
    seeded_session.contract.route(
        f"{environment_url}/filesystem/workspace/{paths[0]}?base=host",
        lambda route: route.fulfill(
            json={
                "object": "session.environment.filesystem.file_content",
                "path": f"/workspace/{paths[0]}",
                "content": "# Panel 0",
                "encoding": "utf-8",
                "content_type": "text/markdown",
                "bytes": 9,
            }
        ),
    )
    state = [
        {
            "id": seeded_session.session_id,
            "state": {
                "open": True,
                "widthPx": width,
                "rightRailTab": "files",
                "openFiles": paths,
                "selectedFilePath": paths[0],
            },
        }
    ]
    encoded_state = json.dumps(json.dumps(state))
    page.add_init_script(
        f"localStorage.setItem('omnigent:session-workspace-state', {encoded_state});"
        "localStorage.setItem('omnigent:workspace-panel-widen-for-content', '0');"
        "localStorage.setItem('omnigent:panel-size-preferences', "
        f"JSON.stringify({{inlinePanelWidthPx: {width}}}));"
    )
    page.set_viewport_size({"width": 1800, "height": 1000})
    page.goto(f"{seeded_session.contract.base_url}/c/{seeded_session.session_id}")
    toolbar = page.get_by_role("toolbar", name="Workspace tabs")
    expect(toolbar.locator("[data-workspace-tabs-viewport]")).to_be_visible(timeout=30_000)
    return toolbar


def test_workspace_tabs_shrink_before_showing_icons(
    page: Page, seeded_session: BrowserSession
) -> None:
    # Too wide together at full width, but they fit at the 64px floor.
    files = {f"src/shrinking-workspace-tab-{index}.md": f"# Tab {index}" for index in range(6)}
    toolbar = _open_file_tabs(page, seeded_session, files, 900)
    viewport = toolbar.locator("[data-workspace-tabs-viewport]")
    tabs = viewport.locator('[role="button"][aria-current]')
    expect(tabs).to_have_count(len(files))

    # The tabs keep their labels, truncated to fit, and the strip doesn't scroll.
    expect(viewport.locator("[data-compact]")).to_have_count(0)
    assert viewport.evaluate("el => el.scrollWidth <= el.clientWidth")
    assert tabs.evaluate_all("""tabs => tabs.every((tab) => {
        const label = tab.querySelector('span.truncate');
        const width = tab.getBoundingClientRect().width;
        return label && label.scrollWidth > label.clientWidth && width >= 63.5;
    })""")


@pytest.mark.parametrize("width", [240, 360, 472, 900])
def test_workspace_tab_overflow(
    page: Page, seeded_session: BrowserSession, width: int, output_path: str
) -> None:
    Path(output_path).mkdir(parents=True, exist_ok=True)
    files = {f"src/long-panel-name-{index}.md": f"# Panel {index}" for index in range(12)}
    paths = list(files)
    toolbar = _open_file_tabs(page, seeded_session, files, width)
    viewport = toolbar.locator("[data-workspace-tabs-viewport]")
    expect(viewport).to_be_visible(timeout=30_000)
    # Tabs fit the strip; the picker holds tabs beyond the minimum usable width.
    assert viewport.evaluate("el => el.scrollWidth <= el.clientWidth")
    visible_tabs = viewport.locator("[data-workspace-tab]:visible")
    assert 0 < visible_tabs.count() < len(paths)
    assert visible_tabs.evaluate_all("""tabs => tabs.every(tab => {
        const close = tab.querySelector('button[aria-label^="Close "]');
        const box = tab.getBoundingClientRect();
        const x = close.getBoundingClientRect();
        return box.width >= 64 && x.left >= box.left && x.right <= box.right;
    })""")
    assert (
        abs(page.get_by_role("complementary", name="Workspace").bounding_box()["width"] - width)
        < 1
    )
    viewport.hover()
    page.mouse.wheel(0, 150)
    assert viewport.evaluate("el => el.scrollLeft") == 0

    toolbar.get_by_role("button", name="Select panel").click()
    expect(page.get_by_role("menuitem", name=paths[0], exact=True)).to_have_attribute(
        "aria-current", "true"
    )
    page.screenshot(path=Path(output_path) / f"tab-picker-{width}.png")
    page.get_by_role("menuitem", name=paths[-1], exact=True).click()
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text(
        f"Panel {len(paths) - 1}"
    )
    # Radix refocuses the trigger from a timer queued after the menu unmounts; a
    # mouse pick must skip that or the tooltip re-opens. Give that timer a short,
    # bounded window before asserting that focus and the tooltip stayed away.
    expect(page.get_by_role("menu")).to_have_count(0)
    page.mouse.move(500, 500)
    page.wait_for_timeout(100)
    expect(toolbar.get_by_role("button", name="Select panel")).not_to_be_focused()
    expect(page.get_by_role("tooltip")).to_have_count(0)
    active_tab = viewport.locator('[role="button"][aria-current="true"]')
    assert active_tab.evaluate(
        """el => {
            const a = el.getBoundingClientRect();
            const v = el.closest('[data-workspace-tabs-viewport]').getBoundingClientRect();
            const visibleWidth = Math.min(a.right, v.right) - Math.max(a.left, v.left);
            return visibleWidth >= Math.min(a.width, v.width) - 1;
        }"""
    )
    page.screenshot(path=Path(output_path) / f"tabs-{width}.png")

    # Resize through fullscreen and back with the last tab selected.
    toolbar.get_by_role("button", name="Full screen", exact=True).click()
    toolbar.get_by_role("button", name="Exit full screen", exact=True).click()
    expect(active_tab).to_be_in_viewport()

    # The picker remains keyboard accessible and offers fixed navigation panels.
    toolbar.get_by_role("button", name="Select panel").focus()
    page.keyboard.press("Enter")
    page.get_by_role("menuitem", name="Changes", exact=True).focus()
    page.keyboard.press("Enter")
    expect(toolbar.locator('[data-workspace-tab="changes"]')).to_have_attribute(
        "data-state", "active"
    )
    for path in paths[2:]:
        # The picker can reveal a tab without requiring it to be initially visible.
        toolbar.get_by_role("button", name="Select panel").click()
        page.get_by_role("menuitem", name=path, exact=True).click()
        viewport.get_by_role("button", name=f"Close {path.split('/')[-1]}", exact=True).click()
    # With only two files left, both close controls fit without scrolling.
    expect(viewport.locator("[data-workspace-tab]:visible")).to_have_count(2)
    assert viewport.evaluate("el => el.scrollWidth <= el.clientWidth")
    page.screenshot(path=Path(output_path) / f"two-tabs-{width}.png")
    viewport.locator(f'[data-workspace-tab][title="{paths[0]}"]').click()
    page.get_by_role("button", name=f"Edit file path: {paths[0]}").click()
    address = page.get_by_role("textbox", name="File path", exact=True)
    expect(address).to_have_value(f"/workspace/{paths[0]}")
    assert address.evaluate("el => el.selectionStart === 0 && el.selectionEnd === el.value.length")
    address.fill(paths[1])
    address.press("Enter")
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text("Panel 1")
    page.get_by_role("button", name=f"Edit file path: {paths[1]}").click()
    address.fill("cancel.md")
    address.press("Escape")
    expect(page.get_by_role("button", name=f"Edit file path: {paths[1]}")).to_be_visible()
    page.get_by_role("button", name=f"Edit file path: {paths[1]}").click()
    address.fill("missing.md")
    address.press("Enter")
    expect(page.get_by_role("alert")).to_contain_text("404")
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text("Panel 1")
    page.screenshot(path=Path(output_path) / f"path-error-{width}.png")
    address.fill(f"/workspace/{paths[0]}")
    address.press("Enter")
    expect(
        page.get_by_role("button", name=f"Edit file path: /workspace/{paths[0]}")
    ).to_be_visible()
    first_close = f"Close {paths[0].split('/')[-1]}"
    expect(
        viewport.locator(f'[data-workspace-tab][title="{paths[0]}"]').get_by_role(
            "button", name=first_close, exact=True
        )
    ).to_be_in_viewport()
    if width == 472:
        # A lone tab fits the full slot but not the slot minus two arrows.
        assert viewport.evaluate(
            "el => el.firstElementChild.clientWidth <= el.parentElement.clientWidth"
        )
        expect(toolbar.get_by_role("button", name="Scroll tabs right")).to_have_count(0)
    toolbar.get_by_role("button", name="Full screen", exact=True).click()
    expect(toolbar.get_by_role("button", name="Scroll tabs right")).to_have_count(0)
    expect(toolbar.get_by_role("button", name="Scroll tabs left")).to_have_count(0)


@pytest.mark.parametrize("widen", [False, True], ids=["normal-width", "file-width"])
def test_workspace_file_width_respects_content_preference(
    page: Page, seeded_session: BrowserSession, widen: bool
) -> None:
    path = "src/panel.md"
    _mock_markdown_files(page, seeded_session, {path: "# File width"})
    state = [
        {
            "id": seeded_session.session_id,
            "state": {
                "open": True,
                "rightRailTab": "files",
                "openFiles": [path],
                "selectedFilePath": path,
            },
        }
    ]
    page.add_init_script(
        "localStorage.setItem('omnigent:session-workspace-state', "
        f"{json.dumps(json.dumps(state))});"
        "localStorage.setItem('omnigent:workspace-panel-widen-for-content', "
        f"{json.dumps('1' if widen else '0')});"
        "localStorage.setItem('omnigent:panel-size-preferences', "
        "JSON.stringify({inlinePanelWidthPx: 360, inlinePanelWideWidthPx: 760}));"
    )
    page.set_viewport_size({"width": 1800, "height": 1000})
    page.goto(f"{seeded_session.contract.base_url}/c/{seeded_session.session_id}")
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text("File width")
    panel = page.get_by_role("complementary", name="Workspace")
    expected = 760 if widen else 360
    expect(panel).to_have_css("width", f"{expected}px")
    page.get_by_role("separator", name="Resize panel", exact=True).press("ArrowLeft")
    expect(panel).to_have_css("width", f"{expected + 20}px")
    preferences = page.evaluate(
        "JSON.parse(localStorage.getItem('omnigent:panel-size-preferences'))"
    )
    assert preferences["inlinePanelWidthPx"] == (360 if widen else 380)
    assert preferences["inlinePanelWideWidthPx"] == (780 if widen else 760)
