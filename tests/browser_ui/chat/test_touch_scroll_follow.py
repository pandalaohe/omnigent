"""Touch scrolling and approval taps against the built, streaming chat UI."""

from __future__ import annotations

from contextlib import suppress

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item

_SCROLLER = '[role="log"] > div'
_TEXT = "Streaming paragraph with enough words to wrap on a phone. " * 12


def _open_stream(page: Page, chat: ChatSessionContract, harness: str) -> None:
    chat.harness = harness
    chat.set_items(
        [
            message_item("new-request", "user", "Tell a long story.", response_id="touch-turn"),
            message_item("old-reply", "assistant", _TEXT * 8, response_id="old-turn"),
        ]
    )
    page.goto(chat.url)
    expect(page.locator(_SCROLLER)).to_be_visible(timeout=20_000)
    chat.wait_for_stream()
    chat.emit_busy()
    chat.emit({"event": "response.created", "data": {"id": "touch-turn", "status": "in_progress"}})
    _grow(page, chat, "First live paragraph")
    page.locator(_SCROLLER).evaluate("el => { el.scrollTop = el.scrollHeight; }")
    page.wait_for_timeout(150)


def _grow(page: Page, chat: ChatSessionContract, marker: str) -> None:
    chat.emit({"event": "response.output_text.delta", "data": {"delta": f"\n\n{marker} {_TEXT}"}})
    expect(page.get_by_text(marker, exact=False).last).to_be_attached()
    page.wait_for_timeout(80)


@pytest.mark.browser_context_args(
    viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
)
def test_upward_touch_keeps_stream_from_pulling_reader_down(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    chat = chat_session_contract
    _open_stream(page, chat, "codex-native")
    scroller = page.locator(_SCROLLER)
    before = scroller.evaluate("el => el.scrollTop")
    scroller.evaluate("""el => {
        let growth = 0;
        const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
        window.touchDownWrites = [];
        window.trackTouchWrites = false;
        Object.defineProperty(el, 'scrollTop', {
            get() { return descriptor.get.call(this); },
            set(value) {
                const before = descriptor.get.call(this);
                descriptor.set.call(this, value);
                if (window.trackTouchWrites && value > before + 2)
                    window.touchDownWrites.push([before, value]);
            }
        });
        window.stopTouchGrowth = false;
        const tick = () => {
            if (window.stopTouchGrowth) return;
            growth += 30;
            el.firstElementChild.style.paddingBottom = `${growth}px`;
            requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
    }""")
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [{"x": 190, "y": 250}]},
        )
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [{"x": 190, "y": 300}]},
        )
        page.evaluate("window.trackTouchWrites = true")
        _grow(page, chat, "Growth during first touch move")
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [{"x": 190, "y": 450}]},
        )
        _grow(page, chat, "Growth during second touch move")
        assert page.evaluate("window.touchDownWrites") == []
        assert scroller.evaluate("el => el.scrollTop") < before - 50
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(200)
        held = scroller.evaluate("el => el.scrollTop")
        _grow(page, chat, "Growth while reading history")
        assert scroller.evaluate("el => el.scrollTop") <= held + 2
        page.evaluate("window.stopTouchGrowth = true; window.trackTouchWrites = false")
        page.locator('[role="log"] > button').tap()
        page.wait_for_timeout(150)
        _grow(page, chat, "Growth after returning to bottom")
        assert scroller.evaluate("el => el.scrollHeight - el.clientHeight - el.scrollTop") <= 2
    finally:
        page.evaluate("window.stopTouchGrowth = true")
        with suppress(Exception):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchCancel", "touchPoints": []})
        cdp.detach()


@pytest.mark.parametrize("harness", ["codex-native", "claude-native"])
@pytest.mark.parametrize("card_kind", ["approval", "question", "question-form"])
@pytest.mark.browser_context_args(
    viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
)
def test_one_approval_tap_while_reply_streams(
    page: Page, chat_session_contract: ChatSessionContract, harness: str, card_kind: str
) -> None:
    chat = chat_session_contract
    _open_stream(page, chat, harness)
    resolved: list[dict] = []
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/elicitations/touch-approval/resolve",
        lambda request: resolved.append(request.post_data_json) or {"queued": True},
        method="POST",
    )
    wrong_verdicts: list[dict] = []
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/elicitations/next-approval/resolve",
        lambda request: wrong_verdicts.append(request.post_data_json) or {"queued": True},
        method="POST",
    )
    chat.emit(
        {
            "event": "response.elicitation_request",
            "data": {
                "elicitation_id": "touch-approval",
                "params": {
                    "mode": "form",
                    "message": "Approve this operation?",
                    "phase": "pre_tool_use",
                    "policy_name": "permission",
                    "requestedSchema": {
                        "properties": {"answer": {"enum": ["First choice", "Second choice"]}}
                    }
                    if card_kind == "question"
                    else {},
                    "ask_user_question": {
                        "questions": [
                            {
                                "id": "route-choice",
                                "question": "Choose a route.",
                                "header": "Route",
                                "options": [
                                    {"label": "First choice", "description": "Take this route."},
                                    {
                                        "label": "Second choice",
                                        "description": "Take another route.",
                                    },
                                ],
                            }
                        ]
                    }
                    if card_kind == "question-form"
                    else None,
                },
            },
        }
    )
    if card_kind == "question-form":
        page.get_by_role("radio").first.tap()
    approve = page.get_by_role(
        "button",
        name={"question": "First choice", "question-form": "Submit", "approval": "Approve"}[
            card_kind
        ],
        exact=True,
    )
    expect(approve).to_be_visible()
    page.wait_for_timeout(150)
    box = approve.bounding_box()
    assert box is not None
    point = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
        _grow(page, chat, "Growth during approval tap")
        chat.emit(
            {
                "event": "response.elicitation_request",
                "data": {
                    "elicitation_id": "next-approval",
                    "params": {
                        "mode": "form",
                        "message": "A second operation needs approval.",
                        "phase": "pre_tool_use",
                        "policy_name": "permission",
                    },
                },
            }
        )
        expect(page.get_by_text("A second operation needs approval.", exact=True)).to_be_attached()
        page.wait_for_timeout(80)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        expect(
            page.locator('[data-testid="approval-card"][data-state="responded"]')
        ).to_contain_text("Approved" if card_kind == "approval" else "First choice")
        expected = {"action": "accept"}
        if card_kind == "question":
            expected["content"] = {"answer": "First choice"}
        elif card_kind == "question-form":
            expected["content"] = {"route-choice": "First choice"}
        assert resolved == [expected]
        assert wrong_verdicts == []
    finally:
        with suppress(Exception):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchCancel", "touchPoints": []})
        cdp.detach()
