"""Host-side resource sampler for the system-status monitor.

Samples the host's machine metrics and the omnigent process tree — daemon,
zygote, runners, and the harness / tmux processes recorded in the runner
owner files — attributing every descendant to its session. Process discovery
uses each platform's native child list, never a whole process-table scan,
except the documented fallback when no native list exists.

:meth:`ResourceSampler.sample` is blocking and must run on one stable thread
(the host's dedicated executor) so CPU baselines are comparable; it never
raises.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psutil

from omnigent._platform import IS_POSIX
from omnigent.host.frames import (
    HostResourceSnapshotFrame,
    ResourceMachine,
    ResourceProcessRow,
)
from omnigent.runner.owner_file import read_owner_entries

_logger = logging.getLogger(__name__)

# Payload cap: all roots are kept; descendants are ranked by CPU until this
# many rows, and every kept row's ancestors are kept too.
_MAX_ROWS = 200
# A synthetic row folding the descendants the cap dropped under one parent.
_FOLDED_ROLE = "folded"

# With no native child list, one process-table walk is at most this frequent
# even while the server holds a fast-sampling lease.
_FALLBACK_TREE_INTERVAL_S = 60.0

_CHILD_BUFFER_SIZE = 1024

_OWNER_KINDS = ("harness", "tmux")


def child_pids(pid: int) -> list[int] | None:
    """Return *pid*'s direct children, or ``None`` when unsupported.

    Native per platform: Linux reads ``/proc/<pid>/task/<tid>/children`` for
    every thread; macOS calls ``libproc.proc_listchildpids``. ``None`` means
    the platform cannot list children (Windows, missing procfs children
    support, or a failed ``libproc`` load) and the caller must use its
    fallback.

    :param pid: Parent process id.
    :returns: Sorted child pids, or ``None`` when unsupported.
    """
    if sys.platform.startswith("linux"):
        return _linux_child_pids(pid)
    if sys.platform == "darwin":
        return _macos_child_pids(pid)
    return None


def _linux_child_pids(pid: int) -> list[int] | None:
    task_dir = Path(f"/proc/{pid}/task")
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return None
    found: set[int] = set()
    read_any = False
    for tid in tids:
        try:
            text = (task_dir / tid / "children").read_text(encoding="ascii")
        except OSError:
            continue
        read_any = True
        for token in text.split():
            try:
                found.add(int(token))
            except ValueError:
                continue
    if not read_any:
        # No children file at all: the kernel lacks CONFIG_PROC_CHILDREN (or
        # every thread raced away); the caller must take the fallback path.
        return None
    return sorted(found)


_libproc: ctypes.CDLL | None = None
_libproc_failed = False


def _load_libproc() -> ctypes.CDLL | None:
    """Load and bind ``libproc`` once; ``None`` when unavailable."""
    global _libproc, _libproc_failed
    if _libproc is not None or _libproc_failed:
        return _libproc
    try:
        library = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
        library.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        library.proc_listchildpids.restype = ctypes.c_int
    except Exception:  # noqa: BLE001 — any load/bind failure means fallback
        _logger.debug("libproc unavailable; resource sampling uses the fallback", exc_info=True)
        _libproc_failed = True
        return None
    _libproc = library
    return library


def _macos_child_pids(pid: int) -> list[int] | None:
    library = _load_libproc()
    if library is None:
        return None
    buffer = (ctypes.c_int * _CHILD_BUFFER_SIZE)()
    count = library.proc_listchildpids(pid, ctypes.byref(buffer), ctypes.sizeof(buffer))
    if count < 0:
        return None
    return [buffer[index] for index in range(min(count, _CHILD_BUFFER_SIZE))]


@dataclass
class _ProcSample:
    """Per-process CPU baseline, keyed by ``(pid, create_time)``."""

    cpu_total: float
    wall: float


@dataclass(frozen=True)
class _Root:
    """A root process and the attribution its whole subtree inherits."""

    role: str
    session_id: str | None


class ResourceSampler:
    """Builds :class:`HostResourceSnapshotFrame` samples for one host.

    :param data_dir: Host data directory; also the filesystem whose disk
        usage is reported.
    :param daemon_pid: The host daemon process (the sampler's own process).
    """

    def __init__(self, *, data_dir: Path, daemon_pid: int) -> None:
        self._data_dir = data_dir
        self._daemon_pid = daemon_pid
        # The caller measures thread_time() around sample + encode on the
        # sampler's thread and stores it here; each frame carries the value
        # from the PREVIOUS sample, so this sample cannot measure its own
        # full cost.
        self.last_cpu_ms = 0.0
        self._machine_cpu_baseline: tuple[float, float] | None = None
        self._daemon_rss_baseline: int | None = None
        self._proc_cache: dict[tuple[int, float], _ProcSample] = {}
        self._fresh_proc_cache: dict[tuple[int, float], _ProcSample] = {}
        self._native_supported: bool | None = None
        self._fallback_tree: tuple[float, dict[int, list[int]]] | None = None

    def sample(
        self,
        *,
        runner_sessions: dict[int, str | None],
        zygote_pid: int | None,
        interval_s: int,
    ) -> HostResourceSnapshotFrame:
        """Build one snapshot of the machine and the omnigent process tree.

        Blocking; never raises — a failing pid is skipped and a failing
        machine metric reads as ``0`` / ``None``.

        :param runner_sessions: Live runner pid → its primary session (which
            may be ``None``).
        :param zygote_pid: The runner zygote's pid, if one is running.
        :param interval_s: Sampling interval this snapshot is taken under.
        :returns: The snapshot frame.
        """
        sampled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            machine = self._machine_metrics()
            roots = self._collect_roots(runner_sessions, zygote_pid)
            self._fresh_proc_cache = {}
            try:
                rows = self._collect_rows(roots)
            finally:
                # Drop vanished pids' baselines: the fresh cache holds only
                # the pids that answered this sample.
                self._proc_cache = self._fresh_proc_cache
            return HostResourceSnapshotFrame(
                sampled_at=sampled_at,
                interval_s=interval_s,
                machine=machine,
                processes=_apply_payload_cap(rows, root_pids=set(roots)),
                runner_count=len(runner_sessions),
                sampler_cpu_ms=self.last_cpu_ms,
                monitor_rss_delta=self._monitor_rss_delta(),
            )
        except Exception:  # noqa: BLE001 — a monitor must never raise
            _logger.debug("resource sample failed; sending an empty snapshot", exc_info=True)
            return HostResourceSnapshotFrame(
                sampled_at=sampled_at,
                interval_s=interval_s,
                machine=ResourceMachine(
                    cpu_pct=0.0,
                    mem_used=0,
                    mem_total=0,
                    disk_used=0,
                    disk_total=0,
                    load1=None,
                ),
                processes=[],
                runner_count=len(runner_sessions),
                sampler_cpu_ms=self.last_cpu_ms,
                monitor_rss_delta=0,
            )

    def _collect_roots(
        self,
        runner_sessions: dict[int, str | None],
        zygote_pid: int | None,
    ) -> dict[int, _Root]:
        roots: dict[int, _Root] = {self._daemon_pid: _Root("daemon", None)}
        if zygote_pid is not None:
            roots[zygote_pid] = _Root("zygote", None)
        for pid, session_id in runner_sessions.items():
            roots[pid] = _Root("runner", session_id)
        # Owner records are written at spawn, so they win over the None a
        # zygote / runner child would inherit — the harness is a zygote
        # child, the tmux server has daemonized away.
        for pid, record in read_owner_entries(self._data_dir).items():
            if record.kind in _OWNER_KINDS:
                roots[pid] = _Root(record.kind, record.conversation_id)
        return roots

    def _collect_rows(self, roots: dict[int, _Root]) -> list[ResourceProcessRow]:
        rows: dict[int, ResourceProcessRow] = {}
        seen: set[int] = set(roots)
        queue: deque[tuple[int, str | None]] = deque()
        for pid, root in roots.items():
            row = self._build_row(pid, root.role, root.session_id)
            if row is not None:
                rows[pid] = row
            queue.append((pid, root.session_id))
        while queue:
            parent, session_id = queue.popleft()
            for child in self._children_of(parent):
                if child in seen:
                    continue
                seen.add(child)
                row = self._build_row(child, "child", session_id)
                if row is None or row.ppid != parent:
                    # A cached parent/child edge can be stale after pid reuse;
                    # a row whose real parent differs is not that child.
                    continue
                rows[child] = row
                queue.append((child, session_id))
        return list(rows.values())

    def _build_row(self, pid: int, role: str, session_id: str | None) -> ResourceProcessRow | None:
        try:
            process = psutil.Process(pid)
            create_time = process.create_time()
            times = process.cpu_times()
            cpu_total = float(times.user) + float(times.system)
            now = time.monotonic()
            key = (pid, create_time)
            previous = self._proc_cache.get(key)
            if previous is None:
                cpu_pct = 0.0
            else:
                elapsed = now - previous.wall
                cpu_pct = (
                    max(0.0, (cpu_total - previous.cpu_total) / elapsed * 100.0)
                    if elapsed > 0
                    else 0.0
                )
            self._fresh_proc_cache[key] = _ProcSample(cpu_total, now)
            return ResourceProcessRow(
                pid=pid,
                ppid=process.ppid(),
                name=process.name(),
                role=role,
                session_id=session_id,
                cpu_pct=cpu_pct,
                rss=process.memory_info().rss,
            )
        except Exception:  # noqa: BLE001 — skip the pid, keep the sample
            _logger.debug("resource sampler skipped pid %s", pid, exc_info=True)
            return None

    def _children_of(self, pid: int) -> list[int]:
        if self._native_children_available():
            return child_pids(pid) or []
        return self._fallback_children_map().get(pid, [])

    def _native_children_available(self) -> bool:
        if self._native_supported is None:
            try:
                supported = child_pids(self._daemon_pid) is not None
            except Exception:  # noqa: BLE001 — treat a broken probe as unsupported
                _logger.debug("native child-list probe failed; using fallback", exc_info=True)
                supported = False
            self._native_supported = supported
        return self._native_supported

    def _fallback_children_map(self) -> dict[int, list[int]]:
        cached = self._fallback_tree
        now = time.monotonic()
        if cached is not None and now - cached[0] < _FALLBACK_TREE_INTERVAL_S:
            return cached[1]
        mapping: dict[int, list[int]] = {}
        try:
            for child in psutil.Process(self._daemon_pid).children(recursive=True):
                mapping.setdefault(child.ppid(), []).append(child.pid)
        except Exception:  # noqa: BLE001 — an empty tree is a valid answer
            _logger.debug("fallback process-tree walk failed", exc_info=True)
            mapping = {}
        self._fallback_tree = (now, mapping)
        return mapping

    def _machine_metrics(self) -> ResourceMachine:
        cpu_pct = self._machine_cpu_pct()
        try:
            memory = psutil.virtual_memory()
            mem_total = int(memory.total)
            mem_used = mem_total - int(memory.available)
        except Exception:  # noqa: BLE001 — one metric must not fail the sample
            _logger.debug("resource sampler could not read memory", exc_info=True)
            mem_used = 0
            mem_total = 0
        try:
            usage = psutil.disk_usage(str(self._data_dir))
            disk_used = int(usage.used)
            disk_total = int(usage.total)
        except Exception:  # noqa: BLE001
            _logger.debug("resource sampler could not read disk", exc_info=True)
            disk_used = 0
            disk_total = 0
        load1: float | None = None
        if IS_POSIX:
            try:
                load1 = float(os.getloadavg()[0])
            except OSError:
                _logger.debug("resource sampler could not read load", exc_info=True)
                load1 = None
        return ResourceMachine(
            cpu_pct=cpu_pct,
            mem_used=mem_used,
            mem_total=mem_total,
            disk_used=disk_used,
            disk_total=disk_total,
            load1=load1,
        )

    def _machine_cpu_pct(self) -> float:
        try:
            busy, total = _cpu_busy_total(psutil.cpu_times())
        except Exception:  # noqa: BLE001
            _logger.debug("resource sampler could not read machine CPU", exc_info=True)
            return 0.0
        previous = self._machine_cpu_baseline
        self._machine_cpu_baseline = (busy, total)
        if previous is None:
            return 0.0
        busy_delta = busy - previous[0]
        total_delta = total - previous[1]
        if total_delta <= 0:
            return 0.0
        return max(0.0, min(100.0, busy_delta / total_delta * 100.0))

    def _monitor_rss_delta(self) -> int:
        try:
            rss = int(psutil.Process(self._daemon_pid).memory_info().rss)
        except Exception:  # noqa: BLE001
            _logger.debug("resource sampler could not read its own RSS", exc_info=True)
            return 0
        if self._daemon_rss_baseline is None:
            self._daemon_rss_baseline = rss
        return rss - self._daemon_rss_baseline


def _cpu_busy_total(times: object) -> tuple[float, float]:
    """Return ``(busy, total)`` CPU seconds from a psutil cpu_times tuple."""
    if not isinstance(times, tuple):
        return 0.0, 0.0
    values = [float(value) for value in times if isinstance(value, (int, float))]
    total = sum(values)
    if sys.platform.startswith("linux"):
        # On Linux guest / guest_nice are already counted in user / nice
        # (psutil's own _cpu_tot_time subtracts them), so summing every
        # field would count them twice.
        for field_name in ("guest", "guest_nice"):
            total -= float(getattr(times, field_name, 0.0))
    idle = float(getattr(times, "idle", 0.0)) + float(getattr(times, "iowait", 0.0))
    return total - idle, total


def _apply_payload_cap(
    rows: list[ResourceProcessRow], *, root_pids: set[int]
) -> list[ResourceProcessRow]:
    """Cap *rows* to :data:`_MAX_ROWS`, folding dropped descendants.

    All roots are kept; descendants are kept by CPU until the cap, and a
    kept row's ancestors are kept with it. Every remaining row contributes
    to one folded row under its nearest kept ancestor.

    :param rows: Rows in discovery order (roots first).
    :param root_pids: Pids that must never be dropped.
    :returns: Capped rows with folded summaries.
    """
    if len(rows) <= _MAX_ROWS:
        return rows
    by_pid = {row.pid: row for row in rows}
    kept: dict[int, ResourceProcessRow] = {row.pid: row for row in rows if row.pid in root_pids}
    descendants = sorted(
        (row for row in rows if row.pid not in root_pids),
        key=lambda row: row.cpu_pct,
        reverse=True,
    )
    for row in descendants:
        if row.pid in kept:
            continue
        missing: list[ResourceProcessRow] = []
        ancestor = by_pid.get(row.ppid)
        while ancestor is not None and ancestor.pid not in kept:
            missing.append(ancestor)
            ancestor = by_pid.get(ancestor.ppid)
        if len(kept) + 1 + len(missing) > _MAX_ROWS:
            continue
        kept[row.pid] = row
        for ancestor_row in missing:
            kept[ancestor_row.pid] = ancestor_row

    folded: dict[int, list[float]] = {}
    for row in rows:
        if row.pid in kept:
            continue
        parent_pid = _nearest_kept_ancestor(row, by_pid, kept)
        totals = folded.setdefault(parent_pid, [0.0, 0.0, 0.0])
        totals[0] += 1
        totals[1] += row.cpu_pct
        totals[2] += row.rss

    result = [row for row in rows if row.pid in kept]
    for parent_pid in sorted(folded):
        count, cpu_pct, rss = folded[parent_pid]
        parent = kept.get(parent_pid)
        result.append(
            ResourceProcessRow(
                pid=0,
                ppid=parent_pid,
                name=f"{int(count)} other processes",
                role=_FOLDED_ROLE,
                session_id=parent.session_id if parent is not None else None,
                cpu_pct=cpu_pct,
                rss=int(rss),
            )
        )
    return result


def _nearest_kept_ancestor(
    row: ResourceProcessRow,
    by_pid: dict[int, ResourceProcessRow],
    kept: dict[int, ResourceProcessRow],
) -> int:
    """Walk up *row*'s ppid chain to the closest kept ancestor's pid."""
    parent_pid = row.ppid
    while parent_pid not in kept:
        parent = by_pid.get(parent_pid)
        if parent is None:
            break
        parent_pid = parent.ppid
    return parent_pid
