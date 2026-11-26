"""Weak ETag helpers for conditional workspace file reads."""

from __future__ import annotations

import os


def workspace_file_etag(st: os.stat_result) -> str:
    """Build a weak ``ETag`` from cheap stat metadata (size, mtime_ns, inode).

    Uses ``st_size``, ``st_mtime_ns``, and ``st_ino`` so in-place edits,
    truncations, atomic renames, and symlink retargets all yield a new
    validator without hashing file bytes.

    :param st: ``os.stat`` / ``os.fstat`` result for a regular file.
    :returns: A weak entity tag of the form ``W/"<size>-<mtime_ns>-<ino>"``.
    """
    return f'W/"{st.st_size:x}-{st.st_mtime_ns:x}-{st.st_ino:x}"'


def if_none_match_matches(header: str | None, etag: str) -> bool:
    """Check an ``If-None-Match`` request header against an entity tag.

    Uses RFC 9110 §8.8.3.2 weak comparison: strips any ``W/`` prefix from both
    sides before comparing quoted opaque-tags. Supports comma-separated tag
    lists and the wildcard ``*``.

    :param header: Raw ``If-None-Match`` header value, or ``None`` when absent.
    :param etag: Current entity tag (strong or weak, quoted).
    :returns: ``True`` when the client's cached representation is still current.
    """
    if not header:
        return False
    target = etag.strip().removeprefix("W/")
    if len(target) < 2 or not (target.startswith('"') and target.endswith('"')):
        return False
    for raw in header.split(","):
        candidate = raw.strip()
        if candidate == "*":
            return True
        if candidate.removeprefix("W/") == target:
            return True
    return False
