"""E2E: mobile sidebar toggle reachability + chat-header tap-target sizes.

Regression guards for the mobile chat header's touch ergonomics. Drives the
real SPA at an iPhone-class viewport (390x844, touch, mobile UA) and checks
two symptoms as a user experiences them:

1. The chat-header "Open sidebar" toggle (top-left) must be tappable and must
   actually open the sidebar drawer -- and the drawer must close from a tap on
   the exposed right strip and reopen from the toggle again.
2. The chat header's icon buttons must meet the ~44px mobile tap-target
   minimum (Apple HIG / WCAG target-size guidance). A press anywhere inside
   the 44px minimum zone centred on a control must activate it; sub-44px hit
   boxes are how "need to tap a couple times" happens on a phone.

The near-miss press in the second test uses a precise pointer
(``page.mouse``), not ``page.touchscreen``: Chromium's touch emulation snaps
near-miss taps onto the nearest target from up to ~8px away (verified while
authoring this test), which would mask the undersized hit box. A precise
press has no such fuzz, so it deterministically probes the geometry that the
44px rule guarantees to imprecise fingers.

Same no-LLM pattern as the sibling mobile tests: the seeded session page
renders its header and drawer deterministically with no model output needed.
"""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

# iPhone-12/13-class portrait viewport -- comfortably below the Tailwind
# ``md`` breakpoint (768px) so every ``max-md:`` rule is in effect.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}

# Minimum mobile tap target (Apple HIG 44pt; WCAG's enhanced target size).
# Half a pixel of tolerance absorbs subpixel layout rounding.
_MIN_TAP_PX = 44.0
_EPSILON = 0.5

# A visible touch dot for recorded runs, so the filmed journey shows *where*
# the user tapped (Playwright videos otherwise render touch input invisibly).
# pointer-events: none keeps it from ever intercepting the tap itself; gated
# on the recording env var so normal CI runs drive an unmodified page.
_TOUCH_DOT_SCRIPT = """
  window.addEventListener('pointerdown', (e) => {
    const dot = document.createElement('div');
    dot.style.cssText =
      'position:fixed;z-index:2147483647;pointer-events:none;' +
      'width:36px;height:36px;border-radius:50%;' +
      'background:rgba(255,80,80,0.45);border:2px solid rgba(255,80,80,0.9);' +
      `left:${e.clientX - 18}px;top:${e.clientY - 18}px;` +
      'transition:opacity 0.6s ease-out;';
    document.body.appendChild(dot);
    requestAnimationFrame(() => { dot.style.opacity = '0'; });
    setTimeout(() => dot.remove(), 700);
  }, true);
"""


def _new_mobile_page(browser: Browser) -> tuple[BrowserContext, Page]:
    """Open a phone-profile context (+ touch dot overlay when recording)."""
    context = browser.new_context(
        viewport=_MOBILE_VIEWPORT,
        has_touch=True,
        is_mobile=True,
        record_video_dir=os.environ.get("OMNIGENT_E2E_RECORD_DIR"),
    )
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        context.add_init_script(_TOUCH_DOT_SCRIPT)
    return context, context.new_page()


def _beat(page: Page) -> None:
    """Pause briefly between journey steps -- only while filming a clip."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(900)


def _open_session(page: Page, base_url: str, session_id: str) -> None:
    """Load the session page and prove the mobile layout branch is active."""
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    assert page.evaluate("matchMedia('(max-width: 767.98px)').matches"), (
        "expected the mobile (max-md) layout branch to be in effect"
    )


def test_sidebar_opens_from_header_toggle_on_mobile(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """The top-left header toggle opens the drawer; the strip tap closes it.

    Symptom guarded: "Sidebar button isn't clickable on mobile (top-left)."
    The toggle must offer a >=~44px tap target, be the element that actually
    receives a tap at its centre (nothing overlaying it), and drive the full
    drawer cycle: tap -> open, strip tap -> close, tap -> open again.
    """
    base_url, session_id = seeded_session
    context, page = _new_mobile_page(browser)
    try:
        _open_session(page, base_url, session_id)

        toggle = page.get_by_role("button", name="Open sidebar")
        expect(toggle).to_be_visible(timeout=10_000)

        # Tap-target size: the toggle itself must meet the 44px minimum.
        box = toggle.bounding_box()
        assert box is not None
        assert (
            box["width"] >= _MIN_TAP_PX - _EPSILON and box["height"] >= _MIN_TAP_PX - _EPSILON
        ), (
            f"'Open sidebar' toggle hit box is {box['width']:.1f}x"
            f"{box['height']:.1f}px, below the {_MIN_TAP_PX:.0f}px mobile minimum"
        )

        # Reachability: the toggle (not some overlay) receives a centre tap.
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        assert page.evaluate(
            """([x, y]) => {
                const el = document.elementFromPoint(x, y);
                const btn = el && el.closest('button');
                return !!(btn && btn.getAttribute('aria-label') === 'Open sidebar');
            }""",
            [cx, cy],
        ), "an overlay intercepts taps on the 'Open sidebar' toggle"

        drawer = page.locator('aside[aria-label="Conversations"]')
        expect(drawer).to_have_attribute("aria-hidden", "true")

        # 1st tap: the drawer must open (this is the reported failure point).
        _beat(page)
        toggle.tap()
        expect(drawer).to_have_attribute("aria-hidden", "false", timeout=5_000)
        expect(page.get_by_test_id("sidebar-conversation-list")).to_be_visible()

        # Tap the exposed right strip (the drawer stops 56px short of the
        # right edge; the scrim behind it is the documented way back).
        _beat(page)
        page.get_by_test_id("sidebar-scrim").tap(position={"x": 362, "y": 420})
        expect(drawer).to_have_attribute("aria-hidden", "true", timeout=5_000)

        # 2nd tap: reopening must work too ("need to tap a couple times").
        _beat(page)
        toggle.tap()
        expect(drawer).to_have_attribute("aria-hidden", "false", timeout=5_000)
        expect(page.get_by_test_id("sidebar-conversation-list")).to_be_visible()
        _beat(page)
    finally:
        context.close()


@pytest.mark.parametrize("width,height", [(320, 844), (390, 844), (844, 390)])
def test_settings_phone_hit_areas_activate_without_stealing_neighbours(
    browser: Browser, live_server: str, width: int, height: int
) -> None:
    """Small controls keep their visuals while precise outer-ring presses work."""
    context, page = _new_mobile_page(browser)
    try:
        page.set_viewport_size({"width": width, "height": height})
        page.goto(f"{live_server}/settings/shortcuts")
        page.get_by_role("textbox", name="Search shortcuts").fill("Start a new session")
        shortcut = page.get_by_test_id("shortcut-editor-row-newSession")
        expect(shortcut).to_be_visible(timeout=30_000)
        shortcut_toggle = shortcut.get_by_role("switch")
        shortcut_toggle.scroll_into_view_if_needed()
        box = shortcut_toggle.bounding_box()
        assert box is not None
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2 + 20.5)
        expect(shortcut_toggle).to_have_attribute("aria-checked", "false")
        shortcut_toggle.click()
        shortcut.get_by_role(
            "button", name="Record common shortcut for Start a new session"
        ).click()
        page.keyboard.press("Alt+Shift+F12")
        expect(shortcut.get_by_text("F12", exact=True)).to_be_visible()
        restore = shortcut.get_by_role(
            "button", name="Restore default shortcut for Start a new session"
        )
        restore.scroll_into_view_if_needed()
        box = restore.bounding_box()
        assert box is not None
        page.mouse.click(box["x"] + box["width"] / 2 + 20, box["y"] + box["height"] / 2)
        expect(shortcut.get_by_text("F12", exact=True)).to_have_count(0)
        expect(page.get_by_role("menu")).not_to_be_visible()
        shortcut.get_by_role("button", name="Add system override for Start a new session").click()
        page.get_by_role("menuitem", name="macOS", exact=True).click()
        page.keyboard.press("Escape")
        platform = page.get_by_test_id("shortcut-platform-newSession-macos")
        restore = platform.get_by_role(
            "button", name="Restore macOS shortcut for Start a new session"
        )
        remove = platform.get_by_role(
            "button", name="Delete macOS override for Start a new session"
        )
        restore.scroll_into_view_if_needed()
        restore_box, remove_box = restore.bounding_box(), remove.bounding_box()
        assert restore_box is not None and remove_box is not None
        assert remove_box["x"] - restore_box["x"] >= 44 - _EPSILON
        page.mouse.click(
            restore_box["x"] + restore_box["width"] / 2 + 20,
            restore_box["y"] + restore_box["height"] / 2,
        )
        expect(platform).to_be_visible()
        remove.click()
        expect(platform).to_have_count(0)

        toggle = page.get_by_role("switch", name="Enable mobile floating assistant")
        expect(toggle).to_be_visible(timeout=30_000)
        toggle.scroll_into_view_if_needed()
        box = toggle.bounding_box()
        assert box is not None
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        initial = toggle.get_attribute("aria-checked")
        page.mouse.click(cx, cy)
        expect(toggle).to_have_attribute("aria-checked", "false" if initial == "true" else "true")
        page.mouse.click(cx, cy + 20)
        expect(toggle).to_have_attribute("aria-checked", initial or "false")
        # The assistant is draggable app chrome; keep it out of the settings
        # probes, whose targets are the underlying shared controls.
        if toggle.get_attribute("aria-checked") == "true":
            toggle.click()
        expect(page.get_by_test_id("mobile-floating-assistant")).to_have_count(0)
        # All viewport cases share the test server's user preferences.
        page.get_by_role("button", name="Restore default buttons", exact=True).click()

        # Chat rows deliberately retain 24px icons: their 44px targets must
        # survive the rounded list's overflow clipping and remain independent.
        edit = page.get_by_role("button", name="Edit Approve", exact=True)
        delete = page.get_by_role("button", name="Delete Approve", exact=True)
        edit.scroll_into_view_if_needed()
        edit_box, delete_box = edit.bounding_box(), delete.bounding_box()
        assert edit_box is not None and delete_box is not None
        assert delete_box["x"] - edit_box["x"] >= 44 - _EPSILON
        ex = edit_box["x"] + edit_box["width"] / 2
        ey = edit_box["y"] + edit_box["height"] / 2
        for offset in (0, -20):
            page.mouse.click(ex + offset, ey)
            dialog = page.get_by_role("dialog")
            expect(dialog).to_be_visible()
            dialog.get_by_role("button", name="Cancel", exact=True).click()
            expect(dialog).not_to_be_visible()
        page.mouse.click(
            delete_box["x"] + delete_box["width"] / 2,
            delete_box["y"] + delete_box["height"] / 2,
        )
        expect(page.get_by_role("dialog")).not_to_be_visible()
        expect(edit).to_have_count(0)

        page.get_by_role("radio", name="Terminal", exact=True).click()
        assert page.evaluate("matchMedia('(pointer: coarse)').matches")
        terminal_toggle = page.get_by_role("switch", name="Enable terminal key rows")
        terminal_toggle.evaluate("el => el.scrollIntoView({block: 'center'})")
        box = terminal_toggle.bounding_box()
        assert box is not None
        initial = terminal_toggle.get_attribute("aria-checked")
        assert 22 <= box["y"] + box["height"] / 2 <= height - 22
        probe = [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2 + 20]
        assert terminal_toggle.evaluate(
            "(el, [x, y]) => el.contains(document.elementFromPoint(x, y))", probe
        ), "terminal key-row switch's outer target is clipped or covered"
        page.mouse.click(*probe)
        expect(terminal_toggle).to_have_attribute(
            "aria-checked", "false" if initial == "true" else "true"
        )
        # The next switch's centre must still address only that switch.
        second = page.get_by_role("switch", name="Show second terminal key row")
        second.click()
        expect(terminal_toggle).to_have_attribute(
            "aria-checked", "false" if initial == "true" else "true"
        )
    finally:
        context.close()


def test_header_kebab_meets_mobile_tap_target(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """The header 'Conversation actions' kebab offers a >=~44px tap target.

    Symptom guarded: "Navbar buttons too small -- need to tap a couple
    times." The kebab's interactive hit area must cover the 44px minimum tap
    zone centred on it, so an imprecise finger press still activates it.
    Without a mobile size override (``max-md:size-11``) the kebab renders
    40x40 (Button size="icon" = ``size-10 md:size-8``), and a press landing
    in the outer ring of the minimum zone hits dead header space instead.
    """
    base_url, session_id = seeded_session
    context, page = _new_mobile_page(browser)
    try:
        _open_session(page, base_url, session_id)

        kebab = page.get_by_test_id("header-conversation-actions")
        expect(kebab).to_be_visible(timeout=10_000)
        box = kebab.bounding_box()
        assert box is not None
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        menu = page.get_by_role("menu")

        # Journey step 1: a dead-centre tap works -- the control itself is
        # functional, the defect is purely its undersized hit area.
        _beat(page)
        page.touchscreen.tap(cx, cy)
        expect(menu).to_be_visible(timeout=5_000)
        _beat(page)
        page.keyboard.press("Escape")
        expect(menu).not_to_be_visible(timeout=5_000)

        # Journey step 2: press inside the 44px minimum zone, 1px outside a
        # 40px hit box (21px left of centre: |offset| > 40/2, < 44/2, with a
        # 1px margin each way). On a compliant >=44px control this activates
        # the menu; on the shrunken one it lands on dead header space -- the
        # mobile mis-tap users reported. Precise pointer, not touchscreen:
        # Chromium touch emulation would snap the near-miss onto the button
        # and mask the bug (see module docstring).
        ring_x, ring_y = cx - 21.0, cy
        _beat(page)
        page.mouse.click(ring_x, ring_y)
        try:
            expect(menu).to_be_visible(timeout=2_000)
        except AssertionError:
            raise AssertionError(
                f"Press at ({ring_x:.1f},{ring_y:.1f}) -- inside the "
                f"{_MIN_TAP_PX:.0f}px minimum tap zone centred on the "
                f"'Conversation actions' kebab -- did not open the menu: the "
                f"kebab's actual hit box is {box['width']:.1f}x"
                f"{box['height']:.1f}px, below the mobile tap-target minimum."
            ) from None
        page.keyboard.press("Escape")

        # Belt and braces: every icon-only button in the chat header meets
        # the minimum, so a future header control is held to the same bar.
        offenders = page.evaluate(
            """(min) => {
                const out = [];
                const header = document.querySelector('.chat-header');
                if (!header) return ['no .chat-header found'];
                for (const el of header.querySelectorAll('button')) {
                    if ((el.textContent || '').trim()) continue;  // icon-only
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;  // hidden
                    if (r.width < min || r.height < min) {
                        const name = el.getAttribute('aria-label')
                            || el.getAttribute('data-testid') || el.outerHTML.slice(0, 60);
                        out.push(`${name}: ${r.width.toFixed(1)}x${r.height.toFixed(1)}`);
                    }
                }
                return out;
            }""",
            _MIN_TAP_PX - _EPSILON,
        )
        assert not offenders, (
            "chat-header icon buttons below the "
            f"{_MIN_TAP_PX:.0f}px mobile tap-target minimum: {offenders}"
        )
    finally:
        context.close()
