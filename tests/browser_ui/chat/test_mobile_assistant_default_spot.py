"""A new phone starts with the floating assistant docked in the right gutter, off the composer."""

from __future__ import annotations

import pytest
from playwright.sync_api import FloatRect, Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item

_PROSE = "\n\n".join(f"Paragraph {index} before the code." for index in range(24))
_CODE = "\n".join(f"const line{index} = {index};" for index in range(40))
_RECORD_CLIPBOARD = """
window.__copied = [];
Object.defineProperty(navigator, "clipboard", {
  configurable: true,
  value: { writeText: async (text) => { window.__copied.push(text); } },
});
"""
# Each poll scrolls the transcript until Copy Code sits level with the docked handle.
_LEVEL_COPY_WITH_HANDLE = """() => {
  const handle = document.querySelector('[aria-label="Expand floating assistant"]');
  const copy = document.querySelector('[aria-label="Copy Code"]');
  const scroller = document.querySelector('[role="log"]').firstElementChild;
  const middle = (element) => {
    const rect = element.getBoundingClientRect();
    return rect.top + rect.height / 2;
  };
  const offset = middle(copy) - middle(handle);
  if (Math.abs(offset) < 2) return true;
  scroller.scrollTop += offset;
  return false;
}"""
# Taps across the button's width: none may reach the handle, and the centre is its own.
_TAP_REPORT = """(button, handle) => {
  const rect = button.getBoundingClientRect();
  const y = rect.top + rect.height / 2;
  const handleHits = [];
  for (let x = rect.left + 1; x < rect.right; x += 2) {
    const hit = document.elementFromPoint(x, y);
    if (hit && handle.contains(hit)) handleHits.push(x);
  }
  const centre = document.elementFromPoint(rect.left + rect.width / 2, y);
  return { handleHits, ownsCentre: button.contains(centre) };
}"""


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


@pytest.mark.parametrize(
    "viewport",
    [
        pytest.param({"width": 360, "height": 780}, id="android-360"),
        pytest.param({"width": 402, "height": 874}, id="iphone-402"),
    ],
)
@pytest.mark.browser_context_args(has_touch=True, is_mobile=True)
def test_first_use_dock_leaves_code_block_controls_clickable(
    page: Page,
    chat_session_contract: ChatSessionContract,
    viewport: dict[str, int],
) -> None:
    """Level with the docked handle, code block controls stay uncovered and Copy Code copies."""
    chat = chat_session_contract
    chat.set_items(
        [
            message_item(
                "dock-code",
                "assistant",
                f"{_PROSE}\n\n```ts\n{_CODE}\n```\n\nDone.",
                response_id="dock-code-response",
            )
        ]
    )
    page.add_init_script(_RECORD_CLIPBOARD)
    page.set_viewport_size(viewport)
    page.goto(chat.url)

    block = page.locator('[data-streamdown="code-block"]').first.locator("..")
    copy = block.get_by_role("button", name="Copy Code", exact=True)
    handle = page.get_by_role("button", name="Expand floating assistant")
    expect(copy).to_be_visible(timeout=20_000)
    expect(handle).to_be_visible()
    page.wait_for_function(_LEVEL_COPY_WITH_HANDLE)

    handle_box = handle.bounding_box()
    assert handle_box is not None
    for name in ("Toggle word wrap", "Copy Code", "Download file"):
        control = block.get_by_role("button", name=name, exact=True)
        control_box = control.bounding_box()
        assert control_box is not None
        assert not _intersects(handle_box, control_box), (name, handle_box, control_box)
        report = control.evaluate(_TAP_REPORT, handle.element_handle())
        assert report == {"handleHits": [], "ownsCentre": True}, (name, report)

    copy_box = copy.bounding_box()
    assert copy_box is not None
    page.touchscreen.tap(
        copy_box["x"] + copy_box["width"] / 2, copy_box["y"] + copy_box["height"] / 2
    )
    page.wait_for_function(
        "expected => window.__copied.some((text) => text.trim() === expected)", arg=_CODE
    )
    expect(handle).to_be_visible()
