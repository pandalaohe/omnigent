"""Tests for host-owned background maintenance."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pytest

from omnigent.host import maintenance
from omnigent.host.maintenance import (
    HostMaintenanceJanitor,
    RunnerLogRunawayTracker,
    sweep_runner_logs,
)

_MB = 1024 * 1024
# Fixed sweep clock so age comparisons never depend on the wall clock.
_NOW = 1_800_000_000.0
_TS = "20260101-000000-000000"


def _runner_log(
    log_dir: Path,
    session: str,
    *,
    ts: str = _TS,
    archive: int | None = None,
    size: int = 0,
    mtime: float | None = None,
) -> Path:
    """Create one sparse runner log matching ``runner-<session>-<ts>.log[.N]``."""
    name = f"runner-{session}-{ts}.log"
    if archive is not None:
        name = f"{name}.{archive}"
    path = log_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    if size:
        os.truncate(path, size)
    os.utime(path, (mtime if mtime is not None else _NOW - 2 * 24 * 3600,) * 2)
    return path


def _total_bytes(paths: list[Path]) -> int:
    return sum(path.stat().st_size for path in paths if path.exists())


def test_host_janitor_covers_global_runner_cleanup() -> None:
    janitor = HostMaintenanceJanitor.for_host()

    assert [name for name, _stage in janitor._stages] == [
        "harness_process_orphans",
        "codex_process_registry",
        "terminal_orphans",
        "native_bridge_orphans",
        "runner_log_retention",
    ]
    assert janitor._stage_skip_reasons == {
        "native_bridge_orphans": frozenset({"runner_superseded"})
    }


async def test_start_runs_stages_in_background_and_in_order(tmp_path: Path) -> None:
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    finished = asyncio.Event()
    calls: list[str] = []

    async def _first() -> int:
        calls.append("first")
        first_started.set()
        await first_release.wait()
        return 1

    async def _second() -> int:
        calls.append("second")
        finished.set()
        return 1

    janitor = HostMaintenanceJanitor(
        stages=(("first", _first), ("second", _second)),
        lock_path=tmp_path / "maintenance.lock",
        startup_delay_s=0,
    )

    janitor.start()
    janitor.start()
    await asyncio.wait_for(first_started.wait(), timeout=5.0)
    task = janitor._task
    assert task is not None
    assert calls == ["first"]
    assert not task.done()
    first_release.set()
    await asyncio.wait_for(finished.wait(), timeout=5.0)
    await janitor.shutdown()

    assert calls == ["first", "second"]


async def test_host_janitor_uses_absolute_unresolved_harness_tmp_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_root = tmp_path / "relative-parent" / ".." / "harness-sockets"
    expected_root = Path(os.path.abspath(configured_root.expanduser()))
    observed_roots: list[Path | None] = []

    async def _sweep(*, tmp_parent: Path | None = None) -> None:
        observed_roots.append(tmp_parent)

    monkeypatch.setattr(
        "omnigent.runtime.harnesses.process_manager.sweep_orphaned_harness_processes",
        _sweep,
    )
    # The probe backstop scans real ps output; never let a unit test reap a
    # developer's processes.
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.process_registry.reap_orphaned_codex_model_probes",
        lambda: 0,
    )
    janitor = HostMaintenanceJanitor.for_host(harness_tmp_parent=configured_root)

    janitor.trigger("runner_exited")
    task = janitor._task
    assert task is not None
    await asyncio.wait_for(task, timeout=5.0)
    await janitor.shutdown()

    assert observed_roots == [expected_root]


async def test_runner_lifecycle_trigger_reaps_native_bridge_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge_sweeps: list[int] = []

    async def _sweep_harness_processes(*, tmp_parent: Path | None = None) -> None:
        assert tmp_parent is not None

    monkeypatch.setattr(
        "omnigent.runtime.harnesses.process_manager.sweep_orphaned_harness_processes",
        _sweep_harness_processes,
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.process_registry.reconcile_codex_native_process_registry",
        lambda: None,
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.process_registry.reap_orphaned_codex_model_probes",
        lambda: 0,
    )
    monkeypatch.setattr("omnigent.inner.terminal.reap_orphaned_terminals", lambda: None)
    monkeypatch.setattr(
        "omnigent.native.native_bridge_common.reap_orphaned_native_bridge_dirs",
        lambda: bridge_sweeps.append(1) or 2,
    )
    janitor = HostMaintenanceJanitor.for_host(harness_tmp_parent=tmp_path / "harness-sockets")
    janitor._lock_path = tmp_path / "maintenance.lock"

    janitor.trigger("runner_exited")
    task = janitor._task
    assert task is not None
    await asyncio.wait_for(task, timeout=5.0)
    await janitor.shutdown()

    assert bridge_sweeps == [1]


async def test_codex_stage_logs_reaped_process_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The codex stage reports what it reaped instead of cleaned_items=None."""

    async def _sweep_harness_processes(*, tmp_parent: Path | None = None) -> None:
        assert tmp_parent is not None

    monkeypatch.setattr(
        "omnigent.runtime.harnesses.process_manager.sweep_orphaned_harness_processes",
        _sweep_harness_processes,
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.process_registry.reconcile_codex_native_process_registry",
        lambda: 2,
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.process_registry.reap_orphaned_codex_model_probes",
        lambda: 1,
    )
    monkeypatch.setattr("omnigent.inner.terminal.reap_orphaned_terminals", lambda: None)
    monkeypatch.setattr(
        "omnigent.native.native_bridge_common.reap_orphaned_native_bridge_dirs",
        lambda: 0,
    )
    janitor = HostMaintenanceJanitor.for_host(harness_tmp_parent=tmp_path / "harness-sockets")
    janitor._lock_path = tmp_path / "maintenance.lock"

    with caplog.at_level(logging.INFO):
        janitor.trigger("runner_exited")
        task = janitor._task
        assert task is not None
        await asyncio.wait_for(task, timeout=5.0)
        await janitor.shutdown()

    assert (
        "stage=codex_process_registry cleaned_items={'registry': 2, 'orphaned_probes': 1}"
        in caplog.text
    )


async def test_start_fires_periodic_self_trigger(tmp_path: Path) -> None:
    """A long-lived host self-triggers cleanup on the periodic interval."""
    calls = 0
    second_pass = asyncio.Event()

    async def _stage() -> int:
        nonlocal calls
        calls += 1
        if calls >= 2:
            second_pass.set()
        return 0

    janitor = HostMaintenanceJanitor(
        stages=(("stage", _stage),),
        lock_path=tmp_path / "maintenance.lock",
        startup_delay_s=60,
        periodic_interval_s=0.01,
    )

    janitor.start()
    try:
        # The startup pass is 60 s away, so only the periodic timer can run.
        await asyncio.wait_for(second_pass.wait(), timeout=5.0)
    finally:
        await janitor.shutdown()

    assert calls >= 2
    assert janitor._periodic_task is None


async def test_triggers_during_cleanup_coalesce_into_one_follow_up(tmp_path: Path) -> None:
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    second_finished = asyncio.Event()
    calls = 0

    async def _stage() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await first_release.wait()
        else:
            second_finished.set()
        return 0

    janitor = HostMaintenanceJanitor(
        stages=(("stage", _stage),),
        lock_path=tmp_path / "maintenance.lock",
    )
    janitor.trigger("first_runner_exit")
    await asyncio.wait_for(first_started.wait(), timeout=5.0)
    janitor.trigger("runner_exited")
    janitor.trigger("runner_stopped")
    first_release.set()
    await asyncio.wait_for(second_finished.wait(), timeout=5.0)
    await janitor.shutdown()

    assert calls == 2


async def test_superseded_pass_skips_native_bridge_cleanup(tmp_path: Path) -> None:
    calls: list[str] = []

    async def _global_cleanup() -> None:
        calls.append("global")

    async def _native_bridge_cleanup() -> None:
        calls.append("native_bridge")

    janitor = HostMaintenanceJanitor(
        stages=(
            ("global", _global_cleanup),
            ("native_bridge_orphans", _native_bridge_cleanup),
        ),
        lock_path=tmp_path / "maintenance.lock",
        stage_skip_reasons={"native_bridge_orphans": {"runner_superseded"}},
    )

    janitor.trigger("runner_superseded")
    task = janitor._task
    assert task is not None
    await asyncio.wait_for(task, timeout=5.0)
    assert calls == ["global"]

    janitor.trigger("runner_exited")
    task = janitor._task
    assert task is not None
    await asyncio.wait_for(task, timeout=5.0)
    await janitor.shutdown()

    assert calls == ["global", "global", "native_bridge"]


async def test_runner_trigger_does_not_wait_for_startup_delay(tmp_path: Path) -> None:
    finished = asyncio.Event()

    async def _stage() -> None:
        finished.set()

    janitor = HostMaintenanceJanitor(
        stages=(("stage", _stage),),
        lock_path=tmp_path / "maintenance.lock",
        startup_delay_s=60,
    )

    janitor.start()
    janitor.trigger("runner_exited")
    await asyncio.wait_for(finished.wait(), timeout=5.0)
    await janitor.shutdown()


@pytest.mark.skipif(os.name != "posix", reason="flock is POSIX-only")
async def test_lock_prevents_cross_host_overlap(tmp_path: Path) -> None:
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    second_calls = 0
    second_finished = asyncio.Event()

    async def _blocking_stage() -> int:
        first_started.set()
        await first_release.wait()
        return 0

    async def _second_stage() -> int:
        nonlocal second_calls
        second_calls += 1
        second_finished.set()
        return 0

    lock_path = tmp_path / "maintenance.lock"
    first = HostMaintenanceJanitor(
        stages=(("first", _blocking_stage),),
        lock_path=lock_path,
    )
    second = HostMaintenanceJanitor(
        stages=(("second", _second_stage),),
        lock_path=lock_path,
        lock_retry_s=0.01,
    )

    first.trigger("first_runner_exit")
    await asyncio.wait_for(first_started.wait(), timeout=5.0)
    second.trigger("second_runner_exit")
    await asyncio.sleep(0.05)
    assert second_calls == 0

    first_release.set()
    await asyncio.wait_for(second_finished.wait(), timeout=5.0)
    await first.shutdown()
    await second.shutdown()

    assert second_calls == 1


async def test_permanent_lock_failure_does_not_retry_forever(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def _stage() -> None:
        nonlocal calls
        calls += 1

    def _deny_open(*_args: object, **_kwargs: object) -> int:
        raise PermissionError("read-only data directory")

    monkeypatch.setattr("omnigent.host.maintenance.os.open", _deny_open)
    janitor = HostMaintenanceJanitor(
        stages=(("stage", _stage),),
        lock_path=tmp_path / "maintenance.lock",
        lock_retry_s=0.01,
    )

    janitor.trigger("runner_exit")
    task = janitor._task
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)

    assert calls == 0
    assert janitor._pending_reasons == set()


async def test_shutdown_cancels_active_cleanup(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _stage() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    janitor = HostMaintenanceJanitor(
        stages=(("stage", _stage),),
        lock_path=tmp_path / "maintenance.lock",
    )
    janitor.trigger("runner_exit")
    task = janitor._task
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=5.0)
    await janitor.shutdown()

    assert task.cancelled()
    assert cancelled.is_set()


async def test_stage_failure_does_not_abort_later_stages(tmp_path: Path) -> None:
    later_ran = asyncio.Event()

    async def _broken() -> None:
        raise RuntimeError("boom")

    async def _later() -> None:
        later_ran.set()

    janitor = HostMaintenanceJanitor(
        stages=(("broken", _broken), ("later", _later)),
        lock_path=tmp_path / "maintenance.lock",
    )
    janitor.trigger("runner_exit")
    await asyncio.wait_for(later_ran.wait(), timeout=5.0)
    await janitor.shutdown()


async def test_shutdown_cancels_delayed_startup_pass(tmp_path: Path) -> None:
    calls = 0

    async def _stage() -> None:
        nonlocal calls
        calls += 1

    janitor = HostMaintenanceJanitor(
        stages=(("stage", _stage),),
        lock_path=tmp_path / "maintenance.lock",
        startup_delay_s=60,
    )

    janitor.start()
    await janitor.shutdown()

    assert calls == 0
    assert janitor._startup_task is None


# ── Runner-log retention sweep ───────────────────────────


def test_sweep_copytruncates_live_file_and_writer_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live log over the threshold is archived, then truncated in place.

    The runner and its harness child hold O_APPEND fds, so the sweep must
    copy the content aside and truncate rather than rename or delete; a
    still-open writer has to continue at the new end of the file.
    """
    monkeypatch.setattr(maintenance, "_RUNNER_LOG_COPYTRUNCATE_BYTES", 1024)
    live = _runner_log(tmp_path, "sess-a", size=2048, mtime=_NOW - 10)
    rotations: list[tuple[Path, int, tuple[int, int]]] = []
    live_info = live.stat()
    writer = open(live, "ab", buffering=0)  # noqa: SIM115 - the fd is the point
    try:
        counts = sweep_runner_logs(
            tmp_path,
            live_paths={live},
            now=_NOW,
            on_rotated=lambda path, size, file_id: rotations.append((path, size, file_id)),
        )

        assert counts == {"copied": 1, "truncated": 1, "deleted": 0, "failed": 0}
        assert rotations == [(live, 2048, (live_info.st_dev, live_info.st_ino))]
        archive = tmp_path / f"{live.name}.1"
        assert archive.stat().st_size == 2048
        assert live.stat().st_size == 0
        # The writer's fd survived the truncate and appends at the new end.
        writer.write(b"next line\n")
        assert live.read_bytes() == b"next line\n"
    finally:
        writer.close()


def test_sweep_shifts_archives_and_keeps_at_most_four(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotation shifts ``.1``->``.2`` and drops anything past ``.4``."""
    monkeypatch.setattr(maintenance, "_RUNNER_LOG_COPYTRUNCATE_BYTES", 1024)
    live = _runner_log(tmp_path, "sess-a", size=2048, mtime=_NOW - 10)
    for index, body in ((1, b"one"), (2, b"two"), (3, b"three"), (4, b"four")):
        archive = tmp_path / f"{live.name}.{index}"
        archive.write_bytes(body)
        os.utime(archive, (_NOW - 2 * 24 * 3600,) * 2)

    counts = sweep_runner_logs(tmp_path, live_paths={live}, now=_NOW)

    assert counts["copied"] == 1
    assert (tmp_path / f"{live.name}.1").stat().st_size == 2048
    assert (tmp_path / f"{live.name}.2").read_bytes() == b"one"
    assert (tmp_path / f"{live.name}.3").read_bytes() == b"two"
    assert (tmp_path / f"{live.name}.4").read_bytes() == b"three"
    # The oldest archive fell off the end.
    assert not (tmp_path / f"{live.name}.5").exists()


def test_sweep_failed_copytruncate_is_warned_and_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A copy/truncate failure (Windows sharing violation) never crashes.

    The file must be left alone — no half-rotated archive, no truncated
    live file — and the failure reported once at WARNING.
    """
    monkeypatch.setattr(maintenance, "_RUNNER_LOG_COPYTRUNCATE_BYTES", 1024)
    live = _runner_log(tmp_path, "sess-a", size=2048, mtime=_NOW - 10)

    def _deny_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError(32, "The process cannot access the file")

    monkeypatch.setattr(maintenance.shutil, "copyfileobj", _deny_copy)
    with caplog.at_level(logging.WARNING, logger="omnigent.host.maintenance"):
        counts = sweep_runner_logs(tmp_path, live_paths={live}, now=_NOW)

    assert counts["failed"] == 1
    assert counts["copied"] == 0
    assert live.stat().st_size == 2048
    assert not (tmp_path / f"{live.name}.1").exists()
    warnings = [r for r in caplog.records if "runner log copytruncate failed" in r.message]
    assert len(warnings) == 1


def test_sweep_skips_symlink_archive_without_writing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(maintenance, "_RUNNER_LOG_COPYTRUNCATE_BYTES", 1024)
    log_dir = tmp_path / "logs"
    live = _runner_log(log_dir, "sess-a", size=2048, mtime=_NOW - 10)
    target = tmp_path / "outside"
    archive = log_dir / f"{live.name}.1"
    archive.symlink_to(target)

    with caplog.at_level(logging.WARNING, logger="omnigent.host.maintenance"):
        counts = sweep_runner_logs(log_dir, live_paths={live}, now=_NOW)

    assert counts == {"copied": 0, "truncated": 0, "deleted": 0, "failed": 1}
    assert archive.is_symlink()
    assert not target.exists()
    assert live.stat().st_size == 2048
    assert sum("runner log copytruncate failed" in r.message for r in caplog.records) == 1


def test_sweep_failed_copy_keeps_archive_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "_RUNNER_LOG_COPYTRUNCATE_BYTES", 1024)
    live = _runner_log(tmp_path, "sess-a", size=2048, mtime=_NOW - 10)
    archives = [tmp_path / f"{live.name}.{index}" for index in range(1, 5)]
    for index, archive in enumerate(archives, 1):
        archive.write_bytes(f"archive {index}".encode())
        os.utime(archive, (_NOW - 2 * 24 * 3600,) * 2)

    def _deny_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError(32, "copy failed")

    monkeypatch.setattr(maintenance.shutil, "copyfileobj", _deny_copy)
    counts = sweep_runner_logs(tmp_path, live_paths={live}, now=_NOW)

    assert counts["failed"] == 1
    assert [archive.read_bytes() for archive in archives] == [
        f"archive {index}".encode() for index in range(1, 5)
    ]
    assert live.stat().st_size == 2048
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [live.name, *(archive.name for archive in archives)]
    )


def test_sweep_skips_recent_archive_before_shifting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "_RUNNER_LOG_COPYTRUNCATE_BYTES", 1024)
    live = _runner_log(tmp_path, "sess-a", size=2048, mtime=_NOW - 10)
    archive = _runner_log(tmp_path, "sess-a", archive=4, size=5, mtime=_NOW - 1)

    counts = sweep_runner_logs(tmp_path, live_paths={live}, now=_NOW)

    assert counts["failed"] == 1
    assert archive.stat().st_size == 5
    assert live.stat().st_size == 2048


def test_sweep_keeps_archive_refreshed_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "_RUNNER_LOG_COPYTRUNCATE_BYTES", 1024)
    live = _runner_log(tmp_path, "sess-a", size=2048, mtime=_NOW - 10)
    archives = [tmp_path / f"{live.name}.{index}" for index in range(1, 5)]
    for index, archive in enumerate(archives, 1):
        archive.write_bytes(f"archive {index}".encode())
        os.utime(archive, (_NOW - 2 * 24 * 3600,) * 2)
    originals = [(archive.stat().st_ino, archive.read_bytes()) for archive in archives]
    copyfileobj = maintenance.shutil.copyfileobj

    def _refresh_oldest(source: object, target: object) -> None:
        copyfileobj(source, target)
        os.utime(archives[-1], (_NOW,) * 2)

    monkeypatch.setattr(maintenance.shutil, "copyfileobj", _refresh_oldest)
    counts = sweep_runner_logs(tmp_path, live_paths={live}, now=_NOW)

    assert counts == {"copied": 0, "truncated": 0, "deleted": 0, "failed": 1}
    assert [(archive.stat().st_ino, archive.read_bytes()) for archive in archives] == originals
    assert live.stat().st_size == 2048
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [live.name, *(archive.name for archive in archives)]
    )


def test_sweep_does_not_delete_candidate_refreshed_after_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [
        _runner_log(
            tmp_path,
            "sess-a",
            ts=f"20260101-000000-00000{index}",
            size=100 * _MB,
            mtime=_NOW - 2 * 24 * 3600 - (8 - index) * 60,
        )
        for index in range(7)
    ]
    original_unlink = maintenance._unlink_runner_log

    def _refresh_second_after_first(*args: object, **kwargs: object) -> None:
        original_unlink(*args, **kwargs)
        if args[0] == files[0]:
            os.utime(files[1], (_NOW,) * 2)

    monkeypatch.setattr(maintenance, "_unlink_runner_log", _refresh_second_after_first)
    counts = sweep_runner_logs(tmp_path, now=_NOW)

    assert counts["deleted"] == 2
    assert not files[0].exists()
    assert files[1].exists()
    assert not files[2].exists()


def test_unlink_skips_candidate_replaced_after_snapshot(tmp_path: Path) -> None:
    candidate = _runner_log(tmp_path, "sess-a", size=10, mtime=_NOW - 2 * 24 * 3600)
    snapshot = candidate.lstat()
    candidate.rename(tmp_path / "moved")
    candidate.write_bytes(b"new file")
    os.utime(candidate, (_NOW - 2 * 24 * 3600,) * 2)
    counts = {"deleted": 0}

    maintenance._unlink_runner_log(candidate, snapshot, set(), _NOW, counts)

    assert candidate.read_bytes() == b"new file"
    assert counts["deleted"] == 0


def test_sweep_enforces_session_cap_oldest_first_and_keeps_live(tmp_path: Path) -> None:
    """A session over 500 MB sheds its oldest non-live files; live stays."""
    session = "sess-cap"
    live = _runner_log(
        tmp_path, session, ts="20260101-000000-000001", size=100 * _MB, mtime=_NOW - 10
    )
    old = [
        _runner_log(
            tmp_path,
            session,
            ts=f"20260101-000000-00000{index}",
            size=100 * _MB,
            # Later entries are newer, so ``old[0]`` is the oldest.
            mtime=_NOW - 2 * 24 * 3600 - (12 - index) * 60,
        )
        for index in range(2, 7)
    ]

    counts = sweep_runner_logs(tmp_path, live_paths={live}, now=_NOW)

    # 600 MB total: the oldest non-live file goes, leaving the 500 MB cap.
    assert counts["deleted"] == 1
    assert live.exists()
    assert not old[0].exists()
    remaining = [path for path in [live, *old[1:]] if path.exists()]
    assert _total_bytes(remaining) == 500 * _MB


def test_sweep_deletes_aged_files_but_not_recent_or_live(tmp_path: Path) -> None:
    """Only non-live files older than 30 days are deleted."""
    aged = _runner_log(tmp_path, "sess-old", mtime=_NOW - 31 * 24 * 3600, size=1024)
    recent = _runner_log(tmp_path, "sess-recent", mtime=_NOW - 120, size=1024)
    live_old_mtime = _runner_log(
        tmp_path, "sess-live", ts="20260101-000000-000002", mtime=_NOW - 40 * 24 * 3600, size=1024
    )

    counts = sweep_runner_logs(tmp_path, live_paths={live_old_mtime}, now=_NOW)

    assert counts["deleted"] == 1
    assert not aged.exists()
    # A recent mtime marks a file another daemon or the CLI may still write.
    assert recent.exists()
    # An explicit live path is protected even when its mtime is ancient.
    assert live_old_mtime.exists()


def test_sweep_enforces_directory_cap_oldest_first(tmp_path: Path) -> None:
    """A directory over 3 GB sheds non-live files until it is under."""
    # One session per file so the per-session cap never fires first.
    files = [
        _runner_log(
            tmp_path,
            f"sess-{index:02d}",
            ts=f"20260101-000000-00000{index}",
            size=100 * _MB,
            # Later entries are newer, so ``files[0]`` is the oldest.
            mtime=_NOW - 2 * 24 * 3600 - (40 - index) * 60,
        )
        for index in range(1, 33)
    ]
    live = files[-1]

    counts = sweep_runner_logs(tmp_path, live_paths={live}, now=_NOW)

    # 3.2 GB total: deleting the two oldest gets under the 3 GB cap.
    assert counts["deleted"] == 2
    assert not files[0].exists()
    assert not files[1].exists()
    assert live.exists()


def test_sweep_under_all_thresholds_changes_nothing(tmp_path: Path) -> None:
    """Negative control: an in-policy directory is left byte-for-byte alone."""
    live = _runner_log(tmp_path, "sess-a", size=1024, mtime=_NOW - 10)
    quiet = _runner_log(tmp_path, "sess-b", size=2048, mtime=_NOW - 3 * 24 * 3600)
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime) for path in tmp_path.iterdir()
    }

    counts = sweep_runner_logs(tmp_path, live_paths={live}, now=_NOW)

    assert counts == {"copied": 0, "truncated": 0, "deleted": 0, "failed": 0}
    after = {path.name: (path.stat().st_size, path.stat().st_mtime) for path in tmp_path.iterdir()}
    assert after == before
    assert quiet.exists()


# ── Runner-log runaway tracker ───────────────────────────


def test_runaway_tracker_reports_once_per_crossing() -> None:
    """Crossing 5 MB/h reports once; staying above reports nothing more."""
    tracker = RunnerLogRunawayTracker()

    assert tracker.observe("runner_1", 0, 0.0) is None
    assert tracker.observe("runner_1", 3 * _MB, 60.0) is None
    assert tracker.observe("runner_1", 6 * _MB, 120.0) == 6 * _MB
    # Already reported and still above the threshold: no second frame.
    assert tracker.observe("runner_1", 7 * _MB, 180.0) is None
    assert tracker.observe("runner_1", 8 * _MB, 240.0) is None


def test_runaway_tracker_rearms_after_falling_below() -> None:
    """After a quiet hour the tracker can report the next crossing."""
    tracker = RunnerLogRunawayTracker()
    assert tracker.observe("runner_1", 0, 0.0) is None
    assert tracker.observe("runner_1", 6 * _MB, 60.0) == 6 * _MB
    assert tracker.observe("runner_1", 7 * _MB, 120.0) is None

    # More than a window later with no growth: the burst left the window.
    assert tracker.observe("runner_1", 7 * _MB, 4 * 3600.0) is None
    assert tracker.observe("runner_1", 13 * _MB, 4 * 3600.0 + 60) == 6 * _MB


def test_runaway_tracker_copytruncate_does_not_reset_count() -> None:
    """Bytes removed by an in-place rotation stay in the measured rate."""
    tracker = RunnerLogRunawayTracker()
    assert tracker.observe("runner_1", 0, 0.0) is None
    assert tracker.observe("runner_1", 3 * _MB, 60.0) is None
    # Copytruncate: the live file is copied to ``.1`` and truncated to zero.
    assert tracker.observe("runner_1", 0, 120.0) is None
    # 3 MB before the rotation + 3 MB after crosses the threshold.
    assert tracker.observe("runner_1", 3 * _MB, 180.0) == 6 * _MB


def test_runaway_tracker_counts_unsampled_bytes_at_rotation() -> None:
    tracker = RunnerLogRunawayTracker()
    assert tracker.observe("runner_1", 99 * _MB, 0.0) is None
    tracker.note_rotated("runner_1", 103 * _MB, 60.0)
    assert tracker.observe("runner_1", 3 * _MB, 120.0) == 7 * _MB


@pytest.mark.parametrize("sample_first", [True, False])
def test_runaway_tracker_rotation_counts_each_byte_once(sample_first: bool) -> None:
    tracker = RunnerLogRunawayTracker()
    assert tracker.observe("runner_1", 103 * _MB, 0.0) is None
    if sample_first:
        assert tracker.observe("runner_1", 0, 60.0) is None
    tracker.note_rotated("runner_1", 103 * _MB, 61.0)
    if not sample_first:
        assert tracker.observe("runner_1", 0, 62.0) is None
    assert tracker.observe("runner_1", 0, 63.0) is None
    assert tracker.observe("runner_1", 6 * _MB, 64.0) == 6 * _MB


@pytest.mark.parametrize("sample_first", [True, False])
def test_runaway_tracker_keeps_unsampled_growth_across_rotation(sample_first: bool) -> None:
    tracker = RunnerLogRunawayTracker()
    assert tracker.observe("runner_1", 99 * _MB, 0.0) is None
    if sample_first:
        assert tracker.observe("runner_1", 0, 60.0) is None
    tracker.note_rotated("runner_1", 103 * _MB, 61.0)
    if not sample_first:
        assert tracker.observe("runner_1", 0, 62.0) is None
    assert tracker.observe("runner_1", 3 * _MB, 63.0) == 7 * _MB


def test_runaway_tracker_keeps_cutoff_baseline() -> None:
    tracker = RunnerLogRunawayTracker()
    assert tracker.observe("runner_1", 0, 0.0) is None
    reports = []
    for index in range(1, 49):
        report = tracker.observe("runner_1", int(index * 5.2 * _MB / 12), index * 300.01)
        if report is not None:
            reports.append(report)
    assert len(reports) == 1
    assert reports[0] > 5 * _MB


def test_runaway_tracker_ignores_first_sighting_of_existing_content() -> None:
    """A runner discovered with a large log is not instantly a runaway."""
    tracker = RunnerLogRunawayTracker()
    assert tracker.observe("runner_1", 40 * _MB, 0.0) is None
    assert tracker.observe("runner_1", 40 * _MB + 1, 60.0) is None


def test_runaway_tracker_retain_drops_unknown_runners() -> None:
    """State for runners the host no longer owns is discarded."""
    tracker = RunnerLogRunawayTracker()
    assert tracker.observe("runner_1", 0, 0.0) is None
    assert tracker.observe("runner_1", 6 * _MB, 60.0) == 6 * _MB

    tracker.retain({"runner_2"})

    assert tracker._samples == {}
    assert tracker._last_sizes == {}
    assert tracker._reported == set()
