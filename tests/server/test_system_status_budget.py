"""Budget guard: the system-status hub must cost almost nothing.

Runs the approved server workload in a child process — 10 hosts x 60
snapshots (40 process rows each), 10 ticks, one history payload build +
write and 60 admin views — and asserts the measured process CPU and RSS
growth stay inside the approved budget (0.5 % of one core over 600 s; 20 MB
RSS). The test fails when either number is exceeded.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

from omnigent.host.frames import (
    CAP_RESOURCE_SNAPSHOT,
    HostResourceSnapshotFrame,
    ResourceMachine,
    ResourceProcessRow,
)
from omnigent.server.system_status import SystemStatusHub

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Keep the child's imports outside the timed region: the budget measures the
# monitor workload, not interpreter startup.
_CHILD = r"""
import json
import tempfile
import time
from pathlib import Path

import psutil

from omnigent.host.frames import (
    HostResourceSnapshotFrame,
    ResourceMachine,
    ResourceProcessRow,
)
from omnigent.server.system_status import SystemStatusHub, write_history

GIB = 1024 * 1024 * 1024


def frame(host_index, seq):
    processes = [
        ResourceProcessRow(
            pid=1000 + row,
            ppid=999,
            name=f"proc{row}",
            role="child",
            session_id=f"conv_{row % 5}",
            cpu_pct=float(row) / 20.0,
            rss=8 * 1024 * 1024,
        )
        for row in range(40)
    ]
    return HostResourceSnapshotFrame(
        sampled_at="2026-09-29T09:25:00+00:00",
        interval_s=10,
        machine=ResourceMachine(
            cpu_pct=30.0 + (host_index % 5),
            mem_used=8 * GIB,
            mem_total=32 * GIB,
            disk_used=40 * GIB,
            disk_total=200 * GIB,
            load1=1.0,
        ),
        processes=processes,
        runner_count=5,
        sampler_cpu_ms=0.4,
        monitor_rss_delta=4096,
    )


def main():
    tmp = tempfile.mkdtemp(prefix="system-status-budget-")
    rss_before = psutil.Process().memory_info().rss
    started = time.process_time()

    hub = SystemStatusHub(Path(tmp), None)
    for host_index in range(10):
        hub.host_changed(
            host_id=f"host{host_index}",
            workspace_id=0,
            owner="admin",
            name=f"host{host_index}",
            conn_capabilities=["resource_snapshot"],
            now=0.0,
        )
    for minute in range(10):
        base = minute * 60.0
        for host_index in range(10):
            for step in range(6):
                now = base + step * 10.0
                hub.ingest(
                    host_id=f"host{host_index}",
                    workspace_id=0,
                    frame=frame(host_index, step),
                    now=now,
                )
        hub.tick(base + 60.0, server_disk_pct=0.0)
    write_history(hub.history_path, hub.history_payload())
    for _ in range(60):
        hub.view(
            user_id="admin",
            is_admin=True,
            own_host_ids=set(),
            own_offline_hosts=[],
            workspace_id=0,
            summary=False,
        )

    cpu_s = time.process_time() - started
    rss_delta = psutil.Process().memory_info().rss - rss_before
    print(json.dumps({"cpu_s": cpu_s, "rss_delta": rss_delta}))


main()
"""


def test_system_status_hub_budget() -> None:
    """10 hosts x 60 snapshots + 10 ticks + flush + 60 views stays in budget."""
    result = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])

    assert payload["cpu_s"] <= 0.005 * 600, (
        f"system-status hub CPU budget exceeded: {payload['cpu_s']:.3f}s > 3.0s"
    )
    assert payload["rss_delta"] <= 20 * 1024 * 1024, (
        f"system-status hub RSS budget exceeded: {payload['rss_delta']} bytes > 20 MiB"
    )


def _brief_budget_frame(host_index: int) -> HostResourceSnapshotFrame:
    long_name = "p" * 60
    processes = [
        ResourceProcessRow(
            pid=1000 + row,
            ppid=999,
            name=f"{long_name}{row}",
            role="runner",
            session_id=None,
            cpu_pct=float(row),
            rss=8 * 1024 * 1024,
        )
        for row in range(200)
    ]
    return HostResourceSnapshotFrame(
        sampled_at="2026-09-29T09:25:00+00:00",
        interval_s=60,
        machine=ResourceMachine(
            cpu_pct=30.0 + host_index,
            mem_used=8 * 1024 * 1024 * 1024,
            mem_total=32 * 1024 * 1024 * 1024,
            disk_used=40 * 1024 * 1024 * 1024,
            disk_total=200 * 1024 * 1024 * 1024,
            load1=1.0,
        ),
        processes=processes,
        runner_count=1,
        sampler_cpu_ms=0.4,
        monitor_rss_delta=4096,
    )


def test_brief_cpu_budget(tmp_path: Path) -> None:
    """Scenario 8: 3 hosts x 200 rows + 3 x 1440 + 1440 points <= 50 ms CPU."""
    hub = SystemStatusHub(tmp_path, None)
    now = 1_750_000_000.0
    hub._server_points = [
        {
            "t": now - 60.0 * (index + 1),
            "cpu": float(index % 100),
            "rss": 100 * 1024 * 1024,
            "in_flight": index % 10,
            "websockets": 1,
            "req": 10,
            "err": index % 3,
            "load1": 0.5,
            "disk_pct": 40.0,
        }
        for index in range(1440)
    ]
    for host_index in range(3):
        host_id = f"host{host_index}"
        hub.host_changed(
            host_id=host_id,
            workspace_id=0,
            owner="admin",
            name=host_id,
            conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
            now=now,
        )
        hub.ingest(
            host_id=host_id,
            workspace_id=0,
            frame=_brief_budget_frame(host_index),
            now=now,
        )
        hub._entries[(0, host_id)].points = [
            {
                "t": now - 60.0 * (index + 1),
                "cpu_max": float(index % 100),
                "mem_used": 8 * 1024 * 1024 * 1024,
                "mem_total": 32 * 1024 * 1024 * 1024,
                "disk_pct": 40.0,
                "load1": 1.0,
                "top": [["conv_a", 1.0], ["conv_b", 2.0], ["conv_c", 3.0]],
            }
            for index in range(1440)
        ]

    def one_call() -> str:
        return hub.brief(
            now=now,
            workspace_id=0,
            own_offline_hosts=[],
            host_versions={},
            server_version="0.16.0.dev0",
        )

    one_call()
    times = []
    for _ in range(20):
        started = time.process_time()
        text = one_call()
        times.append(time.process_time() - started)

    assert len(text.encode("utf-8")) <= 16_384
    median = statistics.median(times)
    assert median <= 0.050, f"brief CPU budget exceeded: {median * 1000:.1f} ms > 50 ms"
