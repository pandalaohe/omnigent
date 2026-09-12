"""Fork-local stop_session settle coverage for a gone runner.

Regression tests for the rescue-vs-backstop split in
``POST /v1/sessions/{id}/events`` (``stop_session``): the rescue must settle
the confirmed-gone shape the backstop's conditional store UPDATE cannot
(cache "running" but the persisted row already idle), while never erasing a
persisted terminal failure.
"""

from __future__ import annotations

import httpx
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.server.routes._sessions.helpers import _session_status_cache
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def _seed_session(db_uri: str, *, live_status: str) -> str:
    """Seed one session with a bound stale runner and the given live status."""
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
    # Leave runner_last_seen unset so the runner reads confirmed-gone.
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
    session_id = _seed_session(db_uri, live_status="idle")
    _session_status_cache[session_id] = "running"

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "stop_session", "data": {}},
    )
    assert 200 <= resp.status_code < 300
    assert _session_status_cache.get(session_id) == "idle"


async def test_stop_leaves_failed_row_with_stale_runner(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A persisted failure is terminal: stop must not settle it to idle.

    The sticky guard in _publish_status only covers the cache, so the rescue
    must not publish idle over a failed row (which would erase it via
    persist_live_status).
    """
    session_id = _seed_session(db_uri, live_status="failed")
    _session_status_cache[session_id] = "running"

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "stop_session", "data": {}},
    )
    assert 200 <= resp.status_code < 300
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conv is not None
    assert conv.live_status == "failed"
    assert _session_status_cache.get(session_id) != "idle"
