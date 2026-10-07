"""Local filesystem implementation of ArtifactStore."""

from __future__ import annotations

import os
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

from omnigent.stores.artifact_store import ArtifactStore

# Copy chunk for put_stream. Bounds peak memory to one chunk regardless of the
# blob size.
_STREAM_CHUNK_BYTES = 1024 * 1024


class LocalArtifactStore(ArtifactStore):
    """
    Stores binary blobs as flat files under a local directory.

    The ``storage_location`` is a filesystem path used as the root
    directory.  Layout::

        storage_location/
            <key1>
            nested/key2
            ...

    Keys use forward slashes as separators and are mapped to the
    native OS path on disk.  Traversal sequences (``..``) and
    backslashes are rejected; a post-resolution containment check
    ensures the resolved path stays within the root even if symlinks
    are involved.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the local artifact store.

        Creates the root directory if it does not exist.

        :param storage_location: Filesystem path to the root
            directory, e.g. ``"/data/artifacts"``.
        """
        super().__init__(storage_location)
        self._root = Path(storage_location)
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        """
        Map *key* (forward-slash separated) to an absolute
        filesystem path.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: The resolved absolute :class:`Path`.
        :raises ValueError: If the key is empty, contains
            traversal sequences (``..``), backslashes, or
            resolves outside the root directory.
        """
        parts = PurePosixPath(key).parts
        if (
            not parts
            or ".." in parts
            or "\\" in key
            or PurePosixPath(key).is_absolute()
            or PureWindowsPath(key).is_absolute()
        ):
            raise ValueError(f"invalid artifact key: {key!r}")

        # Join validated parts with OS-native separator
        resolved = (self._root / Path(*parts)).resolve()
        if not resolved.is_relative_to(self._root.resolve()):
            raise ValueError(f"artifact key escapes root directory: {key!r}")
        return resolved

    # ── ArtifactStore interface ──────────────────────────────

    def put(self, key: str, data: bytes) -> None:
        """
        Write bytes to a file under the root directory.

        Creates intermediate directories as needed. Overwrites the file
        if it already exists. Writes to a sibling temp file first and
        ``os.replace``s it into place: a concurrent :meth:`get` of the
        same key (e.g. a re-``put`` of a builtin/managed agent racing an
        in-flight cache load of the same bundle) always observes either
        the old complete content or the new complete content, never a
        truncated read from a plain overwrite-in-place.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :param data: Raw bytes to write.
        """
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_bytes(data)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def put_stream(self, key: str, fileobj: BinaryIO, *, max_bytes: int | None) -> int:
        """
        Copy *fileobj* into place in 1 MiB chunks.

        Writes to a sibling temp file and ``os.replace``s it in, matching
        :meth:`put`'s atomicity: a reader never observes a partial blob.
        Peak memory is one chunk. Over *max_bytes*, the temp file is removed
        and ``ValueError`` is raised without touching an existing blob.

        :param key: Forward-slash-separated artifact key.
        :param fileobj: Binary file object positioned at the start.
        :param max_bytes: Maximum allowed size in bytes, or ``None``.
        :returns: The number of bytes written.
        :raises ValueError: If the stream exceeds *max_bytes*.
        """
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        written = 0
        try:
            with open(tmp_path, "wb") as handle:
                while True:
                    chunk = fileobj.read(_STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise ValueError(f"artifact {key!r} exceeds the {max_bytes}-byte limit")
                    handle.write(chunk)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return written

    def local_path(self, key: str) -> Path | None:
        """
        Return the on-disk file for *key*, or ``None`` when it does not exist.

        :param key: Forward-slash-separated artifact key.
        :returns: The resolved path when a regular file exists there.
        """
        path = self._resolve(key)
        return path if path.is_file() else None

    def get(self, key: str) -> bytes:
        """
        Read bytes from a file under the root directory.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: The raw bytes of the file.
        :raises KeyError: If no file exists at the resolved path.
        """
        path = self._resolve(key)
        if not path.exists():
            raise KeyError(key)
        return path.read_bytes()

    def delete(self, key: str) -> None:
        """
        Remove a file under the root directory. No-op if the file
        does not exist.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        """
        path = self._resolve(key)
        if path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        """
        Check whether a file exists under the root directory.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: ``True`` if the file exists, ``False`` otherwise.
        """
        return self._resolve(key).exists()
