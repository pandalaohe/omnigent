"""Fork-local stop_session settle coverage for a gone runner.

Regression tests for the rescue-vs-backstop split in
``POST /v1/sessions/{id}/events`` (``stop_session``): the rescue must settle
every confirmed-gone shape the backstop's conditional store UPDATE cannot
(cache "running" but the persisted row already idle), while still deferring
the exact settleable shape to the backstop and leaving a fresh runner alone.
"""

from __future__ import annotations

import time

import httpx
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.server.routes._sessions.helpers import _session_status_cache
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def _seed_session(
    db_uri: str, *, live_status: str, runner_fresh: bool
) -> str:
    """Seed one session with a bound runner and the given live status."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(
        agent_id,
        name=f"stop-settle-agent-{agent_id}",
        bundle_location="test:///bundle",
    )
    conv = conv_store.create_conversation(agent_id=agent_id)
    runner_id = f"runner_{conv.id}"
    assert conv_store.set_runner_id(conv.id, runner_id)
    conv_store.set_session_live_status(conv.id, live_status)
    if runner_fresh:
        conv_store.touch_runner_liveness([runner_id], int(time.time()))
    _session_status_cache.pop(conv.id, None)
    return conv.id


@pytest_asyncio.fixture(autouse=True)
def _isolate_status_cache() -> None:
    """Keep the module-level relay status cache from leaking across tests."""
    snapshot = dict(_session_status_cache)
    yield
    _session_status_cache.clear()
    _session_status_cache.update(snapshot)


async def test_stop_settles_stale_cache_when_row_already_idle(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Cache "running" over an already-idle row must not strand on "running".

    The backstop's conditional UPDATE matches zero rows here, so the rescue
    must settle it directly instead of deferring.
    """
    session_id = _seed_session(db_uri, live_status="idle", runner_fresh=False)
    _session_status_cache[session_id] = "running"

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "stop_session", "data": {}},
    )
    assert 200 <= resp.status_code < 300
    assert _session_status_cache.get(session_id) == "idle"


async def test_stop_settles_running_row_with_stale_runner(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The deferred (backstop-settleable) shape still settles end to end."""
    session_id = _seed_session(db_uri, live_status="running", runner_fresh=False)

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "stop_session", "data": {}},
    )
    assert 200 <= resp.status_code < 300
    assert _session_status_cache.get(session_id) == "idle"


async def test_stop_leaves_running_row_with_fresh_runner(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A stop that can't reach a still-fresh runner must not force idle."""
    session_id = _seed_session(db_uri, live_status="running", runner_fresh=True)

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "stop_session", "data": {}},
    )
    assert 200 <= resp.status_code < 300
    assert _session_status_cache.get(session_id) != "idle"
