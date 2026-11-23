"""An async question stays in place while its answer is edited in live chat."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item

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
    chat.set_items(
        [
            message_item("card-request", "user", "Explain the plan.", response_id="card-turn"),
            message_item("earlier-reply", "assistant", _TEXT * 5, response_id="earlier-turn"),
        ]
    )
    posted: list[dict] = []

    def resolve(request: Request) -> dict[str, bool]:
        posted.append(request.post_data_json)
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


def _expect_following(page: Page) -> None:
    page.wait_for_function(
        """selector => {
          const el = document.querySelector(selector);
          return el.scrollHeight - el.clientHeight - el.scrollTop <= 2;
        }""",
        arg=_SCROLLER,
    )
