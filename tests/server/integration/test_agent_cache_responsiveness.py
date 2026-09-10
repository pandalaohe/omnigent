"""Event-loop coverage for Server routes that mutate the agent cache."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import httpx
import pytest

from omnigent.runtime import get_conversation_store
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.routes._sessions.orchestration import _native_pane_harness
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio


async def test_update_agent_cache_waits_do_not_block_event_loop(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contended replacement and response loading both yield to the event loop."""
    agent = await create_test_agent(
        client,
        name="cache-responsive-agent",
        description="original",
    )
    replace_started = threading.Event()
    release_replace = threading.Event()
    load_started = threading.Event()
    release_load = threading.Event()
    original_replace = AgentCache.replace
    original_load = AgentCache.load

    def blocked_replace(self: AgentCache, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        replace_started.set()
        assert release_replace.wait(timeout=1), "event loop did not release cache replacement"
        return original_replace(self, *args, **kwargs)

    def blocked_load(self: AgentCache, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        load_started.set()
        assert release_load.wait(timeout=1), "event loop did not release response cache load"
        return original_load(self, *args, **kwargs)

    monkeypatch.setattr(AgentCache, "replace", blocked_replace)
    monkeypatch.setattr(AgentCache, "load", blocked_load)
    updated_bundle = build_agent_bundle(
        name="cache-responsive-agent",
        description="updated",
    )
    request = asyncio.create_task(
        client.put(
            f"/v1/sessions/{agent['_session_id']}/agent",
            files={"bundle": ("agent.tar.gz", updated_bundle, "application/gzip")},
        )
    )

    try:
        assert await asyncio.to_thread(replace_started.wait, 1)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
        assert not request.done()
    finally:
        release_replace.set()

    try:
        assert await asyncio.to_thread(load_started.wait, 1)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
        assert not request.done()
    finally:
        release_load.set()

    response = await asyncio.wait_for(request, timeout=2)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == agent["id"]


async def test_no_override_native_pane_resolution_does_not_block_event_loop(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native-pane routing resolves its bound harness without blocking."""
    agent = await create_test_agent(
        client,
        name="harness-responsive-agent",
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
    )
    conversation_store = get_conversation_store()
    conv = await asyncio.to_thread(
        conversation_store.get_conversation,
        agent["_session_id"],
    )
    assert conv is not None
    assert conv.harness_override is None

    load_started = threading.Event()
    release_load = threading.Event()
    original_load = AgentCache.load

    def blocked_load(self: AgentCache, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        load_started.set()
        assert release_load.wait(timeout=1), "event loop did not release harness resolution"
        return original_load(self, *args, **kwargs)

    monkeypatch.setattr(AgentCache, "load", blocked_load)
    resolution = asyncio.create_task(_native_pane_harness(conv))

    try:
        assert await asyncio.to_thread(load_started.wait, 1)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
        assert not resolution.done()
    finally:
        release_load.set()

    assert await asyncio.wait_for(resolution, timeout=2) == "codex-native"


async def test_patch_model_override_non_native_check_does_not_block_event_loop(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PATCH route's no-override SDK classification yields during cache load."""
    agent = await create_test_agent(client, name="sdk-classification-responsive-agent")
    load_started = threading.Event()
    release_load = threading.Event()
    original_load = AgentCache.load
    block_next_load = True

    def blocked_load(self: AgentCache, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal block_next_load
        if block_next_load:
            block_next_load = False
            load_started.set()
            assert release_load.wait(timeout=1), "event loop did not release SDK classification"
        return original_load(self, *args, **kwargs)

    monkeypatch.setattr(AgentCache, "load", blocked_load)
    request = asyncio.create_task(
        client.patch(
            f"/v1/sessions/{agent['_session_id']}",
            json={"model_override": "databricks-gpt-5-4"},
        )
    )

    try:
        assert await asyncio.to_thread(load_started.wait, 1)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
        assert not request.done()
    finally:
        release_load.set()

    response = await asyncio.wait_for(request, timeout=2)
    assert response.status_code == 200, response.text
    assert response.json()["model_override"] == "databricks-gpt-5-4"
