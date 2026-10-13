"""
Route tests for ``POST /v1/sessions/{id}/async-questions``.

The route backs the runner's ``ask_user_async`` tool: it publishes a
question card on the calling session's own stream only, parks the
elicitation detached from any HTTP request, and returns immediately. The
web verdict is delivered to the session later as a ``[System: …]`` user
message through the retrying delivery helper.

Covers design Test Scenarios 1-4, 17 and 18.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from omnigent.runtime import pending_elicitations, subagent_block_notifier
from omnigent.runtime.subagent_block_notifier import SubagentBlockNotifier
from omnigent.server.routes import sessions as sessions_route
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.sessions import routes_events as events_routes
from omnigent.server.routes.sessions import routes_hooks as hooks_routes
from omnigent.stores.conversation_store import DetachedCard
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from tests.server.helpers import create_test_agent, start_session_stream_collector

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_pending_elicitations() -> None:
    """Drain the elicitation index + observer between tests."""
    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


@pytest.fixture(autouse=True)
def _instant_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the block notifier's escalation grace out of test wall-clock."""

    async def _instant(_seconds: float) -> None:
        return

    monkeypatch.setattr(subagent_block_notifier, "_escalation_sleep", _instant)


@pytest_asyncio.fixture
async def conv_store(db_uri: str) -> AsyncIterator[SqlAlchemyConversationStore]:
    """A real store on the per-test SQLite database."""
    yield SqlAlchemyConversationStore(db_uri)


class _DeliveryRecorder:
    """Record the async verdict deliveries instead of posting them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.image_calls: list[list[str]] = []

    async def __call__(
        self,
        session_id: str,
        text: str,
        *,
        conversation_store: Any,
        runner_router: Any,
        persist_failure: bool = True,
        image_ids: list[str] | None = None,
    ) -> bool:
        self.calls.append((session_id, text))
        self.image_calls.append(image_ids or [])
        return True


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


async def _post_approval(
    client: httpx.AsyncClient,
    session_id: str,
    elicitation_id: str,
    action: str,
    content: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> httpx.Response:
    data: dict[str, Any] = {"elicitation_id": elicitation_id, "action": action}
    if content is not None:
        data["content"] = content
    if meta is not None:
        data["_meta"] = meta
    return await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "approval", "data": data},
    )


async def _next_elicitation(
    collector: Any,
    *,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    """Drain the collector until an elicitation request event arrives."""
    async with asyncio.timeout(timeout_s):
        while True:
            event = await collector.next_event(timeout_s)
            if event.get("type") == "response.elicitation_request":
                return event
    raise AssertionError("no elicitation request event arrived")


async def _wait_until(predicate: Any, *, timeout_s: float = 3.0) -> None:
    """Spin until ``predicate()`` is truthy or the budget elapses."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0)


async def _create_agent_session(client: httpx.AsyncClient, name: str) -> tuple[str, str]:
    agent = await create_test_agent(client, name)
    session_id = await _create_session(client, agent["id"])
    return agent["id"], session_id


async def test_async_question_happy_path(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A card returns at once on its own stream; accept delivers both answers."""
    delivered = _DeliveryRecorder()
    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", delivered)

    _, session_id = await _create_agent_session(client, "test-async-question-happy")
    collector = await start_session_stream_collector(session_id)
    try:
        resp = await client.post(
            f"/v1/sessions/{session_id}/async-questions",
            json={
                "questions": [
                    {
                        "question": "Which framework?",
                        "header": "Stack",
                        "options": [
                            {"label": "React", "description": "JS UI"},
                            {"label": "Django"},
                        ],
                    },
                    {"question": "Which targets?", "multiSelect": True},
                ],
                "context": "[report](/tmp/report.html)",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["qid"] == "q" + body["elicitation_id"].removeprefix("elicit_")[:6]

        event = await _next_elicitation(collector)
        assert event["elicitation_id"] == body["elicitation_id"]
        params = event["params"]
        assert params["phase"] == "async_question"
        assert params["policy_name"] == "omnigent_async_question"
        assert params["async_kind"] == "question"
        assert params["context"] == "[report](/tmp/report.html)"
        assert params["ask_user_question"]["questions"] == [
            {
                "question": "Which framework?",
                "header": "Stack",
                "options": [
                    {"label": "React", "description": "JS UI"},
                    {"label": "Django"},
                ],
                "multiSelect": False,
            },
            {
                "question": "Which targets?",
                "header": "",
                "options": [],
                "multiSelect": True,
            },
        ]

        verdict = await _post_approval(
            client,
            session_id,
            body["elicitation_id"],
            "accept",
            content={"Which framework?": "React", "Which targets?": ["iOS", "Android"]},
        )
        assert verdict.status_code == 202, verdict.text

        await _wait_until(lambda: bool(delivered.calls))
        assert len(delivered.calls) == 1
        target, text = delivered.calls[0]
        assert target == session_id
        assert text == (
            f"[System: answers to your question card #{body['qid']}]\n"
            "Which framework? → React\n"
            "Which targets? → iOS; Android"
        )
    finally:
        await collector.stop()


async def test_async_question_image_only_answer_keeps_references_and_rejects_foreign_files(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered = _DeliveryRecorder()
    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", delivered)
    _, session_id = await _create_agent_session(client, "test-async-question-image")
    _, other_id = await _create_agent_session(client, "test-async-question-other")
    file_store = SqlAlchemyFileStore(db_uri)
    image = file_store.create("diagram.png", 3, "image/png", session_id=session_id)
    foreign = file_store.create("other.png", 3, "image/png", session_id=other_id)
    non_image = file_store.create("notes.txt", 3, "text/plain", session_id=session_id)
    collector = await start_session_stream_collector(session_id)
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/async-questions",
            json={"questions": [{"question": "Show the issue"}]},
        )
        assert response.status_code == 200
        elicitation_id = (await _next_elicitation(collector))["elicitation_id"]
        for invalid_id in (foreign.id, non_image.id, "0" * 32):
            rejected = await _post_approval(
                client,
                session_id,
                elicitation_id,
                "accept",
                {"Show the issue": ""},
                {"omnigent_question_images": {"Show the issue": [invalid_id]}},
            )
            assert rejected.status_code == 400, (invalid_id, rejected.text)
            assert pending_elicitations.count_for(session_id) == 1
        accepted = await _post_approval(
            client,
            session_id,
            elicitation_id,
            "accept",
            {"Show the issue": ""},
            {"omnigent_question_images": {"Show the issue": [image.id]}},
        )
        assert accepted.status_code == 202, accepted.text
        await _wait_until(lambda: bool(delivered.calls))
        assert delivered.image_calls == [[image.id]]
        items = await client.get(f"/v1/sessions/{session_id}/items")
        assert items.status_code == 200, items.text
        answers = [
            item
            for item in items.json()["data"]
            if item.get("role") == "user"
            and any(
                block.get("text", "").startswith("[System: Question answered]\n")
                for block in item.get("content", [])
            )
        ]
        assert len(answers) == 1
        assert {"type": "input_image", "file_id": image.id} in answers[0]["content"]
    finally:
        await collector.stop()


async def test_async_question_image_delivery_dispatches_image_with_file_store(
    client: httpx.AsyncClient,
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session_id = await _create_agent_session(client, "test-async-image-dispatch")
    runner_client = object()
    file_store = object()
    artifact_store = object()
    dispatched: list[tuple[list[dict[str, Any]], object, object]] = []

    async def bound_runner(*_args: object) -> object:
        return runner_client

    async def dispatch(*args: Any, **kwargs: Any) -> Any:
        dispatched.append(
            (args[2].data["content"], kwargs["file_store"], kwargs["artifact_store"])
        )
        return orchestration._SessionEventDispatchResult(item_id=None, pending_id=None)

    import omnigent.runtime as runtime

    monkeypatch.setattr(orchestration, "_get_runner_client", bound_runner)
    monkeypatch.setattr(orchestration, "_ensure_runner_relay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestration, "_dispatch_session_event_to_runner", dispatch)
    monkeypatch.setattr(runtime, "get_file_store", lambda: file_store)
    monkeypatch.setattr(runtime, "get_artifact_store", lambda: artifact_store)

    delivered = await orchestration._post_system_message(
        session_id,
        "[System: answers to your question card]",
        conversation_store=conv_store,
        runner_router=None,
        image_ids=["image-a"],
    )

    assert delivered
    assert dispatched == [
        (
            [
                {"type": "input_text", "text": "[System: answers to your question card]"},
                {"type": "input_image", "file_id": "image-a"},
            ],
            file_store,
            artifact_store,
        )
    ]

    async def failed_dispatch(*_args: Any, **_kwargs: Any) -> Any:
        return orchestration._SessionEventDispatchResult(
            item_id="failure-item", pending_id=None, forwarded=False
        )

    monkeypatch.setattr(orchestration, "_dispatch_session_event_to_runner", failed_dispatch)
    assert not await orchestration._post_system_message(
        session_id,
        "[System: answers to your question card]",
        conversation_store=conv_store,
        runner_router=None,
        image_ids=["image-a"],
    )


async def test_restored_answered_card_redelivers_its_image_metadata(
    client: httpx.AsyncClient,
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session_id = await _create_agent_session(client, "test-async-image-restoration")
    now = int(time.time())
    conv_store.add_detached_card(
        DetachedCard(
            elicitation_id="restored-image-card",
            session_id=session_id,
            kind="question",
            state="answered",
            mirror=False,
            params={},
            payload={"qid": "q1", "questions": [{"question": "Show the issue"}]},
            grant_key=None,
            verdict={
                "action": "accept",
                "content": {"Show the issue": ""},
                "_meta": {"omnigent_question_images": {"Show the issue": ["image-restored"]}},
            },
            delivery_text="[System: answers to your question card #q1]",
            created_at=now,
            expires_at=now + 60,
        )
    )
    delivered = _DeliveryRecorder()
    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", delivered)

    await hooks_routes.restore_detached_cards(conv_store, runner_router=None)
    await _wait_until(lambda: bool(delivered.calls))
    await _wait_until(lambda: not conv_store.list_detached_cards())

    assert delivered.calls == [(session_id, "[System: answers to your question card #q1]")]
    assert delivered.image_calls == [["image-restored"]]


async def test_failed_dispatch_does_not_mark_question_comments_addressed(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session_id = await _create_agent_session(client, "test-async-comment-dispatch-failure")
    updated: list[str] = []

    class Comments:
        def get(self, comment_id: str, owner_session_id: str) -> object | None:
            return (
                object() if (comment_id, owner_session_id) == ("comment-a", session_id) else None
            )

        def update_comment(self, comment_id: str, *_args: Any, **_kwargs: Any) -> None:
            updated.append(comment_id)

    async def runner(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def relay(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def failed_dispatch(*_args: Any, **_kwargs: Any) -> Any:
        return orchestration._SessionEventDispatchResult(
            item_id="failure-item", pending_id=None, forwarded=False
        )

    import omnigent.runtime as runtime

    monkeypatch.setattr(runtime, "get_comment_store", lambda: Comments())
    monkeypatch.setattr(events_routes, "_get_runner_client", runner)
    monkeypatch.setattr(events_routes, "_ensure_runner_relay_ready", relay)
    monkeypatch.setattr(events_routes, "_dispatch_session_event_to_runner", failed_dispatch)
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "reply"}],
                "omnigent_comment_ids": ["comment-a"],
            },
        },
    )

    assert response.status_code == 409, response.text
    assert updated == []


async def test_async_question_shape_errors_and_many_questions(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shape violations are 400 with nothing published; six questions are fine."""
    delivered = _DeliveryRecorder()
    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", delivered)

    _, session_id = await _create_agent_session(client, "test-async-question-shape")
    url = f"/v1/sessions/{session_id}/async-questions"
    bad_bodies: list[dict[str, Any]] = [
        {"questions": []},
        {"questions": [{"header": "no text"}]},
        {"questions": [{"question": ""}]},
        {"questions": [{"question": "ok", "options": [{"description": "no label"}]}]},
        {"questions": [{"question": "ok", "options": "nope"}]},
        {"questions": [{"question": "ok", "multiSelect": "yes"}]},
        {"questions": [{"question": "ok", "header": 7}]},
        {"questions": [{"question": "ok"}], "context": 5},
        {"questions": "nope"},
    ]
    for bad in bad_bodies:
        resp = await client.post(url, json=bad)
        assert resp.status_code == 400, f"{bad!r} -> {resp.status_code} {resp.text}"
    assert pending_elicitations.count_for(session_id) == 0

    collector = await start_session_stream_collector(session_id)
    try:
        six = [{"question": f"Question {index}?"} for index in range(6)]
        resp = await client.post(url, json={"questions": six})
        assert resp.status_code == 200, resp.text
        event = await _next_elicitation(collector)
        questions = event["params"]["ask_user_question"]["questions"]
        assert len(questions) == 6
        assert [q["question"] for q in questions] == [f"Question {index}?" for index in range(6)]
        verdict = await _post_approval(client, session_id, event["elicitation_id"], "decline")
        assert verdict.status_code == 202, verdict.text
    finally:
        await collector.stop()


async def test_child_async_question_is_not_mirrored_or_notified(
    client: httpx.AsyncClient,
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child's card stays on the child stream/snapshot; the notifier ignores it."""
    delivered = _DeliveryRecorder()
    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", delivered)

    agent_id, parent_id = await _create_agent_session(client, "test-async-question-child")
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:asks",
        parent_conversation_id=parent_id,
        agent_id=agent_id,
    )

    parent_collector = await start_session_stream_collector(parent_id)
    child_collector = await start_session_stream_collector(child.id)
    wakes: list[str] = []

    async def _record_wake(parent: str, _child: Any, _notice: str) -> bool:
        wakes.append(parent)
        return True

    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=_record_wake,
        loop=asyncio.get_running_loop(),
    )
    pending_elicitations.set_elicitation_observer(notifier.observe)
    try:
        resp = await client.post(
            f"/v1/sessions/{child.id}/async-questions",
            json={"questions": [{"question": "Pick one"}]},
        )
        assert resp.status_code == 200, resp.text
        event = await _next_elicitation(child_collector)

        # The parent stream never sees the card; the parent snapshot never
        # replays it; the notifier never arms for it.
        await parent_collector.assert_no_event(0.2)
        snapshot = await client.get(f"/v1/sessions/{parent_id}")
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["pending_elicitations"] == []
        with notifier._lock:
            assert event["elicitation_id"] not in notifier._notified
        assert wakes == []

        verdict = await _post_approval(client, child.id, event["elicitation_id"], "decline")
        assert verdict.status_code == 202, verdict.text
    finally:
        pending_elicitations.set_elicitation_observer(None)
        await parent_collector.stop()
        await child_collector.stop()


async def test_async_question_decline_delivers_dismissed_notice(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declining posts the dismissed notice; the park reports once."""
    delivered = _DeliveryRecorder()
    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", delivered)

    _, session_id = await _create_agent_session(client, "test-async-question-decline")
    collector = await start_session_stream_collector(session_id)
    try:
        resp = await client.post(
            f"/v1/sessions/{session_id}/async-questions",
            json={"questions": [{"question": "Continue?"}]},
        )
        assert resp.status_code == 200, resp.text
        qid = resp.json()["qid"]
        event = await _next_elicitation(collector)

        verdict = await _post_approval(client, session_id, event["elicitation_id"], "decline")
        assert verdict.status_code == 202, verdict.text
        await _wait_until(lambda: bool(delivered.calls))
        assert delivered.calls == [
            (
                session_id,
                f"[System: the user dismissed question card #{qid} without answering.]",
            )
        ]
    finally:
        await collector.stop()


async def test_async_question_timeout_posts_nothing(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A park that expires posts no message to the asking session."""
    delivered = _DeliveryRecorder()
    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", delivered)

    real_start = orchestration.start_detached_elicitation

    def _short_park(session_id: str, params: Any, **kwargs: Any) -> str:
        return real_start(session_id, params, **{**kwargs, "timeout_s": 0.05})

    monkeypatch.setattr(hooks_routes, "start_detached_elicitation", _short_park)

    _, session_id = await _create_agent_session(client, "test-async-question-timeout")
    resp = await client.post(
        f"/v1/sessions/{session_id}/async-questions",
        json={"questions": [{"question": "Anyone there?"}]},
    )
    assert resp.status_code == 200, resp.text

    async def _drained() -> None:
        while sessions_route._detached_elicitation_tasks:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_drained(), timeout=3.0)
    assert delivered.calls == []


async def test_async_question_free_text_is_normalized(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A question without options normalizes to an empty-options free-text row."""
    delivered = _DeliveryRecorder()
    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", delivered)

    _, session_id = await _create_agent_session(client, "test-async-question-free-text")
    collector = await start_session_stream_collector(session_id)
    try:
        resp = await client.post(
            f"/v1/sessions/{session_id}/async-questions",
            json={"questions": [{"question": "What should I do next?"}]},
        )
        assert resp.status_code == 200, resp.text
        qid = resp.json()["qid"]
        event = await _next_elicitation(collector)
        assert event["params"]["ask_user_question"]["questions"] == [
            {
                "question": "What should I do next?",
                "header": "",
                "options": [],
                "multiSelect": False,
            }
        ]

        verdict = await _post_approval(
            client,
            session_id,
            event["elicitation_id"],
            "accept",
            content={"What should I do next?": "Ship it"},
        )
        assert verdict.status_code == 202, verdict.text
        await _wait_until(lambda: bool(delivered.calls))
        assert delivered.calls == [
            (
                session_id,
                f"[System: answers to your question card #{qid}]\n"
                "What should I do next? → Ship it",
            )
        ]
    finally:
        await collector.stop()


async def test_delivery_retry_recovers_after_transient_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A False first delivery retries and succeeds without a notice."""
    attempts = 0
    persisted: list[str] = []

    async def _post_once_missing(
        session_id: str,
        text: str,
        *,
        conversation_store: Any,
        runner_router: Any,
        confirm_delivery: bool,
    ) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts >= 2

    async def _no_sleep(_seconds: float) -> None:
        return

    async def _record_persist(session_id: str, text: str, store: Any, *, status: str) -> None:
        persisted.append(text)

    monkeypatch.setattr(orchestration, "_post_system_message", _post_once_missing)
    monkeypatch.setattr(orchestration, "_delivery_retry_sleep", _no_sleep)
    monkeypatch.setattr(orchestration, "_persist_answer_delivery_notice", _record_persist)

    text = "[System: answers to your question card #q1a2b3c]"
    assert await orchestration._deliver_with_retry(
        "conv_test", text, conversation_store=object(), runner_router=None
    )
    assert attempts == 2
    assert persisted == []


async def test_delivery_retry_persists_notice_on_final_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exhausted retry window logs + persists the re-ask notice."""
    attempts = 0
    persisted: list[tuple[str, str]] = []

    async def _post_never(
        session_id: str,
        text: str,
        *,
        conversation_store: Any,
        runner_router: Any,
        confirm_delivery: bool,
    ) -> bool:
        nonlocal attempts
        attempts += 1
        return False

    async def _no_sleep(_seconds: float) -> None:
        return

    async def _record_persist(session_id: str, text: str, store: Any, *, status: str) -> None:
        assert status == "undelivered"
        persisted.append((session_id, text))

    monkeypatch.setattr(orchestration, "_post_system_message", _post_never)
    monkeypatch.setattr(orchestration, "_delivery_retry_sleep", _no_sleep)
    monkeypatch.setattr(orchestration, "_persist_answer_delivery_notice", _record_persist)
    # Collapse the retry window so the exhaustion branch is reachable fast.
    monkeypatch.setattr(orchestration, "_DELIVER_RETRY_BACKOFFS_S", (0.0,))
    monkeypatch.setattr(orchestration, "_DELIVER_RETRY_INTERVAL_S", 0.0)
    monkeypatch.setattr(orchestration, "_DELIVER_RETRY_MAX_S", 0.0)

    text = "[System: answers to your question card #q1a2b3c]"
    assert not await orchestration._deliver_with_retry(
        "conv_test", text, conversation_store=object(), runner_router=None
    )
    assert persisted == [("conv_test", text)]
    assert attempts >= 1


async def test_answer_survives_retry_window_until_runner_returns(
    client: httpx.AsyncClient,
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached answer remains owed after one exhausted window and dispatches once."""
    online = False
    first_attempt = asyncio.Event()
    delivered: list[str] = []

    async def _post(
        session_id: str,
        text: str,
        *,
        conversation_store: Any,
        runner_router: Any,
        confirm_delivery: bool = False,
    ) -> bool:
        first_attempt.set()
        if online:
            delivered.append(text)
            return True
        return False

    monkeypatch.setattr(orchestration, "_post_system_message", _post)
    monkeypatch.setattr(orchestration, "_DELIVER_RETRY_MAX_S", 0.0)
    monkeypatch.setattr(hooks_routes, "_CARD_RETRY_INTERVAL_S", 0.01)
    _, session_id = await _create_agent_session(client, "test-question-runner-return")
    response = await client.post(
        f"/v1/sessions/{session_id}/async-questions",
        json={"questions": [{"question": "Which DB?"}]},
    )
    assert response.status_code == 200
    elicitation_id = response.json()["elicitation_id"]
    verdict = await _post_approval(
        client, session_id, elicitation_id, "accept", content={"Which DB?": "SQLite"}
    )
    assert verdict.status_code == 202
    await asyncio.wait_for(first_attempt.wait(), timeout=3)
    assert any(card.elicitation_id == elicitation_id for card in conv_store.list_detached_cards())
    async with asyncio.timeout(3):
        while True:
            items = await client.get(f"/v1/sessions/{session_id}/items")
            waiting = [
                item for item in items.json()["data"] if item.get("code") == "answer_waiting"
            ]
            if waiting:
                break
            await asyncio.sleep(0.01)
    assert len(waiting) == 1
    assert "saved" in waiting[0]["message"]
    assert "retrying" in waiting[0]["message"]
    online = True
    await _wait_until(lambda: bool(delivered))
    assert delivered == [
        f"[System: answers to your question card #{response.json()['qid']}]\nWhich DB? → SQLite"
    ]
    async with asyncio.timeout(3):
        while True:
            items = await client.get(f"/v1/sessions/{session_id}/items")
            statuses = [
                item for item in items.json()["data"] if item.get("code") == "answer_delivered"
            ]
            if statuses:
                break
            await asyncio.sleep(0.01)
    assert len(statuses) == 1
    await _wait_until(
        lambda: all(
            card.elicitation_id != elicitation_id for card in conv_store.list_detached_cards()
        )
    )


async def test_answer_record_write_retries_before_delivery(
    client: httpx.AsyncClient,
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient database write failure cannot arm or lose an unrecorded verdict."""
    delivered = _DeliveryRecorder()
    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", delivered)
    monkeypatch.setattr(hooks_routes, "_CARD_RETRY_INTERVAL_S", 0.01)
    original = SqlAlchemyConversationStore.update_detached_card
    attempts = 0

    def _fail_once(self: SqlAlchemyConversationStore, elicitation_id: str, **fields: Any) -> None:
        nonlocal attempts
        if fields.get("state") == "answered":
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient store failure")
        original(self, elicitation_id, **fields)

    monkeypatch.setattr(SqlAlchemyConversationStore, "update_detached_card", _fail_once)
    _, session_id = await _create_agent_session(client, "test-answer-write-retry")
    response = await client.post(
        f"/v1/sessions/{session_id}/async-questions",
        json={"questions": [{"question": "Which DB?"}]},
    )
    assert response.status_code == 200
    elicitation_id = response.json()["elicitation_id"]
    verdict = await _post_approval(
        client, session_id, elicitation_id, "accept", content={"Which DB?": "SQLite"}
    )
    assert verdict.status_code == 202
    await _wait_until(lambda: bool(delivered.calls))
    assert attempts == 2
    assert "SQLite" in delivered.calls[0][1]
    await _wait_until(
        lambda: all(
            card.elicitation_id != elicitation_id for card in conv_store.list_detached_cards()
        )
    )


async def test_pending_delivery_exhaustion_does_not_post_answer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable runner should not turn a pending hint into a lost-answer error."""
    notices: list[str] = []

    async def _offline(*args: Any, **kwargs: Any) -> bool:
        return False

    async def _notice(session_id: str, text: str, store: Any, *, status: str) -> None:
        notices.append(text)

    monkeypatch.setattr(orchestration, "_post_system_message", _offline)
    monkeypatch.setattr(orchestration, "_persist_answer_delivery_notice", _notice)
    monkeypatch.setattr(orchestration, "_DELIVER_RETRY_MAX_S", 0.0)
    hooks_routes._start_background_delivery(
        "conv_pending",
        "Approval pending (#a123456): shell(pnpm test)",
        conversation_store=object(),
        runner_router=None,
        elicitation_id="elicit_pending",
    )
    task = hooks_routes._pending_delivery_tasks["elicit_pending"]
    assert not await asyncio.wait_for(task, timeout=3)
    assert notices == []


async def test_pending_delivery_is_cancelled_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending-notice delivery is registered where teardown cancels it."""
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def _blocking_delivery(
        session_id: str,
        text: str,
        *,
        conversation_store: Any,
        runner_router: Any,
        persist_failure: bool = True,
    ) -> bool:
        started.set()
        await blocker.wait()
        return True

    monkeypatch.setattr(hooks_routes, "_deliver_with_retry", _blocking_delivery)

    hooks_routes._start_background_delivery(
        "conv_shutdown",
        "[System: approval pending]",
        conversation_store=object(),
        runner_router=None,
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    pending = list(orchestration._detached_elicitation_tasks)
    assert len(pending) == 1

    await orchestration.cancel_detached_elicitation_tasks()

    assert pending[0].cancelled()
    assert pending[0] not in orchestration._detached_elicitation_tasks
