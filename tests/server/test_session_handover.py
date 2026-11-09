"""Server handover-endpoint tests.

``POST /v1/sessions/{id}/handover`` persists the calling session's handover
as a visible assistant message, points ``omnigent.handover_item`` at it, and
(with ``rotate``) arms ``omnigent.rotate_requested`` plus a visible notice.
Only a top-level session with a supported rotation harness may call it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.entities import ErrorData
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auto_rotation import INJECTED_REVISION_LABEL_KEY
from omnigent.server.routes import sessions as sessions_facade
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store import (
    HANDOVER_ITEM_LABEL_KEY,
    ROTATE_REQUESTED_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyConversationStore:
    """A conversation store backed by the per-test SQLite database."""
    return SqlAlchemyConversationStore(db_uri)


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
def handover_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Any,
) -> FastAPI:
    """App with real stores and no permission store (single-user posture)."""
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
    )


@pytest_asyncio.fixture()
async def client(handover_app: FastAPI) -> Any:
    """Async client wired to the handover app."""
    transport = httpx.ASGITransport(app=handover_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _create(
    store: SqlAlchemyConversationStore,
    *,
    parent: str | None = None,
    native: bool = True,
) -> str:
    """Create a conversation, optionally stamped as a claude-native wrapper."""
    conversation = store.create_conversation(parent_conversation_id=parent, title="session")
    if native:
        store.set_labels(
            conversation.id,
            {WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE},
        )
    return conversation.id


def _handover_messages(store: SqlAlchemyConversationStore, session_id: str) -> list[Any]:
    """All persisted message items on a session."""
    page = store.list_items(session_id, limit=200, order="desc", type="message")
    return list(page.data)


def _rotation_notices(store: SqlAlchemyConversationStore, session_id: str) -> list[Any]:
    """All persisted rotation notices on a session."""
    page = store.list_items(session_id, limit=200, order="desc", type="error")
    return [item for item in page.data if getattr(item.data, "code", None) == "session_rotation"]


async def test_handover_records_item_and_labels(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    published: list[tuple[str, dict[str, Any]]],
) -> None:
    """One note item, both labels, and a rotation notice land on the session."""
    session_id = _create(store)

    response = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "Continue the migration."},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rotate"] is True
    assert isinstance(body["item_id"], str) and body["item_id"]

    messages = _handover_messages(store, session_id)
    assert len(messages) == 1
    assert messages[0].id == body["item_id"]
    assert messages[0].data.role == "assistant"
    assert messages[0].data.content == [{"type": "output_text", "text": "Continue the migration."}]

    row = store.get_conversation(session_id)
    assert row is not None
    assert row.labels[HANDOVER_ITEM_LABEL_KEY] == body["item_id"]
    assert row.labels[ROTATE_REQUESTED_LABEL_KEY] == str(row.archive_revision)

    notices = _rotation_notices(store, session_id)
    assert len(notices) == 1
    assert notices[0].data.message == (
        "This session will continue in a new session when the current turn ends."
    )

    published_item_ids = [
        event.get("item", {}).get("id")
        for session, event in published
        if session == session_id and event.get("type") == "response.output_item.done"
    ]
    assert body["item_id"] in published_item_ids
    assert notices[0].id in published_item_ids


async def test_handover_rotate_false_records_only_the_note(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """``rotate=false`` records the note but arms nothing."""
    session_id = _create(store)

    response = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "Context for later.", "rotate": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["rotate"] is False

    row = store.get_conversation(session_id)
    assert row is not None
    assert row.labels[HANDOVER_ITEM_LABEL_KEY] == response.json()["item_id"]
    assert ROTATE_REQUESTED_LABEL_KEY not in row.labels
    assert _rotation_notices(store, session_id) == []


async def test_handover_same_text_does_not_duplicate(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """A retry with the same text reuses the stable item and notice ids."""
    session_id = _create(store)

    first = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "Same note."},
    )
    second = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "Same note."},
    )
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["item_id"] == first.json()["item_id"]
    assert len(_handover_messages(store, session_id)) == 1
    assert len(_rotation_notices(store, session_id)) == 1


async def test_handover_child_session_refused(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """A sub-agent session cannot record a handover."""
    parent_id = _create(store)
    child_id = _create(store, parent=parent_id)

    response = await client.post(
        f"/v1/sessions/{child_id}/handover",
        json={"handover": "Child note."},
    )
    assert response.status_code == 400, response.text
    assert "not_top_level" in response.text
    assert _handover_messages(store, child_id) == []


async def test_handover_non_native_session_refused(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """A session that is not claude-native / codex-native cannot rotate."""
    session_id = _create(store, native=False)

    response = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "SDK note."},
    )
    assert response.status_code == 400, response.text
    assert "not_native" in response.text
    assert _handover_messages(store, session_id) == []


@pytest.mark.parametrize("harness", ["pi", "pi-native", "antigravity-native"])
@pytest.mark.parametrize("rotate", [True, False])
async def test_pi_handover_is_recorded_for_top_level_session(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    harness: str,
    rotate: bool,
) -> None:
    session = store.create_conversation(harness_override=harness)

    response = await client.post(
        f"/v1/sessions/{session.id}/handover",
        json={"handover": "Continue with the children.", "rotate": rotate},
    )

    assert response.status_code == 200, response.text
    row = store.get_conversation(session.id)
    assert row is not None
    assert row.labels[HANDOVER_ITEM_LABEL_KEY] == response.json()["item_id"]
    if rotate:
        assert row.labels[ROTATE_REQUESTED_LABEL_KEY] == str(row.archive_revision)
    else:
        assert ROTATE_REQUESTED_LABEL_KEY not in row.labels


async def test_handover_empty_text_refused(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
) -> None:
    """Whitespace-only handover text is rejected."""
    session_id = _create(store)

    response = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "   \n"},
    )
    assert response.status_code == 400, response.text
    assert "invalid_input" in response.text
    assert _handover_messages(store, session_id) == []


@pytest.mark.parametrize(
    "harness", ["claude-native", "codex-native", "pi", "pi-native", "antigravity-native"]
)
@pytest.mark.parametrize("mode", ["off", "due", "compacted", "above_threshold"])
async def test_auto_handover_rechecks_context(client, store, harness, mode):
    from omnigent.server.auto_rotation import FRESH_ROUND_LABEL_KEY, NOTICED_COUNT_LABEL_KEY

    conv = store.create_conversation(harness_override=harness)
    marker = "4" if harness == "antigravity-native" else "0"
    store.set_labels(
        conv.id,
        {
            "omnigent.auto_rotation": "off" if mode == "off" else "on",
            "omnigent.last_context_tokens": "100000" if mode == "above_threshold" else "150000",
            "omnigent.last_auto_compact_token_limit": "200000",
            "omnigent.auto_rotation_state.inferred_compactions": "4",
            FRESH_ROUND_LABEL_KEY: marker,
            NOTICED_COUNT_LABEL_KEY: "previous" if mode == "compacted" else marker,
        },
    )
    response = await client.post(
        f"/v1/sessions/{conv.id}/handover",
        json={"handover": "Continue.", "rotate": True, "auto": True},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["rotate"] is (mode == "due")
    assert (
        result["deferred"]
        == {
            "off": "switch_off",
            "due": None,
            "compacted": "compacted",
            "above_threshold": "above_threshold",
        }[mode]
    )
    labels = store.get_conversation(conv.id).labels
    assert HANDOVER_ITEM_LABEL_KEY in labels
    assert (ROTATE_REQUESTED_LABEL_KEY in labels) is (mode == "due")
    if mode in {"off", "above_threshold"}:
        assert NOTICED_COUNT_LABEL_KEY not in labels


async def test_auto_without_rotation_is_invalid(client, store):
    session_id = _create(store)
    response = await client.post(
        f"/v1/sessions/{session_id}/handover",
        json={"handover": "Continue.", "rotate": False, "auto": True},
    )
    assert response.status_code == 400
    assert _handover_messages(store, session_id) == []


async def test_metadata_exposes_archive_revision_for_stale_rotation_guard(client, store):
    agent = SqlAlchemyAgentStore(store.storage_location).create(
        "a" * 32, "test-agent", "unused-bundle"
    )
    session_id = store.create_conversation(agent_id=agent.id, harness_override="codex-native").id
    store.set_labels(session_id, {ROTATE_REQUESTED_LABEL_KEY: "0"})
    store.update_conversation(session_id, archived=True)
    store.update_conversation(session_id, archived=False)
    response = await client.get(
        f"/v1/sessions/{session_id}",
        params={"include_items": "false", "include_liveness": "false"},
    )
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert snapshot["archive_revision"] == store.get_conversation(session_id).archive_revision
    assert snapshot["archive_revision"] > 0
    assert snapshot["labels"][ROTATE_REQUESTED_LABEL_KEY] == "0"


async def test_injection_claim_serializes_replays_and_new_revision(client, store):
    session_id = _create(store)
    store.set_labels(session_id, {ROTATE_REQUESTED_LABEL_KEY: "0"})
    url = f"/v1/sessions/{session_id}/handover/injection"
    replies = await asyncio.gather(
        *(client.post(url, json={"archive_revision": "0"}) for _ in range(2))
    )
    assert sorted(r.json()["status"] for r in replies) == ["claimed", "unconfirmed"]
    assert store.get_conversation(session_id).labels[INJECTED_REVISION_LABEL_KEY] == "0"
    await client.post(url, json={"archive_revision": "0", "input_cleared": "true"})
    store.update_conversation(session_id, archived=True)
    store.update_conversation(session_id, archived=False)
    revision = str(store.get_conversation(session_id).archive_revision)
    store.set_labels(session_id, {ROTATE_REQUESTED_LABEL_KEY: revision})
    assert (await client.post(url, json={"archive_revision": revision})).json() == {
        "status": "claimed"
    }
    assert store.get_conversation(session_id).labels[INJECTED_REVISION_LABEL_KEY] == revision
    response = await client.patch(
        f"/v1/sessions/{session_id}", json={"labels": {INJECTED_REVISION_LABEL_KEY: "0"}}
    )
    assert response.status_code == 400
    assert store.get_conversation(session_id).labels[INJECTED_REVISION_LABEL_KEY] == revision


@pytest.mark.parametrize(
    "state", ["archived", "child", "superseded", "stale", "missing", "unsupported"]
)
async def test_injection_claim_rejects_ineligible_request(client, store, state):
    session_id = _create(store, native=state != "unsupported")
    store.set_labels(session_id, {ROTATE_REQUESTED_LABEL_KEY: "0"})
    if state == "archived":
        store.update_conversation(session_id, archived=True)
    elif state == "child":
        parent = _create(store)
        session_id = _create(store, parent=parent)
        store.set_labels(session_id, {ROTATE_REQUESTED_LABEL_KEY: "0"})
    elif state == "superseded":
        store.set_labels(session_id, {"omnigent.succeeded_by": "conv_next"})
    elif state == "stale":
        store.set_labels(session_id, {ROTATE_REQUESTED_LABEL_KEY: "1"})
    elif state == "missing":
        store.delete_label(session_id, ROTATE_REQUESTED_LABEL_KEY)
    response = await client.post(
        f"/v1/sessions/{session_id}/handover/injection", json={"archive_revision": "0"}
    )
    assert response.json() == {"status": "not_requested"}
    assert INJECTED_REVISION_LABEL_KEY not in store.get_conversation(session_id).labels


async def test_injection_claim_rejects_invalid_or_absent_session(client, store):
    session_id = _create(store)
    response = await client.post(
        f"/v1/sessions/{session_id}/handover/injection", json={"archive_revision": "bad"}
    )
    assert response.status_code == 400
    response = await client.post(
        "/v1/sessions/conv_missing/handover/injection", json={"archive_revision": "0"}
    )
    assert response.status_code == 404


async def test_runner_restart_keeps_injection_claim_and_reports_unconfirmed(
    client, store, monkeypatch
):
    from omnigent.harnesses.claude_native import bridge
    from tests.runner.conftest import _runner_client
    from tests.runner.test_pending_rotation_idle_edge import _build_app, _RotationServerClient
    from tests.server.test_auto_rotation_notices import service

    session_id = store.create_conversation(harness_override="claude-native").id
    store.set_labels(session_id, {ROTATE_REQUESTED_LABEL_KEY: "0", "omnigent.auto_rotation": "on"})
    effects = []

    def inject(*args, **kwargs):
        # Observe committed server state at the actual injection boundary.
        assert store.get_conversation(session_id).labels[INJECTED_REVISION_LABEL_KEY] == "0"
        effects.append(kwargs["command"])

    monkeypatch.setattr(bridge, "inject_slash_command", inject)

    class Server(_RotationServerClient):
        async def get(self, url, **kwargs):
            row = store.get_conversation(session_id)
            self.labels, self.archive_revision = row.labels, row.archive_revision
            return await super().get(url, **kwargs)

        async def post(self, url, **kwargs):
            if url.endswith("/handover/injection"):
                return await client.post(url, **kwargs)
            return await super().post(url, **kwargs)

    server = Server()
    url = f"/v1/sessions/{session_id}/pending-rotation"
    async with _runner_client(_build_app(server)) as runner:
        response = await runner.post(
            "/v1/sessions", json={"session_id": session_id, "agent_id": "ag_rotation"}
        )
        assert response.status_code == 201
        assert (await runner.post(url)).json() == {"status": "started"}
    assert effects == ["/clear"]
    # A new app creates a genuinely empty rotations_started closure.
    async with _runner_client(_build_app(server)) as restarted:
        response = await restarted.post(
            "/v1/sessions", json={"session_id": session_id, "agent_id": "ag_rotation"}
        )
        assert response.status_code == 201
        assert (await restarted.post(url)).json() == {"status": "unconfirmed"}
        notices = service(store, restarted, [], ["idle"], [{}])
        await notices.start(SimpleNamespace())
        try:
            for _ in range(6):
                await notices.tick()
            assert effects == ["/clear"]
            errors = [
                i.data
                for i in store.list_items(session_id, type="error").data
                if isinstance(i.data, ErrorData) and i.data.code == "session_rotation_failed"
            ]
            assert len(errors) == 1
            assert "unconfirmed" in errors[0].message
            assert "will not be retried automatically" in errors[0].message
            # A distinct round at the same archive revision can inject once.
            store.set_labels(session_id, {"omnigent.auto_rotation_state.attempt": "next-round"})
            notices.track(session_id, "0")
            assert (await restarted.post(url)).json() == {"status": "started"}
            assert (await restarted.post(url)).json() == {"status": "unconfirmed"}
            for _ in range(2):
                await notices.tick()
            assert effects == ["/clear", "/clear"]
            assert len(store.list_items(session_id, type="error").data) == 2
        finally:
            await notices.shutdown()


async def test_new_attempt_same_revision_claims_once_and_rejects_old_request(client, store):
    prefix = "omnigent.auto_rotation_state."
    session_id = _create(store)
    store.set_labels(
        session_id,
        {
            ROTATE_REQUESTED_LABEL_KEY: "0",
            INJECTED_REVISION_LABEL_KEY: "0",
            prefix + "attempt": "next-round",
        },
    )
    url = f"/v1/sessions/{session_id}/handover/injection"
    assert (await client.post(url, json={"archive_revision": "0"})).json()[
        "status"
    ] == "not_requested"
    body = {"archive_revision": "0", "attempt": "next-round"}
    replies = await asyncio.gather(*(client.post(url, json=body) for _ in range(2)))
    assert sorted(r.json()["status"] for r in replies) == ["claimed", "unconfirmed"]
    assert (await client.post(url, json=body | {"draft_may_remain": "true"})).json()[
        "status"
    ] == "draft_may_remain"
    store.set_labels(session_id, {prefix + "attempt": "later-round"})
    assert (
        await client.post(url, json={"archive_revision": "0", "attempt": "later-round"})
    ).json()["status"] == "draft_may_remain"


@pytest.mark.parametrize("harness", ["claude-native", "antigravity-native"])
async def test_failed_round_rearms_after_four_compactions(client, store, harness):
    from omnigent.server.auto_rotation import (
        ATTEMPT_LABEL_KEY,
        DRAFT_LABEL_KEY,
        FAILED_ATTEMPT_LABEL_KEY,
        FRESH_ROUND_LABEL_KEY,
        INFERRED_COMPACTIONS_LABEL_KEY,
        auto_rotation_for,
        rotation_attempt,
    )
    from tests.server.test_auto_rotation import _compact

    conv = store.create_conversation(harness_override=harness)
    store.set_labels(
        conv.id,
        {
            "omnigent.auto_rotation": "on",
            "omnigent.last_context_tokens": "150000",
            "omnigent.last_context_window": "200000",
            FRESH_ROUND_LABEL_KEY: "0",
        },
    )
    url = f"/v1/sessions/{conv.id}/handover"
    first = await client.post(url, json={"handover": "Same note", "rotate": True, "auto": True})
    assert first.json()["rotate"] is True
    first_row = store.get_conversation(conv.id)
    first_attempt = rotation_attempt(first_row.labels, 0)
    store.set_labels(conv.id, {FAILED_ATTEMPT_LABEL_KEY: first_attempt})
    for count in range(5):
        row = store.get_conversation(conv.id)
        status = auto_rotation_for(row, store)
        store.set_labels(conv.id, {FRESH_ROUND_LABEL_KEY: str(status.round_marker)})
        assert auto_rotation_for(store.get_conversation(conv.id), store).due is (count == 4)
        if count == 1:
            refused = await client.post(
                url, json={"handover": "Same note", "rotate": True, "auto": True}
            )
            assert refused.json()["rotate"] is False
        if count < 4:
            if harness == "antigravity-native":
                store.set_labels(conv.id, {INFERRED_COMPACTIONS_LABEL_KEY: str(count + 1)})
            else:
                _compact(store, conv.id)
    next_round = await client.post(
        url, json={"handover": "Same note", "rotate": True, "auto": True}
    )
    assert next_round.json()["rotate"] is True
    row = store.get_conversation(conv.id)
    assert row.archive_revision == 0
    assert row.labels[ATTEMPT_LABEL_KEY] != first_row.labels[ATTEMPT_LABEL_KEY]
    body = {"archive_revision": "0", "attempt": row.labels[ATTEMPT_LABEL_KEY]}
    claim_url = url + "/injection"
    assert (await client.post(claim_url, json=body)).json()["status"] == "claimed"
    assert store.get_conversation(conv.id).labels[DRAFT_LABEL_KEY] == "true"
    assert (await client.post(claim_url, json=body | {"input_cleared": "true"})).json()[
        "status"
    ] == "unconfirmed"
    assert DRAFT_LABEL_KEY not in store.get_conversation(conv.id).labels
    assert (await client.post(claim_url, json=body)).json()["status"] == "unconfirmed"
    store.set_labels(conv.id, {FAILED_ATTEMPT_LABEL_KEY: rotation_attempt(row.labels, 0)})
    # A note persisted during deferral must not shorten the next interval.
    for count in range(5):
        status = auto_rotation_for(store.get_conversation(conv.id), store)
        store.set_labels(conv.id, {FRESH_ROUND_LABEL_KEY: str(status.round_marker)})
        assert auto_rotation_for(store.get_conversation(conv.id), store).due is (count == 4)
        if count < 4:
            if harness == "antigravity-native":
                store.set_labels(conv.id, {INFERRED_COMPACTIONS_LABEL_KEY: str(5 + count)})
            else:
                _compact(store, conv.id)


@pytest.mark.parametrize(
    "key",
    [
        "attempt",
        "injected_attempt",
        "failed_attempt",
        "attempt_item",
        "attempt_compactions",
        "draft_may_remain",
    ],
)
async def test_rotation_state_cannot_be_seeded_or_patched(client, store, key):
    key = "omnigent.auto_rotation_state." + key
    session_id = _create(store)
    assert (
        await client.patch(f"/v1/sessions/{session_id}", json={"labels": {key: "forged"}})
    ).status_code == 400
    from omnigent.server.routes._sessions.helpers import _strip_succession_operation_labels

    labels = {key: "copied", "omnigent.auto_rotation": "on"}
    _strip_succession_operation_labels(labels)
    assert labels == {"omnigent.auto_rotation": "on"}


async def test_pi_claim_never_marks_terminal_draft(client, store):
    conv = store.create_conversation(harness_override="pi")
    store.set_labels(
        conv.id,
        {ROTATE_REQUESTED_LABEL_KEY: "0", "omnigent.auto_rotation_state.attempt": "rpc-attempt"},
    )
    response = await client.post(
        f"/v1/sessions/{conv.id}/handover/injection",
        json={"archive_revision": "0", "attempt": "rpc-attempt"},
    )
    assert response.json()["status"] == "claimed"
    assert (
        "omnigent.auto_rotation_state.draft_may_remain"
        not in store.get_conversation(conv.id).labels
    )
