"""Answer one async card and retain one transcript record live and after reload."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item


@pytest.mark.parametrize("harness", ["claude-native", "codex-native"])
@pytest.mark.parametrize("answer", ["SQLite", "First paragraph\n\nSecond paragraph with A → B"])
def test_async_question_answer_echo_is_not_a_second_record(
    page: Page, chat_session_contract: ChatSessionContract, harness: str, answer: str
) -> None:
    chat = chat_session_contract
    chat.harness = harness
    question = "Choose a library."
    elicitation_id = "elicit_abcdef123456"
    posted: list[dict] = []

    def resolve(request: Request) -> dict[str, bool]:
        verdict = request.post_data_json
        posted.append(verdict)
        receipt = "[System: Question answered]\n" + json.dumps(
            {
                "elicitation_id": elicitation_id,
                "action": "accept",
                "answers": [{"question": question, "answer": verdict["content"][question]}],
            }
        )
        echo = f"[System: answers to your question card #qabcdef]\n{question} → {answer}\n"
        items = [
            message_item("answer-receipt", "user", receipt, response_id="receipt-response"),
            message_item("answer-echo", "user", echo, response_id="echo-response"),
        ]
        chat.set_items(list(reversed(items)))
        for item in items:
            chat.emit(
                {
                    "event": "session.input.consumed",
                    "data": {
                        "type": "session.input.consumed",
                        "data": {
                            "item_id": item["id"],
                            "type": "message",
                            "data": {
                                "role": "user",
                                "user_authored": True,
                                "content": item["content"],
                            },
                        },
                    },
                }
            )
        return {"queued": True}

    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/elicitations/{elicitation_id}/resolve",
        resolve,
        method="POST",
    )
    page.goto(chat.url)
    chat.wait_for_stream()
    chat.emit(
        {
            "event": "response.elicitation_request",
            "data": {
                "elicitation_id": elicitation_id,
                "params": {
                    "mode": "form",
                    "message": "Choose a library.",
                    "phase": "pre_tool_use",
                    "policy_name": "permission",
                    "ask_user_question": {
                        "questions": [
                            {
                                "question": question,
                                "header": "Library",
                                "options": [{"label": "SQLite", "description": "Use SQLite."}],
                            }
                        ]
                    },
                },
            },
        }
    )
    if answer == "SQLite":
        page.get_by_role("radio").first.check()
    else:
        page.get_by_placeholder("Type something", exact=True).fill(answer)
    page.get_by_role("button", name="Submit", exact=True).click()

    record = page.locator('[data-system-kind="question_answer"]')
    expect(record).to_have_count(1)
    expect(record).to_contain_text(question)
    expect(record).to_contain_text(answer)
    assert posted == [{"action": "accept", "content": {question: answer}}]
    page.reload()
    chat.wait_for_stream()
    expect(record).to_have_count(1)
    expect(record).to_contain_text(question)
    expect(record).to_contain_text(answer)
