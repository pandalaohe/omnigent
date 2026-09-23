"""Best-effort machine-global cleanup owned by the host."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import stat
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from omnigent.debug_logging import debug_event

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock.
    fcntl = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

_DEFAULT_STARTUP_DELAY_S = 3.0
_DEFAULT_LOCK_RETRY_S = 0.5
# A long-lived host sees no runner lifecycle events for hours, so a periodic
# self-trigger keeps orphan cleanup running while the host stays up.
_DEFAULT_PERIODIC_INTERVAL_S = 600.0

# ── Runner-log retention ─────────────────────────────────
#
# A runner log has several writers holding O_APPEND fds (the runner and its
# harness child) and raw stdout/stderr that bypasses the logging module, so a
# logging-handler rotation cannot bound it. Rotation here is copytruncate —
# copy to ``<name>.1`` then truncate in place — which keeps every existing
# writer appending at the new end.

_RUNNER_LOG_COPYTRUNCATE_BYTES = 100 * 1024 * 1024
_RUNNER_LOG_ARCHIVES_KEEP = 4
_RUNNER_SESSION_LOG_TOTAL_BYTES = 500 * 1024 * 1024
_RUNNER_LOG_MAX_AGE_S = 30 * 24 * 60 * 60
_RUNNER_LOG_DIR_TOTAL_BYTES = 3 * 1024 * 1024 * 1024
# A log touched this recently may belong to a runner owned by another daemon
# or by the CLI sharing the data directory, so it must never be deleted.
_RUNNER_LOG_LIVE_MTIME_S = 60 * 60

# ``runner-<session-slug>-<ts>.log[.N]``; the session slug is the sanitized
# session id (see ``_handle_launch``), so it groups a session's logs across
# relaunches and rotations.
_RUNNER_LOG_NAME_RE = re.compile(
    r"^runner-(?P<session>.+?)-(?P<ts>\d{8}-\d{6}-\d{6})\.log(?:\.(?P<archive>\d+))?$"
)

# Runner-log runaway detection (see :class:`RunnerLogRunawayTracker`).
_RUNNER_LOG_RUNAWAY_BYTES = 5 * 1024 * 1024
_RUNNER_LOG_RUNAWAY_WINDOW_S = 60 * 60

MaintenanceStage = tuple[str, Callable[[], Awaitable[object]]]
_LockOutcome = Literal["acquired", "busy", "failed"]
_RunOutcome = Literal["completed", "busy", "failed"]


async def _run_sync_stage(call: Callable[[], object]) -> object:
    """Do not release the janitor lock while its worker thread still runs."""
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await task
        raise


@contextmanager
def _maintenance_lock(path: Path) -> Iterator[_LockOutcome]:
    """Yield the outcome of the non-blocking cleanup lock attempt."""
    if fcntl is None:
        yield "acquired"
        return
    fd: int | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if fd is not None:
            os.close(fd)
        yield "busy"
        return
    except OSError:
        _logger.warning("host maintenance lock acquisition failed", exc_info=True)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        yield "failed"
        return
    try:
        yield "acquired"
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def _live_runner_log(path: Path, live_paths: set[Path], now: float) -> bool:
    """Return whether *path* may still be written by a runner.

    :param path: Candidate log file under ``<data-dir>/logs/runner``.
    :param live_paths: Log paths of this host process's live runners.
    :param now: Epoch seconds of the sweep.
    :returns: ``True`` for a live runner's current log or a recently
        modified file (a runner owned by another daemon or the CLI).
    """
    if path in live_paths:
        return True
    try:
        mtime = path.stat().st_mtime
    except OSError:
        # Unreadable now: treat as live so the sweep never deletes blind.
        return True
    return now - mtime < _RUNNER_LOG_LIVE_MTIME_S


def _runner_log_files(log_dir: Path) -> list[Path]:
    """List regular log files under *log_dir*, tolerating a missing dir."""
    try:
        entries = list(log_dir.iterdir())
    except FileNotFoundError:
        return []
    except OSError:
        _logger.warning("runner log sweep could not list %s", log_dir, exc_info=True)
        return []
    return [path for path in entries if path.is_file() and not path.is_symlink()]


def _log_stat(path: Path) -> os.stat_result | None:
    """Return a file's identity, mtime, and size, or ``None`` when gone."""
    try:
        return path.lstat()
    except OSError:
        return None


def _archive_path(path: Path, index: int) -> Path:
    """Return the ``.N`` rotated sibling of *path*."""
    return path.with_name(f"{path.name}.{index}")


def _archive_index(path: Path) -> int | None:
    """Return the rotation index when *path* is an ``.N`` archive."""
    match = re.search(r"\.(?P<index>\d+)$", path.name)
    return int(match.group("index")) if match is not None else None


def _copytruncate_runner_log(path: Path, now: float) -> tuple[int, tuple[int, int]]:
    """Copy *path* to a temp file, shift archives, then truncate it.

    :param path: Live log file over the copytruncate threshold.
    :raises OSError: When the copy or truncate fails, e.g. a Windows
        sharing violation on a file another process holds open.
    """
    archives: dict[Path, os.stat_result | None] = {}
    for index in range(1, _RUNNER_LOG_ARCHIVES_KEEP + 1):
        archive = _archive_path(path, index)
        try:
            info = archive.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and not _safe_runner_log(archive, info, set(), now):
            raise OSError(f"unsafe runner log archive: {archive}")
        archives[archive] = info

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as target, path.open("rb") as source:
            source_info = os.fstat(source.fileno())
            shutil.copyfileobj(source, target)
        # Preserve the source mtime for the age and oldest-first rules.
        shutil.copystat(path, temp)
        copied_size = temp.stat().st_size
        for archive, info in archives.items():
            if not _safe_runner_log(archive, info, set(), now):
                raise OSError(f"unsafe runner log archive: {archive}")
        with contextlib.suppress(FileNotFoundError):
            _archive_path(path, _RUNNER_LOG_ARCHIVES_KEEP).unlink()
        for index in range(_RUNNER_LOG_ARCHIVES_KEEP - 1, 0, -1):
            source = _archive_path(path, index)
            if source.exists():
                os.replace(source, _archive_path(path, index + 1))
        os.replace(temp, _archive_path(path, 1))
        os.truncate(path, 0)
        return copied_size, (source_info.st_dev, source_info.st_ino)
    finally:
        temp.unlink(missing_ok=True)


def _safe_runner_log(
    path: Path,
    snapshot: os.stat_result | None,
    live: set[Path],
    now: float,
    min_age_s: float = _RUNNER_LOG_LIVE_MTIME_S,
) -> bool:
    if path in live:
        return False
    try:
        current = path.lstat()
    except FileNotFoundError:
        return snapshot is None
    return snapshot is not None and (
        stat.S_ISREG(current.st_mode)
        and (current.st_dev, current.st_ino) == (snapshot.st_dev, snapshot.st_ino)
        and now - current.st_mtime >= min_age_s
    )


def _unlink_runner_log(
    path: Path,
    snapshot: os.stat_result,
    live: set[Path],
    now: float,
    counts: dict[str, int],
    *,
    min_age_s: float = _RUNNER_LOG_LIVE_MTIME_S,
) -> None:
    """Delete one non-live log, counting failures instead of raising."""
    try:
        if not _safe_runner_log(path, snapshot, live, now, min_age_s):
            return
        path.unlink()
    except OSError:
        _logger.debug("runner log delete failed: %s", path, exc_info=True)
        return
    counts["deleted"] += 1


def sweep_runner_logs(
    log_dir: Path,
    *,
    live_paths: Collection[Path] = (),
    now: float | None = None,
    on_rotated: Callable[[Path, int, tuple[int, int]], None] | None = None,
) -> dict[str, int]:
    """Apply the runner-log retention policy to *log_dir*.

    Rules, in order:

    1. A live file over ``_RUNNER_LOG_COPYTRUNCATE_BYTES`` is copied to
       ``.1`` (shifting older archives, keeping at most
       ``_RUNNER_LOG_ARCHIVES_KEEP`` per live file) and truncated to zero.
       A copy or truncate failure is logged and the file skipped.
    2. A session over ``_RUNNER_SESSION_LOG_TOTAL_BYTES`` across its live
       file and archives loses its oldest non-live files until it is under.
    3. A non-live file older than ``_RUNNER_LOG_MAX_AGE_S`` is deleted.
    4. While the directory exceeds ``_RUNNER_LOG_DIR_TOTAL_BYTES``, non-live
       files are deleted oldest-mtime-first.

    "Live" means the file is in *live_paths* (this host's current runner
    logs) or its mtime is within ``_RUNNER_LOG_LIVE_MTIME_S``. Live files
    are never deleted, only copytruncated.

    :param log_dir: ``<data-dir>/logs/runner``.
    :param live_paths: Log paths of this host process's live runners.
    :param now: Epoch seconds for the age comparisons; ``None`` uses the
        wall clock.
    :param on_rotated: Receives the live path, copied byte count, and source
        file identity after a successful copytruncate.
    :returns: Counts, e.g.
        ``{"copied": 1, "truncated": 1, "deleted": 2, "failed": 0}``.
    """
    now = time.time() if now is None else now
    live = {Path(path) for path in live_paths}
    counts = {"copied": 0, "truncated": 0, "deleted": 0, "failed": 0}

    for path in _runner_log_files(log_dir):
        # An archive is never a live file, even though the copy that created
        # it inherited the source's recent mtime; rotating it again would
        # cascade the same file into ``.1.1``, ``.1.1.1``, ...
        if _archive_index(path) is not None:
            continue
        if not _live_runner_log(path, live, now):
            continue
        info = _log_stat(path)
        if info is None or info.st_size <= _RUNNER_LOG_COPYTRUNCATE_BYTES:
            continue
        try:
            copied_size, file_id = _copytruncate_runner_log(path, now)
        except OSError:
            # Expected on Windows, where an open log cannot be truncated.
            _logger.warning("runner log copytruncate failed: %s", path, exc_info=True)
            counts["failed"] += 1
            continue
        counts["copied"] += 1
        counts["truncated"] += 1
        if on_rotated is not None:
            on_rotated(path, copied_size, file_id)

    # Rule 1 changed sizes and archive names; re-list for the size rules.
    sessions: dict[str, list[Path]] = {}
    for path in _runner_log_files(log_dir):
        match = _RUNNER_LOG_NAME_RE.match(path.name)
        if match is not None:
            sessions.setdefault(match.group("session"), []).append(path)

    for paths in sessions.values():
        stats = [(path, info) for path in paths if (info := _log_stat(path)) is not None]
        total = sum(info.st_size for _path, info in stats)
        if total <= _RUNNER_SESSION_LOG_TOTAL_BYTES:
            continue
        oldest_first = sorted(
            ((path, info) for path, info in stats if not _live_runner_log(path, live, now)),
            key=lambda item: item[1].st_mtime,
        )
        for path, info in oldest_first:
            if total <= _RUNNER_SESSION_LOG_TOTAL_BYTES:
                break
            before = counts["deleted"]
            _unlink_runner_log(path, info, live, now, counts)
            if counts["deleted"] > before:
                total -= info.st_size

    for path in _runner_log_files(log_dir):
        if _live_runner_log(path, live, now):
            continue
        info = _log_stat(path)
        if info is None:
            continue
        if now - info.st_mtime > _RUNNER_LOG_MAX_AGE_S:
            _unlink_runner_log(path, info, live, now, counts, min_age_s=_RUNNER_LOG_MAX_AGE_S)

    stats = [
        (path, info)
        for path in _runner_log_files(log_dir)
        if (info := _log_stat(path)) is not None
    ]
    total = sum(info.st_size for _path, info in stats)
    if total > _RUNNER_LOG_DIR_TOTAL_BYTES:
        oldest_first = sorted(
            ((path, info) for path, info in stats if not _live_runner_log(path, live, now)),
            key=lambda item: item[1].st_mtime,
        )
        for path, info in oldest_first:
            if total <= _RUNNER_LOG_DIR_TOTAL_BYTES:
                break
            before = counts["deleted"]
            _unlink_runner_log(path, info, live, now, counts)
            if counts["deleted"] > before:
                total -= info.st_size

    return counts


class RunnerLogRunawayTracker:
    """Detect runners whose logs grow faster than the runaway threshold.

    A host-owned state machine: the caller samples each live runner's log
    size on a fixed cadence and feeds the samples here. Sizes accumulate into
    a cumulative byte counter, so an in-place copytruncate — which resets the
    file size to zero — never resets the measured rate; growth is measured
    over a sliding window. A runner is reported once per crossing: after a
    report, the tracker re-arms only when the windowed bytes fall back to or
    below the threshold.
    """

    def __init__(
        self,
        *,
        window_s: float = _RUNNER_LOG_RUNAWAY_WINDOW_S,
        threshold_bytes: int = _RUNNER_LOG_RUNAWAY_BYTES,
    ) -> None:
        self._window_s = window_s
        self._threshold_bytes = threshold_bytes
        self._samples: dict[str, deque[tuple[float, int]]] = {}
        self._last_sizes: dict[str, int] = {}
        self._file_ids: dict[str, tuple[int, int]] = {}
        self._pre_rotation_sizes: dict[str, int] = {}
        self._reported: set[str] = set()

    def observe(
        self, runner_id: str, size_bytes: int, now: float, file_id: tuple[int, int] | None = None
    ) -> int | None:
        """Record one log-size sample.

        :param runner_id: Runner whose log was sampled.
        :param size_bytes: Current log size in bytes.
        :param now: Sample time in seconds, monotonic per caller.
        :returns: The windowed ``bytes_last_hour`` when this sample crosses
            the threshold and should be reported, else ``None``.
        """
        samples = self._samples.setdefault(runner_id, deque())
        cumulative = samples[-1][1] if samples else 0
        if file_id is not None:
            if runner_id in self._file_ids and self._file_ids[runner_id] != file_id:
                self._last_sizes.pop(runner_id, None)
                self._pre_rotation_sizes.pop(runner_id, None)
            self._file_ids[runner_id] = file_id
        previous = self._last_sizes.get(runner_id)
        if previous is None:
            # First sighting: existing content is not this window's output.
            delta = 0
        elif size_bytes >= previous:
            delta = size_bytes - previous
        else:
            # Copytruncate removed the file's content; those bytes are
            # already in the counter, and the current size is output written
            # since the truncate.
            self._pre_rotation_sizes.setdefault(runner_id, previous)
            delta = size_bytes
        cumulative += delta
        samples.append((now, cumulative))
        self._last_sizes[runner_id] = size_bytes
        cutoff = now - self._window_s
        while len(samples) > 1 and samples[1][0] <= cutoff:
            samples.popleft()

        bytes_last_hour = cumulative - samples[0][1]
        if bytes_last_hour > self._threshold_bytes:
            if runner_id in self._reported:
                return None
            self._reported.add(runner_id)
            return bytes_last_hour
        self._reported.discard(runner_id)
        return None

    def note_rotated(
        self,
        runner_id: str,
        copied_size: int,
        now: float,
        file_id: tuple[int, int] | None = None,
    ) -> None:
        """Account for bytes copied after the last sample and reset live size."""
        if file_id is not None and self._file_ids.get(runner_id) != file_id:
            return
        previous = self._pre_rotation_sizes.pop(runner_id, None)
        sampled_after_rotation = previous is not None
        if previous is None:
            previous = self._last_sizes.get(runner_id)
        if previous is None:
            return
        samples = self._samples[runner_id]
        samples.append((now, samples[-1][1] + max(0, copied_size - previous)))
        if not sampled_after_rotation:
            self._last_sizes[runner_id] = 0

    def retain(self, runner_ids: Collection[str]) -> None:
        """Drop state for runners this host no longer owns.

        :param runner_ids: Runner ids still tracked by the caller.
        """
        for runner_id in [rid for rid in self._samples if rid not in runner_ids]:
            del self._samples[runner_id]
            self._last_sizes.pop(runner_id, None)
            self._file_ids.pop(runner_id, None)
            self._pre_rotation_sizes.pop(runner_id, None)
            self._reported.discard(runner_id)


class HostMaintenanceJanitor:
    """Coalesce host lifecycle triggers into background cleanup passes."""

    def __init__(
        self,
        *,
        stages: Sequence[MaintenanceStage],
        lock_path: Path,
        startup_delay_s: float = _DEFAULT_STARTUP_DELAY_S,
        lock_retry_s: float = _DEFAULT_LOCK_RETRY_S,
        periodic_interval_s: float = _DEFAULT_PERIODIC_INTERVAL_S,
        stage_skip_reasons: Mapping[str, Collection[str]] | None = None,
    ) -> None:
        self._stages = tuple(stages)
        self._lock_path = lock_path
        self._startup_delay_s = startup_delay_s
        self._lock_retry_s = lock_retry_s
        self._periodic_interval_s = periodic_interval_s
        self._stage_skip_reasons = {
            stage_name: frozenset(reasons)
            for stage_name, reasons in (stage_skip_reasons or {}).items()
        }
        self._pending_reasons: set[str] = set()
        self._startup_task: asyncio.Task[None] | None = None
        self._periodic_task: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False

    @classmethod
    def for_host(
        cls,
        *,
        harness_tmp_parent: Path | None = None,
        live_runner_log_paths: Callable[[], Collection[Path]] | None = None,
        on_runner_log_rotated: Callable[[Path, int, tuple[int, int]], None] | None = None,
    ) -> HostMaintenanceJanitor:
        """Build the machine-global cleanup stages for a host daemon.

        :param harness_tmp_parent: Override for the harness temp parent.
        :param live_runner_log_paths: Returns the log paths of this host's
            live runners, so the runner-log sweep never deletes a file a
            runner still writes. The janitor cannot see the host's runner
            set itself, so the owner passes this in. Resolved on the caller's
            thread (the event loop) before the sweep runs off-loop.
        """
        from omnigent.process_logging import data_dir, process_log_dir
        from omnigent.runtime.harnesses.paths import (
            absolute_harness_tmp_parent,
            resolve_harness_tmp_parent,
        )

        resolved_harness_tmp_parent = (
            absolute_harness_tmp_parent(harness_tmp_parent)
            if harness_tmp_parent is not None
            else resolve_harness_tmp_parent()
        )
        runner_log_dir = process_log_dir("runner")

        async def _reap_harness_processes() -> None:
            from omnigent.runtime.harnesses.process_manager import (
                sweep_orphaned_harness_processes,
            )

            await sweep_orphaned_harness_processes(tmp_parent=resolved_harness_tmp_parent)

        async def _reconcile_codex_processes() -> object:
            from omnigent.harnesses.codex_native.process_registry import (
                reap_orphaned_codex_model_probes,
                reconcile_codex_native_process_registry,
            )

            reconciled = await _run_sync_stage(reconcile_codex_native_process_registry)
            orphaned_probes = await _run_sync_stage(reap_orphaned_codex_model_probes)
            return {"registry": reconciled, "orphaned_probes": orphaned_probes}

        async def _reap_terminals() -> object:
            from omnigent.inner.terminal import reap_orphaned_terminals

            return await _run_sync_stage(reap_orphaned_terminals)

        async def _reap_native_bridge_dirs() -> object:
            from omnigent.native.native_bridge_common import reap_orphaned_native_bridge_dirs

            return await _run_sync_stage(reap_orphaned_native_bridge_dirs)

        async def _retain_runner_logs() -> object:
            live_paths = (
                tuple(live_runner_log_paths()) if live_runner_log_paths is not None else ()
            )
            return await _run_sync_stage(
                lambda: sweep_runner_logs(
                    runner_log_dir, live_paths=live_paths, on_rotated=on_runner_log_rotated
                )
            )

        return cls(
            stages=(
                ("harness_process_orphans", _reap_harness_processes),
                ("codex_process_registry", _reconcile_codex_processes),
                ("terminal_orphans", _reap_terminals),
                ("native_bridge_orphans", _reap_native_bridge_dirs),
                ("runner_log_retention", _retain_runner_logs),
            ),
            lock_path=data_dir().resolve() / "locks" / "host-maintenance.lock",
            stage_skip_reasons={"native_bridge_orphans": {"runner_superseded"}},
        )

    def start(self) -> None:
        """Schedule one delayed startup pass and the periodic self-trigger."""
        if self._closing or self._started:
            return
        self._started = True
        self._startup_task = asyncio.create_task(
            self._trigger_after_startup_delay(),
            name="host-global-maintenance-startup-delay",
        )
        self._periodic_task = asyncio.create_task(
            self._trigger_periodically(),
            name="host-global-maintenance-periodic",
        )

    def trigger(self, reason: str) -> None:
        """Request cleanup after a host-observed lifecycle event."""
        if self._closing:
            return
        self._pending_reasons.add(reason)
        self._ensure_drain_task()

    def _ensure_drain_task(self) -> None:
        """Start the drain task when pending lifecycle work has no owner."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._drain_pending(),
                name="host-global-maintenance-janitor",
            )

    async def shutdown(self) -> None:
        """Cancel delayed, periodic, and active work during host shutdown."""
        self._closing = True
        periodic_task = self._periodic_task
        if periodic_task is not None:
            if not periodic_task.done():
                periodic_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await periodic_task
            self._periodic_task = None
        startup_task = self._startup_task
        if startup_task is not None:
            if not startup_task.done():
                startup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await startup_task
            self._startup_task = None
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def _trigger_after_startup_delay(self) -> None:
        try:
            # A prior host's runners poll parent death every 0.5s and allow 2s
            # for graceful exit. Wait past that bounded window before deciding
            # which process-manager directories are truly orphaned.
            await asyncio.sleep(self._startup_delay_s)
            self.trigger("host_startup")
        finally:
            self._startup_task = None

    async def _trigger_periodically(self) -> None:
        """Self-trigger cleanup every :attr:`_periodic_interval_s` while running."""
        try:
            while not self._closing:
                await asyncio.sleep(self._periodic_interval_s)
                self.trigger("host_periodic")
        finally:
            self._periodic_task = None

    async def _drain_pending(self) -> None:
        try:
            while self._pending_reasons and not self._closing:
                reasons = sorted(self._pending_reasons)
                self._pending_reasons.clear()
                outcome = await self._run_once(reasons)
                if outcome == "busy":
                    self._pending_reasons.update(reasons)
                    await asyncio.sleep(self._lock_retry_s)
        finally:
            self._task = None
            if self._pending_reasons and not self._closing:
                self._ensure_drain_task()

    async def _run_once(self, reasons: Sequence[str]) -> _RunOutcome:
        with _maintenance_lock(self._lock_path) as lock_outcome:
            if lock_outcome == "busy":
                _logger.info(
                    "host global maintenance deferred; another host owns the sweep",
                    extra=debug_event("host_maintenance_skipped", reasons=list(reasons)),
                )
                return "busy"
            if lock_outcome == "failed":
                return "failed"
            for stage_name, stage in self._stages:
                skipped_reasons = self._stage_skip_reasons.get(stage_name, frozenset())
                if skipped_reasons.intersection(reasons):
                    _logger.info(
                        "host global maintenance stage skipped: stage=%s reasons=%s",
                        stage_name,
                        list(reasons),
                        extra=debug_event(
                            "host_maintenance_stage",
                            reasons=list(reasons),
                            stage=stage_name,
                            status="skipped",
                        ),
                    )
                    continue
                started_at = time.monotonic()
                try:
                    cleaned_items = await stage()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception(
                        "host global maintenance stage failed: stage=%s",
                        stage_name,
                        extra=debug_event(
                            "host_maintenance_stage",
                            reasons=list(reasons),
                            stage=stage_name,
                            status="failed",
                            elapsed_ms=int((time.monotonic() - started_at) * 1000),
                        ),
                    )
                    continue
                _logger.info(
                    "host global maintenance stage completed: stage=%s cleaned_items=%s",
                    stage_name,
                    cleaned_items,
                    extra=debug_event(
                        "host_maintenance_stage",
                        reasons=list(reasons),
                        stage=stage_name,
                        status="completed",
                        elapsed_ms=int((time.monotonic() - started_at) * 1000),
                    ),
                )
        return "completed"
