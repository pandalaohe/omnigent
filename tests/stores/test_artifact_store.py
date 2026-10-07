"""Tests for LocalArtifactStore."""

from __future__ import annotations

import tracemalloc
from io import BytesIO

import pytest

from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.artifact_store.local import LocalArtifactStore


@pytest.fixture()
def store(tmp_path):
    return LocalArtifactStore(str(tmp_path / "artifacts"))


# ── put / get round-trip ────────────────────────────────────


def test_put_and_get(store):
    store.put("abc123", b"hello world")
    assert store.get("abc123") == b"hello world"


def test_put_overwrites(store):
    store.put("k", b"first")
    store.put("k", b"second")
    assert store.get("k") == b"second"


def test_put_empty_bytes(store):
    store.put("empty", b"")
    assert store.get("empty") == b""


def test_put_binary_data(store):
    data = bytes(range(256))
    store.put("bin", data)
    assert store.get("bin") == data


# ── nested keys ─────────────────────────────────────────────


def test_nested_key_put_get(store):
    store.put("agents/abc/bundle.tar", b"bundle-data")
    assert store.get("agents/abc/bundle.tar") == b"bundle-data"


def test_nested_key_exists(store):
    store.put("a/b/c", b"deep")
    assert store.exists("a/b/c")
    assert not store.exists("a/b/d")


def test_nested_key_delete(store):
    store.put("x/y", b"data")
    store.delete("x/y")
    assert not store.exists("x/y")


# ── get errors ──────────────────────────────────────────────


def test_get_missing_raises_key_error(store):
    with pytest.raises(KeyError, match=r"no-such-key"):
        store.get("no-such-key")


# ── delete ──────────────────────────────────────────────────


def test_delete_removes_blob(store):
    store.put("to-delete", b"data")
    store.delete("to-delete")
    assert not store.exists("to-delete")


def test_delete_missing_is_noop(store):
    store.delete("nonexistent")


# ── exists ──────────────────────────────────────────────────


def test_exists_true(store):
    store.put("present", b"x")
    assert store.exists("present")


def test_exists_false(store):
    assert not store.exists("absent")


# ── root directory creation ─────────────────────────────────


def test_creates_root_on_init(tmp_path):
    root = tmp_path / "deep" / "nested" / "dir"
    assert not root.exists()
    LocalArtifactStore(str(root))
    assert root.is_dir()


# ── key validation ──────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "..",
        "../etc/passwd",
        "foo\\bar",
        "a/../b",
        "valid/../../escape",
        "/absolute/path",
        "C:/windows/drive",
    ],
)
def test_rejects_invalid_keys(store, bad_key):
    with pytest.raises(ValueError, match=r"invalid artifact key|escapes root"):
        store.put(bad_key, b"x")


def test_all_methods_validate_keys(store):
    """Every public method rejects bad keys (all go through _resolve)."""
    for method, args in [
        (store.get, ("",)),
        (store.delete, ("..",)),
        (store.exists, ("a/../b",)),
    ]:
        with pytest.raises(ValueError):
            method(*args)


# ── symlink traversal ──────────────────────────────────────


def test_rejects_symlink_escape(tmp_path):
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(str(root))

    # Create a symlink inside root that points outside
    escape_target = tmp_path / "secret"
    escape_target.write_bytes(b"sensitive")
    (root / "evil-link").symlink_to(escape_target)

    with pytest.raises(ValueError, match=r"escapes root directory"):
        store.get("evil-link")


# ── put_stream / local_path ─────────────────────────────────


def test_put_stream_round_trip(store):
    payload = b"a" * (1024 * 1024 + 17)
    written = store.put_stream("streamed", BytesIO(payload), max_bytes=None)
    assert written == len(payload)
    assert store.get("streamed") == payload


def test_put_stream_over_limit_raises_and_leaves_nothing(store):
    with pytest.raises(ValueError, match="exceeds the 100-byte limit"):
        store.put_stream("limited", BytesIO(b"x" * 101), max_bytes=100)
    assert not store.exists("limited")
    assert list(store._root.rglob("*.tmp")) == []


def test_put_stream_over_limit_keeps_the_existing_blob(store):
    store.put("keep", b"original")
    with pytest.raises(ValueError):
        store.put_stream("keep", BytesIO(b"x" * 11), max_bytes=10)
    assert store.get("keep") == b"original"


def test_local_path_existing_and_missing(store):
    store.put("nested/file.bin", b"data")
    path = store.local_path("nested/file.bin")
    assert path is not None
    assert path.read_bytes() == b"data"
    assert store.local_path("absent") is None


def test_local_path_validates_keys(store):
    with pytest.raises(ValueError):
        store.local_path("../escape")


def test_put_stream_peak_memory_is_bounded(tmp_path):
    """A 100 MB copy holds at most one chunk in Python memory (scenario 13)."""
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    source_path = tmp_path / "big.bin"
    with source_path.open("wb") as handle:
        handle.truncate(100 * 1024 * 1024)

    tracemalloc.start()
    try:
        with source_path.open("rb") as source:
            written = store.put_stream("big", source, max_bytes=None)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert written == 100 * 1024 * 1024
    assert peak < 8 * 1024 * 1024


def test_base_put_stream_reads_and_delegates(tmp_path):
    """Backends without a streaming write keep working through the fallback."""
    calls: list[tuple[str, bytes]] = []

    class _MemoryStore(ArtifactStore):
        def put(self, key: str, data: bytes) -> None:
            calls.append((key, data))

        def get(self, key: str) -> bytes:
            raise KeyError(key)

        def delete(self, key: str) -> None:
            pass

        def exists(self, key: str) -> bool:
            return False

    store = _MemoryStore("memory")
    assert store.local_path("anything") is None
    assert store.put_stream("k", BytesIO(b"data"), max_bytes=None) == 4
    assert calls == [("k", b"data")]

    with pytest.raises(ValueError):
        store.put_stream("k", BytesIO(b"12345"), max_bytes=4)
    assert calls == [("k", b"data")]
