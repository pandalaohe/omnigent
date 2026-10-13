"""Async-question reply-helper tests for Codex forwarder."""

from __future__ import annotations

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd

# ---------------------------------------------------------------------------
# Codex async question reply helpers
# ---------------------------------------------------------------------------


def test_question_item_id_is_the_codex_json_array() -> None:
    """The id Codex matches replies by is the compact JSON array string."""
    assert (
        fwd.question_item_id("call_5q2o9DeiiNfGFtf6rNXgPIVy", 0)
        == '["request_user_input_async","call_5q2o9DeiiNfGFtf6rNXgPIVy",0]'
    )


def test_build_question_reply_matches_the_codex_tui_bytes() -> None:
    """The reply bytes match the TUI's own reply in the 2026-09-24 rollout."""
    reply = fwd.build_question_reply(
        [
            (
                "我在cli中看到了shift+左是有一个选择卡",
                "选择卡显示测试：你能看到这张可点击的单选卡吗？",
                fwd.question_item_id("call_5q2o9DeiiNfGFtf6rNXgPIVy", 0),
            )
        ]
    )
    assert reply == (
        "<send_user_message_question_reply>\n"
        '[{"answer":"我在cli中看到了shift+左是有一个选择卡",'
        '"question":"选择卡显示测试：你能看到这张可点击的单选卡吗？",'
        '"questionItemId":"[\\"request_user_input_async\\",'
        '\\"call_5q2o9DeiiNfGFtf6rNXgPIVy\\",0]"}]\n'
        "</send_user_message_question_reply>"
    )


def test_parse_question_reply_round_trips_entries() -> None:
    """Every built reply parses back into its (call, question, index, answer)."""
    reply = fwd.build_question_reply(
        [
            ("A", "问题一", fwd.question_item_id("call_a", 0)),
            ("B", "问题二", fwd.question_item_id("call_b", 3)),
        ]
    )

    assert fwd.parse_question_reply(reply) == [
        ("call_a", "问题一", 0, "A"),
        ("call_b", "问题二", 3, "B"),
    ]


def test_parse_question_reply_accepts_array_item_id() -> None:
    reply = (
        '<send_user_message_question_reply>\n[{"answer":"First line\\nSecond line",'
        '"question":"What next?","questionItemId":["request_user_input_async","call_a",0]}]'
        "\n</send_user_message_question_reply>"
    )
    assert fwd.parse_question_reply(reply) == [
        ("call_a", "What next?", 0, "First line\nSecond line")
    ]


@pytest.mark.parametrize(
    "text",
    [
        "plain text",
        "<send_user_message_question_reply>[]</send_user_message_question_reply>",
        "<send_user_message_question_reply>\nnot json\n</send_user_message_question_reply>",
        "<send_user_message_question_reply>\n{}\n</send_user_message_question_reply>",
        "<send_user_message_question_reply>\n[]\n</send_user_message_question_reply>\nprose",
        (
            '<send_user_message_question_reply>\n[{"question":"Q",'
            '"questionItemId":"[\\"request_user_input_async\\",\\"call_a\\",0]"}]'
            "\n</send_user_message_question_reply>"
        ),
        (
            "<send_user_message_question_reply>\n"
            '[{"answer":"A","question":"Q","questionItemId":"not json"}]\n'
            "</send_user_message_question_reply>"
        ),
        (
            "<send_user_message_question_reply>\n"
            '[{"answer":"A","question":"Q",'
            '"questionItemId":"[\\"other_tool\\",\\"call_a\\",0]"}]\n'
            "</send_user_message_question_reply>"
        ),
        (
            "<send_user_message_question_reply>\n"
            '[{"answer":"A","question":"Q",'
            '"questionItemId":"[\\"request_user_input_async\\",\\"call_a\\",true]"}]\n'
            "</send_user_message_question_reply>"
        ),
    ],
)
def test_parse_question_reply_rejects_non_replies(text: str) -> None:
    """Anything but one clean tag pair around valid entries is not a reply."""
    assert fwd.parse_question_reply(text) is None


def test_async_questions_from_item_reads_live_async_calls() -> None:
    """A live async agentMessage yields one (title, options) per question."""
    item = {
        "type": "agentMessage",
        "id": "call_q1",
        "delivery": "async",
        "questions": [
            {"title": "选择卡显示测试", "options": ["看到了", "没看到"]},
            {"title": "自定义", "options": None},
        ],
    }

    assert fwd.async_questions_from_item(item) == [
        ("选择卡显示测试", ["看到了", "没看到"]),
        ("自定义", None),
    ]


@pytest.mark.parametrize(
    "item",
    [
        {"type": "agentMessage", "id": "call_q1", "questions": [{"title": "Q"}]},
        {"type": "agentMessage", "id": "call_q1", "delivery": "async", "questions": []},
        {"type": "agentMessage", "id": "call_q1", "delivery": "async", "questions": [{}]},
        {
            "type": "agentMessage",
            "id": "call_q1",
            "delivery": "async",
            "questions": [{"title": "", "options": ["a"]}],
        },
        {
            "type": "agentMessage",
            "id": "call_q1",
            "delivery": "async",
            "questions": [{"title": "Q", "options": ["a", 2]}],
        },
        {
            "type": "agentMessage",
            "id": "call_q1",
            "delivery": "async",
            "questions": [{"title": "Q", "options": "a"}],
        },
        {"type": "message", "id": "call_q1", "delivery": "async", "questions": [{"title": "Q"}]},
    ],
)
def test_async_questions_from_item_rejects_the_text_path(item: dict) -> None:
    """Malformed or non-async items keep the plain-text path."""
    assert fwd.async_questions_from_item(item) is None


async def test_image_only_async_answer_keeps_protocol_reply_and_image_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = fwd._CodexAsyncQuestionTracker()
    question = tracker.register(
        call_id="call_a",
        index=0,
        title="Show the issue",
        options=None,
        thread_id=None,
        turn_id=None,
    )
    assert question is not None
    sent: list[dict] = []

    async def fake_hook(*_args: object, **_kwargs: object) -> dict:
        return {
            "answers": {},
            "_meta": {
                "omnigent_question_images": {"call_a:0": ["image-a"]},
                "omnigent_comment_ids": ["comment-a"],
            },
        }

    async def fake_post(*_args: object, **kwargs: object) -> fwd._PostResult:
        sent.append(kwargs)
        return fwd._PostResult(response=httpx.Response(202))

    monkeypatch.setattr(fwd, "_codex_elicitation_hook_result", fake_hook)
    monkeypatch.setattr(fwd, "_post_session_event_result", fake_post)

    await tracker._run_wait(None, "session-a", question, fwd._async_question_envelope(question))  # type: ignore[arg-type]

    assert question.answered
    assert len(sent) == 1
    assert sent[0]["max_attempts"] == 1
    assert sent[0]["data"]["content"][1] == {"type": "input_image", "file_id": "image-a"}
    assert sent[0]["data"]["omnigent_comment_ids"] == ["comment-a"]
    assert fwd.parse_question_reply(sent[0]["data"]["content"][0]["text"]) == [
        ("call_a", "Show the issue", 0, "")
    ]


async def test_async_answer_reparks_when_server_reports_native_start_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = fwd._CodexAsyncQuestionTracker()
    question = tracker.register(
        call_id="call_a",
        index=0,
        title="Show the issue",
        options=None,
        thread_id=None,
        turn_id=None,
    )
    assert question is not None
    restarted: list[int] = []

    async def fake_hook(*_args: object, **_kwargs: object) -> dict:
        return {"answers": {"call_a:0": {"answers": ["Here"]}}}

    async def fake_post(*_args: object, **_kwargs: object) -> fwd._PostResult:
        return fwd._PostResult(response=httpx.Response(202, json={"forwarded": False}))

    monkeypatch.setattr(fwd, "_codex_elicitation_hook_result", fake_hook)
    monkeypatch.setattr(fwd, "_post_session_event_result", fake_post)
    monkeypatch.setattr(
        tracker, "_start_wait", lambda *_args: restarted.append(question.generation)
    )

    await tracker._run_wait(None, "session-a", question, fwd._async_question_envelope(question))  # type: ignore[arg-type]

    assert not question.answered
    assert question.generation == 1
    assert restarted == [1]
