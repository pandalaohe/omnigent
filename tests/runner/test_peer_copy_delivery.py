"""A peer-attributed child turn is a silent copy, not a mother result.

The server annotates a dispatched child's settling ``external_session_status``
edge with ``data.peer_turn`` when that turn answered a peer message from a
session other than the child's mother. On the mother's runner such an edge must
not reach her inbox as a sub-agent result and must not wake her: it is recorded
as a silent copy she reads only when something else makes her drain her inbox.

The mother's own dispatches keep today's delivery path. These tests post the
same ``external_session_status`` edges the native forwarders emit and inspect
the runner's copy buffer, inbox and wake POSTs.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app, subagent_work
from omnigent.runner.native.interrupt import NativeInterruptRunner
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.test_native_subagent_inbox_delivery import (
    CHILD_SESSION_ID,
    DISPATCH_ID,
    PARENT_SESSION_ID,
    _assistant_item,
    _child_snapshot,
    _child_summary,
    _delivery_app,
    _RecoveryServerClient,
    _SnapshotServerClient,
)

_NEW_PARENT_ID = "conv_new_mother"


@pytest.fixture(autouse=True)
def _stub_native_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Status-delivery tests initialize native sessions without launching a pane."""
    monkeypatch.setattr(runner_app, "_launch_native_terminal", AsyncMock(return_value=True))


def _peer_turn(
    *,
    result_item_id: str = "item_peer",
    parent_session_id: str = PARENT_SESSION_ID,
    sender_session_id: str = "conv_hermes_lead",
) -> dict[str, str]:
    """Build the server's ``data.peer_turn`` annotation."""
    return {
        "parent_session_id": parent_session_id,
        "peer_id": "peer_msg_1",
        "result_item_id": result_item_id,
        "ref": "omn054-hermes-facts",
        "sender_session_id": sender_session_id,
        "sender_title": "hermes lead",
        "sender_origin": "agent",
        "excerpt": "please check the facts",
    }


def _peer_app(items: list[dict[str, Any]] | None = None) -> tuple[Any, _PatchRecordingClient]:
    """Build a runner app whose child resolves to a peer-copy recording server."""
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="reviewer")

    server_client = _PatchRecordingClient(
        {
            **_child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
            "workspace": "/opt/work/omnigent",
        },
        items=items,
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    return app, server_client


def _forwarder_confirmed_app() -> tuple[Any, _PatchRecordingClient]:
    """Build a runner whose child harness confirms turn outcomes (claude-native)."""
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="reviewer",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    server_client = _PatchRecordingClient(
        {
            **_child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
            "workspace": "/opt/work/omnigent",
        },
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    return app, server_client


class _PatchRecordingClient(_SnapshotServerClient):
    """Snapshot client that records the label PATCHes the runner sends."""

    def __init__(self, child_body: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(child_body, **kwargs)
        self.patches: list[tuple[str, dict[str, Any]]] = []

    async def patch(self, url: str, **kwargs: Any) -> Any:
        self.patches.append((url, kwargs))
        return self._Response()


class _WindowRecoveryServerClient(_RecoveryServerClient):
    """Recovery client that also serves the item window endpoint by anchor id."""

    async def get(self, url: str, **kwargs: Any) -> Any:
        if url.endswith("/items/window"):
            params = dict(kwargs.get("params") or {})
            self.requests.append((url, params))
            anchor_id = params.get("anchor_id")
            for item in self.child_items:
                if item.get("id") == anchor_id:
                    return self._Resp(
                        {
                            "data": [item],
                            "anchor_id": anchor_id,
                            "has_older": False,
                            "has_newer": False,
                        }
                    )
            return self._Resp({"error": {"code": "stale_cursor"}}, status_code=400)
        return await super().get(url, **kwargs)


async def _post_status(client: Any, data: dict[str, Any]) -> Any:
    return await client.post(
        f"/v1/sessions/{CHILD_SESSION_ID}/events",
        json={"type": "external_session_status", "data": data},
    )


def _wake_posts(server_client: Any, parent_id: str = PARENT_SESSION_ID) -> list[str]:
    return [url for url, _ in server_client.posts if url == f"/v1/sessions/{parent_id}/events"]


@pytest.mark.asyncio
async def test_peer_turn_copy_is_recorded_not_delivered(
    _clean_subagent_registry: None,
) -> None:
    """An annotated settling edge with no mother entry becomes one silent copy.

    The child has no work entry on the mother's runner (the turn was a peer
    request), so nothing may be created, delivered or woken: the copy is the
    only record.
    """
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, server_client = _delivery_app([])
    async with _runner_client(app) as client:
        resp = await _post_status(
            client,
            {"status": "completed", "output": "the answer", "peer_turn": _peer_turn()},
        )
        await asyncio.sleep(0.05)

    assert resp.status_code == 204, resp.text
    copies, dropped = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert dropped == 0
    assert len(copies) == 1
    copy = copies[0]
    assert copy["type"] == "peer_copy"
    assert copy["child_session_id"] == CHILD_SESSION_ID
    assert copy["status"] == "completed"
    assert copy["result_item_id"] == "item_peer"
    assert copy["sender_session_id"] == "conv_hermes_lead"
    assert copy["excerpt"] == "please check the facts"
    assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None, (
        "a peer turn must not create a work entry"
    )
    assert subagent_work._session_inboxes_ref[PARENT_SESSION_ID].empty(), (
        "a peer turn must not reach the mother inbox"
    )
    assert _wake_posts(server_client) == [], "a peer turn must not wake the mother"


@pytest.mark.asyncio
async def test_peer_turn_ambiguous_idle_records_nothing_then_completed_copies(
    _clean_subagent_registry: None,
) -> None:
    """A bare idle the forwarder would confirm records nothing; the completion copies.

    The claude-native forwarder stamps ``turn_completed`` on its real turn end,
    so a bare quiescence idle proves nothing and must leave no copy and no work
    entry. The confirmed completion is the settling edge and becomes the copy.
    """
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, server_client = _forwarder_confirmed_app()
    async with _runner_client(app) as client:
        init = await client.post(
            "/v1/sessions",
            json={"session_id": CHILD_SESSION_ID, "agent_id": "ag_reviewer"},
        )
        assert init.status_code == 201, init.text
        idle = await _post_status(
            client,
            {"status": "idle", "output": "partial", "peer_turn": _peer_turn()},
        )
        assert idle.status_code == 204
        copies, _ = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
        assert copies == [], "an unconfirmed idle must not record a copy"
        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None

        done = await _post_status(
            client,
            {"status": "completed", "output": "the answer", "peer_turn": _peer_turn()},
        )
        assert done.status_code == 204

    copies, _ = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert len(copies) == 1
    assert copies[0]["status"] == "completed"
    assert _wake_posts(server_client) == []


@pytest.mark.asyncio
async def test_annotated_edge_keeps_its_own_outcome_over_cancelled_mother(
    _clean_subagent_registry: None,
) -> None:
    """A peer turn's copy carries the edge's own status and output, not the mother's.

    A mother entry already cancelled and confirmed must not substitute its
    earlier cancelled status/output for the peer turn's own outcome, and the
    copy decision must not touch that entry.
    """
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, server_client = _delivery_app([])
    entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review",
    )
    entry.status = "cancelled"
    entry.cancellation_confirmed = True
    entry.output = "old cancelled output"
    async with _runner_client(app) as client:
        resp = await _post_status(
            client,
            {"status": "idle", "output": "peer answer", "peer_turn": _peer_turn()},
        )
        await asyncio.sleep(0.05)

    assert resp.status_code == 204
    copies, _ = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert len(copies) == 1
    assert copies[0]["status"] == "completed", "the edge's own outcome wins"
    assert copies[0]["output"] == "peer answer"
    assert entry.status == "cancelled"
    assert entry.cancellation_confirmed is True
    assert entry.output == "old cancelled output"
    assert subagent_work._session_inboxes_ref[PARENT_SESSION_ID].empty()
    assert _wake_posts(server_client) == []


@pytest.mark.asyncio
async def test_annotated_edge_does_not_confirm_mother_cancellation(
    _clean_subagent_registry: None,
) -> None:
    """A cancelled ``turn_outcome`` on a peer edge must not confirm the mother's entry."""
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, _server_client = _delivery_app([])
    entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review",
    )
    entry.recovered = True
    entry.status = "running"
    async with _runner_client(app) as client:
        resp = await _post_status(
            client,
            {
                "status": "idle",
                "turn_outcome": "cancelled",
                "output": "peer cancelled",
                "peer_turn": _peer_turn(),
            },
        )
        await asyncio.sleep(0.05)

    assert resp.status_code == 204
    copies, _ = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert len(copies) == 1
    assert copies[0]["status"] == "cancelled"
    assert entry.cancellation_confirmed is False, (
        "the peer edge must not confirm the mother's entry cancellation"
    )


@pytest.mark.asyncio
async def test_annotated_edge_does_not_resolve_pending_mother_interrupt(
    _clean_subagent_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending mother interrupt survives an annotated peer edge.

    ``resolve_pending_interrupt`` would consume a pending record bound to the
    mother's dispatch; a peer turn is not that dispatch, so the record must
    survive the edge.
    """
    captured: list[Any] = []
    original_init = NativeInterruptRunner.__init__

    def _capture(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        captured.append(self)

    monkeypatch.setattr(NativeInterruptRunner, "__init__", _capture)
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, _server_client = _delivery_app([])
    entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review",
    )
    entry.recovered = True
    entry.status = "running"
    async with _runner_client(app) as client:
        runner = captured[-1]
        runner._pending_interrupts[CHILD_SESSION_ID] = entry.work_id
        resp = await _post_status(
            client,
            {"status": "idle", "output": "peer answer", "peer_turn": _peer_turn()},
        )
        await asyncio.sleep(0.05)

    assert resp.status_code == 204
    assert runner.take_pending_interrupt(CHILD_SESSION_ID)[0] is True, (
        "an annotated peer edge must not consume the mother's pending interrupt"
    )


@pytest.mark.asyncio
async def test_master_switch_off_still_records_annotated_copy(
    _clean_subagent_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collab master switch off must not drop an annotated copy.

    The pre-existing master-switch return acknowledges a result this runner did
    not dispatch; a peer turn is still kept as a copy rather than dropped.
    """

    async def _disabled(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(runner_app, "_collab_enabled_live", _disabled)
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, server_client = _peer_app([_assistant_item("item_x", "the answer")])
    async with _runner_client(app) as client:
        resp = await _post_status(
            client,
            {"status": "completed", "output": "the answer", "peer_turn": _peer_turn()},
        )
        await asyncio.sleep(0.05)

    assert resp.status_code == 204
    copies, _ = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert len(copies) == 1
    assert copies[0]["status"] == "completed"
    assert copies[0]["result_key"] == "item_x"
    assert _wake_posts(server_client) == []


@pytest.mark.asyncio
async def test_running_mother_dispatch_wins_over_peer_annotation(
    _clean_subagent_registry: None,
) -> None:
    """A running mother-owned entry keeps today's delivery and wake.

    The annotation only applies when the child has no mother-owned open entry;
    a running dispatch is the mother's own work and must still deliver and wake.
    """
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, server_client = _delivery_app([])
    entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review",
    )
    entry.status = "running"
    async with _runner_client(app) as client:
        resp = await _post_status(
            client,
            {"status": "idle", "output": "the result", "peer_turn": _peer_turn()},
        )
        await asyncio.sleep(0.05)

    assert resp.status_code == 204
    assert subagent_work.pop_peer_copies(PARENT_SESSION_ID)[0] == []
    inbox = subagent_work._session_inboxes_ref[PARENT_SESSION_ID]
    assert [inbox.get_nowait()["status"]] == ["completed"]
    assert len(_wake_posts(server_client)) == 1, "the mother's own dispatch must still wake her"


@pytest.mark.asyncio
async def test_annotated_copy_clears_recovered_launching_placeholder(
    _clean_subagent_registry: None,
) -> None:
    """A copy settles the recovered ``launching`` placeholder its bare idle left.

    An earlier unannotated bare idle of the same turn registers a
    ``recovered=True`` entry in ``launching``. The later annotated completion is
    copied and must clear that placeholder: left open, the launch reaper would
    later deliver a fake "no start acknowledgment" failure to the mother.
    """
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, server_client = _forwarder_confirmed_app()
    async with _runner_client(app) as client:
        init = await client.post(
            "/v1/sessions",
            json={"session_id": CHILD_SESSION_ID, "agent_id": "ag_reviewer"},
        )
        assert init.status_code == 201, init.text
        idle = await _post_status(client, {"status": "idle", "output": "partial"})
        assert idle.status_code == 204
        entry = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert entry is not None and entry.recovered and entry.status == "launching"
        done = await _post_status(
            client,
            {"status": "completed", "output": "the answer", "peer_turn": _peer_turn()},
        )
        assert done.status_code == 204

    copies, _ = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert len(copies) == 1
    assert copies[0]["status"] == "completed"
    assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None, (
        "the recovered launching placeholder must be cleared by the copy"
    )
    reaped = subagent_work.reap_stalled_subagent_launches(now=time.time() + 10_000, timeout_s=1)
    assert reaped == [], "a cleared placeholder must not be reaped as a fake failure"
    assert subagent_work._session_inboxes_ref[PARENT_SESSION_ID].empty()
    assert _wake_posts(server_client) == []


@pytest.mark.asyncio
async def test_peer_copy_correction_amends_in_place(
    _clean_subagent_registry: None,
) -> None:
    """A failed edge for the same result amends the unread copy, without a wake."""
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app, server_client = _delivery_app([])
    async with _runner_client(app) as client:
        first = await _post_status(
            client,
            {"status": "completed", "output": "first", "peer_turn": _peer_turn()},
        )
        failed = await _post_status(
            client,
            {"status": "failed", "output": "second", "peer_turn": _peer_turn()},
        )
        await asyncio.sleep(0.05)

    assert first.status_code == 204 and failed.status_code == 204
    copies, _ = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert len(copies) == 1, "the correction must amend, not append"
    assert copies[0]["status"] == "failed"
    assert copies[0]["output"] == "second"
    assert _wake_posts(server_client) == []


@pytest.mark.asyncio
async def test_peer_copy_replay_is_a_noop(
    _clean_subagent_registry: None,
) -> None:
    """The same edge twice leaves exactly one copy."""
    app, _server_client = _delivery_app([])
    async with _runner_client(app) as client:
        for _ in range(2):
            resp = await _post_status(
                client,
                {"status": "completed", "output": "the answer", "peer_turn": _peer_turn()},
            )
            assert resp.status_code == 204

    copies, dropped = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert len(copies) == 1
    assert dropped == 0


@pytest.mark.asyncio
async def test_cross_host_mirror_edge_records_no_copy(
    _clean_subagent_registry: None,
) -> None:
    """A cross-host mirror edge returns before the peer path; no copy is recorded."""
    app, _server_client = _delivery_app([])
    async with _runner_client(app) as client:
        resp = await _post_status(
            client,
            {
                "status": "idle",
                "output": "done",
                "cross_host": True,
                "peer_turn": _peer_turn(),
            },
        )

    assert resp.status_code == 204
    assert subagent_work.pop_peer_copies(PARENT_SESSION_ID) == ([], 0)


def test_peer_copy_cap_succession_and_teardown(
    _clean_subagent_registry: None,
) -> None:
    """The buffer caps at 20 with a drop count, follows succession, and clears."""
    for index in range(21):
        subagent_work.record_peer_copy(
            PARENT_SESSION_ID,
            f"child_{index}",
            f"key_{index}",
            {
                "type": "peer_copy",
                "child_session_id": f"child_{index}",
                "result_item_id": f"key_{index}",
                "status": "completed",
            },
        )
    copies, dropped = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert len(copies) == 20
    assert dropped == 1
    assert copies[0]["result_item_id"] == "key_1", "the oldest copy must be dropped"

    subagent_work.record_peer_copy(
        "conv_old_mother",
        CHILD_SESSION_ID,
        "key_move",
        {
            "type": "peer_copy",
            "child_session_id": CHILD_SESSION_ID,
            "result_item_id": "key_move",
            "status": "completed",
        },
    )
    runner_app._rekey_subagent_work_for_succession("conv_old_mother", _NEW_PARENT_ID)
    moved, _ = subagent_work.pop_peer_copies(_NEW_PARENT_ID)
    assert [copy["result_item_id"] for copy in moved] == ["key_move"]
    assert subagent_work.pop_peer_copies("conv_old_mother") == ([], 0)

    subagent_work.record_peer_copy(
        _NEW_PARENT_ID,
        CHILD_SESSION_ID,
        "key_teardown",
        {
            "type": "peer_copy",
            "child_session_id": CHILD_SESSION_ID,
            "result_item_id": "key_teardown",
            "status": "completed",
        },
    )
    subagent_work.unregister_subagent_work_for_session(_NEW_PARENT_ID)
    assert subagent_work.pop_peer_copies(_NEW_PARENT_ID) == ([], 0)


def test_record_peer_copy_follows_succession(
    _clean_subagent_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy recorded under a retired parent id lands on the successor."""
    monkeypatch.setitem(subagent_work._succeeded_parents, "conv_old_mother", _NEW_PARENT_ID)
    subagent_work.record_peer_copy(
        "conv_old_mother",
        CHILD_SESSION_ID,
        "key_after",
        {
            "type": "peer_copy",
            "child_session_id": CHILD_SESSION_ID,
            "result_item_id": "key_after",
            "status": "completed",
        },
    )
    moved, _ = subagent_work.pop_peer_copies(_NEW_PARENT_ID)
    assert [copy["result_item_id"] for copy in moved] == ["key_after"]
    assert subagent_work.pop_peer_copies("conv_old_mother") == ([], 0)


def test_move_peer_copies_merges_into_populated_successor(
    _clean_subagent_registry: None,
) -> None:
    """Moving into a successor that already has copies keeps both sets.

    The retired parent's copies are older and are appended first; the cap is
    re-applied, the overflow counted as dropped, and the drop counts add.
    """
    subagent_work.record_peer_copy(
        _NEW_PARENT_ID,
        "child_new",
        "key_new",
        {
            "type": "peer_copy",
            "child_session_id": "child_new",
            "result_item_id": "key_new",
            "status": "completed",
        },
    )
    for index in range(21):
        subagent_work.record_peer_copy(
            "conv_old_mother",
            f"child_old_{index}",
            f"key_old_{index}",
            {
                "type": "peer_copy",
                "child_session_id": f"child_old_{index}",
                "result_item_id": f"key_old_{index}",
                "status": "completed",
            },
        )
    subagent_work.move_peer_copies("conv_old_mother", _NEW_PARENT_ID)

    copies, dropped = subagent_work.pop_peer_copies(_NEW_PARENT_ID)
    keys = [copy["result_item_id"] for copy in copies]
    assert len(copies) == 20, "the cap must be re-applied after the merge"
    assert dropped == 2, "the retired parent's drop and the new overflow both count"
    assert "key_new" in keys, "the successor's own copy must survive the merge"
    assert "key_old_20" in keys, "the retired parent's newest copy must survive"
    assert "key_old_0" not in keys, "the oldest copies drop first"


def test_peer_copy_correction_after_pop_is_flagged(
    _clean_subagent_registry: None,
) -> None:
    """A status change after a copy was popped appends a correction copy."""
    subagent_work.record_peer_copy(
        PARENT_SESSION_ID,
        CHILD_SESSION_ID,
        "key",
        {
            "type": "peer_copy",
            "child_session_id": CHILD_SESSION_ID,
            "result_item_id": "key",
            "status": "completed",
        },
    )
    first, _ = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert len(first) == 1 and "correction" not in first[0]

    subagent_work.record_peer_copy(
        PARENT_SESSION_ID,
        CHILD_SESSION_ID,
        "key",
        {
            "type": "peer_copy",
            "child_session_id": CHILD_SESSION_ID,
            "result_item_id": "key",
            "status": "failed",
        },
    )
    second, _ = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert len(second) == 1
    assert second[0]["correction"] is True
    assert second[0]["status"] == "failed"


def test_peer_copy_replay_after_pop_is_a_noop(
    _clean_subagent_registry: None,
) -> None:
    """Re-recording a popped copy with the same status appends nothing."""
    payload = {
        "type": "peer_copy",
        "child_session_id": CHILD_SESSION_ID,
        "result_item_id": "key",
        "status": "completed",
    }
    subagent_work.record_peer_copy(PARENT_SESSION_ID, CHILD_SESSION_ID, "key", payload)
    subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    subagent_work.record_peer_copy(PARENT_SESSION_ID, CHILD_SESSION_ID, "key", payload)
    assert subagent_work.pop_peer_copies(PARENT_SESSION_ID) == ([], 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mother_status", ["completed", "cancelled", "failed", "stopped", "killed"]
)
async def test_undrained_delivered_mother_result_stamps_result_item_label(
    _clean_subagent_registry: None,
    mother_status: str,
) -> None:
    """A peer copy on an undrained delivered mother result stamps the restart label."""
    app, server_client = _peer_app([])
    entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review",
    )
    entry.status = mother_status
    entry.delivered = True
    entry.delivered_result_key = "item_old"
    work_id = entry.work_id
    async with _runner_client(app) as client:
        resp = await _post_status(
            client,
            {"status": "completed", "output": "the peer answer", "peer_turn": _peer_turn()},
        )
        for _ in range(200):
            if server_client.patches:
                break
            await asyncio.sleep(0.01)

    assert resp.status_code == 204
    assert server_client.patches == [
        (
            f"/v1/sessions/{CHILD_SESSION_ID}",
            {
                "json": {
                    "labels": {
                        subagent_work.SUBAGENT_RESULT_ITEM_LABEL_KEY: (
                            f"{work_id}:{mother_status}:item_old"
                        )
                    }
                },
                "timeout": 30.0,
            },
        )
    ], "an undrained delivered mother result must stamp the result-item label"


@pytest.mark.asyncio
async def test_recovery_uses_stamped_result_item_over_latest_assistant(
    _clean_subagent_registry: None,
) -> None:
    """Recovery reads the stamped item, not the newer latest assistant message."""
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    labels = {
        subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID,
        subagent_work.SUBAGENT_RESULT_ITEM_LABEL_KEY: f"{DISPATCH_ID}:completed:item_old",
    }
    child_items = [
        _assistant_item("item_new", "newer latest"),
        _assistant_item("item_old", "the labelled answer"),
    ]
    app = create_runner_app(
        server_client=_WindowRecoveryServerClient(  # type: ignore[arg-type]
            [_child_summary(labels=labels)], child_items=child_items
        ),
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = subagent_work._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["output"] == "the labelled answer", (
        "recovery must use the stamped result item, not the latest assistant message"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mother_status", ["completed", "cancelled", "failed", "stopped", "killed"]
)
async def test_recovery_pinned_item_wins_over_failed_child_status(
    _clean_subagent_registry: None,
    mother_status: str,
) -> None:
    """A pin matching the dispatch recovers the pinned text even if the child failed."""
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    labels = {
        subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID,
        subagent_work.SUBAGENT_RESULT_ITEM_LABEL_KEY: f"{DISPATCH_ID}:{mother_status}:item_old",
    }
    child_items = [
        _assistant_item("item_new", "newer latest"),
        _assistant_item("item_old", "the labelled answer"),
    ]
    child = _child_summary(
        current_task_status="failed",
        last_task_error={"code": "required_terminal_exited", "message": "pane died"},
        labels=labels,
    )
    app = create_runner_app(
        server_client=_WindowRecoveryServerClient(  # type: ignore[arg-type]
            [child], child_items=child_items
        ),
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = subagent_work._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["status"] == mother_status
    assert payload["output"] == "the labelled answer"

    entry = subagent_work.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None
    assert entry.delivered_result_key == "item_old"


@pytest.mark.asyncio
async def test_recovery_pin_naming_a_missing_item_recovers_nothing(
    _clean_subagent_registry: None,
) -> None:
    """A pin that names a missing item skips the child; the latest item is not used."""
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    labels = {
        subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID,
        subagent_work.SUBAGENT_RESULT_ITEM_LABEL_KEY: f"{DISPATCH_ID}:completed:item_missing",
    }
    child_items = [_assistant_item("item_new", "newer latest")]
    app = create_runner_app(
        server_client=_WindowRecoveryServerClient(  # type: ignore[arg-type]
            [_child_summary(labels=labels)], child_items=child_items
        ),
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    assert subagent_work._session_inboxes_ref[PARENT_SESSION_ID].empty()
    assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None


@pytest.mark.asyncio
async def test_recovery_pin_for_another_dispatch_uses_latest_item(
    _clean_subagent_registry: None,
) -> None:
    """A pin naming another dispatch is inert; recovery uses the latest item."""
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    labels = {
        subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID,
        subagent_work.SUBAGENT_RESULT_ITEM_LABEL_KEY: "subagent_other:completed:item_old",
    }
    child_items = [
        _assistant_item("item_new", "newer latest"),
        _assistant_item("item_old", "the labelled answer"),
    ]
    app = create_runner_app(
        server_client=_WindowRecoveryServerClient(  # type: ignore[arg-type]
            [_child_summary(labels=labels)], child_items=child_items
        ),
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = subagent_work._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["output"] == "newer latest"


@pytest.mark.asyncio
async def test_recovery_missing_pin_continues_to_second_child(
    _clean_subagent_registry: None,
) -> None:
    """A stale pinned cursor skips only its child, allowing the next recovery."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    first = _child_summary(
        labels={
            subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID,
            subagent_work.SUBAGENT_RESULT_ITEM_LABEL_KEY: f"{DISPATCH_ID}:completed:item_missing",
        },
    )
    second = _child_summary(id="conv_second_child")
    server_client = _WindowRecoveryServerClient(
        [first, second], child_items=[_assistant_item("item_second", "second child answer")]
    )
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = inbox.get_nowait()
    assert payload["task_id"] == "conv_second_child"
    assert payload["status"] == "completed"
    assert payload["output"] == "second child answer"
    assert inbox.empty()
    assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label",
    [
        f"{DISPATCH_ID}:item_old",
        f"{DISPATCH_ID}:running:item_old",
        f"{DISPATCH_ID}:completed:",
        f"{DISPATCH_ID}:completed:item_old:extra",
        DISPATCH_ID,
        42,
    ],
)
async def test_recovery_malformed_pin_is_inert(
    _clean_subagent_registry: None,
    label: object,
) -> None:
    """Malformed pins leave the normal latest-result recovery path intact."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    server_client = _WindowRecoveryServerClient(
        [
            _child_summary(
                labels={
                    subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID,
                    subagent_work.SUBAGENT_RESULT_ITEM_LABEL_KEY: label,
                },
            )
        ],
        child_items=[
            _assistant_item("item_new", "newer latest"),
            _assistant_item("item_old", "old answer"),
        ],
    )
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    assert inbox.get_nowait()["output"] == "newer latest"
    assert not any(url.endswith("/items/window") for url, _ in server_client.requests)


@pytest.mark.asyncio
async def test_peer_copy_placeholder_removal_preserves_drained_result(
    _clean_subagent_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late replay stays drained after a peer copy removes its placeholder."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    app, server_client = _forwarder_confirmed_app()

    async def _key(_client: Any, _session_id: str, *, output: str | None = None) -> str:
        return {
            "mother answer": "item_mother",
            "partial": "item_peer",
            "peer answer": "item_peer",
        }[str(output)]

    monkeypatch.setattr(runner_app, "_result_key", _key)
    mother = subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review",
    )
    ack = subagent_work.mark_subagent_work_terminal(
        CHILD_SESSION_ID, status="completed", output="mother answer", result_key="item_mother"
    )
    assert ack.delivered_now
    assert inbox.get_nowait()["output"] == "mother answer"
    subagent_work.unregister_subagent_work(
        CHILD_SESSION_ID, work_id=mother.work_id, remember_drained_delivery=True
    )

    async with _runner_client(app) as client:
        init = await client.post(
            "/v1/sessions",
            json={"session_id": CHILD_SESSION_ID, "agent_id": "ag_reviewer"},
        )
        assert init.status_code == 201, init.text
        idle = await _post_status(client, {"status": "idle", "output": "partial"})
        assert idle.status_code == 204
        entry = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert entry is not None and entry.recovered and entry.status == "launching"
        assert entry.delivered_result_key == "item_mother"
        done = await _post_status(
            client,
            {"status": "completed", "output": "peer answer", "peer_turn": _peer_turn()},
        )
        assert done.status_code == 204
        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None
        replay = await _post_status(client, {"status": "completed", "output": "mother answer"})
        assert replay.status_code == 204
        await asyncio.sleep(0.05)

    assert inbox.empty()
    assert _wake_posts(server_client) == []
    assert subagent_work._drained_delivered_subagent_results[CHILD_SESSION_ID] == "item_mother"
    assert subagent_work._subagent_retained_state_parents[CHILD_SESSION_ID] == PARENT_SESSION_ID
    copies, dropped = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert len(copies) == 1 and dropped == 0


def test_move_peer_copies_deduplicates_identity_with_successor_outcome(
    _clean_subagent_registry: None,
) -> None:
    """Duplicate identities keep their earlier slot with the successor's outcome."""
    for parent, child, key, status, output in [
        ("conv_old_mother", "child_same", "key_same", "completed", "old answer"),
        ("conv_old_mother", "child_old", "key_old", "completed", "old other"),
        (_NEW_PARENT_ID, "child_new", "key_new", "completed", "new other"),
        (_NEW_PARENT_ID, "child_same", "key_same", "failed", "new answer"),
    ]:
        subagent_work.record_peer_copy(
            parent,
            child,
            key,
            {
                "child_session_id": child,
                "result_item_id": key,
                "status": status,
                "output": output,
            },
        )

    subagent_work.move_peer_copies("conv_old_mother", _NEW_PARENT_ID)

    copies, dropped = subagent_work.pop_peer_copies(_NEW_PARENT_ID)
    assert [copy["result_item_id"] for copy in copies] == ["key_same", "key_old", "key_new"]
    assert copies[0]["status"] == "failed"
    assert copies[0]["output"] == "new answer"
    assert dropped == 0


def test_move_peer_copies_keeps_successor_seen_identity_after_overflow(
    _clean_subagent_registry: None,
) -> None:
    """The successor's newest seen result survives the bounded merge."""
    successor_payload = {
        "child_session_id": "child_new",
        "result_item_id": "key_new",
        "status": "failed",
    }
    subagent_work.record_peer_copy(_NEW_PARENT_ID, "child_new", "key_new", successor_payload)
    subagent_work.pop_peer_copies(_NEW_PARENT_ID)
    for index in range(100):
        subagent_work.record_peer_copy(
            "conv_old_mother",
            "child_old",
            f"key_{index}",
            {
                "child_session_id": "child_old",
                "result_item_id": f"key_{index}",
                "status": "completed",
            },
        )
        subagent_work.pop_peer_copies("conv_old_mother")

    subagent_work.move_peer_copies("conv_old_mother", _NEW_PARENT_ID)
    subagent_work.record_peer_copy(_NEW_PARENT_ID, "child_new", "key_new", successor_payload)

    assert subagent_work.pop_peer_copies(_NEW_PARENT_ID) == ([], 0)


def test_move_peer_copies_successor_seen_status_wins(
    _clean_subagent_registry: None,
) -> None:
    """A shared seen identity keeps the successor's newer status."""
    for parent, status in [("conv_old_mother", "completed"), (_NEW_PARENT_ID, "failed")]:
        subagent_work.record_peer_copy(
            parent,
            CHILD_SESSION_ID,
            "key_same",
            {
                "child_session_id": CHILD_SESSION_ID,
                "result_item_id": "key_same",
                "status": status,
            },
        )
        subagent_work.pop_peer_copies(parent)

    subagent_work.move_peer_copies("conv_old_mother", _NEW_PARENT_ID)
    subagent_work.record_peer_copy(
        _NEW_PARENT_ID,
        CHILD_SESSION_ID,
        "key_same",
        {
            "child_session_id": CHILD_SESSION_ID,
            "result_item_id": "key_same",
            "status": "failed",
        },
    )

    assert subagent_work.pop_peer_copies(_NEW_PARENT_ID) == ([], 0)


@pytest.mark.asyncio
async def test_peer_copy_decision_precedes_mother_dispatch_during_result_read(
    _clean_subagent_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mother dispatch registered during the result await belongs to a later turn."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    app, server_client = _delivery_app([])

    async def _key(_client: Any, _session_id: str, *, output: str | None = None) -> str:
        await asyncio.sleep(0)
        subagent_work.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="reviewer",
            title="later mother dispatch",
            work_id="subagent_later",
        )
        return "item_peer"

    monkeypatch.setattr(runner_app, "_result_key", _key)
    async with _runner_client(app) as client:
        response = await _post_status(
            client,
            {"status": "completed", "output": "peer answer", "peer_turn": _peer_turn()},
        )
        await asyncio.sleep(0.05)

    assert response.status_code == 204
    copies, dropped = subagent_work.pop_peer_copies(PARENT_SESSION_ID)
    assert len(copies) == 1 and dropped == 0
    assert copies[0]["output"] == "peer answer"
    entry = subagent_work.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None
    assert entry.work_id == "subagent_later"
    assert entry.status == "launching"
    assert entry.delivered is False
    assert entry.output is None
    assert inbox.empty()
    assert _wake_posts(server_client) == []
