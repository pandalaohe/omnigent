"""Accepted web answers survive independently of harness delivery."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from omnigent.entities import MessageData
from omnigent.runtime import pending_elicitations, session_stream
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
)
from omnigent.server.routes import sessions as routes
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.sessions import routes_elicitations, routes_hooks
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_cards() -> Any:
    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


async def _session(client: httpx.AsyncClient) -> str:
    agent = await create_test_agent(client, "answer-history")
    response = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _records(client: httpx.AsyncClient, session_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/v1/sessions/{session_id}/items", params={"order": "asc"})
    assert response.status_code == 200, response.text
    records = []
    for item in response.json()["data"]:
        for block in item.get("content", []):
            text = block.get("text", "")
            if text.startswith("[System: Question answered]\n"):
                assert item["role"] == "user"
                records.append(json.loads(text.split("\n", 1)[1]))
    return records


@pytest.mark.parametrize("transport", ["events", "url"])
@pytest.mark.parametrize(
    "params,content,expected",
    [
        (
            {
                "message": "Claude has questions",
                "ask_user_question": {"questions": [{"question": "Which targets?"}]},
            },
            {"Which targets?": ["Phone", "Desktop"]},
            [{"question": "Which targets?", "answer": "Phone; Desktop"}],
        ),
        (
            {
                "message": "Codex needs input",
                "ask_user_question": {
                    "questions": [{"id": "q_stack", "question": "Which framework?"}]
                },
            },
            {"q_stack": "React\nKeep the tests."},
            [
                {
                    "question": "Which framework?",
                    "answer": "React\nKeep the tests.",
                    "id": "q_stack",
                }
            ],
        ),
        (
            {
                "message": "Choose a workspace",
                "requestedSchema": {
                    "type": "object",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "title": "Workspace directory",
                            "description": "Choose the directory to open.",
                        }
                    },
                },
            },
            {"directory": "/opt/work/example\nSecond line"},
            [
                {
                    "question": (
                        "Choose a workspace\nWorkspace directory\nChoose the directory to open."
                    ),
                    "answer": "/opt/work/example\nSecond line",
                }
            ],
        ),
        (
            {"message": "Claude wants to run the command"},
            {"feedback": "Only run the checks.\nKeep all output."},
            [
                {
                    "question": "Claude wants to run the command",
                    "answer": "Only run the checks.\nKeep all output.",
                }
            ],
        ),
    ],
    ids=["claude-question", "codex-input", "schema-form", "permission-feedback"],
)
async def test_answer_receipt_is_in_items_before_harness_delivery(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
    params: dict[str, Any],
    content: dict[str, Any],
    expected: list[dict[str, Any]],
) -> None:
    session_id = await _session(client)
    elicitation_id = "elicit_answer_example"
    session_stream.publish(
        session_id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": elicitation_id,
            "params": {"mode": "form", **params},
        },
    )

    async def offline(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_forward_approval_to_runner", offline)
    body = {"action": "accept", "content": content}
    if transport == "events":
        response = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "approval", "data": {"elicitation_id": elicitation_id, **body}},
        )
    else:
        response = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve", json=body
        )
    assert response.status_code == 202, response.text
    assert await _records(client, session_id) == [
        {"elicitation_id": elicitation_id, "action": "accept", "answers": expected}
    ]
    from omnigent.runner.tool_dispatch import _session_get_history_via_rest

    history = json.loads(
        await _session_get_history_via_rest(
            {"conversation_id": session_id, "content_max_chars": 10000}, client
        )
    )
    messages = [item["text"] for item in history["items"] if item.get("role") == "user"]
    assert json.loads(messages[-1].split("\n", 1)[1])["answers"] == expected
    # Retrying after the prompt resolved cannot append a second answer.
    response = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve", json=body
    )
    assert response.status_code == 202
    assert len(await _records(client, session_id)) == 1


@pytest.mark.parametrize("final_failure", [False, True])
async def test_claude_async_answer_is_saved_even_when_delivery_fails(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    final_failure: bool,
) -> None:
    session_id = await _session(client)
    delivered = asyncio.Event()

    async def offline(*args: Any, **kwargs: Any) -> bool:
        delivered.set()
        return False

    if final_failure:
        monkeypatch.setattr(orchestration, "_post_system_message", offline)
        monkeypatch.setattr(orchestration, "_DELIVER_RETRY_BACKOFFS_S", ())
        monkeypatch.setattr(orchestration, "_DELIVER_RETRY_MAX_S", 0)
    else:
        monkeypatch.setattr(routes_hooks, "_deliver_with_retry", offline)
    response = await client.post(
        f"/v1/sessions/{session_id}/async-questions",
        json={"questions": [{"question": "What next?"}]},
    )
    assert response.status_code == 200
    elicitation_id = response.json()["elicitation_id"]
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "approval",
            "data": {
                "elicitation_id": elicitation_id,
                "action": "accept",
                "content": {"What next?": "Check mobile first.\nThen review."},
            },
        },
    )
    assert response.status_code == 202, response.text
    await asyncio.wait_for(delivered.wait(), timeout=3)
    assert await _records(client, session_id) == [
        {
            "elicitation_id": elicitation_id,
            "action": "accept",
            "answers": [{"question": "What next?", "answer": "Check mobile first.\nThen review."}],
        }
    ]
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
    assert not any(item.get("code") == "answer_undelivered" for item in items.json()["data"])
    assert (await _records(client, session_id))[0]["answers"][0][
        "answer"
    ] == "Check mobile first.\nThen review."


async def test_claude_sdk_question_is_recorded_with_its_full_prompt(
    client: httpx.AsyncClient,
) -> None:
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: None)  # type: ignore[arg-type]
    question = "Which framework? " + "context " * 100
    params = adapter._permission_card(
        "AskUserQuestion",
        {"questions": [{"question": question, "options": [{"label": "React"}]}]},
    )
    session_id = await _session(client)
    session_stream.publish(
        session_id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_sdk",
            "params": params.model_dump(),
        },
    )
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "approval",
            "data": {
                "elicitation_id": "elicit_sdk",
                "action": "accept",
                "content": {question: "React\nKeep all tests."},
            },
        },
    )
    assert response.status_code == 202, response.text
    assert (await _records(client, session_id))[0]["answers"] == [
        {"question": question, "answer": "React\nKeep all tests."}
    ]


@pytest.mark.parametrize("secret_kind", ["question", "schema"])
async def test_secret_form_answers_are_redacted_in_history(
    client: httpx.AsyncClient,
    secret_kind: str,
) -> None:
    session_id = await _session(client)
    params: dict[str, Any] = {"mode": "form", "message": "Enter the value"}
    if secret_kind == "question":
        params["ask_user_question"] = {
            "questions": [{"id": "value", "question": "Private value", "isSecret": True}]
        }
    else:
        params["requestedSchema"] = {"properties": {"value": {"format": "password"}}}
    session_stream.publish(
        session_id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_secret",
            "params": params,
        },
    )
    response = await client.post(
        f"/v1/sessions/{session_id}/elicitations/elicit_secret/resolve",
        json={"action": "accept", "content": {"value": "fictional test value"}},
    )
    assert response.status_code == 202, response.text
    record = (await _records(client, session_id))[0]
    assert record["answers"][0]["answer"] == "[redacted]"
    assert "fictional test value" not in json.dumps(record)


async def test_foreign_prompt_does_not_create_an_answer_in_another_session(
    client: httpx.AsyncClient,
) -> None:
    owner = await _session(client)
    other = await _session(client)
    session_stream.publish(
        owner,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_foreign",
            "params": {"message": "Which framework?", "mode": "form"},
        },
    )
    response = await client.post(
        f"/v1/sessions/{other}/elicitations/elicit_foreign/resolve",
        json={"action": "accept", "content": {"answer": "React"}},
    )
    assert response.status_code == 202
    assert await _records(client, owner) == []
    assert await _records(client, other) == []


async def test_concurrent_answers_resolve_with_the_durably_saved_winner(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _session(client)
    elicitation_id = "elicit_competing_answers"
    future = asyncio.get_running_loop().create_future()
    _harness_elicitation_registry[elicitation_id] = future
    _harness_elicitation_owners[elicitation_id] = session_id
    _harness_parked_elicitations[elicitation_id] = _ParkedHarnessElicitation(
        session_id=session_id,
        tool_name=None,
        tool_input=None,
        resolved_elsewhere=asyncio.Event(),
        request_fingerprint="fingerprint_one",
    )
    session_stream.publish(
        session_id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": elicitation_id,
            "params": {"mode": "form", "message": "Pick one"},
        },
    )
    first_saved = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    accepted: list[dict[str, Any]] = []
    original_thread = asyncio.to_thread
    original_resolve = routes_elicitations._resolve_elicitation

    async def pause_first_append(operation: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(operation, "__name__", "") == "append" and len(args) == 2:
            item = args[1][0]
            if isinstance(item.data, MessageData) and item.data.content[0].get(
                "text", ""
            ).startswith("[System: Question answered]\n"):
                result = operation(*args, **kwargs)
                payload = json.loads(item.data.content[0]["text"].split("\n", 1)[1])
                if payload["answers"][0]["answer"] == "A":
                    first_saved.set()
                    await release_first.wait()
                return result
        return await original_thread(operation, *args, **kwargs)

    async def observe_second(target: str, data: dict[str, Any], *args: Any) -> None:
        if data.get("content") == {"answer": "B"}:
            second_started.set()
        await original_resolve(target, data, *args)
        accepted.append(data["content"])

    monkeypatch.setattr(orchestration.asyncio, "to_thread", pause_first_append)
    monkeypatch.setattr(routes_elicitations, "_resolve_elicitation", observe_second)
    url = f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve"
    first = asyncio.create_task(
        client.post(url, json={"action": "accept", "content": {"answer": "A"}})
    )
    second = None
    try:
        await asyncio.wait_for(first_saved.wait(), timeout=3)
        second = asyncio.create_task(
            client.post(url, json={"action": "accept", "content": {"answer": "B"}})
        )
        await asyncio.wait_for(second_started.wait(), timeout=3)
        release_first.set()
        responses = await asyncio.gather(first, second)
        assert all(response.status_code == 202 for response in responses)
        assert (await _records(client, session_id))[0]["answers"][0]["answer"] == "A"
        assert future.result().content == {"answer": "A"}
        assert _harness_pre_resolved_elicitations[elicitation_id].result.content == {"answer": "A"}
        assert accepted == [{"answer": "A"}, {"answer": "A"}]
        original_timestamp = _harness_pre_resolved_elicitations[elicitation_id].created_at
        _harness_elicitation_registry.pop(elicitation_id)
        _harness_elicitation_owners.pop(elicitation_id)
        retry = await client.post(url, json={"action": "accept", "content": {"answer": "B"}})
        assert retry.status_code == 202
        assert _harness_pre_resolved_elicitations[elicitation_id].result.content == {"answer": "A"}
        assert _harness_pre_resolved_elicitations[elicitation_id].created_at == original_timestamp
        replay = orchestration._consume_pre_resolved_harness_elicitation(
            session_id, elicitation_id, "fingerprint_one"
        )
        assert replay is not None
        assert replay.result.content == {"answer": "A"}
    finally:
        release_first.set()
        await asyncio.gather(
            first, *([second] if second is not None else []), return_exceptions=True
        )
        _harness_elicitation_registry.pop(elicitation_id, None)
        _harness_elicitation_owners.pop(elicitation_id, None)
        _harness_pre_resolved_elicitations.pop(elicitation_id, None)
        _harness_parked_elicitations.pop(elicitation_id, None)


async def test_failed_history_write_leaves_the_answer_retryable(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _session(client)
    elicitation_id = "elicit_retryable"
    future = asyncio.get_running_loop().create_future()
    _harness_elicitation_registry[elicitation_id] = future
    _harness_elicitation_owners[elicitation_id] = session_id
    session_stream.publish(
        session_id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": elicitation_id,
            "params": {"mode": "form", "message": "What next?"},
        },
    )
    original_append = SqlAlchemyConversationStore.append

    def fail_receipt(store: Any, *args: Any, **kwargs: Any) -> Any:
        if len(args) == 2 and any(
            isinstance(item.data, MessageData)
            and item.data.content[0].get("text", "").startswith("[System: Question answered]\n")
            for item in args[1]
        ):
            raise OSError("History temporarily unavailable")
        return original_append(store, *args, **kwargs)

    url = f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve"
    body = {"action": "accept", "content": {"answer": "Check mobile"}}
    try:
        with monkeypatch.context() as failing:
            failing.setattr(SqlAlchemyConversationStore, "append", fail_receipt)
            with pytest.raises(OSError, match="History temporarily unavailable"):
                await client.post(url, json=body)
        assert not future.done()
        assert pending_elicitations.lookup(elicitation_id) is not None
        assert await _records(client, session_id) == []
        retry = await client.post(url, json=body)
        assert retry.status_code == 202
        assert future.result().content == {"answer": "Check mobile"}
        assert (await _records(client, session_id))[0]["answers"][0]["answer"] == "Check mobile"
    finally:
        _harness_elicitation_registry.pop(elicitation_id, None)
        _harness_elicitation_owners.pop(elicitation_id, None)
        _harness_pre_resolved_elicitations.pop(elicitation_id, None)
