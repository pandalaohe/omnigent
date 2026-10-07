"""Browser-lane geometry for the runner-log runaway warning band."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat._geometry_helpers import assert_no_overlap, box
from tests.browser_ui.chat.session_contract import ChatSessionContract

TOLERANCE = 1.0
RUNAWAY_LABELS = {
    "omnigent.runner_log_runaway": "2026-09-23T09:25:00+00:00",
    "omnigent.runner_log_runaway_mb": "5",
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


def _fade_end(page: Page) -> float:
    """Offset below the transcript viewport's top edge where its top mask lets content show."""
    mask = page.get_by_role("log").evaluate(
        "el => getComputedStyle(el).maskImage || getComputedStyle(el).webkitMaskImage"
    )
    if not mask or mask == "none":
        return 0.0
    match = _FIRST_PX_STOP.search(mask)
    return float(match.group(1)) if match else 0.0


@pytest.mark.parametrize(
    ("viewport", "shell"),
    [
        pytest.param({"width": 402, "height": 874}, "web", id="phone-web"),
        pytest.param({"width": 402, "height": 874}, "ios", id="phone-ios-shell"),
        pytest.param({"width": 1280, "height": 852}, "web", id="desktop"),
    ],
)
def test_runaway_banner_leaves_no_dead_band_under_the_header(
    page: Page,
    chat_session_contract: ChatSessionContract,
    viewport: dict[str, int],
    shell: str,
) -> None:
    """The warning stays readable and the transcript still starts under the header."""
    chat = chat_session_contract
    chat.contract.json("/v1/system/status", {"revision": 0, "level": "ok", "findings": []})
    chat.seed_transcript(40)
    chat.update_session(labels=RUNAWAY_LABELS)
    if shell == "ios":
        page.add_init_script(IOS_SHELL_SCRIPT)
    page.set_viewport_size(viewport)
    page.goto(chat.url)
    banner = page.get_by_test_id("runner-log-runaway-banner")
    expect(banner).to_contain_text("5 MB in the last hour", timeout=20_000)
    expect(page.get_by_text("Explain browser fixture turn 40.")).to_be_visible()

    header = box(page.locator("header.chat-header"))
    transcript = box(page.get_by_role("log"))
    warning = box(banner)
    # The band is neither tucked under the status bar and floating header nor
    # on top of the transcript.
    assert warning["y"] >= header["y"] + header["height"] - TOLERANCE, (warning, header)
    assert warning["y"] + warning["height"] <= viewport["height"] + TOLERANCE, warning
    assert_no_overlap(warning, transcript)
    # The transcript's header clearance is calibrated to a viewport that starts
    # under the header; pushed down, that clearance becomes a blank band.
    visible_from = transcript["y"] + _fade_end(page)
    assert visible_from <= header["y"] + header["height"] + TOLERANCE, (
        visible_from,
        header,
        transcript,
    )
