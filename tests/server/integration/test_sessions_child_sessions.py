"""Integration tests for ``GET /v1/sessions/{id}/child_sessions``.

The endpoint exposes sub-agent (child) sessions spawned from a parent
session so debug surfaces can enumerate sub-agent calls without parsing
parent ``function_call_output`` JSON handles. Tests here seed sub-agent
conversations directly via the SqlAlchemy stores (rather than going
through the spawn workflow) — the route depends only on
``list_conversations(kind="sub_agent", parent_conversation_id=...)``
and the relay-fed ``_session_status_cache``, so direct seeding gives
fast, deterministic coverage of every response field.

The tasks table has been removed. ``current_task_id`` is now always
``None``. ``current_task_status`` is derived from session lifecycle state
when available, and is otherwise ``None``. ``agent_id`` is populated from
the conversation row's ``agent_id`` column, and ``agent_name`` /
``harness`` are resolved from the bound agent row per conversation.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import tarfile
import time
from dataclasses import dataclass
from typing import Any, NoReturn

import httpx
import pytest
import yaml

from omnigent.entities import Conversation
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.runtime import set_runner_client
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes.sessions import routes_events as routes_events_module
from omnigent.stores.conversation_store import (
    sqlalchemy_store as sqlalchemy_store_module,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_pending_elicitations_index() -> Any:
    """
    Reset the process-global pending-elicitations index around each test.

    The ``pending_elicitations_count`` field on each child summary reads
    this index. Without a reset, an entry recorded by one test would
    inflate another's count (the ``== 0`` assertions would break).
    """
    from omnigent.runtime import pending_elicitations

    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


# ── Helpers ──────────────────────────────────────────────


async def _create_parent_session(
    client: httpx.AsyncClient,
    agent_name: str = "test-agent",
) -> dict[str, Any]:
    """
    Create a parent session bound to a fresh test agent.

    :param client: The test HTTP client.
    :param agent_name: Name for the underlying agent. Tests that
        spin up multiple parents in the same DB must pass distinct
        names — the agent_store enforces unique-by-name and the
        ``test-agent`` default collides on the second call.
    :returns: The ``POST /v1/sessions`` response body.
    """
    agent = await create_test_agent(client, name=agent_name)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
    )
    assert resp.status_code == 201, f"session create failed: {resp.text}"
    return resp.json()


def _seed_child(
    *,
    conv_store: SqlAlchemyConversationStore,
    parent_id: str,
    title: str,
    agent_id: str | None = None,
    host_id: str | None = None,
    workspace: str | None = None,
    worktree: str | None = None,
    git_branch: str | None = None,
    harness_override: str | None = None,
) -> Conversation:
    """
    Create a child sub-agent conversation.

    Mirrors what :func:`omnigent.tools.builtins.spawn._spawn_one` does,
    minus the workflow start and SSE publish. The tasks table has been
    removed — ``current_task_id`` and ``current_task_status`` fields in
    the summary are always ``None``.

    :param conv_store: Store for the child conversation.
    :param parent_id: Parent conversation id, e.g. ``"0c4b962f26d3fb76dce69d9dade142f5"``.
    :param title: Sub-agent title in the canonical
        ``"{agent_type}:{session_name}"`` format,
        e.g. ``"researcher:auth"``.
    :param agent_id: Agent id to bind to this conversation (populates
        the ``agent_id`` field in the summary).
    :param host_id: Child's own host, e.g. ``"host_h2"``; requires
        ``workspace``.
    :param workspace: Child's own launch directory.
    :param worktree: Child's own working tree when it differs from
        ``workspace``.
    :param git_branch: Branch checked out in the child's own working tree.
    :param harness_override: Per-session harness override, e.g.
        ``"codex-native"``.
    :returns: The created child :class:`Conversation`.
    """
    return conv_store.create_conversation(
        kind="sub_agent",
        title=title,
        parent_conversation_id=parent_id,
        agent_id=agent_id,
        host_id=host_id,
        workspace=workspace,
        worktree=worktree,
        git_branch=git_branch,
        harness_override=harness_override,
    )


def _empty_terminal_runner() -> httpx.AsyncClient:
    page = {"object": "list", "data": [], "first_id": None, "last_id": None, "has_more": False}
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=page)),
        base_url="http://runner.test",
    )


async def _child_row(client: httpx.AsyncClient, parent_id: str, child_id: str) -> dict[str, Any]:
    rows = (await client.get(f"/v1/sessions/{parent_id}/child_sessions")).json()["data"]
    return next(row for row in rows if row["id"] == child_id)


async def _next_child_update(collector: Any) -> dict[str, Any]:
    """
    Await the next ``session.child_session.updated`` event on a collector.

    Other event types can interleave on the same conversation stream
    (e.g. the child's own ``session.status`` edge), so skip until the
    child-update frame arrives.

    :param collector: A running ``SessionStreamCollector``.
    :returns: The first child-update event seen.
    """
    while True:
        event = await asyncio.wait_for(collector.queue.get(), timeout=5.0)
        if event.get("type") == "session.child_session.updated":
            return event


# ── 404 ──────────────────────────────────────────────────


async def test_child_sessions_404_for_nonexistent_session(
    client: httpx.AsyncClient,
) -> None:
    """Route returns 404 when the parent session does not exist."""
    resp = await client.get("/v1/sessions/ad563e906854634c49e1a6fd2fbb31d4/child_sessions")
    assert resp.status_code == 404


async def test_child_sessions_expose_warm_state_from_the_keep_warm_label(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The child list carries the keep-warm pill derived from the label."""
    parent = await _create_parent_session(client, "warm-state-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    now = int(time.time())
    warm = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="researcher:warm",
        harness_override="claude-native",
    )
    cold = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="researcher:cold",
        harness_override="claude-native",
    )
    unlabeled = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="researcher:unlabeled",
        harness_override="claude-native",
    )
    conv_store.set_labels(
        warm.id,
        {"omnigent.keep_warm": json.dumps({"s": "w", "t": now - 100, "w": now + 3600})},
    )
    conv_store.set_labels(
        cold.id,
        {"omnigent.keep_warm": json.dumps({"s": "p", "why": "fail", "t": now - 100})},
    )

    resp = await client.get(f"/v1/sessions/{parent['id']}/child_sessions?limit=100")
    assert resp.status_code == 200, resp.text
    by_id = {row["id"]: row for row in resp.json()["data"]}
    assert by_id[warm.id]["warm_state"] == "warm"
    assert by_id[cold.id]["warm_state"] == "cold"
    assert by_id[unlabeled.id]["warm_state"] is None


async def test_replayed_unverified_native_child_is_not_busy_and_live_activity_restores_it(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A legacy running snapshot is quarantined until a new live edge arrives."""
    parent = await _create_parent_session(client, "unverified-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="Explore:legacy",
        agent_id=parent["agent_id"],
    )
    conv_store.set_labels(
        child.id,
        {"omnigent.wrapper": "claude-code-native-ui-subagent"},
    )
    sessions_module._session_status_cache[child.id] = "running"
    try:
        response = await client.post(
            f"/v1/sessions/{child.id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "activity_unverified", "replayed": True},
            },
        )
        assert response.status_code == 202, response.text
        row = await _child_row(client, parent["id"], child.id)
        assert row["busy"] is False
        assert row["current_task_status"] is None
        assert row["activity_unverified"] is True

        snapshot = (await client.get(f"/v1/sessions/{child.id}")).json()
        assert snapshot["status"] == "idle"

        sessions_module._session_status_cache[child.id] = "running"
        parent_row = next(
            row
            for row in (await client.get("/v1/sessions")).json()["data"]
            if row["id"] == parent["id"]
        )
        assert parent_row["background_activity_count"] == 0
        assert parent_row["status"] == "idle"

        response = await client.post(
            f"/v1/sessions/{child.id}/events",
            json={"type": "external_session_status", "data": {"status": "idle"}},
        )
        assert response.status_code == 202, response.text
        assert sessions_module._session_status_cache[child.id] == "activity_unverified"

        # A Server restart drops the process-local cache. The durable label
        # must still keep this row out of busy/B and out of the false Done state.
        sessions_module._session_status_cache.pop(child.id, None)
        row = await _child_row(client, parent["id"], child.id)
        assert row["busy"] is False
        assert row["current_task_status"] is None
        assert row["activity_unverified"] is True

        # A newly resumed turn also supersedes a terminal verdict repaired for
        # the child's earlier dispatch; both durable markers must be cleared.
        conv_store.set_labels(child.id, {"omnigent.subagent.terminal_status": "completed"})
        response = await client.post(
            f"/v1/sessions/{child.id}/events",
            json={"type": "external_session_status", "data": {"status": "running"}},
        )
        assert response.status_code == 202, response.text
        row = await _child_row(client, parent["id"], child.id)
        assert row["busy"] is True
        assert row["activity_unverified"] is False
        refreshed = conv_store.get_conversation(child.id)
        assert refreshed is not None
        assert refreshed.labels.get("omnigent.subagent.terminal_status") == ""
        assert refreshed.labels.get("omnigent.subagent.status_generation")
    finally:
        sessions_module._session_status_cache.pop(child.id, None)


async def test_native_terminal_event_atomically_clears_concurrent_quarantine(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal verdict wins when quarantine lands immediately before it."""
    parent = await _create_parent_session(client, "terminal-wins-quarantine-race")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="Explore:terminal-race",
        agent_id=parent["agent_id"],
    )
    conv_store.set_labels(
        child.id,
        {"omnigent.wrapper": "claude-code-native-ui-subagent"},
    )
    original_set_labels = SqlAlchemyConversationStore.set_labels
    injected = False

    def _inject_quarantine_before_terminal_write(
        self: SqlAlchemyConversationStore,
        conversation_id: str,
        labels: dict[str, str],
    ) -> None:
        nonlocal injected
        if (
            conversation_id == child.id
            and labels.get("omnigent.subagent.terminal_status") == "completed"
            and not injected
        ):
            injected = True
            original_set_labels(
                self,
                child.id,
                {"omnigent.subagent.activity_unverified": "true"},
            )
        original_set_labels(self, conversation_id, labels)

    monkeypatch.setattr(
        SqlAlchemyConversationStore,
        "set_labels",
        _inject_quarantine_before_terminal_write,
    )
    parent_updates: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        routes_events_module,
        "_publish_child_status_to_parent",
        lambda session_id, status: parent_updates.append((session_id, status)),
    )
    # The terminal edge's public value is also idle, so ordinary cache-value
    # dedupe cannot be responsible for the required durable-state fanout.
    sessions_module._session_status_cache[child.id] = "idle"
    runner = _empty_terminal_runner()
    set_runner_client(runner)
    try:
        response = await client.post(
            f"/v1/sessions/{child.id}/events",
            json={"type": "external_session_status", "data": {"status": "completed"}},
        )
        assert response.status_code == 202, response.text
        current = conv_store.get_conversation(child.id)
        assert injected
        assert current is not None
        assert current.labels.get("omnigent.subagent.terminal_status") == "completed"
        assert current.labels.get("omnigent.subagent.activity_unverified") != "true"
        assert current.labels.get("omnigent.subagent.status_generation")
        assert parent_updates == [(child.id, None)]
        row = await _child_row(client, parent["id"], child.id)
        assert row["current_task_status"] == "completed"
        assert row["activity_unverified"] is False
    finally:
        set_runner_client(None)
        await runner.aclose()
        sessions_module._session_status_cache.pop(child.id, None)


async def test_empty_native_parent_terminal_inventory_invalidates_stale_child_activity(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A dead parent TUI quarantines a child after its final visible output."""
    parent = await _create_parent_session(client, "missing-native-terminal-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_labels(
        parent["id"],
        {
            "omnigent.ui": "terminal",
            "omnigent.wrapper": "claude-code-native-ui",
        },
    )
    conv_store.replace_runner_id(parent["id"], "runner-parent")
    child = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="Explore:stale-running",
        agent_id=parent["agent_id"],
    )
    conv_store.set_labels(
        child.id,
        {"omnigent.wrapper": "claude-code-native-ui-subagent"},
    )
    conv_store.replace_runner_id(child.id, "runner-parent")
    conv_store.set_session_live_status(child.id, "running")
    conv_store.append(
        child.id,
        [
            NewConversationItem(
                type="message",
                response_id="native-child-final",
                data=MessageData(
                    role="assistant",
                    agent="Explore",
                    content=[{"type": "output_text", "text": "CHILD_DONE"}],
                ),
            )
        ],
    )
    # Claude's child can emit its final visible output and a transient idle
    # display edge without a structured terminal verdict. The durable relay
    # status remains running, which otherwise leaves the Agents rail on
    # Working forever after the parent's terminal has disappeared.
    sessions_module._session_status_cache[child.id] = "idle"
    runner = _empty_terminal_runner()
    set_runner_client(runner)
    try:
        terminals = await client.get(
            f"/v1/sessions/{parent['id']}/resources/terminals?order=asc&limit=1000"
        )
        assert terminals.status_code == 200, terminals.text
        assert terminals.json()["data"] == []

        child_row = await _child_row(client, parent["id"], child.id)
        assert child_row["busy"] is False
        assert child_row["activity_unverified"] is True
        persisted_child = conv_store.get_conversation(child.id)
        assert persisted_child is not None
        assert persisted_child.live_status == "running"
        assert persisted_child.labels.get("omnigent.subagent.status_generation")
        persisted_parent = conv_store.get_conversation(parent["id"])
        assert persisted_parent is not None
        assert "omnigent.claude_native.bridge_id" not in persisted_parent.labels

        sessions_module._session_status_cache.pop(child.id, None)
        cold_child_row = await _child_row(client, parent["id"], child.id)
        assert cold_child_row["busy"] is False
        assert cold_child_row["activity_unverified"] is True
        assert cold_child_row["current_task_status"] is None

        parent_row = next(
            row
            for row in (await client.get("/v1/sessions")).json()["data"]
            if row["id"] == parent["id"]
        )
        assert parent_row["background_activity_count"] == 0
        assert parent_row["status"] == "idle"
    finally:
        set_runner_client(None)
        await runner.aclose()
        sessions_module._session_status_cache.pop(child.id, None)


async def test_terminal_inventory_survives_best_effort_reconciliation_failure(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repair-store failure cannot turn a successful terminal GET into 500."""
    from omnigent.server.routes._sessions import subagent_reconciliation

    parent = await _create_parent_session(client, "terminal-repair-failure-parent")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(parent["id"], {"omnigent.wrapper": "claude-code-native-ui"})
    store.replace_runner_id(parent["id"], "runner-parent")
    runner = _empty_terminal_runner()

    async def _fail_repair(**_kwargs: Any) -> int:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(
        subagent_reconciliation,
        "_invalidate_native_subagents_for_missing_parent_terminal_impl",
        _fail_repair,
    )
    set_runner_client(runner)
    try:
        response = await client.get(
            f"/v1/sessions/{parent['id']}/resources/terminals?order=asc&limit=1000"
        )
    finally:
        set_runner_client(None)
        await runner.aclose()

    assert response.status_code == 200, response.text
    assert response.json()["data"] == []


async def test_activity_unverified_rejects_live_and_non_claude_native_sessions(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Only a replayed Claude-native child may invalidate historical activity."""
    parent = await _create_parent_session(client, "unverified-validation-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="Explore:legacy",
    )
    conv_store.set_labels(
        child.id,
        {"omnigent.wrapper": "claude-code-native-ui-subagent"},
    )
    live = await client.post(
        f"/v1/sessions/{child.id}/events",
        json={"type": "external_session_status", "data": {"status": "activity_unverified"}},
    )
    wrong_session = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "external_session_status",
            "data": {"status": "activity_unverified", "replayed": True},
        },
    )
    assert live.status_code == 400
    assert wrong_session.status_code == 400


@pytest.mark.parametrize("terminal_timing", ["before", "during_cas"])
async def test_activity_unverified_does_not_override_durable_terminal_status(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    terminal_timing: str,
) -> None:
    """An unknown replay cannot downgrade stronger structured completion evidence."""
    parent = await _create_parent_session(client, "unverified-terminal-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="Explore:completed",
    )
    conv_store.set_labels(
        child.id,
        {
            "omnigent.wrapper": "claude-code-native-ui-subagent",
            **(
                {"omnigent.subagent.terminal_status": "completed"}
                if terminal_timing == "before"
                else {}
            ),
        },
    )
    original_reconcile = SqlAlchemyConversationStore.reconcile_native_subagent_status
    injected = False

    def _complete_before_unverified_cas(
        self: SqlAlchemyConversationStore, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal injected
        injected = True
        self.set_labels(
            child.id,
            {
                "omnigent.subagent.terminal_status": "completed",
                "omnigent.subagent.activity_unverified": "",
                "omnigent.subagent.status_generation": "terminal",
            },
        )
        sessions_module._session_status_cache[child.id] = "idle"
        return original_reconcile(self, *args, **kwargs)

    if terminal_timing == "during_cas":
        monkeypatch.setattr(
            SqlAlchemyConversationStore,
            "reconcile_native_subagent_status",
            _complete_before_unverified_cas,
        )
    sessions_module._session_status_cache[child.id] = "running"
    try:
        response = await client.post(
            f"/v1/sessions/{child.id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "activity_unverified", "replayed": True},
            },
        )
        assert response.status_code == 202, response.text
        assert sessions_module._session_status_cache[child.id] == "idle"
        assert injected is (terminal_timing == "during_cas")
        row = await _child_row(client, parent["id"], child.id)
        assert row["current_task_status"] == "completed"
        assert row["activity_unverified"] is False
    finally:
        sessions_module._session_status_cache.pop(child.id, None)


# ── Empty ────────────────────────────────────────────────


async def test_child_sessions_empty_when_no_children(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A parent session with no sub-agents returns an empty page.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    # Empty page: no rows, no cursors, no more pages. Vacuous
    # `len() == 0` would still pass if the route returned ``None``
    # under has_more or omitted the cursors; the exact-match form
    # catches that drift.
    assert body["data"] == []
    assert body["first_id"] is None
    assert body["last_id"] is None
    assert body["has_more"] is False


# ── Full response shape ──────────────────────────────────


async def test_child_sessions_returns_seeded_child_with_full_shape(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A single seeded child surfaces every documented summary field.

    The tasks table has been removed — ``current_task_id`` and
    ``current_task_status`` are always ``None``. ``agent_id`` is
    populated from the conversation row's ``agent_id`` column and
    ``agent_name`` from the bound agent row. ``busy`` is derived from
    the relay-fed cache (defaults to ``False`` with no cache entry).

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    body = resp.json()

    assert len(body["data"]) == 1
    row = body["data"][0]

    # Identity + parent linkage.
    assert row["id"] == child.id
    assert row["object"] == "child_session"
    assert row["parent_session_id"] == session["id"]
    assert row["kind"] == "sub_agent"

    # Title parsing — proves the `:` partition path executed and
    # the prefix/suffix were both surfaced.
    assert row["title"] == "researcher:auth"
    assert row["tool"] == "researcher"
    assert row["session_name"] == "auth"

    # agent_id comes from the conversation row; agent_name is resolved
    # from the bound agent row (tasks table removed).
    assert row["agent_id"] == session["agent_id"]
    assert row["agent_name"] == "test-agent"
    assert row["current_task_id"] is None
    assert row["current_task_status"] is None
    assert row["last_task_error"] is None
    # No cache entry → busy=False.
    assert row["busy"] is False

    # No message items yet → no preview.
    assert row["last_message_preview"] is None

    # No outstanding elicitations → 0 (the index is empty for a freshly
    # seeded child that never published an elicitation_request).
    assert row["pending_elicitations_count"] == 0


async def test_child_sessions_surfaces_durable_failure_error(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child with runner-owned failure labels is visibly failed.

    Terminal/native harnesses can fail before a transcript item exists. The
    session-status relay persists that failure as labels; the child summary
    must project them as typed ``last_task_error`` so clients do not parse
    internal labels or render the row as idle.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )
    conv_store.set_labels(
        child.id,
        {
            sessions_module._LAST_TASK_ERROR_CODE_LABEL_KEY: "required_terminal_exited",
            sessions_module._LAST_TASK_ERROR_MESSAGE_LABEL_KEY: (
                "Required terminal exited unexpectedly"
            ),
        },
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")

    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["busy"] is False
    assert row["current_task_status"] == "failed"
    assert row["last_task_error"] == {
        "code": "required_terminal_exited",
        "message": "Required terminal exited unexpectedly",
    }


# ── Pending elicitation count ─────────────────────────────


async def test_child_sessions_surfaces_pending_elicitation_count(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child parked on an elicitation reports ``pending_elicitations_count``.

    The Agents rail reads this field to render an "awaiting input"
    badge for a sub-agent that needs attention. The count
    comes from the same in-memory index that feeds the sidebar badge;
    seed it via ``record_publish`` (the SSE publish chokepoint's hook)
    and confirm the endpoint surfaces it.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    from omnigent.runtime import pending_elicitations

    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )

    pending_elicitations.record_publish(
        child.id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_q1",
            "params": {"mode": "form", "message": "Pick one"},
        },
    )
    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    # 1 = the child is parked on one prompt; the rail badges it.
    # 0 here means the count isn't surfaced from the index, so the
    # Agents tab stays blind to a sub-agent needing input.
    assert row["pending_elicitations_count"] == 1


async def test_parent_session_snapshot_replays_child_pending_elicitation(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A parent snapshot includes outstanding child approval payloads.

    A child can publish an elicitation before the user opens the
    parent chat. The live SSE stream has no replay, so the parent
    ``GET /sessions/{id}`` snapshot must synthesize a targeted
    pending event from the child index; otherwise Nessie renders no
    actionable approval card on reload.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    from omnigent.runtime import pending_elicitations

    session = await _create_parent_session(client, agent_name="snapshot-child-pending")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:needs-approval",
        agent_id=session["agent_id"],
    )

    pending_elicitations.record_publish(
        child.id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_child_q1",
            "params": {
                "mode": "form",
                "message": "Approve child command",
                "phase": "codex_command_approval",
            },
        },
    )

    resp = await client.get(f"/v1/sessions/{session['id']}")
    assert resp.status_code == 200, resp.text
    prompts = resp.json()["pending_elicitations"]
    assert len(prompts) == 1
    assert prompts[0]["elicitation_id"] == "elicit_child_q1"
    assert prompts[0]["params"]["message"] == "Approve child command"
    assert prompts[0]["params"]["target_session_id"] == child.id


async def test_child_sessions_zero_pending_when_index_empty(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child with nothing parked reports ``pending_elicitations_count == 0``.

    Inverse of the surfacing test — the field must default to 0, not
    omit or invent a count, so the rail shows no badge for an idle
    sub-agent.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)
    _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    # No index entry → 0. A non-zero value here means the count is
    # leaking from another session or defaulting wrong.
    assert row["pending_elicitations_count"] == 0


# ── No agent_id (defensive shape) ─────────────────────────


async def test_child_sessions_handles_child_without_agent_id(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child conversation without an agent binding is surfaced with
    ``agent_id=None`` and other task fields nulled out.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="coder:fix-bug",
        agent_id=None,
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == child.id
    # Tool/session_name still parsed from title even without an agent.
    assert row["tool"] == "coder"
    assert row["session_name"] == "fix-bug"
    # Task-derived fields are absent.
    assert row["current_task_id"] is None
    assert row["current_task_status"] is None
    assert row["agent_id"] is None
    assert row["agent_name"] is None
    assert row["busy"] is False


# ── Last message preview ─────────────────────────────────


async def test_child_sessions_returns_latest_message_preview(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child with committed message items surfaces the latest message
    text as ``last_message_preview``.

    Seeds three messages and asserts the route returns the most recent
    one (not the first or a concatenation). Proves both that
    ``list_items(..., order='desc', limit=1)`` is used and that
    ``input_text`` / ``output_text`` blocks are extracted.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )
    # Use a synthetic response_id since there is no task row.
    response_id = "seed"
    # Append in chronological order so the desc lookup picks the last.
    conv_store.append(
        child.id,
        [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "find the auth bug"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    agent="researcher",
                    content=[{"type": "output_text", "text": "investigating now"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    agent="researcher",
                    content=[
                        {
                            "type": "output_text",
                            "text": "Found a stale token check in auth/middleware.py",
                        },
                    ],
                ),
            ),
        ],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["last_message_preview"] == "Found a stale token check in auth/middleware.py"


async def test_child_sessions_preview_skips_meta_messages(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Child-session previews hide durable meta messages.

    A skill invocation can append a hidden ``message.is_meta`` row
    after the last visible message. The Subagents rail must keep
    showing the latest non-meta text instead of leaking raw
    ``<skill>`` content.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )
    conv_store.append(
        child.id,
        [
            NewConversationItem(
                type="message",
                response_id="seed",
                data=MessageData(
                    role="assistant",
                    agent="researcher",
                    content=[{"type": "output_text", "text": "Visible child progress"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="seed",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "<skill>hidden</skill>"}],
                    is_meta=True,
                ),
            ),
        ],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["last_message_preview"] == "Visible child progress"


@pytest.mark.parametrize(
    "cached_status,expected_busy",
    [
        ("running", True),
        ("waiting", True),
        ("idle", False),
        ("failed", False),
    ],
)
async def test_child_sessions_busy_reflects_relay_status_cache(
    client: httpx.AsyncClient,
    db_uri: str,
    cached_status: str,
    expected_busy: bool,
) -> None:
    """
    ``busy`` mirrors ``_session_status_cache`` when it has data —
    matching the same precedence the single GET uses for ``status``.

    The tasks table is gone, so the cache is the exclusive source of
    busy state. Asserts each of the four cached values the live relay
    can produce maps to the expected ``busy``.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    :param cached_status: Status value to inject into the cache.
    :param expected_busy: Expected ``busy`` field value in the summary.
    """
    from omnigent.server.routes import sessions as sessions_module

    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )

    # Seed the cache for the child, not the parent.
    sessions_module._session_status_cache[child.id] = cached_status
    try:
        resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
        assert resp.status_code == 200
        row = resp.json()["data"][0]
        assert row["busy"] is expected_busy
    finally:
        sessions_module._session_status_cache.pop(child.id, None)


@pytest.mark.parametrize(
    ("cached_status", "expected_task_status"),
    [
        ("running", "in_progress"),
        ("waiting", "in_progress"),
        ("idle", "completed"),
        ("failed", "failed"),
    ],
)
async def test_child_sessions_current_task_status_reflects_relay_status_cache(
    client: httpx.AsyncClient,
    db_uri: str,
    cached_status: str,
    expected_task_status: str,
) -> None:
    """
    ``current_task_status`` mirrors the child lifecycle cache.

    The REST snapshot should use the same public task-status vocabulary as
    live ``session.child_session.updated`` fan-out events: active children
    are ``in_progress``, idle children are ``completed``, and failed children
    are ``failed``.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    :param cached_status: Status value to inject into the cache.
    :param expected_task_status: Expected ``current_task_status`` in the summary.
    """
    from omnigent.server.routes import sessions as sessions_module

    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )

    sessions_module._session_status_cache[child.id] = cached_status
    try:
        resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
        assert resp.status_code == 200
        row = resp.json()["data"][0]
        assert row["current_task_status"] == expected_task_status
    finally:
        sessions_module._session_status_cache.pop(child.id, None)


async def test_child_status_edge_fans_out_to_parent_stream(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child's status transition reaches the parent's stream from the server.

    The runner only fans out ``session.child_session.updated`` for children
    it registered in-process, so a child driven outside its parent's runner
    (or after a runner restart) used to change status with no parent-stream
    event at all, leaving the REPL badge and the web rail on ``Idle``. The
    server sees every transition in the status cache, so it publishes the
    child's current summary to the parent: ``running`` → busy /
    ``in_progress``, ``idle`` → ``completed``, and a repeated identical edge
    stays quiet.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    from omnigent.runtime import session_stream
    from tests.server.helpers import start_session_stream_collector

    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )
    collector = await start_session_stream_collector(session["id"])
    try:
        sessions_module._publish_status(child.id, "running")
        sessions_module._publish_status(child.id, "running")
        sessions_module._publish_status(child.id, "idle")

        updates: list[dict[str, Any]] = []
        while len(updates) < 2:
            event = await asyncio.wait_for(collector.queue.get(), timeout=5.0)
            if event.get("type") == "session.child_session.updated":
                updates.append(event)
        assert [u["child_session_id"] for u in updates] == [child.id, child.id]
        assert [u["conversation_id"] for u in updates] == [session["id"]] * 2
        assert updates[0]["child"]["busy"] is True
        assert updates[0]["child"]["current_task_status"] == "in_progress"
        assert updates[1]["child"]["busy"] is False
        assert updates[1]["child"]["current_task_status"] == "completed"
        assert updates[1]["child"]["title"] == "researcher:auth"
        await asyncio.sleep(0.2)
        assert collector.queue.empty(), "a repeated identical edge must not fan out"
    finally:
        await collector.stop()
        sessions_module._session_status_cache.pop(child.id, None)
        session_stream.close(session["id"])


async def test_child_sessions_truncates_long_message_preview(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Messages longer than the 150-char preview limit are truncated with
    a trailing ellipsis, and the total preview length stays bounded.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )
    # 200 chars — exceeds the 150 limit.
    long_text = "x" * 200
    conv_store.append(
        child.id,
        [
            NewConversationItem(
                type="message",
                response_id="seed",
                data=MessageData(
                    role="assistant",
                    agent="researcher",
                    content=[{"type": "output_text", "text": long_text}],
                ),
            ),
        ],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    preview = resp.json()["data"][0]["last_message_preview"]
    assert preview is not None
    assert preview.endswith("…")
    # The preview replaces one char with the ellipsis, so total
    # length stays at the limit. Failure indicates the truncation
    # math drifted (off-by-one, wrong cap, etc.).
    assert len(preview) == 150


# ── Title without colon (legacy / malformed) ─────────────


async def test_child_sessions_handles_title_without_colon(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child whose title has no ``:`` is still surfaced.

    The canonical spawn path always writes ``"type:name"``, but the
    schema does not enforce it. The route must treat the title as
    opaque-but-displayable (tool = raw title, session_name = None)
    rather than dropping the row or crashing.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="legacy-untyped",
        agent_id=session["agent_id"],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    rows = resp.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "legacy-untyped"
    # Defensive parse: whole title falls into `tool`, no session_name.
    assert row["tool"] == "legacy-untyped"
    assert row["session_name"] is None


# ── Title with "ui:" prefix (user-added agent from Web UI) ─


@pytest.mark.parametrize(
    "title,expected_tool,expected_session_name",
    [
        # Canonical user-added Claude Code child.
        ("ui:claude-native-ui:1", "claude-native-ui", "1"),
        # A different agent type + a multi-word label.
        ("ui:codex:my-task-2", "codex", "my-task-2"),
        # A label that itself contains colons: only the first two colons
        # are structural, so the whole remainder is the label.
        ("ui:claude-native-ui:a:b:c", "claude-native-ui", "a:b:c"),
    ],
)
async def test_child_sessions_parses_ui_added_agent_title(
    client: httpx.AsyncClient,
    db_uri: str,
    title: str,
    expected_tool: str,
    expected_session_name: str,
) -> None:
    """
    A child added from the Web UI "Add agent" picker carries the
    3-segment ``"ui:<agent_name>:<user_label>"`` title; the route
    surfaces ``tool=<agent_name>`` and ``session_name=<user_label>``
    so the Agents rail renders it like an LLM-spawned sub-agent.

    The leading ``"ui"`` sentinel distinguishes it from the 2-segment
    ``"<sub_agent_name>:<session_name>"`` form. Without the 3-segment
    branch the route would surface ``tool="ui"`` and
    ``session_name="<agent_name>:<user_label>"`` (the regression this
    guards). The colon-bearing-label case proves only the first two
    colons are structural — the remainder stays in the label.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    :param title: Seeded ``ui:``-prefixed conversation title.
    :param expected_tool: Agent name the route should surface as ``tool``.
    :param expected_session_name: Label the route should surface as
        ``session_name``.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title=title,
        agent_id=session["agent_id"],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["title"] == title
    assert row["tool"] == expected_tool
    assert row["session_name"] == expected_session_name


# ── Multiple children, ordering, pagination ───────────────


async def test_child_sessions_multiple_children_default_desc(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Multiple children come back newest-first by default.

    Seeds three children in a known order; the response's first row
    must be the LAST-seeded one. If the route ever changes the
    default sort to ascending, this assert flips and the test fails
    — protecting clients that rely on "most recent first" semantics.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    first = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:a",
        agent_id=session["agent_id"],
    )
    second = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:b",
        agent_id=session["agent_id"],
    )
    third = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:c",
        agent_id=session["agent_id"],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    rows = resp.json()["data"]
    assert [r["id"] for r in rows] == [third.id, second.id, first.id]


async def test_child_sessions_limit_pagination(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``limit`` caps page size and ``has_more`` flags the overflow.

    Three children + ``limit=2`` should return exactly 2 rows with
    ``has_more=True``. If the route forgets to forward ``limit`` or
    mis-maps ``has_more`` from the store's PagedList, this catches
    both regressions.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    for suffix in ("a", "b", "c"):
        _seed_child(
            conv_store=conv_store,
            parent_id=session["id"],
            title=f"researcher:{suffix}",
            agent_id=session["agent_id"],
        )

    resp = await client.get(
        f"/v1/sessions/{session['id']}/child_sessions",
        params={"limit": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["has_more"] is True


# ── Scoping — parent isolation ────────────────────────────


async def test_child_sessions_scoped_to_requested_parent(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Children of session A do not leak into session B's listing.

    Without the ``parent_conversation_id`` filter on
    ``list_conversations``, the route would return every sub-agent
    conversation in the DB. This test seeds children under two
    distinct parents and asserts the response only contains the
    requested parent's rows.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session_a = await _create_parent_session(client, agent_name="agent-a")
    session_b = await _create_parent_session(client, agent_name="agent-b")
    conv_store = SqlAlchemyConversationStore(db_uri)

    child_a = _seed_child(
        conv_store=conv_store,
        parent_id=session_a["id"],
        title="researcher:only-in-a",
        agent_id=session_a["agent_id"],
    )
    child_b = _seed_child(
        conv_store=conv_store,
        parent_id=session_b["id"],
        title="researcher:only-in-b",
        agent_id=session_b["agent_id"],
    )

    resp_a = await client.get(f"/v1/sessions/{session_a['id']}/child_sessions")
    ids_a = [r["id"] for r in resp_a.json()["data"]]
    assert ids_a == [child_a.id]

    resp_b = await client.get(f"/v1/sessions/{session_b['id']}/child_sessions")
    ids_b = [r["id"] for r in resp_b.json()["data"]]
    assert ids_b == [child_b.id]


async def test_closed_child_session_display_is_sanitized_and_read_only(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Closed child sessions hide the internal tombstone and reject chat.

    Legacy closed rows only have a ``:closed:<id>`` title suffix. The
    API must strip that suffix from display fields, synthesize the
    ``omnigent.closed=true`` label for clients, and reject new user
    messages sent directly to the child session.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )
    tombstoned_title = f"researcher:auth:closed:{child.id}"
    conv_store.update_conversation(child.id, title=tombstoned_title)

    children_resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert children_resp.status_code == 200
    row = children_resp.json()["data"][0]
    assert row["title"] == "researcher:auth"
    assert row["tool"] == "researcher"
    assert row["session_name"] == "auth"
    assert row["labels"][CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE

    snapshot_resp = await client.get(f"/v1/sessions/{child.id}")
    assert snapshot_resp.status_code == 200
    snapshot = snapshot_resp.json()
    assert snapshot["title"] == "researcher:auth"
    assert snapshot["labels"][CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE

    message_resp = await client.post(
        f"/v1/sessions/{child.id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "please continue"}],
            },
        },
    )
    assert message_resp.status_code == 409
    assert "Session is closed" in message_resp.text


# ── Per-child attribution across a 5-10 fan-out ───────────


async def test_child_sessions_per_child_fields_isolated_across_fanout(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With a realistic 5-10 sub-agent fan-out, every per-child field
    stays attributed to its own row — no cross-child bleed.

    The route batch-loads latest message candidates for all children,
    then builds each summary from that map plus the in-memory
    ``_session_status_cache``. The existing multi-child tests only assert
    id ordering; the single-child preview/busy tests can't catch a lookup
    keyed on the wrong id. This seeds eight children, each with a
    distinct latest message and a distinct cached status, then asserts
    the response maps each field back to the correct child.

    The test also monkeypatches ``list_items`` to raise after seeding.
    If the route regresses to the old per-child N+1 lookup, the request
    returns 500 instead of 200.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    :param monkeypatch: Pytest monkeypatch fixture used to reject the
        old per-child item-listing path.
    """
    from omnigent.server.routes import sessions as sessions_module

    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    # Eight children — above the "typical 1-5" fan-out the route's
    # N+1 note calls out, so the per-row loop runs enough iterations
    # for a mis-keyed lookup to surface.
    fanout = 8
    # running/waiting → busy True; idle/failed/absent → busy False.
    # Cycling four cached values (plus one uncached) proves the busy
    # bit tracks each child's own cache entry, not a shared default.
    cache_cycle = ["running", "waiting", "idle", "failed", None]

    @dataclass
    class _Expected:
        child_id: str
        tool: str
        session_name: str
        preview: str
        busy: bool

    expected: dict[str, _Expected] = {}
    seeded_cache_ids: list[str] = []
    for i in range(fanout):
        tool = f"agent{i}"
        session_name = f"task-{i}"
        child = _seed_child(
            conv_store=conv_store,
            parent_id=session["id"],
            title=f"{tool}:{session_name}",
            agent_id=session["agent_id"],
        )
        # Distinct latest message per child so a misaligned preview
        # query maps the wrong text and the assertion catches it.
        preview = f"child {i} latest status line"
        conv_store.append(
            child.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="seed",
                    data=MessageData(
                        role="assistant",
                        agent=tool,
                        content=[{"type": "output_text", "text": preview}],
                    ),
                ),
            ],
        )
        cached_status = cache_cycle[i % len(cache_cycle)]
        if cached_status is not None:
            sessions_module._session_status_cache[child.id] = cached_status
            seeded_cache_ids.append(child.id)
        expected[child.id] = _Expected(
            child_id=child.id,
            tool=tool,
            session_name=session_name,
            preview=preview,
            busy=cached_status in ("running", "waiting"),
        )

    try:

        def _fail_list_items(
            _self: SqlAlchemyConversationStore,
            conversation_id: str,
            limit: int = 100,
            after: str | None = None,
            before: str | None = None,
            order: str = "asc",
            type: str | None = None,
        ) -> NoReturn:
            """Fail if child summary rendering uses the old N+1 path.

            The signature mirrors ``SqlAlchemyConversationStore.list_items``
            so keyword calls reach this assertion instead of failing with a
            shape mismatch.

            :param _self: Conversation store instance passed by method binding.
            :param conversation_id: Child conversation id passed by the old path.
            :param limit: Item-page limit passed by the old path.
            :param after: Optional forward cursor passed by the old path.
            :param before: Optional backward cursor passed by the old path.
            :param order: Sort order passed by the old path.
            :param type: Optional item type passed by the old path.
            :returns: Never returns.
            :raises AssertionError: Always, because this path is forbidden.
            """
            del _self, conversation_id, limit, after, before, order, type
            raise AssertionError("child summaries must use the batched preview query")

        monkeypatch.setattr(SqlAlchemyConversationStore, "list_items", _fail_list_items)
        # Default limit is 20, so all eight come back in one page.
        resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
        assert resp.status_code == 200
        rows = resp.json()["data"]
        # All seeded children present — a short page would mean the
        # route dropped rows or the default limit shrank below the
        # fan-out.
        assert len(rows) == fanout

        by_id = {row["id"]: row for row in rows}
        # No id appeared twice and none were lost in the loop.
        assert set(by_id) == set(expected)

        for child_id, exp in expected.items():
            row = by_id[child_id]
            # Each field must come from THIS child's row/cache, not a
            # neighbor's. A mismatch on any one points at a lookup
            # keyed on the wrong id during the per-child build.
            assert row["tool"] == exp.tool
            assert row["session_name"] == exp.session_name
            assert row["last_message_preview"] == exp.preview
            assert row["busy"] is exp.busy
            assert row["agent_id"] == session["agent_id"]
    finally:
        for cid in seeded_cache_ids:
            sessions_module._session_status_cache.pop(cid, None)


# ── Native-harness sub-agent terminal-UI label stamping ──────────────


def _bundle_with_harnessed_subagents(name: str, sub_agents: list[dict[str, Any]]) -> bytes:
    """
    Build a bundle whose sub-agents carry an explicit executor harness.

    ``tests.server.helpers.build_agent_bundle`` writes sub-agent configs
    without an ``executor`` block, so it can't express a native harness.
    This minimal builder writes ``agents/<dir>/config.yaml`` with the
    given ``harness`` so the create-session path can resolve a native
    sub-agent's harness from the parent bundle.

    :param name: Parent agent name, e.g. ``"nessie-like"``.
    :param sub_agents: Sub-agent dicts, each with ``name`` and ``harness``
        and an optional ``config`` mapping merged into the sub-agent's
        ``executor.config`` (e.g.
        ``{"name": "impl", "harness": "claude-native",
        "config": {"permission_mode": "bypassPermissions"}}`` to declare
        YOLO bypass).
    :returns: A gzipped tar bundle.
    """
    config: dict[str, Any] = {
        "spec_version": 1,
        "name": name,
        "llm": {"model": name, "connection": {"api_key": "test-key"}},
        "executor": {"config": {"harness": "claude-sdk"}},
        "tools": {"agents": [sa["name"] for sa in sub_agents]},
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        cfg = yaml.dump(config).encode()
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(cfg)
        tf.addfile(info, io.BytesIO(cfg))
        for sa in sub_agents:
            sa_config = {
                "spec_version": 1,
                "name": sa["name"],
                "llm": {"model": sa["name"], "connection": {"api_key": "test-key"}},
                # Merge any extra config (e.g. permission_mode / yolo) over
                # the harness so YOLO-declaring bundles can be expressed.
                "executor": {"config": {"harness": sa["harness"], **sa.get("config", {})}},
            }
            sa_bytes = yaml.dump(sa_config).encode()
            sa_info = tarfile.TarInfo(name=f"agents/{sa['name']}/config.yaml")
            sa_info.size = len(sa_bytes)
            tf.addfile(sa_info, io.BytesIO(sa_bytes))
    return buf.getvalue()


async def _create_parent_with_subagents(
    client: httpx.AsyncClient,
    name: str,
    sub_agents: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Register a bundle with harnessed sub-agents and create a parent session.

    :param client: The test HTTP client.
    :param name: Parent agent name (must be unique within the test DB).
    :param sub_agents: Sub-agent dicts with ``name`` + ``harness`` and an
        optional ``config`` mapping (see
        :func:`_bundle_with_harnessed_subagents`).
    :returns: A dict with ``session_id`` (the parent session) and
        ``agent_id`` (the durable agent id resolved from the session).
    """
    bundle = _bundle_with_harnessed_subagents(name, sub_agents)
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code == 201, f"parent create failed: {resp.text}"
    session_id = resp.json()["session_id"]
    agent_resp = await client.get(f"/v1/sessions/{session_id}/agent")
    assert agent_resp.status_code == 200, f"parent agent lookup failed: {agent_resp.text}"
    return {"session_id": session_id, "agent_id": agent_resp.json()["id"]}


@pytest.mark.parametrize(
    "harness,expected_wrapper",
    [
        ("claude-native", "claude-code-native-ui"),
        ("codex-native", "codex-native-ui"),
    ],
)
async def test_native_subagent_session_stamps_terminal_ui_labels(
    client: httpx.AsyncClient,
    harness: str,
    expected_wrapper: str,
) -> None:
    """
    A sub-agent whose spec uses a native terminal harness gets the
    terminal-first wrapper labels at create time, so the web UI renders
    the Chat/Terminal pill (gated on ``omnigent.ui == "terminal"``).

    Without the stamping, the child row's labels are empty and the pill
    never shows for nessie-style native implementer sub-agents.
    """
    parent = await _create_parent_with_subagents(
        client,
        name=f"orch-{harness}",
        sub_agents=[{"name": "impl", "harness": harness}],
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "impl:task-1",
            "sub_agent_name": "impl",
        },
    )
    assert resp.status_code == 201, resp.text
    labels = resp.json()["labels"]
    assert labels.get("omnigent.wrapper") == expected_wrapper
    assert labels.get("omnigent.ui") == "terminal"


@pytest.mark.parametrize(
    "harness,sub_config,expected_args",
    [
        (
            "claude-native",
            {"permission_mode": "bypassPermissions"},
            ["--permission-mode", "bypassPermissions"],
        ),
        (
            "codex-native",
            {"yolo": True},
            ["--dangerously-bypass-approvals-and-sandbox"],
        ),
        (
            "cursor-native",
            {"yolo": True},
            ["--yolo"],
        ),
    ],
)
async def test_native_subagent_yolo_args_derived_from_trusted_spec(
    client: httpx.AsyncClient,
    harness: str,
    sub_config: dict[str, Any],
    expected_args: list[str],
) -> None:
    """
    A YOLO-declaring native worker bundle gets bypass ``terminal_launch_args``.

    The worker sub-agent's own bundle declares its full-bypass intent
    (``permission_mode: bypassPermissions`` for claude-native,
    ``yolo: true`` for codex-native / cursor-native). On a sub-agent create,
    the server derives the matching flag list from that trusted,
    server-loaded spec and persists it as the child session's
    ``terminal_launch_args`` — which the runner appends to the native CLI
    argv so the headless worker can edit without stalling on an
    ApprovalCard.

    A failure here means the translation seam regressed and the worker
    would launch in its default prompting mode (and hang headless).
    """
    parent = await _create_parent_with_subagents(
        client,
        name=f"orch-yolo-{harness}",
        sub_agents=[{"name": "impl", "harness": harness, "config": sub_config}],
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "impl:task-yolo",
            "sub_agent_name": "impl",
        },
    )
    assert resp.status_code == 201, resp.text
    # The persisted child session carries exactly the YOLO bypass flags;
    # an empty / None value would mean the worker launches prompting.
    assert resp.json()["terminal_launch_args"] == expected_args


async def test_native_subagent_yolo_args_reject_overlong_spec_value(
    client: httpx.AsyncClient,
) -> None:
    """
    Overlong spec-derived launch args fail as ``invalid_input``.

    ``permission_mode`` is declared in the uploaded bundle, but it is
    still persisted as a native CLI argument. The create path must run
    derived args through the same bounds as request-supplied
    ``terminal_launch_args`` and return a client-correctable 400 instead
    of writing an oversized row or surfacing an internal error.
    """
    # Route validation caps each terminal_launch_args entry at 4096
    # bytes/chars; one more proves the derived path is bounded too.
    parent = await _create_parent_with_subagents(
        client,
        name="orch-yolo-overlong-permission-mode",
        sub_agents=[
            {
                "name": "impl",
                "harness": "claude-native",
                "config": {"permission_mode": "x" * 4097},
            }
        ],
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "impl:task-yolo",
            "sub_agent_name": "impl",
        },
    )
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert "invalid terminal_launch_args in sub-agent spec" in error["message"]


async def test_subagent_create_rejects_undeclared_name(
    client: httpx.AsyncClient,
) -> None:
    """
    A ``sub_agent_name`` the parent's spec does not declare fails the create.

    The downstream spec-swap sites are all guarded by ``if ... is not None``
    with no ``else``: a name that resolves to nothing would leave the
    parent's spec, workdir, harness and instructions in place and boot the
    child as a full clone of the parent — silently escalating a worker to
    the orchestrator's capability and instruction surface. The create route
    must reject the undeclared name up front (404) so nothing is persisted,
    mirroring normal ``sys_session_send`` dispatch and the AGENTSPEC.md
    contract that unlisted names are rejected.
    """
    parent = await _create_parent_with_subagents(
        client,
        name="orch-undeclared-subagent",
        sub_agents=[{"name": "impl", "harness": "claude-native"}],
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "ghost:task",
            "sub_agent_name": "does-not-exist",
        },
    )
    assert resp.status_code == 404, resp.text
    error = resp.json()["error"]
    assert error["code"] == "not_found"
    assert "does-not-exist" in error["message"]


@pytest.mark.parametrize(
    "sub_config,expected_persisted",
    [
        # No bypass declared in the trusted spec -> the server derives
        # nothing, so the smuggled flag must NOT be persisted.
        ({}, None),
        # Bypass IS declared -> the server derives the YOLO flag from the
        # trusted spec; the smuggled flag must still be ignored.
        ({"permission_mode": "bypassPermissions"}, ["--permission-mode", "bypassPermissions"]),
    ],
)
async def test_subagent_create_ignores_caller_supplied_launch_args(
    client: httpx.AsyncClient,
    sub_config: dict[str, Any],
    expected_persisted: list[str] | None,
) -> None:
    """
    Caller-supplied ``terminal_launch_args`` never influence a sub-agent create.

    The security boundary: launch wiring for a sub-agent is derived ONLY
    from the trusted, server-loaded sub-spec. A caller who smuggles
    ``terminal_launch_args`` into the sub-agent create body must not be
    able to inject CLI flags into the worker's launch — the persisted
    value must equal what the trusted spec derives (``None`` when the
    spec declares no bypass; the derived YOLO flags when it does), never
    the caller's injected list.

    A failure here means the spawn body became a launch-arg injection
    vector — a caller could, e.g., pass ``--permission-mode
    bypassPermissions`` to a non-YOLO worker and escalate it.
    """
    parent = await _create_parent_with_subagents(
        client,
        name=f"orch-inject-{'yolo' if sub_config else 'plain'}",
        sub_agents=[{"name": "impl", "harness": "claude-native", "config": sub_config}],
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "impl:task-inject",
            "sub_agent_name": "impl",
            # Smuggled flags a caller should not be able to apply.
            "terminal_launch_args": ["--dangerously-skip-permissions", "--evil"],
        },
    )
    assert resp.status_code == 201, resp.text
    # Persisted value is what the trusted spec derives, NOT the body args.
    assert resp.json()["terminal_launch_args"] == expected_persisted


@pytest.mark.parametrize(
    "harness,expected_wrapper,expected_model,expected_terminal",
    [
        ("claude-native", "claude-code-native-ui", "claude-native-ui", "claude"),
        ("codex-native", "codex-native-ui", "codex-native-ui", "codex"),
    ],
)
async def test_native_subagent_message_uses_native_terminal_forward(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    expected_wrapper: str,
    expected_model: str,
    expected_terminal: str,
) -> None:
    """
    Native-harness sub-agent child messages take the terminal bypass.

    A ``sys_session_send`` call creates a child session and then posts a
    user message to that child. If the child sub-agent uses
    ``claude-native`` or ``codex-native``, Omnigent must forward the prompt to
    the runner's native terminal event shape and must not persist its
    own AP-side copy; the native transcript forwarder is the single
    writer for conversation items.

    :param client: Test HTTP client.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param harness: Native harness declared by the sub-agent spec,
        e.g. ``"claude-native"``.
    :param expected_wrapper: Wrapper label expected on the child row,
        e.g. ``"claude-code-native-ui"``.
    :param expected_model: Native wrapper model forwarded to the
        runner, e.g. ``"claude-native-ui"``.
    :param expected_terminal: Native terminal resource name sent to
        the runner ensure endpoint, e.g. ``"claude"``.
    """
    parent = await _create_parent_with_subagents(
        client,
        name=f"orch-forward-{harness}",
        sub_agents=[{"name": "impl", "harness": harness}],
    )
    child_resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "impl:task-2",
            "sub_agent_name": "impl",
        },
    )
    assert child_resp.status_code == 201, child_resp.text
    child = child_resp.json()
    assert child["labels"].get("omnigent.wrapper") == expected_wrapper

    forwarded: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """
        Capture the event Omnigent forwards to the fake runner.

        :param request: HTTP request sent to the fake runner.
        :returns: Accepted response.
        """
        forwarded.append(
            {
                "path": request.url.path,
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(204)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )

    async def _fake_get_runner_client(
        session_id: str,
        runner_router: object,
    ) -> httpx.AsyncClient:
        """
        Route the native child message to the fake runner.

        :param session_id: Session being routed, e.g. ``"ff5cac23d0beb79fad914046049f32ff"``.
        :param runner_router: Real runner router, unused.
        :returns: The fake runner client.
        """
        del session_id, runner_router
        return fake_runner

    monkeypatch.setattr(sessions_module, "_get_runner_client", _fake_get_runner_client)
    try:
        message_resp = await client.post(
            f"/v1/sessions/{child['id']}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "build the patch"}],
                },
            },
        )
    finally:
        await fake_runner.aclose()

    assert message_resp.status_code == 202, message_resp.text
    # Native (claude-/codex-native) message bypass returns queued=True plus a
    # pending-input id: the message isn't persisted AP-side (the transcript
    # forwarder is the single writer), so the server records a pending-input
    # entry for the optimistic bubble and returns its id.
    message_body = message_resp.json()
    assert message_body["queued"] is True
    assert message_body["pending_id"].startswith("pending_")
    assert forwarded == [
        {
            "path": f"/v1/sessions/{child['id']}/resources/terminals",
            "body": {
                "terminal": expected_terminal,
                "session_key": "main",
                "ensure_native_terminal": True,
                "persist_resource_event": True,
            },
        },
        {
            "path": f"/v1/sessions/{child['id']}/events",
            "body": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "build the patch"}],
                "model": expected_model,
                "harness": harness,
                "agent_id": parent["agent_id"],
            },
        },
    ]

    items_resp = await client.get(f"/v1/sessions/{child['id']}/items")
    assert items_resp.status_code == 200, items_resp.text
    assert items_resp.json()["data"] == [], (
        "Native sub-agent prompts must not be persisted by AP; the native "
        "forwarder mirrors accepted terminal transcript items later."
    )


async def test_non_native_subagent_session_has_no_terminal_ui_labels(
    client: httpx.AsyncClient,
) -> None:
    """
    A sub-agent on a non-native harness (e.g. ``claude-sdk``) must NOT get
    the terminal-first labels — it's a headless chat sub-agent with no
    takeover terminal, so the pill must stay hidden.
    """
    parent = await _create_parent_with_subagents(
        client,
        name="orch-sdk",
        sub_agents=[{"name": "reviewer", "harness": "claude-sdk"}],
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "reviewer:task-1",
            "sub_agent_name": "reviewer",
        },
    )
    assert resp.status_code == 201, resp.text
    labels = resp.json()["labels"]
    assert "omnigent.wrapper" not in labels
    assert "omnigent.ui" not in labels


# ── Multipart (bundled) child creates ────────────────────


async def test_multipart_create_with_parent_links_child(
    client: httpx.AsyncClient,
) -> None:
    """
    A multipart create with ``metadata.parent_session_id`` produces a
    sub-agent child of that session bound to the freshly uploaded agent.

    This is the bundle-mode ``sys_session_create`` server path. The
    child must land in the parent's tree (parent linkage + child_sessions
    listing) and the response must carry the created agent identifiers —
    the runner builds the orchestrator's handle from them.
    """
    parent = await _create_parent_session(client, agent_name="bundle-parent")
    child_bundle = build_agent_bundle(name="bundle-child")
    resp = await client.post(
        "/v1/sessions",
        data={
            "metadata": json.dumps({"parent_session_id": parent["id"], "title": "bundled helper"})
        },
        files={"bundle": ("agent.tar.gz", child_bundle, "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    child_id = body["session_id"]
    # The created agent identifiers prove the response contract the
    # runner's bundle-mode handle depends on; a missing/empty agent_id
    # would make sys_session_create fail loud on the runner side.
    assert len(body["agent_id"]) == 32
    assert body["agent_name"] == "bundle-child"

    snap = await client.get(f"/v1/sessions/{child_id}")
    assert snap.status_code == 200, snap.text
    # Parent linkage + agent binding traversed metadata → store → row.
    assert snap.json()["parent_session_id"] == parent["id"]
    assert snap.json()["agent_id"] == body["agent_id"]

    listing = await client.get(f"/v1/sessions/{parent['id']}/child_sessions")
    assert listing.status_code == 200, listing.text
    listed_ids = [c["id"] for c in listing.json()["data"]]
    # kind="sub_agent" is what the child_sessions listing filters on —
    # absence here means the multipart path created a top-level row.
    assert child_id in listed_ids


async def test_multipart_child_init_notify_carries_global_instructions(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bundled child's runner notify carries the full session-init envelope.

    A config-path child inherits the parent's runner, and the legacy id-only
    notify left the runner without the global instructions text — its terminal
    launch then missed them.
    """
    from omnigent.runtime import _globals
    from omnigent.stores.global_instructions_store.sqlalchemy_store import (
        SqlAlchemyGlobalInstructionsStore,
    )

    parent = await _create_parent_session(client, agent_name="bundle-init-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    assert conv_store.set_runner_id(parent["id"], "runner-bundled-init")

    global_text = "always run the focused test [bundle-init-marker]"
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

    monkeypatch.setattr(sessions_module, "_get_runner_client", _resolve_bound_runner)

    child_bundle = build_agent_bundle(name="bundle-init-child")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"parent_session_id": parent["id"]})},
        files={"bundle": ("agent.tar.gz", child_bundle, "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["session_id"]

    init_bodies = [
        body
        for path, body in posts
        if path == "/v1/sessions" and isinstance(body, dict) and body.get("session_id") == child_id
    ]
    assert len(init_bodies) == 1, posts
    snapshot = init_bodies[0]["session_init"]["snapshot"]
    assert snapshot["global_instructions"] == global_text, init_bodies[0]


@pytest.mark.parametrize(
    "harness,config,expected_args",
    [
        (
            "codex-native",
            {"yolo": True},
            ["--dangerously-bypass-approvals-and-sandbox"],
        ),
        (
            "claude-native",
            {"permission_mode": "bypassPermissions"},
            ["--permission-mode", "bypassPermissions"],
        ),
    ],
)
async def test_multipart_child_derives_native_bypass_args_from_uploaded_spec(
    client: httpx.AsyncClient,
    harness: str,
    config: dict[str, Any],
    expected_args: list[str],
) -> None:
    """A config-path child persists the uploaded agent's bypass stance."""
    parent = await _create_parent_session(client, agent_name=f"bundle-yolo-parent-{harness}")
    child_bundle = build_agent_bundle(
        name=f"bundle-yolo-child-{harness}",
        executor={"type": "omnigent", "config": {"harness": harness, **config}},
        include_llm=False,
    )

    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"parent_session_id": parent["id"]})},
        files={"bundle": ("agent.tar.gz", child_bundle, "application/gzip")},
    )

    assert resp.status_code == 201, resp.text
    child = await client.get(f"/v1/sessions/{resp.json()['session_id']}")
    assert child.status_code == 200, child.text
    assert child.json()["terminal_launch_args"] == expected_args


async def test_multipart_create_with_unknown_parent_404s(
    client: httpx.AsyncClient,
) -> None:
    """
    A multipart create pointing at a nonexistent parent fails with 404
    and creates nothing.

    Without the parent existence check failing loud, the create would
    either orphan a child row or 500 on the FK — both leak a stored
    bundle with no usable session.
    """
    bundle = build_agent_bundle(name="bundle-orphan")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"parent_session_id": "5eca720dc2bc6cdc3a99028d7bd0f917"})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code == 404, resp.text


async def _create_native_child(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
    """
    Create a claude-native sub-agent child under a fresh parent.

    :param client: The test HTTP client.
    :param name: Unique parent agent name for this test.
    :returns: The created child session JSON.
    """
    parent = await _create_parent_with_subagents(
        client,
        name=name,
        sub_agents=[{"name": "impl", "harness": "claude-native"}],
    )
    child_resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "impl:task-1",
            "sub_agent_name": "impl",
        },
    )
    assert child_resp.status_code == 201, child_resp.text
    return child_resp.json()


async def test_subagent_idle_forward_recovers_via_parent_when_child_runner_stale(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A sub-agent ``idle`` whose direct forward 503s is re-delivered via recovery.

    Reproduces the production hang's server edge: the child's pinned runner is
    gone (direct ``_forward_session_change_to_runner`` returns ``None``), so the
    terminal-status branch must invoke
    ``_recover_subagent_status_forward_via_parent`` and, when it lands, accept
    the event (``202`` — the parent gets the child result) instead of the old
    hard ``503`` that left the parent hanging.
    """
    child = await _create_native_child(client, name="orch-recover-ok")

    async def _forward_none(*_args: Any, **_kwargs: Any) -> None:
        """Child's pinned runner is unreachable — the direct forward fails."""
        return

    recovered_for: list[str] = []

    async def _recover_spy(child_conv: Any, *_args: Any, **_kwargs: Any) -> Any:
        """Stand in for recovery: record the child and report a delivered 202."""
        recovered_for.append(child_conv.id)
        return sessions_module._RunnerForwardResult(status_code=202, body="")

    monkeypatch.setattr(sessions_module, "_forward_session_change_to_runner", _forward_none)
    monkeypatch.setattr(
        sessions_module, "_recover_subagent_status_forward_via_parent", _recover_spy
    )

    resp = await client.post(
        f"/v1/sessions/{child['id']}/events",
        json={"type": "external_session_status", "data": {"status": "idle"}},
    )

    # 202 Accepted is the endpoint's success code; the body confirms the event
    # was handled (not the old 503 that stranded the parent).
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}
    # Recovery was invoked for THIS child (the stale-binding heal path).
    assert recovered_for == [child["id"]]


async def test_subagent_background_task_count_still_delivers_to_parent(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lingering background shell must not strand the parent orchestrator.

    Regression for the parent-orchestrator hang. The ``Stop`` turn-end edge
    carries the background-shell count, and the terminal-delivery branch fires
    only for ``idle``/``failed`` — so the edge has to stay ``idle`` and let the
    count ride alongside. (It used to be relabeled to ``waiting`` for the
    spinner's sake, which skipped delivery and made the parent wait forever;
    the spinner now stays lit off the count instead.)
    """
    child = await _create_native_child(client, name="orch-bg-waiting")

    async def _forward_none(*_args: Any, **_kwargs: Any) -> None:
        """Force the direct forward to miss so delivery takes the recovery path."""
        return

    recovered_for: list[str] = []

    async def _recover_spy(child_conv: Any, *_args: Any, **_kwargs: Any) -> Any:
        recovered_for.append(child_conv.id)
        return sessions_module._RunnerForwardResult(status_code=202, body="")

    monkeypatch.setattr(sessions_module, "_forward_session_change_to_runner", _forward_none)
    monkeypatch.setattr(
        sessions_module, "_recover_subagent_status_forward_via_parent", _recover_spy
    )

    resp = await client.post(
        f"/v1/sessions/{child['id']}/events",
        json={
            "type": "external_session_status",
            "data": {"status": "idle", "background_task_count": 1},
        },
    )

    # A positive count does not suppress delivery: the terminal-status branch
    # ran for THIS child (recovery invoked, 202 Accepted) rather than silently
    # skipping and stranding the parent.
    assert resp.status_code == 202, resp.text
    assert recovered_for == [child["id"]]


async def test_subagent_idle_forward_503s_when_recovery_also_fails(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When recovery cannot reach a live parent runner either, the 503 is preserved.

    The runner re-posts on a 503, so failing here (rather than acking a
    delivery that never happened) keeps the at-least-once contract intact.
    """
    child = await _create_native_child(client, name="orch-recover-fail")

    async def _forward_none(*_args: Any, **_kwargs: Any) -> None:
        """Both the direct forward and (below) recovery cannot reach a runner."""
        return

    async def _recover_none(*_args: Any, **_kwargs: Any) -> None:
        """Recovery also fails to resolve a live parent runner."""
        return

    monkeypatch.setattr(sessions_module, "_forward_session_change_to_runner", _forward_none)
    monkeypatch.setattr(
        sessions_module, "_recover_subagent_status_forward_via_parent", _recover_none
    )

    resp = await client.post(
        f"/v1/sessions/{child['id']}/events",
        json={"type": "external_session_status", "data": {"status": "idle"}},
    )

    assert resp.status_code == 503, resp.text


# ── message-send stale-runner heal (issue #3067) ─────────────────────────────


async def test_subagent_message_heals_stale_runner_binding_via_parent(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Sending a message to a sub-agent with a stale runner_id succeeds when the
    parent has a live replacement runner.

    Regression for #3067: the message-send path previously returned a permanent
    503 for any sub-agent whose runner had idle-timed-out, even while the
    parent's replacement runner was healthy.  After the fix the path calls
    ``_heal_subagent_runner_binding_via_parent``, which rebinds the child's DB
    row to the parent's live runner and returns the runner client, allowing
    normal message dispatch to proceed.
    """
    child = await _create_native_child(client, name="msg-heal-ok")

    forwarded: list[dict[str, Any]] = []

    def _runner_handler(request: httpx.Request) -> httpx.Response:
        forwarded.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx.Response(204)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_runner_handler),
        base_url="http://runner",
    )

    # _get_runner_client returns None on the first call (the child's stale
    # runner_id resolves nothing) and the fake runner on subsequent calls
    # (the heal resolved the parent's live runner).
    call_count = 0

    async def _runner_client_stub(
        _session_id: str,
        _runner_router: object,
    ) -> httpx.AsyncClient | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None
        return fake_runner

    healed_for: list[str] = []

    async def _heal_spy(child_conv: Any, *_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        healed_for.append(child_conv.id)
        return fake_runner

    monkeypatch.setattr(routes_events_module, "_get_runner_client", _runner_client_stub)
    monkeypatch.setattr(
        routes_events_module, "_heal_subagent_runner_binding_via_parent", _heal_spy
    )

    async def _no_init(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(routes_events_module, "_ensure_runner_session_initialized", _no_init)

    resp = await client.post(
        f"/v1/sessions/{child['id']}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        },
    )

    assert resp.status_code in {200, 202}, resp.text
    assert healed_for == [child["id"]], "heal was not invoked for the stale child"


async def test_subagent_message_503s_when_heal_finds_no_live_ancestor(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When both the child's runner and the parent runner are unavailable, the
    message-send path still returns 503 — recovery must not silently pick an
    unrelated runner.
    """
    child = await _create_native_child(client, name="msg-heal-no-ancestor")

    async def _runner_none(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _heal_none(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(routes_events_module, "_get_runner_client", _runner_none)
    monkeypatch.setattr(
        routes_events_module, "_heal_subagent_runner_binding_via_parent", _heal_none
    )

    resp = await client.post(
        f"/v1/sessions/{child['id']}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        },
    )

    assert resp.status_code == 503, resp.text


async def test_non_subagent_session_not_healed_via_parent(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A top-level session with host_id=None (e.g. CLI-launched) is never treated
    as a recoverable sub-agent child — the heal path is guarded to
    ``kind == "sub_agent"`` only.
    """
    # Create a plain top-level session (no parent).
    agent = await create_test_agent(client, name="msg-heal-toplevel")
    session_resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
    )
    assert session_resp.status_code == 201, session_resp.text
    session_id = session_resp.json()["id"]

    heal_called: list[bool] = []

    async def _heal_spy(*_args: Any, **_kwargs: Any) -> None:
        heal_called.append(True)
        return

    async def _runner_none(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(routes_events_module, "_get_runner_client", _runner_none)
    monkeypatch.setattr(
        routes_events_module, "_heal_subagent_runner_binding_via_parent", _heal_spy
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        },
    )

    assert resp.status_code == 503, resp.text
    assert not heal_called, "heal must not run for a top-level session"


async def test_sdk_subagent_heal_skips_session_init(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    For SDK (non-native) sub-agents, message-send after heal must NOT call
    ``_ensure_runner_session_initialized``.

    SDK sub-agent sessions are loaded in-process by the runner on startup; the
    parent's live runner already holds the child's session state. Calling
    ``_ensure_runner_session_initialized`` would be a spurious timeout at best.
    This test pins the contract: the heal path sets
    ``_runner_needs_session_init = False`` for non-native harnesses.
    """
    parent = await _create_parent_with_subagents(
        client,
        name="msg-heal-sdk-no-init",
        sub_agents=[{"name": "impl", "harness": "claude-sdk"}],
    )
    child_resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "impl:task-sdk",
            "sub_agent_name": "impl",
        },
    )
    assert child_resp.status_code == 201, child_resp.text
    child = child_resp.json()

    forwarded: list[dict[str, Any]] = []

    def _runner_handler(request: httpx.Request) -> httpx.Response:
        forwarded.append({"path": request.url.path})
        return httpx.Response(204)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_runner_handler),
        base_url="http://runner",
    )

    async def _heal_spy(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return fake_runner

    init_called: list[bool] = []

    async def _init_spy(*_a: Any, **_k: Any) -> bool:
        init_called.append(True)
        return False

    async def _runner_none(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(routes_events_module, "_get_runner_client", _runner_none)
    monkeypatch.setattr(
        routes_events_module, "_heal_subagent_runner_binding_via_parent", _heal_spy
    )
    monkeypatch.setattr(routes_events_module, "_ensure_runner_session_initialized", _init_spy)

    resp = await client.post(
        f"/v1/sessions/{child['id']}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        },
    )

    assert resp.status_code in {200, 202}, resp.text
    assert not init_called, (
        "_ensure_runner_session_initialized must not be called for SDK sub-agents after heal"
    )


# ── Promotion (forking a child) ───────────────────────────


async def test_fork_of_child_promotes_it_into_the_sidebar(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Forking a sub-agent yields a session the sidebar lists.

    This is the promotion path end to end. The sidebar asks for
    ``kind="default"``, which is derived from parent-nullness, so the
    fork only surfaces there if the copy is genuinely parentless — and
    the source has to stay put, since promotion copies rather than
    moves the child out of its parent's tree.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    parent = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="researcher:auth",
        agent_id=parent["agent_id"],
    )

    resp = await client.post(f"/v1/sessions/{child.id}/fork", json={"title": "Promoted"})
    assert resp.status_code == 201, f"promoting a sub-agent failed: {resp.text}"
    promoted = resp.json()

    assert promoted["id"] != child.id, "promotion must produce a new session"
    assert promoted["parent_session_id"] is None, (
        f"promoted session must have no parent, got {promoted['parent_session_id']!r}"
    )
    assert promoted["kind"] == "default", (
        f"promoted session must not read as a sub-agent, got {promoted['kind']!r}"
    )

    # The sidebar's own query (default kind) must now include it.
    listing = await client.get("/v1/sessions")
    assert listing.status_code == 200, listing.text
    listed = {row["id"] for row in listing.json()["data"]}
    assert promoted["id"] in listed, (
        f"promoted session {promoted['id']} missing from the sidebar list {listed}"
    )
    assert child.id not in listed, "the source child must stay out of the sidebar"

    # The source keeps its place under the parent, and the promoted copy
    # never joins it there.
    children = await client.get(f"/v1/sessions/{parent['id']}/child_sessions")
    assert children.status_code == 200, children.text
    child_ids = {row["id"] for row in children.json()["data"]}
    assert child_ids == {child.id}, (
        f"parent's children must be exactly the untouched source, got {child_ids}"
    )


# ── Placement (SCC16): workspace, worktree, cross-project ─────────────

_PLACEMENT_HOST_ID = "2b8753b34a61b09af35a01136d40fadf"
_PLACEMENT_WORKSPACE = "/Users/alice/myrepo"
_PLACEMENT_PROJECT_ID = "bb11cc22dd33ee44ff55667788990011"


class _FakePlacementWebSocket:
    """Minimal host WebSocket stand-in (the registry only enqueues)."""

    async def send_text(self, data: str) -> None:
        """No-op: frames flow through the connection's outbound queue."""
        del data


@pytest.fixture()
async def placement_host(app: Any, db_uri: str) -> Any:
    """Register a fake host answering workspace stats and worktree creates."""
    import contextlib as _contextlib

    import pytest_asyncio  # noqa: F401 — fixture decorator precedent

    from omnigent.host.frames import (
        HostCreateWorktreeFrame,
        HostHelloFrame,
        HostRemoveWorktreeFrame,
        HostStatFrame,
        decode_host_frame,
    )
    from omnigent.server.auth import RESERVED_USER_LOCAL
    from omnigent.stores.host_store import HostStore

    HostStore(db_uri).upsert_on_connect(_PLACEMENT_HOST_ID, "placement-host", RESERVED_USER_LOCAL)
    conn = app.state.host_registry.register(
        host_id=_PLACEMENT_HOST_ID,
        ws=_FakePlacementWebSocket(),  # type: ignore[arg-type] — duck-typed
        hello=HostHelloFrame(
            version="0.1.0-test", frame_protocol_version=1, name="placement-host"
        ),
        owner=RESERVED_USER_LOCAL,
    )
    created: list[Any] = []

    async def _drain() -> None:
        while True:
            frame_text = await conn.outbound_queue.get()
            if frame_text is None:
                return
            frame = decode_host_frame(frame_text)
            if isinstance(frame, HostStatFrame):
                fut = conn.pending_stats.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(
                        {
                            "status": "ok",
                            "exists": True,
                            "type": "directory",
                            "canonical_path": frame.path,
                            "error": None,
                        }
                    )
            elif isinstance(frame, HostCreateWorktreeFrame):
                created.append(frame)
                fut = conn.pending_create_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    dirname = frame.branch_name.replace("/", "-")
                    fut.set_result(
                        {
                            "status": "ok",
                            "worktree_path": f"{frame.repo_path}-worktrees/{dirname}",
                            "branch": frame.branch_name,
                            "error": None,
                        }
                    )
            elif isinstance(frame, HostRemoveWorktreeFrame):
                fut = conn.pending_remove_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result({"status": "ok", "error": None})

    task = asyncio.create_task(_drain())
    try:
        yield created
    finally:
        conn.outbound_queue.put_nowait(None)
        with _contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        if not task.done():
            task.cancel()


async def test_child_with_workspace_gets_that_cwd(
    placement_host: list[Any], client: httpx.AsyncClient, db_uri: str
) -> None:
    """A child created with a host and workspace is stored at that cwd.

    The workspace is host-validated (the fake host stat answers the
    boundary round-trip) and the child keeps the parent's runner.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent = conv_store.create_conversation(
        host_id=_PLACEMENT_HOST_ID,
        workspace=_PLACEMENT_WORKSPACE,
        runner_id="runner_parent",
    )
    agent = await create_test_agent(client, name="workspace-child-agent")

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent.id,
            "host_id": _PLACEMENT_HOST_ID,
            "workspace": _PLACEMENT_WORKSPACE,
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["host_id"] == _PLACEMENT_HOST_ID
    assert body["workspace"] == _PLACEMENT_WORKSPACE
    assert body["runner_id"] == "runner_parent", "a same-host child keeps the parent runner"


async def test_child_with_worktree_gets_a_new_branch_worktree(
    placement_host: list[Any], client: httpx.AsyncClient, db_uri: str
) -> None:
    """A child create with a git block cuts its own worktree on the host."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent = conv_store.create_conversation(
        host_id=_PLACEMENT_HOST_ID,
        workspace=_PLACEMENT_WORKSPACE,
        runner_id="runner_parent",
    )
    agent = await create_test_agent(client, name="worktree-child-agent")

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent.id,
            "host_id": _PLACEMENT_HOST_ID,
            "workspace": _PLACEMENT_WORKSPACE,
            "git": {"branch_name": "child/fix-auth", "base_branch": "main"},
        },
    )

    assert resp.status_code == 201, resp.text
    assert len(placement_host) == 1, placement_host
    frame = placement_host[0]
    assert frame.repo_path == _PLACEMENT_WORKSPACE
    assert frame.branch_name == "child/fix-auth"
    assert frame.base_branch == "main"
    body = resp.json()
    assert body["git_branch"] == "child/fix-auth"
    assert body["workspace"] == f"{_PLACEMENT_WORKSPACE}-worktrees/child-fix-auth"


async def test_same_host_child_of_hostless_row_parent_keeps_the_parent_runner(
    placement_host: list[Any], client: httpx.AsyncClient, db_uri: str
) -> None:
    """Effective-host affinity: a hostless-row parent still shares its root's runner.

    The parent row carries no host_id (a mirrored/native child row) but its
    root is host-bound. A placed child that names that same host must keep
    the inherited runner instead of being unbound for a second launch.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    root = conv_store.create_conversation(
        host_id=_PLACEMENT_HOST_ID,
        workspace=_PLACEMENT_WORKSPACE,
        runner_id="runner_root",
    )
    parent = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=root.id,
        runner_id="runner_root",
    )
    agent = await create_test_agent(client, name="affinity-child-agent")

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent.id,
            "host_id": _PLACEMENT_HOST_ID,
            "workspace": _PLACEMENT_WORKSPACE,
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["host_id"] == _PLACEMENT_HOST_ID
    assert body["runner_id"] == "runner_root", (
        "a same-host placed child of a hostless-row parent must keep the parent runner"
    )


async def test_cross_project_child_is_readable_and_sendable_by_its_mother(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child joined to another project stays reachable from its mother.

    Project membership does not gate a parent's access to its child: the
    mother can read the child's snapshot and post a message into it. The
    child is seeded into a second project directly (create-time placement
    is covered by the project-create suites).
    """
    from omnigent.server.routes.sessions import routes_events as events_module
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

    parent = await _create_parent_session(client, agent_name="cross-project-agent")
    SqlAlchemyProjectStore(db_uri).create(_PLACEMENT_PROJECT_ID, "Other project", None)
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="worker:other-project",
        parent_conversation_id=parent["id"],
        agent_id=parent["agent_id"],
        project_id=_PLACEMENT_PROJECT_ID,
    )

    read = await client.get(f"/v1/sessions/{child.id}")
    assert read.status_code == 200, read.text
    assert read.json()["parent_session_id"] == parent["id"]
    assert read.json()["project_id"] == _PLACEMENT_PROJECT_ID

    forwarded: list[tuple[str, dict[str, Any]]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        forwarded.append((request.url.path, json.loads(request.content)))
        return httpx.Response(202, json={"queued": True})

    runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_capture), base_url="http://runner.test"
    )

    async def _get_runner_client(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    monkeypatch.setattr(events_module, "_get_runner_client", _get_runner_client)
    try:
        send = await client.post(
            f"/v1/sessions/{child.id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "status?"}],
                },
            },
        )
    finally:
        await runner.aclose()

    assert send.status_code == 202, send.text
    assert len(forwarded) == 1
    path, body = forwarded[0]
    assert path == f"/v1/sessions/{child.id}/events"
    assert body["type"] == "message"
    assert body["content"] == [{"type": "input_text", "text": "status?"}]


# ── Agents rail: zone filter + effective placement ────────


async def test_child_sessions_active_zone_pages_exclude_archived(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``zone=active`` returns every non-archived child, newest-created first.

    The rail's active zone pages through all active children, so the
    filter must exclude archived rows on every page and keep the
    created-at-desc order stable across the cursor.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    :param monkeypatch: Pytest patcher, used to pin the store clock so
        created-at ordering is deterministic.
    """
    parent = await _create_parent_session(client, "zone-active-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    base = 1_700_000_000
    children: list[Conversation] = []
    for index in range(25):
        monkeypatch.setattr(sqlalchemy_store_module, "now_epoch", lambda index=index: base + index)
        children.append(
            _seed_child(
                conv_store=conv_store,
                parent_id=parent["id"],
                title=f"researcher:c{index}",
                agent_id=parent["agent_id"],
            )
        )
    for offset, child in enumerate(children[:8]):
        monkeypatch.setattr(
            sqlalchemy_store_module, "now_epoch", lambda offset=offset: base + 100 + offset
        )
        conv_store.update_conversation(child.id, archived=True)

    expected = [child.id for child in reversed(children[8:])]
    first = (
        await client.get(
            f"/v1/sessions/{parent['id']}/child_sessions",
            params={"zone": "active", "limit": 10},
        )
    ).json()
    assert first["has_more"] is True
    assert [row["id"] for row in first["data"]] == expected[:10]
    assert all(row["archived"] is False for row in first["data"])

    second = (
        await client.get(
            f"/v1/sessions/{parent['id']}/child_sessions",
            params={"zone": "active", "limit": 10, "after": first["last_id"]},
        )
    ).json()
    assert second["has_more"] is False
    assert [row["id"] for row in second["data"]] == expected[10:]


async def test_child_sessions_past_zone_orders_by_archived_at(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``zone=past`` returns only archived children, newest-archived first.

    Each row carries its ``archived_at`` so the rail can label the row,
    and cursor paging follows the archived-at desc order.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    :param monkeypatch: Pytest patcher, used to pin the store clock so
        archived-at ordering is deterministic.
    """
    parent = await _create_parent_session(client, "zone-past-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    base = 1_700_000_000
    children: list[Conversation] = []
    for index in range(25):
        monkeypatch.setattr(sqlalchemy_store_module, "now_epoch", lambda index=index: base + index)
        children.append(
            _seed_child(
                conv_store=conv_store,
                parent_id=parent["id"],
                title=f"researcher:p{index}",
                agent_id=parent["agent_id"],
            )
        )
    archived = children[:8]
    for offset, child in enumerate(archived):
        monkeypatch.setattr(
            sqlalchemy_store_module, "now_epoch", lambda offset=offset: base + 100 + offset
        )
        conv_store.update_conversation(child.id, archived=True)

    expected = [child.id for child in reversed(archived)]
    first = (
        await client.get(
            f"/v1/sessions/{parent['id']}/child_sessions",
            params={"zone": "past", "limit": 5},
        )
    ).json()
    assert first["has_more"] is True
    assert [row["id"] for row in first["data"]] == expected[:5]
    assert all(row["archived"] is True for row in first["data"])

    second = (
        await client.get(
            f"/v1/sessions/{parent['id']}/child_sessions",
            params={"zone": "past", "limit": 5, "after": first["last_id"]},
        )
    ).json()
    assert second["has_more"] is False
    assert [row["id"] for row in second["data"]] == expected[5:]
    archived_times = [row["archived_at"] for row in [*first["data"], *second["data"]]]
    assert all(value is not None for value in archived_times)
    assert archived_times == sorted(archived_times, reverse=True)


async def test_child_sessions_zone_rejects_include_archived_combo(
    client: httpx.AsyncClient,
) -> None:
    """``zone`` and ``include_archived=true`` together are rejected.

    :param client: The test HTTP client.
    """
    parent = await _create_parent_session(client, "zone-bad-combo")
    resp = await client.get(
        f"/v1/sessions/{parent['id']}/child_sessions",
        params={"zone": "past", "include_archived": "true"},
    )
    assert resp.status_code == 400
    assert "zone" in resp.text


async def test_child_sessions_inherits_placement_from_ancestor(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A hostless child and grandchild report the nearest placed ancestor.

    Host and directory resolve independently up the chain; the branch
    travels with the row the directory came from.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    agent = await create_test_agent(client, name="placement-inherit-agent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    root = conv_store.create_conversation(
        kind="default",
        title="placed-root",
        agent_id=agent["id"],
        host_id="a1b2c3d4e5f60718293a4b5c6d7e8f90",
        workspace="/srv/parent",
        worktree="/srv/parent-wt",
        git_branch="feature/parent",
    )
    child = _seed_child(
        conv_store=conv_store,
        parent_id=root.id,
        title="researcher:child",
        agent_id=agent["id"],
    )
    grandchild = _seed_child(
        conv_store=conv_store,
        parent_id=child.id,
        title="researcher:grandchild",
        agent_id=agent["id"],
    )

    expected = ("a1b2c3d4e5f60718293a4b5c6d7e8f90", "/srv/parent-wt", "feature/parent")
    child_row = await _child_row(client, root.id, child.id)
    assert (child_row["host_id"], child_row["cwd"], child_row["git_branch"]) == expected
    grandchild_row = await _child_row(client, child.id, grandchild.id)
    assert (
        grandchild_row["host_id"],
        grandchild_row["cwd"],
        grandchild_row["git_branch"],
    ) == expected


async def test_child_sessions_own_placement_wins_with_null_branch(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A child's own host / workspace wins, and its null branch stays null.

    The inherited branch must not leak onto a directly placed child that
    records no branch of its own.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    agent = await create_test_agent(client, name="placement-own-agent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    root = conv_store.create_conversation(
        kind="default",
        title="branch-parent",
        agent_id=agent["id"],
        host_id="51dc949aba31e24ca8f047d6fba31a0d",
        workspace="/srv/root",
        git_branch="parent-branch",
    )
    child = _seed_child(
        conv_store=conv_store,
        parent_id=root.id,
        title="researcher:own",
        agent_id=agent["id"],
        host_id="9b2ec6de30f5e014c7056afe505510c3",
        workspace="/srv/child",
    )

    row = await _child_row(client, root.id, child.id)
    assert row["host_id"] == "9b2ec6de30f5e014c7056afe505510c3"
    assert row["cwd"] == "/srv/child"
    assert row["git_branch"] is None


async def test_child_sessions_no_placement_anywhere(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A hostless tree reports null placement rather than inventing values.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    parent = await _create_parent_session(client, "placement-none-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="researcher:nowhere",
        agent_id=parent["agent_id"],
    )

    row = await _child_row(client, parent["id"], child.id)
    assert row["host_id"] is None
    assert row["cwd"] is None
    assert row["git_branch"] is None


async def test_child_sessions_agent_name_and_harness(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A child bound to a codex agent reports its name and harness.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    parent = await _create_parent_session(client, "identity-parent")
    codex_agent = await create_test_agent(
        client,
        name="codex-rail-agent",
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
    )
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="researcher:codex",
        agent_id=codex_agent["id"],
    )

    row = await _child_row(client, parent["id"], child.id)
    assert row["agent_name"] == "codex-rail-agent"
    assert row["harness"] == "codex-native"
    assert row["sub_agent_name"] is None


async def test_child_sessions_harness_resolved_per_conversation(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Two children of one agent row report their own harness.

    ``harness_override`` lives on the conversation, so the resolver must
    run per conversation instead of caching by ``agent_id``.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    parent = await _create_parent_session(client, "mixed-harness-parent")
    agent = await create_test_agent(client, name="mixed-harness-agent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    base = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="researcher:base",
        agent_id=agent["id"],
    )
    overridden = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="researcher:override",
        agent_id=agent["id"],
        harness_override="codex-native",
    )

    rows = (await client.get(f"/v1/sessions/{parent['id']}/child_sessions")).json()["data"]
    by_id = {row["id"]: row for row in rows}
    assert by_id[base.id]["harness"] == "claude-sdk"
    assert by_id[overridden.id]["harness"] == "codex-native"
    assert by_id[base.id]["agent_name"] == "mixed-harness-agent"
    assert by_id[overridden.id]["agent_name"] == "mixed-harness-agent"


async def test_child_sessions_shared_agent_row_read_once_per_list(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twenty children on one agent read its row once, not per child.

    The harness still resolves per conversation (``harness_override`` lives
    on the child row), so a per-``agent_id`` harness cache would be wrong;
    only the agent row fetch is shared across the list call.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    :param monkeypatch: Pytest patcher for the agent-store read counter.
    """
    from omnigent.runtime._globals import _agent_store

    assert _agent_store is not None
    parent = await _create_parent_session(client, "memo-parent")
    agent = await create_test_agent(client, name="memo-agent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    for index in range(20):
        _seed_child(
            conv_store=conv_store,
            parent_id=parent["id"],
            title=f"researcher:memo-{index}",
            agent_id=agent["id"],
        )

    reads: list[str] = []
    original_get = _agent_store.get

    def counting_get(agent_id: str, *args: Any, **kwargs: Any) -> Any:
        reads.append(agent_id)
        return original_get(agent_id, *args, **kwargs)

    monkeypatch.setattr(_agent_store, "get", counting_get)
    resp = await client.get(f"/v1/sessions/{parent['id']}/child_sessions")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 20
    assert reads.count(agent["id"]) == 1


async def test_child_status_fan_out_keeps_placement(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A status fan-out event carries the same effective placement as the list.

    The rail patches cached child rows from these events, so an event
    that omits placement would blank the host / cwd group labels.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    from omnigent.runtime import session_stream
    from tests.server.helpers import start_session_stream_collector

    agent = await create_test_agent(client, name="fanout-placement-agent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    root = conv_store.create_conversation(
        kind="default",
        title="fanout-root",
        agent_id=agent["id"],
        host_id="a65b7d8e4613a95946c9134383308ac7",
        workspace="/srv/fan",
        git_branch="fan-branch",
    )
    child = _seed_child(
        conv_store=conv_store,
        parent_id=root.id,
        title="researcher:fanout",
        agent_id=agent["id"],
    )
    collector = await start_session_stream_collector(root.id)
    try:
        sessions_module._publish_status(child.id, "running")
        event = await _next_child_update(collector)
        payload = event["child"]
        assert payload["host_id"] == "a65b7d8e4613a95946c9134383308ac7"
        assert payload["cwd"] == "/srv/fan"
        assert payload["git_branch"] == "fan-branch"
        assert payload["agent_name"] == "fanout-placement-agent"
    finally:
        await collector.stop()
        sessions_module._session_status_cache.pop(child.id, None)
        session_stream.close(root.id)


async def test_child_archive_transition_publishes_to_parent_stream(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archive and unarchive each publish the child summary to the parent.

    An archived child emits no further status edges, so the archive
    transition itself must move the rail row.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    :param monkeypatch: Pytest patcher, used to zero the archive-stop undo
        grace so the deferred teardown runs immediately.
    """
    from omnigent.runtime import session_stream
    from tests.server.helpers import start_session_stream_collector

    parent = await _create_parent_session(client, "archive-publish-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=parent["id"],
        title="researcher:archive",
        agent_id=parent["agent_id"],
    )
    monkeypatch.setattr(sessions_module, "_ARCHIVE_STOP_UNDO_GRACE_S", 0.0)
    collector = await start_session_stream_collector(parent["id"])
    try:
        archived_resp = await client.patch(f"/v1/sessions/{child.id}", json={"archived": True})
        assert archived_resp.status_code == 200, archived_resp.text
        archived_event = await _next_child_update(collector)
        assert archived_event["child"]["archived"] is True
        assert archived_event["child"]["archived_at"] is not None

        unarchived_resp = await client.patch(f"/v1/sessions/{child.id}", json={"archived": False})
        assert unarchived_resp.status_code == 200, unarchived_resp.text
        unarchived_event = await _next_child_update(collector)
        assert unarchived_event["child"]["archived"] is False
        assert unarchived_event["child"]["archived_at"] is None
    finally:
        await collector.stop()
        session_stream.close(parent["id"])


async def test_child_snapshot_reports_effective_placement_from_root(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A deeply nested child snapshot carries the root's effective placement.

    The child page header reads the snapshot directly, so it must not
    depend on the rail's list cache being loaded.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    agent = await create_test_agent(client, name="header-placement-agent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    root = conv_store.create_conversation(
        kind="default",
        title="header-root",
        agent_id=agent["id"],
        host_id="3f866cafac81246fb60ae6ceb1a738da",
        workspace="/srv/header",
        git_branch="header-branch",
    )
    child = _seed_child(
        conv_store=conv_store,
        parent_id=root.id,
        title="researcher:header-child",
        agent_id=agent["id"],
    )
    grandchild = _seed_child(
        conv_store=conv_store,
        parent_id=child.id,
        title="researcher:header-grandchild",
        agent_id=agent["id"],
    )

    snapshot = (await client.get(f"/v1/sessions/{grandchild.id}")).json()
    assert snapshot["host_id"] is None
    assert snapshot["effective_host_id"] == "3f866cafac81246fb60ae6ceb1a738da"
    assert snapshot["effective_cwd"] == "/srv/header"
    assert snapshot["effective_git_branch"] == "header-branch"


async def test_child_sessions_zones_at_scale(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D13 load: 20 active children and 200 archived children page cleanly.

    The active zone loads every non-archived child through the cursor
    loop; the past zone pages 20 at a time to 200, newest-archived
    first.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    :param monkeypatch: Pytest patcher, used to advance the store clock so
        creation and archive ordering are deterministic at this scale.
    """
    parent = await _create_parent_session(client, "scale-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    clock = itertools.count(1_700_100_000)
    monkeypatch.setattr(sqlalchemy_store_module, "now_epoch", lambda: next(clock))

    active_ids: list[str] = []
    for index in range(20):
        active_ids.append(
            _seed_child(
                conv_store=conv_store,
                parent_id=parent["id"],
                title=f"researcher:active{index}",
                agent_id=parent["agent_id"],
            ).id
        )
    archived_ids: list[str] = []
    for index in range(200):
        child = _seed_child(
            conv_store=conv_store,
            parent_id=parent["id"],
            title=f"researcher:past{index}",
            agent_id=parent["agent_id"],
        )
        conv_store.update_conversation(child.id, archived=True)
        archived_ids.append(child.id)

    active_rows: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        params: dict[str, Any] = {"zone": "active", "limit": 20}
        if after is not None:
            params["after"] = after
        page = (
            await client.get(f"/v1/sessions/{parent['id']}/child_sessions", params=params)
        ).json()
        active_rows.extend(page["data"])
        if not page["has_more"]:
            break
        after = page["last_id"]
    assert [row["id"] for row in active_rows] == list(reversed(active_ids))
    assert all(row["archived"] is False for row in active_rows)

    past_rows: list[dict[str, Any]] = []
    page_sizes: list[int] = []
    after = None
    while True:
        params = {"zone": "past", "limit": 20}
        if after is not None:
            params["after"] = after
        page = (
            await client.get(f"/v1/sessions/{parent['id']}/child_sessions", params=params)
        ).json()
        page_sizes.append(len(page["data"]))
        past_rows.extend(page["data"])
        if not page["has_more"]:
            break
        after = page["last_id"]
    assert page_sizes == [20] * 10
    assert [row["id"] for row in past_rows] == list(reversed(archived_ids))
    archived_times = [row["archived_at"] for row in past_rows]
    assert all(value is not None for value in archived_times)
    assert archived_times == sorted(archived_times, reverse=True)
