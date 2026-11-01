"""Host-load notices use inherited placement, a fake clock and real stores/hub."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.host.frames import LoadLevelReport
from omnigent.server.host_load_notices import HostLoadNotices
from omnigent.server.system_status import SystemStatusHub
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.test_system_status import _connect, _frame

HOST_A = "a" * 32
HOST_B = "b" * 32
RUNNER = "c" * 32


@pytest.fixture
def notices(db_uri, tmp_path):
    store = SqlAlchemyConversationStore(db_uri)
    hub = SystemStatusHub(tmp_path, None)
    clock = SimpleNamespace(now=100000.0, state="idle", result={}, error=False)
    posts = []

    async def state(parent):
        return clock.state, True

    async def post(request, parent, event, **kwargs):
        posts.append((parent, event.data["content"][0]["text"], kwargs["acting_user_id"]))
        if clock.error:
            raise TimeoutError("lost response")
        return clock.result

    notifier = HostLoadNotices(
        conversation_store=store,
        owner_for=lambda parent: "owner",
        owns_parent=lambda parent: parent.runner_id == RUNNER,
        true_state=state,
        post_event=post,
        clock=lambda: clock.now,
    )
    yield SimpleNamespace(store=store, hub=hub, clock=clock, posts=posts, notifier=notifier)
    hub.close()


def _parent(h, host=HOST_A):
    parent = h.store.create_conversation(
        title="mother", host_id=host, workspace="/opt/work/project", runner_id=RUNNER
    )
    h.store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id, title="child")
    return parent


def _report(h, level, *, host=HOST_A, reason="load", workspace=0):
    now = h.clock.now
    _connect(h.hub, host_id=host, now=now, workspace_id=workspace)
    stamp = datetime.fromtimestamp(now, UTC).isoformat()
    frame = _frame(mem_used=92, mem_total=100)
    h.hub.ingest(
        host_id=host,
        workspace_id=workspace,
        now=now,
        frame=replace(
            frame,
            sampled_at=stamp,
            machine=replace(frame.machine, cpu_count=8, load_per_core=4.25, load_equiv=34),
            load_level=LoadLevelReport(level, reason, stamp, 4, 5, None),
        ),
    )


async def _tick(h):
    await h.notifier.tick(h.hub, None)
    await asyncio.gather(*list(h.notifier._tasks.values()))


async def test_busy_hold_clear_hold_composes_current_level_once(notices):
    h = notices
    parent = _parent(h)
    h.clock.state = "busy"
    for level in ("heavy_hold", "normal", "new_session_hold", "heavy_hold"):
        _report(h, level)
        await _tick(h)
        h.clock.now += 60
    assert not h.posts
    h.clock.state = "steerable"
    await _tick(h)
    assert len(h.posts) == 1
    assert h.posts[0][0] == parent.id
    assert "heavy-work line (4.25 per core ≥ 4)" in h.posts[0][1]
    assert "Do not dispatch new heavy work" in h.posts[0][1]
    assert h.posts[0][2] == "owner"
    await _tick(h)
    assert len(h.posts) == 1


async def test_cooldown_and_spare_clear_then_normal_are_quiet(notices):
    h = notices
    _parent(h)
    _report(h, "heavy_hold", reason="memory")
    await _tick(h)
    assert "memory 92.0% ≥ 90%" in h.posts[0][1]
    h.clock.now += 299
    _report(h, "spare", reason=None)
    await _tick(h)
    assert len(h.posts) == 1
    h.clock.now += 1
    await _tick(h)
    assert "Heavy work may resume" in h.posts[1][1]
    for level in ("normal", "spare", "normal"):
        h.clock.now += 300
        _report(h, level, reason=None)
        await _tick(h)
    assert len(h.posts) == 2


async def test_recipients_inherit_host_and_include_parent_own_host(notices):
    h = notices
    inherited = _parent(h)
    other = _parent(h, HOST_B)
    cross = h.store.create_conversation(
        title="cross-host mother", host_id=HOST_A, workspace="/opt/work/project", runner_id=RUNNER
    )
    h.store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=cross.id,
        host_id=HOST_B,
        workspace="/opt/work/other",
    )
    archived = _parent(h)
    h.store.update_conversation(archived.id, archived=True)
    closed = _parent(h)
    h.store.set_labels(closed.id, {"omnigent.closed": "true"})
    _report(h, "heavy_hold")
    await _tick(h)
    recipients = {parent for parent, _, _ in h.posts}
    assert recipients == {inherited.id, cross.id}
    assert other.id not in recipients


@pytest.mark.parametrize("outcome", ["rejected", "uncertain"])
async def test_definite_failure_retries_but_lost_response_spends_budget(notices, outcome):
    h = notices
    _parent(h)
    _report(h, "new_session_hold")
    h.clock.result = {"forwarded": False}
    h.clock.error = outcome == "uncertain"
    await _tick(h)
    assert "queued and start automatically" in h.posts[0][1]
    h.clock.result, h.clock.error = {}, False
    await _tick(h)
    assert len(h.posts) == (2 if outcome == "rejected" else 1)


async def test_workspace_and_archived_after_mark_are_isolated(notices):
    h = notices
    _parent(h)
    with workspace_scope(2):
        parent = _parent(h)
    h.clock.state = "busy"
    _report(h, "heavy_hold", workspace=2)
    await _tick(h)
    with workspace_scope(2):
        h.store.update_conversation(parent.id, archived=True)
    h.clock.state = "idle"
    await _tick(h)
    assert not h.posts


async def test_readiness_probe_does_not_deliver_an_old_level(notices):
    h = notices
    _parent(h)
    _report(h, "new_session_hold")

    async def state(parent):
        _report(h, "heavy_hold", reason="memory")
        return "idle", True

    h.notifier._true_state = state
    await _tick(h)
    assert len(h.posts) == 1
    assert "memory 92.0%" in h.posts[0][1]


async def test_owner_read_failure_keeps_pending_until_retry(notices):
    h = notices
    _parent(h)
    _report(h, "heavy_hold")

    def unavailable(parent):
        raise RuntimeError("owner lookup unavailable")

    h.notifier._owner_for = unavailable
    await _tick(h)
    assert not h.posts
    h.notifier._owner_for = lambda parent: "owner"
    await _tick(h)
    assert len(h.posts) == 1


async def test_slow_posts_are_bounded_without_blocking_tick_or_duplicate_delivery(notices):
    h = notices
    parents = [_parent(h) for _ in range(6)]
    _report(h, "heavy_hold")
    started, release = asyncio.Event(), asyncio.Event()
    posts = []
    active = peak = 0

    async def slow_post(request, parent, event, **kwargs):
        nonlocal active, peak
        posts.append(parent)
        active += 1
        peak = max(peak, active)
        if active == 4:
            started.set()
        await release.wait()
        active -= 1
        return {}

    h.notifier._post_event = slow_post
    try:
        await asyncio.wait_for(h.notifier.tick(h.hub, None), timeout=2)
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(h.notifier.tick(h.hub, None), timeout=2)
        assert len(posts) == 4
        assert len(h.notifier._tasks) == 6
        release.set()
        await asyncio.gather(*list(h.notifier._tasks.values()))
        assert peak == 4
        assert set(posts) == {parent.id for parent in parents}
        assert len(posts) == 6
    finally:
        release.set()
        await h.notifier.shutdown()


async def test_queue_disabled_notice_does_not_promise_automatic_start(notices):
    h = notices
    _parent(h)
    h.hub.put_settings({"load_lines": {"queue_enabled": False}}, workspace_id=0)
    _report(h, "new_session_hold")
    await _tick(h)
    assert "queuing is disabled" in h.posts[0][1]
    assert "queued and start automatically" not in h.posts[0][1]
