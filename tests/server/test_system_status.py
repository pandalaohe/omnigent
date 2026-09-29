"""Unit tests for the server-side system-status hub."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from pathlib import Path

import pytest

from omnigent.host.frames import (
    CAP_RESOURCE_SNAPSHOT,
    HostResourceSnapshotFrame,
    ResourceMachine,
    ResourceProcessRow,
)
from omnigent.server import system_status
from omnigent.server.system_status import SystemStatusHub

_GIB = 1024 * 1024 * 1024


def _frame(
    *,
    cpu_pct: float = 10.0,
    mem_used: int = 4 * _GIB,
    mem_total: int = 16 * _GIB,
    disk_used: int = 40,
    disk_total: int = 100,
    load1: float | None = 0.5,
    runner_count: int = 1,
    sampler_cpu_ms: float = 0.5,
    monitor_rss_delta: int = 1024,
    sessions: dict[str, float] | None = None,
    processes: list[ResourceProcessRow] | None = None,
) -> HostResourceSnapshotFrame:
    """Build a realistic-enough snapshot frame for hub tests."""
    if processes is None:
        session_cpu = {"conv_a": 1.0} if sessions is None else sessions
        processes = [
            ResourceProcessRow(
                pid=100 + index,
                ppid=99,
                name=f"proc{index}",
                role="child",
                session_id=session_id,
                cpu_pct=cpu,
                rss=1024 * 1024,
            )
            for index, (session_id, cpu) in enumerate(session_cpu.items())
        ]
    return HostResourceSnapshotFrame(
        sampled_at="2026-09-29T09:25:00+00:00",
        interval_s=60,
        machine=ResourceMachine(
            cpu_pct=cpu_pct,
            mem_used=mem_used,
            mem_total=mem_total,
            disk_used=disk_used,
            disk_total=disk_total,
            load1=load1,
        ),
        processes=processes,
        runner_count=runner_count,
        sampler_cpu_ms=sampler_cpu_ms,
        monitor_rss_delta=monitor_rss_delta,
    )


def _connect(hub: SystemStatusHub, *, host_id: str, now: float, workspace_id: int = 0) -> None:
    hub.host_changed(
        host_id=host_id,
        workspace_id=workspace_id,
        owner="alice",
        name=f"laptop-{host_id}",
        conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
        now=now,
    )


def _tick(hub: SystemStatusHub, now: float) -> bool:
    """Run one tick; the server tick loop reads disk usage off-loop."""
    return hub.tick(now, server_disk_pct=0.0)


def test_host_changed_tracks_connect_disconnect_and_needs_update(tmp_path: Path) -> None:
    """State transitions restamp ``since`` only when the state changes."""
    hub = SystemStatusHub(tmp_path, None)
    entry_key = (0, "host_a")

    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name="laptop",
        conn_capabilities=None,
        now=1.0,
    )
    entry = hub._entries[entry_key]
    assert entry.state == "offline"
    assert entry.since == 1.0
    assert entry.owner == "alice"
    assert entry.name == "laptop"

    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name="laptop",
        conn_capabilities=["something_else"],
        now=2.0,
    )
    assert entry.state == "needs_update"
    assert entry.since == 2.0

    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name="laptop",
        conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
        now=3.0,
    )
    assert entry.state == "online"
    assert entry.since == 3.0

    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name="laptop",
        conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
        now=4.0,
    )
    assert entry.since == 3.0, "an unchanged state must not restamp since"

    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(), now=4.5)
    snapshot = entry.last_snapshot
    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name=None,
        conn_capabilities=None,
        now=5.0,
    )
    assert entry.state == "offline"
    assert entry.since == 5.0
    assert entry.last_snapshot == snapshot, "a disconnect keeps the last snapshot"


def test_owner_change_resets_telemetry_and_findings(tmp_path: Path) -> None:
    """A new owner must not see the previous owner's points or findings."""
    hub = SystemStatusHub(tmp_path, None)
    now = time.time()
    _connect(hub, host_id="host_a", now=now)
    for minute in range(10):
        hub.ingest(
            host_id="host_a",
            workspace_id=0,
            frame=_frame(cpu_pct=95.0),
            now=now + minute * 60.0 + 1,
        )
        _tick(hub, now + (minute + 1) * 60.0)
    assert (0, "host_a:cpu") in hub._findings
    entry = hub._entries[(0, "host_a")]
    assert entry.points and entry.last_snapshot is not None

    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="bob",
        name="bob-laptop",
        conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
        now=now + 650.0,
    )
    assert entry.owner == "bob"
    assert entry.points == []
    assert entry.minute_buffer == []
    assert entry.last_snapshot is None
    assert entry.last_runner_count == 0
    assert not any(finding_id.startswith("host_a:") for _ws, finding_id in hub._findings)

    _tick(hub, now + 700.0)
    assert not any(finding_id.startswith("host_a:") for _ws, finding_id in hub._findings), (
        "the next evaluation must not resurrect the old findings"
    )

    view = hub.view(
        user_id="bob",
        is_admin=False,
        own_host_ids=set(),
        own_offline_hosts=[],
        workspace_id=0,
        summary=False,
    )
    assert view["findings"] == []
    assert view["hosts"][0]["last_snapshot"] is None
    assert (
        hub.history("host_a", user_id="bob", is_admin=False, own_host_ids=set(), workspace_id=0)
        == []
    )


def test_owner_change_announces_dropped_findings(tmp_path: Path, monkeypatch) -> None:
    """An owner reset that drops a finding bumps the revision and announces once."""
    hub = SystemStatusHub(tmp_path, None)
    now = time.time()
    _connect(hub, host_id="host_a", now=now)
    for minute in range(10):
        hub.ingest(
            host_id="host_a",
            workspace_id=0,
            frame=_frame(cpu_pct=95.0),
            now=now + minute * 60.0 + 1,
        )
        _tick(hub, now + (minute + 1) * 60.0)
    assert (0, "host_a:cpu") in hub._findings

    announcements: list[set[int]] = []
    monkeypatch.setattr(hub, "_announce", announcements.append)
    revision = hub._revision
    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="bob",
        name="bob-laptop",
        conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
        now=now + 650.0,
    )
    assert hub._revision == revision + 1
    assert announcements == [{0}]

    _connect(hub, host_id="host_b", now=now + 660.0)
    hub.host_changed(
        host_id="host_b",
        workspace_id=0,
        owner="bob",
        name="bob-laptop",
        conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
        now=now + 670.0,
    )
    assert hub._revision == revision + 1, "an owner change with no findings must not bump"
    assert len(announcements) == 1


def test_tick_closes_a_minute_point_with_top_sessions(tmp_path: Path) -> None:
    """A minute of snapshots becomes one point with the top 3 sessions."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    hub.ingest(
        host_id="host_a",
        workspace_id=0,
        frame=_frame(cpu_pct=20.0, sessions={"conv_a": 1.0, "conv_b": 5.0}),
        now=10.0,
    )
    hub.ingest(
        host_id="host_a",
        workspace_id=0,
        frame=_frame(cpu_pct=40.0, sessions={"conv_a": 2.0, "conv_b": 1.0}),
        now=40.0,
    )

    assert _tick(hub, 60.0) is False
    (point,) = hub._entries[(0, "host_a")].points
    assert point["t"] == 60.0
    assert point["cpu_avg"] == 30.0
    assert point["cpu_max"] == 40.0
    assert point["mem_used"] == 4 * _GIB
    assert point["mem_total"] == 16 * _GIB
    assert point["disk_pct"] == 40.0
    assert point["load1"] == 0.5
    assert point["top"] == [["conv_b", 6.0], ["conv_a", 3.0]]

    assert _tick(hub, 120.0) is False
    assert len(hub._entries[(0, "host_a")].points) == 1, "an empty minute adds no point"


def test_tick_trims_and_drops_a_stale_offline_target(tmp_path: Path) -> None:
    """A target offline past 24 h loses its points and then its entry."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(), now=1.0)
    _tick(hub, 60.0)
    assert len(hub._entries[(0, "host_a")].points) == 1

    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name=None,
        conn_capabilities=None,
        now=120.0,
    )
    _tick(hub, 26 * 60 * 60.0)

    assert (0, "host_a") not in hub._entries
    view = hub.view(
        user_id="alice",
        is_admin=True,
        own_host_ids=set(),
        own_offline_hosts=[],
        workspace_id=0,
        summary=False,
    )
    assert view["hosts"] == []


def test_offline_with_runners_is_red_after_two_minutes(tmp_path: Path) -> None:
    """An offline host with runners raises a red finding after 120 s."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(runner_count=2), now=1.0)
    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name=None,
        conn_capabilities=None,
        now=100.0,
    )

    assert _tick(hub, 160.0) is False, "under 120 s offline there is no finding"
    (entry,) = hub._entries.values()
    assert entry.points, "the point established before the disconnect survives"

    assert _tick(hub, 221.0) is True
    findings = hub._findings[(0, "host_a:offline")]
    assert findings["level"] == "red"
    assert findings["since"] == 220.0

    assert _tick(hub, 280.0) is False, "a still-holding finding does not re-announce"
    assert hub._findings[(0, "host_a:offline")]["since"] == 220.0


def test_offline_without_runners_is_not_a_finding(tmp_path: Path) -> None:
    """Offline with no runners (and needs_update) stays out of findings."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(runner_count=0), now=1.0)
    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name=None,
        conn_capabilities=None,
        now=100.0,
    )
    assert _tick(hub, 1000.0) is False
    assert hub._findings == {}

    hub.host_changed(
        host_id="host_b",
        workspace_id=0,
        owner="alice",
        name="old",
        conn_capabilities=[],
        now=1001.0,
    )
    assert _tick(hub, 2000.0) is False
    assert hub._findings == {}


def test_cpu_finding_needs_ten_sustained_minutes(tmp_path: Path) -> None:
    """A CPU finding appears only after 10 points above the threshold."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    for minute in range(9):
        hub.ingest(
            host_id="host_a",
            workspace_id=0,
            frame=_frame(cpu_pct=90.0),
            now=minute * 60.0 + 1,
        )
        assert _tick(hub, (minute + 1) * 60.0) is False
    assert (0, "host_a:cpu") not in hub._findings

    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(cpu_pct=90.0), now=9 * 60.0 + 1)
    assert _tick(hub, 10 * 60.0) is True
    finding = hub._findings[(0, "host_a:cpu")]
    assert finding["level"] == "amber"
    assert finding["kind"] == "cpu"
    assert finding["id"] == "host_a:cpu"


def test_cpu_finding_honors_custom_sustain_minutes(tmp_path: Path) -> None:
    """``cpu_sustain_min`` decides how many hot points the CPU finding needs."""
    hub = SystemStatusHub(tmp_path, None)
    hub.put_settings({"cpu_sustain_min": 3})
    _connect(hub, host_id="host_a", now=0.0)
    for minute in range(2):
        hub.ingest(
            host_id="host_a",
            workspace_id=0,
            frame=_frame(cpu_pct=90.0),
            now=minute * 60.0 + 1,
        )
        assert _tick(hub, (minute + 1) * 60.0) is False
    assert (0, "host_a:cpu") not in hub._findings

    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(cpu_pct=90.0), now=2 * 60.0 + 1)
    assert _tick(hub, 3 * 60.0) is True
    finding = hub._findings[(0, "host_a:cpu")]
    assert finding["level"] == "amber"
    assert finding["detail"] == "cpu above 85% for 3 minutes"


def test_cpu_finding_needs_contiguous_minutes(tmp_path: Path) -> None:
    """Ten hot points an hour apart are not ten sustained minutes."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    for hour in range(10):
        hub.ingest(
            host_id="host_a",
            workspace_id=0,
            frame=_frame(cpu_pct=95.0),
            now=hour * 3600.0 + 1,
        )
        _tick(hub, (hour + 1) * 3600.0)
    assert (0, "host_a:cpu") not in hub._findings


@pytest.mark.parametrize(
    "conn_capabilities", [None, ["something_else"]], ids=["offline", "needs_update"]
)
def test_stale_points_clear_every_resource_finding(
    tmp_path: Path, conn_capabilities: list[str] | None
) -> None:
    """Hot points older than the freshness window raise no finding."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    for minute in range(10):
        hub.ingest(
            host_id="host_a",
            workspace_id=0,
            frame=_frame(
                cpu_pct=95.0,
                mem_used=15 * _GIB,
                mem_total=16 * _GIB,
                disk_used=95,
                disk_total=100,
            ),
            now=minute * 60.0 + 1,
        )
        _tick(hub, (minute + 1) * 60.0)
    assert {(0, "host_a:cpu"), (0, "host_a:mem"), (0, "host_a:disk")} <= set(hub._findings)

    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name=None,
        conn_capabilities=conn_capabilities,
        now=650.0,
    )
    _tick(hub, 1000.0)

    assert (0, "host_a:cpu") not in hub._findings
    assert (0, "host_a:mem") not in hub._findings
    assert (0, "host_a:disk") not in hub._findings


def test_revision_bumps_only_when_the_finding_set_changes(tmp_path: Path) -> None:
    """Revision stays put while the same findings hold across ticks."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(runner_count=1), now=1.0)
    assert _tick(hub, 60.0) is False
    assert hub._revision == 0
    assert _tick(hub, 120.0) is False
    assert hub._revision == 0

    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name=None,
        conn_capabilities=None,
        now=130.0,
    )
    assert _tick(hub, 260.0) is True, "a new red finding bumps the revision"
    assert hub._revision == 1
    assert _tick(hub, 320.0) is False
    assert hub._revision == 1


def test_view_visibility_for_admin_and_member(tmp_path: Path) -> None:
    """Admins see the server card and every host; members see only their own."""
    hub = SystemStatusHub(tmp_path, None)
    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name="alice-laptop",
        conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
        now=0.0,
    )
    hub.host_changed(
        host_id="host_b",
        workspace_id=0,
        owner="bob",
        name="bob-laptop",
        conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
        now=0.0,
    )
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(), now=1.0)
    hub.ingest(host_id="host_b", workspace_id=0, frame=_frame(), now=1.0)
    _tick(hub, 60.0)

    admin = hub.view(
        user_id="admin",
        is_admin=True,
        own_host_ids=set(),
        own_offline_hosts=[],
        workspace_id=0,
        summary=False,
    )
    assert {host["host_id"] for host in admin["hosts"]} == {"host_a", "host_b"}
    assert admin["server"] is not None
    assert admin["server"]["last_point"] is None

    member = hub.view(
        user_id="bob",
        is_admin=False,
        own_host_ids={"host_b"},
        own_offline_hosts=[{"host_id": "host_c", "name": "old-bob-host"}],
        workspace_id=0,
        summary=False,
    )
    assert {host["host_id"] for host in member["hosts"]} == {"host_b", "host_c"}
    assert member["server"] is None
    assert member["hosts"][1]["state"] == "offline"
    assert member["hosts"][1]["last_snapshot"] is None

    summary = hub.view(
        user_id="bob",
        is_admin=False,
        own_host_ids=set(),
        own_offline_hosts=[],
        workspace_id=0,
        summary=True,
    )
    assert set(summary) == {"revision", "level", "findings"}


def test_view_findings_are_filtered_by_visibility(tmp_path: Path) -> None:
    """A member never sees another owner's host finding or the server finding."""
    hub = SystemStatusHub(tmp_path, None)
    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name="alice-laptop",
        conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
        now=0.0,
    )
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(runner_count=1), now=1.0)
    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name=None,
        conn_capabilities=None,
        now=100.0,
    )
    _tick(hub, 300.0)
    assert hub._findings[(0, "host_a:offline")]["level"] == "red"

    member = hub.view(
        user_id="bob",
        is_admin=False,
        own_host_ids=set(),
        own_offline_hosts=[],
        workspace_id=0,
        summary=False,
    )
    assert member["findings"] == []
    assert member["level"] == "ok"

    admin = hub.view(
        user_id="admin",
        is_admin=True,
        own_host_ids=set(),
        own_offline_hosts=[],
        workspace_id=0,
        summary=False,
    )
    assert [finding["id"] for finding in admin["findings"]] == ["host_a:offline"]
    assert admin["level"] == "red"


def test_history_visibility_and_age_filter(tmp_path: Path) -> None:
    """History is age-filtered at read time and only visible per caller."""
    hub = SystemStatusHub(tmp_path, None)
    now = time.time()
    fresh = {"t": now - 60.0, "cpu_avg": 10.0}
    stale = {"t": now - 25 * 60 * 60.0, "cpu_avg": 99.0}
    hub._server_points = [stale, fresh]
    _connect(hub, host_id="host_a", now=now)
    hub._entries[(0, "host_a")].points = [stale, fresh]

    assert hub.history(
        "server", user_id="admin", is_admin=True, own_host_ids=set(), workspace_id=0
    ) == [fresh]
    assert (
        hub.history("server", user_id="bob", is_admin=False, own_host_ids=set(), workspace_id=0)
        is None
    )
    assert hub.history(
        "host_a", user_id="alice", is_admin=False, own_host_ids=set(), workspace_id=0
    ) == [fresh]
    assert (
        hub.history("host_a", user_id="bob", is_admin=False, own_host_ids=set(), workspace_id=0)
        is None
    )
    assert hub.history(
        "host_a", user_id="bob", is_admin=False, own_host_ids={"host_a"}, workspace_id=0
    ) == [fresh]
    assert (
        hub.history("host_x", user_id="admin", is_admin=True, own_host_ids=set(), workspace_id=0)
        is None
    )


def test_load_drops_points_older_than_24_hours(tmp_path: Path) -> None:
    """Startup restore keeps 1 h-old points and drops 25 h-old ones."""
    hub = SystemStatusHub(tmp_path, None)
    state_dir = tmp_path / "system-status"
    state_dir.mkdir(parents=True)
    now = time.time()
    (state_dir / "history.json").write_text(
        json.dumps(
            {
                "version": 1,
                "server": [{"t": now - 25 * 60 * 60.0}, {"t": now - 60 * 60.0}],
                "hosts": {
                    "0:host_a": {
                        "owner": "alice",
                        "name": "laptop",
                        "points": [
                            {"t": now - 25 * 60 * 60.0},
                            {"t": now - 60 * 60.0, "cpu_avg": 10.0},
                        ],
                    },
                    "0:host_b": {
                        "owner": "bob",
                        "name": "old",
                        "points": [{"t": now - 25 * 60 * 60.0}],
                    },
                },
            }
        )
    )

    hub.load()

    assert hub._server_points == [{"t": now - 60 * 60.0}]
    assert list(hub._entries) == [(0, "host_a")]
    entry = hub._entries[(0, "host_a")]
    assert entry.owner == "alice"
    assert entry.name == "laptop"
    assert entry.state == "offline"
    assert len(entry.points) == 1


def test_corrupt_history_file_is_ignored(tmp_path: Path) -> None:
    """A corrupt history file is logged, ignored, and replaced on write."""
    state_dir = tmp_path / "system-status"
    state_dir.mkdir(parents=True)
    (state_dir / "history.json").write_text("{not json")

    hub = SystemStatusHub(tmp_path, None)
    hub.load()
    assert hub._entries == {}
    assert hub._server_points == []

    _connect(hub, host_id="host_a", now=0.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(), now=1.0)
    _tick(hub, 60.0)
    system_status.write_history(hub.history_path, hub.history_payload())
    payload = json.loads((state_dir / "history.json").read_text())
    assert payload["version"] == 1
    assert set(payload["hosts"]) == {"0:host_a"}


def test_history_payload_snapshots_the_point_lists(tmp_path: Path) -> None:
    """The payload owns copies, so a worker can serialize it without racing."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(), now=1.0)
    _tick(hub, 60.0)

    payload = hub.history_payload()
    hub._entries[(0, "host_a")].points.clear()

    assert payload["hosts"]["0:host_a"]["points"], "the payload owns a copy"


def test_save_history_without_points_creates_nothing(tmp_path: Path) -> None:
    """A hub that never recorded a point must not create the state directory."""
    hub = SystemStatusHub(tmp_path, None)

    asyncio.run(hub.save_history())
    hub.close()

    assert not hub.history_path.exists()
    assert not hub.history_path.parent.exists()


def test_save_history_overwrites_stale_file_after_owner_reset(tmp_path: Path) -> None:
    """An owner reset empties the history; the stale points must not survive."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(), now=1.0)
    _tick(hub, 60.0)
    asyncio.run(hub.save_history())
    assert hub.history_path.exists()

    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="bob",
        name="laptop-host_a",
        conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
        now=120.0,
    )
    assert hub.history_payload()["hosts"]["0:host_a"]["points"] == []

    asyncio.run(hub.save_history())
    hub.close()

    assert json.loads(hub.history_path.read_text()) == {
        "version": 1,
        "server": [],
        "hosts": {"0:host_a": {"owner": "bob", "name": "laptop-host_a", "points": []}},
    }


def test_save_history_writes_in_submission_order(tmp_path: Path, monkeypatch) -> None:
    """A cancelled save leaves its queued write ahead of the next one."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(cpu_pct=10.0), now=1.0)
    _tick(hub, 60.0)

    written: list[dict] = []
    started = threading.Event()
    release = threading.Event()
    real_write_history = system_status.write_history

    def blocking_write_history(path: Path, payload: dict) -> None:
        started.set()
        assert release.wait(timeout=5.0)
        written.append(payload)
        real_write_history(path, payload)

    monkeypatch.setattr(system_status, "write_history", blocking_write_history)

    async def scenario() -> None:
        first = asyncio.create_task(hub.save_history())
        assert await asyncio.to_thread(started.wait, 5.0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(cpu_pct=20.0), now=61.0)
        _tick(hub, 120.0)
        second = asyncio.create_task(hub.save_history())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(second, timeout=5.0)

    asyncio.run(scenario())
    hub.close()

    assert [len(payload["hosts"]["0:host_a"]["points"]) for payload in written] == [1, 2]
    assert json.loads(hub.history_path.read_text()) == written[1]


def test_save_settings_writes_in_submission_order(tmp_path: Path, monkeypatch) -> None:
    """A cancelled settings save leaves its queued write ahead of the next one."""
    hub = SystemStatusHub(tmp_path, None)
    old = hub.put_settings({"cpu_pct": 80.0})
    new = hub.put_settings({"cpu_pct": 70.0})

    written: list[dict] = []
    started = threading.Event()
    release = threading.Event()
    real_write_settings = system_status.write_settings

    def blocking_write_settings(path: Path, payload: dict) -> None:
        started.set()
        assert release.wait(timeout=5.0)
        written.append(payload)
        real_write_settings(path, payload)

    monkeypatch.setattr(system_status, "write_settings", blocking_write_settings)

    async def scenario() -> None:
        first = asyncio.create_task(hub.save_settings(old))
        assert await asyncio.to_thread(started.wait, 5.0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(hub.save_settings(new))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(second, timeout=5.0)

    asyncio.run(scenario())
    hub.close()

    assert [payload["cpu_pct"] for payload in written] == [80.0, 70.0]
    assert json.loads(hub.settings_path.read_text()) == new


def test_settings_round_trip_and_validation(tmp_path: Path) -> None:
    """Thresholds persist and reject unknown keys / out-of-range numbers."""
    hub = SystemStatusHub(tmp_path, None)
    assert hub.get_settings() == {
        "cpu_pct": 85.0,
        "cpu_sustain_min": 10,
        "mem_pct": 90.0,
        "disk_pct": 90.0,
        "server_5xx_pct": 5.0,
        "health_check": {"project_id": None, "host_id": None, "prompt": None},
    }

    updated = hub.put_settings({"cpu_pct": 70, "cpu_sustain_min": 3.0})
    assert updated["cpu_pct"] == 70.0
    assert updated["cpu_sustain_min"] == 3
    assert updated["mem_pct"] == 90.0
    system_status.write_settings(hub.settings_path, updated)
    reloaded = SystemStatusHub(tmp_path, None)
    assert reloaded.get_settings()["cpu_pct"] == 70.0
    assert reloaded.get_settings()["cpu_sustain_min"] == 3

    (tmp_path / "system-status" / "settings.json").write_text(
        json.dumps({"cpu_sustain_min": 0, "cpu_pct": 70})
    )
    repaired = SystemStatusHub(tmp_path, None)
    assert repaired.get_settings()["cpu_sustain_min"] == 10
    assert repaired.get_settings()["cpu_pct"] == 70.0

    bad_settings: list[dict[str, object]] = [
        {"nope": 1},
        {"cpu_pct": True},
        {"cpu_pct": 0},
        {"cpu_pct": 101},
        {"cpu_pct": "x"},
        {"cpu_sustain_min": 0},
        {"cpu_sustain_min": 1441},
        {"cpu_sustain_min": 2.5},
        {"cpu_sustain_min": True},
        {"cpu_sustain_min": "3"},
    ]
    for bad in bad_settings:
        try:
            hub.put_settings(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")


def test_fast_hosts_leases(tmp_path: Path) -> None:
    """A viewer lease keeps a host fast for 25 s and then lapses."""
    hub = SystemStatusHub(tmp_path, None)
    hub.mark_viewer(["host_a", "host_b"], workspace_id=0, now=100.0)
    assert set(hub.fast_hosts(110.0)) == {(0, "host_a"), (0, "host_b")}
    assert set(hub.fast_hosts(125.0)) == {(0, "host_a"), (0, "host_b")}
    assert hub.fast_hosts(125.1) == []


def test_hub_never_calls_metrics_snapshot() -> None:
    """The hub reads ``metrics.last_snapshot``; snapshot() resets its baseline."""
    source = inspect.getsource(system_status)
    assert ".snapshot(" not in source


def _brief(
    hub: SystemStatusHub,
    now: float,
    *,
    workspace_id: int = 0,
    own_offline_hosts: list[dict[str, object]] | None = None,
    host_versions: dict[str, str] | None = None,
    server_version: str = "9.9.9",
) -> str:
    return hub.brief(
        now=now,
        workspace_id=workspace_id,
        own_offline_hosts=own_offline_hosts or [],
        host_versions=host_versions or {},
        server_version=server_version,
    )


def test_health_check_settings_round_trip(tmp_path: Path) -> None:
    """The health_check object round-trips; the thresholds stay numeric."""
    hub = SystemStatusHub(tmp_path, None)
    assert hub.get_settings()["health_check"] == {
        "project_id": None,
        "host_id": None,
        "prompt": None,
    }

    updated = hub.put_settings(
        {"health_check": {"project_id": "p1", "host_id": "h1", "prompt": None}}
    )
    assert updated["health_check"] == {"project_id": "p1", "host_id": "h1", "prompt": None}
    assert updated["cpu_pct"] == 85.0
    assert updated["cpu_sustain_min"] == 10

    replaced = hub.put_settings({"health_check": {"project_id": "p2"}})
    assert replaced["health_check"] == {"project_id": "p2", "host_id": None, "prompt": None}
    assert hub.get_settings()["health_check"]["project_id"] == "p2"


def test_health_check_settings_validation(tmp_path: Path) -> None:
    """Bad payloads raise; a whitespace-only prompt becomes ``None``."""
    hub = SystemStatusHub(tmp_path, None)
    hub.put_settings({"health_check": {"project_id": "p1", "host_id": "h1", "prompt": "x"}})
    before = hub.get_settings()

    bad_values: list[object] = [
        5,
        {"foo": 1},
        {"project_id": 5},
        {"project_id": ""},
        {"project_id": "x" * 65},
        {"host_id": 5},
        {"host_id": ""},
        {"host_id": "x" * 65},
        {"prompt": 5},
        {"prompt": "x" * 20_001},
    ]
    for bad in bad_values:
        with pytest.raises(ValueError):
            hub.put_settings({"health_check": bad})
    assert hub.get_settings() == before

    cleared = hub.put_settings({"health_check": {"prompt": "  \n "}})
    assert cleared["health_check"] == {"project_id": None, "host_id": None, "prompt": None}


def test_health_check_settings_survive_restart(tmp_path: Path) -> None:
    """A persisted health_check is restored; an invalid one falls back."""
    hub = SystemStatusHub(tmp_path, None)
    stored = hub.put_settings(
        {"health_check": {"project_id": "p1", "host_id": "h1", "prompt": "custom"}}
    )
    system_status.write_settings(hub.settings_path, stored)
    hub.close()

    reloaded = SystemStatusHub(tmp_path, None)
    assert reloaded.get_settings()["health_check"] == {
        "project_id": "p1",
        "host_id": "h1",
        "prompt": "custom",
    }

    hub.settings_path.write_text(json.dumps({"cpu_pct": 70, "health_check": {"project_id": 5}}))
    repaired = SystemStatusHub(tmp_path, None)
    assert repaired.get_settings()["health_check"] == {
        "project_id": None,
        "host_id": None,
        "prompt": None,
    }
    assert repaired.get_settings()["cpu_pct"] == 70.0


def test_brief_happy_path(tmp_path: Path) -> None:
    """Brief carries header, finding, server maxima, host summaries and tables."""
    hub = SystemStatusHub(tmp_path, None)
    now = 1_750_000_000.0
    hub._server_points = [
        {
            "t": now - 3600.0,
            "cpu": 10.0,
            "rss": 100 * 1024 * 1024,
            "in_flight": 1,
            "websockets": 2,
            "req": 100,
            "err": 20,
            "load1": 0.5,
            "disk_pct": 95.0,
        },
        {
            "t": now - 60.0,
            "cpu": 20.0,
            "rss": 200 * 1024 * 1024,
            "in_flight": 5,
            "websockets": 3,
            "req": 50,
            "err": 1,
            "load1": 0.6,
            "disk_pct": 40.0,
        },
    ]
    hub._findings[(0, "host_a:cpu")] = {
        "id": "host_a:cpu",
        "target": "host_a",
        "kind": "cpu",
        "level": "amber",
        "since": now - 600.0,
        "detail": "cpu above 85% for 10 minutes",
        "top_session": "conv_a",
    }

    _connect(hub, host_id="host_a", now=now)
    hub.ingest(
        host_id="host_a",
        workspace_id=0,
        frame=_frame(
            cpu_pct=12.0,
            processes=[
                ResourceProcessRow(
                    pid=1,
                    ppid=0,
                    name="omnigent-daemon",
                    role="daemon",
                    session_id=None,
                    cpu_pct=0.5,
                    rss=50 * 1024 * 1024,
                    started_at=now - 11_520.0,
                ),
                ResourceProcessRow(
                    pid=2,
                    ppid=1,
                    name="hot-child",
                    role="child",
                    session_id="conv_a",
                    cpu_pct=40.0,
                    rss=10 * 1024 * 1024,
                    started_at=now - 60.0,
                ),
            ],
        ),
        now=now,
    )
    hub._entries[(0, "host_a")].points = [
        {
            "t": now - 120.0,
            "cpu_max": 95.0,
            "mem_used": 15 * _GIB,
            "mem_total": 16 * _GIB,
            "disk_pct": 80.0,
            "load1": 4.0,
            "top": [["conv_b", 10.0], ["conv_a", 2.0]],
        },
        {
            "t": now - 60.0,
            "cpu_max": 20.0,
            "mem_used": 8 * _GIB,
            "mem_total": 16 * _GIB,
            "disk_pct": 50.0,
            "load1": 1.0,
            "top": [["conv_b", 5.0], ["conv_c", 3.0]],
        },
    ]

    _connect(hub, host_id="host_b", now=now)
    hub.ingest(host_id="host_b", workspace_id=0, frame=_frame(cpu_pct=5.0), now=now)
    hub._entries[(0, "host_b")].points = [
        {
            "t": now - 60.0,
            "cpu_max": 30.0,
            "mem_used": 4 * _GIB,
            "mem_total": 16 * _GIB,
            "disk_pct": 20.0,
            "load1": 0.5,
            "top": [],
        }
    ]

    text = _brief(hub, now, host_versions={"host_a": "1.2.3", "host_b": "1.2.3"})

    assert text.startswith(
        f"Monitor snapshot, generated {system_status._format_utc(now)}, server 9.9.9\n"
    )
    assert "- [amber] host_a: cpu above 85% for 10 minutes" in text
    assert "top conv_a)" in text
    assert f"24h: max cpu 20.0% at {system_status._format_utc(now - 60.0)}" in text
    assert f"max in-flight 5 at {system_status._format_utc(now - 60.0)}" in text
    assert f"max disk 95.0% at {system_status._format_utc(now - 3600.0)}" in text
    assert "requests 150" in text
    assert "5xx 21 (14.0%)" in text
    assert text.count("version 1.2.3") == 2
    assert "coverage 2/1440 min" in text
    assert "max memory 93.8%" in text
    assert "top sessions: conv_b 15.0%, conv_c 3.0%, conv_a 2.0%" in text
    assert text.index("omnigent-daemon") < text.index("hot-child"), "roots come first"
    assert text.rstrip().endswith(
        "Only Omnigent's own processes are sampled; other processes on these machines "
        "are not listed."
    )


def test_brief_stays_under_the_byte_cap(tmp_path: Path) -> None:
    """3 hosts x 200 long-named root rows stay within 16 KiB with cap lines."""
    hub = SystemStatusHub(tmp_path, None)
    now = 1_750_000_000.0
    hub._server_points = [
        {
            "t": now - 60.0,
            "cpu": 10.0,
            "rss": 100 * 1024 * 1024,
            "in_flight": 1,
            "websockets": 1,
            "req": 10,
            "err": 0,
            "load1": 0.5,
            "disk_pct": 40.0,
        }
    ]
    hub._findings[(0, "host_a:cpu")] = {
        "id": "host_a:cpu",
        "target": "host_a",
        "kind": "cpu",
        "level": "amber",
        "since": now - 600.0,
        "detail": "cpu above 85% for 10 minutes",
        "top_session": None,
    }
    long_name = "p" * 60
    for host_id in ("host_a", "host_b", "host_c"):
        _connect(hub, host_id=host_id, now=now)
        rows = [
            ResourceProcessRow(
                pid=index,
                ppid=0,
                name=f"{long_name}{index}",
                role="runner",
                session_id=None,
                cpu_pct=float(index),
                rss=1024 * 1024,
                started_at=now - index,
            )
            for index in range(200)
        ]
        hub.ingest(host_id=host_id, workspace_id=0, frame=_frame(processes=rows), now=now)
        hub._entries[(0, host_id)].points = [
            {
                "t": now - 60.0,
                "cpu_max": 10.0,
                "mem_used": _GIB,
                "mem_total": 2 * _GIB,
                "disk_pct": 10.0,
                "load1": 0.1,
                "top": [["conv_a", 1.0]],
            }
        ]

    text = _brief(hub, now)

    assert len(text.encode("utf-8")) <= 16_384
    assert "Monitor snapshot, generated" in text
    assert "- [amber] host_a: cpu above 85%" in text
    assert "Server:" in text
    assert "24h: max cpu 10.0%" in text
    for host_id in ("host_a", "host_b", "host_c"):
        assert f"Host {host_id} (" in text
        assert f"Host {host_id} process table:" in text
    assert text.count("24h: max cpu") == 4, "server + one per host"
    assert text.count("coverage 1/1440 min") == 4, "server + one per host"
    assert text.count("top sessions: conv_a 1.0%") == 3
    assert text.count("rows not shown (cap)") >= 3
    assert text.rstrip().endswith(
        "Only Omnigent's own processes are sampled; other processes on these machines "
        "are not listed."
    )


def test_brief_caps_a_large_host_head(tmp_path: Path) -> None:
    """60 online hosts with no process rows still stay within 16 KiB."""
    hub = SystemStatusHub(tmp_path, None)
    now = 1_750_000_000.0
    hub._server_points = [
        {
            "t": now - 60.0,
            "cpu": 10.0,
            "rss": 100 * 1024 * 1024,
            "in_flight": 1,
            "websockets": 1,
            "req": 10,
            "err": 0,
            "load1": 0.5,
            "disk_pct": 40.0,
        }
    ]
    for index in range(60):
        host_id = f"h{index:02d}"
        _connect(hub, host_id=host_id, now=now)
        hub.ingest(host_id=host_id, workspace_id=0, frame=_frame(processes=[]), now=now)
        hub._entries[(0, host_id)].points = [
            {
                "t": now - 60.0,
                "cpu_max": 10.0,
                "mem_used": _GIB,
                "mem_total": 2 * _GIB,
                "disk_pct": 10.0,
                "load1": 0.1,
                "top": [["conv_a", 1.0]],
            }
        ]

    text = _brief(hub, now)

    assert len(text.encode("utf-8")) <= 16_384
    assert text.startswith("Monitor snapshot, generated ")
    assert "Server:" in text
    assert "Latest: cpu 10.0%" in text
    assert text.endswith("… brief truncated at the 16 KB cap\n")


def test_brief_offline_host_shows_only_the_sample_age(tmp_path: Path) -> None:
    """A host with no live tunnel gets one line and no metrics or table."""
    hub = SystemStatusHub(tmp_path, None)
    now = 1_750_000_000.0
    _connect(hub, host_id="host_a", now=now - 10_000.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(), now=now - 8100.0)
    _tick(hub, now - 8000.0)
    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name=None,
        conn_capabilities=None,
        now=now - 100.0,
    )

    text = _brief(hub, now, host_versions={"host_a": "1.2.3"})

    host_lines = [line for line in text.splitlines() if line.startswith("Host host_a")]
    assert len(host_lines) == 1
    assert "— offline, version 1.2.3, last sample 2h13m ago" in host_lines[0]
    assert "Host host_a process table:" not in text
    assert "Latest:" not in text


def test_brief_coverage_counts_distinct_minutes(tmp_path: Path) -> None:
    """300 one-minute points inside the window read as 300/1440."""
    hub = SystemStatusHub(tmp_path, None)
    now = 1_750_000_000.0
    _connect(hub, host_id="host_a", now=now - 100_000.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(), now=now - 200.0)
    points = [
        {
            "t": now - 60.0 * (index + 1),
            "cpu_max": 1.0,
            "mem_used": 1,
            "mem_total": 2,
            "disk_pct": 1.0,
            "load1": 0.1,
            "top": [],
        }
        for index in range(300)
    ]
    points.append(dict(points[0]))
    hub._entries[(0, "host_a")].points = points

    text = _brief(hub, now)

    assert "coverage 300/1440 min" in text
