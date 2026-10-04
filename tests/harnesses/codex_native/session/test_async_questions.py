"""Async-question web-card tests for Codex session."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.elicitation import codex_elicitation_id
from tests.harnesses.codex_native.session._support import (
    _elicitation_tracker,
    _FakeCodexAppServerClient,
    _usage_coalescer,
)

# ── Codex async questions as web question cards ──────────────

_ASYNC_QUESTION_TITLE = "选择卡显示测试：你能看到这张可点击的单选卡吗？"
_ASYNC_QUESTION_OPTIONS = ["看到了，可以点击选项", "看到了，但无法点击"]


class _AsyncQuestionOmnigentClient:
    """
    Omnigent client double for the async-question web-card paths.

    :param answers: Answer text returned for each hook call naming that
        question id.
    :param hold: Question ids whose hook calls park until
        :meth:`release_hooks`, then return ``held_hook_status``.
    :param answer_once: Question ids answered on their first hook call and
        parked on every later one (a proven-non-delivered reply re-parks
        under a new generation).
    :param hook_failures: Leading hook calls answered with a 500, as a
        restarting server would.
    :param held_hook_status: Status a released held hook call returns,
        e.g. ``500``.
    :param message_failures: Leading ``message`` event posts answered with
        ``message_failure_status``.
    :param message_failure_status: Status those posts return, e.g. ``400``
        (proven non-delivery) or ``500`` (unknown delivery).
    :param message_timeouts: Leading ``message`` event posts that raise
        ``httpx.ReadTimeout``, an ambiguous transport failure.
    :param hold_message_once: When ``True``, the first ``message`` event
        post parks until :meth:`release_messages`, then fails with
        ``message_failure_status``.
    """

    def __init__(
        self,
        *,
        answers: dict[str, str] | None = None,
        hold: set[str] | None = None,
        answer_once: set[str] | None = None,
        hook_failures: int = 0,
        held_hook_status: int = 200,
        message_failures: int = 0,
        message_failure_status: int = 500,
        message_timeouts: int = 0,
        hold_message_once: bool = False,
    ) -> None:
        self.answers = dict(answers or {})
        self.hold = set(hold or ())
        self.answer_once = set(answer_once or ())
        self.hook_failures = hook_failures
        self.held_hook_status = held_hook_status
        self.message_failures = message_failures
        self.message_failure_status = message_failure_status
        self.message_timeouts = message_timeouts
        self.hold_message_once = hold_message_once
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.hook_bodies: list[dict[str, Any]] = []
        self._hooks_released = asyncio.Event()
        self._messages_released = asyncio.Event()

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        timeout: Any = None,
    ) -> httpx.Response:
        """
        Record one post and answer it per this double's script.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_123/events"``.
        :param json: JSON body.
        :param timeout: Ignored request timeout.
        :returns: Scripted Omnigent response.
        """
        self.posts.append((url, json))
        if url.endswith("/hooks/codex-elicitation-request"):
            return await self._answer_hook(json)
        if json.get("type") == "message":
            if self.hold_message_once:
                self.hold_message_once = False
                await self._messages_released.wait()
                return httpx.Response(self.message_failure_status, text="message post failed")
            if self.message_timeouts > 0:
                self.message_timeouts -= 1
                raise httpx.ReadTimeout(
                    "message post response lost", request=httpx.Request("POST", url)
                )
            if self.message_failures > 0:
                self.message_failures -= 1
                return httpx.Response(self.message_failure_status, text="message post failed")
        return httpx.Response(202, json={"queued": False})

    async def _answer_hook(self, body: dict[str, Any]) -> httpx.Response:
        """
        Answer one hook call from the configured answers/holds.

        :param body: Codex elicitation request envelope posted by the
            forwarder.
        :returns: Omnigent hook response.
        """
        self.hook_bodies.append(body)
        if self.hook_failures > 0:
            self.hook_failures -= 1
            return httpx.Response(500, text="hook unavailable")
        question_id = body["params"]["questions"][0]["id"]
        if question_id in self.answer_once:
            self.answer_once.discard(question_id)
            self.hold.add(question_id)
        elif question_id in self.hold:
            await self._hooks_released.wait()
            return httpx.Response(self.held_hook_status)
        answer = self.answers.get(question_id)
        if answer is None:
            return httpx.Response(200)
        return httpx.Response(200, json={"answers": {question_id: {"answers": [answer]}}})

    def release_hooks(self) -> None:
        """Release every parked hook call (they then return their held status)."""
        self._hooks_released.set()

    def release_messages(self) -> None:
        """Release a parked reply POST (it then fails with its configured status)."""
        self._messages_released.set()

    def events(self, event_type: str) -> list[dict[str, Any]]:
        """
        Return recorded posts of one event type, in order.

        :param event_type: Session event type, e.g. ``"message"``.
        :returns: Matching post bodies.
        """
        return [body for _url, body in self.posts if body.get("type") == event_type]

    def conversation_items(self, item_type: str) -> list[dict[str, Any]]:
        """
        Return recorded conversation items of one item type, in order.

        :param item_type: Item type, e.g. ``"function_call"``.
        :returns: Matching ``data`` payloads.
        """
        return [
            body["data"]
            for body in self.events("external_conversation_item")
            if body["data"].get("item_type") == item_type
        ]


def _async_question_state() -> codex_native_forwarder._CodexForwarderState:
    """
    Build forwarder state whose own route is ``conv_123``.

    :returns: Fresh state with an empty async-question tracker.
    """
    return codex_native_forwarder._CodexForwarderState(parent_session_id="conv_123")


def _async_question_item(
    *,
    call_id: str = "call_q1",
    questions: list[dict[str, Any]] | None = None,
    text: str = "选择卡显示测试：你能看到这张可点击的单选卡吗？\n- 看到了\n- 没看到",
) -> dict[str, Any]:
    """
    Build a live Codex async ``request_user_input_async`` agentMessage.

    :param call_id: Codex tool call id, e.g. ``"call_q1"``.
    :param questions: Question payloads; default one two-option question.
    :param text: Assistant text the plain-text path would post.
    :returns: Codex ``agentMessage`` item dict.
    """
    if questions is None:
        questions = [{"title": _ASYNC_QUESTION_TITLE, "options": list(_ASYNC_QUESTION_OPTIONS)}]
    return {
        "type": "agentMessage",
        "id": call_id,
        "text": text,
        "delivery": "async",
        "questions": questions,
    }


def _item_completed_event(
    item: dict[str, Any],
    *,
    thread_id: str = "thread_123",
    turn_id: str = "turn_123",
) -> dict[str, Any]:
    """
    Wrap a Codex item in its ``item/completed`` notification.

    :param item: Codex thread item.
    :param thread_id: Codex thread id, e.g. ``"thread_123"``.
    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :returns: Codex notification envelope.
    """
    return {
        "method": "item/completed",
        "params": {"threadId": thread_id, "turnId": turn_id, "item": item},
    }


def _tagged_question_reply(
    answer: str,
    *,
    call_id: str = "call_q1",
    index: int = 0,
    question: str = _ASYNC_QUESTION_TITLE,
) -> str:
    """
    Build a tagged reply the way Codex's TUI writes it.

    :param answer: Answered text.
    :param call_id: Codex tool call id, e.g. ``"call_q1"``.
    :param index: Question index, e.g. ``0``.
    :param question: Question title echoed in the reply.
    :returns: Tagged reply text.
    """
    question_item_id = json.dumps(
        ["request_user_input_async", call_id, index], separators=(",", ":")
    )
    payload = [{"answer": answer, "question": question, "questionItemId": question_item_id}]
    return (
        "<send_user_message_question_reply>\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n</send_user_message_question_reply>"
    )


def _tagged_user_message_event(
    text: str,
    *,
    item_id: str = "item_reply",
    thread_id: str = "thread_123",
    turn_id: str = "turn_456",
) -> dict[str, Any]:
    """
    Wrap tagged reply text in a Codex ``userMessage`` item.

    :param text: Tagged reply text.
    :param item_id: Codex user-message item id, e.g. ``"item_reply"``.
    :param thread_id: Codex thread id, e.g. ``"thread_123"``.
    :param turn_id: Codex turn id, e.g. ``"turn_456"``.
    :returns: ``item/completed`` notification envelope.
    """
    return _item_completed_event(
        {
            "type": "userMessage",
            "id": item_id,
            "content": [{"type": "text", "text": text}],
        },
        thread_id=thread_id,
        turn_id=turn_id,
    )


def _expected_async_question_envelope(
    *,
    call_id: str = "call_q1",
    index: int = 0,
    generation: int = 0,
    title: str = _ASYNC_QUESTION_TITLE,
    options: list[str] | None = None,
) -> dict[str, Any]:
    """
    Build the synthetic ``requestUserInput`` envelope the forwarder posts.

    :param call_id: Codex tool call id, e.g. ``"call_q1"``.
    :param index: Question index, e.g. ``0``.
    :param generation: Card generation, e.g. ``0``.
    :param title: Question title.
    :param options: Option labels; ``None`` means the question's own
        default (a free-text question passes ``[]`` explicitly).
    :returns: Expected Codex JSON-RPC request envelope.
    """
    labels = _ASYNC_QUESTION_OPTIONS if options is None else options
    return {
        "id": f"async_question:{call_id}:{index}:{generation}",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": call_id,
            "omnigentAsyncQuestion": True,
            "questions": [
                {
                    "id": f"{call_id}:{index}",
                    "question": title,
                    "isOther": True,
                    "isSecret": False,
                    "options": [{"label": label} for label in labels],
                }
            ],
        },
    }


async def _drive_async_question(
    client: _AsyncQuestionOmnigentClient,
    state: codex_native_forwarder._CodexForwarderState,
    bridge_dir: Path,
    item: dict[str, Any],
) -> None:
    """
    Send one live async-question item through the forwarder.

    :param client: Omnigent client double.
    :param state: Forwarder state owning the tracker.
    :param bridge_dir: Native Codex bridge directory.
    :param item: Codex ``agentMessage`` item.
    :returns: None.
    """
    await codex_native_forwarder._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_123",
        bridge_dir=bridge_dir,
        usage_coalescer=_usage_coalescer(client),  # type: ignore[arg-type]
        elicitation_tracker=_elicitation_tracker(),
        event=_item_completed_event(item),
        forwarder_state=state,
    )


async def _wait_until(predicate: Callable[[], bool], *, attempts: int = 200) -> None:
    """
    Yield to the event loop until ``predicate`` holds.

    Deliberately timer-free so the 50-minute-bound test can run it under a
    patched loop clock.

    :param predicate: Condition to poll, e.g. a hook-call count.
    :param attempts: Maximum yields before failing the test.
    :raises AssertionError: When the condition never becomes true.
    """
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def _settle_async_question_wait(
    state: codex_native_forwarder._CodexForwarderState,
    *,
    call_id: str = "call_q1",
    index: int = 0,
) -> None:
    """
    Await one question's background wait so its posts land before asserts.

    :param state: Forwarder state owning the tracker.
    :param call_id: Codex tool call id, e.g. ``"call_q1"``.
    :param index: Question index, e.g. ``0``.
    :returns: None.
    """
    question = state.async_question_tracker.question(call_id, index)
    assert question is not None, "async question was not registered"
    assert question.wait_task is not None, "async question has no active wait"
    await asyncio.wait_for(question.wait_task, timeout=2.0)
    await asyncio.sleep(0)


def test_async_question_posts_one_card_per_question(tmp_path: Path) -> None:
    """
    A live async ``agentMessage`` becomes a ``function_call`` item and a
    parked web card instead of plain assistant text.

    The app-server delivers ``request_user_input_async`` as an async
    agentMessage; posting only its text (today's behavior) leaves the web
    without the clickable card the Codex TUI shows.
    """
    omnigent = _AsyncQuestionOmnigentClient(answers={"call_q1:0": _ASYNC_QUESTION_OPTIONS[0]})
    state = _async_question_state()

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _settle_async_question_wait(state)
        await state.async_question_tracker.close()

    asyncio.run(run())

    function_calls = omnigent.conversation_items("function_call")
    assert len(function_calls) == 1
    item_data = function_calls[0]["item_data"]
    assert item_data["agent"] == "codex-native-ui"
    assert item_data["name"] == "request_user_input_async"
    assert item_data["call_id"] == "call_q1:0"
    assert function_calls[0]["source_id"] == "thread_123:turn_123:call_q1"
    assert json.loads(item_data["arguments"]) == {
        "questions": [
            {
                "id": "call_q1:0",
                "question": _ASYNC_QUESTION_TITLE,
                "options": [{"label": option} for option in _ASYNC_QUESTION_OPTIONS],
                "multiSelect": False,
            }
        ]
    }
    assistant_text = [
        data
        for data in omnigent.conversation_items("message")
        if data["item_data"]["role"] == "assistant"
    ]
    assert assistant_text == []
    assert omnigent.hook_bodies == [_expected_async_question_envelope()]


def test_async_question_web_answer_posts_the_tagged_reply(tmp_path: Path) -> None:
    """
    A web answer returns to Codex as the protocol's tagged user message.

    The reply must be byte-equal to the Codex TUI's own reply format, or
    Codex cannot match it to the question by ``questionItemId``.
    """
    omnigent = _AsyncQuestionOmnigentClient(answers={"call_q1:0": "看到了，可以点击选项"})
    state = _async_question_state()

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _settle_async_question_wait(state)
        await state.async_question_tracker.close()

    asyncio.run(run())

    messages = omnigent.events("message")
    assert len(messages) == 1
    assert messages[0]["data"] == {
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": (
                    "<send_user_message_question_reply>\n"
                    '[{"answer":"看到了，可以点击选项",'
                    '"question":"选择卡显示测试：你能看到这张可点击的单选卡吗？",'
                    '"questionItemId":"[\\"request_user_input_async\\",\\"call_q1\\",0]"}]\n'
                    "</send_user_message_question_reply>"
                ),
            }
        ],
    }


def test_async_question_free_text_answer_round_trips(tmp_path: Path) -> None:
    """An ``options: null`` question parks a free-text card and answers it."""
    omnigent = _AsyncQuestionOmnigentClient(answers={"call_q1:0": "自定义"})
    state = _async_question_state()
    item = _async_question_item(questions=[{"title": "你选哪个？", "options": None}])

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, item)
        await _settle_async_question_wait(state)
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert omnigent.hook_bodies == [
        _expected_async_question_envelope(title="你选哪个？", options=[])
    ]
    call = omnigent.conversation_items("function_call")[0]
    assert json.loads(call["item_data"]["arguments"])["questions"][0]["options"] == []
    reply_text = omnigent.events("message")[0]["data"]["content"][0]["text"]
    assert '"answer":"自定义"' in reply_text
    assert '"questionItemId":"[\\"request_user_input_async\\",\\"call_q1\\",0]"' in reply_text


def test_async_question_tui_answer_flips_the_parked_card(tmp_path: Path) -> None:
    """
    A TUI answer resolves the parked card and posts the tool output.

    Without the resolve the web card stays clickable after the TUI already
    answered; without the output the tool row has no answer text. The
    parked hook wait is ended so a severed poll cannot re-POST it.
    """
    omnigent = _AsyncQuestionOmnigentClient(hold={"call_q1:0"})
    state = _async_question_state()
    reply = _tagged_question_reply("TUI 的选择")

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _wait_until(lambda: len(omnigent.hook_bodies) == 1)
        await codex_native_forwarder._handle_event(
            omnigent,  # type: ignore[arg-type]
            session_id="conv_123",
            bridge_dir=tmp_path,
            usage_coalescer=_usage_coalescer(omnigent),  # type: ignore[arg-type]
            elicitation_tracker=_elicitation_tracker(),
            event=_tagged_user_message_event(reply),
            forwarder_state=state,
        )
        question = state.async_question_tracker.question("call_q1", 0)
        assert question is not None and question.answered
        assert question.wait_task is None
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert omnigent.events("external_elicitation_resolved") == [
        {
            "type": "external_elicitation_resolved",
            "data": {
                "elicitation_id": codex_elicitation_id(
                    "conv_123",
                    "item/tool/requestUserInput",
                    "async_question:call_q1:0:0",
                )
            },
        }
    ]
    assert omnigent.conversation_items("function_call_output") == [
        {
            "item_type": "function_call_output",
            "item_data": {"call_id": "call_q1:0", "output": "TUI 的选择"},
            "response_id": "codex_turn_456",
            "source_id": "async_question:call_q1:0:output",
        }
    ]
    assert omnigent.events("message") == []
    user_items = [
        data
        for data in omnigent.conversation_items("message")
        if data["item_data"]["role"] == "user"
    ]
    assert user_items[0]["item_data"]["content"][0]["text"] == reply


def test_async_question_tui_echo_cancels_the_parked_hook_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An echo of a TUI answer ends the parked hook wait.

    A severed poll would otherwise re-POST the envelope and re-park the
    answered question; the wait must be cancelled, not merely resolved.
    """
    monkeypatch.setattr(
        codex_native_forwarder, "_elicitation_retry_sleep", lambda _seconds: asyncio.sleep(0)
    )
    omnigent = _AsyncQuestionOmnigentClient(hold={"call_q1:0"}, held_hook_status=500)
    state = _async_question_state()
    reply = _tagged_question_reply("TUI 的选择")

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _wait_until(lambda: len(omnigent.hook_bodies) == 1)
        await codex_native_forwarder._handle_event(
            omnigent,  # type: ignore[arg-type]
            session_id="conv_123",
            bridge_dir=tmp_path,
            usage_coalescer=_usage_coalescer(omnigent),  # type: ignore[arg-type]
            elicitation_tracker=_elicitation_tracker(),
            event=_tagged_user_message_event(reply),
            forwarder_state=state,
        )
        question = state.async_question_tracker.question("call_q1", 0)
        assert question is not None and question.answered
        assert question.wait_task is None
        omnigent.release_hooks()
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(omnigent.hook_bodies) == 1
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert [body["id"] for body in omnigent.hook_bodies] == ["async_question:call_q1:0:0"]
    assert len(omnigent.events("external_elicitation_resolved")) == 1


def test_async_question_web_answer_echo_does_not_re_resolve(tmp_path: Path) -> None:
    """
    Codex's echo of a delivered web answer confirms it without resolving a
    dead wait or sending a second reply.
    """
    omnigent = _AsyncQuestionOmnigentClient(answers={"call_q1:0": "A"})
    state = _async_question_state()
    reply = _tagged_question_reply("A")

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _settle_async_question_wait(state)
        await codex_native_forwarder._handle_event(
            omnigent,  # type: ignore[arg-type]
            session_id="conv_123",
            bridge_dir=tmp_path,
            usage_coalescer=_usage_coalescer(omnigent),  # type: ignore[arg-type]
            elicitation_tracker=_elicitation_tracker(),
            event=_tagged_user_message_event(reply, item_id="item_echo"),
            forwarder_state=state,
        )
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert len(omnigent.events("message")) == 1
    assert omnigent.events("external_elicitation_resolved") == []
    assert len(omnigent.conversation_items("function_call_output")) == 1


def test_async_question_duplicate_live_delivery_parks_one_card(tmp_path: Path) -> None:
    """Redelivering the same live item does not park a second card."""
    omnigent = _AsyncQuestionOmnigentClient(answers={"call_q1:0": "A"})
    state = _async_question_state()
    event = _item_completed_event(_async_question_item())

    async def run() -> None:
        for _ in range(2):
            await codex_native_forwarder._handle_event(
                omnigent,  # type: ignore[arg-type]
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(omnigent),  # type: ignore[arg-type]
                elicitation_tracker=_elicitation_tracker(),
                event=event,
                forwarder_state=state,
            )
        await _settle_async_question_wait(state)
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert len(omnigent.hook_bodies) == 1
    assert len(omnigent.conversation_items("function_call")) == 1


def test_async_question_replay_takes_the_text_path(tmp_path: Path) -> None:
    """
    A replayed async item posts honest text under the same source id and
    never parks a card (a replay is indistinguishable from a pre-update
    item, so parking would resurrect zombie cards).
    """
    omnigent = _AsyncQuestionOmnigentClient()
    state = _async_question_state()
    item = _async_question_item()
    response = {
        "result": {
            "thread": {
                "id": "thread_123",
                "turns": [{"id": "turn_123", "items": [item]}],
            }
        }
    }

    async def run() -> None:
        await codex_native_forwarder._replay_resume_response(
            omnigent,  # type: ignore[arg-type]
            session_id="conv_123",
            bridge_dir=tmp_path,
            response=response,
            usage_coalescer=_usage_coalescer(omnigent),  # type: ignore[arg-type]
            elicitation_tracker=_elicitation_tracker(),
            forwarder_state=state,
        )
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert omnigent.hook_bodies == []
    assert state.async_question_tracker.question("call_q1", 0) is None
    messages = omnigent.conversation_items("message")
    assert len(messages) == 1
    assert messages[0]["item_data"]["role"] == "assistant"
    assert messages[0]["item_data"]["content"][0]["text"] == item["text"]
    assert messages[0]["source_id"] == "thread_123:turn_123:call_q1"


def test_async_question_two_questions_answer_one(tmp_path: Path) -> None:
    """Each of a call's questions is answerable alone; the other stays parked."""
    omnigent = _AsyncQuestionOmnigentClient(
        answers={"call_q1:1": "B"},
        hold={"call_q1:0"},
    )
    state = _async_question_state()
    item = _async_question_item(
        questions=[
            {"title": "问题一", "options": ["A1", "A2"]},
            {"title": "问题二", "options": ["B1", "B2"]},
        ]
    )

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, item)
        await _wait_until(lambda: len(omnigent.hook_bodies) == 2)
        await _settle_async_question_wait(state, index=1)
        await codex_native_forwarder._handle_event(
            omnigent,  # type: ignore[arg-type]
            session_id="conv_123",
            bridge_dir=tmp_path,
            usage_coalescer=_usage_coalescer(omnigent),  # type: ignore[arg-type]
            elicitation_tracker=_elicitation_tracker(),
            event=_tagged_user_message_event(
                _tagged_question_reply("B", index=1), item_id="item_echo"
            ),
            forwarder_state=state,
        )
        unanswered = state.async_question_tracker.question("call_q1", 0)
        assert unanswered is not None
        assert not unanswered.answered
        assert unanswered.wait_task is not None and not unanswered.wait_task.done()
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert [body["id"] for body in omnigent.hook_bodies] == [
        "async_question:call_q1:0:0",
        "async_question:call_q1:1:0",
    ]
    assert omnigent.events("external_elicitation_resolved") == []
    assert omnigent.conversation_items("function_call_output") == [
        {
            "item_type": "function_call_output",
            "item_data": {"call_id": "call_q1:1", "output": "B"},
            "response_id": "codex_turn_456",
            "source_id": "async_question:call_q1:1:output",
        }
    ]


def test_async_question_repolls_after_a_transient_hook_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A severed/restarting hook re-POSTs the same envelope once and answers."""
    monkeypatch.setattr(
        codex_native_forwarder, "_elicitation_retry_sleep", lambda _seconds: asyncio.sleep(0)
    )
    omnigent = _AsyncQuestionOmnigentClient(answers={"call_q1:0": "A"}, hook_failures=1)
    state = _async_question_state()

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _settle_async_question_wait(state)
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert len(omnigent.hook_bodies) == 2
    assert omnigent.hook_bodies[0] == omnigent.hook_bodies[1]
    assert len(omnigent.events("message")) == 1


def test_async_question_card_has_no_fifty_minute_expiry(tmp_path: Path) -> None:
    """
    A parked card outlives any 50-minute bound.

    No timer may send a reply or resolve the card: with the hook held and a
    clock far past 3000 s, nothing is posted and the wait is still parked.
    """
    omnigent = _AsyncQuestionOmnigentClient(hold={"call_q1:0"})
    state = _async_question_state()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        real_time = loop.time
        clock = {"now": 0.0}

        def _fake_time() -> float:
            return clock["now"]

        loop.time = _fake_time  # type: ignore[method-assign]
        try:
            await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
            await _wait_until(lambda: len(omnigent.hook_bodies) == 1)
            clock["now"] = 3600.0
            for _ in range(3):
                await asyncio.sleep(0)
            question = state.async_question_tracker.question("call_q1", 0)
            assert question is not None and question.wait_task is not None
            assert not question.wait_task.done()
            assert not question.answered
            assert omnigent.events("message") == []
            assert omnigent.events("external_elicitation_resolved") == []
        finally:
            loop.time = real_time  # type: ignore[method-assign]
        omnigent.release_hooks()
        await _settle_async_question_wait(state)
        await state.async_question_tracker.close()

    asyncio.run(run())


def test_async_question_decline_sends_nothing(tmp_path: Path) -> None:
    """An empty hook body (decline, cancel, timeout) sends no reply."""
    omnigent = _AsyncQuestionOmnigentClient()
    state = _async_question_state()

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _settle_async_question_wait(state)
        question = state.async_question_tracker.question("call_q1", 0)
        assert question is not None and not question.answered
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert omnigent.events("message") == []
    assert omnigent.events("external_elicitation_resolved") == []


def test_async_question_declined_question_is_not_revived_by_a_later_ask(
    tmp_path: Path,
) -> None:
    """
    A wait that ended without an answer retires the question.

    Re-parking every unanswered question when another ask arrives would
    resurrect a declined card the moment a later question lands.
    """
    omnigent = _AsyncQuestionOmnigentClient(hold={"call_q2:0"})
    state = _async_question_state()

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _settle_async_question_wait(state)
        await _drive_async_question(
            omnigent, state, tmp_path, _async_question_item(call_id="call_q2")
        )
        await _wait_until(lambda: len(omnigent.hook_bodies) == 2)
        declined = state.async_question_tracker.question("call_q1", 0)
        assert declined is not None and declined.wait_task is None
        later = state.async_question_tracker.question("call_q2", 0)
        assert later is not None and later.wait_task is not None
        assert not later.wait_task.done()
        assert [body["id"] for body in omnigent.hook_bodies] == [
            "async_question:call_q1:0:0",
            "async_question:call_q2:0:0",
        ]
        omnigent.release_hooks()
        await _settle_async_question_wait(state, call_id="call_q2")
        await state.async_question_tracker.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "questions",
    [
        [],
        [{"options": ["a"]}],
        [{"title": "", "options": ["a"]}],
        [{"title": "哪個？", "options": ["a", 3]}],
    ],
)
def test_async_question_malformed_item_posts_plain_text(
    tmp_path: Path, questions: list[dict[str, Any]]
) -> None:
    """Malformed async questions keep the ordinary assistant-text path."""
    omnigent = _AsyncQuestionOmnigentClient()
    state = _async_question_state()
    item = _async_question_item(questions=questions)

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, item)
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert omnigent.hook_bodies == []
    assert state.async_question_tracker.question("call_q1", 0) is None
    messages = omnigent.conversation_items("message")
    assert len(messages) == 1
    assert messages[0]["item_data"]["role"] == "assistant"
    assert messages[0]["item_data"]["content"][0]["text"] == item["text"]


def test_async_question_child_route_posts_plain_text(tmp_path: Path) -> None:
    """An async question on a real child thread stays text on the child."""
    omnigent = _AsyncQuestionOmnigentClient()
    state = _async_question_state()
    state.note_child_thread("thread_child", "conv_child")
    item = _async_question_item(call_id="call_child")

    async def run() -> None:
        await codex_native_forwarder._handle_event(
            omnigent,  # type: ignore[arg-type]
            session_id="conv_123",
            bridge_dir=tmp_path,
            usage_coalescer=_usage_coalescer(omnigent),  # type: ignore[arg-type]
            elicitation_tracker=_elicitation_tracker(),
            event=_item_completed_event(item, thread_id="thread_child", turn_id="turn_child"),
            expected_thread_id="thread_123",
            forwarder_state=state,
        )
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert omnigent.hook_bodies == []
    assert state.async_question_tracker.question("call_child", 0) is None
    assert omnigent.posts[0][0] == "/v1/sessions/conv_child/events"
    assert omnigent.posts[0][1]["type"] == "external_conversation_item"
    assert omnigent.posts[0][1]["data"]["item_data"]["role"] == "assistant"


def _lookalike_reply_with_trailing_prose(_call_id: str, _index: int) -> str:
    """A well-formed reply plus extra prose is not a reply."""
    return _tagged_question_reply("A") + "\n请继续"


def _lookalike_reply_with_foreign_tool(call_id: str, index: int) -> str:
    """A reply whose ``questionItemId`` names another tool is not a reply."""
    return (
        "<send_user_message_question_reply>\n"
        + json.dumps(
            [
                {
                    "answer": "A",
                    "question": "Q",
                    "questionItemId": json.dumps(
                        ["other_tool", call_id, index], separators=(",", ":")
                    ),
                }
            ],
            separators=(",", ":"),
        )
        + "\n</send_user_message_question_reply>"
    )


def _lookalike_reply_with_bad_json(_call_id: str, _index: int) -> str:
    """A tag pair around non-JSON text is not a reply."""
    return "<send_user_message_question_reply>\nnot json\n</send_user_message_question_reply>"


@pytest.mark.parametrize(
    "reply_builder",
    [
        _lookalike_reply_with_trailing_prose,
        _lookalike_reply_with_foreign_tool,
        _lookalike_reply_with_bad_json,
    ],
)
def test_async_question_lookalike_reply_is_just_a_message(
    tmp_path: Path,
    reply_builder: Callable[[str, int], str],
) -> None:
    """A tagged message that is not a valid reply resolves nothing."""
    omnigent = _AsyncQuestionOmnigentClient(hold={"call_q1:0"})
    state = _async_question_state()
    reply = reply_builder("call_q1", 0)

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _wait_until(lambda: len(omnigent.hook_bodies) == 1)
        await codex_native_forwarder._handle_event(
            omnigent,  # type: ignore[arg-type]
            session_id="conv_123",
            bridge_dir=tmp_path,
            usage_coalescer=_usage_coalescer(omnigent),  # type: ignore[arg-type]
            elicitation_tracker=_elicitation_tracker(),
            event=_tagged_user_message_event(reply),
            forwarder_state=state,
        )
        question = state.async_question_tracker.question("call_q1", 0)
        assert question is not None and not question.answered
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert omnigent.conversation_items("function_call_output") == []
    assert omnigent.events("external_elicitation_resolved") == []
    user_items = [
        data
        for data in omnigent.conversation_items("message")
        if data["item_data"]["role"] == "user"
    ]
    assert user_items[0]["item_data"]["content"][0]["text"] == reply


def test_async_question_unregistered_reply_is_just_a_message(tmp_path: Path) -> None:
    """A tagged reply for a question this run never registered only posts."""
    omnigent = _AsyncQuestionOmnigentClient()
    state = _async_question_state()
    reply = _tagged_question_reply("A", call_id="call_unknown")

    async def run() -> None:
        await codex_native_forwarder._handle_event(
            omnigent,  # type: ignore[arg-type]
            session_id="conv_123",
            bridge_dir=tmp_path,
            usage_coalescer=_usage_coalescer(omnigent),  # type: ignore[arg-type]
            elicitation_tracker=_elicitation_tracker(),
            event=_tagged_user_message_event(reply),
            forwarder_state=state,
        )
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert omnigent.conversation_items("function_call_output") == []
    assert omnigent.events("external_elicitation_resolved") == []
    user_items = [
        data
        for data in omnigent.conversation_items("message")
        if data["item_data"]["role"] == "user"
    ]
    assert user_items[0]["item_data"]["content"][0]["text"] == reply


def test_async_question_send_failure_reparks_as_the_next_generation(
    tmp_path: Path,
) -> None:
    """
    A proven-non-delivered (4xx) reply POST is not retried; the question
    re-parks as gen 1 and a later echo resolves that generation.
    """
    omnigent = _AsyncQuestionOmnigentClient(
        answers={"call_q1:0": "A"},
        answer_once={"call_q1:0"},
        message_failures=1,
        message_failure_status=400,
    )
    state = _async_question_state()

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _wait_until(lambda: len(omnigent.hook_bodies) == 2)
        question = state.async_question_tracker.question("call_q1", 0)
        assert question is not None and question.generation == 1
        await codex_native_forwarder._handle_event(
            omnigent,  # type: ignore[arg-type]
            session_id="conv_123",
            bridge_dir=tmp_path,
            usage_coalescer=_usage_coalescer(omnigent),  # type: ignore[arg-type]
            elicitation_tracker=_elicitation_tracker(),
            event=_tagged_user_message_event(_tagged_question_reply("A")),
            forwarder_state=state,
        )
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert [body["id"] for body in omnigent.hook_bodies] == [
        "async_question:call_q1:0:0",
        "async_question:call_q1:0:1",
    ]
    assert len(omnigent.events("message")) == 1
    assert omnigent.events("external_elicitation_resolved") == [
        {
            "type": "external_elicitation_resolved",
            "data": {
                "elicitation_id": codex_elicitation_id(
                    "conv_123",
                    "item/tool/requestUserInput",
                    "async_question:call_q1:0:1",
                )
            },
        }
    ]
    assert len(omnigent.conversation_items("function_call_output")) == 1


def test_async_question_unknown_reply_delivery_does_not_reopen_submission(
    tmp_path: Path,
) -> None:
    """
    A reply POST whose response was lost leaves the question unanswered.

    Codex keeps the later of two replies for one question (no consumer-side
    dedup), so an unknown delivery must not re-park: a second answer would
    overwrite the first.
    """
    omnigent = _AsyncQuestionOmnigentClient(answers={"call_q1:0": "A"}, message_timeouts=1)
    state = _async_question_state()

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _settle_async_question_wait(state)
        question = state.async_question_tracker.question("call_q1", 0)
        assert question is not None and not question.answered
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert [body["id"] for body in omnigent.hook_bodies] == ["async_question:call_q1:0:0"]
    assert len(omnigent.events("message")) == 1


def test_async_question_echo_in_flight_blocks_the_re_park(tmp_path: Path) -> None:
    """
    An echo reconciled while the reply POST is in flight wins.

    That POST then fails with a proven non-delivery, but the question is
    already answered, so no generation-1 card may be parked.
    """
    omnigent = _AsyncQuestionOmnigentClient(
        answers={"call_q1:0": "A"},
        hold_message_once=True,
        message_failure_status=400,
    )
    state = _async_question_state()
    reply = _tagged_question_reply("A")

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _wait_until(lambda: len(omnigent.events("message")) == 1)
        await codex_native_forwarder._handle_event(
            omnigent,  # type: ignore[arg-type]
            session_id="conv_123",
            bridge_dir=tmp_path,
            usage_coalescer=_usage_coalescer(omnigent),  # type: ignore[arg-type]
            elicitation_tracker=_elicitation_tracker(),
            event=_tagged_user_message_event(reply, item_id="item_echo"),
            forwarder_state=state,
        )
        question = state.async_question_tracker.question("call_q1", 0)
        assert question is not None and question.answered
        omnigent.release_messages()
        await _settle_async_question_wait(state)
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert [body["id"] for body in omnigent.hook_bodies] == ["async_question:call_q1:0:0"]


def test_async_question_recovered_reply_reconciles(tmp_path: Path) -> None:
    """
    A tagged reply recovered through ``thread/resume`` reconciles like a
    live TUI answer.
    """
    omnigent = _AsyncQuestionOmnigentClient(hold={"call_q1:0"})
    state = _async_question_state()
    reply = _tagged_question_reply("TUI 的选择")
    user_item = {
        "type": "userMessage",
        "id": "item_recovered",
        "content": [{"type": "text", "text": reply}],
    }
    fake_codex = _FakeCodexAppServerClient(
        response={
            "result": {
                "thread": {
                    "turns": [{"id": "turn_456", "items": [user_item]}],
                }
            }
        }
    )

    async def run() -> None:
        await _drive_async_question(omnigent, state, tmp_path, _async_question_item())
        await _wait_until(lambda: len(omnigent.hook_bodies) == 1)
        # The recovery path only runs once the app-server client is wired
        # (which is how the live forwarder misses then recovers the reply).
        state.codex_client = fake_codex  # type: ignore[assignment]
        await codex_native_forwarder._ensure_user_message_posted(
            omnigent,  # type: ignore[arg-type]
            "conv_123",
            {"threadId": "thread_123", "turnId": "turn_456", "item": user_item},
            state,
        )
        await state.async_question_tracker.close()

    asyncio.run(run())

    assert fake_codex.requests == [("thread/resume", {"threadId": "thread_123"})]
    assert len(omnigent.events("external_elicitation_resolved")) == 1
    assert omnigent.conversation_items("function_call_output") == [
        {
            "item_type": "function_call_output",
            "item_data": {"call_id": "call_q1:0", "output": "TUI 的选择"},
            "response_id": "codex_turn_456",
            "source_id": "async_question:call_q1:0:output",
        }
    ]
    user_items = [
        data
        for data in omnigent.conversation_items("message")
        if data["item_data"]["role"] == "user"
    ]
    assert user_items[0]["item_data"]["content"][0]["text"] == reply
