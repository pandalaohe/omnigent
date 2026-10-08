"""
Shared helpers for materializing multimodal attachment blocks to disk.

Both native executors (Claude Code, Codex) receive user messages whose
image/file content blocks carry resolved base64 data URIs. Inlining that
base64 into the text sent to the native CLI is wrong: Claude Code cannot
view it, and the Codex app-server rejects any turn whose input text
exceeds 1 MiB (``input_too_large``). Instead each executor decodes the
data URI to a file on disk and references it by path — Claude Code via
its Read tool, Codex via a ``localImage`` input item. This module owns
that shared decode-and-write step.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import errno
import hashlib
import logging
import os
import re
import shutil
import stat
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path, PurePath
from types import ModuleType
from typing import IO, Any, Literal

import httpx

from omnigent.debug_logging import debug_event
from omnigent.process_logging import data_dir

_logger = logging.getLogger(__name__)
_ATTACHMENT_READ_ATTEMPTS = 3
_ATTACHMENT_RESOLVE_TIMEOUT_S = 60.0
_ATTACHMENT_RETRY_STATUSES = frozenset({408, 500, 502, 503, 504})

# Characters that would corrupt a "[Attached: ...]" / "[Attachment ...]"
# marker line for the consumers that regex-match it (forwarders, title
# seeding): brackets end the match early, newlines break the line shape.
_MARKER_UNSAFE = re.compile(r"[\[\]\r\n]")
FRAMEWORK_NOTICE_BLOCK_TYPE = "_omnigent_framework_notice"

# Maps a data-URI MIME type to the file extension used when no filename
# is supplied, e.g. ``"image/png"`` -> ``".png"``.
MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
}


@dataclass(frozen=True)
class DataUri:
    """
    Decoded components of a ``data:`` URI.

    :param mime_type: The MIME type, e.g. ``"image/png"``.
    :param base64_payload: The base64-encoded payload following the
        comma, e.g. ``"iVBORw0KGgo..."``.
    """

    mime_type: str
    base64_payload: str


def parse_data_uri(uri: str) -> DataUri:
    """
    Split a ``data:`` URI into its MIME type and base64 payload.

    :param uri: Data URI string,
        e.g. ``"data:image/png;base64,iVBOR..."``.
    :returns: A :class:`DataUri` with the MIME type and base64 payload.
    :raises ValueError: If the URI has no comma separating header from
        payload.
    """
    # "data:image/png;base64,iVBOR..."
    header, _, payload = uri.partition(",")
    if not payload:
        raise ValueError(f"Malformed data URI: no comma separator in {uri[:80]}")
    # header = "data:image/png;base64"
    mime_part = header.removeprefix("data:").removesuffix(";base64")
    return DataUri(mime_type=mime_part, base64_payload=payload)


# These formats require the harness's filesystem tools. Match by extension
# because browsers can mislabel Office documents as ZIP or generic binary data.
_FILESYSTEM_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
    {".zip", ".docx", ".xlsx", ".pptx", ".db", ".sqlite", ".sqlite3"}
)

# Advertised by builds that can deliver and restore these attachments.
CAP_FILESYSTEM_ATTACHMENTS = "filesystem_attachments"
# Advertised by builds that stream by-path attachments to the agent host.
CAP_PATH_ATTACHMENTS = "path_attachments"


def requires_filesystem(filename: str | None) -> bool:
    """
    Whether an attachment needs a harness that can open local files.

    :param filename: The original filename, e.g. ``"report.docx"``.
    :returns: True for supported archives, Office documents, and databases.
    """
    return bool(
        filename and PurePath(filename).suffix.lower() in _FILESYSTEM_ATTACHMENT_EXTENSIONS
    )


def is_by_path(filename: str | None, source_metadata: object) -> bool:
    """
    Whether a stored file is delivered by path instead of inlined.

    The upload route persists ``source_metadata["delivery"] = "filesystem"``
    for every file that must not enter the model context; the stored key wins
    over the filename so a row's delivery cannot drift with a rename. Rows
    written before that key existed fall back to the extension rule.

    :param filename: The stored filename, e.g. ``"clip.mp4"``.
    :param source_metadata: The stored row's ``source_metadata`` value.
    :returns: True when the file is delivered by path.
    """
    if isinstance(source_metadata, Mapping) and "delivery" in source_metadata:
        return source_metadata["delivery"] == "filesystem"
    return requires_filesystem(filename)


def is_purged(source_metadata: object) -> bool:
    """
    Whether the archive cleanup deleted the stored bytes of this row.

    Only ``done`` means gone: a ``claimed`` row behaves like a live one (its
    content is served and quota-counted) until the artifact-store delete
    completes. Old rows without a purge record read as live.

    :param source_metadata: The stored row's ``source_metadata`` value.
    :returns: True when the row's purge state is ``done``.
    """
    if not isinstance(source_metadata, Mapping):
        return False
    purge = source_metadata.get("purge")
    return isinstance(purge, Mapping) and purge.get("state") == "done"


def inline_filesystem_attachment_name(content: object) -> str | None:
    """
    Filename of the first attachment requiring filesystem tools with inline bytes.

    These files must arrive as uploaded ``file_id`` references, so the
    upload route's harness, denylist, and quota checks run before any bytes
    reach the sandbox.

    :param content: A message's content blocks.
    :returns: The offending filename, e.g. ``"payload.zip"``, or ``None``.
    """
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        filename = block.get("filename")
        if not isinstance(filename, str) or not requires_filesystem(filename):
            continue
        if block.get("file_data") or block.get("image_url"):
            return filename
    return None


def attachment_cache_dir(bridge_dir: Path) -> Path:
    """Return the local attachment cache for a native session's bridge.

    The bridge path identifies the session across live turns and resume rebuilds.
    All harnesses share ``~/.omnigent/attachments/`` (or ``OMNIGENT_DATA_DIR``).
    """
    key = hashlib.sha256(os.fsencode(bridge_dir.resolve())).hexdigest()[:32]
    return data_dir().resolve() / "attachments" / key


def session_attachment_dir(session_id: str) -> Path:
    """Return the deterministic host directory for a session's by-path files.

    Keyed by session id so the path survives a restart and is the same on
    every host that runs the session; files live under ``<file_id>/<name>``.
    """
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:32]
    return data_dir().resolve() / "attachments" / f"s-{digest}"


# One "[Attached: <path>]" line whose path ends in the per-session layout
# ``...attachments<sep>s-<32 hex><sep><file_id><sep><name>``, with either
# separator so a transcript written on another OS can still be redirected.
_ATTACHED_PATH_RE = re.compile(
    r"\[Attached: [^\]\r\n]*?attachments[\\/]s-[0-9a-f]{32}[\\/]([^\\/\]]+)[\\/]([^\\/\]]+)\]"
)


def contains_attached_path(text: str) -> bool:
    """
    Whether *text* carries an ``[Attached: ...]`` line in the session layout.

    A line already pointing at this host's session dir is a match too, so
    callers that restore files can trigger on the marker even when the rewrite
    leaves the text unchanged.

    :param text: Persisted text that may contain attachment reference lines.
    :returns: ``True`` when any ``[Attached: ...]`` line matches the layout.
    """
    return _ATTACHED_PATH_RE.search(text) is not None


def rewrite_attached_paths(text: str, session_id: str) -> str:
    """
    Redirect persisted ``[Attached: ...]`` lines to this host's session dir.

    Compaction persists attachment reference lines that point at the host
    directory of whichever machine produced them. Rewriting the session key
    and ``file_id``/``name`` tail keeps the same file identity while moving
    the prefix to :func:`session_attachment_dir` for *session_id*. Lines that
    do not match the layout are left exactly as written.

    :param text: Persisted text that may contain attachment reference lines.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: *text* with matching paths pointing at this session's dir.
    """

    def _replace(match: re.Match[str]) -> str:
        path = session_attachment_dir(session_id) / match.group(1) / match.group(2)
        return f"[Attached: {path}]"

    return _ATTACHED_PATH_RE.sub(_replace, text)


def mark_purged_attachments(text: str, session_id: str, purged: Mapping[str, str]) -> str:
    """
    Replace purged attachment lines with the visible could-not-load marker.

    The archive cleanup deletes a purged row's bytes, so a persisted
    ``[Attached: ...]`` line for that row would hand the harness a dead path
    and the model a hallucinated file. A line whose ``file_id`` is purged
    becomes the :func:`unresolved_attachment_marker` for the row's stored
    name, unless the host file at the rewritten path still exists — per
    design, a surviving copy keeps working. Lines of live rows and lines that
    do not match the layout are left exactly as written.

    :param text: Persisted text that may contain attachment reference lines.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param purged: This session's purged rows, ``file_id`` -> stored name.
    :returns: *text* with purged rows' lines replaced by the marker.
    """
    if not purged:
        return text

    def _replace(match: re.Match[str]) -> str:
        file_id, path_name = match.group(1), match.group(2)
        if file_id not in purged:
            return match.group(0)
        if (session_attachment_dir(session_id) / file_id / path_name).exists():
            return match.group(0)
        return unresolved_attachment_marker({"filename": purged[file_id]})

    return _ATTACHED_PATH_RE.sub(_replace, text)


def materialize_attachment(block: Mapping[str, object], bridge_dir: Path) -> Path | None:
    """
    Decode an attachment into the session's cache outside the working directory.

    The artifact store retains the original upload. Local copies are recreated
    when rebuilding history. Files are never extracted or made executable.

    :param block: A content block dict with ``type`` of
        ``"input_image"`` or ``"input_file"``. Expected to carry a
        resolved data URI in ``image_url`` or ``file_data``,
        e.g. ``"data:image/png;base64,iVBOR..."``. May also carry a
        ``filename``, e.g. ``"diagram.png"``.
    :param bridge_dir: Session bridge path, used to identify its attachment cache.
    :returns: Path to the written file, or ``None`` if the block could
        not be materialized (missing data URI, decode error).
    """
    decoded = _decode_attachment_block(block)
    if decoded is None:
        return None
    raw_bytes, filename = decoded

    if filename in (".", "..") or os.sep in filename:
        return None

    attachments_dir = attachment_cache_dir(bridge_dir)
    if not _dir_fd_supported():
        return _materialize_attachment_by_path(attachments_dir, filename, raw_bytes)
    try:
        attachments_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_fd = os.open(attachments_dir.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with contextlib.suppress(FileExistsError):
                os.mkdir(attachments_dir.name, mode=0o700, dir_fd=root_fd)
            dir_fd = os.open(
                attachments_dir.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        finally:
            os.close(root_fd)
    except OSError:
        _logger.warning("Refusing to materialize into %s", attachments_dir, exc_info=True)
        return None
    try:
        stem, suffix = os.path.splitext(filename)
        digest = hashlib.sha256(raw_bytes).hexdigest()[:12]
        for name in (filename, f"{stem}_{digest}{suffix}"):
            outcome = _place_no_follow(dir_fd, name, raw_bytes)
            if outcome == "symlink":
                _logger.warning("Refusing to write through symlink %s", attachments_dir / name)
                return None
            if outcome == "placed":
                return attachments_dir / name
        _logger.warning("Attachment names for %s already hold other content", filename)
        return None
    except OSError:
        _logger.warning("Failed to materialize attachment %s", filename, exc_info=True)
        return None
    finally:
        os.close(dir_fd)


def _materialize_attachment_by_path(
    attachments_dir: Path, filename: str, raw_bytes: bytes
) -> Path | None:
    """Place decoded bytes where dir_fd-relative no-follow opens are unavailable.

    Windows hosts have no ``os.O_DIRECTORY`` / ``dir_fd`` support; the same
    traversal and symlink refusals are enforced with path-based lstat checks.
    Writes land in a temporary sibling and are published with ``os.replace``.
    """
    try:
        attachments_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        _logger.warning("Refusing to materialize into %s", attachments_dir, exc_info=True)
        return None
    for directory in (attachments_dir.parent, attachments_dir):
        if directory.is_symlink():
            _logger.warning("Refusing to materialize into %s", attachments_dir)
            return None
    stem, suffix = os.path.splitext(filename)
    digest = hashlib.sha256(raw_bytes).hexdigest()[:12]
    for name in (filename, f"{stem}_{digest}{suffix}"):
        target = attachments_dir / name
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        except OSError:
            _logger.warning("Refusing to write through %s", target, exc_info=True)
            return None
        if info is not None:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                _logger.warning("Refusing to write through %s", target)
                return None
            if info.st_size == len(raw_bytes):
                try:
                    existing = target.read_bytes()
                except OSError:
                    # An unreadable target is unusable; fall through to the
                    # collision name like an unclearable execute bit.
                    _logger.warning("Refusing to reuse unreadable %s", target, exc_info=True)
                    continue
                if existing == raw_bytes:
                    try:
                        target.chmod(stat.S_IMODE(info.st_mode) & ~0o111)
                    except OSError:
                        # A reused file must not stay executable; if the bits
                        # can't be cleared, fall through to the collision name.
                        continue
                    return target
            continue
        temp_path = attachments_dir / f"{name}.{uuid.uuid4().hex}.tmp"
        created = False
        published = False
        try:
            try:
                with open(temp_path, "xb") as handle:
                    created = True
                    view = memoryview(raw_bytes)
                    while view:
                        view = view[handle.write(view) :]
            except FileExistsError:
                # A colliding temp name is not ours to touch; try the
                # other candidate name.
                continue
            except OSError:
                _logger.warning("Failed to materialize attachment %s", filename, exc_info=True)
                return None
            try:
                os.replace(temp_path, target)
            except OSError:
                _logger.warning("Failed to place attachment %s", target, exc_info=True)
                return None
            published = True
            return target
        finally:
            if created and not published:
                with contextlib.suppress(OSError):
                    temp_path.unlink()
    _logger.warning("Attachment names for %s already hold other content", filename)
    return None


def _dir_fd_supported() -> bool:
    """Whether this host exposes dir_fd-relative no-follow opens."""
    return os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY")


def _is_safe_path_component(part: object, pathmod: ModuleType = os.path) -> bool:
    """Whether *part* is one safe path component: a non-empty string that is not
    ``.``/``..``, has no NUL, no ``sep``/``altsep``, and no drive."""
    if not isinstance(part, str) or not part or part in (".", "..") or "\x00" in part:
        return False
    if pathmod.sep in part:
        return False
    if pathmod.altsep is not None and pathmod.altsep in part:
        return False
    return not pathmod.splitdrive(part)[0]


async def _stream_file_content(
    file_id: str,
    *,
    session_id: str,
    client: httpx.AsyncClient,
    handle: IO[bytes],
) -> int:
    """Stream one file resource's bytes into *handle* and return the count."""
    count = 0
    async with client.stream(
        "GET",
        f"{_file_resource_base(session_id, file_id)}/content",
        timeout=httpx.Timeout(10.0, read=60.0),
    ) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            handle.write(chunk)
            count += len(chunk)
    return count


async def materialize_file_reference(
    file_id: str,
    meta: Mapping[str, object],
    *,
    session_id: str,
    client: httpx.AsyncClient,
) -> Path | None:
    """
    Stream a by-path attachment to its deterministic host path.

    Writes ``<data_dir>/attachments/s-<session key>/<file_id>/<name>`` so an
    ``[Attached: ...]`` line keeps pointing at the same file across turns. An
    existing regular file of the expected size is reused; otherwise the
    content is streamed to a temporary sibling and atomically renamed. The
    whole body is never buffered. Marker-breaking characters in the stored
    name are replaced in the local name so the path cannot break the marker.
    Hosts without dir_fd-relative opens (Windows) use path-based checks.

    :param file_id: The stored file's id, e.g. ``"c531a3..."``.
    :param meta: The file resource JSON, carrying ``name`` and
        ``metadata.bytes``.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param client: HTTP client pointed at the Omnigent server.
    :returns: The host path, or ``None`` when a name is unsafe, the transfer
        failed, or the streamed size did not match the stored size.
    """
    name = meta.get("name")
    if not _is_safe_path_component(name) or not _is_safe_path_component(file_id):
        _logger.warning("Refusing unsafe attachment name for file_id=%s", file_id)
        return None
    assert isinstance(name, str)  # narrowed by _is_safe_path_component
    local_name = _MARKER_UNSAFE.sub("_", name)
    resource_metadata = meta.get("metadata")
    expected_bytes = (
        resource_metadata.get("bytes") if isinstance(resource_metadata, Mapping) else None
    )
    if _dir_fd_supported():
        return await _materialize_file_reference_no_follow(
            file_id,
            local_name,
            expected_bytes,
            session_id=session_id,
            client=client,
        )
    return await _materialize_file_reference_by_path(
        file_id,
        local_name,
        expected_bytes,
        session_id=session_id,
        client=client,
    )


async def _materialize_file_reference_no_follow(
    file_id: str,
    local_name: str,
    expected_bytes: object,
    *,
    session_id: str,
    client: httpx.AsyncClient,
) -> Path | None:
    """Deliver a by-path file with dir_fd-relative no-follow opens.

    :param file_id: The stored file's id.
    :param local_name: Marker-safe base filename.
    :param expected_bytes: Stored size, or a non-int when unknown.
    :param session_id: Omnigent conversation id.
    :param client: HTTP client pointed at the Omnigent server.
    :returns: The host path, or ``None`` on a refusal or transfer failure.
    """
    attachments_dir = session_attachment_dir(session_id)
    target_dir = attachments_dir / file_id
    try:
        attachments_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_fd = os.open(attachments_dir.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        _logger.warning("Refusing to materialize into %s", target_dir, exc_info=True)
        return None
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(attachments_dir.name, mode=0o700, dir_fd=root_fd)
        dir_fd = os.open(
            attachments_dir.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
    except OSError:
        _logger.warning("Refusing to materialize into %s", target_dir, exc_info=True)
        return None
    finally:
        os.close(root_fd)
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(file_id, mode=0o700, dir_fd=dir_fd)
        target_fd = os.open(file_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        _logger.warning("Refusing to materialize into %s", target_dir, exc_info=True)
        return None
    finally:
        os.close(dir_fd)

    try:
        try:
            info = os.stat(local_name, dir_fd=target_fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None:
            if not stat.S_ISREG(info.st_mode):
                _logger.warning("Refusing to write through %s", target_dir / local_name)
                return None
            if info.st_size == expected_bytes:
                return target_dir / local_name

        temp_name = f"{local_name}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=target_fd,
            )
        except OSError:
            _logger.warning(
                "Failed to create temporary attachment %s", target_dir / temp_name, exc_info=True
            )
            return None
        count = 0
        published = False
        try:
            try:
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    count = await _stream_file_content(
                        file_id, session_id=session_id, client=client, handle=handle
                    )
            except (httpx.HTTPError, OSError):
                _logger.warning(
                    "failed to materialize file_id=%s for session=%s",
                    file_id,
                    session_id,
                    exc_info=True,
                )
                return None
            if isinstance(expected_bytes, int) and count != expected_bytes:
                _logger.warning(
                    "attachment file_id=%s for session=%s streamed %d bytes, expected %d",
                    file_id,
                    session_id,
                    count,
                    expected_bytes,
                )
                return None
            try:
                os.replace(temp_name, local_name, src_dir_fd=target_fd, dst_dir_fd=target_fd)
            except OSError:
                _logger.warning(
                    "failed to place attachment %s", target_dir / local_name, exc_info=True
                )
                return None
            published = True
            return target_dir / local_name
        finally:
            if fd >= 0:
                os.close(fd)
            if not published:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name, dir_fd=target_fd)
    finally:
        os.close(target_fd)


async def _materialize_file_reference_by_path(
    file_id: str,
    local_name: str,
    expected_bytes: object,
    *,
    session_id: str,
    client: httpx.AsyncClient,
) -> Path | None:
    """Deliver a by-path file where dir_fd-relative opens are unavailable.

    Windows hosts have no ``os.O_DIRECTORY`` / ``dir_fd`` support; the same
    traversal and symlink refusals are enforced with path-based lstat checks.

    :param file_id: The stored file's id.
    :param local_name: Marker-safe base filename.
    :param expected_bytes: Stored size, or a non-int when unknown.
    :param session_id: Omnigent conversation id.
    :param client: HTTP client pointed at the Omnigent server.
    :returns: The host path, or ``None`` on a refusal or transfer failure.
    """
    attachments_dir = session_attachment_dir(session_id)
    target_dir = attachments_dir / file_id
    try:
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        _logger.warning("Refusing to materialize into %s", target_dir, exc_info=True)
        return None
    for directory in (attachments_dir.parent, attachments_dir, target_dir):
        if directory.is_symlink():
            _logger.warning("Refusing to materialize into %s", target_dir)
            return None

    target = target_dir / local_name
    try:
        info = target.lstat()
    except FileNotFoundError:
        info = None
    except OSError:
        _logger.warning("Refusing to write through %s", target, exc_info=True)
        return None
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            _logger.warning("Refusing to write through %s", target)
            return None
        if info.st_size == expected_bytes:
            return target

    temp_path = target_dir / f"{local_name}.{uuid.uuid4().hex}.tmp"
    created = False
    published = False
    try:
        try:
            with open(temp_path, "xb") as handle:
                created = True
                count = await _stream_file_content(
                    file_id, session_id=session_id, client=client, handle=handle
                )
        except (httpx.HTTPError, OSError):
            _logger.warning(
                "failed to materialize file_id=%s for session=%s",
                file_id,
                session_id,
                exc_info=True,
            )
            return None
        if isinstance(expected_bytes, int) and count != expected_bytes:
            _logger.warning(
                "attachment file_id=%s for session=%s streamed %d bytes, expected %d",
                file_id,
                session_id,
                count,
                expected_bytes,
            )
            return None
        try:
            os.replace(temp_path, target)
        except OSError:
            _logger.warning("failed to place attachment %s", target, exc_info=True)
            return None
        published = True
        return target
    finally:
        if created and not published:
            with contextlib.suppress(OSError):
                temp_path.unlink()


async def restore_session_attachments(
    session_id: str,
    client: httpx.AsyncClient,
    *,
    purged: MutableMapping[str, str] | None = None,
) -> bool:
    """
    Re-materialize every by-path file a session owns on this host.

    The deterministic host copies can be removed by cleanup on another host
    (or on this one after a CLI release); a transcript that still references
    them needs the files back before the harness resumes. The session file
    listing supplies the by-path rows; a file whose size still matches is
    reused without fetching. Rows the archive cleanup purged (``done``) are
    skipped — their bytes are gone by design. Every failure is logged and
    skipped so a resume is never blocked by one missing attachment.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param client: HTTP client pointed at the Omnigent server.
    :param purged: When given, filled with this session's purged by-path
        rows (``file_id`` -> stored name) from the same listing, so a caller
        can pass them to :func:`mark_purged_attachments` without a second
        listing request.
    :returns: ``True`` when the listing succeeded and every by-path row
        materialized; ``False`` when the listing failed or at least one
        by-path row could not be materialized.
    """
    restored_all = True
    after: str | None = None
    while True:
        params: dict[str, str] = {"limit": "1000", "order": "asc"}
        if after is not None:
            params["after"] = after
        try:
            response = await client.get(
                f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/resources/files",
                params=params,
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            _logger.warning(
                "failed to list attachments for session=%s",
                session_id,
                exc_info=True,
            )
            return False
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            _logger.warning("unusable attachment listing for session=%s", session_id)
            return False
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            file_id = row.get("id")
            metadata = row.get("metadata")
            source_metadata = (
                metadata.get("source_metadata") if isinstance(metadata, Mapping) else None
            )
            name = row.get("name")
            if not isinstance(file_id, str) or not file_id:
                continue
            if not is_by_path(name if isinstance(name, str) else None, source_metadata):
                continue
            if is_purged(source_metadata):
                # The archive cleanup deleted the bytes; a 410 would fail the
                # whole restore and every later turn would retry it. Report the
                # row so the caller can mark its transcript line as lost.
                if purged is not None:
                    purged[file_id] = name if isinstance(name, str) and name else file_id
                continue
            try:
                path = await materialize_file_reference(
                    file_id, row, session_id=session_id, client=client
                )
            except Exception:  # noqa: BLE001 — a restore failure is never fatal.
                _logger.warning(
                    "failed to restore file_id=%s for session=%s",
                    file_id,
                    session_id,
                    exc_info=True,
                )
                restored_all = False
                continue
            if path is None:
                restored_all = False
        if not (isinstance(payload, dict) and payload.get("has_more")):
            return restored_all
        last_id = payload.get("last_id")
        if not isinstance(last_id, str) or not last_id:
            return restored_all
        after = last_id


def _decode_attachment_block(block: Mapping[str, object]) -> tuple[bytes, str] | None:
    """
    Decode a block's data URI and derive a safe base filename for it.

    :param block: Attachment content block (see
        :func:`materialize_attachment`).
    :returns: ``(raw_bytes, filename)`` where *filename* carries no
        directory components and no marker-breaking characters, or ``None``
        when the block has no usable data URI.
    """
    data_uri = block.get("image_url") or block.get("file_data")
    if not isinstance(data_uri, str) or not data_uri.startswith("data:"):
        if block.get("file_id"):
            _logger.error(
                "Native executor received unresolved file_id %s — "
                "content resolver may not have run",
                block["file_id"],
            )
        return None

    try:
        parsed = parse_data_uri(data_uri)
        raw_bytes = base64.b64decode(parsed.base64_payload)
    except (ValueError, binascii.Error):
        _logger.warning("Failed to decode data URI for attachment", exc_info=True)
        return None

    ext = MIME_TO_EXT.get(parsed.mime_type, "")
    filename = block.get("filename")
    if not isinstance(filename, str) or not filename:
        filename = f"attachment_{uuid.uuid4().hex[:8]}{ext}"
    else:
        # ``.name`` drops any directory part, so "../../etc/passwd" becomes
        # "passwd" and a traversal attempt can't escape the destination dir.
        filename = Path(filename).name or f"attachment_{uuid.uuid4().hex[:8]}{ext}"
    return raw_bytes, _MARKER_UNSAFE.sub("_", filename)


def _place_no_follow(
    dir_fd: int, name: str, raw_bytes: bytes
) -> Literal["placed", "taken", "symlink"]:
    """
    Reuse or create *name* under *dir_fd* without following symlinks.

    :param dir_fd: No-follow descriptor for the attachments directory.
    :param name: Base filename, no directory components.
    :param raw_bytes: Decoded attachment payload.
    :returns: ``"placed"`` when the name now holds *raw_bytes* (freshly
        created, or an identical regular file reused with its execute bits
        cleared); ``"taken"`` when it holds other content or is not a regular
        file; ``"symlink"`` when it is a symlink.
    :raises OSError: When writing a new file fails part-way.
    """
    try:
        # O_NONBLOCK keeps a FIFO planted at the name from hanging the open.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return _create_no_follow(dir_fd, name, raw_bytes)
    except OSError as exc:
        return "symlink" if exc.errno == errno.ELOOP else "taken"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(raw_bytes):
            return "taken"
        if _read_fd(fd, info.st_size) != raw_bytes:
            return "taken"
        try:
            os.fchmod(fd, stat.S_IMODE(info.st_mode) & ~0o111)
        except OSError:
            # A reused file must not stay executable; if the bits can't be
            # cleared, fall through to the collision name instead.
            return "taken"
        return "placed"
    finally:
        os.close(fd)


def _create_no_follow(
    dir_fd: int, name: str, raw_bytes: bytes
) -> Literal["placed", "taken", "symlink"]:
    """
    Create *name* exclusively under *dir_fd* and write *raw_bytes* to it.

    :param dir_fd: No-follow descriptor for the attachments directory.
    :param name: Base filename, no directory components.
    :param raw_bytes: Decoded attachment payload.
    :returns: ``"placed"`` on success; ``"taken"`` when something appeared at
        the name first; ``"symlink"`` when that something is a symlink.
    :raises OSError: When the write fails; the partial file is removed.
    """
    try:
        # Private, non-executable files; existing entries are never overwritten.
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd
        )
    except FileExistsError:
        return "taken"
    except OSError as exc:
        return "symlink" if exc.errno == errno.ELOOP else "taken"
    try:
        view = memoryview(raw_bytes)
        while view:
            view = view[os.write(fd, view) :]
    except OSError:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dir_fd)
        raise
    os.close(fd)
    return "placed"


def _read_fd(fd: int, size: int) -> bytes:
    """
    Read exactly *size* bytes from *fd*, stopping early at end of file.

    :param fd: Open file descriptor positioned at the start.
    :param size: Number of bytes to read.
    :returns: The bytes read.
    """
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# Regex source matching the exact line unresolved_attachment_marker() emits.
# Consumers (title synthesis, TUI forwarders) compose their marker-matching
# patterns from this so the shapes cannot drift apart.
UNRESOLVED_ATTACHMENT_MARKER_PATTERN = r"\[Attachment [^\]]+ could not be loaded\]"

# TUI forwarders strip local file paths from mirrored chat bubbles.
# Codex's binary file inputs also use the "[Attached file: ...]" shape.
ATTACHMENT_MARKER_STRIP_PATTERN = (
    rf"\[Attached(?: file)?:[^\]]*\]|{UNRESOLVED_ATTACHMENT_MARKER_PATTERN}"
)


def unresolved_attachment_marker(block: Mapping[str, object]) -> str:
    """
    Visible placeholder for an attachment that could not be loaded.

    Callers emit this in place of the usual path reference when
    :func:`materialize_attachment` fails, so the model (and the mirrored
    transcript) sees that an attachment was lost instead of silently
    receiving nothing and hallucinating its content.

    :param block: The content block that failed to materialize. Named by
        its ``filename``, falling back to ``file_id`` then ``"attachment"``,
        with marker-breaking characters replaced by ``_``.
    :returns: Marker line, e.g.
        ``"[Attachment photo.png could not be loaded]"``. Always matches
        :data:`UNRESOLVED_ATTACHMENT_MARKER_PATTERN`.
    """
    name = str(block.get("filename") or block.get("file_id") or "attachment")
    return f"[Attachment {_MARKER_UNSAFE.sub('_', name)} could not be loaded]"


def attachment_reference_line(block: Mapping[str, object], bridge_dir: Path) -> str:
    """
    Materialize *block* and return the transcript line referencing it.

    The line shape is load-bearing: TUI forwarders and title seeding
    (``omnigent/entities/conversation.py``) match it via
    :data:`ATTACHMENT_MARKER_STRIP_PATTERN`.

    :param block: Attachment content block (see
        :func:`materialize_attachment`).
    :param bridge_dir: Session bridge path identifying the attachment cache.
    :returns: ``"[Attached: <path>]"`` on success, else the visible
        marker from :func:`unresolved_attachment_marker`.
    """
    path = materialize_attachment(block, bridge_dir)
    if path is not None:
        return f"[Attached: {path}]"
    return unresolved_attachment_marker(block)


def has_unresolved_file_id(block: Mapping[str, object]) -> bool:
    """
    True if *block* carries a ``file_id`` no resolver has inlined yet.

    :param block: Message content block dict.
    :returns: Whether the block still needs :func:`resolve_file_id_block`.
    """
    file_id = block.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return False
    data_uri = block.get("image_url") or block.get("file_data")
    return not (isinstance(data_uri, str) and data_uri.startswith("data:"))


def resize_notice(source_metadata: object) -> str | None:
    """
    Model-facing note that an uploaded image was downscaled, or ``None``.

    The single source of truth for the resize-notice wording, shared by
    every attachment-resolution path (the in-process resolver in
    ``omnigent.runtime.content_resolver`` and the runner/native-harness
    resolver in :func:`resolve_file_id_block`) so the two never drift.

    :param source_metadata: A stored file's ``source_metadata`` dict (see
        :class:`omnigent.entities.file.StoredFile`). A downscaled image
        carries the pre-downscale ``width`` / ``height``.
    :returns: The notice text when the image was downscaled, else ``None``
        (passthrough images, non-images, or absent metadata).
    """
    dimensions = resize_dimensions(source_metadata)
    if dimensions is None:
        return None
    width, height = dimensions["width"], dimensions["height"]
    return (
        f"Note: the attached image was downscaled from {width}×{height} px to fit "
        "size limits, so you are viewing a lower-resolution version. Ask the user "
        "for a crop of the original if you need finer detail — re-uploading the whole "
        "image would be downscaled the same way."
    )


def resize_dimensions(source_metadata: object) -> dict[str, int] | None:
    """Return positive integer image dimensions from stored metadata."""
    if not isinstance(source_metadata, Mapping):
        return None
    width, height = source_metadata.get("width"), source_metadata.get("height")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        return None
    return {"width": width, "height": height}


def reject_authored_framework_notices(content: object) -> object:
    """Reject reserved context blocks in authored message content."""
    if isinstance(content, dict):
        if content.get("type") == FRAMEWORK_NOTICE_BLOCK_TYPE:
            raise ValueError("Framework notice blocks are reserved for attachment resolution")
        for value in content.values():
            reject_authored_framework_notices(value)
    elif isinstance(content, list):
        for value in content:
            reject_authored_framework_notices(value)
    return content


def framework_notice_block(source_metadata: Mapping[str, object]) -> dict[str, object]:
    """Build transient model context that must not become user text."""
    return {"type": FRAMEWORK_NOTICE_BLOCK_TYPE, "source_metadata": dict(source_metadata)}


def framework_notices(content: object) -> list[str]:
    """Extract transient framework notices from structured content."""
    if not isinstance(content, list):
        return []
    notices: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != FRAMEWORK_NOTICE_BLOCK_TYPE:
            continue
        text = resize_notice(block.get("source_metadata"))
        if text:
            notices.append(text)
    return notices


def expand_framework_notices(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render structured notices as system context at a provider boundary."""
    result: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            result.append(message)
            continue
        visible_content = [
            block
            for block in content
            if not isinstance(block, dict) or block.get("type") != FRAMEWORK_NOTICE_BLOCK_TYPE
        ]
        if len(visible_content) == len(content):
            result.append(message)
            continue
        result.extend(
            {"role": "system", "content": [{"type": "input_text", "text": notice}]}
            for notice in framework_notices(content)
        )
        if visible_content or not content:
            result.append({**message, "content": visible_content})
    return result


def codex_resize_metadata_path(path: Path, source_metadata: object) -> Path:
    """Encode resize metadata in a persistent attachment-cache alias."""
    dimensions = resize_dimensions(source_metadata)
    if dimensions is None:
        return path
    width, height = dimensions["width"], dimensions["height"]
    try:
        alias = path.with_name(
            f"{path.stem[:80]}_{hashlib.sha256(path.read_bytes()).hexdigest()[:12]}"
            f"__omnigent-downscaled-from-{width}x{height}"
            f"-request-crop-for-fine-detail{path.suffix}"
        )
        if not alias.exists():
            temporary = alias.with_name(f".{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(path, temporary)
                temporary.replace(alias)
            finally:
                temporary.unlink(missing_ok=True)
    except OSError:
        _logger.warning("Failed to add resize metadata to Codex image path", exc_info=True)
        return path
    return alias


def _file_resource_base(session_id: str, file_id: str) -> str:
    """Session-scoped URL prefix for one uploaded file's resource endpoints."""
    return (
        f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}"
        f"/resources/files/{urllib.parse.quote(file_id, safe='')}"
    )


async def fetch_file_meta(
    file_id: str,
    *,
    session_id: str,
    client: httpx.AsyncClient,
) -> dict[str, object] | None:
    """
    Fetch one uploaded file's metadata JSON.

    :param file_id: The stored file's id, e.g. ``"c531a3..."``.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param client: HTTP client pointed at the Omnigent server.
    :returns: The parsed metadata dict; ``{}`` when the body is missing or
        unusable, so the caller falls back to the content response's
        Content-Type, or ``None`` on an HTTP error.
    """
    try:
        async with asyncio.timeout(_ATTACHMENT_RESOLVE_TIMEOUT_S):
            response = await _read_attachment_resource(
                client,
                _file_resource_base(session_id, file_id),
                session_id=session_id,
                stage="metadata",
                timeout_s=10.0,
            )
    except (httpx.HTTPError, TimeoutError) as exc:
        _logger.warning(
            "Attachment %s read failed",
            "metadata",
            extra=debug_event(
                "native_attachment_read_failed",
                session_id=session_id,
                stage="metadata",
                http_status=exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None,
                exception_type=type(exc).__name__,
                deadline_exceeded=isinstance(exc, TimeoutError),
            ),
        )
        return None
    try:
        parsed = response.json() if response.content else {}
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        if response.content:
            # Unusable metadata only costs the media-type hint; the content
            # response's Content-Type header still provides it.
            _logger.warning(
                "unusable file metadata for file_id=%s in session=%s; "
                "falling back to the content headers",
                file_id,
                session_id,
            )
        return {}
    return parsed


async def _read_attachment_resource(
    client: httpx.AsyncClient,
    path: str,
    *,
    session_id: str,
    stage: str,
    timeout_s: float,
) -> httpx.Response:
    """Retry transient GET failures within the enclosing attachment deadline."""
    for attempt in range(1, _ATTACHMENT_READ_ATTEMPTS + 1):
        try:
            response = await client.get(path, timeout=timeout_s)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            retryable = status in _ATTACHMENT_RETRY_STATUSES or isinstance(
                exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
            )
            if not retryable or attempt == _ATTACHMENT_READ_ATTEMPTS:
                raise
            _logger.warning(
                "Retrying attachment %s read after transient failure",
                stage,
                extra=debug_event(
                    "native_attachment_read_retry",
                    session_id=session_id,
                    stage=stage,
                    attempt=attempt,
                    http_status=status,
                    exception_type=type(exc).__name__,
                ),
            )
            await asyncio.sleep(0.25 * 2 ** (attempt - 1))
        else:
            if attempt > 1:
                _logger.info(
                    "Attachment %s read recovered",
                    stage,
                    extra=debug_event(
                        "native_attachment_read_recovered",
                        session_id=session_id,
                        stage=stage,
                        attempts=attempt,
                    ),
                )
            return response
    raise AssertionError("attachment retry loop exited without a result")


async def resolve_file_id_block(
    block: Mapping[str, object],
    *,
    session_id: str,
    client: httpx.AsyncClient,
    meta: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, int] | None] | None:
    """
    Fetch a ``file_id`` attachment's bytes and inline them as a data URI.

    Used wherever message content must be consumed away from the server's
    file store (the out-of-process runner, transcript rebuilds): the bytes
    are fetched back through the session-scoped file resource endpoints
    and inlined under ``image_url`` (images) or ``file_data`` (other
    files).

    :param block: Content block for which :func:`has_unresolved_file_id`
        is true.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param client: HTTP client pointed at the Omnigent server.
    :param meta: Metadata already fetched by the caller, so the call does
        not GET it twice. Fetched here when absent.
    :returns: ``(rebuilt_block, notice)`` — the block without ``file_id``,
        and source dimensions for a sibling framework block, or ``None``.
        Returns ``None`` (not a tuple) when the fetch failed, so callers
        keep the original block and a visible marker can surface downstream.
    """
    file_id = str(block.get("file_id"))
    base = _file_resource_base(session_id, file_id)
    if meta is None:
        meta = await fetch_file_meta(file_id, session_id=session_id, client=client)
        if meta is None:
            return None
    stage = "content"
    try:
        async with asyncio.timeout(_ATTACHMENT_RESOLVE_TIMEOUT_S):
            content_resp = await _read_attachment_resource(
                client, f"{base}/content", session_id=session_id, stage=stage, timeout_s=30.0
            )
    except (httpx.HTTPError, TimeoutError) as exc:
        _logger.warning(
            "Attachment %s read failed",
            stage,
            extra=debug_event(
                "native_attachment_read_failed",
                session_id=session_id,
                stage=stage,
                http_status=exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None,
                exception_type=type(exc).__name__,
                deadline_exceeded=isinstance(exc, TimeoutError),
            ),
        )
        return None
    content_type = meta.get("content_type")
    if not isinstance(content_type, str) or not content_type:
        content_type = content_resp.headers.get("content-type") or "application/octet-stream"
    # Strip any charset suffix: data URIs need the media type hint.
    content_type = content_type.split(";", 1)[0]
    encoded = base64.b64encode(content_resp.content).decode("ascii")
    new_block = {k: v for k, v in block.items() if k != "file_id"}
    stored_name = meta.get("name")
    if isinstance(stored_name, str) and stored_name:
        # The stored name decides delivery. The block's own filename comes from
        # the client and could steer an upload past the upload checks.
        new_block["filename"] = stored_name
    notice: dict[str, int] | None = None
    if block.get("type") == "input_image":
        new_block["image_url"] = f"data:{content_type};base64,{encoded}"
        resource_metadata = meta.get("metadata")
        if isinstance(resource_metadata, Mapping):
            notice = resize_dimensions(resource_metadata.get("source_metadata"))
    else:
        new_block["file_data"] = f"data:{content_type};base64,{encoded}"
    return new_block, notice


async def resolve_file_reference(
    block: Mapping[str, object],
    *,
    session_id: str,
    client: httpx.AsyncClient,
) -> list[dict[str, object]] | None:
    """
    Resolve one unresolved ``file_id`` block to its replacement blocks.

    Fetches the stored row first: a by-path row is streamed to its
    deterministic host path and becomes a single ``[Attached: <path>]`` text
    block; an inline row is inlined exactly as :func:`resolve_file_id_block`
    does, followed by a framework notice block when the image was downscaled.

    :param block: Content block for which :func:`has_unresolved_file_id`
        is true.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param client: HTTP client pointed at the Omnigent server.
    :returns: The replacement blocks, or ``None`` when resolution failed, in
        which case the caller keeps the original block and a visible marker
        can surface downstream.
    """
    file_id = str(block.get("file_id"))
    meta = await fetch_file_meta(file_id, session_id=session_id, client=client)
    if meta is None:
        return None
    resource_metadata = meta.get("metadata")
    source_metadata = (
        resource_metadata.get("source_metadata")
        if isinstance(resource_metadata, Mapping)
        else None
    )
    name = meta.get("name")
    if is_by_path(name if isinstance(name, str) else None, source_metadata):
        path = await materialize_file_reference(
            file_id, meta, session_id=session_id, client=client
        )
        if path is None:
            return None
        return [{"type": "input_text", "text": f"[Attached: {path}]"}]
    result = await resolve_file_id_block(block, session_id=session_id, client=client, meta=meta)
    if result is None:
        return None
    new_block, notice = result
    blocks: list[dict[str, object]] = [new_block]
    if notice is not None:
        blocks.append(framework_notice_block(notice))
    return blocks


_HISTORY_TEXT_BLOCK_TYPES = frozenset({"input_text", "output_text", "text"})


def _map_message_text_attachments(item: dict[str, Any], transform: Callable[[str], str]) -> None:
    """Apply *transform* to every attachment-bearing text inside one message item."""
    content = item.get("content")
    if isinstance(content, str):
        item["content"] = transform(content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in _HISTORY_TEXT_BLOCK_TYPES:
            continue
        text = block.get("text")
        if isinstance(text, str):
            block["text"] = transform(text)


def _rewrite_message_text_attachments(item: dict[str, Any], session_id: str) -> None:
    """Redirect ``[Attached: ...]`` texts inside one message item, in place."""
    _map_message_text_attachments(item, lambda text: rewrite_attached_paths(text, session_id))


def mark_purged_message_attachments(
    item: dict[str, Any], session_id: str, purged: Mapping[str, str]
) -> None:
    """Replace purged ``[Attached: ...]`` texts inside one message item, in place."""
    if purged:
        _map_message_text_attachments(
            item, lambda text: mark_purged_attachments(text, session_id, purged)
        )


async def _resolve_message_item_file_references(
    item: dict[str, Any],
    *,
    session_id: str,
    client: httpx.AsyncClient,
) -> None:
    """Resolve one message's ``file_id`` blocks and redirect its attached paths."""
    if item.get("type") != "message":
        return
    content = item.get("content")
    if isinstance(content, list):
        resolved_content: list[object] = []
        for block in content:
            if not (isinstance(block, dict) and has_unresolved_file_id(block)):
                resolved_content.append(block)
                continue
            replacement = await resolve_file_reference(block, session_id=session_id, client=client)
            if replacement is None:
                resolved_content.append(block)
                continue
            resolved_content.extend(replacement)
        item["content"] = resolved_content
    _rewrite_message_text_attachments(item, session_id)


async def resolve_session_item_file_references(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Resolve ``file_id`` attachment blocks in rebuilt history.

    Message items come back from the server with the upload's raw ``file_id``.
    A cold-resume rebuild runs where no file/artifact stores exist, so bytes are
    fetched back through the session file endpoints, as for a live turn. A
    by-path row becomes an ``[Attached: <path>]`` text line so a rebuilt
    transcript never carries a multi-gigabyte base64 copy; inline rows carry
    ``image_url`` / ``file_data`` data URIs as before. A failed fetch is
    non-fatal: the block stays unresolved and surfaces a visible marker.

    Compaction items are rewritten too: both native transcript builders consume
    their ``compacted_messages`` directly, so an ``[Attached: ...]`` line a
    previous host persisted there must move to this host's session dir.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param items: Flat API item dicts from ``GET /v1/sessions/{id}/items``.
    :returns: The same items with resolvable attachment blocks rewritten.
    """
    for item in items:
        if item.get("type") == "compaction":
            compacted = item.get("compacted_messages")
            if isinstance(compacted, list):
                for message in compacted:
                    if isinstance(message, dict):
                        await _resolve_message_item_file_references(
                            message, session_id=session_id, client=client
                        )
            continue
        await _resolve_message_item_file_references(item, session_id=session_id, client=client)
    return items
