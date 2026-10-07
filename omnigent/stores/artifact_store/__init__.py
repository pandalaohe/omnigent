"""Artifact store — blob storage for agent bundles and user files."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO


class ArtifactStore(ABC):
    """
    Blob storage for binary artifacts (agent bundles, user-uploaded
    files). Keyed by a unique string identifier. Metadata (filename,
    size, etc.) is managed separately by the route layer.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the store with a backend-specific storage location.

        The interpretation of *storage_location* depends on the
        concrete implementation -- e.g. a filesystem path for local
        storage, an S3 URI for cloud storage, etc.

        :param storage_location: Backend-specific root location,
            e.g. ``"/data/artifacts"`` for local filesystem or
            ``"s3://my-bucket/artifacts"`` for S3.
        """
        self._storage_location = storage_location

    @property
    def storage_location(self) -> str:
        """
        The backend-specific storage location (path, URI, etc.).

        :returns: The storage location string passed at init.
        """
        return self._storage_location

    @abstractmethod
    def put(self, key: str, data: bytes) -> None:
        """
        Store a blob under the given key. Overwrites if the key
        already exists.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :param data: Raw bytes to store.
        """
        ...

    @abstractmethod
    def get(self, key: str) -> bytes:
        """
        Retrieve a blob by key.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: The raw bytes of the stored blob.
        :raises KeyError: If no blob exists for the given key.
        """
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """
        Remove a blob. No-op if the key does not exist.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        """
        ...

    @abstractmethod
    def exists(self, key: str) -> bool:
        """
        Check whether a blob exists for the given key.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: ``True`` if a blob exists, ``False`` otherwise.
        """
        ...

    def put_stream(self, key: str, fileobj: BinaryIO, *, max_bytes: int | None) -> int:
        """
        Store a blob by reading *fileobj*, bounded by *max_bytes*.

        The default implementation reads the whole file object into memory and
        delegates to :meth:`put`; backends with a streaming write path (e.g.
        the local filesystem store) override this to copy in chunks. The read
        is bounded to ``max_bytes + 1`` so an oversized stream is detected
        without buffering all of it.

        :param key: Forward-slash-separated artifact key.
        :param fileobj: Binary file object positioned at the start of the
            content, e.g. the multipart parser's spooled upload.
        :param max_bytes: Maximum allowed size in bytes, or ``None`` for no
            limit.
        :returns: The number of bytes written.
        :raises ValueError: If the stream exceeds *max_bytes*.
        """
        data = fileobj.read() if max_bytes is None else fileobj.read(max_bytes + 1)
        if max_bytes is not None and len(data) > max_bytes:
            raise ValueError(f"artifact {key!r} exceeds the {max_bytes}-byte limit")
        self.put(key, data)
        return len(data)

    def local_path(self, key: str) -> Path | None:  # noqa: ARG002 — default has no local files
        """
        Return the on-disk path backing *key*, when the backend has one.

        The server's content route streams from this path when available
        instead of reading the whole blob into memory. Backends without local
        files (S3, Databricks Volumes) return ``None``.

        :param key: Forward-slash-separated artifact key.
        :returns: The blob's local path, or ``None`` when it has none.
        """
        return None
