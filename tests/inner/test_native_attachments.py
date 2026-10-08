"""Tests for the shared native-executor attachment helpers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import ntpath
import posixpath
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.inner.native_attachments import (
    ATTACHMENT_MARKER_STRIP_PATTERN,
    FRAMEWORK_NOTICE_BLOCK_TYPE,
    UNRESOLVED_ATTACHMENT_MARKER_PATTERN,
    DataUri,
    _is_safe_path_component,
    attachment_cache_dir,
    attachment_reference_line,
    codex_resize_metadata_path,
    has_unresolved_file_id,
    is_by_path,
    is_purged,
    mark_purged_attachments,
    materialize_attachment,
    materialize_file_reference,
    parse_data_uri,
    requires_filesystem,
    resize_notice,
    resolve_file_id_block,
    resolve_file_reference,
    resolve_session_item_file_references,
    restore_session_attachments,
    rewrite_attached_paths,
    session_attachment_dir,
    unresolved_attachment_marker,
)

# A 1x1 transparent PNG, base64-encoded — small but a real decodable image.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
)
_PNG_DATA_URI = f"data:image/png;base64,{_PNG_B64}"


def test_resize_alias_copy_failure_leaves_no_partial_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "photo.png"
    path.write_bytes(base64.b64decode(_PNG_B64))
    dimensions = {"width": 6000, "height": 4000}

    def fail_copy(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes()[:8])
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr("omnigent.inner.native_attachments.shutil.copyfile", fail_copy)
        assert codex_resize_metadata_path(path, dimensions) == path
    assert list(tmp_path.iterdir()) == [path]
    alias = codex_resize_metadata_path(path, dimensions)
    assert alias != path
    assert alias.read_bytes() == path.read_bytes()
    assert codex_resize_metadata_path(path, dimensions) == alias


def test_parse_data_uri_splits_mime_and_payload() -> None:
    """
    parse_data_uri returns the MIME type and base64 payload separately.

    Proves the header is stripped of both the ``data:`` prefix and the
    ``;base64`` suffix so callers get a clean MIME type. A failure here
    means downstream extension/MIME logic would key off a malformed
    string and pick the wrong file extension.
    """
    parsed = parse_data_uri(_PNG_DATA_URI)

    assert parsed == DataUri(mime_type="image/png", base64_payload=_PNG_B64)


def test_parse_data_uri_without_comma_raises() -> None:
    """
    parse_data_uri rejects a URI that has no comma separator.

    A failure (no raise) would mean a malformed URI silently yields an
    empty payload and a later base64 decode produces empty bytes
    instead of surfacing the bad input.
    """
    with pytest.raises(ValueError, match="no comma separator"):
        parse_data_uri("data:image/png;base64")


def test_materialize_attachment_writes_decoded_bytes(tmp_path: Path) -> None:
    """
    An image block is decoded and written into the session cache.

    Proves the bytes written are the decoded PNG (not the base64 text),
    so a Codex ``localImage`` path or a Claude ``[Attached: ...]``
    reference points at a real, openable image. A failure means the
    attachment never reached disk and the model would see nothing.
    """
    block = {"type": "input_image", "image_url": _PNG_DATA_URI}

    path = materialize_attachment(block, tmp_path)

    assert path is not None
    assert path.parent == attachment_cache_dir(tmp_path)
    assert path.read_bytes() == base64.b64decode(_PNG_B64)
    assert path.suffix == ".png"  # MIME-derived extension when no filename given


def test_materialize_attachment_uses_block_filename(tmp_path: Path) -> None:
    """
    A supplied filename is honored (basename only, to avoid traversal).

    Proves a caller-provided ``filename`` is used for the on-disk name
    but stripped to its basename. A failure here would either lose the
    user's filename or, worse, let ``../`` components escape the
    cache directory.
    """
    block = {
        "type": "input_image",
        "image_url": _PNG_DATA_URI,
        "filename": "../../evil.png",
    }

    path = materialize_attachment(block, tmp_path)

    assert path is not None
    assert path.name == "evil.png"
    assert path.parent == attachment_cache_dir(tmp_path)


def test_materialize_attachment_ignores_non_string_filename(tmp_path: Path) -> None:
    block = {
        "type": "input_image",
        "image_url": _PNG_DATA_URI,
        "filename": 42,
    }

    path = materialize_attachment(block, tmp_path)

    assert path is not None
    assert path.name.startswith("attachment_")
    assert path.suffix == ".png"


def test_materialize_attachment_returns_none_without_data_uri(tmp_path: Path) -> None:
    """
    A block whose data URI is missing yields ``None`` and writes nothing.

    Proves an unresolved attachment (e.g. a bare ``file_id`` the content
    resolver never filled in) is skipped rather than crashing. A failure
    would surface as an exception mid-turn or an empty file on disk.
    """
    block = {"type": "input_image", "file_id": "file_unresolved"}

    path = materialize_attachment(block, tmp_path)

    assert path is None
    assert not (attachment_cache_dir(tmp_path)).exists()


def test_materialize_attachment_unresolved_file_id_logs_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """
    An unresolved ``file_id`` block is logged at ERROR, not WARNING.

    The block reaching an executor unresolved means the attachment is
    about to be lost for the whole turn; a warning was too quiet for a
    failure whose user-visible symptom is a hallucinated attachment.
    """
    block = {"type": "input_image", "file_id": "file_unresolved"}

    with caplog.at_level(logging.ERROR, logger="omnigent.inner.native_attachments"):
        path = materialize_attachment(block, tmp_path)

    assert path is None
    records = [
        record
        for record in caplog.records
        if "unresolved file_id file_unresolved" in record.getMessage()
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR


def test_unresolved_attachment_marker_names_the_attachment() -> None:
    """
    The marker names the attachment by filename, falling back to file_id.

    Proves the placeholder callers emit for a failed attachment tells
    the model (and the user, via the mirrored transcript) WHICH file was
    lost, instead of the attachment silently vanishing.
    """
    named = {"type": "input_image", "file_id": "file_x", "filename": "photo.png"}
    unnamed = {"type": "input_image", "file_id": "file_x"}
    bare = {"type": "input_image"}

    assert unresolved_attachment_marker(named) == "[Attachment photo.png could not be loaded]"
    assert unresolved_attachment_marker(unnamed) == "[Attachment file_x could not be loaded]"
    assert unresolved_attachment_marker(bare) == "[Attachment attachment could not be loaded]"


def test_unresolved_attachment_marker_sanitizes_bracketed_names() -> None:
    """
    Brackets and newlines in the filename cannot break the marker shape.

    Consumers (title synthesis, TUI forwarders) match the marker via
    UNRESOLVED_ATTACHMENT_MARKER_PATTERN; an unsanitized ``]`` in the
    name would end their match early and leak marker fragments into
    titles and mirrored chat bubbles.
    """
    bracketed = unresolved_attachment_marker(
        {"type": "input_image", "filename": "shot [final].png"}
    )
    multiline = unresolved_attachment_marker({"type": "input_image", "filename": "a\nb.png"})

    assert bracketed == "[Attachment shot _final_.png could not be loaded]"
    assert re.fullmatch(UNRESOLVED_ATTACHMENT_MARKER_PATTERN, bracketed)
    assert re.fullmatch(UNRESOLVED_ATTACHMENT_MARKER_PATTERN, multiline)


def test_materialize_attachment_reuses_identical_existing_file(tmp_path: Path) -> None:
    """
    Re-materializing identical bytes returns the existing file.

    History replays re-materialize the same blocks on every resume;
    without content-equal dedupe the cache would grow a suffixed
    copy per resume. Different bytes under the same name still get a
    fresh suffixed path.
    """
    block = {"type": "input_image", "image_url": _PNG_DATA_URI, "filename": "photo.png"}
    other_payload = base64.b64encode(b"other-bytes").decode()
    other = {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{other_payload}",
        "filename": "photo.png",
    }

    first = materialize_attachment(block, tmp_path)
    second = materialize_attachment(block, tmp_path)
    third = materialize_attachment(other, tmp_path)

    assert first is not None
    assert second == first
    assert third is not None and third != first
    assert len(list((attachment_cache_dir(tmp_path)).iterdir())) == 2


def test_materialize_attachment_same_name_collision_is_bounded(tmp_path: Path) -> None:
    """
    Same-named attachments with different bytes stay bounded across rebuilds.

    A transcript that carries two distinct ``image.png`` uploads is
    re-materialized on every runner restart. A randomized collision path
    would hand the second attachment a fresh name each rebuild and grow
    the cache without bound; the collision path must be derived from
    the content so each distinct payload keeps exactly one file.
    """
    first_payload = base64.b64encode(b"first-image-bytes").decode()
    second_payload = base64.b64encode(b"second-image-bytes").decode()
    first_block = {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{first_payload}",
        "filename": "image.png",
    }
    second_block = {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{second_payload}",
        "filename": "image.png",
    }

    rebuilds = [
        (
            materialize_attachment(first_block, tmp_path),
            materialize_attachment(second_block, tmp_path),
        )
        for _ in range(4)
    ]

    uploads = attachment_cache_dir(tmp_path)
    assert len(list(uploads.iterdir())) == 2
    # Every rebuild resolves to the same pair of paths.
    assert all(pair == rebuilds[0] for pair in rebuilds)
    assert rebuilds[0][0] != rebuilds[0][1]
    assert rebuilds[0][0].read_bytes() == base64.b64decode(first_payload)
    assert rebuilds[0][1].read_bytes() == base64.b64decode(second_payload)


def test_materialize_attachment_sanitizes_bracketed_filenames(tmp_path: Path) -> None:
    """
    Brackets in the filename cannot break the "[Attached: ...]" line.

    The success-path reference line is matched by the same consumers as
    the unresolved marker; an unsanitized ``]`` in the written path
    would end their ``\\[Attached:[^\\]]*\\]`` match early.
    """
    block = {
        "type": "input_image",
        "image_url": _PNG_DATA_URI,
        "filename": "shot [final].png",
    }

    path = materialize_attachment(block, tmp_path)

    assert path is not None
    assert path.name == "shot _final_.png"


def test_attachment_reference_line_covers_both_outcomes(tmp_path: Path) -> None:
    """
    One call site yields the path line or the visible loss marker.

    Both shapes must match ATTACHMENT_MARKER_STRIP_PATTERN so TUI
    forwarders can strip them from mirrored bubbles.
    """
    resolved = {"type": "input_image", "image_url": _PNG_DATA_URI, "filename": "photo.png"}
    unresolved = {"type": "input_image", "file_id": "file_x", "filename": "photo.png"}

    resolved_line = attachment_reference_line(resolved, tmp_path)
    unresolved_line = attachment_reference_line(unresolved, tmp_path)

    assert resolved_line == f"[Attached: {attachment_cache_dir(tmp_path) / 'photo.png'}]"
    assert unresolved_line == "[Attachment photo.png could not be loaded]"
    assert re.fullmatch(ATTACHMENT_MARKER_STRIP_PATTERN, resolved_line)
    assert re.fullmatch(ATTACHMENT_MARKER_STRIP_PATTERN, unresolved_line)


# Attachment cache writes.


_ZIP_BYTES = b"PK\x03\x04 not really a zip"
_ZIP_DATA_URI = f"data:application/zip;base64,{base64.b64encode(_ZIP_BYTES).decode()}"


def _zip_block(filename: str = "archive.zip") -> dict[str, object]:
    """Build a resolved input_file block for a zip attachment."""
    return {"type": "input_file", "file_data": _ZIP_DATA_URI, "filename": filename}


@pytest.mark.parametrize("existing_mode", [None, 0o600, 0o755])
def test_materialize_cache_creates_or_reuses_non_executable_files(
    tmp_path: Path, existing_mode: int | None
) -> None:
    """Fresh and reused files keep their bytes, path, and non-executable permissions."""
    expected = attachment_cache_dir(tmp_path) / "archive.zip"
    if existing_mode is not None:
        expected.parent.mkdir(parents=True)
        expected.write_bytes(_ZIP_BYTES)
        expected.chmod(existing_mode)

    path = materialize_attachment(_zip_block(), tmp_path)

    assert path == expected
    assert path.read_bytes() == _ZIP_BYTES
    assert path.stat().st_mode & 0o111 == 0
    assert materialize_attachment(_zip_block(), tmp_path) == path
    assert list(path.parent.iterdir()) == [path]


def test_materialize_cache_contains_path_traversal(tmp_path: Path) -> None:
    """Traversal components cannot place an attachment outside its cache."""
    path = materialize_attachment(_zip_block("../../escaped.zip"), tmp_path)

    assert path is not None
    assert path.parent == attachment_cache_dir(tmp_path)
    assert not (tmp_path.parent / "escaped.zip").exists()


@pytest.mark.parametrize("collision", [False, True])
@pytest.mark.parametrize("outside_exists", [False, True])
def test_materialize_cache_refuses_symlinked_destination(
    tmp_path: Path, collision: bool, outside_exists: bool
) -> None:
    """Neither the original nor collision filename may redirect a cache write."""
    attachments_dir = attachment_cache_dir(tmp_path)
    attachments_dir.mkdir(parents=True)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.zip"
    if outside_exists:
        outside.write_bytes(b"precious")
    destination = attachments_dir / "archive.zip"
    if collision:
        destination.write_bytes(b"different content")
        digest = hashlib.sha256(_ZIP_BYTES).hexdigest()[:12]
        destination = attachments_dir / f"archive_{digest}.zip"
    destination.symlink_to(outside)

    assert materialize_attachment(_zip_block(), tmp_path) is None
    if outside_exists:
        assert outside.read_bytes() == b"precious"
    else:
        assert not outside.exists()


def test_materialize_cache_refuses_symlinked_attachments_dir(tmp_path: Path) -> None:
    """A symlinked attachments directory is refused rather than written through."""
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    attachment_cache_dir(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    (attachment_cache_dir(tmp_path)).symlink_to(elsewhere, target_is_directory=True)

    assert materialize_attachment(_zip_block(), tmp_path) is None
    assert list(elsewhere.iterdir()) == []


def test_materialize_cache_does_not_overwrite_when_both_names_taken(
    tmp_path: Path,
) -> None:
    """With the original and collision names holding other content, nothing is overwritten."""
    attachments_dir = attachment_cache_dir(tmp_path)
    attachments_dir.mkdir(parents=True)
    digest = hashlib.sha256(_ZIP_BYTES).hexdigest()[:12]
    (attachments_dir / "archive.zip").write_bytes(b"first")
    (attachments_dir / f"archive_{digest}.zip").write_bytes(b"second")

    assert materialize_attachment(_zip_block(), tmp_path) is None
    assert (attachments_dir / "archive.zip").read_bytes() == b"first"
    assert (attachments_dir / f"archive_{digest}.zip").read_bytes() == b"second"


def test_attachment_cache_isolates_sessions_and_leaves_git_workspace_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uploads from two sessions stay separate and never dirty the checkout."""
    import subprocess

    storage = tmp_path / "omnigent"
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(storage))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    monkeypatch.chdir(workspace)
    first = materialize_attachment(_zip_block(), tmp_path / "claude-native" / "session")
    second = materialize_attachment(_zip_block(), tmp_path / "codex-native" / "session")

    assert first is not None and second is not None
    assert first != second
    assert first.is_relative_to(storage / "attachments")
    assert second.is_relative_to(storage / "attachments")
    assert first.read_bytes() == second.read_bytes() == _ZIP_BYTES
    status = subprocess.check_output(["git", "status", "--porcelain"], text=True)
    assert status == ""


def test_attachment_cache_defaults_to_omnigent_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default storage location is independent of the working directory."""
    monkeypatch.delenv("OMNIGENT_DATA_DIR")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert (
        attachment_cache_dir(tmp_path / "bridge").parent == tmp_path / ".omnigent" / "attachments"
    )


@pytest.mark.parametrize(
    "filename", ["archive.zip", "report.docx", "sheet.xlsx", "deck.pptx", "app.sqlite3"]
)
def test_requires_filesystem_allowed(filename: str) -> None:
    """Archives, office documents, and databases are filesystem."""
    assert requires_filesystem(filename)


@pytest.mark.parametrize("filename", ["photo.png", "report.pdf", "notes.txt", "blob", None])
def test_requires_filesystem_false_for_other_types(filename: str | None) -> None:
    """Other file types keep their existing admission and delivery rules."""
    assert not requires_filesystem(filename)


async def test_relaunch_re_resolution_keeps_a_zip_on_the_filesystem_path() -> None:
    """Restored metadata keeps ZIP files on the filesystem input path."""
    client = _FakeFileClient(
        {"filename": "bundle.zip", "content_type": "application/zip"}, body=_ZIP_BYTES
    )
    block = {"type": "input_file", "file_id": "file_zip", "filename": "bundle.zip"}
    assert has_unresolved_file_id(block)

    result = await resolve_file_id_block(block, session_id="conv_1", client=client)

    assert result is not None
    resolved, _notice = result
    assert resolved["file_data"] == _ZIP_DATA_URI
    assert requires_filesystem(str(resolved["filename"]))


async def test_re_resolution_takes_the_filename_from_stored_metadata() -> None:
    """Stored filenames govern admission and delivery despite a conflicting message name."""
    client = _FakeFileClient({"name": "payload.txt", "content_type": "text/plain"}, body=b"hello")
    block = {"type": "input_file", "file_id": "file_txt", "filename": "payload.db"}

    result = await resolve_file_id_block(block, session_id="conv_1", client=client)

    assert result is not None
    resolved, _notice = result
    assert resolved["filename"] == "payload.txt"
    assert not requires_filesystem(str(resolved["filename"]))


def test_client_server_filesystem_extension_parity() -> None:
    """Client and server accept the same filesystem attachment extensions."""
    from omnigent.inner.native_attachments import _FILESYSTEM_ATTACHMENT_EXTENSIONS

    ts_path = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "attachments.ts"
    if not ts_path.exists():
        pytest.skip("web/src/lib/attachments.ts not present (server-only checkout)")
    block = ts_path.read_text().split("FILESYSTEM_ATTACHMENT_EXTENSIONS = new Set([")[1]
    client_exts = set(re.findall(r'"(\.[a-z0-9]+)"', block.split("]")[0]))

    assert client_exts, "could not parse client FILESYSTEM_ATTACHMENT_EXTENSIONS"
    assert client_exts == set(_FILESYSTEM_ATTACHMENT_EXTENSIONS)


# ── by-path delivery ─────────────────────────────────────────────────


def test_is_by_path_reads_the_stored_delivery_key() -> None:
    """The persisted delivery key decides, whatever the filename looks like."""
    assert is_by_path("clip.mp4", {"delivery": "filesystem"})
    assert is_by_path("photo.png", {"delivery": "filesystem"})
    assert not is_by_path("clip.mp4", {"delivery": "inline"})
    assert not is_by_path("archive.zip", {"delivery": "inline"})
    assert not is_by_path("photo.png", {"width": 6000, "height": 4000})


def test_is_purged_only_for_done_state() -> None:
    """Only ``done`` means the bytes are gone; claimed rows read live."""
    assert is_purged({"purge": {"state": "done", "revision": 1, "at": 2}})
    assert not is_purged({"purge": {"state": "claimed", "revision": 1, "at": 2}})
    assert not is_purged({"delivery": "filesystem"})
    assert not is_purged({"purge": "done"})  # malformed record
    assert not is_purged(None)


def test_is_by_path_falls_back_to_the_extension_for_legacy_rows() -> None:
    """Rows written before the delivery key keep the extension rule."""
    assert is_by_path("archive.zip", None)
    assert is_by_path("report.docx", {})
    assert not is_by_path("clip.mp4", None)
    assert not is_by_path(None, None)


def _file_meta(name: str, *, stored_bytes: int = 5) -> dict[str, Any]:
    """Resource metadata JSON as the file endpoint returns it."""
    return {
        "id": "file_1",
        "name": name,
        "content_type": "video/mp4",
        "metadata": {"bytes": stored_bytes},
    }


# sha256("conv_1")[:32], pinned so path assertions do not recompute the key.
_CONV_1_ATTACHMENTS = "s-214d6c05cb183e28721dc2a7dc43797d"


def _session_dir(tmp_path: Path, file_id: str) -> Path:
    return tmp_path / "data" / "attachments" / _CONV_1_ATTACHMENTS / file_id


def _session_target(tmp_path: Path, file_id: str, name: str) -> Path:
    return _session_dir(tmp_path, file_id) / name


def test_is_safe_path_component_rejects_windows_traversal() -> None:
    """Windows separators, alternate separators, and drives are refused."""
    for unsafe in ("../escape.mp4", "C:/outside/escape.mp4", "D:escape.mp4", "a\\b.mp4"):
        assert not _is_safe_path_component(unsafe, ntpath), unsafe
    assert _is_safe_path_component("clip.mp4", ntpath)


def test_is_safe_path_component_keeps_posix_backslash_legal() -> None:
    """POSIX has no altsep or drives, so a backslash stays a legal name."""
    assert _is_safe_path_component("a\\b.mp4", posixpath)
    assert not _is_safe_path_component("../x", posixpath)


@pytest.mark.asyncio
async def test_materialize_file_reference_streams_to_the_session_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content is streamed straight to the deterministic host path."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"hello")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        path = await materialize_file_reference(
            "file_1", _file_meta("clip.mp4"), session_id="conv_1", client=client
        )

    assert path is not None
    assert path == _session_target(tmp_path, "file_1", "clip.mp4")
    assert path.read_bytes() == b"hello"
    assert [request.url.path for request in requests] == [
        "/v1/sessions/conv_1/resources/files/file_1/content"
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [("clip[1].mp4", "clip_1_.mp4"), ("clip\n1.mp4", "clip_1.mp4")],
)
@pytest.mark.asyncio
async def test_materialize_file_reference_sanitizes_marker_unsafe_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, expected: str
) -> None:
    """Brackets and newlines in the stored name cannot break the marker line."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"hello")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        path = await materialize_file_reference(
            "file_1", _file_meta(name), session_id="conv_1", client=client
        )

    assert path is not None
    assert path.name == expected
    line = f"[Attached: {path}]"
    assert "\n" not in line
    assert "[" not in str(path) and "]" not in str(path)


@pytest.mark.asyncio
async def test_materialize_file_reference_reuses_a_complete_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose size matches the stored row is reused without a fetch."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    target_dir = _session_dir(tmp_path, "file_1")
    target_dir.mkdir(parents=True)
    (target_dir / "clip.mp4").write_bytes(b"hello")
    content_gets = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal content_gets
        content_gets += 1
        return httpx.Response(200, content=b"hello")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        path = await materialize_file_reference(
            "file_1", _file_meta("clip.mp4"), session_id="conv_1", client=client
        )

    assert path == target_dir / "clip.mp4"
    assert content_gets == 0


@pytest.mark.asyncio
async def test_materialize_file_reference_discards_a_size_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream shorter than the stored size leaves nothing behind."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"hello")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        path = await materialize_file_reference(
            "file_1", _file_meta("clip.mp4", stored_bytes=9), session_id="conv_1", client=client
        )

    assert path is None
    assert list(_session_dir(tmp_path, "file_1").iterdir()) == []


@pytest.mark.asyncio
async def test_materialize_file_reference_returns_none_on_fetch_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 404 leaves the caller's block unresolved and writes nothing."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        path = await materialize_file_reference(
            "file_1", _file_meta("clip.mp4"), session_id="conv_1", client=client
        )

    assert path is None
    assert list(_session_dir(tmp_path, "file_1").iterdir()) == []


class _CancellingStreamResponse:
    """Content response that yields one chunk and then cancels."""

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield b"partial"
        raise asyncio.CancelledError


class _CancellingStream:
    """Async context manager yielding the cancelling response."""

    async def __aenter__(self) -> _CancellingStreamResponse:
        return _CancellingStreamResponse()

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


class _CancellingStreamClient:
    """Fake async client whose content stream cancels mid-body."""

    def stream(self, *args: Any, **kwargs: Any) -> _CancellingStream:
        del args, kwargs
        return _CancellingStream()


@pytest.mark.asyncio
async def test_materialize_file_reference_cancellation_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling mid-stream leaves no temp file, final file or open fd."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))

    with pytest.raises(asyncio.CancelledError):
        await materialize_file_reference(
            "file_1",
            _file_meta("clip.mp4"),
            session_id="conv_1",
            client=_CancellingStreamClient(),  # type: ignore[arg-type]
        )

    assert list(_session_dir(tmp_path, "file_1").iterdir()) == []


@pytest.mark.asyncio
async def test_materialize_file_reference_refuses_a_symlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink at the target name is never written through."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    target_dir = _session_dir(tmp_path, "file_1")
    target_dir.mkdir(parents=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"precious")
    (target_dir / "clip.mp4").symlink_to(outside)
    fetches = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal fetches
        fetches += 1
        return httpx.Response(200, content=b"hello")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        path = await materialize_file_reference(
            "file_1", _file_meta("clip.mp4"), session_id="conv_1", client=client
        )

    assert path is None
    assert fetches == 0
    assert outside.read_bytes() == b"precious"
    assert [entry.name for entry in target_dir.iterdir()] == ["clip.mp4"]


@pytest.mark.asyncio
async def test_materialize_file_reference_path_fallback_writes_the_same_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forcing the no-dir_fd branch lands the same bytes at the same path."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("omnigent.inner.native_attachments._dir_fd_supported", lambda: False)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"hello")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        path = await materialize_file_reference(
            "file_1", _file_meta("clip.mp4"), session_id="conv_1", client=client
        )

    assert path is not None
    assert path == _session_target(tmp_path, "file_1", "clip.mp4")
    assert path.read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_materialize_file_reference_path_fallback_refuses_symlinked_file_id_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked file_id dir is refused on the no-dir_fd path too."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("omnigent.inner.native_attachments._dir_fd_supported", lambda: False)
    session_dir = tmp_path / "data" / "attachments" / _CONV_1_ATTACHMENTS
    session_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (session_dir / "file_1").symlink_to(outside, target_is_directory=True)

    def respond(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch through a symlinked file_id dir")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        path = await materialize_file_reference(
            "file_1", _file_meta("clip.mp4"), session_id="conv_1", client=client
        )

    assert path is None
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_materialize_file_reference_path_fallback_preserves_preexisting_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed exclusive create must not unlink a pre-existing temp file."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("omnigent.inner.native_attachments._dir_fd_supported", lambda: False)
    monkeypatch.setattr("omnigent.inner.native_attachments.uuid.uuid4", lambda: uuid.UUID(int=0))
    target_dir = _session_dir(tmp_path, "file_1")
    target_dir.mkdir(parents=True)
    temp_path = target_dir / f"clip.mp4.{uuid.UUID(int=0).hex}.tmp"
    temp_path.write_bytes(b"precious")

    def respond(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch when the temp path already exists")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        path = await materialize_file_reference(
            "file_1", _file_meta("clip.mp4"), session_id="conv_1", client=client
        )

    assert path is None
    assert temp_path.read_bytes() == b"precious"


@pytest.mark.parametrize("name", ["", ".", "..", "nested/clip.mp4", "clip\x00.mp4"])
@pytest.mark.asyncio
async def test_materialize_file_reference_refuses_unsafe_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Names that are empty or escape the per-file directory are refused."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))

    def respond(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch for an unsafe name")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        path = await materialize_file_reference(
            "file_1", _file_meta(name), session_id="conv_1", client=client
        )

    assert path is None


# ── one per-block resolver ───────────────────────────────────────────


def _by_path_meta(name: str, *, stored_bytes: int = 5) -> dict[str, Any]:
    """A stored by-path row as the file metadata endpoint returns it."""
    return {
        "id": "file_1",
        "name": name,
        "content_type": "video/mp4",
        "metadata": {
            "bytes": stored_bytes,
            "source_metadata": {"delivery": "filesystem"},
        },
    }


@pytest.mark.asyncio
async def test_resolve_file_reference_by_path_reuses_without_content_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A by-path row with a complete host copy becomes one [Attached:] block."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    target_dir = _session_dir(tmp_path, "file_1")
    target_dir.mkdir(parents=True)
    target = target_dir / "clip.mp4"
    target.write_bytes(b"hello")
    content_gets = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal content_gets
        if request.url.path.endswith("/content"):
            content_gets += 1
            return httpx.Response(200, content=b"hello")
        return httpx.Response(200, json=_by_path_meta("clip.mp4"))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        blocks = await resolve_file_reference(
            {"type": "input_file", "file_id": "file_1", "filename": "clip.mp4"},
            session_id="conv_1",
            client=client,
        )

    assert blocks == [{"type": "input_text", "text": f"[Attached: {target}]"}]
    assert content_gets == 0


@pytest.mark.asyncio
async def test_resolve_file_reference_inline_matches_resolve_file_id_block() -> None:
    """An inline row resolves exactly as resolve_file_id_block, plus its notice."""
    payload = {
        "id": "file_img",
        "filename": "shot.webp",
        "content_type": "image/webp",
        "metadata": {"source_metadata": {"width": 6000, "height": 4000}},
    }
    block = {"type": "input_image", "file_id": "file_img"}
    client = _FakeFileClient(payload, body=b"webp-bytes")

    replacement = await resolve_file_reference(
        block,
        session_id="conv_1",
        client=client,  # type: ignore[arg-type]
    )
    expected = await resolve_file_id_block(
        block,
        session_id="conv_1",
        client=client,  # type: ignore[arg-type]
    )

    assert expected is not None
    new_block, notice = expected
    assert replacement == [
        new_block,
        {"type": FRAMEWORK_NOTICE_BLOCK_TYPE, "source_metadata": notice},
    ]


@pytest.mark.asyncio
async def test_resolve_file_reference_returns_none_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed metadata or by-path content fetch keeps the original block."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    block = {"type": "input_file", "file_id": "file_1"}

    def metadata_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(metadata_404), base_url="http://test"
    ) as client:
        assert await resolve_file_reference(block, session_id="conv_1", client=client) is None

    def content_404(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content"):
            return httpx.Response(404)
        return httpx.Response(200, json=_by_path_meta("clip.mp4"))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(content_404), base_url="http://test"
    ) as client:
        assert await resolve_file_reference(block, session_id="conv_1", client=client) is None


@pytest.mark.asyncio
async def test_resolve_session_item_file_references_by_path_emits_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A by-path .mp4 row rebuilds as a path line, never base64 file_data."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"hello")
        return httpx.Response(200, json=_by_path_meta("clip.mp4"))

    items: list[dict[str, Any]] = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_file", "file_id": "file_1"}],
        }
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        resolved = await resolve_session_item_file_references(
            client, session_id="conv_1", items=items
        )

    block = resolved[0]["content"][0]
    assert block == {
        "type": "input_text",
        "text": f"[Attached: {_session_target(tmp_path, 'file_1', 'clip.mp4')}]",
    }
    assert "file_data" not in block


@pytest.mark.asyncio
async def test_resolve_session_item_file_references_rewrites_string_message_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compacted message with plain-string content gets its path rewritten.

    Claude's compaction forwarder stores message content as a plain string;
    transcript synthesis consumes it verbatim, so an other-host
    ``[Attached: ...]`` path must still move to this host's session dir.
    """
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    items: list[dict[str, Any]] = [
        {
            "type": "compaction",
            "compacted_messages": [
                {
                    "type": "message",
                    "role": "user",
                    "content": (
                        "[Attached: /home/other/.omnigent/attachments/"
                        f"s-{'a' * 32}/file_mp4/a.mp4] inspect"
                    ),
                }
            ],
        }
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        resolved = await resolve_session_item_file_references(
            client, session_id="conv_1", items=items
        )

    expected = session_attachment_dir("conv_1") / "file_mp4" / "a.mp4"
    compacted = resolved[0]["compacted_messages"][0]
    assert compacted["content"] == f"[Attached: {expected}] inspect"


# ── restore_session_attachments ──────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_session_attachments_pages_and_skips_present_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """By-path rows are restored across pages; inline rows are never fetched."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    present_dir = _session_dir(tmp_path, "file_present")
    present_dir.mkdir(parents=True)
    (present_dir / "clip.mp4").write_bytes(b"hello")
    content_requests: list[str] = []
    after_params: list[str | None] = []

    present = {
        "id": "file_present",
        "name": "clip.mp4",
        "metadata": {"bytes": 5, "source_metadata": {"delivery": "filesystem"}},
    }
    missing = {
        "id": "file_missing",
        "name": "clip.mp4",
        "metadata": {"bytes": 5, "source_metadata": {"delivery": "filesystem"}},
    }
    inline = {
        "id": "file_inline",
        "name": "shot.png",
        "metadata": {"bytes": 4, "source_metadata": {"delivery": "inline"}},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/resources/files"):
            after_params.append(request.url.params.get("after"))
            if request.url.params.get("after") is None:
                return httpx.Response(
                    200,
                    json={"data": [present, missing], "has_more": True, "last_id": "file_missing"},
                )
            return httpx.Response(200, json={"data": [inline], "has_more": False})
        if path.endswith("/content"):
            content_requests.append(path)
            return httpx.Response(200, content=b"hello")
        raise AssertionError(f"unexpected request {path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        restored = await restore_session_attachments("conv_1", client)

    assert restored is True
    assert after_params == [None, "file_missing"]
    assert content_requests == ["/v1/sessions/conv_1/resources/files/file_missing/content"]
    assert (present_dir / "clip.mp4").read_bytes() == b"hello"
    assert (_session_dir(tmp_path, "file_missing") / "clip.mp4").read_bytes() == b"hello"
    assert not _session_dir(tmp_path, "file_inline").exists()


@pytest.mark.asyncio
async def test_restore_session_attachments_skips_purged_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``done`` row is never fetched and does not fail the restore."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    content_requests: list[str] = []
    purged_rows: dict[str, str] = {}
    purged = {
        "id": "file_purged",
        "name": "clip.mp4",
        "metadata": {
            "bytes": 5,
            "source_metadata": {
                "delivery": "filesystem",
                "purge": {"state": "done", "revision": 1, "at": 1700000000},
            },
        },
    }
    live = {
        "id": "file_live",
        "name": "clip.mp4",
        "metadata": {"bytes": 5, "source_metadata": {"delivery": "filesystem"}},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/resources/files"):
            return httpx.Response(200, json={"data": [purged, live], "has_more": False})
        if path.endswith("/content"):
            content_requests.append(path)
            return httpx.Response(200, content=b"hello")
        raise AssertionError(f"unexpected request {path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        restored = await restore_session_attachments("conv_1", client, purged=purged_rows)

    assert restored is True
    assert purged_rows == {"file_purged": "clip.mp4"}
    assert content_requests == ["/v1/sessions/conv_1/resources/files/file_live/content"]
    assert not _session_dir(tmp_path, "file_purged").exists()
    assert (_session_dir(tmp_path, "file_live") / "clip.mp4").read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_restore_session_attachments_logs_a_fetch_failure_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A 404 on one by-path row is logged; the restore call still returns."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    row = {
        "id": "file_missing",
        "name": "clip.mp4",
        "metadata": {"bytes": 5, "source_metadata": {"delivery": "filesystem"}},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/resources/files"):
            return httpx.Response(200, json={"data": [row], "has_more": False})
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        with caplog.at_level(logging.WARNING, logger="omnigent.inner.native_attachments"):
            restored = await restore_session_attachments("conv_1", client)

    assert restored is False
    assert not (_session_dir(tmp_path, "file_missing") / "clip.mp4").exists()
    assert any(
        "failed to materialize file_id=file_missing" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_restore_session_attachments_reports_a_listing_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 500 on the file listing reports False so callers can retry later."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        assert await restore_session_attachments("conv_1", client) is False


# ── rewrite_attached_paths ───────────────────────────────────────────


def test_rewrite_attached_paths_redirects_both_separator_styles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisted POSIX and Windows layout paths move to this session's dir."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    key = "a" * 32
    posix = f"[Attached: /opt/work/omnigent/fork/wt/data/attachments/s-{key}/file_1/a.mp4]"
    windows = "[Attached: C:\\Users\\other\\.omnigent\\attachments\\s-" + key + "\\file_2\\b.mp4]"
    local = session_attachment_dir("session-a")

    assert (
        rewrite_attached_paths(posix, "session-a") == f"[Attached: {local / 'file_1' / 'a.mp4'}]"
    )
    assert (
        rewrite_attached_paths(windows, "session-a") == f"[Attached: {local / 'file_2' / 'b.mp4'}]"
    )
    assert (
        rewrite_attached_paths(f"see {posix} now", "session-a")
        == f"see [Attached: {local / 'file_1' / 'a.mp4'}] now"
    )


def test_rewrite_attached_paths_leaves_other_text_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text without the layout — or without a marker — stays as written."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    unchanged = [
        "[Attached: /tmp/somewhere/a.mp4]",
        "[Attached: /x/attachments/not-a-session-key/file_1/a.mp4]",
        "[Attachment a.mp4 could not be loaded]",
        "plain text without a marker",
    ]

    for text in unchanged:
        assert rewrite_attached_paths(text, "session-a") == text


# ── mark_purged_attachments ──────────────────────────────────────────


def test_mark_purged_attachments_marks_only_the_purged_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A purged row's line becomes the marker; a live row's line is untouched."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    key = "b" * 32
    purged_line = f"[Attached: /home/other/.omnigent/attachments/s-{key}/file_gone/trip.mp4]"
    live_line = f"[Attached: /home/other/.omnigent/attachments/s-{key}/file_live/clip.mp4]"

    marked = mark_purged_attachments(
        f"{purged_line} then {live_line}", "session-a", {"file_gone": "trip.mp4"}
    )

    assert marked == f"[Attachment trip.mp4 could not be loaded] then {live_line}"


def test_mark_purged_attachments_keeps_a_surviving_host_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A purged row whose host copy still exists keeps the working path."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    session_id = "session-a"
    target = session_attachment_dir(session_id) / "file_gone" / "trip.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"data")
    line = f"[Attached: {target}]"

    assert mark_purged_attachments(line, session_id, {"file_gone": "trip.mp4"}) == line


# ── materialize_attachment without dir_fd (Windows) ──────────────────


def test_materialize_attachment_path_fallback_matches_the_posix_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forcing the no-dir_fd branch lands the same bytes at the same name."""
    posix_path = materialize_attachment(_zip_block(), tmp_path / "posix-bridge")

    with monkeypatch.context() as patch:
        patch.setattr("omnigent.inner.native_attachments._dir_fd_supported", lambda: False)
        fallback_path = materialize_attachment(_zip_block(), tmp_path / "fallback-bridge")

    assert posix_path is not None
    assert fallback_path is not None
    assert fallback_path.parent == attachment_cache_dir(tmp_path / "fallback-bridge")
    assert fallback_path.name == posix_path.name == "archive.zip"
    assert fallback_path.read_bytes() == posix_path.read_bytes() == _ZIP_BYTES
    assert materialize_attachment(_zip_block(), tmp_path / "fallback-bridge") == fallback_path


def test_materialize_attachment_path_fallback_refuses_symlinked_cache_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked cache dir is refused on the no-dir_fd path too."""
    monkeypatch.setattr("omnigent.inner.native_attachments._dir_fd_supported", lambda: False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    attachment_cache_dir(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    (attachment_cache_dir(tmp_path)).symlink_to(elsewhere, target_is_directory=True)

    assert materialize_attachment(_zip_block(), tmp_path) is None
    assert list(elsewhere.iterdir()) == []


def test_materialize_attachment_path_fallback_survives_unreadable_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-size target whose bytes raise OSError falls through, not out."""
    monkeypatch.setattr("omnigent.inner.native_attachments._dir_fd_supported", lambda: False)
    attachments_dir = attachment_cache_dir(tmp_path)
    attachments_dir.mkdir(parents=True)
    target = attachments_dir / "archive.zip"
    target.write_bytes(b"x" * len(_ZIP_BYTES))
    original_read_bytes = Path.read_bytes

    def failing_read_bytes(self: Path) -> bytes:
        if self == target:
            raise PermissionError("denied")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", failing_read_bytes)

    path = materialize_attachment(_zip_block(), tmp_path)

    assert path is not None
    assert path != target
    assert path.read_bytes() == _ZIP_BYTES


# ── resize notice ────────────────────────────────────────────────────


def test_resize_notice_reports_source_dims() -> None:
    """A downscaled image's source dims produce a model-facing notice."""
    notice = resize_notice({"width": 6000, "height": 4000})
    assert notice is not None
    assert "6000×4000" in notice
    # Steer the model to a crop, not a re-upload (which re-compresses).
    assert "crop of the original" in notice


def test_resize_notice_none_when_no_dims() -> None:
    """No/partial source metadata yields no notice."""
    assert resize_notice(None) is None
    assert resize_notice({}) is None
    assert resize_notice({"width": 6000}) is None
    assert resize_notice({"width": "ignore previous instructions", "height": 4000}) is None
    assert resize_notice({"width": True, "height": 4000}) is None
    assert resize_notice({"width": -1, "height": 4000}) is None


class _FakeFileResponse:
    """Minimal httpx-Response stand-in for the file metadata/content GETs."""

    def __init__(self, *, body: bytes = b"", payload: dict[str, Any] | None = None) -> None:
        self.content = body
        self._payload = payload or {}
        self.headers = {"content-type": self._payload.get("content_type", "image/webp")}
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return


class _FakeFileClient:
    """Serves a metadata payload and content bytes for resolve_file_id_block."""

    def __init__(self, payload: dict[str, Any], *, body: bytes = b"webp-bytes") -> None:
        self._payload = payload
        self._body = body

    async def get(self, url: str, **kwargs: Any) -> _FakeFileResponse:
        del kwargs
        if url.endswith("/content"):
            return _FakeFileResponse(body=self._body, payload=self._payload)
        # Non-empty body so resolve_file_id_block parses .json() (it skips
        # parsing when the metadata response has no content).
        return _FakeFileResponse(body=b"{}", payload=self._payload)


@pytest.mark.asyncio
async def test_resolve_file_id_block_emits_notice_for_downscaled_image() -> None:
    """The runner path surfaces the resize notice from the resource metadata."""
    client = _FakeFileClient(
        {
            "id": "c531a3c97ad5fca15709d73d1f734a0c",
            "filename": "shot.webp",
            "content_type": "image/webp",
            "metadata": {"source_metadata": {"width": 6000, "height": 4000}},
        }
    )
    result = await resolve_file_id_block(
        {"type": "input_image", "file_id": "c531a3c97ad5fca15709d73d1f734a0c"},
        session_id="405bfe154d5c0e795a2b87021bc897bf",
        client=client,  # type: ignore[arg-type]
    )
    assert result is not None
    new_block, notice = result
    assert new_block["image_url"].startswith("data:image/webp;base64,")
    assert "file_id" not in new_block
    assert notice == {"width": 6000, "height": 4000}


@pytest.mark.asyncio
async def test_resolve_file_id_block_no_notice_without_source_metadata() -> None:
    """A non-downscaled image resolves with no notice."""
    client = _FakeFileClient(
        {
            "id": "c531a3c97ad5fca15709d73d1f734a0c",
            "filename": "a.webp",
            "content_type": "image/webp",
        }
    )
    result = await resolve_file_id_block(
        {"type": "input_image", "file_id": "c531a3c97ad5fca15709d73d1f734a0c"},
        session_id="405bfe154d5c0e795a2b87021bc897bf",
        client=client,  # type: ignore[arg-type]
    )
    assert result is not None
    _, notice = result
    assert notice is None
