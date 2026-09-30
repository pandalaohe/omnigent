"""Native sub-agent completions must reach the parent inbox.

A native CLI sub-agent's completion is the forwarder's ``external_session_status:
idle`` (or ``failed``) event POSTed to the child's ``/events``. The runner turns
that into a ``sub_agent`` payload in the parent's async inbox (``sys_read_inbox``)
so the orchestrator wakes instead of busy-polling ``sys_session_get_history``.

Delivery is dropped whenever the runner's in-memory work entry for the child is
missing — a reconnect / restart wiped ``_subagent_work_by_child`` mid-turn, or a
``sys_session_create`` child never registered one (the server records a
``parent_session_id`` but no ``sub_agent_name``). The old code then returned
HTTP 204 and lost the completion. The fix rebuilds the entry from the server
snapshot before delivering, and returns 503 (so the forwarder retries) when
delivery still can't be confirmed on this runner.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent._wrapper_labels import WRAPPER_LABEL_KEY
from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)

# Reuse the proven runner-turn stubs from the sessions-native suite.
from tests.runner.helpers import NullServerClient

PARENT_SESSION_ID = "conv_parent_orchestrator"
CHILD_SESSION_ID = "conv_child_reviewer"


class _SnapshotServerClient(NullServerClient):
    """Server client whose ``GET /v1/sessions/{child}`` carries the sub-agent snapshot.

    Mirrors ``SessionResponse`` (server routes/sessions.py): the authoritative
    source the runner uses to rebuild a lost sub-agent work entry. The body is
    configurable so a test can model a declared sub-agent (``sub_agent_name``
    set), a ``sys_session_create`` child (``sub_agent_name`` null but
    ``parent_session_id`` set + ``agent_name``), or a top-level session (no
    parent). All other endpoints fall through to the empty-200 base.
    """

    def __init__(
        self,
        child_body: dict[str, Any],
        parent_body: dict[str, Any] | None = None,
        items: list[dict[str, Any]] | None = None,
        collab_enabled: bool | None = None,
        host_names: dict[str, str] | None = None,
    ) -> None:
        """Configure the bodies returned for the child and parent session GETs."""
        self._child_body = child_body
        self._parent_body = parent_body
        self.items: list[dict[str, Any]] = [] if items is None else items
        self.collab_enabled = collab_enabled
        self.host_names = host_names or {}
        self.posts: list[tuple[str, dict[str, Any]]] = []

    class _Resp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if url.rstrip("/").endswith(CHILD_SESSION_ID):
            return self._Resp(self._child_body)
        if self._parent_body is not None and url.rstrip("/").endswith(PARENT_SESSION_ID):
            return self._Resp(self._parent_body)
        if url.rstrip("/").endswith("/items"):
            return self._Resp({"data": self.items, "has_more": False})
        if url.rstrip("/").endswith("/collab-settings") and self.collab_enabled is not None:
            return self._Resp({"enabled": self.collab_enabled})
        if "/v1/hosts/" in url:
            host_id = url.rstrip("/").rsplit("/", 1)[-1]
            name = self.host_names.get(host_id)
            if name is not None:
                return self._Resp({"host_id": host_id, "name": name})
        return self._Response()

    async def post(self, url: str, **kwargs: Any) -> Any:
        """Record the request, then answer like the empty-200 base client."""
        self.posts.append((url, kwargs))
        return self._Response()


DISPATCH_ID = "subagent_dispatch0001"
_CHILD_RESULT_ITEM: dict[str, Any] = {
    "type": "message",
    "role": "assistant",
    "content": [{"type": "output_text", "text": "review complete: LGTM"}],
}


def _child_summary(**overrides: Any) -> dict[str, Any]:
    """
    Build a terminal child-session summary as the sessions API returns it.

    :param overrides: Field overrides, e.g. ``current_task_status="failed"``.
    :returns: Child summary carrying an undrained dispatch id by default.
    """
    summary: dict[str, Any] = {
        "id": CHILD_SESSION_ID,
        "tool": "reviewer",
        "session_name": "review",
        "current_task_status": "completed",
        "labels": {runner_app.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID},
    }
    summary.update(overrides)
    return summary


class _RecoveryServerClient(NullServerClient):
    """Serve the durable child records that restart recovery reads."""

    def __init__(
        self,
        children: list[dict[str, Any]],
        *,
        child_items: list[dict[str, Any]] | None = None,
        failed_item_sessions: set[str] | None = None,
    ) -> None:
        """
        Configure the child list and transcript returned by the fake server.

        :param children: Parent's child-session summaries.
        :param child_items: Child transcript, newest first.
        :param failed_item_sessions: Session ids whose item read returns 503.
        """
        self.children = children
        self.child_items = [_CHILD_RESULT_ITEM] if child_items is None else child_items
        self.failed_item_sessions = failed_item_sessions or set()
        self.requests: list[tuple[str, dict[str, Any]]] = []

    class _Resp:
        """Minimal HTTP response carrying a JSON payload."""

        def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
            """
            Store one JSON response payload.

            :param payload: JSON object returned by :meth:`json`.
            :param status_code: HTTP status exposed to production code.
            """
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, Any]:
            """Return the configured JSON payload."""
            return self._payload

    async def get(self, url: str, **kwargs: Any) -> Any:
        """
        Return child summaries and transcripts for recovery reads.

        :param url: Requested sessions API path.
        :param kwargs: HTTP request options; ``params`` are recorded.
        :returns: Minimal response for the requested resource.
        """
        params = dict(kwargs.get("params") or {})
        self.requests.append((url, params))
        if url.endswith(f"/{PARENT_SESSION_ID}/child_sessions"):
            return self._Resp({"data": self.children, "has_more": False})
        if url.endswith("/items"):
            session_id = url.rstrip("/").split("/")[-2]
            if session_id in self.failed_item_sessions:
                return self._Resp({}, status_code=503)
            return self._Resp({"data": self.child_items, "has_more": False})
        return self._Resp({"data": [], "has_more": False})


def _child_snapshot(
    *,
    sub_agent_name: str | None,
    parent_session_id: str | None,
    agent_name: str | None = "cursor-native-ui",
    labels: dict[str, str] | None = None,
    host_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Build a child ``SessionResponse``-shaped body."""
    return {
        "id": CHILD_SESSION_ID,
        "agent_id": "ag_reviewer",
        "agent_name": agent_name,
        "sub_agent_name": sub_agent_name,
        "parent_session_id": parent_session_id,
        "created_at": 0,
        "workspace": None,
        "labels": labels or {},
        **({"host_id": host_id} if host_id is not None else {}),
        **({"status": status} if status is not None else {}),
    }


def _parent_snapshot(*, parent_session_id: str | None) -> dict[str, Any]:
    """Build the parent's ``SessionResponse``-shaped body."""
    return {
        "id": PARENT_SESSION_ID,
        "agent_id": "ag_orchestrator",
        "agent_name": "claude-native-ui",
        "sub_agent_name": "explorer" if parent_session_id else None,
        "parent_session_id": parent_session_id,
        "created_at": 0,
        "workspace": None,
    }


async def _post_native_status(
    *,
    child_body: dict[str, Any],
    seed_parent_inbox: bool,
    register_work: bool,
    status: str = "idle",
    output: str = "review complete: LGTM",
    parent_body: dict[str, Any] | None = None,
) -> tuple[int, list[dict[str, Any]], _SnapshotServerClient]:
    """POST an ``external_session_status`` edge and return (http, inbox items, client).

    Models the forwarder reporting a finished native sub-agent turn.
    ``register_work`` seeds the in-memory work entry (the healthy case); leaving
    it ``False`` models a reconnect-wiped map or a ``sys_session_create`` child
    the dispatch never registered. ``seed_parent_inbox`` controls whether the
    parent's inbox queue is present on this runner. ``parent_body`` is the
    parent's session snapshot; when omitted the parent reads as top-level. The
    returned client records every POST it served, so a test can assert that no
    wake notice was posted.
    """
    if seed_parent_inbox:
        runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    if register_work:
        runner_app.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="reviewer",
            title="review",
        )

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="reviewer",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    server_client = _SnapshotServerClient(child_body, parent_body)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={
                "type": "external_session_status",
                "data": {"status": status, "output": output},
            },
        )

    inbox = runner_app._session_inboxes_ref.get(PARENT_SESSION_ID)
    items: list[dict[str, Any]] = []
    if inbox is not None:
        while not inbox.empty():
            items.append(inbox.get_nowait())
    return resp.status_code, items, server_client


async def _post_native_idle(
    *,
    child_body: dict[str, Any],
    seed_parent_inbox: bool,
    register_work: bool,
    output: str = "review complete: LGTM",
    parent_body: dict[str, Any] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """POST a native ``external_session_status: idle`` and return (http, inbox items)."""
    http, items, _client = await _post_native_status(
        child_body=child_body,
        seed_parent_inbox=seed_parent_inbox,
        register_work=register_work,
        output=output,
        parent_body=parent_body,
    )
    return http, items


@pytest.mark.asyncio
async def test_native_completion_recovers_reconnect_wiped_work_entry(
    _clean_subagent_registry: None,
) -> None:
    """A declared native sub-agent still delivers after its work entry was lost.

    With no in-memory work entry (a reconnect wiped it mid-turn), the idle edge
    dropped silently on the old code. The fix rebuilds the entry from the
    snapshot's ``parent_session_id`` + ``sub_agent_name`` and delivers.
    """
    http, items = await _post_native_idle(
        child_body=_child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        seed_parent_inbox=True,
        register_work=False,
    )

    assert items, (
        "native sub-agent reported idle but nothing was delivered to the parent "
        "inbox: the work entry was missing (reconnect-wiped) and the idle edge "
        f"was silently 204-acked. (http={http})"
    )
    payload = items[0]
    assert payload["type"] == "sub_agent"
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["status"] == "completed"
    assert payload["output"] == "review complete: LGTM"


@pytest.mark.asyncio
async def test_sys_session_create_child_without_sub_agent_name_delivers(
    _clean_subagent_registry: None,
) -> None:
    """A ``sys_session_create`` child (no ``sub_agent_name``) still wakes the parent.

    The child has ``agent_name: cursor-native-ui`` but ``sub_agent_name: null``,
    and the dispatch never registered a work entry. The fix recovers the parent
    link from the snapshot (keying on ``parent_session_id``) and labels the work
    with the agent name.
    """
    http, items = await _post_native_idle(
        child_body=_child_snapshot(
            sub_agent_name=None,
            parent_session_id=PARENT_SESSION_ID,
            agent_name="cursor-native-ui",
        ),
        seed_parent_inbox=True,
        register_work=False,
    )

    assert items, (
        f"sys_session_create child reported idle but the parent inbox stayed empty. (http={http})"
    )
    assert items[0]["status"] == "completed"
    assert items[0]["agent"] == "cursor-native-ui"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_claude_agent_tool_mirror_terminal_status_is_not_redelivered(
    _clean_subagent_registry: None,
    status: str,
) -> None:
    """A mirrored Claude Agent-tool sub-agent must not be delivered twice.

    The forwarder mirrors each Claude Code Agent-tool sub-agent and derives the
    parent edge from Claude's own hand-back of the result, so the parent already
    has it natively. The terminal status still arrives here for display
    bookkeeping; enqueueing an inbox entry and posting a wake notice on top
    would deliver the result a second time.
    """
    http, items, server_client = await _post_native_status(
        status=status,
        child_body=_child_snapshot(
            sub_agent_name=None,
            parent_session_id=PARENT_SESSION_ID,
            labels={WRAPPER_LABEL_KEY: CLAUDE_NATIVE_CODING_AGENT.subagent_wrapper_label},
        ),
        seed_parent_inbox=True,
        register_work=False,
    )
    # Let a wake task, if one was scheduled, reach its POST before asserting.
    await asyncio.sleep(0.05)

    assert http == 204
    assert items == [], f"a mirrored Agent-tool sub-agent was delivered twice (status={status})"
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None
    assert server_client.posts == [], (
        f"a mirrored Claude Agent-tool sub-agent posted a wake notice (status={status}); "
        "Claude Code already handed the result back to the parent"
    )


@pytest.mark.asyncio
async def test_healthy_registered_work_entry_still_delivers(
    _clean_subagent_registry: None,
) -> None:
    """Control: the normal path (work entry present) keeps delivering.

    Guards against the fix regressing the common case where dispatch already
    registered the work entry on this runner.
    """
    http, items = await _post_native_idle(
        child_body=_child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        seed_parent_inbox=True,
        register_work=True,
    )

    assert http == 204
    assert items and items[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_undeliverable_native_completion_returns_503_not_silent_204(
    _clean_subagent_registry: None,
) -> None:
    """A recoverable sub-agent whose parent inbox is elsewhere must 503, not 204.

    When the parent inbox is not on this runner (the parent lives on a different
    runner, or the runner restarted and lost it), delivery cannot be confirmed.
    The handler must return 503 so the forwarder retries and server-side recovery
    re-routes to the parent's runner — instead of a silent 204 that drops it.
    """
    http, items = await _post_native_idle(
        child_body=_child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        seed_parent_inbox=False,
        register_work=False,
    )

    assert http == 503, (
        "an undeliverable native sub-agent completion was acked with "
        f"http={http}; expected 503 so the forwarder retries. Items={items!r}"
    )


@pytest.mark.asyncio
async def test_nested_subagent_parent_without_inbox_is_acked(
    _clean_subagent_registry: None,
) -> None:
    """A completion whose parent is itself a sub-agent is ACKed without an inbox.

    Claude Code sub-agents can fan out further, and the forwarder mirrors the
    grandchildren under the mid-level child. That parent is never initialized
    on the runner, so its inbox never exists and a 503 only made the forwarder
    retry every 30 s for the life of the runner; the result reaches the parent
    natively inside the Claude process. The entry stays terminal and
    undelivered so a parent that does run here later can still receive it.
    """
    http, items = await _post_native_idle(
        child_body=_child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        seed_parent_inbox=False,
        register_work=False,
        parent_body=_parent_snapshot(parent_session_id="conv_top_level"),
    )

    assert http == 204
    assert items == []
    entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None
    assert entry.status == "completed"
    assert entry.delivered is False


@pytest.mark.asyncio
async def test_retained_result_is_delivered_when_parent_inbox_is_created(
    _clean_subagent_registry: None,
) -> None:
    """A result acknowledged without a parent inbox is delivered once the inbox exists.

    After the nested-parent 204 the forwarder never resends, so the runner must
    hand the retained result over itself when it creates the parent's inbox
    (session init or a drain), the way the pending retry used to the moment
    the inbox appeared. Delivered exactly once.
    """
    child_body = _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="reviewer",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SnapshotServerClient(  # type: ignore[arg-type]
            child_body, _parent_snapshot(parent_session_id="conv_top_level")
        ),
    )
    async with _runner_client(app) as client:
        acked = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={"type": "external_session_status", "data": {"status": "idle", "output": "x"}},
        )
        assert acked.status_code == 204
        assert PARENT_SESSION_ID not in runner_app._session_inboxes_ref

        # The parent's inbox appears on this process; a second creation is a no-op.
        await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
        await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    assert inbox.qsize() == 1
    delivered = inbox.get_nowait()
    assert delivered["task_id"] == CHILD_SESSION_ID
    assert delivered["status"] == "completed"
    entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None
    assert entry.delivered is True


@pytest.mark.asyncio
async def test_replayed_idle_after_drain_does_not_redeliver(
    _clean_subagent_registry: None,
) -> None:
    """The recovery must not re-deliver a child already delivered and drained.

    Guards the snapshot-recovery arm against a duplicate: once a completion was
    delivered and the parent drained it, the runner keeps a delivered tombstone.
    A replayed idle whose snapshot *does* carry a ``parent_session_id`` (the
    production shape) must NOT rebuild the work entry and re-enqueue — it stays a
    benign already-delivered 204. (The existing suite's dedup test uses a stub
    snapshot with no parent, so it would not catch a recovery-induced re-deliver.)
    """
    child_body = _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID)
    # First completion delivers normally.
    http1, items1 = await _post_native_idle(
        child_body=child_body, seed_parent_inbox=True, register_work=True
    )
    assert http1 == 204
    assert len(items1) == 1  # drained by the helper

    # Mark the child delivered-and-drained, exactly as sys_read_inbox does.
    runner_app.unregister_subagent_work(CHILD_SESSION_ID, remember_drained_delivery=True)
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None

    # Replay the idle — snapshot carries a parent, so a naive recovery would
    # rebuild the entry and re-deliver. The tombstone guard must prevent that.
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="reviewer",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SnapshotServerClient(child_body),  # type: ignore[arg-type]
    )
    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    async with _runner_client(app) as client:
        replay = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={"type": "external_session_status", "data": {"status": "idle", "output": "x"}},
        )

    assert replay.status_code == 204
    assert inbox.qsize() == 0, "replayed idle re-delivered a duplicate to the parent inbox"


@pytest.mark.asyncio
async def test_top_level_session_idle_is_noop(
    _clean_subagent_registry: None,
) -> None:
    """A top-level session (no parent) idle edge stays a quiet 204 no-op.

    Ensures the recovery arm does not mis-classify a non-sub-agent sender as a
    sub-agent and start 503-ing or fabricating inbox deliveries.
    """
    http, items = await _post_native_idle(
        child_body=_child_snapshot(sub_agent_name=None, parent_session_id=None),
        seed_parent_inbox=True,
        register_work=False,
    )

    assert http == 204
    assert items == []


@pytest.mark.asyncio
async def test_runner_restart_recovers_undrained_terminal_child(
    _clean_subagent_registry: None,
) -> None:
    """A fresh runner rebuilds an undrained child result from the receipt gap.

    Nothing survives the process: no work entry, inbox item, or drained
    tombstone. The child carries a dispatch id but no delivered-id receipt,
    so initializing the parent must re-queue its result under that same id.
    """
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return the parent spec needed by the public initialization route.

        :param agent_id: Bound agent id supplied by initialization.
        :param session_id: Parent session id being initialized.
        :returns: Minimal parent agent specification.
        """
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="orchestrator")

    server_client = _RecoveryServerClient([_child_summary()])
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions",
            json={"session_id": PARENT_SESSION_ID, "agent_id": "ag_orchestrator"},
        )

    assert response.status_code == 201
    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    assert inbox.qsize() == 1
    payload = inbox.get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["status"] == "completed"
    assert payload["output"] == "review complete: LGTM"
    assert payload["work_id"] == DISPATCH_ID
    child_reads = [
        params
        for url, params in server_client.requests
        if url.endswith(f"/{CHILD_SESSION_ID}/items")
    ]
    assert child_reads == [{"limit": "100", "order": "desc"}]
    request_count = len(server_client.requests)
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    assert len(server_client.requests) == request_count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labels",
    [
        {
            runner_app.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID,
            runner_app.SUBAGENT_DELIVERED_ID_LABEL_KEY: DISPATCH_ID,
        },
        {},
    ],
)
async def test_runner_restart_skips_drained_and_unstamped_children(
    _clean_subagent_registry: None,
    labels: dict[str, str],
) -> None:
    """A matching receipt, or a child created before receipts, is not replayed.

    :param _clean_subagent_registry: Isolates module-level runner state.
    :param labels: Either a drained turn's labels or a legacy child's none.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient([_child_summary(labels=labels)])
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].empty()
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None
    assert not any(url.endswith("/items") for url, _ in server_client.requests)


@pytest.mark.asyncio
async def test_runner_restart_replays_continued_turn_with_stale_receipt(
    _clean_subagent_registry: None,
) -> None:
    """A receipt for an earlier turn cannot mask a continued child's new turn."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    labels = {
        runner_app.SUBAGENT_DISPATCH_ID_LABEL_KEY: "subagent_turn2",
        runner_app.SUBAGENT_DELIVERED_ID_LABEL_KEY: "subagent_turn1",
    }
    app = create_runner_app(
        server_client=_RecoveryServerClient([_child_summary(labels=labels)]),  # type: ignore[arg-type]
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["work_id"] == "subagent_turn2"
    assert payload["output"] == "review complete: LGTM"


@pytest.mark.asyncio
async def test_drain_before_session_init_still_recovers(
    _clean_subagent_registry: None,
) -> None:
    """A drain that runs before the session is initialized still recovers.

    After a reconnect the server can dispatch a pending message before it
    re-initializes the session on the replacement runner, so the parent has no
    inbox yet when ``sys_read_inbox`` runs. Recovery must create the inbox and
    queue the result rather than treat the missing inbox as nothing to do.
    """
    app = create_runner_app(
        server_client=_RecoveryServerClient([_child_summary()]),  # type: ignore[arg-type]
    )
    assert PARENT_SESSION_ID not in runner_app._session_inboxes_ref

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["output"] == "review complete: LGTM"


@pytest.mark.asyncio
async def test_runner_restart_recovers_text_less_final_turn_as_no_output(
    _clean_subagent_registry: None,
) -> None:
    """The newest assistant message wins even without text, as in live delivery.

    Walking past it to an older message would surface a previous turn's text
    as this turn's result.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    child_items = [
        {"type": "message", "role": "assistant", "content": []},
        _CHILD_RESULT_ITEM,
    ]
    app = create_runner_app(
        server_client=_RecoveryServerClient([_child_summary()], child_items=child_items),  # type: ignore[arg-type]
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["status"] == "completed"
    assert payload["output"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    [
        {"type": "message", "role": "user", "content": []},
        {"type": "function_call", "name": "shell", "call_id": "call_next"},
        {"type": "function_call_output", "call_id": "call_next", "output": "done"},
    ],
    ids=["user-message", "tool-call", "tool-result"],
)
async def test_runner_restart_does_not_reuse_assistant_text_before_latest_turn_boundary(
    _clean_subagent_registry: None,
    boundary: dict[str, Any],
) -> None:
    """A newer user/tool item keeps recovery from delivering an old answer."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    child_items = [
        {"type": "message", "role": "user", "content": [], "is_meta": True},
        boundary,
        _CHILD_RESULT_ITEM,
    ]
    app = create_runner_app(
        server_client=_RecoveryServerClient([_child_summary()], child_items=child_items),  # type: ignore[arg-type]
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["status"] == "completed"
    assert payload["output"] == "[System: sub-agent completed with no output]"


@pytest.mark.asyncio
async def test_runner_restart_recovers_failed_child_error(
    _clean_subagent_registry: None,
) -> None:
    """A failed child replays its durable error without reading its transcript."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient(
        [
            _child_summary(
                current_task_status="failed",
                last_task_error={"code": "required_terminal_exited", "message": "pane died"},
            )
        ]
    )
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["status"] == "failed"
    assert payload["output"] == "pane died"
    assert not any(url.endswith("/items") for url, _ in server_client.requests)


@pytest.mark.parametrize("terminal_status", ["stopped", "killed"])
async def test_runner_restart_preserves_structured_terminal_child_status(
    _clean_subagent_registry: None,
    terminal_status: str,
) -> None:
    """Restart recovery delivers stopped/killed without laundering to completed."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient(
        [_child_summary(current_task_status=terminal_status)],
        child_items=[],
    )
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["status"] == terminal_status
    assert terminal_status in payload["output"]


@pytest.mark.parametrize(
    ("terminal_status", "error", "expected_output"),
    [
        ("failed", {"code": "required_terminal_exited", "message": "pane died"}, "pane died"),
        (
            "stopped",
            None,
            "Sub-agent stopped before producing a reliable final result.",
        ),
    ],
)
async def test_runner_restart_recovers_a_pre_change_terminal_without_new_labels(
    _clean_subagent_registry: None,
    terminal_status: str,
    error: dict[str, str] | None,
    expected_output: str,
) -> None:
    """A terminal row without the newer dispatch pairing still recovers.

    The r3 reproduction: rows written before the server paired a durable
    terminal with its dispatch (and every terminal not written by that
    handler) carry only the dispatch id. Reconciliation is keyed on that id,
    so both a stored failure and a stoppage must come back after a restart.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient(
        [
            _child_summary(
                current_task_status=terminal_status,
                last_task_error=error,
                labels={runner_app.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID},
            )
        ],
        child_items=[],
    )
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["status"] == terminal_status
    assert payload["output"] == expected_output


@pytest.mark.asyncio
async def test_runner_restart_retries_after_child_history_read_failure(
    _clean_subagent_registry: None,
) -> None:
    """A failed transcript read leaves the scan unfinished so the drain retries it."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient(
        [_child_summary()], failed_item_sessions={CHILD_SESSION_ID}
    )
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].empty()
    assert PARENT_SESSION_ID not in runner_app._subagent_recovery_done

    server_client.failed_item_sessions.clear()
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert PARENT_SESSION_ID in runner_app._subagent_recovery_done


@pytest.mark.asyncio
async def test_concurrent_recovery_scans_deliver_exactly_once(
    _clean_subagent_registry: None,
) -> None:
    """Session initialization racing a ``sys_read_inbox`` drain queues one result."""

    class _YieldingRecoveryServerClient(_RecoveryServerClient):
        """Yield to the event loop on every read, exposing the interleaving."""

        async def get(self, url: str, **kwargs: Any) -> Any:
            await asyncio.sleep(0)
            return await super().get(url, **kwargs)

    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app = create_runner_app(
        server_client=_YieldingRecoveryServerClient([_child_summary()]),  # type: ignore[arg-type]
    )

    await asyncio.gather(
        app.state.recover_undrained_subagent_results(PARENT_SESSION_ID),
        app.state.recover_undrained_subagent_results(PARENT_SESSION_ID),
    )

    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].qsize() == 1


@pytest.mark.asyncio
async def test_routed_child_off_its_native_spec_still_delivers(
    _clean_subagent_registry: None,
) -> None:
    """A child routed onto an SDK harness delivers, though its spec says native.

    The Smart Routing shape: polly's ``claude_code`` worker declares
    ``claude-native``, but a routed child is forwarded ``harness_override`` and
    actually runs ``claude-sdk``. The runner read the harness off the cached
    SPEC, so the turn looked native and its completion was left to a native
    path that never runs — the parent waited forever while the pi sibling
    (whose spec harness is already non-native) was the only one to report.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    runner_app.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="claude_code",
        title="joke-claude",
    )
    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.output_text.delta", "delta": "knock knock"}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="claude_code",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={
                "type": "message",
                "agent_id": "ag_reviewer",
                "harness_override": "claude-sdk",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "tell me a joke"}],
                },
            },
        )
        assert resp.status_code in (200, 202), resp.text
        for _ in range(200):
            if not inbox.empty():
                break
            await asyncio.sleep(0.01)

    items: list[dict[str, Any]] = []
    while not inbox.empty():
        items.append(inbox.get_nowait())
    assert items, (
        "a routed child finished its turn but nothing reached the parent inbox: "
        "the runner read the harness off the spec (claude-native) instead of the "
        "forwarded harness_override (claude-sdk)"
    )
    assert items[0]["status"] == "completed"
    assert items[0]["conversation_id"] == CHILD_SESSION_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("harness_name", ["cursor-native", "claude-sdk"])
@pytest.mark.parametrize("error_code", [None, "runner_disconnected", "runner_failed_to_start"])
@pytest.mark.parametrize("previous_execution", ["new", "finished", "active", "messaged"])
async def test_recovered_child_continues_same_dispatch_before_delivering_result(
    _clean_subagent_registry: None,
    monkeypatch: pytest.MonkeyPatch,
    error_code: str | None,
    previous_execution: str,
    harness_name: str,
) -> None:
    """Parent initialization keeps the child pending; child init resumes its task once."""
    from unittest.mock import AsyncMock

    from omnigent.entities import Conversation
    from omnigent.runner.session_init_protocol import build_runner_session_init_payload
    from tests.runner.conftest import _sse

    monkeypatch.setattr(runner_app, "_launch_native_terminal", AsyncMock(return_value=True))
    monkeypatch.setattr(runner_app, "_resolve_native_spawn_env", AsyncMock(return_value={}))
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_recovered"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_recovered"}}),
        ]
    )
    started, release = asyncio.Event(), asyncio.Event()
    if previous_execution == "messaged":
        original = _ScriptedHarnessClient._StreamHandle.aiter_text

        async def gated_stream(handle: Any) -> Any:
            started.set()
            await release.wait()
            async for frame in original(handle):
                yield frame

        monkeypatch.setattr(_ScriptedHarnessClient._StreamHandle, "aiter_text", gated_stream)
    pm = _FakeProcessManager(harness)
    server = _RecoveryServerClient(
        [
            _child_summary(
                current_task_status="failed" if error_code else "in_progress",
                last_task_error={"code": error_code, "message": "runner lost"},
            )
        ]
    )

    async def resolve(agent_id: str, session_id: str | None = None) -> AgentSpec:
        if agent_id == "ag_reviewer":
            return AgentSpec(
                spec_version=1,
                name="worker",
                executor=ExecutorSpec(type="omnigent", config={"harness": harness_name}),
            )
        return AgentSpec(spec_version=1, name="orchestrator")

    resources = runner_app.SessionResourceRegistry()
    app = create_runner_app(
        resource_registry=resources,
        process_manager=pm,
        spec_resolver=resolve,
        server_client=server,  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        parent = await client.post(
            "/v1/sessions", json={"session_id": PARENT_SESSION_ID, "agent_id": "ag_orchestrator"}
        )
        assert parent.status_code == 201, parent.text
        inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
        entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
        assert entry is not None and entry.work_id == DISPATCH_ID
        assert entry.status not in {"failed", "completed", "cancelled"}
        assert inbox.empty(), "runner crash was delivered as a finished child result"
        child = Conversation(
            id=CHILD_SESSION_ID,
            agent_id="ag_reviewer",
            runner_id="replacement",
            root_conversation_id=PARENT_SESSION_ID,
            parent_conversation_id=PARENT_SESSION_ID,
            created_at=0,
            updated_at=0,
        )
        payload = build_runner_session_init_payload(
            child,
            server_version="test",
            resume_interrupted_turn=True,
        )
        if previous_execution in {"finished", "active"}:
            # Runner A retains its old turn epoch while this child ran on B.
            app.state.begin_turn_slot(CHILD_SESSION_ID)
            app.state.active_turns.pop(CHILD_SESSION_ID)
        if previous_execution == "active":
            resources.note_external_session_status(CHILD_SESSION_ID, "running")
        if previous_execution == "messaged":
            message = await client.post(
                f"/v1/sessions/{CHILD_SESSION_ID}/events",
                json={
                    "type": "message",
                    "agent_id": "ag_reviewer",
                    "content": [{"type": "input_text", "text": "new user instruction"}],
                },
            )
            assert message.status_code == 202, message.text
            await asyncio.wait_for(started.wait(), timeout=5)
        for _ in range(2):
            result = await client.post("/v1/sessions", json=payload)
            assert result.status_code == 201
        if previous_execution == "active":
            assert not harness.posted_bodies, "a surviving native turn must not get another prompt"
        elif previous_execution == "messaged":
            assert len(harness.posted_bodies) == 1
            content = str(harness.posted_bodies[0]["content"])
            assert "new user instruction" in content
            assert "Continue the existing task" not in content
            release.set()
        else:
            for _ in range(100):
                if harness.posted_bodies:
                    break
                await asyncio.sleep(0.01)
            assert len(harness.posted_bodies) == 1, (
                "interrupted child must receive one continuation turn"
            )
            assert "Continue the existing task" in str(harness.posted_bodies[0]["content"])
        if harness_name == "cursor-native" or previous_execution == "active":
            # A surviving turn completes through its existing status forwarder.
            await client.post(
                f"/v1/sessions/{CHILD_SESSION_ID}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "done"},
                },
            )
        else:
            for _ in range(100):
                if not inbox.empty():
                    break
                await asyncio.sleep(0.01)
            assert "review complete: LGTM" in str(harness.posted_bodies[0]["content"])
        result = inbox.get_nowait()
        assert result["conversation_id"] == CHILD_SESSION_ID
        assert result["work_id"] == DISPATCH_ID
        assert result["status"] == "completed"
        turn = app.state.active_turns.get(CHILD_SESSION_ID)
        if turn is not None:
            await turn
        await client.post("/v1/sessions", json=payload)
        assert len(harness.posted_bodies) == (0 if previous_execution == "active" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", [None, "runner_disconnected", "runner_failed_to_start"])
async def test_recovered_pending_child_outlives_launch_timeout_and_delivers_original_result(
    _clean_subagent_registry: None, error_code: str | None
) -> None:
    """Recovery must await the original dispatch, including work on another live runner."""
    from unittest.mock import AsyncMock

    from omnigent.runner.tool_dispatch import _cleanup_drained_subagent_work, _drain_inbox

    child = _child_summary(
        runner_id="other-live-runner",
        current_task_status="failed" if error_code else "in_progress",
        last_task_error={"code": error_code, "message": "runner lost"},
    )
    server = _RecoveryServerClient([child])
    server.patch = AsyncMock(return_value=server._Resp({}))
    app = create_runner_app(server_client=server)  # type: ignore[arg-type]
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None and entry.work_id == DISPATCH_ID
    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]

    assert (
        runner_app.reap_stalled_subagent_launches(now=entry.created_at + 181, timeout_s=180) == []
    )
    assert inbox.empty()
    await _drain_inbox(inbox, server_client=server, conversation_id=PARENT_SESSION_ID)
    server.patch.assert_not_awaited()

    # No local child running edge: the server later records completion elsewhere.
    child.update(current_task_status="completed", last_task_error=None)
    reconciled = asyncio.Event()

    async def reconcile() -> None:
        await app.state.reconcile_pending_subagent_results()
        reconciled.set()

    sweep = asyncio.create_task(
        runner_app.run_subagent_launch_reaper(interval_s=0.001, reconcile_pending=reconcile)
    )
    try:
        await asyncio.wait_for(reconciled.wait(), timeout=5)
    finally:
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep
    assert inbox.qsize() == 1
    payload = inbox.get_nowait()
    assert payload["work_id"] == DISPATCH_ID
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["status"] == "completed"
    assert payload["output"] == "review complete: LGTM"
    await _cleanup_drained_subagent_work(payload, server_client=server)
    server.patch.assert_awaited_once_with(
        f"/v1/sessions/{CHILD_SESSION_ID}",
        json={"labels": {runner_app.SUBAGENT_DELIVERED_ID_LABEL_KEY: DISPATCH_ID}},
        timeout=30.0,
    )
    await app.state.reconcile_pending_subagent_results()
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    assert inbox.empty()
    late = runner_app.mark_subagent_work_terminal(
        CHILD_SESSION_ID, status="completed", output="review complete: LGTM"
    )
    assert late.delivered and not late.delivered_now
    assert inbox.empty()
    assert child["runner_id"] == "other-live-runner"


@pytest.mark.asyncio
async def test_cross_host_mirror_terminal_status_is_acked_without_local_delivery(
    _clean_subagent_registry: None,
) -> None:
    """The child runner's mirror edge must not register undeliverable work.

    A cross-host child's terminal edge reaches its own runner (host B) marked
    ``cross_host``: the PARENT's runner owns delivery into the parent inbox.
    The mirror must update the pane state only — attempting local delivery
    would register a work entry whose parent never runs here and 503.
    """
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    server_client = _SnapshotServerClient(
        _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        _parent_snapshot(parent_session_id=None),
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "output": "done", "cross_host": True},
            },
        )

    assert resp.status_code == 204, resp.text
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None
    assert runner_app._session_inboxes_ref.get(PARENT_SESSION_ID) is None
    # The mirror still feeds this runner's own pane / exit bookkeeping.
    assert app.state.native_pane_status[CHILD_SESSION_ID] == "idle"


@pytest.mark.asyncio
async def test_untracked_sub_agent_terminal_reports_to_the_server(
    _clean_subagent_registry: None,
) -> None:
    """A child this runner serves but does not track reports its terminal upstream.

    A cross-host child's runner never holds the parent's work entry, so a
    terminal edge has nowhere to deliver locally. The runner must hand it back
    to the server, whose sub-agent path forwards it to the parent's runner;
    a tracked child keeps the local path and reports nothing.
    """

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="reviewer",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        )

    server_client = _SnapshotServerClient(
        _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        _parent_snapshot(parent_session_id=None),
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        init = await client.post(
            "/v1/sessions",
            json={
                "session_id": CHILD_SESSION_ID,
                "agent_id": "ag_reviewer",
                "sub_agent_name": "reviewer",
            },
        )
        assert init.status_code == 201, init.text
        app.state.mark_subagent_terminal_and_wake(
            CHILD_SESSION_ID, status="completed", output="review complete: LGTM"
        )
        reports: list[dict[str, Any]] = []
        for _ in range(200):
            reports = [
                kwargs.get("json")
                for url, kwargs in server_client.posts
                if url.rstrip("/").endswith(f"/v1/sessions/{CHILD_SESSION_ID}/events")
            ]
            if reports:
                break
            await asyncio.sleep(0.01)

        assert reports, (
            "an untracked sub-agent's terminal edge never reached the server; the "
            "parent runner can never deliver the result"
        )
        event = reports[-1]
        assert event["type"] == "external_session_status"
        assert event["data"]["status"] == "completed"
        assert event["data"]["output"] == "review complete: LGTM"
        assert runner_app._session_inboxes_ref.get(PARENT_SESSION_ID) is None

        # A tracked child keeps the local delivery path and reports nothing.
        runner_app.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="reviewer",
            title="review",
        )
        before = len(server_client.posts)
        app.state.mark_subagent_terminal_and_wake(
            CHILD_SESSION_ID, status="completed", output="second"
        )
        await asyncio.sleep(0.05)
        assert len(server_client.posts) == before


class _FlakyChildReportServerClient(_SnapshotServerClient):
    """A server client whose child-event POST fails the first *failures* times."""

    def __init__(
        self, child_body: dict[str, Any], parent_body: dict[str, Any] | None, *, failures: int
    ) -> None:
        super().__init__(child_body, parent_body)
        self._failures = failures
        self.attempts = 0

    async def post(self, url: str, **kwargs: Any) -> Any:
        if url.rstrip("/").endswith(f"/v1/sessions/{CHILD_SESSION_ID}/events"):
            self.attempts += 1
            if self.attempts <= self._failures:
                raise httpx.ConnectError("server unreachable")
        return await super().post(url, **kwargs)


@pytest.mark.asyncio
async def test_untracked_terminal_report_is_single_shot(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost terminal report is not retried.

    A retry carries no dispatch id, so a late retry that lands after the parent
    started the child's next dispatch would complete that newer entry. The
    report is one bounded attempt.
    """
    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(runner_app, "_wake_retry_sleep", _record_sleep)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="reviewer",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        )

    server_client = _FlakyChildReportServerClient(
        _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        _parent_snapshot(parent_session_id=None),
        failures=1,
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        init = await client.post(
            "/v1/sessions",
            json={
                "session_id": CHILD_SESSION_ID,
                "agent_id": "ag_reviewer",
                "sub_agent_name": "reviewer",
            },
        )
        assert init.status_code == 201, init.text
        app.state.mark_subagent_terminal_and_wake(
            CHILD_SESSION_ID, status="completed", output="review complete: LGTM"
        )
        for _ in range(200):
            if server_client.attempts:
                break
            await asyncio.sleep(0.01)
        # Give any (wrongly) scheduled retry time to fire before asserting.
        await asyncio.sleep(0.05)

    assert server_client.attempts == 1, "the terminal report must not be retried"
    assert sleeps == [], "a lost report must not back off and retry"


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_running", [True, False])
async def test_child_dispatched_by_a_running_flow_delivers_without_a_wake(
    _clean_subagent_registry: None, flow_running: bool
) -> None:
    """A running flow's child reports through the flow's end wake, not its own."""
    from omnigent.runner import flows
    from omnigent.tools.builtins.flow import validate_flow_start_args

    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    run = flows._FlowRun(
        flow_id="flow_x",
        plan=validate_flow_start_args({"steps": [{"tool": "x", "args": {}}]}),  # type: ignore[arg-type]
        ctx=flows.FlowContext(server_client=None, conversation_id=PARENT_SESSION_ID),  # type: ignore[arg-type]
        created_mono=0.0,
        started_at=0.0,
    )
    flows._session_flows[PARENT_SESSION_ID] = {"flow_x": run}
    token = flows._step_run.set(run if flow_running else None)
    try:
        runner_app.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="reviewer",
            title="review",
        )
    finally:
        flows._step_run.reset(token)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="reviewer")

    server_client = _SnapshotServerClient(
        _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID)
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{CHILD_SESSION_ID}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "ok"},
                },
            )
            await asyncio.sleep(0.1)
    finally:
        flows._session_flows.pop(PARENT_SESSION_ID, None)
    assert resp.status_code == 204
    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].qsize() == 1
    wakes = [
        url for url, _ in server_client.posts if url == f"/v1/sessions/{PARENT_SESSION_ID}/events"
    ]
    assert (len(wakes), run.held_child_results) == ((0, 1) if flow_running else (1, 0))


@pytest.mark.asyncio
async def test_async_result_waker_posts_one_notice_when_inbox_holds_an_item(
    _clean_subagent_registry: None,
) -> None:
    """An async result in the inbox wakes the session once; an empty inbox stays quiet."""
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="worker")

    server_client = _SnapshotServerClient(
        _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID)
    )
    create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    waker = runner_app._async_result_waker
    assert waker is not None, "create_runner_app must install the async-result waker"

    waker(PARENT_SESSION_ID, "handle_abc123", "sys_os_shell", "completed")
    await asyncio.sleep(0.05)
    assert server_client.posts == [], "an empty inbox must not be woken"

    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID].put_nowait(
        {"handle_id": "handle_abc123", "tool_name": "sys_os_shell", "status": "completed"}
    )
    waker(PARENT_SESSION_ID, "handle_abc123", "sys_os_shell", "completed")
    for _ in range(200):
        if server_client.posts:
            break
        await asyncio.sleep(0.01)

    [event] = [
        kwargs["json"]
        for url, kwargs in server_client.posts
        if url == f"/v1/sessions/{PARENT_SESSION_ID}/events"
    ]
    assert event["type"] == "message"
    text = event["data"]["content"][0]["text"]
    assert text.startswith("[System: async task"), text
    assert "handle_abc123" in text
    assert "sys_os_shell" in text
    assert "completed" in text


class _Transient503WakeServerClient(_SnapshotServerClient):
    """Server client whose parent-event POST 503s a bounded number of times."""

    def __init__(self, child_body: dict[str, Any], *, failures: int) -> None:
        super().__init__(child_body)
        self._failures = failures
        self.statuses: list[int] = []

    async def post(self, url: str, **kwargs: Any) -> Any:
        self.posts.append((url, kwargs))
        if url == f"/v1/sessions/{PARENT_SESSION_ID}/events" and self._failures > 0:
            self._failures -= 1
            self.statuses.append(503)
            return httpx.Response(
                503,
                request=httpx.Request("POST", f"http://runner.test{url}"),
                json={"error": "runner unavailable"},
            )
        self.statuses.append(200)
        return self._Response()


@pytest.mark.asyncio
async def test_async_result_waker_rewakes_stranded_parent_on_next_turn(
    _clean_subagent_registry: None,
    _no_wake_backoff: list[float],
) -> None:
    """A failed async-result wake is re-attempted when the next turn starts empty.

    The parent inbox holds only an async result, so the stranded-wake rescue
    paths find no sub-agent work entry to replay; without the recorded async
    result the parent is never re-woken.
    """
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="worker")

    server_client = _Transient503WakeServerClient(
        _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        failures=runner_app._WAKE_POST_MAX_ATTEMPTS,
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    waker = runner_app._async_result_waker
    assert waker is not None, "create_runner_app must install the async-result waker"

    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID].put_nowait(
        {"handle_id": "handle_abc123", "tool_name": "sys_os_shell", "status": "completed"}
    )
    waker(PARENT_SESSION_ID, "handle_abc123", "sys_os_shell", "completed")
    for _ in range(200):
        if len(server_client.posts) >= runner_app._WAKE_POST_MAX_ATTEMPTS:
            break
        await asyncio.sleep(0.01)

    assert server_client.statuses == [503] * runner_app._WAKE_POST_MAX_ATTEMPTS
    assert len(_no_wake_backoff) == runner_app._WAKE_POST_MAX_ATTEMPTS - 1

    await app.state.check_and_start_next_turn(PARENT_SESSION_ID)
    for _ in range(200):
        if len(server_client.posts) > runner_app._WAKE_POST_MAX_ATTEMPTS:
            break
        await asyncio.sleep(0.01)

    assert server_client.statuses == [503] * runner_app._WAKE_POST_MAX_ATTEMPTS + [200]
    last_url, last_kwargs = server_client.posts[-1]
    assert last_url == f"/v1/sessions/{PARENT_SESSION_ID}/events"
    text = last_kwargs["json"]["data"]["content"][0]["text"]
    assert text.startswith("[System: async task"), text
    assert "handle_abc123" in text

    # The inbox stays undrained (no turn ever consumed it), so every later turn
    # boundary finds the same stranded result. The rescue must not re-post the
    # identical notice — otherwise an undrained result wakes the caller at every
    # idle turn boundary forever.
    posts_after_rescue = len(server_client.posts)
    await app.state.check_and_start_next_turn(PARENT_SESSION_ID)
    await app.state.check_and_start_next_turn(PARENT_SESSION_ID)
    await asyncio.sleep(0.05)

    assert len(server_client.posts) == posts_after_rescue
    assert server_client.statuses == [503] * runner_app._WAKE_POST_MAX_ATTEMPTS + [200]


def _assistant_item(item_id: str, text: str) -> dict[str, Any]:
    """Build a transcript item carrying the server item id and assistant text."""
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _delivery_app(
    items: list[dict[str, Any]],
) -> tuple[Any, _SnapshotServerClient]:
    """Build a runner app whose child transcript serves *items* newest first."""
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="reviewer")

    server_client = _SnapshotServerClient(
        _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        items=items,
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    return app, server_client


async def _post_terminal(
    client: Any, *, status: str = "idle", output: str | None = "result"
) -> Any:
    return await client.post(
        f"/v1/sessions/{CHILD_SESSION_ID}/events",
        json={"type": "external_session_status", "data": {"status": status, "output": output}},
    )


@pytest.mark.asyncio
async def test_second_turn_before_drain_is_delivered(
    _clean_subagent_registry: None,
) -> None:
    """A child's next turn delivers even while the prior result is undrained.

    Dedup is by result identity: the second turn's item id differs, so its
    terminal edge is a new result rather than an already-delivered duplicate.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, server_client = _delivery_app([_assistant_item("item_a", "A")])

    async with _runner_client(app) as client:
        assert (await _post_terminal(client, output="A")).status_code == 204
        server_client.items = [_assistant_item("item_b", "B")]
        assert (await _post_terminal(client, output="B")).status_code == 204

    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    assert [inbox.get_nowait()["output"] for _ in range(2)] == ["A", "B"]
    entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None and entry.delivered_result_key == "item_b"


@pytest.mark.asyncio
async def test_second_turn_after_drain_is_delivered(
    _clean_subagent_registry: None,
) -> None:
    """A drained child's next turn rebuilds the entry and delivers.

    The drained result's key is the only tombstone: a different key is a new
    turn and must wake the mother again.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, server_client = _delivery_app([_assistant_item("item_a", "A")])

    async with _runner_client(app) as client:
        assert (await _post_terminal(client, output="A")).status_code == 204
        runner_app.unregister_subagent_work(CHILD_SESSION_ID, remember_drained_delivery=True)
        inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
        inbox.get_nowait()

        server_client.items = [_assistant_item("item_b", "B")]
        assert (await _post_terminal(client, output="B")).status_code == 204

    assert inbox.qsize() == 1
    assert inbox.get_nowait()["output"] == "B"


@pytest.mark.asyncio
async def test_delayed_stop_for_a_prior_turn_does_not_touch_the_next_turn(
    _clean_subagent_registry: None,
) -> None:
    """A delayed Stop replay is deduped by the prior result's key.

    Turn A's key was delivered and drained; the next dispatch carries it onto
    the new running entry, so a replayed Stop for A acknowledges as already
    delivered instead of cancelling turn B.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, server_client = _delivery_app([_assistant_item("item_a", "A")])

    async with _runner_client(app) as client:
        assert (await _post_terminal(client, output="A")).status_code == 204
        runner_app.unregister_subagent_work(CHILD_SESSION_ID, remember_drained_delivery=True)
        inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
        inbox.get_nowait()

        # Turn B dispatch: a fresh running entry that remembers A's key.
        entry = runner_app.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="reviewer",
            title="review",
        )
        entry.status = "running"
        assert entry.delivered_result_key == "item_a"

        # A delayed Stop for A replays A's output, so it names A's item.
        replay = await _post_terminal(client, status="stopped", output="A")
        assert replay.status_code == 204
        assert entry.status == "running"
        assert inbox.empty(), "a delayed stop for the prior turn was delivered"

        server_client.items = [_assistant_item("item_b", "B")]
        assert (await _post_terminal(client, output="B")).status_code == 204

    assert inbox.qsize() == 1
    assert inbox.get_nowait()["output"] == "B"
    assert entry.delivered_result_key == "item_b"


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed_output", ["A", None])
async def test_delayed_terminal_cannot_claim_the_next_turn_key(
    _clean_subagent_registry: None, delayed_output: str | None
) -> None:
    """A delayed terminal edge must not bind to the newer turn's transcript tail.

    Turn A was delivered and drained, then the child wrote turn B's assistant
    item with B running. A's delayed terminal reports A's own text (or no text
    while the child is running), so it can only name A's item; B's later
    completion still delivers and wakes the mother.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    runner_app._drained_delivered_subagent_results[CHILD_SESSION_ID] = "item_a"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="reviewer")

    server_client = _SnapshotServerClient(
        _child_snapshot(
            sub_agent_name="reviewer",
            parent_session_id=PARENT_SESSION_ID,
            status="running",
        ),
        items=[_assistant_item("item_b", "B"), _assistant_item("item_a", "A")],
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    try:
        async with _runner_client(app) as client:
            delayed = await _post_terminal(client, output=delayed_output)
            assert delayed.status_code == 204
            assert inbox.empty(), "a delayed terminal for turn A was delivered"
            assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None, (
                "the delayed turn-A terminal rebuilt an entry under turn B's key"
            )

            server_client.items = [_assistant_item("item_b", "B")]
            assert (await _post_terminal(client, output="B")).status_code == 204
            await asyncio.sleep(0.1)
    finally:
        runner_app._session_inboxes_ref.pop(PARENT_SESSION_ID, None)

    wakes = [
        url for url, _ in server_client.posts if url == f"/v1/sessions/{PARENT_SESSION_ID}/events"
    ]
    entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
    assert wakes, "turn B's completion must wake the mother"
    assert inbox.get_nowait()["output"] == "B"
    assert entry is not None and entry.delivered_result_key == "item_b"


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed_output", ["Error: A failed", None])
async def test_delayed_unmatched_terminal_cannot_claim_the_next_turn_key(
    _clean_subagent_registry: None, delayed_output: str | None
) -> None:
    """A delayed terminal that names no item must yield no key.

    Turn A was delivered and drained, then the child wrote turn B's assistant
    item. A's delayed failed edge reports text that matches no transcript item,
    or no text at all while the child's status is unreadable. Neither names a
    result, so neither may take B's tail item; B's completion still delivers
    and wakes the mother.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    runner_app._drained_delivered_subagent_results[CHILD_SESSION_ID] = "item_a"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="reviewer")

    server_client = _SnapshotServerClient(
        _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        items=[_assistant_item("item_b", "B"), _assistant_item("item_a", "A")],
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    try:
        async with _runner_client(app) as client:
            delayed = await _post_terminal(client, status="failed", output=delayed_output)
            assert delayed.status_code == 204
            assert inbox.empty(), "a delayed unmatched terminal was delivered"
            assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None, (
                "the delayed turn-A terminal rebuilt an entry under turn B's key"
            )

            server_client.items = [_assistant_item("item_b", "B")]
            assert (await _post_terminal(client, output="B")).status_code == 204
            await asyncio.sleep(0.1)
    finally:
        runner_app._session_inboxes_ref.pop(PARENT_SESSION_ID, None)

    wakes = [
        url for url, _ in server_client.posts if url == f"/v1/sessions/{PARENT_SESSION_ID}/events"
    ]
    entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
    assert wakes, "turn B's completion must wake the mother"
    assert inbox.get_nowait()["output"] == "B"
    assert entry is not None and entry.delivered_result_key == "item_b"


@pytest.mark.asyncio
async def test_parent_cleanup_clears_drained_child_state(
    _clean_subagent_registry: None,
) -> None:
    """Deleting the parent drops its drained children's retained state.

    A drain removes the child from the parent index while keeping its delivered
    result tombstone and its dispatch origin, so parent cleanup can only find
    them through the retained-state owner map. Without that, a long-lived
    runner leaks one drained result and one origin per dispatched child.
    """
    entry = runner_app.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review",
        registered_by="sys_session_send",
    )
    entry.delivered = True
    entry.delivered_result_key = "item_a"
    runner_app.unregister_subagent_work(CHILD_SESSION_ID, remember_drained_delivery=True)
    assert CHILD_SESSION_ID in runner_app._drained_delivered_subagent_results
    assert CHILD_SESSION_ID in runner_app._subagent_work_origins

    runner_app.unregister_subagent_work_for_session(PARENT_SESSION_ID)

    assert CHILD_SESSION_ID not in runner_app._drained_delivered_subagent_results
    assert CHILD_SESSION_ID not in runner_app._subagent_work_origins


@pytest.mark.asyncio
async def test_same_key_failure_escalates_a_delivered_completion(
    _clean_subagent_registry: None,
) -> None:
    """A failed edge with the delivered result's key still replaces it.

    The watcher's idle edge records and delivers ``completed``; the turn's
    real ``failed`` edge reports the same result identity (no new assistant
    item). The failure must replace the completed record and notify again
    instead of acknowledging as already delivered.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, _server_client = _delivery_app([_assistant_item("item_a", "partial output")])

    async with _runner_client(app) as client:
        first = await _post_terminal(client, output="partial output")
        failed = await _post_terminal(client, status="failed", output="Error: BOOM from provider")
        await asyncio.sleep(0.1)

    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
    assert first.status_code == 204 and failed.status_code == 204
    assert entry is not None
    assert entry.status == "failed", "the same-key failure must replace the completed record"
    assert entry.output == "Error: BOOM from provider"
    outputs = [inbox.get_nowait()["output"] for _ in range(inbox.qsize())]
    assert outputs == ["partial output", "Error: BOOM from provider"]


@pytest.mark.asyncio
@pytest.mark.parametrize("survives_restart", [True, False])
async def test_codex_mother_wakes_after_recovery_of_a_drained_dispatched_child(
    _clean_subagent_registry: None, survives_restart: bool
) -> None:
    """A drained tool-dispatched child's own next turn still wakes Codex.

    The child was created and dispatched through an Omnigent tool, delivered
    and drained. When the child starts turn B itself, recovery rebuilds the
    entry; its origin must survive the drain — from the runner registry, or,
    after a restart wiped it, from the child's non-codex snapshot — so the
    wake is not suppressed as a codex-internal thread.
    """
    inbox_handle: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox_handle

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        if agent_id == "ag_codex_parent":
            return AgentSpec(
                spec_version=1,
                name="codex",
                executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
            )
        return AgentSpec(spec_version=1, name="reviewer")

    server_client = _SnapshotServerClient(
        _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        # Turn A is a routine result: recorded and drained, but its quiet
        # marker means the only wake in this test is turn B's.
        items=[_assistant_item("item_q", "routine\n[quiet]")],
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    try:
        async with _runner_client(app) as client:
            init = await client.post(
                "/v1/sessions",
                json={"session_id": PARENT_SESSION_ID, "agent_id": "ag_codex_parent"},
            )
            assert init.status_code == 201, init.text
            runner_app.register_subagent_work(
                parent_session_id=PARENT_SESSION_ID,
                child_session_id=CHILD_SESSION_ID,
                agent="reviewer",
                title="review",
                registered_by="sys_session_create",
            )
            assert (await _post_terminal(client, output="routine\n[quiet]")).status_code == 204
            await asyncio.sleep(0.1)

            # The mother drained turn A's quiet result.
            entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
            assert entry is not None and entry.delivered
            runner_app.unregister_subagent_work(CHILD_SESSION_ID, remember_drained_delivery=True)
            if survives_restart:
                runner_app._subagent_work_origins.pop(CHILD_SESSION_ID, None)

            server_client.items = [_assistant_item("item_b", "B")]
            assert (await _post_terminal(client, output="B")).status_code == 204
            await asyncio.sleep(0.1)
    finally:
        runner_app._session_inboxes_ref.pop(PARENT_SESSION_ID, None)

    wakes = [
        url for url, _ in server_client.posts if url == f"/v1/sessions/{PARENT_SESSION_ID}/events"
    ]
    assert len(wakes) == 1, (
        "the drained dispatched child's turn-B completion must wake its Codex "
        f"mother (got {len(wakes)} wake(s))"
    )
    assert inbox_handle.get_nowait()["output"] == "B"


@pytest.mark.asyncio
async def test_recovered_entry_placement_label_uses_effective_host(
    _clean_subagent_registry: None,
) -> None:
    """A hostless-row child keeps its host label when recovery rebuilds it.

    The child row carries no ``host_id``; its parent's row does. The placement
    label must resolve the effective host up the parent chain, not read the
    raw child row.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    runner_app._drained_delivered_subagent_results[CHILD_SESSION_ID] = "item_a"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="reviewer")

    server_client = _SnapshotServerClient(
        _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        parent_body={
            "id": PARENT_SESSION_ID,
            "parent_session_id": None,
            "host_id": "host_a",
        },
        items=[_assistant_item("item_b", "B")],
        host_names={"host_a": "alpha"},
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    try:
        async with _runner_client(app) as client:
            assert (await _post_terminal(client, output="B")).status_code == 204
            await asyncio.sleep(0.1)
    finally:
        runner_app._session_inboxes_ref.pop(PARENT_SESSION_ID, None)

    entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None
    assert entry.host_id == "host_a"
    assert entry.placement_label == "alpha"


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_running", [True, False])
async def test_recovered_flow_child_turn_is_held_only_when_already_dispatched(
    _clean_subagent_registry: None, flow_running: bool
) -> None:
    """A recovered turn joins no new flow; an existing membership holds it.

    The mother drained turn A, then the child started turn B itself. A live
    flow run that had previously dispatched this child still holds the wake
    (and counts it); no run means the recovery wake goes out.
    """
    from omnigent.runner import flows
    from omnigent.tools.builtins.flow import validate_flow_start_args

    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    runner_app._drained_delivered_subagent_results[CHILD_SESSION_ID] = "item_a"
    run = flows._FlowRun(
        flow_id="flow_x",
        plan=validate_flow_start_args({"steps": [{"tool": "x", "args": {}}]}),  # type: ignore[arg-type]
        ctx=flows.FlowContext(server_client=None, conversation_id=PARENT_SESSION_ID),  # type: ignore[arg-type]
        created_mono=0.0,
        started_at=0.0,
    )
    if flow_running:
        run.children.add(CHILD_SESSION_ID)
    flows._session_flows[PARENT_SESSION_ID] = {"flow_x": run}
    app, server_client = _delivery_app([_assistant_item("item_b", "B")])
    try:
        async with _runner_client(app) as client:
            assert (await _post_terminal(client, output="B")).status_code == 204
            await asyncio.sleep(0.1)
        inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
        assert inbox.qsize() == 1
        wakes = [
            url
            for url, _ in server_client.posts
            if url == f"/v1/sessions/{PARENT_SESSION_ID}/events"
        ]
        assert (len(wakes), run.held_child_results) == ((0, 1) if flow_running else (1, 0))
    finally:
        flows._session_flows.pop(PARENT_SESSION_ID, None)


@pytest.mark.asyncio
async def test_quiet_result_is_recorded_without_inbox_or_wake(
    _clean_subagent_registry: None,
) -> None:
    """A ``[quiet]`` result is recorded (key stored) but not delivered (D9).

    The child marks its own routine turn; a replay stays deduped, and the
    child's next real result still delivers.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, server_client = _delivery_app(
        [_assistant_item("item_q", "checked; nothing to report\n[quiet]")]
    )

    async with _runner_client(app) as client:
        first = await _post_terminal(client, output="checked; nothing to report\n[quiet]")
        replay = await _post_terminal(client, output="checked; nothing to report\n[quiet]")
        await asyncio.sleep(0.1)

        inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
        entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
        assert first.status_code == 204 and replay.status_code == 204
        assert entry is not None and entry.delivered is True
        assert entry.delivered_result_key == "item_q"
        assert inbox.empty(), "a quiet result must not enter the inbox"
        wakes = [
            url
            for url, _ in server_client.posts
            if url == f"/v1/sessions/{PARENT_SESSION_ID}/events"
        ]
        assert wakes == [], "a quiet result must not wake the parent"

        server_client.items = [_assistant_item("item_r", "real")]
        assert (await _post_terminal(client, output="real")).status_code == 204

    assert inbox.qsize() == 1
    assert inbox.get_nowait()["output"] == "real"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("registered_by", "expects_wake"),
    [
        ("sys_session_create", True),
        ("sys_session_send", True),
        (None, False),
    ],
)
async def test_codex_mother_is_woken_only_for_dispatched_children(
    _clean_subagent_registry: None, registered_by: str | None, expects_wake: bool
) -> None:
    """A Codex mother is woken for an Omnigent-dispatched child, not its own.

    Codex-internal threads are consumed inside the parent's app-server; a
    child registered by sys_session_create / sys_session_send is a real
    session the mother is waiting on.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        if agent_id == "ag_codex_parent":
            return AgentSpec(
                spec_version=1,
                name="codex",
                executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
            )
        return AgentSpec(spec_version=1, name="reviewer")

    server_client = _SnapshotServerClient(
        _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        items=[_assistant_item("item_c", "done")],
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        init = await client.post(
            "/v1/sessions",
            json={"session_id": PARENT_SESSION_ID, "agent_id": "ag_codex_parent"},
        )
        assert init.status_code == 201, init.text
        runner_app.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="reviewer",
            title="review",
            registered_by=registered_by,
        )
        assert (await _post_terminal(client, output="done")).status_code == 204
        await asyncio.sleep(0.1)

    wakes = [
        url for url, _ in server_client.posts if url == f"/v1/sessions/{PARENT_SESSION_ID}/events"
    ]
    assert bool(wakes) is expects_wake


@pytest.mark.asyncio
async def test_switch_off_suppresses_recovery_of_an_undispatched_turn(
    _clean_subagent_registry: None,
) -> None:
    """With the master switch off, a child-started turn is not recovered (R11).

    The drained prior result's key differs, but the live switch says no new
    collaboration surface: acknowledge without an inbox entry, a wake, or a
    rebuilt work entry.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    runner_app._drained_delivered_subagent_results[CHILD_SESSION_ID] = "item_a"

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="reviewer")

    server_client = _SnapshotServerClient(
        _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        items=[_assistant_item("item_b", "B")],
        collab_enabled=False,
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        resp = await _post_terminal(client, output="B")
        await asyncio.sleep(0.1)

    assert resp.status_code == 204
    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    assert inbox.empty()
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None
    wakes = [
        url for url, _ in server_client.posts if url == f"/v1/sessions/{PARENT_SESSION_ID}/events"
    ]
    assert wakes == []
