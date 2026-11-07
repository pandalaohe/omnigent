"""Icon-only header choices stay centred with real SPA styles and layout."""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat._geometry_helpers import box, open_live
from tests.browser_ui.chat.session_contract import ChatSessionContract


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_view_toggle_icons_are_centred(
    page: Page, chat_session_contract: ChatSessionContract, theme: str
) -> None:
    chat = chat_session_contract
    chat.update_session(labels={"omnigent.ui": "terminal"})
    page.add_init_script(f"localStorage.setItem('web-theme', '{theme}')")
    open_live(page, chat)
    page.evaluate("document.fonts.ready")

    toggle = page.get_by_role("button", name="Expand right panel", exact=True)
    if toggle.is_visible():
        toggle.click()
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail).to_be_visible()
    rail.get_by_role("tab", name=re.compile("^Agents")).click()

    for group in (
        rail.get_by_role("radiogroup", name="Agents view"),
        page.get_by_role("radiogroup", name="Switch between chat and terminal"),
    ):
        expect(group).to_be_visible()
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
