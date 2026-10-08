"""Archive cleanup job: scenarios 15-21, summary file, and /system/brief."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.db_models import workspace_scope
from omnigent.errors import OmnigentError
from omnigent.server.attachment_archive_cleanup import AttachmentArchiveCleanup
from omnigent.server.routes.system_status import create_system_status_router
from omnigent.server.system_status import SystemStatusHub
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

_DAY = 24 * 60 * 60
_MIN_BYTES = 10 * 1024 * 1024
_VIDEO_BYTES = 50 * 1024 * 1024
_SUMMARY_NAME = "attachment-archive-cleanup.json"

_DAYS_30 = {"attachment_archive_cleanup_days": 30}


@pytest.fixture()
def env(db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(
        "omnigent.server.attachment_archive_cleanup.resolve_data_dir",
        lambda: tmp_path,
    )
    conversations = SqlAlchemyConversationStore(db_uri)
    files = SqlAlchemyFileStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    return SimpleNamespace(
        conversations=conversations,
        files=files,
        artifacts=artifacts,
        tmp_path=tmp_path,
        db_uri=db_uri,
    )


def _configure(monkeypatch: pytest.MonkeyPatch, values: dict[str, object]) -> None:
    monkeypatch.setattr(
        "omnigent.server.server_config.load_server_config",
        lambda: values,
    )


def _job(env: SimpleNamespace, *, file_store: Any | None = None) -> AttachmentArchiveCleanup:
    return AttachmentArchiveCleanup(
        conversation_store=env.conversations,
        file_store=file_store if file_store is not None else env.files,
        artifact_store=env.artifacts,
    )


def _archive(env: SimpleNamespace, session_id: str) -> int:
    """Archive a session and return its new archive revision."""
    assert env.conversations.update_conversation(session_id, archived=True) is not None
    conversation = env.conversations.get_conversation(session_id)
    assert conversation is not None and conversation.archived_at is not None
    return conversation.archive_revision


def _new_session(env: SimpleNamespace, title: str = "old session") -> str:
    return env.conversations.create_conversation(title=title).id


def _by_path_row(
    env: SimpleNamespace,
    session_id: str,
    *,
    filename: str = "clip.mp4",
    size: int = _VIDEO_BYTES,
    blob_key: str | None = None,
) -> Any:
    stored = env.files.create(
        session_id=session_id,
        filename=filename,
        bytes=size,
        content_type="video/mp4",
        blob_key=blob_key,
        source_metadata={"delivery": "filesystem"},
    )
    env.artifacts.put(stored.blob_key or stored.id, b"payload")
    return stored


def _fresh(env: SimpleNamespace, file_id: str) -> Any:
    row = env.files.get(file_id)
    assert row is not None
    return row


def _purge(row: Any) -> Any:
    return (row.source_metadata or {}).get("purge")


# ── scenario 15: off by default ─────────────────────────────────────


async def test_scenario_15_cleanup_off_by_default(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without settings nothing is claimed or deleted; summary mode=off."""
    _configure(monkeypatch, {})
    session_id = _new_session(env)
    stored = _by_path_row(env, session_id, size=1024**3)
    _archive(env, session_id)

    summary = await _job(env).sweep_once(now=int(time.time()) + 400 * _DAY)

    assert summary.mode == "off"
    assert summary.files_purged == 0
    assert _purge(_fresh(env, stored.id)) is None
    assert env.artifacts.exists(stored.id)
    written = json.loads((env.tmp_path / "system-status" / _SUMMARY_NAME).read_text())
    assert written["mode"] == "off"


# ── scenario 16: purge large video only ─────────────────────────────


async def test_scenario_16_purges_only_large_by_path_files(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 50 MB video goes; the small zip and the PNG stay."""
    _configure(monkeypatch, _DAYS_30)
    session_id = _new_session(env)
    video = _by_path_row(env, session_id, filename="clip.mp4", size=_VIDEO_BYTES)
    small_zip = _by_path_row(env, session_id, filename="notes.zip", size=2 * 1024 * 1024)
    png = env.files.create(
        session_id=session_id,
        filename="pic.png",
        bytes=5 * 1024 * 1024,
        content_type="image/png",
        source_metadata={"width": 4000, "height": 3000},
    )
    env.artifacts.put(png.blob_key or png.id, b"png-bytes")
    revision = _archive(env, session_id)

    archived_at = env.conversations.get_conversation(session_id).archived_at
    assert archived_at is not None
    summary = await _job(env).sweep_once(now=archived_at + 31 * _DAY)

    assert summary.mode == "on"
    assert summary.sessions_scanned == 1
    assert summary.files_purged == 1
    assert summary.bytes_freed == _VIDEO_BYTES
    video_row = _fresh(env, video.id)
    assert _purge(video_row) == {
        "state": "done",
        "revision": revision,
        "at": archived_at + 31 * _DAY,
    }
    assert not env.artifacts.exists(video.blob_key or video.id)
    assert _purge(_fresh(env, small_zip.id)) is None
    assert env.artifacts.exists(small_zip.blob_key or small_zip.id)
    assert _purge(_fresh(env, png.id)) is None
    assert env.artifacts.exists(png.blob_key or png.id)
    written = json.loads((env.tmp_path / "system-status" / _SUMMARY_NAME).read_text())
    assert written["files_purged"] == 1
    assert written["bytes_freed"] == _VIDEO_BYTES


async def test_large_inline_files_are_never_purged(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large PNG, PDF and .txt are inline rows, so the cleanup keeps them all."""
    _configure(monkeypatch, _DAYS_30)
    session_id = _new_session(env)
    inline = [
        env.files.create(
            session_id=session_id,
            filename="huge.png",
            bytes=_MIN_BYTES,
            content_type="image/png",
            source_metadata={"width": 4000, "height": 3000},
        ),
        env.files.create(
            session_id=session_id,
            filename="huge.pdf",
            bytes=_MIN_BYTES,
            content_type="application/pdf",
        ),
        env.files.create(
            session_id=session_id,
            filename="huge.txt",
            bytes=_MIN_BYTES,
            content_type="text/plain",
        ),
    ]
    for row in inline:
        env.artifacts.put(row.blob_key or row.id, b"inline-bytes")
    _archive(env, session_id)
    archived_at = env.conversations.get_conversation(session_id).archived_at
    assert archived_at is not None

    summary = await _job(env).sweep_once(now=archived_at + 31 * _DAY)

    assert summary.files_purged == 0
    for row in inline:
        assert _purge(_fresh(env, row.id)) is None
        assert env.artifacts.exists(row.blob_key or row.id)


async def test_large_zip_without_delivery_marker_is_never_purged(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only attachment originals: agent-produced and legacy .zip rows are kept."""
    _configure(monkeypatch, _DAYS_30)
    session_id = _new_session(env)
    agent_zip = env.files.create(
        session_id=session_id,
        filename="agent-output.zip",
        bytes=_VIDEO_BYTES,
        content_type="application/zip",
        source_metadata={"tool": "upload_file"},
    )
    env.artifacts.put(agent_zip.blob_key or agent_zip.id, b"zip-bytes")
    legacy_zip = env.files.create(
        session_id=session_id,
        filename="legacy.zip",
        bytes=_VIDEO_BYTES,
        content_type="application/zip",
    )
    env.artifacts.put(legacy_zip.blob_key or legacy_zip.id, b"legacy-bytes")
    original = _by_path_row(env, session_id, filename="uploaded.zip")
    _archive(env, session_id)
    archived_at = env.conversations.get_conversation(session_id).archived_at
    assert archived_at is not None

    summary = await _job(env).sweep_once(now=archived_at + 31 * _DAY)

    assert summary.files_purged == 1
    assert _purge(_fresh(env, agent_zip.id)) is None
    assert env.artifacts.exists(agent_zip.blob_key or agent_zip.id)
    assert _purge(_fresh(env, legacy_zip.id)) is None
    assert env.artifacts.exists(legacy_zip.blob_key or legacy_zip.id)
    assert _purge(_fresh(env, original.id))["state"] == "done"
    assert not env.artifacts.exists(original.blob_key or original.id)


async def test_session_archived_inside_the_window_is_kept(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session archived 29 days ago is not yet a candidate."""
    _configure(monkeypatch, _DAYS_30)
    session_id = _new_session(env)
    stored = _by_path_row(env, session_id)
    _archive(env, session_id)
    archived_at = env.conversations.get_conversation(session_id).archived_at
    assert archived_at is not None

    summary = await _job(env).sweep_once(now=archived_at + 29 * _DAY)

    assert summary.sessions_scanned == 0
    assert summary.files_purged == 0
    assert _purge(_fresh(env, stored.id)) is None
    assert env.artifacts.exists(stored.id)


# ── scenario 17: dry-run ────────────────────────────────────────────


async def test_scenario_17_dry_run_writes_nothing(
    env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dry-run logs the would-purge row, claims nothing, and deletes nothing."""
    _configure(
        monkeypatch,
        {**_DAYS_30, "attachment_archive_cleanup_dry_run": True},
    )
    session_id = _new_session(env)
    stored = _by_path_row(env, session_id)
    _archive(env, session_id)
    archived_at = env.conversations.get_conversation(session_id).archived_at
    assert archived_at is not None

    with caplog.at_level("INFO", logger="omnigent.server.attachment_archive_cleanup"):
        summary = await _job(env).sweep_once(now=archived_at + 31 * _DAY)

    assert summary.mode == "dry-run"
    assert summary.files_purged == 0
    assert _purge(_fresh(env, stored.id)) is None
    assert env.artifacts.exists(stored.id)
    assert any("would purge" in record.getMessage() for record in caplog.records)


# ── scenario 18: unarchived between claim and delete ────────────────


class _UnarchivingFileStore(SqlAlchemyFileStore):
    """Claims a blob, then unarchives the session before the re-read."""

    def __init__(
        self,
        db_uri: str,
        conversations: SqlAlchemyConversationStore,
        session_id: str,
    ) -> None:
        super().__init__(db_uri)
        self._conversations = conversations
        self._session_id = session_id

    def claim_purge(self, blob_key: str, *, session_id: str, revision: int, now: int) -> Any:
        claimed = super().claim_purge(blob_key, session_id=session_id, revision=revision, now=now)
        self._conversations.update_conversation(self._session_id, archived=False)
        return claimed


async def test_scenario_18_unarchived_claim_is_released(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revision change between claim and delete releases the claim."""
    _configure(monkeypatch, _DAYS_30)
    session_id = _new_session(env)
    stored = _by_path_row(env, session_id)
    _archive(env, session_id)
    archived_at = env.conversations.get_conversation(session_id).archived_at
    assert archived_at is not None
    file_store = _UnarchivingFileStore(env.db_uri, env.conversations, session_id)

    summary = await _job(env, file_store=file_store).sweep_once(now=archived_at + 31 * _DAY)

    assert summary.skipped_unarchived == 1
    assert summary.released == 1
    assert summary.files_purged == 0
    assert _purge(_fresh(env, stored.id)) is None
    assert env.artifacts.exists(stored.id)
    conversation = env.conversations.get_conversation(session_id)
    assert conversation is not None and conversation.archived is False


class _RearchivingFileStore(SqlAlchemyFileStore):
    """Claims a blob, then unarchives and re-archives the session before the re-read."""

    def __init__(
        self,
        db_uri: str,
        conversations: SqlAlchemyConversationStore,
        session_id: str,
    ) -> None:
        super().__init__(db_uri)
        self._conversations = conversations
        self._session_id = session_id

    def claim_purge(self, blob_key: str, *, session_id: str, revision: int, now: int) -> Any:
        claimed = super().claim_purge(blob_key, session_id=session_id, revision=revision, now=now)
        self._conversations.update_conversation(self._session_id, archived=False)
        self._conversations.update_conversation(self._session_id, archived=True)
        return claimed


async def test_rearchived_session_releases_the_claim(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unarchive + re-archive between claim and re-read changes the revision."""
    _configure(monkeypatch, _DAYS_30)
    session_id = _new_session(env)
    stored = _by_path_row(env, session_id)
    first_revision = _archive(env, session_id)
    archived_at = env.conversations.get_conversation(session_id).archived_at
    assert archived_at is not None
    file_store = _RearchivingFileStore(env.db_uri, env.conversations, session_id)

    summary = await _job(env, file_store=file_store).sweep_once(now=archived_at + 31 * _DAY)

    assert summary.files_purged == 0
    assert summary.skipped_unarchived == 1
    assert summary.released == 1
    assert _purge(_fresh(env, stored.id)) is None
    assert env.artifacts.exists(stored.id)
    conversation = env.conversations.get_conversation(session_id)
    assert conversation is not None and conversation.archived is True
    assert conversation.archive_revision > first_revision


class _ForkingFileStore(SqlAlchemyFileStore):
    """Claims a blob, then inserts a live fork copy before the referrer re-check."""

    def __init__(
        self,
        db_uri: str,
        *,
        source_id: str,
        live_session_id: str,
    ) -> None:
        super().__init__(db_uri)
        self._source_id = source_id
        self._live_session_id = live_session_id
        self.fork_id: str | None = None

    def claim_purge(self, blob_key: str, *, session_id: str, revision: int, now: int) -> Any:
        claimed = super().claim_purge(blob_key, session_id=session_id, revision=revision, now=now)
        source = super().get(self._source_id)
        assert source is not None
        fork = super().create(
            session_id=self._live_session_id,
            filename=source.filename,
            bytes=source.bytes,
            content_type=source.content_type,
            blob_key=blob_key,
            source_metadata=dict(source.source_metadata or {}),
        )
        self.fork_id = fork.id
        return claimed


async def test_fork_copy_after_the_claim_releases_and_skips_shared(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork row inserted after the claim is caught by the referrer re-check."""
    _configure(monkeypatch, _DAYS_30)
    archived_session = _new_session(env, "archived source")
    source = _by_path_row(env, archived_session)
    _archive(env, archived_session)
    live_session = _new_session(env, "live fork")
    archived_at = env.conversations.get_conversation(archived_session).archived_at
    assert archived_at is not None
    file_store = _ForkingFileStore(env.db_uri, source_id=source.id, live_session_id=live_session)

    summary = await _job(env, file_store=file_store).sweep_once(now=archived_at + 31 * _DAY)

    fork_id = file_store.fork_id
    assert fork_id is not None
    assert summary.files_purged == 0
    assert summary.skipped_shared == 1
    assert summary.released == 1
    assert _purge(_fresh(env, source.id)) is None
    assert _purge(_fresh(env, fork_id)) is None
    assert env.artifacts.exists(source.blob_key or source.id)


async def test_resume_of_a_stale_claim_revision_releases(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimed row under an older revision is released, not finished."""
    _configure(monkeypatch, _DAYS_30)
    session_id = _new_session(env)
    stored = _by_path_row(env, session_id)
    blob_key = stored.blob_key or stored.id
    claim_revision = _archive(env, session_id)
    assert (
        env.files.claim_purge(blob_key, session_id=session_id, revision=claim_revision, now=5)
        is not None
    )
    assert env.conversations.update_conversation(session_id, archived=False) is not None
    assert env.conversations.update_conversation(session_id, archived=True) is not None
    archived_at = env.conversations.get_conversation(session_id).archived_at
    assert archived_at is not None

    summary = await _job(env).sweep_once(now=archived_at + 31 * _DAY)

    assert summary.files_purged == 0
    assert summary.skipped_unarchived == 1
    assert summary.released == 1
    assert _purge(_fresh(env, stored.id)) is None
    assert env.artifacts.exists(blob_key)


# ── scenario 19: shared blob after a fork ───────────────────────────


async def test_scenario_19_shared_blob_is_skipped(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live fork sharing the blob makes the claim refuse; both rows stay."""
    _configure(monkeypatch, _DAYS_30)
    archived_session = _new_session(env, "archived source")
    source = _by_path_row(env, archived_session)
    _archive(env, archived_session)
    live_session = _new_session(env, "live fork")
    fork = _by_path_row(env, live_session, blob_key=source.blob_key)
    archived_at = env.conversations.get_conversation(archived_session).archived_at
    assert archived_at is not None

    summary = await _job(env).sweep_once(now=archived_at + 31 * _DAY)

    assert summary.skipped_shared == 1
    assert summary.files_purged == 0
    assert _purge(_fresh(env, source.id)) is None
    assert _purge(_fresh(env, fork.id)) is None
    assert env.artifacts.exists(source.blob_key or source.id)


# ── scenario 20: crash after claim ──────────────────────────────────


async def test_scenario_20_resumes_a_claimed_row(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimed row from a crashed sweep is finished by the next sweep."""
    _configure(monkeypatch, _DAYS_30)
    session_id = _new_session(env)
    stored = _by_path_row(env, session_id)
    revision = _archive(env, session_id)
    blob_key = stored.blob_key or stored.id
    assert (
        env.files.claim_purge(blob_key, session_id=session_id, revision=revision, now=5)
        is not None
    )
    archived_at = env.conversations.get_conversation(session_id).archived_at
    assert archived_at is not None

    summary = await _job(env).sweep_once(now=archived_at + 31 * _DAY)

    assert summary.files_purged == 1
    assert summary.bytes_freed == _VIDEO_BYTES
    purge = _purge(_fresh(env, stored.id))
    assert purge is not None and purge["state"] == "done"
    assert not env.artifacts.exists(blob_key)


# ── scenario 21: the 500-blob cap ───────────────────────────────────


async def test_scenario_21_caps_finished_blobs(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """600 eligible blobs: 500 finish now, the other 100 wait for the next sweep."""
    _configure(monkeypatch, _DAYS_30)
    session_id = _new_session(env)
    rows = [_by_path_row(env, session_id, filename=f"clip{i}.mp4") for i in range(600)]
    _archive(env, session_id)
    archived_at = env.conversations.get_conversation(session_id).archived_at
    assert archived_at is not None
    job = _job(env)

    first = await job.sweep_once(now=archived_at + 31 * _DAY)
    assert first.files_purged == 500
    assert first.capped is True
    still_stored = [row for row in rows if env.artifacts.exists(row.blob_key or row.id)]
    assert len(still_stored) == 100

    second = await job.sweep_once(now=archived_at + 31 * _DAY)
    assert second.files_purged == 100
    assert second.capped is False

    done = [row for row in (_fresh(env, stored.id) for stored in rows) if _purge(row) is not None]
    assert len(done) == 600
    assert all(_purge(row)["state"] == "done" for row in done)
    assert not any(env.artifacts.exists(row.blob_key or row.id) for row in rows)


# ── workspaces: one sweep covers every tenant ───────────────────────


async def test_sweep_purges_every_workspace_with_archived_sessions(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two workspaces with one eligible video each: one unscoped sweep purges both."""
    _configure(monkeypatch, _DAYS_30)
    workspace_rows: dict[int, Any] = {}
    archived_at: int | None = None
    for workspace_id in (7, 8):
        with workspace_scope(workspace_id):
            session_id = _new_session(env, f"old session in {workspace_id}")
            workspace_rows[workspace_id] = _by_path_row(env, session_id)
            _archive(env, session_id)
            archived_at = env.conversations.get_conversation(session_id).archived_at
    assert archived_at is not None

    summary = await _job(env).sweep_once(now=archived_at + 31 * _DAY)

    assert summary.sessions_scanned == 2
    assert summary.files_purged == 2
    assert summary.errors == 0
    for workspace_id, stored in workspace_rows.items():
        with workspace_scope(workspace_id):
            assert _purge(_fresh(env, stored.id))["state"] == "done"
        assert not env.artifacts.exists(stored.blob_key or stored.id)


# ── summary file + /system/brief ────────────────────────────────────


async def test_summary_file_refuses_a_symlinked_destination(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked summary path is left alone; the real file is untouched."""
    _configure(monkeypatch, _DAYS_30)
    state_dir = env.tmp_path / "system-status"
    state_dir.mkdir(parents=True, exist_ok=True)
    target = env.tmp_path / "target.json"
    target.write_text('{"keep": true}')
    (state_dir / _SUMMARY_NAME).symlink_to(target)

    await _job(env).sweep_once(now=int(time.time()))

    assert (state_dir / _SUMMARY_NAME).is_symlink()
    assert json.loads(target.read_text()) == {"keep": True}


class _EmptyHostStore:
    def list_hosts(self, user_id: str) -> list[Any]:
        del user_id
        return []


async def test_system_brief_exposes_the_last_summary(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The admin brief returns the summary file, or None before a sweep."""
    _configure(monkeypatch, {})
    hub = SystemStatusHub(env.tmp_path, None)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(status_code=exc.http_status, content={"error": str(exc)})

    app.state.system_status = hub
    app.include_router(create_system_status_router(_EmptyHostStore()), prefix="/v1")
    client = TestClient(app)

    assert client.get("/v1/system/brief").json()["attachment_archive_cleanup"] is None

    await _job(env).sweep_once(now=int(time.time()))

    payload = client.get("/v1/system/brief").json()["attachment_archive_cleanup"]
    assert payload is not None
    assert payload["mode"] == "off"


# ── lifespan wiring ─────────────────────────────────────────────────


def test_lifespan_starts_and_stops_the_cleanup(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    """The job is started next to the reaper and shut down with the app."""
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
    )
    assert getattr(app.state, "attachment_archive_cleanup", None) is None

    with TestClient(app):
        job = app.state.attachment_archive_cleanup
        assert isinstance(job, AttachmentArchiveCleanup)
        assert job._task is not None
        assert not job._task.done()

    assert job._task is None
