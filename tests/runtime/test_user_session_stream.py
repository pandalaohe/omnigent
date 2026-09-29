"""Unit tests for :mod:`omnigent.runtime.user_session_stream`.

The per-user discovery fan-out is workspace-namespaced: the same user key can
belong to several workspaces on one pod, so a ``session_added`` (or hosts /
projects changed) event published in one tenant's workspace must not reach that
user's stream in another's. Regression for OMNI-7361.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.runtime import user_session_stream


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    user_session_stream._subscribers.clear()
    yield
    user_session_stream._subscribers.clear()


async def _collect_one(user_key: str) -> dict:
    gen = user_session_stream.subscribe(user_key)
    try:
        async for event in gen:
            return event
        return {}
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_discovery_events_isolated_across_workspaces() -> None:
    """A publish for a user key in one workspace never reaches another's."""
    user = "alice@example.com"
    with workspace_scope(1):
        task = asyncio.create_task(_collect_one(user))
        await asyncio.sleep(0)  # register the subscriber under workspace 1
    with workspace_scope(2):
        # Same user key, different workspace: this publish reaches no one here.
        user_session_stream.publish(user, {"type": "session_added", "session_id": "ws2"})
    with workspace_scope(1):
        user_session_stream.publish(user, {"type": "session_added", "session_id": "ws1"})
    event = await asyncio.wait_for(task, timeout=2.0)
    assert event == {"type": "session_added", "session_id": "ws1"}


@pytest.mark.asyncio
async def test_publish_all_reaches_every_workspace_subscriber() -> None:
    """``publish_all`` fans out to every user in the current workspace only."""
    with workspace_scope(1):
        alice_task = asyncio.create_task(_collect_one("alice@example.com"))
        bob_task = asyncio.create_task(_collect_one("bob@example.com"))
        await asyncio.sleep(0)
    with workspace_scope(2):
        carol_task = asyncio.create_task(_collect_one("carol@example.com"))
        await asyncio.sleep(0)

    with workspace_scope(1):
        user_session_stream.publish_all({"type": "system_status_changed"})

    expected = {"type": "system_status_changed"}
    assert await asyncio.wait_for(alice_task, timeout=2.0) == expected
    assert await asyncio.wait_for(bob_task, timeout=2.0) == expected
    await asyncio.sleep(0.05)
    assert not carol_task.done(), "another workspace's subscriber must not receive it"
    carol_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await carol_task
