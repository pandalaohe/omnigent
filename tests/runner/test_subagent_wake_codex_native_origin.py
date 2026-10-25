"""Wake ownership for codex-native child threads.

A Codex-native child is normally consumed inside the parent's own app-server, so
its completion owes no Omnigent inbox wake. A child Codex spawned for a task the
mother asked for is the exception: its labels record the spawn item's id, which
proves a delegation rather than a user ``/side`` fork, and the mother is waiting
on that result. These tests pin the whole policy — the label reader, the single
ownership predicate, and the wake path plus stranded-wake rescue that both call
it — at the real runner event seam.

What they cannot prove: that a wake POST starts a turn in the native parent, and
whether Codex continues the parent by itself. Both need a turn-start receipt
outside this runner.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent._wrapper_labels import WRAPPER_LABEL_KEY
from omnigent.runner import create_runner_app, subagent_work
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient

PARENT_SESSION_ID = "conv_codex_mother"
CHILD_SESSION_ID = "conv_native_child"
SECOND_CHILD_ID = "conv_native_child_two"

NATIVE_WRAPPER = "codex-native-ui-subagent"
SPAWN_ITEM_ID = "call_7f3a"
DELEGATED = subagent_work.CODEX_NATIVE_ORIGIN_DELEGATED
UNKNOWN = subagent_work.CODEX_NATIVE_ORIGIN_UNKNOWN
TOOL_CALL_ID_LABEL = subagent_work.CODEX_NATIVE_TOOL_CALL_ID_LABEL_KEY
_DELEGATED_LABELS: dict[str, str] = {
    WRAPPER_LABEL_KEY: NATIVE_WRAPPER,
    TOOL_CALL_ID_LABEL: SPAWN_ITEM_ID,
}


def _assistant_item(item_id: str, text: str) -> dict[str, Any]:
    """Build one assistant transcript item as the sessions API returns it."""
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


class _OriginServerClient(NullServerClient):
    """Serve one child snapshot + transcript and record the parent's wake POSTs.

    The child body is the ``SessionResponse`` shape the runner reads when it
    rebuilds a lost work entry, so the origin under test comes from real labels
    rather than from a test-only hook. ``wake_down`` fails the parent's wake
    POST like a runner tunnel outage.
    """

    def __init__(self, child_body: dict[str, Any], *, items: list[dict[str, Any]] | None = None):
        self.child_body = child_body
        self.items = [] if items is None else items
        self.wakes: list[dict[str, Any]] = []
        self.wake_down = False
        self._parent_events_path = f"/v1/sessions/{PARENT_SESSION_ID}/events"

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
        if url.rstrip("/").endswith(f"/v1/sessions/{CHILD_SESSION_ID}"):
            return self._Resp(self.child_body)
        if url.rstrip("/").endswith(f"/v1/sessions/{SECOND_CHILD_ID}"):
            return self._Resp({**self.child_body, "id": SECOND_CHILD_ID})
        if url.rstrip("/").endswith("/items"):
            return self._Resp({"data": self.items, "has_more": False})
        return self._Resp({})

    async def post(self, url: str, **kwargs: Any) -> Any:
        if url != self._parent_events_path:
            return self._Resp({})
        body = kwargs.get("json")
        request = httpx.Request("POST", f"http://runner.test{url}")
        if self.wake_down:
            return httpx.Response(503, request=request, json={"error": "RUNNER_UNAVAILABLE"})
        if isinstance(body, dict):
            self.wakes.append(body)
        return httpx.Response(200, request=request, json={})


def _child_body(labels: dict[str, str] | None) -> dict[str, Any]:
    """Build the child ``SessionResponse`` body carrying *labels*."""
    return {
        "id": CHILD_SESSION_ID,
        "agent_id": "ag_native_child",
        "agent_name": "codex-native-ui",
        "sub_agent_name": "worker",
        "parent_session_id": PARENT_SESSION_ID,
        "created_at": 0,
        "workspace": None,
        "labels": labels or {},
    }


async def _codex_mother_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
    """Resolve the mother and its workers as codex-native sessions."""
    del session_id
    return AgentSpec(
        spec_version=1,
        name="mother" if agent_id == "ag_codex_mother" else "worker",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )


def _build_app(server_client: _OriginServerClient) -> Any:
    """Build a runner app whose sessions resolve against *server_client*."""
    return create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_codex_mother_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )


async def _init_mother(client: Any) -> None:
    """Register the mother session so its harness resolves to codex-native."""
    resp = await client.post(
        "/v1/sessions", json={"session_id": PARENT_SESSION_ID, "agent_id": "ag_codex_mother"}
    )
    assert resp.status_code == 201, resp.text


async def _post_terminal(
    client: Any, *, child_id: str = CHILD_SESSION_ID, output: str, status: str = "idle"
) -> Any:
    """Report a terminal edge for *child_id* through the runner event route."""
    return await client.post(
        f"/v1/sessions/{child_id}/events",
        json={"type": "external_session_status", "data": {"status": status, "output": output}},
    )


def _wake_candidate(
    child_session_id: str,
    *,
    wrapper_label: str | None,
    completed_at: float | None,
    registered_by: str | None = None,
    native_origin: str = UNKNOWN,
) -> subagent_work._SubagentWorkEntry:
    """Build one work entry for the ownership matrix."""
    return subagent_work._SubagentWorkEntry(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=child_session_id,
        work_id=f"subagent_{child_session_id}",
        agent="worker",
        title="task",
        wrapper_label=wrapper_label,
        completed_at=completed_at,
        registered_by=registered_by,
        native_origin=native_origin,
    )


def _wipe_runner_local_state() -> None:
    """Drop the runner-local maps a process restart loses.

    ``_clean_subagent_registry`` restores these after the test; here the wipe is
    part of the scenario, so a second app has to rebuild the child from its
    persisted labels instead of an in-memory registration.
    """
    subagent_work._subagent_work_by_child.clear()
    subagent_work._subagent_work_by_parent.clear()
    subagent_work._subagent_work_origins.clear()
    subagent_work._subagent_retained_state_parents.clear()
    subagent_work._drained_delivered_subagent_results.clear()
    subagent_work._subagent_recovery_done.clear()


# --------------------------------------------------------------------------- #
# Labels -> origin
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (_DELEGATED_LABELS, DELEGATED),
        # The forwarder stamps the spawn item id from either spawn item type
        # (collabAgentToolCall / subAgentActivity); the wrapper half is not
        # needed to reach the verdict.
        ({TOOL_CALL_ID_LABEL: SPAWN_ITEM_ID}, DELEGATED),
        # A /side fork carries a displayed nickname only.
        (
            {WRAPPER_LABEL_KEY: NATIVE_WRAPPER, "omnigent.codex_native.agent_nickname": "Side"},
            UNKNOWN,
        ),
        # Role-only backfill is not trusted: a forked thread can inherit it.
        (
            {WRAPPER_LABEL_KEY: NATIVE_WRAPPER, "omnigent.codex_native.agent_role": "explorer"},
            UNKNOWN,
        ),
        ({TOOL_CALL_ID_LABEL: ""}, UNKNOWN),
        ({}, UNKNOWN),
        (None, UNKNOWN),
        (SPAWN_ITEM_ID, UNKNOWN),
        ([(TOOL_CALL_ID_LABEL, SPAWN_ITEM_ID)], UNKNOWN),
        ({TOOL_CALL_ID_LABEL: None}, UNKNOWN),
        ({TOOL_CALL_ID_LABEL: 1234}, UNKNOWN),
        ({TOOL_CALL_ID_LABEL: object()}, UNKNOWN),
    ],
)
def test_codex_native_origin_from_labels(labels: object, expected: str) -> None:
    """Only a persisted spawn item id proves a delegated Codex job."""
    assert subagent_work.codex_native_origin_from_labels(labels) == expected


# --------------------------------------------------------------------------- #
# One ownership predicate — every legacy verdict preserved
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("wrapper_label", "parent_harness", "registered_by", "native_origin", "expected"),
    [
        # Legacy true verdicts: an internal-label thread a tool registered, and
        # every non-internal thread, keep waking.
        (NATIVE_WRAPPER, "codex-native", "sys_session_create", "unknown", True),
        (NATIVE_WRAPPER, "codex-native", "sys_session_send", "unknown", True),
        (NATIVE_WRAPPER, "codex-native", "recovery", "unknown", True),
        # A codex-native parent suppresses a thread with no native-child wrapper
        # proof: the harness term alone makes it internal (legacy verdict).
        ("codex-native-ui", "codex-native", None, "unknown", False),
        ("claude-code-native-ui-subagent", "claude-native", None, "unknown", True),
        (None, "claude-native", None, "unknown", True),
        # Legacy suppression, preserved: no proven delegation.
        (NATIVE_WRAPPER, "codex-native", None, "unknown", False),
        ("codex-native-ui", "codex-native", None, "delegated", False),
        (None, "codex-native", None, "unknown", False),
        # A native thread whose parent row claims another harness keeps its
        # suppression even with a proven origin.
        (NATIVE_WRAPPER, "claude-native", None, "delegated", False),
        (NATIVE_WRAPPER, "claude-code-native", None, "delegated", False),
        # The fix: a proven delegated Codex job owes the wake; an unresolved
        # parent harness reads as the app-server the wrapper already proves.
        (NATIVE_WRAPPER, "codex-native", None, "delegated", True),
        (NATIVE_WRAPPER, None, None, "delegated", True),
    ],
)
def test_entry_owes_parent_wake_truth_table(
    wrapper_label: str | None,
    parent_harness: str | None,
    registered_by: str | None,
    native_origin: str,
    expected: bool,
) -> None:
    """The ownership matrix: legacy verdicts hold, only a proven job is added."""
    entry = _wake_candidate(
        CHILD_SESSION_ID,
        wrapper_label=wrapper_label,
        completed_at=1.0,
        registered_by=registered_by,
        native_origin=native_origin,
    )

    assert subagent_work.entry_owes_parent_wake(entry, parent_harness) is expected


def test_latest_wake_eligible_uses_the_same_policy() -> None:
    """Stranded-wake selection cannot hold a filter the wake path would reject."""
    delegated = _wake_candidate(
        "conv_delegated",
        wrapper_label=NATIVE_WRAPPER,
        completed_at=100.0,
        native_origin=subagent_work.CODEX_NATIVE_ORIGIN_DELEGATED,
    )
    side_chat = _wake_candidate("conv_side", wrapper_label=NATIVE_WRAPPER, completed_at=200.0)
    # An official child of a Codex mother: registered by a real Omnigent surface,
    # which is what the legacy rule admits regardless of its wrapper label.
    independent = _wake_candidate(
        "conv_worker",
        wrapper_label="codex-native-ui",
        completed_at=50.0,
        registered_by="sys_session_send",
    )

    # A delegated job finishing last no longer shadows the independent child.
    assert (
        subagent_work.latest_wake_eligible_subagent_work(
            [independent, side_chat, delegated], "codex-native"
        )
        is delegated
    )
    # Without a proven origin the internal threads stay ineligible, so the
    # pre-existing rescue choice is unchanged.
    assert (
        subagent_work.latest_wake_eligible_subagent_work([independent, side_chat], "codex-native")
        is independent
    )
    # A parent recorded as another harness keeps its native threads suppressed.
    assert subagent_work.latest_wake_eligible_subagent_work([delegated], "claude-native") is None


def test_reregistration_keeps_a_proven_origin(_clean_subagent_registry: None) -> None:
    """A proven delegated origin survives re-registration that cannot reprove it."""

    def _register(native_origin: str | None) -> subagent_work._SubagentWorkEntry:
        return subagent_work.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="worker",
            title="task",
            wrapper_label=NATIVE_WRAPPER,
            native_origin=native_origin,
        )

    assert _register(DELEGATED).native_origin == DELEGATED
    # A later registration whose labels resolved nothing keeps the proven value.
    assert _register(None).native_origin == DELEGATED
    assert _register(UNKNOWN).native_origin == DELEGATED
    # The other direction still upgrades an entry that had no proof.
    second_child = "conv_native_child_two"
    subagent_work.unregister_subagent_work(CHILD_SESSION_ID)
    first = subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=second_child,
        agent="worker",
        title="task",
        wrapper_label=NATIVE_WRAPPER,
    )
    assert first.native_origin == UNKNOWN
    upgraded = subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=second_child,
        agent="worker",
        title="task",
        wrapper_label=NATIVE_WRAPPER,
        native_origin=DELEGATED,
    )
    assert upgraded.native_origin == DELEGATED


# --------------------------------------------------------------------------- #
# Wake path at the real runner seam
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delegated_native_child_wakes_codex_mother(
    _clean_subagent_registry: None,
) -> None:
    """A rebuilt delegated child delivers and posts exactly one wake."""
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server = _OriginServerClient(
        _child_body(_DELEGATED_LABELS), items=[_assistant_item("item_a", "done")]
    )
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _init_mother(client)
        resp = await _post_terminal(client, output="done")
        await asyncio.sleep(0.05)

    assert resp.status_code == 204, resp.text
    entry = subagent_work.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None
    assert entry.native_origin == subagent_work.CODEX_NATIVE_ORIGIN_DELEGATED
    inbox = subagent_work._session_inboxes_ref[PARENT_SESSION_ID]
    assert inbox.qsize() == 1
    assert len(server.wakes) == 1, "a delegated native child must wake its Codex mother"
    notice = server.wakes[0]["data"]["content"][0]["text"]
    assert "1 result" in notice and "sys_read_inbox" in notice


@pytest.mark.asyncio
async def test_duplicate_terminal_edge_posts_one_wake(_clean_subagent_registry: None) -> None:
    """A redelivered terminal edge for the same result adds no second wake."""
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server = _OriginServerClient(
        _child_body(_DELEGATED_LABELS), items=[_assistant_item("item_a", "done")]
    )
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _init_mother(client)
        assert (await _post_terminal(client, output="done")).status_code == 204
        await asyncio.sleep(0.05)
        assert (await _post_terminal(client, output="done")).status_code == 204
        await asyncio.sleep(0.05)

    inbox = subagent_work._session_inboxes_ref[PARENT_SESSION_ID]
    assert inbox.qsize() == 1
    assert len(server.wakes) == 1


@pytest.mark.asyncio
async def test_side_chat_native_child_stays_silent(_clean_subagent_registry: None) -> None:
    """A /side fork still delivers its result but never wakes the chat."""
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server = _OriginServerClient(
        _child_body(
            {
                WRAPPER_LABEL_KEY: NATIVE_WRAPPER,
                "omnigent.codex_native.agent_nickname": "Side chat",
            }
        ),
        items=[_assistant_item("item_s", "side answer")],
    )
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _init_mother(client)
        assert (await _post_terminal(client, output="side answer")).status_code == 204
        await asyncio.sleep(0.05)

    entry = subagent_work.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None and entry.native_origin == subagent_work.CODEX_NATIVE_ORIGIN_UNKNOWN
    assert subagent_work._session_inboxes_ref[PARENT_SESSION_ID].qsize() == 1
    assert server.wakes == []


@pytest.mark.asyncio
async def test_unlabelled_native_child_stays_silent(_clean_subagent_registry: None) -> None:
    """No spawn label means no proven delegation, so the thread keeps its silence."""
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server = _OriginServerClient(
        _child_body({WRAPPER_LABEL_KEY: NATIVE_WRAPPER}),
        items=[_assistant_item("item_u", "unknown origin")],
    )
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _init_mother(client)
        assert (await _post_terminal(client, output="unknown origin")).status_code == 204
        await asyncio.sleep(0.05)

    assert server.wakes == []


@pytest.mark.asyncio
async def test_tool_dispatched_child_of_codex_mother_still_wakes(
    _clean_subagent_registry: None,
) -> None:
    """D12 unchanged: an Omnigent-dispatched child of a Codex mother wakes it."""
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server = _OriginServerClient(
        _child_body({WRAPPER_LABEL_KEY: NATIVE_WRAPPER}),
        items=[_assistant_item("item_d", "dispatched")],
    )
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _init_mother(client)
        subagent_work.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="worker",
            title="task",
            wrapper_label=NATIVE_WRAPPER,
            registered_by="sys_session_send",
        )
        assert (await _post_terminal(client, output="dispatched")).status_code == 204
        await asyncio.sleep(0.05)

    assert len(server.wakes) == 1


@pytest.mark.asyncio
async def test_restart_rebuild_keeps_delegated_origin(_clean_subagent_registry: None) -> None:
    """A restarted runner rebuilds the child as delegated and wakes for turn B.

    Turn A runs on the first app and is drained by the mother. The runner then
    restarts: every in-memory map and every app-scoped wake closure is gone, and
    the second app learns the origin from the child's persisted labels alone.
    The wake count is asserted from an explicit baseline taken after A, so a
    stale notice left over from turn A cannot satisfy the turn-B assertion.
    """
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server = _OriginServerClient(
        _child_body(_DELEGATED_LABELS), items=[_assistant_item("item_a", "A")]
    )

    first_app = _build_app(server)
    async with _runner_client(first_app) as client:
        await _init_mother(client)
        assert (await _post_terminal(client, output="A")).status_code == 204
        await asyncio.sleep(0.05)
    first_inbox = subagent_work._session_inboxes_ref[PARENT_SESSION_ID]
    assert first_inbox.qsize() == 1
    assert first_inbox.get_nowait()["output"] == "A"
    assert len(server.wakes) == 1, "turn A must wake the mother once"

    # Restart: the process-local registry and origin map are gone.
    _wipe_runner_local_state()
    server.items = [_assistant_item("item_b", "B")]
    server.wakes.clear()  # explicit before-B baseline

    second_app = _build_app(server)
    async with _runner_client(second_app) as client:
        await _init_mother(client)
        assert (await _post_terminal(client, output="B")).status_code == 204
        await asyncio.sleep(0.05)

    entry = subagent_work.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None
    assert entry.native_origin == subagent_work.CODEX_NATIVE_ORIGIN_DELEGATED
    assert len(server.wakes) == 1, "the rebuilt delegated child's turn B must wake the mother"
    notice = server.wakes[0]["data"]["content"][0]["text"]
    assert "sys_read_inbox" in notice, "the wake is a notice, not the child's text"
    second_inbox = subagent_work._session_inboxes_ref[PARENT_SESSION_ID]
    assert second_inbox.qsize() == 1, "turn B's result is the only undrained item"
    assert second_inbox.get_nowait()["output"] == "B"


@pytest.mark.asyncio
async def test_init_envelope_labels_supply_the_delegated_origin(
    _clean_subagent_registry: None,
) -> None:
    """A child described by an init envelope alone still carries its origin.

    The REST body for this child has no labels, so a wake here can only come
    from the envelope loader's snapshot — the second of the two capture points.
    """
    from omnigent.runner.session_init_protocol import (
        SESSION_INIT_PAYLOAD_KEY,
        SESSION_INIT_PROTOCOL_VERSION,
        RunnerSessionInitEnvelope,
        RunnerSessionInitSnapshot,
    )

    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server = _OriginServerClient(
        _child_body({WRAPPER_LABEL_KEY: NATIVE_WRAPPER}),
        items=[_assistant_item("item_a", "done")],
    )
    app = _build_app(server)

    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="0.16.0",
        session_id=CHILD_SESSION_ID,
        agent_id="ag_native_child",
        snapshot=RunnerSessionInitSnapshot(
            created_at=1,
            updated_at=1,
            parent_session_id=PARENT_SESSION_ID,
            labels=_DELEGATED_LABELS,
        ),
    )
    async with _runner_client(app) as client:
        await _init_mother(client)
        init = await client.post(
            "/v1/sessions",
            json={
                "session_id": CHILD_SESSION_ID,
                "agent_id": "ag_native_child",
                SESSION_INIT_PAYLOAD_KEY: envelope.model_dump(mode="json"),
            },
        )
        assert init.status_code == 201, init.text
        assert (await _post_terminal(client, output="done")).status_code == 204
        await asyncio.sleep(0.05)

    entry = subagent_work.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None
    assert entry.native_origin == DELEGATED
    assert len(server.wakes) == 1, "the envelope's spawn label must admit the wake"


@pytest.mark.asyncio
async def test_late_origin_discovery_upgrades_a_silent_entry(
    _clean_subagent_registry: None,
) -> None:
    """An entry registered before its labels were readable still wakes later.

    The first terminal edge sees no spawn label, so the thread keeps the legacy
    silence. When a later read proves the delegation, re-registering carries the
    origin onto the entry and the next terminal edge wakes the mother once.
    """
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server = _OriginServerClient(
        _child_body({WRAPPER_LABEL_KEY: NATIVE_WRAPPER}),
        items=[_assistant_item("item_a", "A")],
    )
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _init_mother(client)
        subagent_work.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="worker",
            title="task",
            wrapper_label=NATIVE_WRAPPER,
        )
        assert (await _post_terminal(client, output="A")).status_code == 204
        await asyncio.sleep(0.05)
        assert server.wakes == []

        # The spawn label is readable now; the same entry learns its origin.
        subagent_work.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="worker",
            title="task",
            wrapper_label=NATIVE_WRAPPER,
            native_origin=subagent_work.codex_native_origin_from_labels(_DELEGATED_LABELS),
        )
        server.items = [_assistant_item("item_b", "B")]
        assert (await _post_terminal(client, output="B")).status_code == 204
        await asyncio.sleep(0.05)

    entry = subagent_work.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None and entry.native_origin == subagent_work.CODEX_NATIVE_ORIGIN_DELEGATED
    assert len(server.wakes) == 1


@pytest.mark.asyncio
async def test_two_delegated_children_coalesce_one_pending_wake(
    _clean_subagent_registry: None,
) -> None:
    """While a wake is pending a second completion grows the inbox, not the wakes."""
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server = _OriginServerClient(
        _child_body(_DELEGATED_LABELS), items=[_assistant_item("item_a", "A")]
    )
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _init_mother(client)
        for child_id, item_id in ((CHILD_SESSION_ID, "item_a"), (SECOND_CHILD_ID, "item_b")):
            subagent_work.register_subagent_work(
                parent_session_id=PARENT_SESSION_ID,
                child_session_id=child_id,
                agent="worker",
                title="task",
                wrapper_label=NATIVE_WRAPPER,
                native_origin=subagent_work.CODEX_NATIVE_ORIGIN_DELEGATED,
            )
            edge = await _post_terminal(client, child_id=child_id, output="done")
            assert edge.status_code == 204, edge.text
            await asyncio.sleep(0.05)

    inbox = subagent_work._session_inboxes_ref[PARENT_SESSION_ID]
    assert inbox.qsize() == 2, "both delegated results must reach the inbox"
    assert len(server.wakes) == 1, "one pending wake covers the results queued behind it"


@pytest.mark.asyncio
async def test_stranded_wake_rescue_posts_for_a_delegated_native_child(
    _clean_subagent_registry: None,
    _no_wake_backoff: list[float],
) -> None:
    """The reconnect rescue re-posts a stranded delegated child's wake.

    This is the route the old double filter silently emptied: the wake POST
    fails during a tunnel outage, and the rescue must select the delegated entry
    whose origin came from the child's labels.
    """
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server = _OriginServerClient(
        _child_body(_DELEGATED_LABELS), items=[_assistant_item("item_a", "done")]
    )
    server.wake_down = True
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _init_mother(client)
        assert (await _post_terminal(client, output="done")).status_code == 204
        # Let the bounded retries exhaust so the parent is stranded.
        deadline = asyncio.get_running_loop().time() + 5.0
        while len(_no_wake_backoff) < subagent_work._WAKE_POST_MAX_ATTEMPTS - 1:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the wake POST never exhausted its bounded retries")
            await asyncio.sleep(0.01)
        assert server.wakes == []

        server.wake_down = False
        await app.state.catch_up_scan()
        deadline = asyncio.get_running_loop().time() + 5.0
        while not server.wakes:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    "the stranded-wake rescue never re-posted the delegated child's wake"
                )
            await asyncio.sleep(0.01)

    assert len(server.wakes) == 1
    assert "sys_read_inbox" in server.wakes[0]["data"]["content"][0]["text"]
