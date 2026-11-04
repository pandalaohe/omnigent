"""Opt-in, paced deletion of long-archived and idle session trees."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import now_epoch
from omnigent.server.server_config import session_retention_days, session_retention_dry_run
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_logger = logging.getLogger(__name__)
FIRST_SWEEP_DELAY_S = 600
SWEEP_INTERVAL_S = 86400
DELETE_DELAY_S = 0.2
SESSION_CAP = 1000
_PAGE_SIZE = 200


@dataclass
class RetentionSummary:
    """Last sweep's totals across all workspaces."""

    mode: str
    trees_seen: int = 0
    sessions_deleted: int = 0
    skipped: int = 0
    errors: int = 0
    capped: bool = False


class SessionRetention:
    """Delete via the existing authorized route body, children before parents."""

    def __init__(
        self,
        conversation_store: SqlAlchemyConversationStore,
        delete_session: Callable[[str], Awaitable[object]],
        *,
        session_tree_idle: Callable[[list[str]], Awaitable[bool]],
        data_dir: Path,
    ) -> None:
        self._store = conversation_store
        self._delete_session = delete_session
        self._session_tree_idle = session_tree_idle
        self._data_dir = data_dir
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="session-retention")

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        await asyncio.sleep(FIRST_SWEEP_DELAY_S)
        while True:
            try:
                await self.sweep_once()
            except Exception:
                _logger.exception("Session retention sweep failed; retrying tomorrow")
            await asyncio.sleep(SWEEP_INTERVAL_S)

    async def sweep_once(self, *, now: int | None = None) -> RetentionSummary:
        """Read current settings, process bounded trees, and save a summary."""
        days = session_retention_days()
        dry_run = session_retention_dry_run()
        summary = RetentionSummary(mode="off" if not days else "dry-run" if dry_run else "on")
        if days:
            cutoff = (now_epoch() if now is None else now) - days * 86400
            workspaces = await asyncio.to_thread(
                self._store.list_workspace_ids_with_archived_before, archived_before=cutoff
            )
            for workspace_id in workspaces:
                try:
                    with workspace_scope(workspace_id):
                        await self._sweep_workspace(cutoff, dry_run, summary)
                except Exception:
                    summary.errors += 1
                    _logger.exception("Session retention failed in workspace %s", workspace_id)
                if summary.capped:
                    break
        await asyncio.to_thread(self._write_summary, summary)
        _logger.info("Session retention: %s", asdict(summary))
        return summary

    async def _sweep_workspace(
        self, cutoff: int, dry_run: bool, summary: RetentionSummary
    ) -> None:
        after: tuple[int, str] | None = None
        seen: set[str] = set()
        while not summary.capped:
            roots = await asyncio.to_thread(
                self._store.list_expired_session_trees, cutoff, _PAGE_SIZE, after=after
            )
            if not roots:
                return
            for root_id, archived_at in roots:
                # Re-archiving can move a skipped tree past the paging cursor.
                if root_id in seen:
                    continue
                seen.add(root_id)
                summary.trees_seen += 1
                try:
                    session_ids = await asyncio.to_thread(
                        self._store.list_session_tree_bottom_up, root_id
                    )
                    if dry_run:
                        conversations = await asyncio.to_thread(
                            self._store.get_conversations, session_ids
                        )
                        _logger.info(
                            "Would delete session tree root=%s title=%r "
                            "sessions=%d last_activity=%d",
                            root_id,
                            conversations[root_id].title,
                            len(session_ids),
                            max(conv.updated_at for conv in conversations.values()),
                        )
                        continue
                    for index, session_id in enumerate(session_ids):
                        if not await asyncio.to_thread(
                            self._store.session_tree_still_expired, root_id, archived_at, cutoff
                        ) or not await self._session_tree_idle(session_ids[index:]):
                            summary.skipped += 1
                            break
                        # Deletion leases follow task completion, even on failure.
                        await asyncio.ensure_future(self._delete_session(session_id))
                        summary.sessions_deleted += 1
                        if summary.sessions_deleted >= SESSION_CAP:
                            summary.capped = True
                            return
                        await asyncio.sleep(DELETE_DELAY_S)
                except Exception:
                    summary.errors += 1
                    _logger.exception("Session retention failed for tree %s; continuing", root_id)
            last_root_id, last_archived_at = roots[-1]
            after = (last_archived_at, last_root_id)

    def _write_summary(self, summary: RetentionSummary) -> None:
        path = self._data_dir / "system-status" / "session-retention.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(asdict(summary), handle)
                os.replace(temporary, path)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary)
        except OSError:
            _logger.warning("Could not write session retention summary", exc_info=True)
