"""Host resource sampler budget test.

The feature's hard constraint: 60 samples + encodes over a 40-process tree
(10 simulated minutes at fast cadence) must cost ≤ 0.5 % of one core — 3.0 s
of 600 s — and grow the host's RSS by ≤ 30 MB. The measurements run in a
fresh subprocess so the sampler's own caches do not pollute pytest's RSS.
Fails loudly when either bound is exceeded.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_CPU_BUDGET_S = 0.005 * 600.0
_RSS_BUDGET_BYTES = 30 * 1024 * 1024

_SCRIPT = r"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

from omnigent.host.frames import encode_host_frame
from omnigent.host.resource_sampler import ResourceSampler

PARENT_CODE = (
    "import subprocess, sys, time\n"
    "kids = [subprocess.Popen([sys.executable, '-c', "
    "'import time; time.sleep(600)']) for _ in range(39)]\n"
    "time.sleep(600)\n"
)

parent = subprocess.Popen([sys.executable, "-c", PARENT_CODE])
try:
    deadline = time.time() + 15.0
    while time.time() < deadline:
        try:
            if len(psutil.Process(parent.pid).children(recursive=True)) >= 39:
                break
        except psutil.Error:
            pass
        time.sleep(0.05)

    data_dir = Path(tempfile.mkdtemp(prefix="resmon-budget-"))
    sampler = ResourceSampler(data_dir=data_dir, daemon_pid=os.getpid())
    # encode_host_frame imports the telemetry runtime on its first call
    # (~50 MB); the host pays that while connecting, long before its first
    # snapshot, so warm it here and measure the sampler itself.
    encode_host_frame(
        sampler.sample(
            runner_sessions={parent.pid: "conv_budget"},
            zygote_pid=None,
            interval_s=10,
        )
    )
    before_rss = psutil.Process().memory_info().rss
    before_cpu = time.thread_time()
    for _ in range(60):
        frame = sampler.sample(
            runner_sessions={parent.pid: "conv_budget"},
            zygote_pid=None,
            interval_s=10,
        )
        encode_host_frame(frame)
    total_cpu_s = time.thread_time() - before_cpu
    after_rss = psutil.Process().memory_info().rss
    print(
        json.dumps(
            {
                "total_cpu_s": total_cpu_s,
                "rss_delta": after_rss - before_rss,
            }
        )
    )
finally:
    try:
        for child in psutil.Process(parent.pid).children(recursive=True):
            child.kill()
    except psutil.Error:
        pass
    parent.kill()
    parent.wait(timeout=10)
"""


def test_sampler_stays_within_cpu_and_memory_budget() -> None:
    """60 samples + encodes of a 40-process tree fit the approved budget."""
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        timeout=240,
        cwd=str(repo_root),
    )

    assert result.returncode == 0, result.stderr
    measured = json.loads(result.stdout.strip().splitlines()[-1])
    assert measured["total_cpu_s"] <= _CPU_BUDGET_S, (
        f"resource sampler used {measured['total_cpu_s']:.3f}s CPU for 60 samples "
        f"(budget {_CPU_BUDGET_S:.3f}s)"
    )
    assert measured["rss_delta"] <= _RSS_BUDGET_BYTES, (
        f"resource sampler grew RSS by {measured['rss_delta']} bytes (budget {_RSS_BUDGET_BYTES})"
    )
