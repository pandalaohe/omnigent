"""Visitor screenshot checks over ordinary session files."""

from __future__ import annotations

import logging
from typing import Any

from omnigent.db.utils import now_epoch
from omnigent.entities import Comment, StoredFile
from omnigent.entities.element_annotation import parse_element_anchor
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.file_store import FileStore

logger = logging.getLogger(__name__)
VISITOR_SCREENSHOT_SECONDS = 30 * 86400
VISITOR_SCREENSHOT_LIMIT = 100
VISITOR_SCREENSHOT_BYTES = 2 * 1024 * 1024


def screenshot_marker(file: StoredFile) -> dict[str, Any] | None:
    metadata = file.source_metadata
    marker = metadata.get("visitor_screenshot") if isinstance(metadata, dict) else None
    if not isinstance(marker, dict):
        return None
    created = marker.get("created_at")
    if (
        not isinstance(marker.get("link_key"), str)
        or len(marker["link_key"]) != 64
        or not isinstance(marker.get("comment_id"), str)
        or not isinstance(created, int)
        or isinstance(created, bool)
        or created <= 0
    ):
        return None
    return marker


def screenshot_expiry(file: StoredFile) -> int | None:
    marker = screenshot_marker(file)
    return marker["created_at"] + VISITOR_SCREENSHOT_SECONDS if marker else None


class VisitorScreenshotFiles:
    """Count and clean marked files; no separate reservation or worker."""

    def __init__(self, files: FileStore, artifacts: ArtifactStore):
        self.files = files
        self.artifacts = artifacts

    def is_bound(self, comment: Comment, file_id: str, now: int | None = None) -> bool:
        """Reject historical anchors naming another comment's or link's file."""
        anchor = parse_element_anchor(comment.anchor_content)
        screenshot = anchor.get("screenshot") if anchor else None
        if not isinstance(screenshot, dict) or screenshot.get("file_id") != file_id:
            return False
        stored = self.files.get(file_id, session_id=comment.conversation_id)
        marker = screenshot_marker(stored) if stored else None
        expiry = screenshot_expiry(stored) if stored else None
        return bool(
            marker
            and marker["comment_id"] == comment.id
            and marker["link_key"] == screenshot.get("link_key")
            and expiry == screenshot.get("expires_at")
            and expiry is not None
            and (now_epoch() if now is None else now) < expiry
        )

    def delete(self, file_id: str, session_id: str) -> None:
        """Keep a failed blob deletion discoverable on the next lazy pass."""
        stored = self.files.get(file_id, session_id=session_id)
        if stored is None:
            return
        blob_key = stored.blob_key or stored.id
        referrers = self.files.list_blob_referrers(blob_key)
        if len(referrers) == 1 and referrers[0].id == stored.id:
            self.artifacts.delete(blob_key)
        self.files.delete(file_id, session_id=session_id)

    def clean_expired_and_count(
        self, session_id: str, *, link_key: str | None = None, now: int | None = None
    ) -> int:
        """Page through session metadata, then remove due marked files.

        Listing finishes before deletion because deleting a pagination cursor
        would make the next page unreachable. A link-scoped call also returns
        its remaining screenshot count for ordinary quota admission.
        """
        reference = now_epoch() if now is None else now
        after: str | None = None
        expired: list[str] = []
        count = 0
        while True:
            page = self.files.list(session_id, limit=100, after=after)
            for stored in page.data:
                marker = screenshot_marker(stored)
                if marker is None or (link_key is not None and marker["link_key"] != link_key):
                    continue
                expiry = screenshot_expiry(stored)
                if expiry is not None and expiry <= reference:
                    expired.append(stored.id)
                else:
                    count += 1
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
        for file_id in expired:
            try:
                self.delete(file_id, session_id)
            except Exception:  # noqa: BLE001 - retry failed cleanup on a later access
                count += 1
                logger.warning(
                    "Visitor screenshot cleanup failed for file %s", file_id, exc_info=True
                )
        return count
