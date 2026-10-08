"""Attachment upload type/size enforcement on POST /v1/sessions/{id}/resources/files."""

from __future__ import annotations

import asyncio
import tracemalloc
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT
from omnigent.host.frames import HOST_CAPABILITIES, HostHelloFrame
from omnigent.runtime.content_resolver import (
    MAX_TEXT_UPLOAD_BYTES,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


@pytest.fixture
def upload_client(db_uri: str, tmp_path) -> Iterator[tuple[TestClient, str]]:
    """A sessions route client with file + artifact stores and one session."""
    conversation_store = SqlAlchemyConversationStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store.create(
        agent_id="087b7cb7ac30abf4debfaa578d052ec6",
        name="test-agent",
        bundle_location="087b7cb7ac30abf4debfaa578d052ec6/bundle",
    )
    conv = conversation_store.create_conversation(
        title="upload session", agent_id="087b7cb7ac30abf4debfaa578d052ec6"
    )
    # A Claude Code session, so filesystem types are accepted.
    conversation_store.set_labels(conv.id, CLAUDE_NATIVE_CODING_AGENT.presentation_labels)
    conversation_store.set_host_id(
        conv.id, "d75381f2c94b4e49a3c684946d4ddbc4", workspace=str(tmp_path)
    )
    host_registry = HostRegistry()
    host_registry.register(
        "d75381f2c94b4e49a3c684946d4ddbc4",
        Mock(),
        HostHelloFrame(
            version="0.15.0",
            frame_protocol_version=1,
            name="upload",
            capabilities=HOST_CAPABILITIES,
        ),
        owner=None,
    )

    app = FastAPI()
    app.state.host_registry = host_registry

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
            file_store=file_store,
            artifact_store=artifact_store,
            host_registry=host_registry,
        ),
        prefix="/v1",
    )

    with TestClient(app) as client:
        yield client, conv.id


def _upload(
    client: TestClient,
    session_id: str,
    filename: str,
    data: bytes = b"data",
    content_type: str = "application/octet-stream",
) -> httpx.Response:
    """Post a multipart file through the actual upload route."""
    return client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": (filename, data, content_type)},
    )


@pytest.mark.parametrize(
    "filename,content_type",
    [
        ("notes.txt", "text/plain"),
        ("archive.zip", "application/zip"),
        ("deck.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("app.db", "application/octet-stream"),
        ("report.docx", "application/zip"),
        ("data.csv", "application/vnd.ms-excel"),
    ],
)
def test_upload_supported_types(
    upload_client: tuple[TestClient, str], filename: str, content_type: str
) -> None:
    """Supported extensions remain usable even with common browser MIME mismatches."""
    client, session_id = upload_client
    response = _upload(client, session_id, filename, content_type=content_type)
    assert response.status_code == 201, response.text
    assert response.json()["name"] == filename


def test_upload_stores_unknown_type_by_path(upload_client: tuple[TestClient, str]) -> None:
    """Formats outside the inline set are stored for by-path delivery."""
    client, session_id = upload_client
    resp = _upload(client, session_id, "clip.mp4", b"\x00\x00\x00 fake mp4 bytes", "video/mp4")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "clip.mp4"
    assert body["metadata"]["source_metadata"] == {"delivery": "filesystem"}


def test_upload_accepts_filesystem_types_for_an_sdk_harness(
    upload_client: tuple[TestClient, str], db_uri: str
) -> None:
    """An SDK session stores by-path files like a native one; no harness gate."""
    client, _ = upload_client
    conversations = SqlAlchemyConversationStore(db_uri)
    sdk_session = conversations.create_conversation(
        title="sdk session", agent_id="087b7cb7ac30abf4debfaa578d052ec6"
    )
    conversations.set_host_id(
        sdk_session.id, "d75381f2c94b4e49a3c684946d4ddbc4", workspace="/tmp/test-upload"
    )
    resp = client.post(
        f"/v1/sessions/{sdk_session.id}/resources/files",
        files={"file": ("archive.zip", b"PK\x03\x04 fake zip", "application/zip")},
    )
    assert resp.status_code == 201, resp.text
    stored = SqlAlchemyFileStore(db_uri).get(resp.json()["id"], session_id=sdk_session.id)
    assert stored is not None
    assert stored.source_metadata == {"delivery": "filesystem"}


@pytest.mark.parametrize(
    "block",
    [
        {
            "type": "input_file",
            "filename": "payload.zip",
            "file_data": "data:application/zip;base64,UEs=",
        },
        # A file_id alongside inline bytes would skip re-resolution, so it is refused too.
        {
            "type": "input_file",
            "file_id": "file_abc",
            "filename": "payload.zip",
            "file_data": "data:application/zip;base64,UEs=",
        },
    ],
)
def test_message_cannot_inline_a_filesystem_attachment(
    upload_client: tuple[TestClient, str], block: dict[str, str]
) -> None:
    """Inline bytes would reach the harness without the upload route's checks."""
    client, session_id = upload_client
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "message", "data": {"role": "user", "content": [block]}},
    )
    assert resp.status_code == 400, resp.text
    assert "payload.zip" in resp.text


@pytest.mark.parametrize(
    "filename,target_harness,event_type,block_type",
    [
        ("archive.zip", "cursor-native", "message", "input_file"),
        ("archive.zip", "openai-agents", "message", "input_file"),
        ("archive.zip", "openai-agents", "message", "input_image"),
        ("archive.zip", "cursor-native", "slash_command", "input_file"),
        ("archive.zip", "claude-native", "message", "input_file"),
        ("archive.zip", "codex-native", "message", "input_file"),
        ("notes.txt", "openai-agents", "message", "input_file"),
        ("clip.mp4", "openai-agents", "message", "input_file"),
    ],
)
def test_send_admits_stored_upload_for_any_harness(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    target_harness: str,
    event_type: str,
    block_type: str,
) -> None:
    """Stored uploads reach policy for every target harness; no harness gate."""
    from omnigent.server.routes import sessions
    from omnigent.server.routes.sessions import routes_events

    client, source_id = upload_client
    uploaded = _upload(client, source_id, filename)
    assert uploaded.status_code == 201, uploaded.text
    target = SqlAlchemyAgentStore(db_uri).create("c" * 32, "target", "target/bundle")

    def load(_agent_id: str, bundle_location: str, **_kwargs: object) -> SimpleNamespace:
        harness = target_harness if bundle_location == target.bundle_location else "claude-native"
        return SimpleNamespace(
            spec=SimpleNamespace(executor=SimpleNamespace(harness_kind=harness))
        )

    monkeypatch.setattr(sessions, "get_agent_cache", lambda: SimpleNamespace(load=load))
    changed = client.post(f"/v1/sessions/{source_id}/fork", json={"agent_id": target.id})
    assert changed.status_code == 201, changed.text
    session_id = changed.json()["id"]
    files = SqlAlchemyFileStore(db_uri).list(session_id).data
    assert len(files) == 1
    assert files[0].filename == filename
    conversations = SqlAlchemyConversationStore(db_uri)
    before = conversations.list_items(session_id).data

    # Stop admitted inputs at policy so the test never needs a live runner.
    policy = AsyncMock(return_value={"verdict": "deny", "reason": "test policy"})
    dispatch = AsyncMock()
    monkeypatch.setattr(routes_events, "_evaluate_input_policy", policy)
    monkeypatch.setattr(routes_events, "_persist_policy_deny_sentinel", AsyncMock())
    monkeypatch.setattr(routes_events, "_dispatch_session_event_to_runner", dispatch)
    data: dict[str, object] = {
        "role": "user",
        "content": [{"type": block_type, "file_id": files[0].id, "filename": "renamed.txt"}],
    }
    if event_type == "slash_command":
        data["name"] = "compact"
    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": event_type, "data": data},
    )
    assert response.status_code == 202, response.text
    assert response.json()["denied"] is True
    policy.assert_awaited_once()
    dispatch.assert_not_awaited()
    assert conversations.list_items(session_id).data == before


def test_upload_rejects_oversized_filesystem_file(
    upload_client: tuple[TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zip over the configured per-file cap is rejected with 413."""
    monkeypatch.setattr(
        "omnigent.server.server_config.load_server_config",
        lambda: {"filesystem_attachment_max_bytes": 1024},
    )
    client, session_id = upload_client
    oversized = b"\x00" * 1025
    resp = _upload(client, session_id, "huge.zip", oversized, "application/zip")
    assert resp.status_code == 413, resp.status_code


def test_upload_rejects_undecodable_oversized_image(
    upload_client: tuple[TestClient, str],
) -> None:
    """Image bytes over the model budget that don't decode are rejected 413.

    Real images are downscaled under the budget; garbage that only claims to
    be an image can't be compressed, so the route surfaces a 413 instead of
    storing an oversized attachment.
    """
    from omnigent.runtime.content_resolver import IMAGE_MODEL_BUDGET_BYTES

    client, session_id = upload_client
    oversized = b"\x00" * (IMAGE_MODEL_BUDGET_BYTES + 1)
    resp = _upload(client, session_id, "huge.png", oversized, "image/png")
    assert resp.status_code == 413, resp.status_code


def test_upload_large_image_is_compressed_under_budget(
    upload_client: tuple[TestClient, str],
) -> None:
    """A large but valid image uploads and is stored shrunk under the model budget."""
    import os
    from io import BytesIO

    from PIL import Image

    from omnigent.runtime.content_resolver import IMAGE_MODEL_BUDGET_BYTES

    client, session_id = upload_client
    side = 1600
    buffer = BytesIO()
    Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(buffer, format="PNG")
    payload = buffer.getvalue()
    assert len(payload) > IMAGE_MODEL_BUDGET_BYTES

    resp = _upload(client, session_id, "screenshot.png", payload, "image/png")
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["metadata"]["bytes"] <= IMAGE_MODEL_BUDGET_BYTES
    # Opaque image re-encodes (WebP preferred, JPEG fallback), so the stored
    # name is realigned to match the new type.
    assert body["name"] in ("screenshot.webp", "screenshot.jpg")


def test_upload_text_just_under_limit_succeeds(upload_client: tuple[TestClient, str]) -> None:
    """A text file just under the text cap is accepted."""
    client, session_id = upload_client
    payload = b"a" * (MAX_TEXT_UPLOAD_BYTES - 1024)
    resp = _upload(client, session_id, "big.txt", payload, "text/plain")
    assert resp.status_code in (200, 201), resp.status_code


@pytest.mark.parametrize("size,rejected", [(100, False), (101, True)])
async def test_read_upload_capped_boundary(size: int, rejected: bool) -> None:
    """The read cap admits the exact limit and rejects one byte more."""
    from io import BytesIO

    from fastapi import HTTPException, UploadFile

    from omnigent.server.routes.sessions import _read_upload_capped

    data = b"x" * size
    upload = UploadFile(file=BytesIO(data))
    if rejected:
        with pytest.raises(HTTPException) as error:
            await _read_upload_capped(upload, 100)
        assert error.value.status_code == 413
    else:
        assert await _read_upload_capped(upload, 100) == data


@pytest.mark.parametrize(
    "filename,content_type,status",
    [
        ("archive.zip", "application/zip", 415),
        ("archive.zip", "text/plain", 415),
        ("report.docx", "application/zip", 201),
    ],
)
def test_upload_denylist_uses_filename(
    upload_client: tuple[TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    content_type: str,
    status: int,
) -> None:
    """The denylist rejects matching extensions regardless of the declared MIME."""
    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_denied_extensions",
        lambda: frozenset({".zip"}),
    )
    client, session_id = upload_client
    response = _upload(client, session_id, filename, content_type=content_type)
    assert response.status_code == status, response.text
    if status == 415:
        assert "not accepted by this deployment" in response.text
    else:
        assert response.json()["name"] == filename


@pytest.mark.parametrize(
    "existing,limit,status",
    [(["a.zip", "b.zip"], 2, 413), (["a.txt", "b.txt", "c.txt"], 1, 201)],
)
def test_upload_quota_counts_only_filesystem_types(
    upload_client: tuple[TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
    existing: list[str],
    limit: int,
    status: int,
) -> None:
    """Only filesystem formats spend the quota across successive uploads."""
    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_file_limit", lambda: limit
    )
    client, session_id = upload_client
    for filename in existing:
        response = _upload(client, session_id, filename)
        assert response.status_code == 201, response.text
    response = _upload(client, session_id, "bundle.zip")
    assert response.status_code == status, response.text
    if status == 413:
        assert "file attachments" in response.text


async def test_parallel_uploads_cannot_overspend_the_filesystem_quota(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two uploads racing for the last free slot: exactly one is stored."""
    import asyncio
    import time

    import httpx

    from omnigent.stores.artifact_store.local import LocalArtifactStore

    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_file_limit",
        lambda: 1,
    )
    real_put_stream = LocalArtifactStore.put_stream

    def slow_put_stream(store, key, fileobj, *, max_bytes):  # type: ignore[no-untyped-def]
        # Widen the gap between the quota check and the store.
        time.sleep(0.05)
        return real_put_stream(store, key, fileobj, max_bytes=max_bytes)

    monkeypatch.setattr(LocalArtifactStore, "put_stream", slow_put_stream)
    client, session_id = upload_client
    transport = httpx.ASGITransport(app=client.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = await asyncio.gather(
            *(
                http.post(
                    f"/v1/sessions/{session_id}/resources/files",
                    files={"file": (f"race{i}.zip", b"PK\x03\x04 fake zip", "application/zip")},
                )
                for i in range(2)
            )
        )

    assert sorted(r.status_code for r in responses) == [201, 413]
    stored = SqlAlchemyFileStore(db_uri).list(session_id=session_id, limit=10).data
    assert [f.filename for f in stored if f.filename.endswith(".zip")] == [
        next(r.json()["name"] for r in responses if r.status_code == 201)
    ]


def test_quota_counts_filesystem_files_past_any_page_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quota accounting walks beyond 20 pages of ordinary files."""
    from fastapi import HTTPException

    from omnigent.entities import StoredFile
    from omnigent.entities.pagination import PagedList
    from omnigent.server.routes._sessions.helpers import _enforce_filesystem_attachment_policy

    records = [
        StoredFile(id=f"f{i:03d}", created_at=i, filename=f"n{i}.txt", bytes=2) for i in range(30)
    ] + [StoredFile(id="f999", created_at=999, filename="one.zip", bytes=4)]

    class _OnePerPageStore:
        """Serves the session's files one record per page, oldest first."""

        def list(self, session_id: str, limit: int, after: str | None, order: str):
            del session_id, limit, order
            index = 0 if after is None else [r.id for r in records].index(after) + 1
            page = records[index : index + 1]
            return PagedList(
                data=page,
                first_id=page[0].id if page else None,
                last_id=page[-1].id if page else None,
                has_more=index + 1 < len(records),
            )

    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_file_limit",
        lambda: 1,
    )

    with pytest.raises(HTTPException) as exc:
        _enforce_filesystem_attachment_policy(
            ["two.zip"],
            session_id="conv_1",
            file_store=_OnePerPageStore(),  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 413


@pytest.mark.parametrize("filename", ["archive.zip", "report.docx", "state.sqlite"])
def test_old_host_refuses_new_types_without_storing(
    upload_client: tuple[TestClient, str], db_uri: str, filename: str
) -> None:
    """A legacy hello cannot promise cold resume; rejection leaves no file row."""
    client, session_id = upload_client
    client.app.state.host_registry.get("d75381f2c94b4e49a3c684946d4ddbc4").hello.capabilities = []
    response = _upload(client, session_id, filename, b"data", "application/octet-stream")
    assert response.status_code == 409, response.text
    assert "Update Omnigent" in response.text
    assert SqlAlchemyFileStore(db_uri).list(session_id).data == []


def test_old_host_keeps_existing_attachment_types(upload_client: tuple[TestClient, str]) -> None:
    """The upgrade requirement does not change existing text/image uploads."""
    from io import BytesIO

    from PIL import Image

    client, session_id = upload_client
    client.app.state.host_registry.get("d75381f2c94b4e49a3c684946d4ddbc4").hello.capabilities = []
    image = BytesIO()
    Image.new("RGB", (2, 2), "red").save(image, format="PNG")
    for filename, data, mime in (
        ("table.csv", b"a,b\n1,2\n", "text/csv"),
        ("picture.png", image.getvalue(), "image/png"),
    ):
        response = _upload(client, session_id, filename, data, mime)
        assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_first_managed_upload_waits_for_host_binding(
    upload_client: tuple[TestClient, str], db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upload racing provisioning waits, then checks the newly bound host."""
    import asyncio

    import httpx

    from omnigent.server.managed_hosts import ManagedLaunchTracker
    from omnigent.server.routes.sessions import routes_resources

    client, session_id = upload_client
    store = SqlAlchemyConversationStore(db_uri)
    store.clear_host_binding(session_id)
    tracker = ManagedLaunchTracker()
    tracker.begin(session_id)
    client.app.state.managed_launches = tracker
    waiting = asyncio.Event()
    original = routes_resources._await_settled_managed_launch

    async def observe_wait(launch):
        waiting.set()
        await original(launch)

    monkeypatch.setattr(routes_resources, "_await_settled_managed_launch", observe_wait)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://test"
    ) as async_client:
        upload = asyncio.create_task(
            async_client.post(
                f"/v1/sessions/{session_id}/resources/files",
                files={"file": ("archive.zip", b"data", "application/zip")},
            )
        )
        await asyncio.wait_for(waiting.wait(), timeout=5)
        assert not upload.done()
        assert SqlAlchemyFileStore(db_uri).list(session_id).data == []
        store.set_host_id(
            session_id, "d75381f2c94b4e49a3c684946d4ddbc4", workspace="/tmp/test-upload"
        )
        tracker.finish(session_id)
        response = await upload
    assert response.status_code == 201, response.text


def test_recovered_host_ignores_stale_managed_launch_failure(
    upload_client: tuple[TestClient, str],
) -> None:
    """A retained failure does not block uploads after the session has recovered."""
    from omnigent.server.managed_hosts import ManagedLaunchTracker

    client, session_id = upload_client
    tracker = ManagedLaunchTracker()
    tracker.begin(session_id)
    tracker.fail(session_id, "earlier provision failed")
    client.app.state.managed_launches = tracker
    response = _upload(client, session_id, "archive.zip", b"data", "application/zip")
    assert response.status_code == 201, response.text


def test_unbound_upload_requires_a_connected_runtime(
    upload_client: tuple[TestClient, str],
    db_uri: str,
) -> None:
    """An unknown host cannot be assumed current just because the server is new."""
    client, session_id = upload_client
    SqlAlchemyConversationStore(db_uri).clear_host_binding(session_id)
    response = _upload(client, session_id, "archive.zip", b"data", "application/zip")
    assert response.status_code == 409, response.text
    assert "Connect an updated" in response.text
    assert SqlAlchemyFileStore(db_uri).list(session_id).data == []


def test_upload_wakes_a_sleeping_runtime_before_checking_support(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first attachment can wake a managed sandbox without requiring a text turn."""
    from unittest.mock import AsyncMock

    from omnigent.server.routes.sessions import routes_resources

    client, session_id = upload_client
    registry = client.app.state.host_registry
    connection = registry.get("d75381f2c94b4e49a3c684946d4ddbc4")
    registry.deregister(connection.host_id)

    async def wake(**kwargs):
        registry.register(connection.host_id, Mock(), connection.hello, owner=None)
        return None, SqlAlchemyConversationStore(db_uri).get_conversation(session_id)

    ensure = AsyncMock(side_effect=wake)
    monkeypatch.setattr(routes_resources, "ensure_runner_connected", ensure)
    response = _upload(client, session_id, "archive.zip", b"data", "application/zip")
    assert response.status_code == 201, response.text
    ensure.assert_awaited_once()


@pytest.mark.parametrize("partial_write", [False, True])
@pytest.mark.parametrize("filename", ["archive.zip", "notes.txt"])
def test_failed_upload_releases_quota_and_can_retry(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    partial_write: bool,
    filename: str,
) -> None:
    """Blob failures leave no metadata, partial bytes, or resource event behind."""
    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_file_limit", lambda: 1
    )
    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_total_bytes_limit", lambda: 4
    )
    client, session_id = upload_client
    original_put = LocalArtifactStore.put
    attempted_ids: list[str] = []

    def fail_put(store: LocalArtifactStore, key: str, data: bytes) -> None:
        attempted_ids.append(key)
        if partial_write:
            original_put(store, key, data[:1])
        raise OSError("test storage write failure")

    def fail_put_stream(
        store: LocalArtifactStore, key: str, fileobj: BinaryIO, *, max_bytes: int | None
    ) -> None:
        attempted_ids.append(key)
        if partial_write:
            original_put(store, key, fileobj.read(1))
        raise OSError("test storage write failure")

    url = f"/v1/sessions/{session_id}/resources/files"
    upload = {"file": (filename, b"data", "application/octet-stream")}
    with monkeypatch.context() as storage_failure:
        storage_failure.setattr(LocalArtifactStore, "put", fail_put)
        storage_failure.setattr(LocalArtifactStore, "put_stream", fail_put_stream)
        failed = client.post(url, files=upload)
    assert failed.status_code == 500, failed.text
    assert "Failed to upload file" in failed.text
    assert len(attempted_ids) == 1
    assert SqlAlchemyFileStore(db_uri).list(session_id).data == []
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    assert not artifacts.exists(attempted_ids[0])
    conversations = SqlAlchemyConversationStore(db_uri)
    assert conversations.list_items(session_id, type="resource_event").data == []

    retried = client.post(url, files=upload)
    assert retried.status_code == 201, retried.text
    assert artifacts.get(retried.json()["id"]) == b"data"
    files = SqlAlchemyFileStore(db_uri).list(session_id).data
    assert [stored.id for stored in files] == [retried.json()["id"]]
    assert len(conversations.list_items(session_id, type="resource_event").data) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancelled_upload_waits_for_worker_then_rolls_back(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancellations: int,
) -> None:
    """A client disconnect during put_stream leaves no row, blob, or held lock."""
    import threading

    from omnigent.server.routes.sessions import routes_resources
    from omnigent.stores.artifact_store.local import LocalArtifactStore

    client, session_id = upload_client
    lock = routes_resources._attachment_upload_lock(session_id)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    lock_at_finish: list[bool] = []
    real_put_stream = LocalArtifactStore.put_stream

    def blocking_put_stream(
        store: LocalArtifactStore, key: str, fileobj: BinaryIO, *, max_bytes: int | None
    ) -> int:
        started.set()
        assert release.wait(timeout=5)
        result = real_put_stream(store, key, fileobj, max_bytes=max_bytes)
        lock_at_finish.append(lock.locked())
        finished.set()
        return result

    monkeypatch.setattr(LocalArtifactStore, "put_stream", blocking_put_stream)

    transport = httpx.ASGITransport(app=client.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        upload = asyncio.create_task(
            http.post(
                f"/v1/sessions/{session_id}/resources/files",
                files={"file": ("clip.mp4", b"\x00\x00\x00 fake mp4", "video/mp4")},
            )
        )
        assert await asyncio.to_thread(started.wait, 5)
        for _ in range(cancellations):
            upload.cancel()
            # Let each cancellation reach the handler's shielded await before
            # the worker finishes — the race this test pins.
            await asyncio.sleep(0.05)
        assert lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await upload

    assert finished.is_set()
    # The lock must survive until the worker's write returned.
    assert lock_at_finish == [True]
    assert SqlAlchemyFileStore(db_uri).list(session_id).data == []
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    assert [p for p in Path(artifacts.storage_location).rglob("*") if p.is_file()] == []
    assert not lock.locked()


# ── request-size limits (scenario 10) ──────────────────────────────


def test_upload_declared_content_length_over_request_limit_is_early_413(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared 3 GiB body is rejected before the handler runs (scenario 10)."""
    from omnigent.server.routes.sessions import routes_resources

    handler = Mock(side_effect=AssertionError("upload handler ran for a rejected request"))
    monkeypatch.setattr(routes_resources, "_enforce_filesystem_attachment_policy", handler)

    client, session_id = upload_client
    response = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        headers={"Content-Length": str(3 * 1024**3)},
    )

    assert response.status_code == 413, response.text
    assert "2 GiB" in response.text
    assert SqlAlchemyFileStore(db_uri).list(session_id).data == []
    handler.assert_not_called()


def test_upload_chunked_body_over_request_limit_is_413(
    upload_client: tuple[TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunked body without Content-Length is bounded as it arrives."""
    monkeypatch.setattr(
        "omnigent.server.server_config.load_server_config",
        lambda: {"attachment_max_upload_bytes": 1024},
    )
    client, session_id = upload_client
    boundary = "test-boundary"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="clip.mp4"\r\n'
        "Content-Type: video/mp4\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    chunks = [prefix + b"x" * 600, b"y" * 600 + suffix]

    response = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        content=iter(chunks),
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )

    assert response.status_code == 413, response.text
    assert "1 KiB" in response.text


# ── configured per-file / per-session limits ───────────────────────


def test_configured_per_file_limit_rejects_by_path_upload(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An 11 MB by-path upload under a 10 MiB cap leaves no row and no blob (scenario 11)."""
    limit = 10 * 1024 * 1024
    monkeypatch.setattr(
        "omnigent.server.server_config.load_server_config",
        lambda: {"filesystem_attachment_max_bytes": limit},
    )
    client, session_id = upload_client
    payload = b"\x00" * (limit + 1024 * 1024)

    response = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("clip.mp4", payload, "video/mp4")},
    )

    assert response.status_code == 413, response.text
    assert SqlAlchemyFileStore(db_uri).list(session_id).data == []
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    assert [p for p in Path(artifacts.storage_location).rglob("*") if p.is_file()] == []


def test_zero_file_count_limit_means_unlimited(
    upload_client: tuple[TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``filesystem_attachment_max_files: 0`` lifts the per-session count (scenario 12)."""
    monkeypatch.setattr(
        "omnigent.server.server_config.load_server_config",
        lambda: {"filesystem_attachment_max_files": 0},
    )
    client, session_id = upload_client
    for index in range(25):
        response = _upload(
            client, session_id, f"clip{index}.mp4", b"\x00\x00\x00\x00", "video/mp4"
        )
        assert response.status_code == 201, response.text


# ── streamed content route (scenario 13) ───────────────────────────


def _get_without_buffering(
    app: Any,
    path: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], int]:
    """Drive the ASGI app directly, discarding body chunks as they arrive.

    ``httpx.ASGITransport`` accumulates the whole response in memory, which
    would hide the streaming bound this test measures.
    """

    async def run() -> tuple[int, dict[str, str], int]:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
            ],
            "client": ("127.0.0.1", 44444),
            "server": ("testserver", 80),
        }
        status = 0
        response_headers: dict[str, str] = {}
        total = 0
        delivered = False

        async def receive() -> dict[str, object]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            # Park the disconnect listener until the response cancels it.
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def send(message: dict[str, object]) -> None:
            nonlocal status, response_headers, total
            if message["type"] == "http.response.start":
                status = int(message["status"])  # type: ignore[call-overload]
                response_headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in message["headers"]  # type: ignore[union-attr]
                }
            elif message["type"] == "http.response.body":
                total += len(message.get("body", b""))

        await app(scope, receive, send)
        return status, response_headers, total

    return asyncio.run(run())


def test_content_route_streams_local_blob_with_bounded_memory(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    tmp_path: Path,
) -> None:
    """A 100 MB local blob streams chunked, far below one whole-file buffer."""
    client, session_id = upload_client
    file_store = SqlAlchemyFileStore(db_uri)
    stored = file_store.create(
        session_id=session_id,
        filename="big.mp4",
        bytes=100 * 1024 * 1024,
        content_type="video/mp4",
    )
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    source = tmp_path / "source.bin"
    with source.open("wb") as handle:
        handle.truncate(100 * 1024 * 1024)
    with source.open("rb") as handle:
        artifacts.put_stream(stored.id, handle, max_bytes=None)

    url = f"/v1/sessions/{session_id}/resources/files/{stored.id}/content"

    tracemalloc.start()
    try:
        status, headers, total = _get_without_buffering(client.app, url)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert status == 200
    assert headers["content-length"] == str(100 * 1024 * 1024)
    assert headers["content-type"] == "video/mp4"
    assert "etag" in headers
    assert headers["content-disposition"].startswith("attachment;")
    assert headers["x-content-type-options"] == "nosniff"
    assert total == 100 * 1024 * 1024
    assert peak < 8 * 1024 * 1024


def test_content_route_streamed_path_keeps_etag_and_304(
    upload_client: tuple[TestClient, str],
) -> None:
    """The streamed local path still revalidates from the cached ETag."""
    client, session_id = upload_client
    uploaded = _upload(client, session_id, "clip.mp4", b"\x00\x00\x00 fake mp4", "video/mp4")
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["id"]
    url = f"/v1/sessions/{session_id}/resources/files/{file_id}/content"

    first = client.get(url)
    assert first.status_code == 200
    assert first.content == b"\x00\x00\x00 fake mp4"
    etag = first.headers["etag"]

    cached = client.get(url, headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["etag"] == etag


# ── archive cleanup: 410 on a purged row (scenario 16) ─────────────


def test_content_route_returns_410_for_a_purged_file(
    upload_client: tuple[TestClient, str],
    db_uri: str,
) -> None:
    """A ``done`` row's content is gone: 410 with the cleanup date, even cached."""
    client, session_id = upload_client
    uploaded = _upload(client, session_id, "clip.mp4", b"\x00\x00\x00 fake mp4", "video/mp4")
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["id"]
    url = f"/v1/sessions/{session_id}/resources/files/{file_id}/content"
    assert client.get(url).status_code == 200
    etag = client.get(url).headers["etag"]

    files = SqlAlchemyFileStore(db_uri)
    stored = files.get(file_id, session_id=session_id)
    assert stored is not None
    blob_key = stored.blob_key or stored.id
    assert (
        files.claim_purge(blob_key, session_id=session_id, revision=1, now=1_700_000_000)
        is not None
    )
    files.finish_purge(blob_key, now=1_700_000_000)

    response = client.get(url)
    assert response.status_code == 410
    assert "removed by the archive cleanup on 2023-11-14" in response.text

    # A cached ETag must not resurrect the deleted bytes via a 304.
    cached = client.get(url, headers={"If-None-Match": etag})
    assert cached.status_code == 410


# ── archive cleanup: quota ignores purged rows ──────────────────────


@pytest.mark.parametrize(
    ("state", "rejected"),
    [("done", False), ("claimed", True), (None, True)],
)
def test_quota_ignores_only_purged_rows(
    monkeypatch: pytest.MonkeyPatch,
    state: str | None,
    rejected: bool,
) -> None:
    """Only ``done`` rows free their quota share; claimed rows still count."""
    from fastapi import HTTPException

    from omnigent.entities import StoredFile
    from omnigent.entities.pagination import PagedList
    from omnigent.server.routes._sessions.helpers import (
        _enforce_filesystem_attachment_policy,
    )

    metadata: dict[str, Any] = {"delivery": "filesystem"}
    if state is not None:
        metadata["purge"] = {"state": state, "revision": 1, "at": 1700000000}
    existing = StoredFile(
        id="f" * 32,
        created_at=1,
        filename="clip.mp4",
        bytes=1_000_000,
        session_id="conv_1",
        source_metadata=metadata,
    )

    class _Store:
        def list(self, session_id: str, limit: int, after: str | None, order: str):
            del session_id, limit, after, order
            return PagedList(
                data=[existing],
                first_id=existing.id,
                last_id=existing.id,
                has_more=False,
            )

    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_file_limit",
        lambda: 1,
    )
    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_total_bytes_limit",
        lambda: 1_000_000,
    )

    if rejected:
        with pytest.raises(HTTPException) as error:
            _enforce_filesystem_attachment_policy(
                ["new.mp4"],
                session_id="conv_1",
                file_store=_Store(),  # type: ignore[arg-type]
                sizes=[1_000_000],
            )
        assert error.value.status_code == 413
    else:
        cap = _enforce_filesystem_attachment_policy(
            ["new.mp4"],
            session_id="conv_1",
            file_store=_Store(),  # type: ignore[arg-type]
            sizes=[1_000_000],
        )
        # The purged row freed the whole session budget for the new upload.
        assert cap == 1_000_000
