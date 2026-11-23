"""An async question stays in place while its answer is edited in live chat."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Request, Route, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, list_payload, message_item

_SCROLLER = '[role="log"] > div'
_TEXT = "Streaming text with enough words to wrap in the transcript. " * 20
_QUESTION = "Describe the change."


def _question(chat: ChatSessionContract, card_id: str, question: str) -> None:
    chat.emit(
        {
            "event": "response.elicitation_request",
            "data": {
                "elicitation_id": card_id,
                "params": {
                    "mode": "form",
                    "message": question,
                    "phase": "async_question",
                    "policy_name": "claude_async_question",
                    "async_kind": "question",
                    "ask_user_question": {
                        "questions": [
                            {
                                "question": question,
                                "options": [{"label": "Use the default", "description": ""}],
                            }
                        ]
                    },
                },
            },
        }
    )


@pytest.mark.parametrize(
    "width",
    [
        1280,
        pytest.param(390, marks=pytest.mark.browser_context_args(has_touch=True, is_mobile=True)),
    ],
)
def test_editing_card_holds_position_and_reveals_the_caret(
    page: Page, chat_session_contract: ChatSessionContract, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    chat = chat_session_contract
    chat.harness = "claude-native"
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/host-terminals", {"terminals": [], "complete": True}
    )
    chat.set_items(
        [
            message_item("card-request", "user", "Explain the plan.", response_id="card-turn"),
            message_item("earlier-reply", "assistant", _TEXT * 5, response_id="earlier-turn"),
        ]
    )
    posted: list[dict] = []

    def resolve(request: Request) -> dict[str, bool]:
        payload = request.post_data_json
        assert payload is not None
        posted.append(payload)
        return {"queued": True}

    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/elicitations/card/resolve", resolve, method="POST"
    )
    page.goto(chat.url)
    chat.wait_for_stream()
    chat.emit_busy("card-turn")
    chat.emit({"event": "response.created", "data": {"id": "card-turn", "status": "in_progress"}})
    _question(chat, "card", _QUESTION)

    card = page.get_by_test_id("bottom-elicitation").filter(has_text=_QUESTION)
    field = card.get_by_role("textbox", name="Custom answer")
    scroller = page.locator(_SCROLLER)
    expect(field).to_be_visible()
    _expect_following(page)
    field.focus()
    expect(scroller).to_have_attribute("data-question-card-scroll-held", "")
    top = card.evaluate("el => el.getBoundingClientRect().top")
    chat.emit({"event": "response.output_text.delta", "data": {"delta": f"First update {_TEXT}"}})
    expect(page.get_by_text("First update", exact=False).first).to_be_attached()
    page.wait_for_function(
        """top => Math.abs(document.querySelector('[data-testid=bottom-elicitation]')
          .getBoundingClientRect().top - top) <= 2""",
        arg=top,
    )

    if width == 390:
        # Headless Chromium does not raise a native keyboard. Model its visual
        # viewport resize while leaving the layout viewport and composer intact.
        page.evaluate("""() => {
          document.documentElement.style.setProperty('--omnigent-android-safe-area-top', '16px');
          Object.defineProperty(visualViewport, 'height', { configurable: true, value: 480 });
          visualViewport.dispatchEvent(new Event('resize'));
        }""")
    answer = "\n".join(f"Line {line} of the answer" for line in range(55))
    page.keyboard.insert_text(answer)
    expect(field).to_have_value(answer)
    # The caret is at the end of a growing textarea. Its last line is the
    # textarea's content-box bottom, independent of the app's measurement code.
    page.wait_for_function("""() => {
      const field = document.activeElement;
      if (!(field instanceof HTMLTextAreaElement) || field.selectionEnd !== field.value.length)
        return false;
      const style = getComputedStyle(field);
      const rect = field.getBoundingClientRect();
      const lineBottom = rect.bottom - parseFloat(style.paddingBottom) - field.clientTop;
      const visibleBottom = Math.min(
        visualViewport.offsetTop + visualViewport.height,
        document.querySelector('.chat-composer-form').getBoundingClientRect().top,
        document.querySelector('[role=log] > div').getBoundingClientRect().bottom,
      );
      return lineBottom <= visibleBottom - 4 && lineBottom > visibleBottom - 80;
    }""")

    if width == 390:
        # Every line fits without wrapping at this width. Move from the last
        # line to an earlier one and verify its position against the CSS mask.
        field.evaluate("""el => {
          const index = el.value.indexOf('Line 10 ');
          el.setSelectionRange(index, index);
        }""")
        page.wait_for_function("""() => {
          const field = document.activeElement;
          const style = getComputedStyle(field);
          const lineHeight = parseFloat(style.lineHeight);
          const lineTop = field.getBoundingClientRect().top + field.clientTop
            + parseFloat(style.paddingTop) + 10 * lineHeight - field.scrollTop;
          const log = document.querySelector('[role=log]');
          return lineTop >= log.getBoundingClientRect().top + 16 + 84
            && lineTop + lineHeight < visualViewport.height - 8;
        }""")

    field.blur()
    expect(scroller).to_have_attribute("data-question-card-scroll-held", "")
    top = card.evaluate("el => el.getBoundingClientRect().top")
    # A second pending card grows the end of the transcript below the draft.
    _question(chat, "later-card", "A later question")
    expect(page.get_by_text("A later question", exact=True)).to_be_attached()
    page.wait_for_function(
        """top => Math.abs(document.querySelector('[data-testid=bottom-elicitation]')
          .getBoundingClientRect().top - top) <= 2""",
        arg=top,
    )

    if width == 390:
        page.evaluate("""() => {
          delete visualViewport.height;
          visualViewport.dispatchEvent(new Event('resize'));
        }""")
    field.focus()
    field.fill("")
    field.blur()
    expect(scroller).not_to_have_attribute("data-question-card-scroll-held", "")
    _expect_following(page)

    field.fill("Ready to submit")
    card.get_by_role("button", name="Submit", exact=True).click()
    expect(scroller).not_to_have_attribute("data-question-card-scroll-held", "")
    assert [verdict["content"] for verdict in posted] == [{_QUESTION: "Ready to submit"}]
    _expect_following(page)


def test_draft_yields_to_history_navigation_and_prepend(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    page.set_viewport_size({"width": 1280, "height": 844})
    chat = chat_session_contract
    chat.harness = "claude-native"
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/host-terminals", {"terminals": [], "complete": True}
    )
    current = [
        message_item("card-request", "user", "Explain the plan.", response_id="card-turn"),
        message_item("earlier-reply", "assistant", _TEXT * 10, response_id="earlier-turn"),
        message_item("earlier-request", "user", "Earlier request", response_id="earlier-turn"),
    ]
    older = [message_item("older-request", "user", "Previous page", response_id="older-turn")]
    pending: list[Route] = []

    def items(route: Route) -> None:
        if "after" in parse_qs(urlparse(route.request.url).query):
            pending.append(route)
        else:
            route.fulfill(json={**list_payload(current), "has_more": True})

    chat.contract.route(
        re.compile(rf"/v1/sessions/{re.escape(chat.session_id)}/items(?:\?.*)?$"), items
    )
    page.goto(chat.url)
    chat.wait_for_stream()
    chat.emit_busy("card-turn")
    chat.emit({"event": "response.created", "data": {"id": "card-turn", "status": "in_progress"}})
    _question(chat, "card", _QUESTION)

    scroller = page.locator(_SCROLLER)
    field = page.get_by_test_id("bottom-elicitation").get_by_role("textbox", name="Custom answer")
    field.fill("Keep this draft")
    field.blur()
    expect(scroller).to_have_attribute("data-question-card-scroll-held", "")
    page.get_by_role("button", name="Jump to: Earlier request").click()
    expect(scroller).not_to_have_attribute("data-question-card-scroll-held", "")
    earlier = page.locator('[data-user-message-id="earlier-request"]')
    expect(earlier).to_be_in_viewport()

    # A wheel-up at the top requests the previous page. Keep it in flight so
    # we can verify growth below the reader and the prepend independently.
    page.mouse.move(650, 300)
    page.mouse.wheel(0, -2000)
    expect(page.get_by_text("Loading earlier messages…")).to_be_visible()
    expect(earlier).to_be_in_viewport()
    top = earlier.evaluate("el => el.getBoundingClientRect().top")
    try:
        chat.emit(
            {"event": "response.output_text.delta", "data": {"delta": f"More output {_TEXT}"}}
        )
        expect(page.get_by_text("More output", exact=False).first).to_be_attached()
        page.wait_for_function("""() => new Promise(resolve => requestAnimationFrame(
          () => requestAnimationFrame(() => resolve(true))))""")
        assert earlier.evaluate("el => el.getBoundingClientRect().top") == pytest.approx(
            top, abs=2
        )

        pending.pop().fulfill(json=list_payload(older))
        expect(page.get_by_text("Previous page", exact=True)).to_be_attached()
        assert earlier.evaluate("el => el.getBoundingClientRect().top") == pytest.approx(
            top, abs=2
        )
    finally:
        for route in pending:
            route.abort()

    # Return to the same draft and confirm that editing takes over again.
    field.scroll_into_view_if_needed()
    field.focus()
    expect(field).to_have_value("Keep this draft")
    expect(scroller).to_have_attribute("data-question-card-scroll-held", "")
    top = field.evaluate("el => el.getBoundingClientRect().top")
    chat.emit({"event": "response.output_text.delta", "data": {"delta": f"Again {_TEXT}"}})
    expect(page.get_by_text("Again", exact=False).first).to_be_attached()
    assert field.evaluate("el => el.getBoundingClientRect().top") == pytest.approx(top, abs=2)

    page.mouse.wheel(0, -300)
    expect(scroller).not_to_have_attribute("data-question-card-scroll-held", "")
    expect(field).to_have_value("Keep this draft")
    page.keyboard.insert_text(".")
    expect(scroller).to_have_attribute("data-question-card-scroll-held", "")
    card_form = page.get_by_test_id("ask-user-question-form")
    card_form.press("ArrowDown")  # The card uses this to move to the next option.
    expect(scroller).to_have_attribute("data-question-card-scroll-held", "")
    card_form.press("PageUp")
    expect(scroller).not_to_have_attribute("data-question-card-scroll-held", "")


def _expect_following(page: Page) -> None:
    page.wait_for_function(
        """selector => {
          const el = document.querySelector(selector);
          return el.scrollHeight - el.clientHeight - el.scrollTop <= 2;
        }""",
        arg=_SCROLLER,
    )
