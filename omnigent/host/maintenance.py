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
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
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

# Runner-log runaway detection (see :class:`RunnerLogWarningCounter`).
# Warning/error bytes (WARN/ERROR/CRIT records and their header-less traceback
# lines) per sliding hour: healthy runners stay under 1 MB/h, while error
# loops write 7-17 MB/h.
_RUNNER_LOG_RUNAWAY_BYTES = 2 * 1024 * 1024
_RUNNER_LOG_RUNAWAY_WINDOW_S = 60 * 60

# Bytes read per chunk while classifying appended output; bounds the memory a
# probe holds while still appending a pathological record.
_RUNNER_LOG_WARNING_CHUNK_BYTES = 1024 * 1024

# ``WARN  10-04 23:01:02`` record header, with an optional ANSI colour prefix
# and reset around the level token.
_RUNNER_LOG_HEADER_RE = re.compile(
    rb"^(?:\x1b\[[\d;]*m)?(DEBUG|INFO|WARN|ERROR|CRIT)"
    rb"(?:\x1b\[[\d;]*m)?\s+\d\d-\d\d \d\d:\d\d:\d\d"
)
_RUNNER_LOG_WARNING_LEVELS = (b"WARN", b"ERROR", b"CRIT")

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
    on_rotated: Callable[[Path, int, tuple[int, int]], object] | None = None,
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


@dataclass(frozen=True)
class _ShrinkSnapshot:
    """Parsing state saved when a shrink is seen before its rotation report.

    Resuming the archive tail requires the record level, the partial line,
    and any stashed long-line length as they stood at the saved offset.
    """

    offset: int
    record_is_warning: bool
    carry: bytes
    long_line_prefix: int


@dataclass
class _RunnerLogWarningState:
    """Warning-byte read state for one runner log."""

    file_id: tuple[int, int]
    offset: int
    warning_bytes: int = 0
    record_is_warning: bool = False
    shrink_snapshot: _ShrinkSnapshot | None = None
    carry: bytes = b""
    long_line_prefix: int = 0


class RunnerLogWarningCounter:
    """Count WARN/ERROR/CRIT bytes each runner appends to its log.

    A runaway is a flood of warnings and errors, not raw log volume, so only
    warning-level records and the header-less lines that follow them
    (tracebacks, a child's raw stderr) count. A runner's first sighting starts
    at the current end of its log: existing content is not this window's
    output. A copytruncate keeps the count — bytes copied to ``<name>.1`` are
    classified once, whether the shrink or the rotation report is seen first —
    and a new file identity restarts at the beginning of the new file.

    :meth:`advance` and :meth:`retain` serialize on a state lock and run in
    worker threads; :meth:`note_rotated` runs on the event loop and only takes
    the pending-rotation lock, so it never waits on a file read.
    """

    def __init__(self) -> None:
        self._states: dict[str, _RunnerLogWarningState] = {}
        self._pending_rotations: dict[str, tuple[int, tuple[int, int]]] = {}
        self._state_lock = threading.Lock()
        self._lock = threading.Lock()

    def advance(self, runner_id: str, path: Path) -> int | None:
        """Classify the log bytes appended since the last call.

        :param runner_id: Runner whose log is sampled.
        :param path: Live log file.
        :returns: Cumulative warning bytes, or ``None`` when the log cannot
            be stat'ed.
        """
        with self._state_lock:
            try:
                info = path.stat()
            except OSError:
                return None
            file_id = (info.st_dev, info.st_ino)
            size = info.st_size
            with self._lock:
                pending = self._pending_rotations.pop(runner_id, None)
            state = self._states.get(runner_id)
            if state is None:
                # First sighting: existing content is not this window's output.
                self._states[runner_id] = _RunnerLogWarningState(file_id=file_id, offset=size)
                return 0
            if pending is not None and pending[1] == state.file_id:
                self._account_rotation(state, path, pending[0])
            else:
                # The rotation report follows its truncate within milliseconds,
                # so a shrink still unreported a probe later was an outside
                # truncate.
                state.shrink_snapshot = None
            if state.file_id != file_id:
                # A replaced file: read the new file from its start.
                state.file_id = file_id
                state.offset = 0
                state.shrink_snapshot = None
                state.record_is_warning = False
                state.carry = b""
                state.long_line_prefix = 0
            elif size < state.offset:
                # An in-place truncate this probe has not seen reported yet:
                # remember where the archived copy resumes, with the parsing
                # state to classify it, and restart the live read at zero.
                state.shrink_snapshot = _ShrinkSnapshot(
                    offset=state.offset,
                    record_is_warning=state.record_is_warning,
                    carry=state.carry,
                    long_line_prefix=state.long_line_prefix,
                )
                state.offset = 0
                state.record_is_warning = False
                state.carry = b""
                state.long_line_prefix = 0
            state.offset = self._read_lines(state, path, state.offset, size)
            return state.warning_bytes

    def note_rotated(self, runner_id: str, copied_size: int, file_id: tuple[int, int]) -> None:
        """Record a copytruncate for the next :meth:`advance` to classify.

        Called on the event loop thread right after the live file was copied
        to ``<name>.1`` and truncated. No file I/O happens here.

        :param runner_id: Runner whose log was rotated.
        :param copied_size: Bytes the live file held when it was copied.
        :param file_id: Source file identity, so a stale report is ignored.
        """
        with self._lock:
            self._pending_rotations[runner_id] = (copied_size, file_id)

    def retain(self, runner_ids: Collection[str]) -> None:
        """Drop state (and pending rotations) for runners no longer owned.

        :param runner_ids: Runner ids still tracked by the caller.
        """
        keep = set(runner_ids)
        with self._state_lock:
            for runner_id in [rid for rid in self._states if rid not in keep]:
                del self._states[runner_id]
            with self._lock:
                for runner_id in [rid for rid in self._pending_rotations if rid not in keep]:
                    del self._pending_rotations[runner_id]

    def _account_rotation(
        self, state: _RunnerLogWarningState, path: Path, copied_size: int
    ) -> None:
        """Classify the archived bytes the live file no longer holds."""
        archive = _archive_path(path, 1)
        if state.shrink_snapshot is not None:
            # The shrink was seen first: resume the archive where the live
            # offset stopped, using a temporary state seeded from the saved
            # parsing state. The live state keeps its post-rotation progress.
            snapshot = state.shrink_snapshot
            state.shrink_snapshot = None
            archived = _RunnerLogWarningState(
                file_id=state.file_id,
                offset=snapshot.offset,
                record_is_warning=snapshot.record_is_warning,
                carry=snapshot.carry,
                long_line_prefix=snapshot.long_line_prefix,
            )
            self._read_lines(archived, archive, snapshot.offset, copied_size)
            state.warning_bytes += archived.warning_bytes
        else:
            # The report was seen first: the live state is still the
            # pre-truncate state, so it can classify the archive directly.
            start = state.offset
            state.offset = 0
            self._read_lines(state, archive, start, copied_size)

    def _read_lines(self, state: _RunnerLogWarningState, path: Path, start: int, end: int) -> int:
        """Classify ``[start, end)`` of *path*; return the bytes consumed."""
        position = start
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                while position < end:
                    chunk = handle.read(min(_RUNNER_LOG_WARNING_CHUNK_BYTES, end - position))
                    if not chunk:
                        break
                    self._consume(state, chunk)
                    position += len(chunk)
        except OSError:
            _logger.debug("runner log warning read failed: %s", path, exc_info=True)
        return position

    def _consume(self, state: _RunnerLogWarningState, data: bytes) -> None:
        """Classify the complete lines in *data*, deferring a partial tail."""
        buffer = state.carry + data
        start = 0
        while (newline := buffer.find(b"\n", start)) >= 0:
            if state.long_line_prefix:
                self._finish_long_line(state, newline + 1)
            else:
                self._count_line(state, buffer[start : newline + 1])
            start = newline + 1
        state.carry = buffer[start:]
        if len(state.carry) > _RUNNER_LOG_WARNING_CHUNK_BYTES:
            self._stash_long_line(state)

    def _stash_long_line(self, state: _RunnerLogWarningState) -> None:
        """Drop a pending long line's bytes, keeping its length and class."""
        if state.long_line_prefix == 0:
            # The first stash sees the line's header; a header line sets the
            # record classification, a continuation keeps the previous one.
            header = _RUNNER_LOG_HEADER_RE.match(state.carry)
            if header is not None:
                state.record_is_warning = header.group(1) in _RUNNER_LOG_WARNING_LEVELS
        state.long_line_prefix += len(state.carry)
        state.carry = b""

    def _finish_long_line(self, state: _RunnerLogWarningState, continuation: int) -> None:
        """Credit a stashed long line's full length once its newline arrives."""
        length = state.long_line_prefix + continuation
        state.long_line_prefix = 0
        if state.record_is_warning:
            state.warning_bytes += length

    def _count_line(self, state: _RunnerLogWarningState, line: bytes) -> None:
        header = _RUNNER_LOG_HEADER_RE.match(line)
        if header is not None:
            state.record_is_warning = header.group(1) in _RUNNER_LOG_WARNING_LEVELS
        if state.record_is_warning:
            state.warning_bytes += len(line)


class RunnerLogRunawayTracker:
    """Detect runners whose warning/error output exceeds the runaway threshold.

    A host-owned state machine: the caller samples each live runner's
    cumulative warning-byte count on a fixed cadence and feeds the totals
    here. Growth is measured over a sliding window, so a copytruncate or a new
    log file never resets the measured rate. A runner is reported once per
    crossing: after a report, the tracker re-arms only when the windowed bytes
    fall back to or below the threshold. :meth:`over_threshold` lets the caller
    re-confirm the report on every sample while the runner stays over.
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
        self._reported: set[str] = set()
        self._windowed_bytes: dict[str, int] = {}

    def observe(self, runner_id: str, total_bytes: int, now: float) -> int | None:
        """Record one cumulative warning-byte sample.

        :param runner_id: Runner whose counter was sampled.
        :param total_bytes: Cumulative warning bytes from
            :class:`RunnerLogWarningCounter`.
        :param now: Sample time in seconds, monotonic per caller.
        :returns: The windowed ``bytes_last_hour`` when this sample crosses
            the threshold and should be reported, else ``None``.
        """
        samples = self._samples.setdefault(runner_id, deque())
        samples.append((now, total_bytes))
        cutoff = now - self._window_s
        while len(samples) > 1 and samples[1][0] <= cutoff:
            samples.popleft()

        bytes_last_hour = samples[-1][1] - samples[0][1]
        self._windowed_bytes[runner_id] = bytes_last_hour
        if bytes_last_hour > self._threshold_bytes:
            if runner_id in self._reported:
                return None
            self._reported.add(runner_id)
            return bytes_last_hour
        self._reported.discard(runner_id)
        return None

    def over_threshold(self, runner_id: str) -> int | None:
        """Return the latest windowed bytes while a reported episode runs.

        The caller re-sends the report on every sample while this returns a
        value, always with the crossing instant.

        :param runner_id: Runner whose counter was sampled.
        :returns: The windowed ``bytes_last_hour`` from the latest
            :meth:`observe`, or ``None`` when the runner is not in a reported
            episode.
        """
        if runner_id not in self._reported:
            return None
        return self._windowed_bytes.get(runner_id)

    def retain(self, runner_ids: Collection[str]) -> None:
        """Drop state for runners this host no longer owns.

        :param runner_ids: Runner ids still tracked by the caller.
        """
        for runner_id in [rid for rid in self._samples if rid not in runner_ids]:
            del self._samples[runner_id]
            self._reported.discard(runner_id)
            self._windowed_bytes.pop(runner_id, None)


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
        on_runner_log_rotated: Callable[[Path, int, tuple[int, int]], object] | None = None,
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
