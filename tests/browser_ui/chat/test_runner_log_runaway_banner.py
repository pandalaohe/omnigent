"""Browser-lane geometry for the runner-log runaway warning band."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import FloatRect, Page, Route, expect

from tests.browser_ui.chat._geometry_helpers import assert_no_overlap, box
from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    list_payload,
    transcript_items,
)

TOLERANCE = 1.0
PHONE = {"width": 402, "height": 874}


def _runaway_labels() -> dict[str, str]:
    """Fresh ``seen`` because the web banner lapses 15 minutes after that stamp."""
    return {
        "omnigent.runner_log_runaway": "2026-09-23T09:25:00+00:00",
        "omnigent.runner_log_runaway_mb": "5",
        "omnigent.runner_log_runaway_seen": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
    }


# The iOS shell renders under the status bar. Headless Chromium reports a zero
# env(safe-area-inset-top), so feed the inset through the shell's override var.
IOS_SHELL_SCRIPT = """
window.omnigentNative = { kind: "ios" };
document.addEventListener("DOMContentLoaded", () => {
  document.documentElement.style.setProperty("--omnigent-android-safe-area-top", "62px");
});
"""
_FIRST_PX_STOP = re.compile(r"(-?\d+(?:\.\d+)?)px")
_SCROLLER_JS = """log => [...log.querySelectorAll("*")].find(
  (el) => ["auto", "scroll"].includes(getComputedStyle(el).overflowY))"""


def _open(
    page: Page,
    chat: ChatSessionContract,
    viewport: dict[str, int],
    *,
    shell: str,
    banner: bool,
) -> None:
    if banner:
        chat.update_session(labels=_runaway_labels())
    if shell == "ios":
        page.add_init_script(IOS_SHELL_SCRIPT)
    page.set_viewport_size(viewport)
    page.goto(chat.url)
    expect(page.get_by_text("Explain browser fixture turn 40.")).to_be_visible(timeout=20_000)
    warning = page.get_by_test_id("runner-log-runaway-banner")
    if banner:
        expect(warning).to_contain_text("5 MB in the last hour")
    else:
        expect(warning).to_have_count(0)


def _fade_end(page: Page) -> float:
    """Offset below the transcript viewport's top edge where its top mask lets content show."""
    mask = page.get_by_role("log").evaluate(
        "el => getComputedStyle(el).maskImage || getComputedStyle(el).webkitMaskImage"
    )
    if not mask or mask == "none":
        return 0.0
    match = _FIRST_PX_STOP.search(mask)
    assert match, f"unrecognised transcript mask: {mask}"
    return float(match.group(1))


def _assert_transcript_starts_under_header(page: Page) -> FloatRect:
    """Header clearance assumes the viewport starts under the header; lower, it is a blank band."""
    header = box(page.locator("header.chat-header"))
    transcript = box(page.get_by_role("log"))
    visible_from = transcript["y"] + _fade_end(page)
    assert visible_from <= header["y"] + header["height"] + TOLERANCE, (
        visible_from,
        header,
        transcript,
    )
    return header


def _scroll_top(page: Page) -> float:
    return page.get_by_role("log").evaluate(f"log => ({_SCROLLER_JS})(log).scrollTop")


def _touch_drag(page: Page, finger_dy: int) -> None:
    """Drag a finger over the transcript; a positive distance scrolls toward older messages."""
    transcript = box(page.get_by_role("log"))
    page.context.new_cdp_session(page).send(
        "Input.synthesizeScrollGesture",
        {
            "x": transcript["x"] + transcript["width"] / 2,
            "y": transcript["y"] + transcript["height"] / 2,
            "yDistance": finger_dy,
            "gestureSourceType": "touch",
        },
    )


@pytest.mark.parametrize("banner", [True, False], ids=["warning", "no-warning"])
@pytest.mark.parametrize(
    ("viewport", "shell"),
    [
        pytest.param(PHONE, "web", id="phone-web"),
        pytest.param(PHONE, "ios", id="phone-ios-shell"),
        pytest.param({"width": 1280, "height": 852}, "web", id="desktop"),
    ],
)
def test_runaway_banner_leaves_no_dead_band_under_the_header(
    page: Page,
    chat_session_contract: ChatSessionContract,
    viewport: dict[str, int],
    shell: str,
    banner: bool,
) -> None:
    """The warning stays readable and the transcript still starts under the header."""
    chat = chat_session_contract
    chat.seed_transcript(40)
    _open(page, chat, viewport, shell=shell, banner=banner)
    # A running background task floats its pill above the composer.
    chat.wait_for_stream()
    chat.emit(
        {
            "event": "session.status",
            "data": {
                "conversation_id": chat.session_id,
                "status": "idle",
                "background_task_count": 1,
            },
        }
    )
    pill = page.locator('[role="status"][data-testid="background-task-pill"]')
    expect(pill).to_be_visible(timeout=10_000)

    header = _assert_transcript_starts_under_header(page)
    if not banner:
        return
    warning = box(page.get_by_test_id("runner-log-runaway-banner"))
    # Neither tucked under the status bar and floating header nor covered by
    # the transcript or the composer's overlays.
    assert warning["y"] >= header["y"] + header["height"] - TOLERANCE, (warning, header)
    assert warning["y"] + warning["height"] <= viewport["height"] + TOLERANCE, warning
    assert_no_overlap(warning, box(page.get_by_role("log")))
    assert_no_overlap(warning, box(pill))


@pytest.mark.browser_context_args(has_touch=True, is_mobile=True)
def test_runaway_banner_transcript_scrolls_by_touch_while_older_history_loads(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """With the warning shown, a finger scrolls the transcript while an older page is in flight."""
    chat = chat_session_contract
    newest = transcript_items(40)
    held: list[Route] = []

    def items(route: Route) -> None:
        if "after" in parse_qs(urlparse(route.request.url).query):
            held.append(route)  # Older page stays in flight until the test ends.
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({**list_payload(newest), "has_more": True}),
        )

    chat.contract.route(
        re.compile(rf"/v1/sessions/{re.escape(chat.session_id)}/items(?:\?.*)?$"), items
    )
    _open(page, chat, PHONE, shell="ios", banner=True)

    try:
        page.get_by_role("log").evaluate(f"log => {{ ({_SCROLLER_JS})(log).scrollTop = 600; }}")
        _touch_drag(page, 400)
        expect(page.get_by_text("Loading earlier messages…")).to_be_visible(timeout=10_000)
        _assert_transcript_starts_under_header(page)

        before = _scroll_top(page)
        _touch_drag(page, -300)
        page.wait_for_function(
            f"([log, before]) => ({_SCROLLER_JS})(log).scrollTop > before + 100",
            arg=[page.get_by_role("log").element_handle(), before],
        )
    finally:
        # An unanswered route stalls the shared browser for every later test.
        for route in held:
            route.abort()
