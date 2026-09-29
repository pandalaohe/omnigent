"""Unit tests for the server-side system-status hub."""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

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
) -> HostResourceSnapshotFrame:
    """Build a realistic-enough snapshot frame for hub tests."""
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

    assert hub.tick(60.0) is False
    (point,) = hub._entries[(0, "host_a")].points
    assert point["t"] == 60.0
    assert point["cpu_avg"] == 30.0
    assert point["cpu_max"] == 40.0
    assert point["mem_used"] == 4 * _GIB
    assert point["mem_total"] == 16 * _GIB
    assert point["disk_pct"] == 40.0
    assert point["load1"] == 0.5
    assert point["top"] == [["conv_b", 6.0], ["conv_a", 3.0]]

    assert hub.tick(120.0) is False
    assert len(hub._entries[(0, "host_a")].points) == 1, "an empty minute adds no point"


def test_tick_trims_and_drops_a_stale_offline_target(tmp_path: Path) -> None:
    """A target offline past 24 h loses its points and then its entry."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(), now=1.0)
    hub.tick(60.0)
    assert len(hub._entries[(0, "host_a")].points) == 1

    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name=None,
        conn_capabilities=None,
        now=120.0,
    )
    hub.tick(26 * 60 * 60.0)

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

    assert hub.tick(160.0) is False, "under 120 s offline there is no finding"
    (entry,) = hub._entries.values()
    assert entry.points, "the point established before the disconnect survives"

    assert hub.tick(221.0) is True
    findings = hub._findings[(0, "host_a:offline")]
    assert findings["level"] == "red"
    assert findings["since"] == 220.0

    assert hub.tick(280.0) is False, "a still-holding finding does not re-announce"
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
    assert hub.tick(1000.0) is False
    assert hub._findings == {}

    hub.host_changed(
        host_id="host_b",
        workspace_id=0,
        owner="alice",
        name="old",
        conn_capabilities=[],
        now=1001.0,
    )
    assert hub.tick(2000.0) is False
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
        assert hub.tick((minute + 1) * 60.0) is False
    assert (0, "host_a:cpu") not in hub._findings

    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(cpu_pct=90.0), now=9 * 60.0 + 1)
    assert hub.tick(10 * 60.0) is True
    finding = hub._findings[(0, "host_a:cpu")]
    assert finding["level"] == "amber"
    assert finding["kind"] == "cpu"
    assert finding["id"] == "host_a:cpu"


def test_revision_bumps_only_when_the_finding_set_changes(tmp_path: Path) -> None:
    """Revision stays put while the same findings hold across ticks."""
    hub = SystemStatusHub(tmp_path, None)
    _connect(hub, host_id="host_a", now=0.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(runner_count=1), now=1.0)
    assert hub.tick(60.0) is False
    assert hub._revision == 0
    assert hub.tick(120.0) is False
    assert hub._revision == 0

    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner="alice",
        name=None,
        conn_capabilities=None,
        now=130.0,
    )
    assert hub.tick(260.0) is True, "a new red finding bumps the revision"
    assert hub._revision == 1
    assert hub.tick(320.0) is False
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
    hub.tick(60.0)

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
    hub.tick(300.0)
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
    """A corrupt history file is logged, ignored, and replaced on flush."""
    state_dir = tmp_path / "system-status"
    state_dir.mkdir(parents=True)
    (state_dir / "history.json").write_text("{not json")

    hub = SystemStatusHub(tmp_path, None)
    hub.load()
    assert hub._entries == {}
    assert hub._server_points == []

    _connect(hub, host_id="host_a", now=0.0)
    hub.ingest(host_id="host_a", workspace_id=0, frame=_frame(), now=1.0)
    hub.tick(60.0)
    hub.flush()
    payload = json.loads((state_dir / "history.json").read_text())
    assert payload["version"] == 1
    assert set(payload["hosts"]) == {"0:host_a"}


def test_settings_round_trip_and_validation(tmp_path: Path) -> None:
    """Thresholds persist and reject unknown keys / out-of-range numbers."""
    hub = SystemStatusHub(tmp_path, None)
    assert hub.get_settings() == {
        "cpu_pct": 85.0,
        "mem_pct": 90.0,
        "disk_pct": 90.0,
        "server_5xx_pct": 5.0,
    }

    updated = hub.put_settings({"cpu_pct": 70})
    assert updated["cpu_pct"] == 70.0
    assert updated["mem_pct"] == 90.0
    reloaded = SystemStatusHub(tmp_path, None)
    assert reloaded.get_settings()["cpu_pct"] == 70.0

    bad_settings: list[dict[str, object]] = [
        {"nope": 1},
        {"cpu_pct": True},
        {"cpu_pct": 0},
        {"cpu_pct": 101},
        {"cpu_pct": "x"},
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
