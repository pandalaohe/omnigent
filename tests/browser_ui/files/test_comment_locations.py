"""Rendered comment creation and reload in the built SPA with a sealed backend."""

from __future__ import annotations

import json
import re
from dataclasses import asdict

import pytest
from playwright.sync_api import Page, Route, expect

from omnigent.entities import Comment
from omnigent.server.routes.artifacts import _inject_in_frame_assets
from omnigent.server.routes.comments import AddCommentRequest, _format_message
from tests.browser_ui.files.test_file_line_navigation import BrowserSession, _open_viewer
from tests.browser_ui.files.test_file_line_navigation import seeded_session as seeded_session


def _comments_backend(session: BrowserSession) -> list[str]:
    api = f"/v1/sessions/{session.session_id}"
    sent: list[str] = []

    def save(request):
        data = AddCommentRequest.model_validate(request.post_data_json).model_dump()
        row = asdict(
            Comment(
                id="saved-comment",
                conversation_id=session.session_id,
                status="draft",
                created_at=1,
                updated_at=1,
                **data,
            )
        )
        session.comments.append(row)
        return row

    def send(request):
        ids = request.post_data_json["comment_ids"]
        rows = [Comment(**row) for row in session.comments if row["id"] in ids]
        message = _format_message(rows)
        sent.append(message)
        return {"formatted_message": message, "sent_comment_ids": ids, "attachments": []}

    def mark(request):
        session.comments[0]["status"] = request.post_data_json["status"]
        return session.comments[0]

    session.contract.json(f"{api}/comments", save, method="POST")
    session.contract.json(f"{api}/comments/send", send, method="POST")
    session.contract.json(f"{api}/comments/saved-comment", mark, method="PATCH")
    session.contract.json(
        f"{api}/events",
        {"queued": True, "forwarded": True, "item_id": "review-turn"},
        method="POST",
        status=202,
    )
    return sent


def _file(session: BrowserSession, path: str, content: str, content_type: str) -> None:
    session.contract.json(
        f"/v1/sessions/{session.session_id}/resources/environments/default/filesystem/{path}",
        {
            "object": "session.environment.filesystem.file_content",
            "path": path,
            "content": content,
            "encoding": "utf-8",
            "content_type": content_type,
            "bytes": len(content),
            "truncated": False,
        },
    )


def _save(page: Page, viewer, session: BrowserSession) -> dict:
    page.get_by_role("button", name="Add comment", exact=True).click()
    viewer.locator("textarea[placeholder='Add a comment…']").fill("Change the second selection")
    viewer.get_by_role("button", name="Add Comment", exact=True).click()
    expect(viewer).to_contain_text("Change the second selection")
    assert len(session.comments) == 1
    return json.loads(session.comments[0]["anchor_content"][8:])


def _html_viewer(page: Page, session: BrowserSession, markup: str):
    path, nonce = "reports/preview.html", "browser-location"
    source = (
        '<!doctype html><html><body><main id="root"></main><script>'
        'document.getElementById("root").innerHTML='
        + json.dumps(markup)
        + ";</script></body></html>"
    )
    _file(session, path, source, "text/html")
    sent = _comments_backend(session)
    session.contract.json(
        f"/v1/sessions/{session.session_id}/artifacts",
        {
            "url": "/v1/artifacts/preview/preview.html",
            "nonce": nonce,
            "kind": "bundle",
            "expires_at": None,
        },
        method="POST",
    )

    def artifact(route: Route) -> None:
        body = _inject_in_frame_assets(source.encode(), nonce)
        assert body is not None
        route.fulfill(content_type="text/html", body=body)

    session.contract.route(
        session.contract.base_url + "/v1/artifacts/preview/preview.html", artifact
    )
    viewer = _open_viewer(page, session, path, {})
    return viewer, sent


@pytest.mark.parametrize(
    "selected,markup",
    [
        ("Code", '<p id="first">Alpha Code</p><p id="second">Beta Code</p>'),
        ("A & B", '<p id="first">Alpha A &amp; B</p><p id="second">Beta A &amp; B</p>'),
        (
            "A bold B",
            '<p id="first">Alpha A <b>bold</b> B</p><p id="second">Beta A <b>bold</b> B</p>',
        ),
    ],
)
def test_dynamic_html_second_selection_survives_reload_and_send(
    page: Page, seeded_session: BrowserSession, selected: str, markup: str
) -> None:
    session = seeded_session
    viewer, sent = _html_viewer(page, session, markup)
    frame = viewer.frame_locator('iframe[title="HTML preview"]')
    expect(frame.locator("#second")).to_contain_text(selected)
    frame.locator("#second").evaluate(
        """(el, text) => {
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      const nodes = []; let n, flat = '';
      while ((n = walker.nextNode())) {
        nodes.push({node:n,start:flat.length}); flat += n.nodeValue;
      }
      const at = flat.indexOf(text), end = at + text.length;
      const a = nodes.find(n => n.start + n.node.nodeValue.length > at);
      const b = nodes.find(n => n.start + n.node.nodeValue.length >= end);
      const r = document.createRange();
      r.setStart(a.node, at-a.start); r.setEnd(b.node, end-b.start);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
      document.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
    }""",
        selected,
    )
    payload = _save(page, viewer, session)
    assert payload["kind"] == "html" and payload["occ"] == 1
    assert payload["text"] == selected and "Beta" in payload["prefix"]
    assert payload["selector"]
    page.reload()
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    frame = viewer.frame_locator('iframe[title="HTML preview"]')
    expect(frame.locator("#second")).to_contain_text(selected)
    frame.locator("#second").evaluate("""async el => {
      for (let i=0;i<100;i++) {
        const ranges = Array.from(CSS.highlights.get('omni-comment') || []);
        if (ranges.length === 1 && el.contains(ranges[0].startContainer)) return;
        await new Promise(requestAnimationFrame);
      }
      throw new Error('Saved highlight did not resolve exclusively to the second element');
    }""")
    show = viewer.get_by_role("button", name="Show comments", exact=True)
    if show.is_visible():
        show.click()
    with page.expect_response(
        lambda r: r.url.endswith("/comments/send") and r.request.method == "POST"
    ):
        viewer.get_by_role("button", name=re.compile("^Address all$", re.IGNORECASE)).click()
    assert sent and "occurrence 2" in sent[0] and "Beta" in sent[0]
    assert "characters 0–0" not in sent[0]


def test_markdown_second_code_survives_reload(page: Page, seeded_session: BrowserSession) -> None:
    session = seeded_session
    path = "reports/layout.md"
    source = "[Intro](https://example.test/" + "a" * 1000 + ")\n\nCode\n\nCode"
    _file(session, path, source, "text/markdown")
    _comments_backend(session)
    viewer = _open_viewer(page, session, path, {})
    editor = viewer.locator(".tiptap")
    expect(editor).to_be_visible()
    editor.locator("p").nth(2).select_text()
    payload = _save(page, viewer, session)
    assert payload["kind"] == "markdown" and payload["occ"] == 1 and payload["text"] == "Code"
    page.reload()
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    editor = viewer.locator(".tiptap")
    expect(editor.locator("p").nth(2).locator(".md-comment")).to_have_text("Code")
    expect(editor.locator("p").nth(1).locator(".md-comment")).to_have_count(0)


def test_html_selection_across_paragraphs_can_be_saved_and_reloaded(
    page: Page, seeded_session: BrowserSession
) -> None:
    viewer, _sent = _html_viewer(page, seeded_session, "<p>Alpha</p><p>Beta</p>")
    frame = viewer.frame_locator('iframe[title="HTML preview"]')
    expect(frame.locator("#root p")).to_have_count(2)
    frame.locator("#root").select_text()
    payload = _save(page, viewer, seeded_session)
    assert payload["text"] == "Alpha\n\nBeta" and payload["occ"] == 0
    page.reload()
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    frame = viewer.frame_locator('iframe[title="HTML preview"]')
    frame.locator("#root").evaluate("""async el => {
      for (let i=0;i<100;i++) {
        const ranges = Array.from(CSS.highlights.get('omni-comment') || []);
        if (ranges.length === 1 && ranges[0].toString() === 'AlphaBeta') return;
        await new Promise(requestAnimationFrame);
      }
      throw new Error('Saved cross-paragraph range was not restored');
    }""")


def test_legacy_unknown_and_whole_page_comments_keep_distinct_orphan_state(
    page: Page, seeded_session: BrowserSession
) -> None:
    session = seeded_session
    for comment_id, anchor, body, author in [
        ("legacy", "Code", "Legacy comment", None),
        ("whole", None, "Whole page comment", "visitor:Reviewer"),
    ]:
        session.comments.append(
            asdict(
                Comment(
                    id=comment_id,
                    conversation_id=session.session_id,
                    path="reports/preview.html",
                    body=body,
                    start_index=0,
                    end_index=0,
                    status="draft",
                    created_at=1,
                    updated_at=1,
                    anchor_content=anchor,
                    created_by=author,
                )
            )
        )
    viewer, _sent = _html_viewer(page, session, "<p>Alpha Code</p><p>Beta Code</p>")
    frame = viewer.frame_locator('iframe[title="HTML preview"]')
    expect(frame.locator("#root p")).to_have_count(2)
    viewer.get_by_role("button", name="Show comments", exact=True).click()
    expect(viewer.get_by_text("Legacy comment", exact=True)).to_be_visible()
    expect(viewer.get_by_text("Whole page comment", exact=True)).to_be_visible()
    expect(
        viewer.get_by_text("Original location cannot be confirmed on this page", exact=True)
    ).to_have_count(1)
    assert (
        frame.locator("body").evaluate(
            "() => Array.from(CSS.highlights.get('omni-comment') || []).length"
        )
        == 0
    )


@pytest.mark.parametrize("hidden", ["hidden", 'style="display:none"', 'style="visibility:hidden"'])
def test_html_hidden_text_stays_out_of_saved_quote_and_reloaded_selection(
    page: Page, seeded_session: BrowserSession, hidden: str
) -> None:
    viewer, _sent = _html_viewer(
        page, seeded_session, f'<div id="selected">Alpha<span {hidden}>UNSEEN</span>Beta</div>'
    )
    frame = viewer.frame_locator('iframe[title="HTML preview"]')
    target = frame.locator("#selected")
    expect(target).to_be_visible()
    target.select_text()
    assert target.evaluate("() => getSelection().toString()") == "AlphaBeta"
    payload = _save(page, viewer, seeded_session)
    assert payload["text"] == "AlphaBeta"
    assert "UNSEEN" not in json.dumps(payload)
    page.reload()
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    frame = viewer.frame_locator('iframe[title="HTML preview"]')
    frame.locator("#selected").evaluate("""async el => {
      for (let i=0;i<100;i++) {
        const ranges = Array.from(CSS.highlights.get('omni-comment') || []);
        if (ranges.length === 1) {
          const selection = getSelection();
          selection.removeAllRanges(); selection.addRange(ranges[0]);
          const text = selection.toString(); selection.removeAllRanges();
          if (text === 'AlphaBeta') return;
        }
        await new Promise(requestAnimationFrame);
      }
      throw new Error('Saved visible selection was not restored');
    }""")
