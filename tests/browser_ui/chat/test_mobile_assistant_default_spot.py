"""The floating assistant's first-use spot clears the composer band, keyboard viewport included."""

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
def test_first_use_spot_clears_the_composer_band(
    page: Page,
    chat_session_contract: ChatSessionContract,
    viewport: dict[str, int],
) -> None:
    """The untouched default must not cover the composer card or queued controls."""
    chat = chat_session_contract
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
    assistant = page.get_by_role("button", name="Open floating assistant")
    assistant_box = assistant.bounding_box()
    assert assistant_box is not None
    for target in (page.locator("[data-composer-card]"), strip, remove):
        target_box = target.bounding_box()
        assert target_box is not None
        assert not _intersects(assistant_box, target_box), (assistant_box, target_box)

    remove.click()
    expect(strip).to_have_count(0)
