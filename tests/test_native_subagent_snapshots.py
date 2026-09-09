"""Native inventory is delivery/liveness evidence, never a task outcome."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from omnigent.native_subagent_snapshot import (
    NativeSubagentSnapshotPublisher,
    parse_native_subagent_snapshot,
)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", True),
        ("sequence", 0),
        ("complete", "yes"),
        ("children", [{"session_id": "x", "status": "fake"}]),
        ("children", [{"session_id": "x", "status": "running"}] * 2),
    ],
)
def test_invalid_inventory_is_rejected(field: str, value: object) -> None:
    payload = {"generation": 1, "sequence": 1, "children": []}
    payload[field] = value
    with pytest.raises(ValueError):
        parse_native_subagent_snapshot(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("final", [{}, {"child": "completed"}])
async def test_final_snapshot_retries_without_another_event(final: dict[str, str]) -> None:
    posts: list[dict] = []
    acknowledged = asyncio.Event()

    def transport(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        if len(posts) == 1:
            return httpx.Response(503)
        acknowledged.set()
        return httpx.Response(202)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(transport)
    ) as client:
        async with NativeSubagentSnapshotPublisher(client, retry_s=0.005) as publisher:
            if not final:
                publisher.update("parent", {"child": "running"})
            publisher.update("parent", final)
            await asyncio.wait_for(acknowledged.wait(), 1)
            await asyncio.sleep(0.015)
    assert len(posts) == 2
    assert posts[0]["data"]["children"] == posts[1]["data"]["children"]
    assert posts[1]["data"]["sequence"] > posts[0]["data"]["sequence"]


@pytest.mark.asyncio
async def test_retirement_retries_and_does_not_retain_old_parent() -> None:
    posts = []
    done = asyncio.Event()

    def transport(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content)["data"])
        if len(posts) == 1:
            return httpx.Response(503)
        done.set()
        return httpx.Response(202)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(transport)
    ) as client:
        async with NativeSubagentSnapshotPublisher(client, retry_s=0.005) as publisher:
            publisher.update("old", {}, retired=True)
            await asyncio.wait_for(done.wait(), 1)
            await asyncio.sleep(0)
            assert "old" not in publisher._inventories
    assert all(post["retired"] for post in posts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "children,retired",
    [
        ({}, False),
        ({"child": "completed"}, False),
        ({}, True),
        ({"child": "completed", "other": "running"}, False),
    ],
)
async def test_undelivered_inventory_retries_after_claude_observation_deadline(
    children: dict[str, str],
    retired: bool,
) -> None:
    failed, acknowledged = asyncio.Event(), asyncio.Event()
    recovered = False

    def transport(request: httpx.Request) -> httpx.Response:
        if not recovered:
            failed.set()
            return httpx.Response(503)
        acknowledged.set()
        return httpx.Response(202)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(transport)
    ) as client:
        async with NativeSubagentSnapshotPublisher(
            client,
            retry_s=0.005,
            observation_timeout_s=35.0,
        ) as publisher:
            publisher.update("parent", {"child": "running"})
            publisher.update("parent", children, retired=retired)
            await asyncio.wait_for(failed.wait(), 1)
            # Equivalent to an outage exceeding the real 35-second deadline;
            # the old parent receives no further polls after /clear rotation.
            publisher._inventories["parent"].changed_at -= 36.0
            recovered = True
            await asyncio.wait_for(acknowledged.wait(), 1)


@pytest.mark.asyncio
async def test_stale_cached_active_heartbeat_still_expires() -> None:
    posted = asyncio.Event()
    posts = []

    def transport(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        posted.set()
        return httpx.Response(202)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(transport)
    ) as client:
        async with NativeSubagentSnapshotPublisher(
            client,
            heartbeat_s=0.005,
            observation_timeout_s=35.0,
        ) as publisher:
            publisher.update("parent", {"child": "running"})
            await asyncio.wait_for(posted.wait(), 1)
            publisher._inventories["parent"].changed_at -= 36.0
            await asyncio.sleep(0.05)
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_old_server_disables_optional_snapshots() -> None:
    count = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(400)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(transport)
    ) as client:
        async with NativeSubagentSnapshotPublisher(client, heartbeat_s=0.005) as publisher:
            publisher.update("parent", {"child": "running"})
            await asyncio.sleep(0.02)
            publisher.update("parent", {"child": "completed"})
            await asyncio.sleep(0.01)
    assert count == 1


@pytest.mark.asyncio
async def test_oversized_inventory_is_explicitly_partial() -> None:
    posted = asyncio.Event()
    payload = {}

    def transport(request: httpx.Request) -> httpx.Response:
        payload.update(json.loads(request.content)["data"])
        posted.set()
        return httpx.Response(202)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(transport)
    ) as client:
        async with NativeSubagentSnapshotPublisher(client) as publisher:
            publisher.update("parent", {str(i): "running" for i in range(513)})
            await asyncio.wait_for(posted.wait(), 1)
    snapshot = parse_native_subagent_snapshot(payload)
    assert not snapshot.complete
    assert len(snapshot.children) == 512


def test_claude_snapshot_preserves_unknown_and_explicit_terminal() -> None:
    from omnigent.claude_native_forwarder import (
        SubagentEntry,
        SubagentForwardState,
        _native_subagent_snapshot,
    )

    state = SubagentForwardState(
        subagents={
            "a": SubagentEntry(
                subagent_id="a",
                child_conversation_id="a",
                last_status="running",
                activity_unverified=True,
            ),
            "b": SubagentEntry(
                subagent_id="b",
                child_conversation_id="b",
                last_status="running",
                terminal_status="stopped",
            ),
        }
    )
    assert _native_subagent_snapshot(state) == {"a": "activity_unverified", "b": "stopped"}


def test_codex_rotation_retains_late_event_routing_without_reparenting_inventory() -> None:
    from omnigent.codex_native_forwarder import (
        _codex_native_subagent_snapshot,
        _CodexForwarderState,
    )

    state = _CodexForwarderState(parent_session_id="old")
    state.note_child_thread("t1", "c1")
    state.note_parent_rotation("new")
    state.note_child_thread("t1", "c1")
    state.note_child_thread("t2", "c2")
    assert state.session_for_child_thread("t1") == "c1"
    assert _codex_native_subagent_snapshot(state) == {"c2": "running"}
