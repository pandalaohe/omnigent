"""A native sub-agent's kickoff prompt must appear once, not duplicated."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from omnigent._wrapper_labels import WRAPPER_LABEL_KEY
from omnigent.harness_plugins import (
    CLAUDE_NATIVE_CODING_AGENT,
    CODEX_NATIVE_CODING_AGENT,
)
from omnigent.server.routes import sessions as sessions_routes
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

KICKOFF = "Start the assigned task [kickoff-marker-7a1f]"
KICKOFF_MARKER = "kickoff-marker-7a1f"

NATIVE_WRAPPER_AGENTS = [
    pytest.param(agent.agent_name, agent.wrapper_label, id=agent.key)
    for agent in (CLAUDE_NATIVE_CODING_AGENT, CODEX_NATIVE_CODING_AGENT)
]


@pytest.fixture()
def bound_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StubRunner:
        async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(200, json={}, request=httpx.Request("POST", "http://runner"))

    runner = _StubRunner()

    async def _resolve_bound_runner(
        session_id: str,
        runner_router: Any,
        *,
        conversation: Any = None,
    ) -> _StubRunner:
        assert conversation is None or conversation.id == session_id
        return runner

    async def _skip_relay_readiness(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_routes, "_get_runner_client", _resolve_bound_runner)
    monkeypatch.setattr(sessions_routes, "_ensure_runner_relay_ready", _skip_relay_readiness)


def _kickoff_item(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }


async def _create_subagent_with_kickoff(
    client: httpx.AsyncClient,
    *,
    parent_agent_name: str,
    child_agent_name: str,
    kickoff: str,
    parent_runner_id: str | None = None,
    db_uri: str | None = None,
    include_initial_items: bool = True,
) -> dict[str, Any]:
    parent_agent = await create_test_agent(client, name=parent_agent_name)
    parent = await client.post("/v1/sessions", json={"agent_id": parent_agent["id"]})
    assert parent.status_code == 201, parent.text
    if parent_runner_id is not None:
        from omnigent.stores.conversation_store.sqlalchemy_store import (
            SqlAlchemyConversationStore,
        )

        assert db_uri is not None
        assert SqlAlchemyConversationStore(db_uri).set_runner_id(
            parent.json()["id"], parent_runner_id
        )

    child_agent = await create_test_agent(client, name=child_agent_name)
    body: dict[str, Any] = {
        "agent_id": child_agent["id"],
        "parent_session_id": parent.json()["id"],
        "title": "impl:task-1",
    }
    if include_initial_items:
        body["initial_items"] = [_kickoff_item(kickoff)]
    child = await client.post("/v1/sessions", json=body)
    assert child.status_code == 201, child.text
    return child.json()


async def _simulate_transcript_forwarder_echo(
    client: httpx.AsyncClient, session_id: str, text: str
) -> None:
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
                "response_id": "resp_claude_echo",
            },
        },
    )
    assert resp.status_code in (200, 201, 202), resp.text


async def _kickoff_message_count(client: httpx.AsyncClient, session_id: str, marker: str) -> int:
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    return sum(
        1
        for item in items
        if item.get("type") == "message"
        and item.get("role") == "user"
        and marker in json.dumps(item.get("content", []))
    )


async def test_plain_subagent_kickoff_persisted_once(
    client: httpx.AsyncClient,
    bound_runner: None,
) -> None:
    child = await _create_subagent_with_kickoff(
        client,
        parent_agent_name="orch-plain",
        child_agent_name="impl-plain",
        kickoff=KICKOFF,
    )
    assert await _kickoff_message_count(client, child["id"], KICKOFF_MARKER) == 1


@pytest.mark.parametrize("agent_name,wrapper_value", NATIVE_WRAPPER_AGENTS)
async def test_native_subagent_kickoff_appears_once(
    client: httpx.AsyncClient,
    bound_runner: None,
    agent_name: str,
    wrapper_value: str,
) -> None:
    child = await _create_subagent_with_kickoff(
        client,
        parent_agent_name=f"orch-{agent_name}",
        child_agent_name=agent_name,
        kickoff=KICKOFF,
    )
    assert child["labels"].get(WRAPPER_LABEL_KEY) == wrapper_value

    await _simulate_transcript_forwarder_echo(client, child["id"], KICKOFF)

    assert await _kickoff_message_count(client, child["id"], KICKOFF_MARKER) == 1


@pytest.mark.parametrize("agent_name,wrapper_value", NATIVE_WRAPPER_AGENTS)
async def test_native_subagent_session_init_precedes_terminal_launch(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    agent_name: str,
    wrapper_value: str,
) -> None:
    """The child's session-init envelope reaches the runner before the launch.

    The kickoff dispatch launches the child's native terminal; if the init
    envelope lands after it, the launch misses the global instructions and
    the recorded workspace state. The handshake must run first, carry the
    stored global instructions, and run exactly once — a second sequential
    init would only repeat it.
    """
    from omnigent.runtime import _globals
    from omnigent.stores.global_instructions_store.sqlalchemy_store import (
        SqlAlchemyGlobalInstructionsStore,
    )

    global_text = "prefer rg [global-init-marker]"
    store = SqlAlchemyGlobalInstructionsStore(db_uri)
    store.save(global_text, created_by=None)
    monkeypatch.setattr(_globals, "_global_instructions_store", store)

    posts: list[tuple[str, Any]] = []

    class _RecordingRunner:
        async def post(self, path: str, *, json: Any = None, **kwargs: Any) -> httpx.Response:
            posts.append((path, json))
            return httpx.Response(
                200,
                json={},
                request=httpx.Request("POST", f"http://runner{path}"),
            )

    runner = _RecordingRunner()

    async def _resolve_bound_runner(*args: Any, **kwargs: Any) -> _RecordingRunner:
        return runner

    async def _skip_relay_readiness(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_routes, "_get_runner_client", _resolve_bound_runner)
    monkeypatch.setattr(sessions_routes, "_ensure_runner_relay_ready", _skip_relay_readiness)

    child = await _create_subagent_with_kickoff(
        client,
        parent_agent_name=f"orch-init-order-{agent_name}",
        child_agent_name=agent_name,
        kickoff=KICKOFF,
        parent_runner_id="runner-init-order",
        db_uri=db_uri,
    )
    assert child["labels"].get(WRAPPER_LABEL_KEY) == wrapper_value
    child_id = child["id"]

    init_indexes = [
        index
        for index, (path, body) in enumerate(posts)
        if path == "/v1/sessions" and isinstance(body, dict) and body.get("session_id") == child_id
    ]
    ensure_indexes = [
        index
        for index, (path, _) in enumerate(posts)
        if path == f"/v1/sessions/{child_id}/resources/terminals"
    ]
    assert len(init_indexes) == 1, posts
    assert ensure_indexes, posts
    assert init_indexes[0] < ensure_indexes[0], posts

    init_body = posts[init_indexes[0]][1]
    assert init_body["session_init"]["snapshot"]["global_instructions"] == global_text, init_body


def _is_child_init(body: Any) -> bool:
    """A child's init envelope carries the parent link; a top-level one does not."""
    return (
        isinstance(body, dict)
        and isinstance(body.get("session_init"), dict)
        and body["session_init"].get("snapshot", {}).get("parent_session_id") is not None
    )


@pytest.mark.parametrize("agent_name,wrapper_value", NATIVE_WRAPPER_AGENTS)
async def test_native_subagent_dispatch_waits_for_session_init(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    agent_name: str,
    wrapper_value: str,
) -> None:
    """The kickoff dispatch must not reach the runner while the init is in flight.

    The fake runner holds the child's session-init response. Until it is
    released no terminal-ensure or event POST may reach the runner: the
    dispatch that launches the native terminal runs only after the handshake
    completes.
    """
    released = asyncio.Event()
    init_seen = asyncio.Event()
    posts: list[tuple[str, Any]] = []

    class _BlockingRunner:
        async def post(self, path: str, *, json: Any = None, **kwargs: Any) -> httpx.Response:
            posts.append((path, json))
            if path == "/v1/sessions" and _is_child_init(json):
                init_seen.set()
                await released.wait()
            return httpx.Response(
                200,
                json={},
                request=httpx.Request("POST", f"http://runner{path}"),
            )

    runner = _BlockingRunner()

    async def _resolve_bound_runner(*args: Any, **kwargs: Any) -> _BlockingRunner:
        return runner

    async def _skip_relay_readiness(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_routes, "_get_runner_client", _resolve_bound_runner)
    monkeypatch.setattr(sessions_routes, "_ensure_runner_relay_ready", _skip_relay_readiness)

    create_task = asyncio.create_task(
        _create_subagent_with_kickoff(
            client,
            parent_agent_name=f"orch-init-wait-{agent_name}",
            child_agent_name=agent_name,
            kickoff=KICKOFF,
            parent_runner_id="runner-init-wait",
            db_uri=db_uri,
        )
    )
    await asyncio.wait_for(init_seen.wait(), timeout=10.0)
    assert not any(path.endswith(("/resources/terminals", "/events")) for path, _ in posts), posts

    released.set()
    child = await asyncio.wait_for(create_task, timeout=30.0)
    assert child["labels"].get(WRAPPER_LABEL_KEY) == wrapper_value
    child_id = child["id"]

    init_indexes = [
        index
        for index, (path, body) in enumerate(posts)
        if path == "/v1/sessions" and isinstance(body, dict) and body.get("session_id") == child_id
    ]
    ensure_indexes = [
        index
        for index, (path, _) in enumerate(posts)
        if path == f"/v1/sessions/{child_id}/resources/terminals"
    ]
    assert len(init_indexes) == 1, posts
    assert ensure_indexes, posts
    assert init_indexes[0] < ensure_indexes[0], posts


@pytest.mark.parametrize("agent_name,wrapper_value", NATIVE_WRAPPER_AGENTS)
async def test_native_subagent_failed_early_init_is_retried_by_notify(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    agent_name: str,
    wrapper_value: str,
) -> None:
    """A failed pre-dispatch init does not stick; the post-create notify retries it.

    The runner rejects the child's first init with a 5xx. The create still
    succeeds, and the notify must deliver a second, successful envelope that
    carries the stored global instructions instead of being skipped.
    """
    from omnigent.runtime import _globals
    from omnigent.stores.global_instructions_store.sqlalchemy_store import (
        SqlAlchemyGlobalInstructionsStore,
    )

    global_text = "prefer rg [retry-init-marker]"
    store = SqlAlchemyGlobalInstructionsStore(db_uri)
    store.save(global_text, created_by=None)
    monkeypatch.setattr(_globals, "_global_instructions_store", store)

    posts: list[tuple[str, Any]] = []
    child_init_attempts = 0

    class _FailOnceRunner:
        async def post(self, path: str, *, json: Any = None, **kwargs: Any) -> httpx.Response:
            nonlocal child_init_attempts
            posts.append((path, json))
            if path == "/v1/sessions" and _is_child_init(json):
                child_init_attempts += 1
                if child_init_attempts == 1:
                    return httpx.Response(
                        500,
                        json={"error": "init boom"},
                        request=httpx.Request("POST", f"http://runner{path}"),
                    )
            return httpx.Response(
                200,
                json={},
                request=httpx.Request("POST", f"http://runner{path}"),
            )

    runner = _FailOnceRunner()

    async def _resolve_bound_runner(*args: Any, **kwargs: Any) -> _FailOnceRunner:
        return runner

    async def _skip_relay_readiness(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_routes, "_get_runner_client", _resolve_bound_runner)
    monkeypatch.setattr(sessions_routes, "_ensure_runner_relay_ready", _skip_relay_readiness)

    child = await _create_subagent_with_kickoff(
        client,
        parent_agent_name=f"orch-init-retry-{agent_name}",
        child_agent_name=agent_name,
        kickoff=KICKOFF,
        parent_runner_id="runner-init-retry",
        db_uri=db_uri,
    )
    assert child["labels"].get(WRAPPER_LABEL_KEY) == wrapper_value
    child_id = child["id"]

    child_inits = [
        body
        for path, body in posts
        if path == "/v1/sessions" and isinstance(body, dict) and body.get("session_id") == child_id
    ]
    assert child_init_attempts == 2, posts
    assert len(child_inits) == 2, posts
    retried = child_inits[-1]
    assert retried["session_init"]["snapshot"]["global_instructions"] == global_text, retried


@pytest.mark.parametrize(
    "include_initial_items",
    [pytest.param(False, id="no-initial-items"), pytest.param(True, id="with-initial-items")],
)
async def test_child_create_broken_envelope_still_sends_id_only_notify(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    include_initial_items: bool,
) -> None:
    """A failed envelope build must still notify the bound runner.

    The global-instructions read feeds both the initializer and the direct
    notify. When it raises, the create must not silently skip the session-init
    POST: the direct path degrades to the id-only body so the runner learns the
    session exists before its first event.
    """
    from omnigent.runtime import _globals

    class _ExplodingStore:
        def current(self) -> Any:
            raise RuntimeError("instructions store unavailable")

    monkeypatch.setattr(_globals, "_global_instructions_store", _ExplodingStore())

    posts: list[tuple[str, Any]] = []

    class _RecordingRunner:
        async def post(self, path: str, *, json: Any = None, **kwargs: Any) -> httpx.Response:
            posts.append((path, json))
            return httpx.Response(
                200,
                json={},
                request=httpx.Request("POST", f"http://runner{path}"),
            )

    runner = _RecordingRunner()

    async def _resolve_bound_runner(*args: Any, **kwargs: Any) -> _RecordingRunner:
        return runner

    async def _skip_relay_readiness(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_routes, "_get_runner_client", _resolve_bound_runner)
    monkeypatch.setattr(sessions_routes, "_ensure_runner_relay_ready", _skip_relay_readiness)

    child = await _create_subagent_with_kickoff(
        client,
        parent_agent_name="orch-init-fallback",
        child_agent_name="impl-init-fallback",
        kickoff=KICKOFF,
        parent_runner_id="runner-init-fallback",
        db_uri=db_uri,
        include_initial_items=include_initial_items,
    )
    child_id = child["id"]

    child_inits = [
        body
        for path, body in posts
        if path == "/v1/sessions" and isinstance(body, dict) and body.get("session_id") == child_id
    ]
    assert len(child_inits) == 1, posts
    assert child_inits[0] == {
        "session_id": child_id,
        "agent_id": child["agent_id"],
        "sub_agent_name": None,
    }, child_inits[0]
