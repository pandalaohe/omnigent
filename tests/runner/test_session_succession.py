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
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app, subagent_work
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
        assert len(server_client.posts) == 1, "a second release woke the successor again"


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


@pytest.mark.asyncio
async def test_succession_cancels_and_lists_old_timer(
    _clean_succession_state: None,
) -> None:
    """A timer on the retired session is cancelled and reported as dropped."""
    runner_app._session_inboxes_ref[NEW_SESSION_ID] = asyncio.Queue()
    timer_task = asyncio.create_task(asyncio.sleep(3600), name="timer-timer_a1b2")
    runner_app.register_timer(OLD_SESSION_ID, "timer_a1b2", timer_task)
    app, _server_client = _build_runner()
    async with _runner_client(app) as client:
        resp = await _post_succession(client, moved_ids=[])

    assert resp.status_code == 200
    assert resp.json()["dropped"] == [
        {"kind": "timer", "id": "timer_a1b2", "label": "timer-timer_a1b2"}
    ]
    await asyncio.sleep(0)
    assert timer_task.cancelled()
    assert runner_app._session_timers.get(OLD_SESSION_ID) in (None, {})


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
    assert second.json() == {"status": "rekeyed", "dropped": []}
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
