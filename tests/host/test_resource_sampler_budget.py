"""Host resource sampler budget test.

The feature's hard constraint: 60 samples + encodes over a 40-process tree
(10 simulated minutes at fast cadence) must cost ≤ 0.5 % of one core — 3.0 s
of 600 s — and grow the host's RSS by ≤ 30 MB, on the native child-list path
and on the 60 s fallback path (what a Windows host does). The measurements
run in a fresh subprocess so the sampler's own caches do not pollute pytest's
RSS. Fails loudly when either bound is exceeded.
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

from omnigent.host import resource_sampler
from omnigent.host.frames import HostResourceSamplingFrame, encode_host_frame
from omnigent.host.resource_sampler import ResourceSampler

PARENT_CODE = (
    "import subprocess, sys, time\n"
    "kids = [subprocess.Popen([sys.executable, '-c', "
    "'import time; time.sleep(600)']) for _ in range(39)]\n"
    "time.sleep(600)\n"
)


def measure(make_sampler, runner_sessions, on_sample=None):
    before_rss = psutil.Process().memory_info().rss
    before_cpu = time.thread_time()
    sampler = make_sampler()
    for _ in range(60):
        if on_sample is not None:
            on_sample()
        frame = sampler.sample(
            runner_sessions=runner_sessions,
            zygote_pid=None,
            interval_s=10,
        )
        encode_host_frame(frame)
    total_cpu_s = time.thread_time() - before_cpu
    after_rss = psutil.Process().memory_info().rss
    return {"total_cpu_s": total_cpu_s, "rss_delta": after_rss - before_rss}


class _Clock:
    def __init__(self):
        self.value = 1000.0

    def monotonic(self):
        return self.value

    def __getattr__(self, name):
        return getattr(time, name)


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
    # encode_host_frame imports the telemetry runtime on its first call
    # (~50 MB); the host pays that while connecting, long before its first
    # snapshot, so warm it here and measure the sampler itself.
    encode_host_frame(HostResourceSamplingFrame(interval_s=10, lease_s=40))

    runner_sessions = {parent.pid: "conv_budget"}
    native = measure(
        lambda: ResourceSampler(data_dir=data_dir, daemon_pid=os.getpid()),
        runner_sessions,
    )

    # Windows path: no native child list, one table snapshot per 60 s. Step
    # the sampler's clock 10 s per sample so 60 samples exercise ~10 rescans.
    clock = _Clock()

    def advance_clock():
        clock.value += 10.0

    resource_sampler.time = clock
    resource_sampler.child_pids = lambda pid: None
    fallback = measure(
        lambda: ResourceSampler(data_dir=data_dir, daemon_pid=os.getpid()),
        runner_sessions,
        on_sample=advance_clock,
    )
    print(json.dumps({"native": native, "fallback": fallback}))
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
    """60 samples + encodes of a 40-process tree fit the budget on both paths."""
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(repo_root),
    )

    assert result.returncode == 0, result.stderr
    measured = json.loads(result.stdout.strip().splitlines()[-1])
    for name in ("native", "fallback"):
        run = measured[name]
        assert run["total_cpu_s"] <= _CPU_BUDGET_S, (
            f"{name} resource sampler used {run['total_cpu_s']:.3f}s CPU for 60 "
            f"samples (budget {_CPU_BUDGET_S:.3f}s)"
        )
        assert run["rss_delta"] <= _RSS_BUDGET_BYTES, (
            f"{name} resource sampler grew RSS by {run['rss_delta']} bytes "
            f"(budget {_RSS_BUDGET_BYTES})"
        )
