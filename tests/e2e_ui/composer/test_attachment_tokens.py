"""Browser proof for inline attachment tokens in the plain-textarea composer.

The composer shows each pending attachment as a visible token (``[image N]`` /
``[file N]``) inside the textarea's value, tinted by ``ComposerTokenBackdrop``
over the field, with a token badge on the tile below it. These tests drive the
live SPA: they attach through the real picker, place the caret with
``setSelectionRange``, measure the tint against the token's own text metrics,
move the token by editing the value, and assert what the composer sends.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _SESSIONS_RE,
    _WORKTREES_RE,
    _agents_body,
    _hosts_body,
)

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

_LANDING_INPUT = "new-chat-landing-input"
_LANDING_FILE_INPUT = "new-chat-landing-file-input"
_LANDING_SUBMIT = "new-chat-landing-submit"
_OVERLAY = '[data-testid="composer-highlight-overlay"]'
_TOKEN = "[image 1]"
# Sub-pixel layout differences between the field and its mirror are fine; a
# mis-anchored overlay (wrong padding, wrong box source) is not.
_TOLERANCE_PX = 3

# The tinted segment's geometry, plus the token's expected slot measured
# independently: a hidden mirror laid out at the textarea's own content origin
# with the textarea's own computed font. ``prefix.right`` is where the token
# starts; ``throughToken.right`` is where it ends; the mirror's line box gives
# the vertical band.
_TOKEN_TINT_GEOMETRY = """
({ token }) => {
  const overlay = document.querySelector('[data-testid="composer-highlight-overlay"]');
  const textarea = document.querySelector('[data-testid="new-chat-landing-input"]');
  if (!overlay || !textarea) return null;
  const tinted = [...overlay.querySelectorAll('span')].find(
    (span) => span.textContent === token,
  );
  if (!tinted) return null;

  const style = getComputedStyle(textarea);
  const mirror = document.createElement('div');
  mirror.style.position = 'fixed';
  mirror.style.margin = '0';
  mirror.style.width = 'max-content';
  mirror.style.whiteSpace = 'pre';
  mirror.style.visibility = 'hidden';
  for (const property of [
    'fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'fontVariant',
    'letterSpacing', 'lineHeight', 'textTransform', 'wordSpacing',
  ]) {
    mirror.style[property] = style[property];
  }
  document.body.appendChild(mirror);
  const fieldBox = textarea.getBoundingClientRect();
  mirror.style.left = `${fieldBox.left + parseFloat(style.paddingLeft)}px`;
  mirror.style.top = `${fieldBox.top + parseFloat(style.paddingTop)}px`;
  const start = textarea.value.indexOf(token);
  mirror.textContent = textarea.value.slice(0, start);
  const prefix = mirror.getBoundingClientRect();
  mirror.textContent = textarea.value.slice(0, start + token.length);
  const throughToken = mirror.getBoundingClientRect();
  mirror.remove();

  const box = tinted.getBoundingClientRect();
  return {
    token: tinted.textContent,
    tinted: { left: box.left, right: box.right, top: box.top, bottom: box.bottom },
    expected: {
      left: prefix.right,
      right: throughToken.right,
      top: prefix.top,
      bottom: prefix.bottom,
    },
  };
}
"""


def _attach(page: Page, name: str) -> None:
    """Attach one PNG through the landing composer's picker."""
    page.get_by_test_id(_LANDING_FILE_INPUT).set_input_files(
        {"name": name, "mimeType": "image/png", "buffer": _ONE_PIXEL_PNG}
    )


def _caret_at(page: Page, offset: int) -> None:
    """Place a collapsed textarea caret at ``offset``."""
    page.get_by_test_id(_LANDING_INPUT).evaluate(
        "(element, offset) => element.setSelectionRange(offset, offset)", offset
    )


def _wait_for(predicate: Any, page: Page, *, timeout_s: float = 30.0) -> None:
    """Poll ``predicate`` while pumping the sync driver's event queue."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _assert_backdrop_tints_token(page: Page, token: str) -> None:
    """The tinted overlay segment sits exactly over the token's slot in the field."""
    geometry = page.evaluate(_TOKEN_TINT_GEOMETRY, {"token": token})
    assert geometry is not None, f"no tinted backdrop segment for {token}"
    assert geometry["token"] == token, geometry
    tinted, expected = geometry["tinted"], geometry["expected"]
    assert abs(tinted["left"] - expected["left"]) <= _TOLERANCE_PX, geometry
    assert abs(tinted["right"] - expected["right"]) <= _TOLERANCE_PX, geometry
    # Vertical overlap only: the tint boxes the glyphs while the mirror carries
    # the full line box, so the two heights need not match.
    assert tinted["top"] < expected["bottom"] + _TOLERANCE_PX, geometry
    assert tinted["bottom"] > expected["top"] - _TOLERANCE_PX, geometry


def test_picker_inserts_token_at_caret_and_backdrop_tints_it(
    page: Page,
    live_server: str,
) -> None:
    """The picker inserts the token where the caret was, tinted in place."""
    page.goto(f"{live_server}/")
    editor = page.get_by_test_id(_LANDING_INPUT)
    expect(editor).to_be_visible(timeout=30_000)
    editor.fill("before after")
    _caret_at(page, 7)

    _attach(page, "middle.png")

    # Inserted at the caret, not appended: the token sits between the words.
    expect(editor).to_have_value("before [image 1] after")
    expect(page.get_by_role("button", name="Remove middle.png")).to_be_visible()
    expect(page.get_by_role("button", name=_TOKEN, exact=True)).to_be_visible()
    expect(page.locator(_OVERLAY)).to_be_visible()

    _assert_backdrop_tints_token(page, _TOKEN)

    # The badge focuses the field holding its token, caret just after it.
    page.get_by_role("button", name=_TOKEN, exact=True).click()
    page.wait_for_function(
        """token => {
          const field = document.querySelector('[data-testid="new-chat-landing-input"]');
          return document.activeElement === field
            && field.selectionStart === field.value.indexOf(token) + token.length;
        }""",
        arg=_TOKEN,
    )


def test_moving_the_token_moves_the_attachment_in_the_send(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Editing the value so the token is last sends the attachment last."""
    base_url, session_id = seeded_session
    creates: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    _stub_landing_composer(page, session_id, creates)
    _capture_sent_messages(page, events)
    page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID!r}: ["/work/repo"] }})
        );"""
    )

    page.goto(f"{base_url}/")
    editor = page.get_by_test_id(_LANDING_INPUT)
    expect(editor).to_be_visible(timeout=30_000)
    editor.fill("before after")
    _caret_at(page, 7)
    _attach(page, "middle.png")
    expect(editor).to_have_value("before [image 1] after")

    # Moving the attachment is ordinary textarea editing: rewrite the value so
    # the token is last, and the tile keeps its token badge.
    editor.fill("before after [image 1]")
    expect(editor).to_have_value("before after [image 1]")
    expect(page.get_by_role("button", name=_TOKEN, exact=True)).to_be_visible()

    submit = page.get_by_test_id(_LANDING_SUBMIT)
    expect(submit).to_be_enabled(timeout=30_000)
    submit.click()
    _wait_for(lambda: len(creates) == 1, page)
    _wait_for(lambda: len(events) == 1, page)

    # What the composer actually sent: text first, the moved attachment last.
    content = events[0]["data"]["content"]
    assert [(block["type"], block.get("text", "").strip()) for block in content] == [
        ("input_text", "before after"),
        ("input_image", ""),
    ]

    # And the sent order is what the session's user bubble shows.
    bubble = page.locator('[data-testid="message-bubble"][data-role="user"]').first
    expect(bubble).to_be_visible(timeout=30_000)
    expect(bubble.locator("img").first).to_be_visible(timeout=30_000)
    order = bubble.evaluate(
        """element => {
          const text = [...element.querySelectorAll('*')].find(
            (node) => (node.textContent || '').trim() === 'before after',
          );
          const image = element.querySelector('img');
          if (!text || !image) return null;
          return {
            textTop: text.getBoundingClientRect().top,
            imageTop: image.getBoundingClientRect().top,
          };
        }"""
    )
    assert order is not None
    assert order["textTop"] < order["imageTop"], order


def _stub_landing_composer(page: Page, session_id: str, creates: list[dict[str, Any]]) -> None:
    """Stub the landing picker's lookups so a create can run, and capture it.

    Mirrors ``test_start_session._register_common_routes``: the tunneled runner
    registers no *host*, so the composer needs a faked online host to reach a
    submittable state. The create is answered with the real seeded session id
    so the follow-up navigation lands on a live page.
    """
    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_hosts_body()
        ),
    )
    page.route(
        "**/v1/hosts/*/harnesses/*/model-options", lambda route: route.fulfill(json={"models": []})
    )
    page.route(
        "**/v1/sandbox-providers/*/harnesses/*/model-options*",
        lambda route: route.fulfill(
            json={
                "configured": False,
                "status": "unconfigured",
                "models": [],
                "configuration_revision": None,
                "provider_label": None,
                "default_model": None,
            }
        ),
    )
    page.route(_WORKTREES_RE, lambda route: route.fulfill(json={"data": []}))
    page.route(
        "**/v1/agents",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_agents_body()
        ),
    )

    def handle_sessions(route: Route) -> None:
        if route.request.method == "POST":
            creates.append(route.request.post_data_json)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": session_id}),
            )
        else:
            route.continue_()

    page.route(_SESSIONS_RE, handle_sessions)
    # Sessions left by other tests can add agents to the discovery scan; keep
    # the stubbed catalog the only source so the picker auto-selects one.
    page.route(
        re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        ),
    )


def _capture_sent_messages(page: Page, events: list[dict[str, Any]]) -> None:
    """Record every POST the session page makes to the message endpoint."""

    def handle_events(route: Route) -> None:
        if route.request.method == "POST":
            body = route.request.post_data_json
            if body is not None:
                events.append(body)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
        )

    page.route("**/v1/sessions/*/events", handle_events)
