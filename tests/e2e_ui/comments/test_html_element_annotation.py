"""E2E: element annotation in the rendered HTML preview (pick → store → send).

An ``.html`` file opens in the sandboxed preview iframe (opaque origin, no
``allow-same-origin``). ``HtmlCommentViewer`` overlays an "Annotate" toggle
(``aria-label="Annotate"``, ``aria-pressed`` mirrors the mode); turning it on
loads a pick runtime into the frame that outlines hovered elements, keeps a
selected outline on the picked element, and reports the pick to the parent
over its own MessagePort. The owner composer (``annotation-composer`` in the
parent page, not the frame) collects the note and submits it. The parent saves
each pick as an ordinary comment whose
``anchor_content`` is ``__element__`` + a JSON anchor (CSS/XPath/text quote,
geometry, console and failed-request diagnostics, a screenshot file id) and,
on a send pick, posts this page's draft element annotations to the agent as
one message that the chat renders as one card per annotation.

The seeded fixture page carries the surfaces the design accepts: a paragraph,
a click-opened ``role="menu"`` dropdown, a ``:hover``-opened menu, an
icon-only button, a load-time ``console.error`` and an image that 404s.

  1. Clicking the toggle, then an element in the frame, opens the owner
     composer over the preview (the textarea autofocuses); ``⌘/Ctrl+Enter``
     stacks it. REST shows one draft ``__element__`` row carrying the element
     address, its text quote, the console error and the 404 image. Nothing
     reaches the chat.
  2. With the dropdown open before the mode turns on, the chord picks one of
     its options; the anchor names a ``button`` and carries a ``screenshot``
     file id whose content is served as ``image/jpeg``.
  3. After a reload the resolver re-finds the live annotation and reports a
     deliberately dead second one as orphaned (``comment-orphan``) only.
  4. Three stacked picks send as one message with three ``annotation-card``s
     and all three comments become ``addressed``.
  5. Rebinding ``toggleAnnotationMode`` in Settings makes the new chord toggle
     the mode in the already-open preview without a document reload; the old
     chord is inert.
  6. The text-selection "Add comment" flow still works beside the new mode.

If this goes red, the regression sits at one of the seams: the annotate
handshake (toggle hidden when the stub never answers), the picker's
click → owner-composer handoff (composer never opens or never focuses), the
parent save (no ``__element__`` row or no screenshot upload), the reload
resolver pass (orphan marker missing or attributed to the live row), the batch
send (cards or addressed state), or the live ``setShortcut`` push (rebinding
has no effect without a reload).
"""

from __future__ import annotations

import copy
import json
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from string import Template
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import FrameLocator, Locator, Page, Request, expect

# The hello_world agent spec uses ``os_env.cwd: .``, so the runner writes seeded
# files into the server process's cwd — the repo root (this file is
# ``<repo>/tests/e2e_ui/comments/...``, so the repo root is ``parents[3]``).
_REPO_ROOT = Path(__file__).resolve().parents[3]

_FIXTURE_PATH = "annotate_fixture.html"

#: ``anchor_content`` marker for element annotations (server + parent codec).
_ELEMENT_PREFIX = "__element__"

#: Default binding of ``toggleAnnotationMode``. Playwright's ``ControlOrMeta``
#: resolves to Cmd on macOS and Ctrl elsewhere, matching the app's "primary".
_DEFAULT_CHORD = "ControlOrMeta+Shift+Period"

#: The chord the rebinding test records for the same action. Primary+Shift+Y
#: collides with no default binding and no Chromium shortcut.
_NEW_CHORD = "ControlOrMeta+Shift+KeyY"

_SHORTCUTS_STORAGE_KEY = "omnigent:keyboard-shortcut-preferences"

_HEADING = "Annotation fixture report"
_PARAGRAPH = "The quarterly summary covers retention, revenue, and support volume."
_CONSOLE_ERROR = "fixture boom"
_MISSING_IMAGE = "missing.png"

_FIXTURE_HTML = Template(
    """\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>$heading</title>
    <style>
      body { font-family: system-ui, sans-serif; margin: 24px; }
      #fixture-hover-menu { display: none; }
      #fixture-hover-wrap:hover #fixture-hover-menu { display: block; }
      #fixture-menu[hidden] { display: none; }
    </style>
  </head>
  <body>
    <h1 id="fixture-heading">$heading</h1>
    <p id="fixture-paragraph">$paragraph</p>
    <div id="fixture-dropdown">
      <button id="fixture-menu-toggle" type="button" aria-expanded="false"
              aria-controls="fixture-menu">Open menu</button>
      <ul id="fixture-menu" role="menu" hidden>
        <li><button id="fixture-option-1" role="menuitem" type="button">First option</button></li>
        <li><button id="fixture-option-2" role="menuitem" type="button">Second option</button></li>
        <li><button id="fixture-option-3" role="menuitem" type="button">Third option</button></li>
      </ul>
    </div>
    <div id="fixture-hover-wrap">
      <button id="fixture-hover-trigger" type="button">Hover for actions</button>
      <ul id="fixture-hover-menu">
        <li><button type="button">Rename</button></li>
        <li><button type="button">Duplicate</button></li>
      </ul>
    </div>
    <button id="fixture-close" type="button" aria-label="Close">
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
        <path d="M2 2 L12 12 M12 2 L2 12" stroke="currentColor" stroke-width="2"></path>
      </svg>
    </button>
    <img id="fixture-missing-image" src="$missing_image" alt="missing" width="40" height="40" />
    <script>
      document.getElementById("fixture-menu-toggle").addEventListener("click", function () {
        var menu = document.getElementById("fixture-menu");
        var wasHidden = menu.hasAttribute("hidden");
        if (wasHidden) menu.removeAttribute("hidden");
        else menu.setAttribute("hidden", "");
        this.setAttribute("aria-expanded", wasHidden ? "true" : "false");
      });
      console.error("$console_error");
    </script>
  </body>
</html>
"""
).substitute(
    heading=_HEADING,
    paragraph=_PARAGRAPH,
    missing_image=_MISSING_IMAGE,
    console_error=_CONSOLE_ERROR,
)


def _cleanup_session_workdir(session_id: str) -> None:
    """Remove a seeded session's repo-root working directory.

    :param session_id: Session whose runner-cwd directory to remove.
    :returns: None.
    """
    shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


@pytest.fixture
def seeded_annotation_fixture(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed the annotation fixture page and yield ``(base_url, session_id, path)``.

    :param seeded_session: Runner-bound session fixture from ``conftest``.
    :returns: ``(base_url, session_id, relative_path)`` for the seeded page.
    """
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FIXTURE_PATH}",
        json={"content": _FIXTURE_HTML, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id, _FIXTURE_PATH)
    finally:
        _cleanup_session_workdir(session_id)


def _open_preview(page: Page, base_url: str, session_id: str) -> tuple[Locator, FrameLocator]:
    """Open the fixture file and return the viewer and preview-frame locators.

    :param page: Playwright page to drive.
    :param base_url: Spawned server base URL.
    :param session_id: Session whose workspace holds the seeded fixture.
    :returns: ``(file_viewer, preview)`` — the visible viewer and its iframe.
    """
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{base_url}/c/{session_id}?file={_FIXTURE_PATH}")
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    preview = file_viewer.frame_locator('iframe[title="HTML preview"]')
    expect(preview.locator("#fixture-heading")).to_have_text(_HEADING, timeout=15_000)
    return file_viewer, preview


def _annotate_toggle(file_viewer: Locator) -> Locator:
    """The preview's annotate toggle button.

    :param file_viewer: The visible file viewer.
    :returns: Locator for the ``aria-label="Annotate"`` button.
    """
    return file_viewer.get_by_role("button", name="Annotate")


def _enter_annotation_mode(file_viewer: Locator) -> None:
    """Wait for the frame's annotate stub, then turn the mode on via the button.

    :param file_viewer: The visible file viewer.
    :returns: None. The toggle ends ``aria-pressed="true"``.
    """
    toggle = _annotate_toggle(file_viewer)
    expect(toggle).to_be_visible(timeout=15_000)
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true", timeout=15_000)


def _pick_and_note(
    page: Page,
    preview: FrameLocator,
    selector: str,
    note: str,
    *,
    send: bool,
) -> None:
    """Pick an element in the frame, type a note in the owner composer, submit.

    :param page: Playwright page; the composer is owner UI on this page.
    :param preview: The preview frame locator (only the pick is in the frame).
    :param selector: CSS selector of the element to pick in the fixture page.
    :param note: Note text to type into the composer.
    :param send: When ``True`` press Enter (send); else ``ControlOrMeta+Enter``
        (stack).
    :returns: None.
    """
    preview.locator(selector).click()
    textarea = page.get_by_test_id("annotation-composer").locator("textarea")
    expect(textarea).to_be_focused(timeout=10_000)
    textarea.fill(note)
    textarea.press("Enter" if send else "ControlOrMeta+Enter")


def _record_message_posts(page: Page, session_id: str) -> list[str]:
    """Record user-message texts POSTed to the session's events endpoint.

    :param page: Playwright page to watch.
    :param session_id: Session whose message posts to record.
    :returns: The recorded ``input_text`` block texts, in POST order.
    """
    posts: list[str] = []

    def record(request: Request) -> None:
        if request.method != "POST":
            return
        if urlparse(request.url).path != f"/v1/sessions/{session_id}/events":
            return
        body = request.post_data_json
        if not isinstance(body, dict) or body.get("type") != "message":
            return
        for block in body.get("data", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "input_text":
                posts.append(str(block.get("text", "")))

    page.on("request", record)
    return posts


def _get_comments(base_url: str, session_id: str, file_path: str) -> list[dict]:
    """Read this session's comments for the fixture path.

    :param base_url: Spawned server base URL.
    :param session_id: Session owning the comments.
    :param file_path: Workspace-relative file the comments belong to.
    :returns: The serialized comment rows.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={file_path}",
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def _wait_for_comments(
    base_url: str,
    session_id: str,
    file_path: str,
    count: int,
    *,
    timeout: float = 30.0,
) -> list[dict]:
    """Poll the comments REST API until at least *count* rows exist.

    :param base_url: Spawned server base URL.
    :param session_id: Session owning the comments.
    :param file_path: Workspace-relative file the comments belong to.
    :param count: Minimum number of rows to wait for.
    :param timeout: Max seconds to wait.
    :returns: The comment rows once *count* exist.
    :raises AssertionError: If fewer than *count* rows exist at the deadline.
    """
    deadline = time.monotonic() + timeout
    comments: list[dict] = []
    while time.monotonic() < deadline:
        comments = _get_comments(base_url, session_id, file_path)
        if len(comments) >= count:
            return comments
        time.sleep(0.2)
    raise AssertionError(f"expected {count} comments, saw {len(comments)}: {comments}")


def _wait_for_addressed(
    base_url: str,
    session_id: str,
    file_path: str,
    count: int,
    *,
    timeout: float = 30.0,
) -> list[dict]:
    """Poll REST until exactly *count* comments all read ``addressed``.

    :param base_url: Spawned server base URL.
    :param session_id: Session owning the comments.
    :param file_path: Workspace-relative file the comments belong to.
    :param count: Exact number of rows expected.
    :param timeout: Max seconds to wait.
    :returns: The addressed comment rows.
    :raises AssertionError: If the rows never all reach ``addressed``.
    """
    deadline = time.monotonic() + timeout
    comments: list[dict] = []
    while time.monotonic() < deadline:
        comments = _get_comments(base_url, session_id, file_path)
        if len(comments) == count and all(c["status"] == "addressed" for c in comments):
            return comments
        time.sleep(0.2)
    statuses = [c.get("status") for c in comments]
    raise AssertionError(f"expected {count} addressed comments, saw {statuses}: {comments}")


def _decode_element_anchor(comment: dict) -> dict:
    """Decode the ``__element__`` JSON payload of a comment row.

    :param comment: A serialized comment row.
    :returns: The parsed anchor payload.
    """
    anchor_content = comment["anchor_content"]
    assert anchor_content.startswith(_ELEMENT_PREFIX), anchor_content
    return json.loads(anchor_content[len(_ELEMENT_PREFIX) :])


def _dead_anchor(anchor: dict) -> dict:
    """A valid element anchor whose selectors, tag and text resolve to nothing.

    :param anchor: A live anchor captured from a UI-created comment.
    :returns: A deep copy aimed at an element that does not exist.
    """
    dead = copy.deepcopy(anchor)
    dead["target"]["id"] = "fixture-removed-target"
    dead["target"]["css"] = "#fixture-removed-target"
    dead["target"]["xpath"] = "//*[@id='fixture-removed-target']"
    dead["target"]["tag"] = "nosuchtag"
    dead["target"]["fingerprint"] = "0:0:removed"
    dead["target"]["quote"] = {"exact": "no such fixture text", "prefix": "gone", "suffix": "gone"}
    dead["target"]["text"] = ""
    dead["target"]["neighborText"] = ""
    dead["screenshot"] = None
    return dead


def test_click_pick_stacks_an_element_annotation(
    page: Page,
    seeded_annotation_fixture: tuple[str, str, str],
) -> None:
    """T1/T8: a click-pick saves a draft anchor with diagnostics, nothing sent.

    :param page: Playwright page.
    :param seeded_annotation_fixture: ``(base_url, session_id, path)`` of the
        seeded fixture page.
    :returns: None.
    """
    base_url, session_id, file_path = seeded_annotation_fixture
    posts = _record_message_posts(page, session_id)
    file_viewer, preview = _open_preview(page, base_url, session_id)

    _enter_annotation_mode(file_viewer)
    note = "Cite a source for this summary."
    _pick_and_note(page, preview, "#fixture-paragraph", note, send=False)

    comments = _wait_for_comments(base_url, session_id, file_path, 1)
    assert len(comments) == 1, comments
    comment = comments[0]
    assert comment["status"] == "draft"
    assert comment["body"] == note
    assert "annotation" in comment, comment
    assert comment["annotation"]["kind"] == "element"

    anchor = _decode_element_anchor(comment)
    assert anchor["kind"] == "element"
    assert anchor["target"]["css"], anchor["target"]
    assert anchor["target"]["xpath"], anchor["target"]
    assert _PARAGRAPH[:30] in anchor["target"]["quote"]["exact"], anchor["target"]["quote"]
    assert anchor["rect"]["w"] > 0, anchor["rect"]
    assert any(_CONSOLE_ERROR in entry["message"] for entry in anchor["console"]), anchor[
        "console"
    ]
    assert any(entry["url"].endswith("/" + _MISSING_IMAGE) for entry in anchor["network"]), anchor[
        "network"
    ]
    assert posts == [], f"stacking must not send to chat: {posts}"


def test_open_dropdown_annotated_with_screenshot(
    page: Page,
    seeded_annotation_fixture: tuple[str, str, str],
) -> None:
    """T3 / acceptance 1: an open click-menu option is picked with its crop.

    The dropdown is opened by the page's own click handler before the mode is
    turned on, the chord is pressed with focus still inside the frame, and the
    second option is picked. After leaving the mode the menu must still be in
    the DOM state the page left it in: visible, with the toggle's
    ``aria-expanded`` still ``"true"`` (freeze only pins styles and must not
    close or remove the page's own menu).

    :param page: Playwright page.
    :param seeded_annotation_fixture: ``(base_url, session_id, path)`` of the
        seeded fixture page.
    :returns: None.
    """
    base_url, session_id, file_path = seeded_annotation_fixture
    file_viewer, preview = _open_preview(page, base_url, session_id)
    toggle = _annotate_toggle(file_viewer)
    expect(toggle).to_be_visible(timeout=15_000)

    preview.locator("#fixture-menu-toggle").click()
    expect(preview.locator("#fixture-menu")).to_be_visible()
    expect(preview.locator("#fixture-menu-toggle")).to_be_focused()

    page.keyboard.press(_DEFAULT_CHORD)
    expect(toggle).to_have_attribute("aria-pressed", "true", timeout=15_000)

    _pick_and_note(page, preview, "#fixture-option-2", "Annotate the second option.", send=False)

    comments = _wait_for_comments(base_url, session_id, file_path, 1)
    assert len(comments) == 1, comments
    anchor = _decode_element_anchor(comments[0])
    assert "button" in anchor["target"]["label"], anchor["target"]["label"]
    screenshot = anchor["screenshot"]
    assert screenshot is not None, anchor
    file_id = screenshot["file_id"]

    content = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/files/{file_id}/content",
        timeout=10.0,
    )
    assert content.status_code == 200, content.status_code
    assert content.headers["content-type"].startswith("image/jpeg"), content.headers

    # Leave the mode: the submit closed the pick, so the first Escape exits
    # (the second is inert) and the page's own menu state must survive.
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    expect(toggle).to_have_attribute("aria-pressed", "false", timeout=10_000)
    expect(preview.locator("#fixture-menu")).to_be_visible()
    expect(preview.locator("#fixture-menu-toggle")).to_have_attribute("aria-expanded", "true")


def test_reload_reanchors_and_orphan_is_reported(
    page: Page,
    seeded_annotation_fixture: tuple[str, str, str],
) -> None:
    """T4/T5 / acceptance 2: reload re-finds the live anchor, orphans the dead.

    One annotation is created through the UI and a second is seeded over REST
    from the first one's anchor with its id, CSS, XPath, tag and quote aimed at
    nothing on the page. After a reload the resolver must re-find the live one
    (no orphan marker on its card) and report only the dead one as orphaned.

    :param page: Playwright page.
    :param seeded_annotation_fixture: ``(base_url, session_id, path)`` of the
        seeded fixture page.
    :returns: None.
    """
    base_url, session_id, file_path = seeded_annotation_fixture
    file_viewer, preview = _open_preview(page, base_url, session_id)

    _enter_annotation_mode(file_viewer)
    live_note = "Keep this anchored across reloads."
    _pick_and_note(page, preview, "#fixture-paragraph", live_note, send=False)
    comments = _wait_for_comments(base_url, session_id, file_path, 1)
    live_anchor = _decode_element_anchor(comments[0])

    dead_note = "This target will be gone."
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": file_path,
            "body": dead_note,
            "start_index": comments[0]["start_index"],
            "end_index": comments[0]["end_index"],
            "anchor_content": _ELEMENT_PREFIX + json.dumps(_dead_anchor(live_anchor)),
        },
        timeout=10.0,
    )
    resp.raise_for_status()

    page.reload()
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible(timeout=30_000)
    preview = file_viewer.frame_locator('iframe[title="HTML preview"]')
    expect(preview.locator("#fixture-heading")).to_have_text(_HEADING, timeout=15_000)
    file_viewer.get_by_role("button", name="Show comments").click()
    expect(file_viewer.get_by_text(live_note)).to_be_visible(timeout=15_000)
    expect(file_viewer.get_by_text(dead_note)).to_be_visible()

    orphan = file_viewer.get_by_test_id("comment-orphan")
    expect(orphan).to_have_count(1, timeout=15_000)
    expect(orphan).to_be_visible()
    # The marker sits on the dead note's card, not the live one's.
    dead_card = orphan.locator(f'xpath=ancestor::*[contains(., "{dead_note}")][1]')
    expect(dead_card).to_be_visible()
    expect(dead_card.get_by_text(live_note)).to_have_count(0)


def test_send_batch_renders_cards(
    page: Page,
    seeded_annotation_fixture: tuple[str, str, str],
) -> None:
    """T2 / acceptance 3: three stacked picks send as one message with cards.

    The comments panel is kept open so each stacked note is visible in the
    client list before the next pick — the send batch reads that list, so this
    waits on real UI state instead of the query refetch racing the next pick.

    :param page: Playwright page.
    :param seeded_annotation_fixture: ``(base_url, session_id, path)`` of the
        seeded fixture page.
    :returns: None.
    """
    base_url, session_id, file_path = seeded_annotation_fixture
    posts = _record_message_posts(page, session_id)
    file_viewer, preview = _open_preview(page, base_url, session_id)
    file_viewer.get_by_role("button", name="Show comments").click()
    _enter_annotation_mode(file_viewer)

    notes = [
        "First target needs work.",
        "Second target needs work.",
        "Third target needs work.",
    ]
    targets = ["#fixture-paragraph", "#fixture-menu-toggle", "#fixture-close"]
    for index in range(2):
        _pick_and_note(page, preview, targets[index], notes[index], send=False)
        _wait_for_comments(base_url, session_id, file_path, index + 1)
        expect(file_viewer.get_by_text(notes[index])).to_be_visible(timeout=15_000)
    _pick_and_note(page, preview, targets[2], notes[2], send=True)

    cards = page.get_by_test_id("annotation-card")
    expect(cards).to_have_count(3, timeout=30_000)
    assert cards.locator('[data-testid="annotation-body"]').all_inner_texts() == notes
    labels = cards.locator('[data-testid="annotation-label"]').all_inner_texts()
    assert all(label.strip() for label in labels), labels

    comments = _wait_for_addressed(base_url, session_id, file_path, 3)
    ordered = sorted(comments, key=lambda c: c["start_index"])
    assert [c["body"] for c in ordered] == notes, ordered
    assert len(posts) == 1, posts


def test_rebinding_takes_effect_without_reload(
    page: Page,
    seeded_annotation_fixture: tuple[str, str, str],
) -> None:
    """T11 / acceptance 4: a Settings rebinding applies to a live preview.

    The binding is changed through the Settings shortcut editor (the only UI
    that writes shortcut preferences), then the app returns to the preview
    through client-side routing (the Back link) — a ``window`` marker proves
    the document was never reloaded. The old chord must be inert: if it still
    toggled, the second new-chord press below would flip the mode back on and
    the final ``aria-pressed="false"`` assertion would time out.

    :param page: Playwright page.
    :param seeded_annotation_fixture: ``(base_url, session_id, path)`` of the
        seeded fixture page.
    :returns: None.
    """
    base_url, session_id, _file_path = seeded_annotation_fixture
    file_viewer, _preview = _open_preview(page, base_url, session_id)
    toggle = _annotate_toggle(file_viewer)
    expect(toggle).to_be_visible(timeout=15_000)
    expect(toggle).to_have_attribute("aria-pressed", "false")

    # Survives SPA routing; a document reload would drop it.
    page.evaluate("() => { window.__omniE2ENoReload = true; }")

    page.keyboard.press("ControlOrMeta+Alt+Comma")
    expect(page.get_by_test_id("settings-nav-shortcuts")).to_be_visible(timeout=30_000)
    page.get_by_test_id("settings-nav-shortcuts").click()

    row = page.get_by_test_id("shortcut-editor-row-toggleAnnotationMode")
    expect(row).to_be_visible(timeout=30_000)
    row.scroll_into_view_if_needed()
    record = row.get_by_role("button", name="Record common shortcut for Toggle annotation mode")
    record.click()
    expect(record).to_have_attribute("aria-pressed", "true")
    page.keyboard.press(_NEW_CHORD)
    expect(record).to_have_attribute("aria-pressed", "false")
    stored = page.evaluate(f"() => localStorage.getItem({_SHORTCUTS_STORAGE_KEY!r})")
    assert stored is not None and "KeyY" in stored, stored

    page.get_by_role("link", name="Back", exact=True).click()
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible(timeout=30_000)
    toggle = _annotate_toggle(file_viewer)
    expect(toggle).to_be_visible(timeout=15_000)
    assert page.evaluate("() => window.__omniE2ENoReload === true"), "returning must not reload"

    preview = file_viewer.frame_locator('iframe[title="HTML preview"]')
    expect(preview.locator("#fixture-heading")).to_have_text(_HEADING, timeout=15_000)
    preview.locator("#fixture-paragraph").click()

    page.keyboard.press(_NEW_CHORD)
    expect(toggle).to_have_attribute("aria-pressed", "true", timeout=15_000)
    page.keyboard.press(_DEFAULT_CHORD)
    page.keyboard.press(_NEW_CHORD)
    expect(toggle).to_have_attribute("aria-pressed", "false", timeout=15_000)


def test_text_selection_comment_still_works_beside_annotation(
    page: Page,
    seeded_annotation_fixture: tuple[str, str, str],
) -> None:
    """T14: the text-selection "Add comment" entry still works beside the mode.

    :param page: Playwright page.
    :param seeded_annotation_fixture: ``(base_url, session_id, path)`` of the
        seeded fixture page.
    :returns: None.
    """
    base_url, session_id, file_path = seeded_annotation_fixture
    file_viewer, preview = _open_preview(page, base_url, session_id)
    toggle = _annotate_toggle(file_viewer)
    expect(toggle).to_be_visible(timeout=15_000)
    expect(toggle).to_have_attribute("aria-pressed", "false")

    preview.locator("#fixture-paragraph").select_text()
    add_comment_btn = page.get_by_role("button", name="Add comment")
    expect(add_comment_btn).to_be_visible(timeout=10_000)
    add_comment_btn.click()

    body = "Still available beside annotation mode."
    textarea = file_viewer.locator("textarea[placeholder='Add a comment…']")
    expect(textarea).to_be_visible()
    textarea.fill(body)
    file_viewer.get_by_role("button", name="Add Comment").click()
    expect(file_viewer).to_contain_text(body)

    comments = _wait_for_comments(base_url, session_id, file_path, 1)
    assert len(comments) == 1, comments
    comment = comments[0]
    assert comment["body"] == body
    assert comment["anchor_content"] == _PARAGRAPH
    assert not comment["anchor_content"].startswith(_ELEMENT_PREFIX)
    raw_index = _FIXTURE_HTML.find(_PARAGRAPH)
    assert raw_index != -1, "fixture bug: paragraph text missing from the page source"
    assert comment["start_index"] == raw_index
    assert comment["end_index"] == raw_index + len(_PARAGRAPH)
