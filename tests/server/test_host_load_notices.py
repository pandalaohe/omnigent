"""Host-load notices use inherited placement, a fake clock and real stores/hub."""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.host import connect as host_connect
from omnigent.host.frames import LoadLevelReport, decode_host_frame
from omnigent.host.load_level import FACTORY_LINES, LevelState
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
    assert "heavy-work hold (current load line 4 per core)" in h.posts[0][1]
    assert "4.25 per core" not in h.posts[0][1]
    assert "Do not dispatch new heavy work" in h.posts[0][1]
    assert h.posts[0][2] == "owner"
    await _tick(h)
    assert len(h.posts) == 1


@pytest.fixture
def sample_host_load(notices, monkeypatch):
    h = notices
    _connect(h.hub, host_id=HOST_A, now=60000, workspace_id=0)
    monkeypatch.setattr(
        host_connect,
        "time",
        SimpleNamespace(time=lambda: h.clock.now, thread_time=time.thread_time),
    )
    sampler = SimpleNamespace()
    host = SimpleNamespace(
        _resource_sampler=sampler,
        _load_state=LevelState(),
        _load_lines=FACTORY_LINES,
        _load_report=None,
        _load_lines_synced_at=None,
    )

    def sample(at, load):
        h.clock.now = at
        frame = _frame(mem_used=50, mem_total=100)
        sampler.sample_load_per_core = load
        sampler.sample = lambda **_: replace(
            frame,
            sampled_at=datetime.fromtimestamp(at, UTC).isoformat(),
            machine=replace(frame.machine, load_per_core=load),
        )
        report = decode_host_frame(
            host_connect.HostProcess._sample_resource_snapshot(
                cast(host_connect.HostProcess, host), {}, None, 60
            )
        )
        h.hub.ingest(host_id=HOST_A, workspace_id=0, now=at, frame=report)

    return sample


async def test_notice_uses_deciding_minute_even_when_latest_sample_crosses_line(
    notices, sample_host_load
):
    h = notices
    _parent(h)
    for at, load in ((60000, 4.5), (60060, 4.5), (60120, 5.8), (60150, 3.2), (60180, 2.01)):
        sample_host_load(at, load)
    await _tick(h)
    assert len(h.posts) == 1
    hold = h.posts[0][1]

    for at, load in ((60240, 2.8), (60300, 3.2), (60360, 4.56)):
        sample_host_load(at, load)
    await _tick(h)
    assert len(h.posts) == 1
    sample_host_load(60420, 4.56)
    sample_host_load(60480, 4.56)
    await _tick(h)
    assert len(h.posts) == 2
    clear = h.posts[1][1]

    assert "is on heavy-work hold" in hold
    assert "deciding minute averaged 4.50 per core and 50.0% memory" in hold
    assert "2.01 per core" not in hold
    assert "is clear" in clear
    assert "deciding minute averaged 3.20 per core and 50.0% memory" in clear
    assert "4.56 per core" not in clear


async def test_pending_notice_labels_the_current_line_after_settings_change(
    notices, sample_host_load
):
    h = notices
    _parent(h)
    h.clock.state = "busy"
    for minute in range(4):
        sample_host_load(60000 + 60 * minute, 4.5)
    await _tick(h)
    assert not h.posts

    h.hub.put_settings({"load_lines": {"default": {"heavy": 5, "new_session": 6}}})
    h.clock.state = "idle"
    await _tick(h)
    assert len(h.posts) == 1
    assert "heavy-work hold (current load line 5 per core)" in h.posts[0][1]
    assert "deciding minute averaged 4.50 per core and 50.0% memory" in h.posts[0][1]


async def test_delayed_clear_keeps_deciding_minute_after_spare_to_normal(
    notices, sample_host_load
):
    h = notices
    _parent(h)
    for minute, load in enumerate((4.5, 4.5, 4.5, 0.4)):
        sample_host_load(60000 + 60 * minute, load)
    await _tick(h)
    assert len(h.posts) == 1
    assert "is on heavy-work hold" in h.posts[0][1]

    h.clock.state = "busy"
    for minute, load in enumerate((0.4, 0.4, 1.4), start=4):
        sample_host_load(60000 + 60 * minute, load)
    assert h.hub.host_load(0, HOST_A, now=h.clock.now)["level"] == "spare"
    await _tick(h)
    assert len(h.posts) == 1

    for minute, load in enumerate((1.4, 4.56, 0.5), start=7):
        sample_host_load(60000 + 60 * minute, load)
    assert h.hub.host_load(0, HOST_A, now=h.clock.now)["level"] == "normal"
    h.clock.state = "idle"
    await _tick(h)
    assert len(h.posts) == 2
    assert "is clear" in h.posts[1][1]
    assert "deciding minute averaged 0.40 per core and 50.0% memory" in h.posts[1][1]


async def test_step_down_to_heavy_hold_omits_new_session_clear_reading(notices, sample_host_load):
    h = notices
    _parent(h)
    for minute, load in enumerate((5.5, 5.5, 5.5, 4.5)):
        sample_host_load(60000 + 60 * minute, load)
    await _tick(h)
    assert len(h.posts) == 1
    assert "is on new-session hold" in h.posts[0][1]
    assert "deciding minute averaged 5.50 per core and 50.0% memory" in h.posts[0][1]

    for minute, load in enumerate((4.5, 2.0, 4.5), start=4):
        sample_host_load(60000 + 60 * minute, load)
    assert h.hub.host_load(0, HOST_A, now=h.clock.now)["level"] == "heavy_hold"
    await _tick(h)
    assert len(h.posts) == 1

    sample_host_load(60420, 4.5)
    sample_host_load(60480, 4.5)
    await _tick(h)
    assert len(h.posts) == 2
    assert "is on heavy-work hold (current load line 4 per core)" in h.posts[1][1]
    assert "Do not dispatch new heavy work" in h.posts[1][1]
    assert "deciding minute" not in h.posts[1][1]


async def test_cooldown_and_spare_clear_then_normal_are_quiet(notices):
    h = notices
    _parent(h)
    _report(h, "heavy_hold", reason="memory")
    await _tick(h)
    assert "heavy-work hold (current memory line 90%)" in h.posts[0][1]
    assert "92.0%" not in h.posts[0][1]
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
    assert "heavy-work hold (current memory line 90%)" in h.posts[0][1]


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
