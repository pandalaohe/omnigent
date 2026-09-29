"""Running-period stamping and the snapshot ``running_since`` field.

``_note_running_edge`` is the single stamping site for the start of a
session's current running period: it is called from the status chokepoint
(``_publish_status``) and from the runner-status probe, the two writers that
can move a session into running. The stamp lives in an in-memory cache on the
tunnel-holding replica and in the ``omnigent.running_since`` conversation
label for restarts; ``GET /v1/sessions/{id}`` reports it as
``running_since`` while the session reads running.

Also covers the snapshot's ``last_message_preview`` gate
(``include_preview=true``), which reuses the session-list excerpt path.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
import pytest
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.server import session_live_state
from omnigent.server.routes._sessions import common, orchestration
from omnigent.server.routes._sessions.common import _RUNNING_SINCE_LABEL_KEY
from omnigent.server.routes._sessions.helpers import (
    _note_running_edge,
    _publish_status,
    _session_running_since_cache,
    _session_status_cache,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture()
def running_since_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, int, bool]]:
    """Record ``persist_running_since`` calls and isolate both status caches.

    The fake stands in for the background worker: it records the enqueue and,
    for an unknown-previous edge, invokes ``on_resolved`` synchronously so a
    test can observe the value the worker would have resolved. Cache
    snapshots are restored on teardown so entries set here never leak out.
    """
    calls: list[tuple[str, int, bool]] = []

    def _record(
        session_id: str,
        started_at: int,
        *,
        previous_known: bool,
        on_resolved: Callable[[int], None],
    ) -> None:
        calls.append((session_id, started_at, previous_known))
        if not previous_known:
            on_resolved(started_at)

    monkeypatch.setattr(session_live_state, "persist_running_since", _record)
    status_snapshot = dict(_session_status_cache)
    memory_snapshot = dict(_session_running_since_cache)
    yield calls
    _session_status_cache.clear()
    _session_status_cache.update(status_snapshot)
    _session_running_since_cache.clear()
    _session_running_since_cache.update(memory_snapshot)


def test_note_running_edge_stamps_on_idle_to_running(
    running_since_calls: list[tuple[str, int, bool]],
) -> None:
    """An idle → running edge stamps now into memory and enqueues the label.

    The memory write is synchronous so a snapshot can never pair the new
    running status with an older (or missing) stamp — the race that made a
    label-only design wrong. The label write is the restart fallback and is
    enqueued with ``previous_known=True`` (no row read).
    """
    before = int(time.time())
    _note_running_edge("conv_a", "idle", "running")
    after = int(time.time())

    (session_id, started_at, previous_known) = running_since_calls[0]
    assert (session_id, previous_known) == ("conv_a", True)
    assert before <= started_at <= after
    assert _session_running_since_cache.get("conv_a") == started_at


def test_note_running_edge_unknown_previous_defers_to_worker(
    running_since_calls: list[tuple[str, int, bool]],
) -> None:
    """A ``None`` previous (restart / probe) enqueues with the row check.

    The worker must decide whether the turn carried over, so the edge does
    not write memory synchronously; it refreshes the cache from
    ``on_resolved`` when the worker resolves.
    """
    _note_running_edge("conv_a", None, "running")

    (session_id, started_at, previous_known) = running_since_calls[0]
    assert (session_id, previous_known) == ("conv_a", False)
    assert _session_running_since_cache.get("conv_a") == started_at


@pytest.mark.parametrize(
    "previous_status,status",
    [
        ("running", "running"),
        ("running", "waiting"),
        ("waiting", "running"),
        ("waiting", "waiting"),
    ],
)
def test_note_running_edge_does_not_restart_mid_turn(
    running_since_calls: list[tuple[str, int, bool]],
    previous_status: str,
    status: str,
) -> None:
    """Edges already inside running/waiting never restart the clock.

    A relay that republishes mid-turn (or a waiting → running wake) must not
    reset ``running_since``, or every duration poll would under-report the
    turn.
    """
    _note_running_edge("conv_a", previous_status, status)
    assert running_since_calls == []


def test_note_running_edge_idle_and_failed_pop_memory(
    running_since_calls: list[tuple[str, int, bool]],
) -> None:
    """Terminal/resting edges drop the stamp so a later read can't serve it."""
    _session_running_since_cache["conv_a"] = 123
    _note_running_edge("conv_a", "running", "idle")
    assert _session_running_since_cache.get("conv_a") is None

    _session_running_since_cache["conv_b"] = 456
    _note_running_edge("conv_b", "running", "failed")
    assert _session_running_since_cache.get("conv_b") is None
    assert running_since_calls == []


def test_note_running_edge_other_statuses_are_noops(
    running_since_calls: list[tuple[str, int, bool]],
) -> None:
    """A status outside running/waiting/idle/failed changes nothing.

    ``activity_unverified`` is written into the status cache directly and
    must not open a running period.
    """
    _note_running_edge("conv_a", None, "activity_unverified")
    assert running_since_calls == []
    assert _session_running_since_cache.get("conv_a") is None


def test_publish_status_stamps_only_the_idle_to_running_edge(
    running_since_calls: list[tuple[str, int, bool]],
) -> None:
    """The chokepoint path stamps once per running period, not per publish.

    idle → running stamps; waiting → running and running → running do not;
    the terminal idle pops. If ``_publish_status`` lost the hook, every
    session would report ``running_since`` null until a restart-free turn
    began; if the hook stamped unconditionally, durations would reset on
    every republish.
    """
    _publish_status("conv_a", "idle")
    assert running_since_calls == []

    _publish_status("conv_a", "running")
    assert len(running_since_calls) == 1
    assert _session_running_since_cache.get("conv_a") is not None

    _publish_status("conv_a", "waiting")
    _publish_status("conv_a", "running")
    assert len(running_since_calls) == 1

    _publish_status("conv_a", "idle")
    assert _session_running_since_cache.get("conv_a") is None


@pytest.mark.asyncio
async def test_probe_running_on_cache_miss_stamps(
    running_since_calls: list[tuple[str, int, bool]],
) -> None:
    """The runner-status probe is the second writer that can open a period.

    A freshly bound session has no cache entry, so a snapshot probes the
    runner; a ``running`` answer must stamp via ``_note_running_edge`` (with
    previous unknown, so the worker's row check applies) — otherwise a
    session moved into running by the probe alone would never report a
    start.
    """
    sid = "conv_probe"
    _session_status_cache.pop(sid, None)
    common._runner_status_probe_backoff.pop(sid, None)
    common._runner_status_probe_inflight.pop(sid, None)

    async def _runner_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "running"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_runner_handler),
        base_url="http://runner",
    ) as runner_client:
        raw = await orchestration._probe_runner_live_status(runner_client, sid, "runner_1")

    assert raw == "running"
    assert _session_status_cache.get(sid) == "running"
    (session_id, started_at, previous_known) = running_since_calls[0]
    assert (session_id, previous_known) == (sid, False)
    assert _session_running_since_cache.get(sid) == started_at
    _session_status_cache.pop(sid, None)


@pytest_asyncio.fixture()
async def seeded(db_uri: str) -> str:
    """One session with a visible message, for the snapshot-field tests."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="status-agent", bundle_location="test:///bundle")
    conv = conv_store.create_conversation(agent_id=agent_id)
    conv_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "peek at this session"}],
                ),
            )
        ],
    )
    return conv.id


async def test_get_session_preview_only_when_requested(
    client: httpx.AsyncClient,
    seeded: str,
) -> None:
    """``last_message_preview`` stays null unless ``include_preview=true``.

    The snapshot must not silently add an items read (and excerpt) to every
    GET; orchestrating callers opt in.
    """
    sid = seeded
    try:
        resp = await client.get(f"/v1/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["last_message_preview"] is None

        resp = await client.get(f"/v1/sessions/{sid}", params={"include_preview": "true"})
        assert resp.status_code == 200
        assert resp.json()["last_message_preview"] == "peek at this session"
    finally:
        _session_status_cache.pop(sid, None)
        _session_running_since_cache.pop(sid, None)


async def test_get_session_running_since_memory_wins_over_stale_label(
    client: httpx.AsyncClient,
    db_uri: str,
    seeded: str,
) -> None:
    """``running_since`` is the label while running, memory first, null idle.

    Turn B's in-memory stamp must win over turn A's still-persisted label:
    the label write is async, so serving the label first would pair the new
    running status with the previous turn's start. Idle never reports a
    stamp (a stale memory or label entry is not allowed through).
    """
    sid = seeded
    conv_store = SqlAlchemyConversationStore(db_uri)
    try:
        conv_store.set_labels(sid, {_RUNNING_SINCE_LABEL_KEY: "1700000000"})
        _session_status_cache[sid] = "running"

        # No memory entry yet (restart / another replica): the label serves.
        resp = await client.get(f"/v1/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
        assert resp.json()["running_since"] == 1700000000

        # Memory beats the stale label.
        _session_running_since_cache[sid] = 1800000000
        resp = await client.get(f"/v1/sessions/{sid}")
        assert resp.json()["running_since"] == 1800000000

        # Not running: neither source is reported.
        _session_status_cache.pop(sid, None)
        resp = await client.get(f"/v1/sessions/{sid}")
        assert resp.json()["status"] == "idle"
        assert resp.json()["running_since"] is None
    finally:
        _session_status_cache.pop(sid, None)
        _session_running_since_cache.pop(sid, None)
