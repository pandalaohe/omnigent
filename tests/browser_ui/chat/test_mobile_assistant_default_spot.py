"""A new phone starts with the floating assistant docked in the right gutter, off the composer."""

from __future__ import annotations

import pytest
from playwright.sync_api import FloatRect, Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract


def _intersects(first: FloatRect, second: FloatRect) -> bool:
    return (
        first["x"] < second["x"] + second["width"]
        and second["x"] < first["x"] + first["width"]
        and first["y"] < second["y"] + second["height"]
        and second["y"] < first["y"] + first["height"]
    )


@pytest.mark.parametrize(
    "viewport",
    [
        pytest.param({"width": 390, "height": 844}, id="phone"),
        pytest.param({"width": 320, "height": 844}, id="narrow-phone"),
        pytest.param({"width": 390, "height": 508}, id="phone-keyboard"),
    ],
)
def test_first_use_dock_stays_in_the_gutter_and_restores(
    page: Page,
    chat_session_contract: ChatSessionContract,
    viewport: dict[str, int],
) -> None:
    """The untouched default covers neither the composer, queued controls nor the content."""
    chat = chat_session_contract
    page.add_init_script("localStorage.setItem('omnigent:always-steer', 'false')")
    page.set_viewport_size(viewport)
    page.goto(chat.url)
    chat.wait_for_stream()

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=20_000)
    composer.fill("Hold this turn open for the floating assistant test.")
    with page.expect_response(
        lambda response: response.url.endswith(f"/{chat.session_id}/events")
    ):
        page.get_by_role("button", name="Send", exact=True).click()

    composer.fill("Queue this follow-up.")
    page.get_by_role("button", name="Send", exact=True).click()

    strip = page.get_by_test_id("composer-queued-strip")
    expect(strip).to_be_visible()
    composer.fill("\n".join(f"Line {n}" for n in range(1, 9)))

    remove = strip.get_by_role("button", name="Remove queued message", exact=True)
    handle = page.get_by_role("button", name="Expand floating assistant")
    handle_box = handle.bounding_box()
    card_box = page.locator("[data-composer-card]").bounding_box()
    assert handle_box is not None and card_box is not None
    for target in (page.locator("[data-composer-card]"), strip, remove):
        target_box = target.bounding_box()
        assert target_box is not None
        assert not _intersects(handle_box, target_box), (handle_box, target_box)
    # The handle and its tap area stay in the right gutter beside the content column.
    gutter = card_box["x"] + card_box["width"]
    assert handle_box["x"] >= gutter - 0.5, (handle_box, card_box)
    mid_y = handle_box["y"] + handle_box["height"] / 2
    owner = """([x, y]) => {
      const hit = document.elementFromPoint(x, y);
      return hit?.closest("button")?.getAttribute("aria-label") ?? null;
    }"""
    assert page.evaluate(owner, [viewport["width"] - 2, mid_y]) == "Expand floating assistant"
    assert page.evaluate(owner, [gutter - 1, mid_y]) != "Expand floating assistant"

    # A tap restores the floating button; dragging it back to the edge docks it again.
    handle.click()
    assistant = page.get_by_role("button", name="Open floating assistant")
    assistant_box = assistant.bounding_box()
    assert assistant_box is not None
    assert not _intersects(assistant_box, card_box), (assistant_box, card_box)
    start_x = assistant_box["x"] + assistant_box["width"] / 2
    start_y = assistant_box["y"] + assistant_box["height"] / 2
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(viewport["width"] - 2, start_y, steps=8)
    page.mouse.up()
    expect(page.get_by_role("button", name="Expand floating assistant")).to_be_visible()

    remove.click()
    expect(strip).to_have_count(0)
