"""Icon-only header choices stay centred with real SPA styles and layout."""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat._geometry_helpers import box, open_live
from tests.browser_ui.chat.session_contract import ChatSessionContract


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 768, 1280])
def test_view_toggle_icons_are_centred(
    page: Page, chat_session_contract: ChatSessionContract, theme: str, width: int
) -> None:
    chat = chat_session_contract
    chat.update_session(labels={"omnigent.ui": "terminal"})
    page.add_init_script(f"localStorage.setItem('web-theme', '{theme}')")
    open_live(page, chat, {"width": width, "height": 900})
    page.evaluate("document.fonts.ready")

    if width < 768:
        page.get_by_test_id("header-conversation-actions").click()
        page.get_by_role("menuitem", name=re.compile("^Agents")).click()
        groups = [page.get_by_role("radiogroup", name="Agents view").filter(visible=True)]
    else:
        close_sidebar = page.get_by_role("button", name="Close sidebar", exact=True)
        if close_sidebar.is_visible():
            close_sidebar.click()
        toggle = page.get_by_role("button", name="Expand right panel", exact=True)
        if toggle.is_visible():
            toggle.click()
        rail = page.get_by_role("complementary", name="Workspace")
        expect(rail).to_be_visible()
        rail.get_by_role("tab", name=re.compile("^Agents")).click()
        groups = [
            rail.get_by_role("radiogroup", name="Agents view"),
            page.get_by_role("radiogroup", name="Switch between chat and terminal"),
        ]

    for group in groups:
        expect(group).to_be_visible()
        header = (
            group.locator("xpath=../..")
            if group.get_attribute("aria-label") == "Agents view"
            else group.locator("xpath=ancestor::header[1]")
        )
        bounds = box(header)
        track = box(group)
        for axis, dimension in (("x", "width"), ("y", "height")):
            assert track[axis] >= bounds[axis] - 0.5
            assert track[axis] + track[dimension] <= bounds[axis] + bounds[dimension] + 0.5
        radios = group.get_by_role("radio")
        for selected_index in (0, 1):
            radios.nth(selected_index).click()
            expect(radios.nth(selected_index)).to_have_attribute("aria-checked", "true")
            for radio in radios.all():
                button = box(radio)
                glyph = box(radio.locator("svg"))
                for axis, dimension in (("x", "width"), ("y", "height")):
                    offset = (
                        glyph[axis] + glyph[dimension] / 2 - button[axis] - button[dimension] / 2
                    )
                    assert abs(offset) <= 0.5, (radio, axis, offset)
