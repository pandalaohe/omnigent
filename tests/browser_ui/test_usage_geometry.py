"""The geometry assertion must reject disappearing, clipped and unreadable figures."""

import pytest
from playwright.sync_api import Page

from tests.browser_ui.usage_geometry import assert_usage_geometry


@pytest.mark.parametrize(
    ("counterexample", "message"),
    [
        ("empty", "expected 1"),
        ("hidden", "not visible"),
        ("clipped", "ancestor clips"),
        ("small", "text below 10px"),
    ],
)
def test_usage_geometry_rejects_counterexamples(page: Page, counterexample: str, message: str):
    ring_style = "display:block;width:92px;height:92px"
    if counterexample == "hidden":
        ring_style += ";display:none"
    font_size = 4 if counterexample == "small" else 12
    figure = f'''<span data-testid="usage-ring" style="{ring_style}">
      <span data-usage-label>Quota</span>
      <span data-usage-amount style="font-size:{font_size}px">$10.00</span>
    </span>'''
    if counterexample == "clipped":
        figure = f'<div style="width:40px;height:40px;overflow:hidden">{figure}</div>'
    if counterexample == "empty":
        figure = ""
    page.set_content(f"""<div class="usage-board" style="font-size:12px">
      <div data-testid="usage-tile" style="width:180px;height:180px">{figure}</div>
    </div>""")
    with pytest.raises(AssertionError, match=message):
        assert_usage_geometry(
            page,
            {
                '[data-testid="usage-ring"]': 1,
                "[data-usage-label]": 1,
                "[data-usage-amount]": 1,
            },
        )


def test_usage_geometry_accepts_unclipped_glyphs_outside_their_line_box(page: Page):
    page.set_content("""<div class="usage-board" style="font-size:16px">
      <div data-testid="usage-tile" style="width:180px;height:180px;padding:20px">
        <span data-testid="usage-ring" style="display:block;width:92px;height:92px">
          <span data-usage-label>Quota</span>
          <span data-usage-amount style="display:block;line-height:10px">$10.00</span>
        </span>
      </div>
    </div>""")
    assert_usage_geometry(
        page,
        {
            '[data-testid="usage-ring"]': 1,
            "[data-usage-label]": 1,
            "[data-usage-amount]": 1,
        },
    )
