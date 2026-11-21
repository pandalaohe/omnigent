"""Runner succession: re-key a retired session's delivery state onto its successor.

A native ``/clear`` rotates a top-level "mother" session: the server moves her
live children to a freshly created successor and then calls the old session's
runner to move the state that only lives in the runner process — sub-agent work
entries, the session's queued inbox, held wakes and archive lineage. Until the
server posts the successor's opening message, child results must be held
instead of woken; ``/succession/release`` drains them.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.db.utils import generate_task_id
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app, subagent_work, tool_dispatch
from omnigent.server import session_succession
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient

OLD_SESSION_ID = "conv_old_mother"
NEW_SESSION_ID = "conv_new_mother"
CHILD_SESSION_ID = "conv_child_reviewer"
OTHER_CHILD_ID = "conv_child_writer"


class _RecordingServerClient(NullServerClient):
    """``NullServerClient`` that records every POST it serves."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> Any:
        self.posts.append((url, kwargs))
        return self._Response()


@pytest.fixture
def _clean_succession_state() -> Iterator[None]:
    """Snapshot and restore the process-wide delivery-state maps."""
    saved = (
        dict(runner_app._subagent_work_by_child),
        {
            parent: set(children)
            for parent, children in runner_app._subagent_work_by_parent.items()
        },
        dict(subagent_work._subagent_retained_state_parents),
        dict(subagent_work._subagent_work_origins),
        dict(subagent_work._drained_delivered_subagent_results),
        dict(runner_app._child_session_parents),
        dict(runner_app._session_inboxes_ref),
        dict(subagent_work._succeeded_parents),
        {new_id: list(items) for new_id, items in subagent_work._held_successions.items()},
        {session: dict(timers) for session, timers in runner_app._session_timers.items()},
        {parent: list(copies) for parent, copies in subagent_work._peer_copies.items()},
        dict(subagent_work._peer_copy_dropped),
        {parent: dict(seen) for parent, seen in subagent_work._peer_copy_seen.items()},
    )
    runner_app._subagent_work_by_child.clear()
    runner_app._subagent_work_by_parent.clear()
    subagent_work._subagent_retained_state_parents.clear()
    subagent_work._subagent_work_origins.clear()
    subagent_work._drained_delivered_subagent_results.clear()
    runner_app._child_session_parents.clear()
    runner_app._session_inboxes_ref.clear()
    subagent_work._succeeded_parents.clear()
    subagent_work._held_successions.clear()
    runner_app._session_timers.clear()
    subagent_work._peer_copies.clear()
    subagent_work._peer_copy_dropped.clear()
    subagent_work._peer_copy_seen.clear()
    try:
        yield
    finally:
        runner_app._subagent_work_by_child.clear()
        runner_app._subagent_work_by_child.update(saved[0])
        runner_app._subagent_work_by_parent.clear()
        runner_app._subagent_work_by_parent.update(saved[1])
        subagent_work._subagent_retained_state_parents.clear()
        subagent_work._subagent_retained_state_parents.update(saved[2])
        subagent_work._subagent_work_origins.clear()
        subagent_work._subagent_work_origins.update(saved[3])
        subagent_work._drained_delivered_subagent_results.clear()
        subagent_work._drained_delivered_subagent_results.update(saved[4])
        runner_app._child_session_parents.clear()
        runner_app._child_session_parents.update(saved[5])
        runner_app._session_inboxes_ref.clear()
        runner_app._session_inboxes_ref.update(saved[6])
        subagent_work._succeeded_parents.clear()
        subagent_work._succeeded_parents.update(saved[7])
        subagent_work._held_successions.clear()
        subagent_work._held_successions.update(saved[8])
        runner_app._session_timers.clear()
        runner_app._session_timers.update(saved[9])
        subagent_work._peer_copies.clear()
        subagent_work._peer_copies.update(saved[10])
        subagent_work._peer_copy_dropped.clear()
        subagent_work._peer_copy_dropped.update(saved[11])
        subagent_work._peer_copy_seen.clear()
        subagent_work._peer_copy_seen.update(saved[12])


def _build_runner() -> tuple[FastAPI, _RecordingServerClient]:
    """Build a runner app over the process-wide registries with a recording server."""
    process_manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    server_client = _RecordingServerClient()
    app = create_runner_app(
        process_manager=process_manager,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    return app, server_client


async def _post_succession(
    client: Any,
    *,
    old_id: str = OLD_SESSION_ID,
    target_id: str = NEW_SESSION_ID,
    moved_ids: list[str],
    archive_states: dict[str, list[dict[str, Any]]] | None = None,
) -> Any:
    """POST one succession call and return the response."""
    return await client.post(
        f"/v1/sessions/{old_id}/succession",
        json={
            "target_session_id": target_id,
            "moved_ids": moved_ids,
            "archive_states": {} if archive_states is None else archive_states,
        },
    )


@pytest.mark.asyncio
async def test_succession_409_when_target_not_ready_changes_nothing(
    _clean_succession_state: None,
) -> None:
    """The successor's runner must hold its inbox before any state moves."""
    runner_app._session_inboxes_ref[OLD_SESSION_ID] = asyncio.Queue()
    subagent_work.register_subagent_work(
        parent_session_id=OLD_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review",
    )
    app, _server_client = _build_runner()
    async with _runner_client(app) as client:
        resp = await _post_succession(client, moved_ids=[CHILD_SESSION_ID])

    assert resp.status_code == 409
    assert resp.json() == {"error": "target_not_ready"}
    assert [entry.child_session_id for entry in runner_app.list_subagent_work(OLD_SESSION_ID)] == [
        CHILD_SESSION_ID
    ]
    assert runner_app.list_subagent_work(NEW_SESSION_ID) == []
    assert subagent_work._succeeded_parents == {}
    assert OLD_SESSION_ID in runner_app._session_inboxes_ref
    assert NEW_SESSION_ID not in subagent_work._held_successions


@pytest.mark.asyncio
async def test_succession_rekeys_children_onto_new_parent(
    _clean_succession_state: None,
) -> None:
    """Work entries for the moved children follow the successor."""
    runner_app._session_inboxes_ref[OLD_SESSION_ID] = asyncio.Queue()
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    subagent_work.register_subagent_work(
        parent_session_id=OLD_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review",
    )
    subagent_work.register_subagent_work(
        parent_session_id=OLD_SESSION_ID,
        child_session_id=OTHER_CHILD_ID,
        agent="writer",
        title="draft",
    )
    drained_id = "conv_child_drained"
    drained = subagent_work.register_subagent_work(
        parent_session_id=OLD_SESSION_ID,
        child_session_id=drained_id,
        agent="drainer",
        title="drained",
    )
    drained.delivered = True
    subagent_work.unregister_subagent_work(drained_id, remember_drained_delivery=True)
    app, _server_client = _build_runner()
    async with _runner_client(app) as client:
        resp = await _post_succession(
            client, moved_ids=[CHILD_SESSION_ID, OTHER_CHILD_ID, drained_id]
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "rekeyed"
    assert {entry.child_session_id for entry in runner_app.list_subagent_work(NEW_SESSION_ID)} == {
        CHILD_SESSION_ID,
        OTHER_CHILD_ID,
    }
    assert runner_app.list_subagent_work(OLD_SESSION_ID) == []
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is not None
    assert runner_app.get_subagent_work(CHILD_SESSION_ID).parent_session_id == NEW_SESSION_ID
    assert subagent_work._subagent_retained_state_parents[drained_id] == NEW_SESSION_ID


@pytest.mark.asyncio
async def test_moved_grandchild_work_stays_parented_by_its_own_parent(
    _clean_succession_state: None,
) -> None:
    """A grandchild in the moved set keeps its parent; only old's entries re-key."""
    runner_app._session_inboxes_ref[OLD_SESSION_ID] = asyncio.Queue()
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    child_id = "conv_child_A"
    grandchild_id = "conv_grandchild_A1"
    subagent_work.register_subagent_work(
        parent_session_id=OLD_SESSION_ID,
        child_session_id=child_id,
        agent="reviewer",
        title="A",
    )
    subagent_work.register_subagent_work(
        parent_session_id=child_id,
        child_session_id=grandchild_id,
        agent="reviewer",
        title="A1",
    )
    app, _server_client = _build_runner()
    async with _runner_client(app) as client:
        resp = await _post_succession(client, moved_ids=[child_id, grandchild_id])

    assert resp.status_code == 200
    child_entry = runner_app.get_subagent_work(child_id)
    grandchild_entry = runner_app.get_subagent_work(grandchild_id)
    assert child_entry is not None and child_entry.parent_session_id == NEW_SESSION_ID
    assert grandchild_entry is not None and grandchild_entry.parent_session_id == child_id
    assert [entry.child_session_id for entry in runner_app.list_subagent_work(child_id)] == [
        grandchild_id
    ]


@pytest.mark.asyncio
async def test_late_registration_after_succession_lands_under_new_parent(
    _clean_succession_state: None,
) -> None:
    """A dispatch registered with the stale parent id follows the map."""
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    app, _server_client = _build_runner()
    async with _runner_client(app) as client:
        resp = await _post_succession(client, moved_ids=[])
    assert resp.status_code == 200

    subagent_work.register_subagent_work(
        parent_session_id=OLD_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review",
    )

    assert [entry.child_session_id for entry in runner_app.list_subagent_work(NEW_SESSION_ID)] == [
        CHILD_SESSION_ID
    ]
    assert runner_app.list_subagent_work(OLD_SESSION_ID) == []
    assert runner_app.get_subagent_work(CHILD_SESSION_ID).parent_session_id == NEW_SESSION_ID


@pytest.mark.asyncio
async def test_held_completion_is_delivered_once_by_release(
    _clean_succession_state: None,
) -> None:
    """A completion after succession waits for release and wakes the successor once."""
    runner_app._session_inboxes_ref[OLD_SESSION_ID] = asyncio.Queue()
    new_inbox = asyncio.Queue()
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = new_inbox
    subagent_work.register_subagent_work(
        parent_session_id=OLD_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review",
    )
    app, server_client = _build_runner()
    async with _runner_client(app) as client:
        resp = await _post_succession(client, moved_ids=[CHILD_SESSION_ID])
        assert resp.status_code == 200

        completion = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "output": "review complete: LGTM"},
            },
        )
        assert completion.status_code == 204

        await asyncio.sleep(0.05)
        assert new_inbox.empty(), "a held completion reached the live inbox"
        assert len(subagent_work._held_successions[NEW_SESSION_ID]) == 1
        assert server_client.posts == [], "a held completion woke the successor"

        release = await client.post(f"/v1/sessions/{NEW_SESSION_ID}/succession/release")
        assert release.status_code == 200
        assert release.json() == {"status": "released", "delivered": 1}

        await asyncio.sleep(0.05)
        item = new_inbox.get_nowait()
        assert item["type"] == "sub_agent"
        assert item["conversation_id"] == CHILD_SESSION_ID
        wake_posts = [
            url
            for url, _kwargs in server_client.posts
            if url.endswith(f"/{NEW_SESSION_ID}/events")
        ]
        assert len(wake_posts) == 1

        repeat = await client.post(f"/v1/sessions/{NEW_SESSION_ID}/succession/release")
        assert repeat.json() == {"status": "released", "delivered": 0}
        await asyncio.sleep(0.05)
        wake_posts = [
            url
            for url, _kwargs in server_client.posts
            if url.endswith(f"/{NEW_SESSION_ID}/events")
        ]
        assert len(wake_posts) == 1, "a second release woke the successor again"


@pytest.mark.asyncio
async def test_engine_releases_pre_rotation_work_with_offline_child_runner(
    _clean_succession_state: None,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    old = store.create_conversation(title="old", runner_id="parent-runner").id
    new = store.create_conversation(title="new", runner_id="parent-runner").id
    child = store.create_conversation(
        parent_conversation_id=old, runner_id="offline-child-runner", title="review"
    ).id
    receipt_id = generate_task_id()
    store.reassign_live_children(old, new, receipt_id)
    runner_app._session_inboxes_ref[old] = asyncio.Queue()
    new_inbox = asyncio.Queue()
    runner_app._session_inboxes_ref[new] = new_inbox
    subagent_work.register_subagent_work(
        parent_session_id=old, child_session_id=child, agent="reviewer", title="review"
    )
    app, server = _build_runner()
    real_open = session_succession._phase_open
    async with _runner_client(app) as client:

        async def resolve(_id: str, _router: Any, *, conversation: Any) -> Any:
            return None if conversation.runner_id == "offline-child-runner" else client

        async def complete_after_open(*args: Any, **kwargs: Any) -> Any:
            receipt = await real_open(*args, **kwargs)
            completion = await client.post(
                f"/v1/sessions/{child}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "LGTM"},
                },
            )
            assert completion.status_code == 204
            assert new_inbox.empty()
            assert len(subagent_work._held_successions[new]) == 1
            return receipt

        monkeypatch.setattr(session_succession, "_get_runner_client", resolve)
        monkeypatch.setattr(session_succession, "_phase_open", complete_after_open)
        result = await session_succession.advance_succession(
            receipt_id, conversation_store=store, runner_router=None, peer_message_store=None
        )
    assert result.phase == "done"
    assert result.deferred_runners == [
        {"runner_id": "offline-child-runner", "session_ids": [child]}
    ]
    assert new_inbox.get_nowait()["conversation_id"] == child
    assert len([url for url, _ in server.posts if url.endswith(f"/{new}/events")]) == 1


@pytest.mark.asyncio
async def test_old_inbox_items_move_to_held_then_release_in_order(
    _clean_succession_state: None,
) -> None:
    """Queued items of the retired session drain in order on release."""
    old_inbox = asyncio.Queue()
    old_inbox.put_nowait({"type": "note", "seq": 1})
    old_inbox.put_nowait({"type": "note", "seq": 2})
    runner_app._session_inboxes_ref[OLD_SESSION_ID] = old_inbox
    new_inbox = asyncio.Queue()
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = new_inbox
    app, _server_client = _build_runner()
    async with _runner_client(app) as client:
        resp = await _post_succession(client, moved_ids=[])
        assert resp.status_code == 200
        assert OLD_SESSION_ID not in runner_app._session_inboxes_ref
        assert [item["seq"] for item in subagent_work._held_successions[NEW_SESSION_ID]] == [1, 2]
        assert new_inbox.empty()

        release = await client.post(f"/v1/sessions/{NEW_SESSION_ID}/succession/release")
        assert release.json() == {"status": "released", "delivered": 2}

        drained = []
        while not new_inbox.empty():
            drained.append(new_inbox.get_nowait())
        assert [item["seq"] for item in drained] == [1, 2]
        assert NEW_SESSION_ID not in subagent_work._held_successions


@pytest.mark.asyncio
async def test_lineage_prunes_stale_scope_and_keeps_newer_own_scope(
    _clean_succession_state: None,
) -> None:
    """A moved child keeps its own archive fence and drops the old lineage."""
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    app, _server_client = _build_runner()
    lifecycle = app.state.cli_runtime_lifecycle
    lifecycle.observe_archive_state(
        CHILD_SESSION_ID, scope_id=OLD_SESSION_ID, revision=3, archived=True
    )
    lifecycle.observe_archive_state(
        CHILD_SESSION_ID, scope_id=CHILD_SESSION_ID, revision=5, archived=False
    )
    async with _runner_client(app) as client:
        resp = await _post_succession(
            client,
            moved_ids=[CHILD_SESSION_ID],
            archive_states={
                CHILD_SESSION_ID: [
                    {"scope_id": CHILD_SESSION_ID, "revision": 1, "archived": False},
                    {"scope_id": NEW_SESSION_ID, "revision": 0, "archived": False},
                ]
            },
        )

    assert resp.status_code == 200
    assert lifecycle.archive_revision(CHILD_SESSION_ID, CHILD_SESSION_ID) == 5
    assert OLD_SESSION_ID not in lifecycle.archive_scope_ids(CHILD_SESSION_ID)
    assert NEW_SESSION_ID in lifecycle.archive_scope_ids(CHILD_SESSION_ID)


class _GatedTimerAsyncio:
    """Stands in for ``tool_dispatch.asyncio``: each timer sleep waits for one release."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._releases: asyncio.Queue[None] = asyncio.Queue()

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        await self._releases.get()

    def release(self) -> None:
        self._releases.put_nowait(None)

    async def wait_for_sleeps(self, count: int) -> None:
        for _ in range(1000):
            if len(self.delays) >= count:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"timer sleeps {self.delays} never reached {count}")


class _TimerServer:
    """Server stand-in for timer firings: session snapshots plus recorded event POSTs."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []
        self.statuses: dict[str, int] = {}
        self.archived: set[str] = set()
        self.blocked: dict[str, asyncio.Event] = {}
        self.checking = asyncio.Event()
        self.refused_posts = 0

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            if self.refused_posts:
                self.refused_posts -= 1
                raise httpx.ConnectError("connection refused")
            text = json.loads(request.content)["data"]["content"][0]["text"]
            self.posts.append((request.url.path, text))
            return httpx.Response(202, json={"queued": True})
        session_id = request.url.path.rsplit("/", 1)[-1]
        if session_id in self.blocked:
            self.checking.set()
            await self.blocked.pop(session_id).wait()
        archived = session_id in self.archived
        return httpx.Response(self.statuses.get(session_id, 200), json={"archived": archived})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle), base_url="http://s")


async def _set_timer(server_client: httpx.AsyncClient, **args: Any) -> str:
    output = json.loads(
        await tool_dispatch._execute_timer_set(
            args, server_client=server_client, conversation_id=OLD_SESSION_ID
        )
    )
    return output["timer_id"]


async def _release(client: Any) -> Any:
    return await client.post(f"/v1/sessions/{NEW_SESSION_ID}/succession/release")


async def _cancel_timer(session_id: str, timer_id: str) -> str:
    output = await tool_dispatch._execute_timer_cancel(
        {"timer_id": timer_id}, conversation_id=session_id
    )
    return json.loads(output)["status"]


@pytest.mark.asyncio
async def test_succession_moves_timers_with_their_schedule(
    _clean_succession_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One-shot and repeating timers move to the successor as the same running tasks."""
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    gate = _GatedTimerAsyncio()
    monkeypatch.setattr(tool_dispatch, "asyncio", gate)
    timer_server = _TimerServer()
    app, _server_client = _build_runner()
    async with timer_server.client() as server_client:
        one_shot = await _set_timer(server_client, seconds=3600, note="resume")
        repeating = await _set_timer(server_client, seconds=900, repeat=True, note="check")
        await gate.wait_for_sleeps(2)
        tasks = dict(runner_app._session_timers[OLD_SESSION_ID])
        try:
            async with _runner_client(app) as client:
                resp = await _post_succession(client, moved_ids=[])

            assert resp.status_code == 200
            assert resp.json()["dropped"] == []
            moved = {item["id"]: item for item in resp.json()["moved_timers"]}
            due = {timer_id: moved[timer_id].pop("due_at") for timer_id in moved}
            assert moved[one_shot] == {
                "kind": "timer",
                "id": one_shot,
                "seconds": 3600.0,
                "repeat": False,
                "note": "resume",
            }
            assert moved[repeating] == {
                "kind": "timer",
                "id": repeating,
                "seconds": 900.0,
                "repeat": True,
                "note": "check",
            }
            assert abs(due[one_shot] - (time.time() + 3600)) < 60
            assert runner_app._session_timers.get(OLD_SESSION_ID) in (None, {})
            assert runner_app._session_timers[NEW_SESSION_ID] == tasks
            assert not any(task.done() for task in tasks.values())
            assert await _cancel_timer(OLD_SESSION_ID, one_shot) == "not_found"
            assert await _cancel_timer(NEW_SESSION_ID, one_shot) == "cancelled"
            assert await _cancel_timer(NEW_SESSION_ID, repeating) == "cancelled"
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
    assert timer_server.posts == []
    assert gate.delays == [3600.0, 900.0]
    assert not {one_shot, repeating} & runner_app._timer_schedules.keys()


@pytest.mark.asyncio
async def test_moved_repeat_timer_fires_once_per_due_time_on_the_same_cadence(
    _clean_succession_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the commit the predecessor gets the firing; after it, only the successor."""
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    gate = _GatedTimerAsyncio()
    monkeypatch.setattr(tool_dispatch, "asyncio", gate)
    timer_server = _TimerServer()
    app, _server_client = _build_runner()
    async with timer_server.client() as server_client:
        timer_id = await _set_timer(server_client, seconds=900, repeat=True, note="check")
        task = runner_app._session_timers[OLD_SESSION_ID][timer_id]
        try:
            await gate.wait_for_sleeps(1)
            gate.release()
            await gate.wait_for_sleeps(2)
            async with _runner_client(app) as client:
                assert (await _post_succession(client, moved_ids=[])).status_code == 200
                assert (await _release(client)).status_code == 200
            gate.release()
            await gate.wait_for_sleeps(3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    text = f"[System: timer {timer_id} fired]\nnote: 'check'"
    assert timer_server.posts == [
        (f"/v1/sessions/{OLD_SESSION_ID}/events", text),
        (f"/v1/sessions/{NEW_SESSION_ID}/events", text),
    ]
    assert gate.delays == [900.0, 900.0, 900.0]
    assert runner_app._session_timers.get(NEW_SESSION_ID) in (None, {})
    assert timer_id not in runner_app._timer_schedules


@pytest.mark.asyncio
async def test_in_flight_check_counts_only_for_its_holder_and_waits_for_the_opening(
    _clean_succession_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A check of the archived predecessor never ends a moved timer; B gets it after release."""
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    gate = _GatedTimerAsyncio()
    monkeypatch.setattr(tool_dispatch, "asyncio", gate)
    timer_server = _TimerServer()
    check_answer = asyncio.Event()
    timer_server.blocked[OLD_SESSION_ID] = check_answer
    app, _server_client = _build_runner()
    async with timer_server.client() as server_client:
        timer_id = await _set_timer(server_client, seconds=900, repeat=True, note="check")
        task = runner_app._session_timers[OLD_SESSION_ID][timer_id]
        try:
            await gate.wait_for_sleeps(1)
            gate.release()
            await asyncio.wait_for(timer_server.checking.wait(), timeout=5)
            async with _runner_client(app) as client:
                assert (await _post_succession(client, moved_ids=[])).status_code == 200
                timer_server.archived.add(OLD_SESSION_ID)
                check_answer.set()
                await gate.wait_for_sleeps(2)
                assert timer_server.posts == []
                assert (await _release(client)).status_code == 200
            gate.release()
            await gate.wait_for_sleeps(3)
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    text = f"[System: timer {timer_id} fired]\nnote: 'check'"
    assert timer_server.posts == [(f"/v1/sessions/{NEW_SESSION_ID}/events", text)]
    assert gate.delays == [900.0, tool_dispatch._TIMER_HELD_POLL_S, 900.0]


@pytest.mark.asyncio
async def test_moved_timer_ends_only_once_its_holder_is_gone(
    _clean_succession_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Errors and a single 404 keep delivering; two 404 ticks in a row end a moved timer."""
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    gate = _GatedTimerAsyncio()
    monkeypatch.setattr(tool_dispatch, "asyncio", gate)
    timer_server = _TimerServer()
    app, _server_client = _build_runner()
    async with timer_server.client() as server_client:
        timer_id = await _set_timer(server_client, seconds=900, repeat=True)
        task = runner_app._session_timers[OLD_SESSION_ID][timer_id]
        try:
            await gate.wait_for_sleeps(1)
            async with _runner_client(app) as client:
                assert (await _post_succession(client, moved_ids=[])).status_code == 200
                assert (await _release(client)).status_code == 200
            for tick, status in enumerate((503, 404, 200, 404), start=2):
                timer_server.statuses[NEW_SESSION_ID] = status
                gate.release()
                await gate.wait_for_sleeps(tick)
            gate.release()
            await asyncio.wait_for(task, timeout=5)
        finally:
            task.cancel()

    fired = (f"/v1/sessions/{NEW_SESSION_ID}/events", f"[System: timer {timer_id} fired]")
    assert timer_server.posts == [fired] * 4
    assert runner_app._session_timers.get(NEW_SESSION_ID) in (None, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["archived", "cap"])
async def test_never_released_successor_still_ends_or_gets_the_firing_after_the_cap(
    _clean_succession_state: None,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """A hold that is never released keeps checking the successor and is bounded."""
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    gate = _GatedTimerAsyncio()
    monkeypatch.setattr(tool_dispatch, "asyncio", gate)
    monkeypatch.setattr(tool_dispatch, "_TIMER_HELD_MAX_POLLS", 2)
    timer_server = _TimerServer()
    app, _server_client = _build_runner()
    async with timer_server.client() as server_client:
        timer_id = await _set_timer(server_client, seconds=900, repeat=True)
        task = runner_app._session_timers[OLD_SESSION_ID][timer_id]
        try:
            await gate.wait_for_sleeps(1)
            async with _runner_client(app) as client:
                assert (await _post_succession(client, moved_ids=[])).status_code == 200
            gate.release()
            await gate.wait_for_sleeps(2)
            if outcome == "archived":
                timer_server.archived.add(NEW_SESSION_ID)
                gate.release()
                await asyncio.wait_for(task, timeout=5)
            else:
                gate.release()
                await gate.wait_for_sleeps(3)
                gate.release()
                await gate.wait_for_sleeps(4)
                assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    poll = tool_dispatch._TIMER_HELD_POLL_S
    if outcome == "archived":
        assert timer_server.posts == []
        assert gate.delays == [900.0, poll]
        assert runner_app._session_timers.get(NEW_SESSION_ID) in (None, {})
    else:
        fired = (f"/v1/sessions/{NEW_SESSION_ID}/events", f"[System: timer {timer_id} fired]")
        assert timer_server.posts == [fired]
        assert gate.delays == [900.0, poll, poll, 900.0]


@pytest.mark.asyncio
async def test_connect_error_retry_follows_the_moved_timer_to_its_successor(
    _clean_succession_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry after a refused connection goes to the new holder, after its opening."""
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    gate = _GatedTimerAsyncio()
    monkeypatch.setattr(tool_dispatch, "asyncio", gate)
    timer_server = _TimerServer()
    timer_server.refused_posts = 1
    app, _server_client = _build_runner()
    async with timer_server.client() as server_client:
        timer_id = await _set_timer(server_client, seconds=60, note="resume")
        task = runner_app._session_timers[OLD_SESSION_ID][timer_id]
        try:
            await gate.wait_for_sleeps(1)
            gate.release()
            await gate.wait_for_sleeps(2)
            async with _runner_client(app) as client:
                assert (await _post_succession(client, moved_ids=[])).status_code == 200
                gate.release()
                await gate.wait_for_sleeps(3)
                assert timer_server.posts == []
                assert (await _release(client)).status_code == 200
            gate.release()
            await asyncio.wait_for(task, timeout=5)
        finally:
            task.cancel()

    text = f"[System: timer {timer_id} fired]\nnote: 'resume'"
    assert timer_server.posts == [(f"/v1/sessions/{NEW_SESSION_ID}/events", text)]
    assert gate.delays == [60.0, 2.0, tool_dispatch._TIMER_HELD_POLL_S]


@pytest.mark.asyncio
async def test_runner_without_the_successor_stops_timers_and_lists_them(
    _clean_succession_state: None,
) -> None:
    """A runner not hosting the successor cannot hand it a task: the timer stops, listed."""
    timer_task = asyncio.create_task(asyncio.sleep(3600), name="timer-timer_a1b2")
    runner_app.register_timer(OLD_SESSION_ID, "timer_a1b2", timer_task)
    app, _server_client = _build_runner()
    async with _runner_client(app) as client:
        resp = await _post_succession(client, moved_ids=[])

    assert resp.status_code == 200
    assert resp.json()["moved_timers"] == []
    assert resp.json()["dropped"] == [
        {"kind": "timer", "id": "timer_a1b2", "label": "timer-timer_a1b2"}
    ]
    await asyncio.sleep(0)
    assert timer_task.cancelled()


@pytest.mark.asyncio
async def test_refused_succession_keeps_the_timer_on_the_predecessor(
    _clean_succession_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successor not ready yet (409) moves nothing; the predecessor keeps its firing."""
    runner_app._session_inboxes_ref[OLD_SESSION_ID] = asyncio.Queue()
    gate = _GatedTimerAsyncio()
    monkeypatch.setattr(tool_dispatch, "asyncio", gate)
    timer_server = _TimerServer()
    app, _server_client = _build_runner()
    async with timer_server.client() as server_client:
        timer_id = await _set_timer(server_client, seconds=60, note="resume")
        task = runner_app._session_timers[OLD_SESSION_ID][timer_id]
        try:
            await gate.wait_for_sleeps(1)
            async with _runner_client(app) as client:
                assert (await _post_succession(client, moved_ids=[])).status_code == 409
            assert runner_app._session_timers[OLD_SESSION_ID] == {timer_id: task}
            gate.release()
            await asyncio.wait_for(task, timeout=5)
        finally:
            task.cancel()

    assert [path for path, _text in timer_server.posts] == [
        f"/v1/sessions/{OLD_SESSION_ID}/events"
    ]
    assert runner_app._session_timers.get(OLD_SESSION_ID) in (None, {})
    assert runner_app._session_timers.get(NEW_SESSION_ID) in (None, {})


@pytest.mark.asyncio
async def test_move_leaves_flow_runs_and_finished_timers_behind(
    _clean_succession_state: None,
) -> None:
    """Flow runs share the registry and keep their own cancel-and-drop path."""
    live = asyncio.create_task(asyncio.sleep(3600))
    flow = asyncio.create_task(asyncio.sleep(3600))
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    runner_app.register_timer(OLD_SESSION_ID, "timer_live", live)
    runner_app.register_timer(OLD_SESSION_ID, "flow_run", flow)
    runner_app.register_timer(OLD_SESSION_ID, "timer_done", finished)
    try:
        moved = runner_app.move_session_timers(OLD_SESSION_ID, NEW_SESSION_ID, keep={"flow_run"})
        assert moved == [{"kind": "timer", "id": "timer_live"}]
        assert runner_app._session_timers[NEW_SESSION_ID] == {"timer_live": live}
        assert set(runner_app._session_timers[OLD_SESSION_ID]) == {"flow_run", "timer_done"}
    finally:
        live.cancel()
        flow.cancel()
        await asyncio.gather(live, flow, return_exceptions=True)


@pytest.mark.asyncio
async def test_succession_repeat_is_idempotent(_clean_succession_state: None) -> None:
    """A repeated succession call re-applies nothing and drops nothing."""
    runner_app._session_inboxes_ref[OLD_SESSION_ID] = asyncio.Queue()
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    subagent_work.register_subagent_work(
        parent_session_id=OLD_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review",
    )
    app, _server_client = _build_runner()
    async with _runner_client(app) as client:
        first = await _post_succession(client, moved_ids=[CHILD_SESSION_ID])
        second = await _post_succession(client, moved_ids=[CHILD_SESSION_ID])

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {"status": "rekeyed", "dropped": [], "moved_timers": []}
    assert {entry.child_session_id for entry in runner_app.list_subagent_work(NEW_SESSION_ID)} == {
        CHILD_SESSION_ID
    }
    assert runner_app.list_subagent_work(OLD_SESSION_ID) == []


@pytest.mark.asyncio
async def test_succession_repeat_after_release_does_not_hold_again(
    _clean_succession_state: None,
) -> None:
    """A resumed succession call after release leaves the successor's delivery live."""
    runner_app._session_inboxes_ref[OLD_SESSION_ID] = asyncio.Queue()
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    app, _server_client = _build_runner()
    async with _runner_client(app) as client:
        assert (await _post_succession(client, moved_ids=[CHILD_SESSION_ID])).status_code == 200
        release = await client.post(f"/v1/sessions/{NEW_SESSION_ID}/succession/release")
        assert release.status_code == 200
        repeat = await _post_succession(client, moved_ids=[CHILD_SESSION_ID])

    assert repeat.status_code == 200
    assert NEW_SESSION_ID not in subagent_work._held_successions
