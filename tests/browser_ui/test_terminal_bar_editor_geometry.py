"""Keep the terminal key editor's pin control clear of its page label."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat._geometry_helpers import assert_no_overlap, box
from tests.browser_ui.test_sessions_appearance import appearance_features as appearance_features
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url


@pytest.mark.parametrize(("width", "height"), [(1280, 800), (390, 844)])
@pytest.mark.parametrize("pinned", [0, 1])
def test_terminal_editor_pin_handle_clears_page_label(
    page: Page,
    appearance_url: str,
    output_path: str,
    width: int,
    height: int,
    pinned: int,
) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.goto(f"{appearance_url}/settings/shortcuts")
    row = page.get_by_role("region", name="Top row", exact=True)
    count = row.get_by_test_id("terminal-bar-pinned-top")
    expect(count).to_have_text("0")
    if pinned:
        row.get_by_role("button", name="Pin one more top row key").press("Space")
    expect(count).to_have_text(str(pinned))
    handle = row.get_by_role("button", name="Drag to change how many top row keys are pinned")
    label = row.get_by_text(re.compile(r"^page 1 · \d+ cells$"))
    handle.scroll_into_view_if_needed()
    try:
        assert_no_overlap(box(handle), box(label))
        assert_no_overlap(box(handle.locator("svg")), box(label))
    except AssertionError:
        page.screenshot(path=Path(output_path) / f"terminal-pin-{pinned}-{width}.png")
        raise

    handle.focus()
    expect(handle).to_be_focused()
    bounds = box(handle)
    x = bounds["x"] + bounds["width"] / 2
    y = bounds["y"] + bounds["height"] / 2
    grid = handle.locator("xpath=..")
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + box(grid)["width"] / 6, y)
    page.mouse.up()
    expect(count).to_have_text(str(pinned + 1))
