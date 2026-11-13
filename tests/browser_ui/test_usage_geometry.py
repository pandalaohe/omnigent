"""The geometry assertion must reject disappearing, clipped and unreadable figures."""

import pytest
from playwright.sync_api import Page

from tests.browser_ui.usage_geometry import USAGE_GEOMETRY, assert_usage_geometry


@pytest.mark.parametrize(
    ("counterexample", "message"),
    [
        ("empty", "expected 1"),
        ("hidden", "not visible"),
        ("clipped", "ancestor clips"),
        ("scroller-horizontal", "ancestor clips horizontally"),
        ("small", "text below 10px"),
        ("name-title", "missing full name title"),
        ("name-wrap", "unbounded name"),
    ],
)
def test_usage_geometry_rejects_counterexamples(page: Page, counterexample: str, message: str):
    ring_style = "display:block;width:92px;height:92px"
    if counterexample == "hidden":
        ring_style += ";display:none"
    font_size = 4 if counterexample == "small" else 12
    label_title = "" if counterexample == "name-title" else "Quota"
    label_style = "display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
    if counterexample == "name-wrap":
        label_style = "display:block"
    figure = f'''<span data-testid="usage-ring" style="{ring_style}">
      <span data-usage-label data-usage-name title="{label_title}"
        style="{label_style}">Quota</span>
      <span data-usage-amount style="font-size:{font_size}px">$10.00</span>
    </span>'''
    if counterexample == "clipped":
        figure = f'<div style="width:40px;height:40px;overflow:hidden">{figure}</div>'
    if counterexample == "scroller-horizontal":
        figure = f'<div style="width:40px;height:40px;overflow-y:auto">{figure}</div>'
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


def test_usage_geometry_accepts_truncated_label_with_full_title(page: Page):
    page.set_content("""<div class="usage-board" style="font-size:16px">
      <div data-testid="usage-tile" style="width:180px;height:180px;padding:20px">
        <span data-usage-label title="Long window name with a meaningful suffix"
          style="display:block;width:60px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
          Long window name with a meaningful suffix
        </span>
      </div>
    </div>""")
    assert_usage_geometry(page, {"[data-usage-label]": 1})


@pytest.mark.parametrize("small_text", [False, True])
def test_usage_geometry_restores_scroll_positions(page: Page, small_text: bool):
    page.set_content("""<div style="height:100px;overflow:hidden;margin-top:1000px">
      <div id="scroller" style="height:100px;overflow-y:auto">
        <div style="height:200px"></div>
        <div class="usage-board" style="font-size:12px">
          <span data-usage-label style="display:block">Quota</span>
        </div>
      </div>
    </div><div style="height:1000px"></div>""")
    page.evaluate("document.querySelector('#scroller').scrollTop = 25; window.scrollTo(0, 500)")
    if small_text:
        page.locator("[data-usage-label]").evaluate("el => el.style.fontSize = '4px'")
    positions = "() => [scrollX, scrollY, document.querySelector('#scroller').scrollTop]"
    before = page.evaluate(positions)
    if small_text:
        with pytest.raises(AssertionError, match="text below 10px"):
            assert_usage_geometry(page, {"[data-usage-label]": 1})
    else:
        assert_usage_geometry(page, {"[data-usage-label]": 1})
    assert page.evaluate(positions) == before


def test_usage_geometry_accepts_explicit_horizontal_table_scrolling(page: Page):
    page.set_content("""<div class="usage-board" style="width:180px;font-size:12px">
      <div class="overflow-x-auto" style="width:100px;overflow-x:auto">
        <table style="width:200px"><tr><td>
          <span data-usage-label style="display:block;width:200px">Quota</span>
        </td></tr></table>
      </div>
    </div>""")
    scroller = page.locator(".overflow-x-auto")
    scroller.evaluate("el => el.scrollLeft = 35")
    before = scroller.evaluate("el => el.scrollLeft")
    assert_usage_geometry(page, {"[data-usage-label]": 1})
    assert scroller.evaluate("el => el.scrollLeft") == before


@pytest.mark.parametrize(
    ("outer_style", "horizontal", "vertical"),
    [
        ("width:40px;height:40px;overflow:hidden", True, True),
        ("width:40px;height:40px;overflow-y:auto", True, True),
        ("width:100px;height:40px;overflow-y:auto", False, True),
        ("width:40px;height:40px;clip-path:inset(0)", True, True),
        ("width:180px;height:180px;overflow:hidden", False, False),
    ],
)
def test_usage_geometry_checks_outer_ancestors(
    page: Page, outer_style: str, horizontal: bool, vertical: bool, record_property
):
    page.set_content(f"""<div id="outer" style="{outer_style}">
      <div class="usage-board" style="font-size:12px;width:180px">
        <div data-testid="usage-tile" style="width:180px;height:180px">
          <span data-testid="usage-ring" style="display:block;width:92px;height:92px">
            <span data-usage-label>Quota</span>
            <span data-usage-amount style="font-size:12px">$10.00</span>
          </span>
        </div>
      </div>
    </div>""")
    failures = page.evaluate(USAGE_GEOMETRY, {'[data-testid="usage-ring"]': 1})
    for axis, clipped in [("horizontally", horizontal), ("vertically", vertical)]:
        assert (
            any(
                message.startswith(f"ancestor clips {axis}: SPAN.")
                and "Quota" in message
                and "$10.00" in message
                for message in failures
            )
            is clipped
        ), failures
    if not horizontal and not vertical:
        assert failures == []
    record_property("outer_clip_failures", str(failures))
