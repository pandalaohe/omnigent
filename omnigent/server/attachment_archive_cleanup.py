"""Opt-in deletion of large by-path attachment originals in archived sessions.

The job runs in the server process, re-reads its settings from the server
config file on every sweep, and defaults off. A purge is authorized by the
session's archive state and revision, never by a request: the sweep claims the
shared blob in the file store, re-checks that every referrer belongs to the
same still-archived session, deletes the artifact bytes, and only then marks
the rows ``done``. ``claimed`` rows keep behaving like live rows, so a crash
mid-protocol loses nothing user-visible.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import now_epoch
from omnigent.entities import Conversation, StoredFile
from omnigent.errors import StaleCursorError
from omnigent.server.admin_list import resolve_data_dir
from omnigent.server.server_config import (
    attachment_archive_cleanup_days,
    attachment_archive_cleanup_dry_run,
    attachment_archive_cleanup_min_bytes,
)
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.file_store import FileStore

_logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 24 * 60 * 60
# The first sweep waits for boot to settle; later sweeps run once a day.
FIRST_SWEEP_DELAY_S = 10 * 60
SWEEP_INTERVAL_S = 24 * 60 * 60
# At most this many blobs finish per sweep; the rest wait for the next one.
PURGE_CAP = 500
_SESSION_PAGE_SIZE = 200
_FILE_PAGE_SIZE = 200
SUMMARY_FILENAME = "attachment-archive-cleanup.json"


@dataclass
class CleanupSummary:
    """
    Result of one archive-cleanup sweep.

    :param mode: ``"off"`` when disabled, ``"dry-run"`` when logging only,
        ``"on"`` when purging.
    :param sessions_scanned: Archived candidate sessions examined.
    :param files_purged: Blobs marked ``done`` this sweep.
    :param bytes_freed: Summed stored sizes of the purged blobs.
    :param skipped_shared: Candidate blobs left alone because a referrer is
        outside the session or not claimed by it.
    :param skipped_unarchived: Claimed blobs released because the session was
        unarchived (or re-archived) between claim and delete.
    :param released: ``claimed`` states cleared by this sweep.
    :param errors: Sessions whose processing raised.
    :param capped: Whether the sweep stopped at :data:`PURGE_CAP`.
    """

    mode: str
    sessions_scanned: int = 0
    files_purged: int = 0
    bytes_freed: int = 0
    skipped_shared: int = 0
    skipped_unarchived: int = 0
    released: int = 0
    errors: int = 0
    capped: bool = False


def _purge_metadata(source_metadata: object) -> Mapping[str, object] | None:
    """The row's ``source_metadata["purge"]`` mapping, or ``None``."""
    if not isinstance(source_metadata, Mapping):
        return None
    purge = source_metadata.get("purge")
    return purge if isinstance(purge, Mapping) else None


class AttachmentArchiveCleanup:
    """
    Periodic purge of large attachment originals in long-archived sessions.

    Settings come from the server config file only, re-read on every sweep;
    a sweep that finds ``attachment_archive_cleanup_days == 0`` returns a
    ``mode="off"`` summary without touching any row. The summary is
    overwritten to ``<data_dir>/system-status/attachment-archive-cleanup.json``
    and read back by the admin ``GET /system/brief``.
    """

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        file_store: FileStore,
        artifact_store: ArtifactStore,
        clock: Callable[[], int] = now_epoch,
        first_sweep_delay_s: float = FIRST_SWEEP_DELAY_S,
        sweep_interval_s: float = SWEEP_INTERVAL_S,
    ) -> None:
        """
        :param conversation_store: Source of archived sessions and revisions.
        :param file_store: File rows; owns the purge claim state.
        :param artifact_store: Blob bytes to delete.
        :param clock: Epoch-seconds clock; tests inject a fixed one.
        :param first_sweep_delay_s: Delay before the first sweep after start.
        :param sweep_interval_s: Delay between later sweeps.
        """
        self._conversation_store = conversation_store
        self._file_store = file_store
        self._artifact_store = artifact_store
        self._clock = clock
        self._first_sweep_delay_s = first_sweep_delay_s
        self._sweep_interval_s = sweep_interval_s
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start this server process's cleanup loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="attachment-archive-cleanup")

    async def shutdown(self) -> None:
        """Stop the cleanup loop and wait for cancellation to settle."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        await asyncio.sleep(self._first_sweep_delay_s)
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Attachment archive cleanup sweep failed; retrying later")
            await asyncio.sleep(self._sweep_interval_s)

    async def sweep_once(self, *, now: int | None = None) -> CleanupSummary:
        """
        Run one complete sweep and overwrite the summary file.

        :param now: Epoch-seconds reference time; the clock's value when
            omitted. Tests pass a fixed value to age sessions.
        :returns: The sweep's :class:`CleanupSummary`.
        """
        reference_time = self._clock() if now is None else now
        days = attachment_archive_cleanup_days()
        if days == 0:
            summary = CleanupSummary(mode="off")
        else:
            summary = await asyncio.to_thread(
                self._sweep,
                reference_time=reference_time,
                days=days,
                min_bytes=attachment_archive_cleanup_min_bytes(),
                dry_run=attachment_archive_cleanup_dry_run(),
            )
        self._write_summary(summary)
        _logger.info(
            "Attachment archive cleanup sweep mode=%s sessions_scanned=%d "
            "files_purged=%d bytes_freed=%d skipped_shared=%d skipped_unarchived=%d "
            "released=%d errors=%d capped=%s",
            summary.mode,
            summary.sessions_scanned,
            summary.files_purged,
            summary.bytes_freed,
            summary.skipped_shared,
            summary.skipped_unarchived,
            summary.released,
            summary.errors,
            summary.capped,
        )
        return summary

    def _sweep(
        self,
        *,
        reference_time: int,
        days: int,
        min_bytes: int,
        dry_run: bool,
    ) -> CleanupSummary:
        """Synchronously sweep every workspace with archived sessions.

        The lifespan's ambient workspace is the default one, so each tenant's
        ids come from one cross-workspace query; the per-session scan then runs
        scoped to that workspace. The cap and summary aggregate across them.
        """
        cutoff = reference_time - days * SECONDS_PER_DAY
        summary = CleanupSummary(mode="dry-run" if dry_run else "on")
        workspace_ids = self._conversation_store.list_workspace_ids_with_archived_before(
            archived_before=cutoff
        )
        for workspace_id in workspace_ids:
            if summary.capped:
                break
            try:
                with workspace_scope(workspace_id):
                    self._sweep_workspace(
                        reference_time=reference_time,
                        cutoff=cutoff,
                        min_bytes=min_bytes,
                        dry_run=dry_run,
                        summary=summary,
                    )
            except Exception:
                _logger.exception(
                    "Attachment archive cleanup failed for workspace %s; continuing",
                    workspace_id,
                )
                summary.errors += 1
        return summary

    def _sweep_workspace(
        self,
        *,
        reference_time: int,
        cutoff: int,
        min_bytes: int,
        dry_run: bool,
        summary: CleanupSummary,
    ) -> None:
        """Walk one workspace's archived sessions (caller holds its scope)."""
        cursor: str | None = None
        while not summary.capped:
            try:
                page = self._conversation_store.list_conversations(
                    limit=_SESSION_PAGE_SIZE,
                    after=cursor,
                    order="asc",
                    sort_by="archived_at",
                    archived_only=True,
                    archived_before=cutoff,
                    include_search_match=False,
                )
            except StaleCursorError:
                # A session deleted between pages; resume from the top next
                # sweep — done rows are skipped, so a re-scan is safe.
                _logger.warning(
                    "Attachment archive cleanup page cursor vanished; next sweep retries"
                )
                break
            for session in page.data:
                summary.sessions_scanned += 1
                try:
                    self._process_session(
                        session,
                        reference_time=reference_time,
                        min_bytes=min_bytes,
                        dry_run=dry_run,
                        summary=summary,
                    )
                except Exception:
                    _logger.exception(
                        "Attachment archive cleanup failed for session %s; continuing",
                        session.id,
                    )
                    summary.errors += 1
                if summary.capped:
                    break
            if not page.has_more or page.last_id is None:
                break
            cursor = page.last_id

    def _process_session(
        self,
        session: Conversation,
        *,
        reference_time: int,
        min_bytes: int,
        dry_run: bool,
        summary: CleanupSummary,
    ) -> None:
        """Apply the authorization protocol to one archived session's rows."""
        session_id = session.id
        after: str | None = None
        while True:
            page = self._file_store.list(
                session_id=session_id,
                limit=_FILE_PAGE_SIZE,
                after=after,
                order="asc",
            )
            for stored in page.data:
                if not (
                    isinstance(stored.source_metadata, Mapping)
                    and stored.source_metadata.get("delivery") == "filesystem"
                ):
                    continue
                if stored.bytes < min_bytes:
                    continue
                purge = _purge_metadata(stored.source_metadata)
                if purge is not None and purge.get("state") == "done":
                    continue
                if dry_run:
                    self._log_row("would purge", session, stored)
                    continue
                blob_key = stored.blob_key or stored.id
                claim_revision = session.archive_revision
                if purge is not None and purge.get("state") == "claimed":
                    claimed_revision = purge.get("revision")
                    if isinstance(claimed_revision, int) and not isinstance(
                        claimed_revision, bool
                    ):
                        # Resume a prior sweep's claim under its own revision.
                        claim_revision = claimed_revision
                    else:
                        purge = None
                if purge is None:
                    if (
                        self._file_store.claim_purge(
                            blob_key,
                            session_id=session_id,
                            revision=session.archive_revision,
                            now=reference_time,
                        )
                        is None
                    ):
                        summary.skipped_shared += 1
                        continue
                    claim_revision = session.archive_revision
                # Every referrer must be claimed under this session and
                # revision; a fork copying the row is caught here and released.
                referrers = self._file_store.list_blob_referrers(blob_key)
                if any(
                    not self._is_claimed_referrer(
                        row, session_id=session_id, revision=claim_revision
                    )
                    for row in referrers
                ):
                    self._file_store.release_purge(blob_key)
                    summary.released += 1
                    summary.skipped_shared += 1
                    continue
                # Re-read the session: an archive change after the claim (or
                # after a crash) releases it, leaving the row live.
                current = self._conversation_store.get_conversation(session_id)
                if (
                    current is None
                    or not current.archived
                    or current.archive_revision != claim_revision
                ):
                    self._file_store.release_purge(blob_key)
                    summary.released += 1
                    summary.skipped_unarchived += 1
                    continue
                # An unarchive landing between this re-read and the delete is a
                # residual race, accepted like the fork race; no lock spans stores.
                self._artifact_store.delete(blob_key)
                self._file_store.finish_purge(blob_key, now=reference_time)
                summary.files_purged += 1
                summary.bytes_freed += stored.bytes
                self._log_row("purged", session, stored)
                if summary.files_purged >= PURGE_CAP:
                    summary.capped = True
                    return
            if summary.capped or not page.has_more or page.last_id is None:
                return
            after = page.last_id

    @staticmethod
    def _is_claimed_referrer(
        row: StoredFile,
        *,
        session_id: str,
        revision: int,
    ) -> bool:
        """Whether *row* is claimed by this session and revision."""
        purge = _purge_metadata(row.source_metadata)
        return (
            row.session_id == session_id
            and purge is not None
            and purge.get("state") == "claimed"
            and purge.get("revision") == revision
        )

    @staticmethod
    def _log_row(action: str, session: Conversation, stored: StoredFile) -> None:
        """Log one purged (or would-purge) row with its identifying fields."""
        _logger.info(
            "Attachment archive cleanup %s session=%s file_id=%s filename=%s "
            "bytes=%d archived_at=%s",
            action,
            session.id,
            stored.id,
            stored.filename,
            stored.bytes,
            session.archived_at,
        )

    def _write_summary(self, summary: CleanupSummary) -> None:
        """Atomically replace the last-run summary; never follow a symlink."""
        path = resolve_data_dir() / "system-status" / SUMMARY_FILENAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                _logger.warning(
                    "Refusing to write attachment archive cleanup summary through %s",
                    path,
                )
                return
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(asdict(summary), handle)
                os.replace(tmp_name, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError:
            _logger.warning(
                "Could not write attachment archive cleanup summary to %s",
                path,
                exc_info=True,
            )
