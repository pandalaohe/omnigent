"""Server succession endpoint and phase-engine tests.

The route moves a mother's live children in one store transaction and runs
the receipt through ``rekeyed → opened → released → cards_closed → archived
→ done`` inline. The runner is faked: the tests assert the runner calls
(succession body, archive lineage, release) and the server-side effects
(receipt phases, notice, wake, publishes, archive without teardown).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import generate_task_id
from omnigent.entities import MessageData, NewConversationItem, SessionPeerMessage
from omnigent.runtime import pending_elicitations
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import session_succession as succession_module
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.routes import sessions as sessions_facade
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store import (
    HANDOVER_ITEM_LABEL_KEY,
    SUCCEEDED_BY_LABEL_KEY,
    SUCCEEDS_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.peer_message_store.sqlalchemy_store import (
    SqlAlchemyPeerMessageStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)

pytestmark = pytest.mark.asyncio

RUNNER_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f01"
RUNNER_ID_B = "b1b2c3d4e5f60718293a4b5c6d7e8f02"
QUESTION_ID = "elicit_q1"


class _FakeRunnerClient:
    """Records runner POSTs and answers succession/release with canned bodies."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any] | None]] = []
        self.succession_status = 200
        self.succession_failures = 0
        self.release_failures = 0
        self.dropped: list[dict[str, Any]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del timeout
        self.posts.append((url, json))
        if url.endswith("/succession"):
            if self.succession_failures > 0:
                self.succession_failures -= 1
                return httpx.Response(503, json={"error": "busy"})
            if self.succession_status == 409:
                return httpx.Response(409, json={"error": "target_not_ready"})
            if self.succession_status >= 400:
                return httpx.Response(self.succession_status, json={"error": "bad"})
            return httpx.Response(200, json={"status": "rekeyed", "dropped": list(self.dropped)})
        if url.endswith("/succession/release"):
            if self.release_failures > 0:
                self.release_failures -= 1
                return httpx.Response(503, json={"error": "busy"})
            return httpx.Response(200, json={"status": "released", "delivered": 0})
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture(autouse=True)
def _clean_pending_elicitations() -> Iterator[None]:
    """Keep the module-global elicitation index from leaking across tests."""
    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyConversationStore:
    """A conversation store backed by the per-test SQLite database."""
    return SqlAlchemyConversationStore(db_uri)


class _FakeRunnerPool:
    """One fake runner client per bound runner id."""

    def __init__(self) -> None:
        self.by_runner: dict[str, _FakeRunnerClient] = {}

    def for_runner(self, runner_id: str) -> _FakeRunnerClient:
        """Return (and lazily create) the client for one runner."""
        return self.by_runner.setdefault(runner_id, _FakeRunnerClient())


@pytest.fixture()
def runners(monkeypatch: pytest.MonkeyPatch) -> _FakeRunnerPool:
    """Install a fake runner client resolved by the session's bound runner."""
    pool = _FakeRunnerPool()

    async def _get_runner_client(
        _session_id: str,
        _runner_router: Any,
        *,
        conversation: Any = None,
    ) -> _FakeRunnerClient:
        runner_id = conversation.runner_id if conversation is not None else RUNNER_ID
        return pool.for_runner(runner_id)

    monkeypatch.setattr(sessions_facade, "_get_runner_client", _get_runner_client)
    return pool


@pytest.fixture()
def runner(runners: _FakeRunnerPool) -> _FakeRunnerClient:
    """The fake client for the default test runner."""
    return runners.for_runner(RUNNER_ID)


@pytest.fixture()
def wake(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Capture the successor wake without driving the runner event path."""
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(succession_module, "_post_system_message", mock)
    return mock


@pytest.fixture()
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Capture every SSE event published on a session stream."""
    events: list[tuple[str, dict[str, Any]]] = []

    def _capture(
        conversation_id: str,
        event: dict[str, Any],
        track_pending: bool = True,
    ) -> None:
        del track_pending
        events.append((conversation_id, event))

    monkeypatch.setattr(sessions_facade.session_stream, "publish", _capture)
    return events


@pytest.fixture()
def succession_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Any,
) -> FastAPI:
    """App with real stores plus a durable peer-message store."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        peer_message_store=SqlAlchemyPeerMessageStore(db_uri),
    )


@pytest_asyncio.fixture()
async def client(succession_app: FastAPI) -> Any:
    """Async client wired to the succession app."""
    transport = httpx.ASGITransport(app=succession_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _create(
    store: SqlAlchemyConversationStore,
    *,
    parent: str | None = None,
    title: str | None = None,
    runner_id: str = RUNNER_ID,
) -> str:
    """Create a conversation, bound to a test runner, and return its id."""
    conversation = store.create_conversation(parent_conversation_id=parent, title=title)
    store.set_runner_id(conversation.id, runner_id)
    return conversation.id


def _question_event() -> dict[str, Any]:
    """An open async question card as the elicitation index stores it."""
    return {
        "type": "response.elicitation_request",
        "elicitation_id": QUESTION_ID,
        "params": {
            "async_kind": "question",
            "message": "Which database?",
            "ask_user_question": {"questions": [{"question": "Postgres or SQLite?"}]},
        },
    }


def _succession_notices(store: SqlAlchemyConversationStore, session_id: str) -> list[Any]:
    """All persisted succession notices on a session."""
    page = store.list_items(session_id, limit=200, order="desc", type="error")
    return [item for item in page.data if getattr(item.data, "code", None) == "session_succession"]


async def test_succession_happy_path_moves_children_and_completes(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runner: _FakeRunnerClient,
    wake: AsyncMock,
    published: list[tuple[str, dict[str, Any]]],
) -> None:
    """The whole phase sequence runs, publishes, archives and wakes once."""
    old = _create(store, title="mother")
    new = _create(store, title="successor")
    child_a = _create(store, parent=old, title="A")
    grandchild = _create(store, parent=child_a, title="A1")
    child_b = _create(store, parent=old, title="B")
    archived_child = _create(store, parent=old, title="C")
    store.update_conversation(archived_child, archived=True)

    handover = store.append(
        old,
        [
            NewConversationItem(
                type="message",
                response_id=generate_task_id(),
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Continue the migration."}],
                ),
                stable_id="a" * 32,
            )
        ],
    )[0]
    store.set_labels(old, {HANDOVER_ITEM_LABEL_KEY: handover.id})
    pending_elicitations.record_publish(old, _question_event())

    response = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "done"
    assert body["phase"] == "done"
    assert set(body["moved_ids"]) == {child_a, grandchild, child_b}

    for session_id in (child_a, child_b):
        moved = store.get_conversation(session_id)
        assert moved is not None
        assert moved.parent_conversation_id == new
        assert moved.root_conversation_id == new
    grandchild_row = store.get_conversation(grandchild)
    assert grandchild_row is not None
    assert grandchild_row.root_conversation_id == new
    stayed = store.get_conversation(archived_child)
    assert stayed is not None
    assert stayed.parent_conversation_id == old

    old_row = store.get_conversation(old)
    new_row = store.get_conversation(new)
    assert old_row is not None and new_row is not None
    assert old_row.archived is True
    assert old_row.archive_close_requested_revision is None
    assert old_row.labels[SUCCEEDED_BY_LABEL_KEY] == new
    assert new_row.labels[SUCCEEDS_LABEL_KEY] == old
    assert store.list_pending_archive_closes() == []

    receipt = store.get_succession(old, new)
    assert receipt is not None
    assert receipt.phase == "done"
    assert [question["elicitation_id"] for question in receipt.questions or []] == [QUESTION_ID]
    assert receipt.opening is not None
    assert "Previous session" in receipt.opening["text"]

    succession_posts = [payload for url, payload in runner.posts if url.endswith("/succession")]
    assert len(succession_posts) == 1
    assert succession_posts[0] is not None
    assert succession_posts[0]["target_session_id"] == new
    assert set(succession_posts[0]["moved_ids"]) == {child_a, grandchild, child_b}
    archive_states = succession_posts[0]["archive_states"]
    assert set(archive_states) == {child_a, grandchild, child_b}
    for states in archive_states.values():
        scope_ids = {state["scope_id"] for state in states}
        assert old not in scope_ids
        assert {child_a, grandchild, child_b, new} & scope_ids

    release_posts = [url for url, _payload in runner.posts if url.endswith("/succession/release")]
    assert release_posts == [f"/v1/sessions/{new}/succession/release"]

    assert wake.await_count == 1
    wake_text = wake.await_args.args[1]
    assert "Continue the migration." in wake_text
    assert "Postgres or SQLite?" in wake_text
    assert f"Previous session: /c/{old}" in wake_text
    assert f"- {child_a}: A" in wake_text

    notices = _succession_notices(store, new)
    assert len(notices) == 1
    assert notices[0].id == receipt.opening_item_id

    superseded = [
        event
        for session_id, event in published
        if event.get("type") == "session.superseded" and session_id == old
    ]
    assert len(superseded) == 1
    assert superseded[0]["target_conversation_id"] == new
    child_created = [
        event
        for session_id, event in published
        if event.get("type") == "session.created" and session_id == new
    ]
    assert {event["child_session_id"] for event in child_created} == {child_a, child_b}
    assert pending_elicitations.count_for(old) == 0


async def test_succession_calls_every_distinct_runner_with_the_full_move(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runners: _FakeRunnerPool,
) -> None:
    """A cross-host child's runner gets the same full moved set as the successor's."""
    old = _create(store, title="mother")
    new = _create(store, title="successor")
    local_child = _create(store, parent=old, title="local")
    remote_child = _create(store, parent=old, title="remote", runner_id=RUNNER_ID_B)

    response = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done"

    for runner_id in (RUNNER_ID, RUNNER_ID_B):
        posts = runners.for_runner(runner_id).posts
        succession_posts = [payload for url, payload in posts if url.endswith("/succession")]
        assert len(succession_posts) == 1, runner_id
        assert succession_posts[0] is not None
        assert set(succession_posts[0]["moved_ids"]) == {local_child, remote_child}
        assert set(succession_posts[0]["archive_states"]) == {local_child, remote_child}
        release_posts = [url for url, _payload in posts if url.endswith("/succession/release")]
        assert release_posts == [f"/v1/sessions/{new}/succession/release"], runner_id


async def test_succession_later_runner_failure_keeps_earlier_dropped_inventory(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runners: _FakeRunnerPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later runner's failure must not lose an earlier runner's dropped list."""
    monkeypatch.setattr(succession_module, "_RETRY_BACKOFFS_S", ())
    monkeypatch.setattr(succession_module, "_retry_attempts", {})
    monkeypatch.setattr(succession_module, "_retry_tasks", {})
    old = _create(store, title="mother")
    new = _create(store, title="successor")
    _create(store, parent=old, title="remote", runner_id=RUNNER_ID_B)
    item = {"kind": "timer", "id": "timer_1", "label": "wake"}
    first_runner = runners.for_runner(RUNNER_ID)
    first_runner.dropped = [item]
    second_runner = runners.for_runner(RUNNER_ID_B)
    second_runner.succession_failures = 1

    first = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert first.status_code == 200
    assert first.json()["phase"] == "moved"
    receipt = store.get_succession(old, new)
    assert receipt is not None and receipt.dropped == [item]

    # The runner is done cancelling: its retry answers nothing new.
    first_runner.dropped = []
    second = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert second.status_code == 200
    assert second.json()["status"] == "done"
    receipt = store.get_succession(old, new)
    assert receipt is not None and receipt.dropped == [item]


async def test_succession_without_handover_or_questions_does_not_wake(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runner: _FakeRunnerClient,
    wake: AsyncMock,
) -> None:
    """With nothing to re-ask, the opening is only a persisted notice."""
    old = _create(store, title="mother")
    new = _create(store, title="successor")
    _create(store, parent=old, title="A")

    response = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done"
    assert wake.await_count == 0
    assert len(_succession_notices(store, new)) == 1


async def test_succession_noop_without_live_children(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """An ordinary rotation of a childless session writes nothing."""
    old = _create(store, title="mother")
    new = _create(store, title="successor")

    response = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "noop"}
    assert store.get_succession(old, new) is None


async def test_succession_can_archive_without_live_children(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runner: _FakeRunnerClient,
    wake: AsyncMock,
) -> None:
    """A user clear finishes the same phases even without live children."""
    old = _create(store, title="previous")
    new = _create(store, title="successor")
    pending_elicitations.record_publish(old, _question_event())
    response = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new, "allow_empty": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done"
    assert response.json()["moved_ids"] == []
    previous = store.get_conversation(old)
    assert previous is not None and previous.archived
    assert previous.labels[SUCCEEDED_BY_LABEL_KEY] == new
    assert pending_elicitations.count_for(old) == 0
    assert len(_succession_notices(store, new)) == 1
    assert wake.await_count == 1
    retry = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new, "allow_empty": True},
    )
    assert retry.json()["status"] == "done"
    assert len(_succession_notices(store, new)) == 1


async def test_succession_noop_for_child_session(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """A child session never starts a succession."""
    root = _create(store, title="root")
    child = _create(store, parent=root, title="child")
    _create(store, parent=child, title="grandchild")
    new = _create(store, title="successor")

    response = await client.post(
        f"/v1/sessions/{child}/succession",
        json={"target_session_id": new},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "noop"}
    assert store.get_succession(child, new) is None


async def test_succession_target_not_ready_stays_moved_then_resumes(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runner: _FakeRunnerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 409 keeps the receipt at ``moved`` and a later call completes it."""
    monkeypatch.setattr(succession_module, "_RETRY_BACKOFFS_S", ())
    old = _create(store, title="mother")
    new = _create(store, title="successor")
    child = _create(store, parent=old, title="A")
    runner.succession_status = 409

    first = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "pending"
    assert first.json()["phase"] == "moved"
    moved = store.get_conversation(child)
    assert moved is not None and moved.parent_conversation_id == new

    runner.succession_status = 200
    second = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert second.status_code == 200
    assert second.json()["status"] == "done"
    receipt = store.get_succession(old, new)
    assert receipt is not None and receipt.phase == "done"


async def test_succession_retry_that_fails_again_keeps_retrying(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runner: _FakeRunnerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-process retry that fails schedules the next attempt until done."""
    monkeypatch.setattr(succession_module, "_RETRY_BACKOFFS_S", (0.01, 0.01, 0.01, 0.01))
    monkeypatch.setattr(succession_module, "_retry_attempts", {})
    monkeypatch.setattr(succession_module, "_retry_tasks", {})
    old = _create(store, title="mother")
    new = _create(store, title="successor")
    _create(store, parent=old, title="A")
    runner.release_failures = 3

    first = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert first.status_code == 200
    assert first.json()["phase"] == "opened"

    receipt = None
    for _ in range(300):
        receipt = store.get_succession(old, new)
        if receipt is not None and receipt.phase == "done":
            break
        await asyncio.sleep(0.02)
    assert receipt is not None and receipt.phase == "done"


async def test_succession_resumes_from_opened_without_second_notice(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runner: _FakeRunnerClient,
    wake: AsyncMock,
) -> None:
    """A release failure parks the receipt at ``opened``; resume keeps one notice."""
    old = _create(store, title="mother")
    new = _create(store, title="successor")
    _create(store, parent=old, title="A")
    pending_elicitations.record_publish(old, _question_event())
    runner.release_failures = 1

    first = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "pending"
    assert first.json()["phase"] == "opened"
    receipt = store.get_succession(old, new)
    assert receipt is not None and receipt.error is not None
    assert len(_succession_notices(store, new)) == 1
    assert wake.await_count == 1

    second = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert second.status_code == 200
    assert second.json()["status"] == "done"
    assert len(_succession_notices(store, new)) == 1
    assert wake.await_count == 1


async def test_succession_title_clash_is_conflict_and_changes_nothing(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """A kept child colliding with an existing child of the target is refused."""
    old = _create(store, title="mother")
    child = _create(store, parent=old, title="clashing")
    new = _create(store, title="successor")
    _create(store, parent=new, title="clashing")

    response = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "title_clash" in response.json()["error"]["message"]
    assert store.get_succession(old, new) is None
    unchanged = store.get_conversation(child)
    assert unchanged is not None
    assert unchanged.parent_conversation_id == old
    old_row = store.get_conversation(old)
    assert old_row is not None and SUCCEEDED_BY_LABEL_KEY not in old_row.labels


async def test_succession_questions_are_stored_before_cards_close(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runner: _FakeRunnerClient,
    wake: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receipt holds the questions before the old card is resolved."""
    old = _create(store, title="mother")
    new = _create(store, title="successor")
    _create(store, parent=old, title="A")
    pending_elicitations.record_publish(old, _question_event())

    observed: list[list[dict[str, Any]] | None] = []
    real_signal = succession_module._signal_harness_elicitation_resolved_by_id

    def _record_signal(session_id: str, elicitation_id: str) -> None:
        receipt = store.get_succession(old, new)
        observed.append(receipt.questions if receipt is not None else None)
        real_signal(session_id, elicitation_id)

    monkeypatch.setattr(
        succession_module,
        "_signal_harness_elicitation_resolved_by_id",
        _record_signal,
    )

    response = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert response.status_code == 200
    assert observed == [[_question_event()]]
    assert pending_elicitations.count_for(old) == 0


async def test_succession_undo_flips_links_and_children(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runner: _FakeRunnerClient,
) -> None:
    """After A→B, calling B→A (with A unarchived) reverses the move."""
    old = _create(store, title="A")
    new = _create(store, title="B")
    child = _create(store, parent=old, title="child")

    first = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert first.status_code == 200 and first.json()["status"] == "done"

    unarchived = store.update_conversation(old, archived=False)
    assert unarchived is not None

    undo = await client.post(
        f"/v1/sessions/{new}/succession",
        json={"target_session_id": old},
    )
    assert undo.status_code == 200, undo.text
    assert undo.json()["status"] == "done"

    old_row = store.get_conversation(old)
    new_row = store.get_conversation(new)
    assert old_row is not None and new_row is not None
    assert SUCCEEDED_BY_LABEL_KEY not in old_row.labels
    assert old_row.labels[SUCCEEDS_LABEL_KEY] == new
    assert new_row.labels[SUCCEEDED_BY_LABEL_KEY] == old
    assert SUCCEEDS_LABEL_KEY not in new_row.labels
    moved_back = store.get_conversation(child)
    assert moved_back is not None
    assert moved_back.parent_conversation_id == old
    assert moved_back.root_conversation_id == old


async def test_succession_refused_undo_keeps_the_forward_link(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runner: _FakeRunnerClient,
) -> None:
    """A refused inverse move must leave the forward labels intact."""
    old = _create(store, title="A")
    new = _create(store, title="B")
    _create(store, parent=old, title="child")

    first = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert first.status_code == 200 and first.json()["status"] == "done"

    undo = await client.post(
        f"/v1/sessions/{new}/succession",
        json={"target_session_id": old},
    )
    assert undo.status_code == 409

    old_row = store.get_conversation(old)
    new_row = store.get_conversation(new)
    assert old_row is not None and old_row.labels[SUCCEEDED_BY_LABEL_KEY] == new
    assert new_row is not None and new_row.labels[SUCCEEDS_LABEL_KEY] == old


async def test_succession_retargets_queued_peer_mail(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runner: _FakeRunnerClient,
    db_uri: str,
) -> None:
    """Queued mail addressed to the old id moves; delivered mail stays."""
    old = _create(store, title="mother")
    new = _create(store, title="successor")
    child = _create(store, parent=old, title="A")
    peer_store = SqlAlchemyPeerMessageStore(db_uri)

    now = int(time.time())
    queued = peer_store.create(
        SessionPeerMessage(
            id="1" * 32,
            sender_session_id=child,
            receiver_session_id=old,
            ref="1" * 32,
            text="for the mother",
            state="queued",
            created_at=now,
            expires_at=now + 3600,
        )
    )
    delivered = peer_store.create(
        SessionPeerMessage(
            id="3" * 32,
            sender_session_id=child,
            receiver_session_id=old,
            ref="3" * 32,
            text="already landed",
            state="delivered",
            created_at=now,
            expires_at=now + 3600,
        )
    )

    response = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "done"

    retargeted = peer_store.get(queued.id)
    assert retargeted is not None
    assert retargeted.receiver_session_id == new
    untouched = peer_store.get(delivered.id)
    assert untouched is not None
    assert untouched.receiver_session_id == old


async def test_succession_auth_requires_edit_on_target(
    runtime_init: None,
    db_uri: str,
    tmp_path: Any,
    store: SqlAlchemyConversationStore,
) -> None:
    """A caller with edit on old but only read on the target gets 403."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        auth_provider=UnifiedAuthProvider(source="header"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        peer_message_store=SqlAlchemyPeerMessageStore(db_uri),
    )
    old = _create(store, title="mother")
    new = _create(store, title="successor")
    _create(store, parent=old, title="A")
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant("bob@example.com", old, LEVEL_EDIT)
    permissions.grant("bob@example.com", new, LEVEL_READ)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/v1/sessions/{old}/succession",
            json={"target_session_id": new},
            headers={"X-Forwarded-Email": "bob@example.com"},
        )
    assert response.status_code == 403
    assert store.get_succession(old, new) is None


async def test_succession_undelivered_wake_stays_rekeyed_and_keeps_old_cards(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    runner: _FakeRunnerClient,
    wake: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A carried question whose wake never lands must not close the old card."""
    monkeypatch.setattr(succession_module, "_RETRY_BACKOFFS_S", ())
    old = _create(store, title="mother")
    new = _create(store, title="successor")
    _create(store, parent=old, title="A")
    pending_elicitations.record_publish(old, _question_event())
    wake.return_value = False

    first = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert first.status_code == 200
    assert first.json()["phase"] == "rekeyed"
    assert pending_elicitations.count_for(old) == 1

    wake.return_value = True
    second = await client.post(
        f"/v1/sessions/{old}/succession",
        json={"target_session_id": new},
    )
    assert second.json()["status"] == "done"
    assert len(_succession_notices(store, new)) == 1
    assert pending_elicitations.count_for(old) == 0
