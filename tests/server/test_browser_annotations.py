"""Receiving-session persistence and the narrow browser session picker."""

from __future__ import annotations

import asyncio
import hashlib
import io
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import update
from sqlalchemy.orm import Session

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationMetadata,
    SqlSessionPermission,
    SqlUser,
)
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.server.annotation_batch import AnnotationBatch
from omnigent.server.annotation_receiver_store import AnnotationReceiverStore, ReceiverConflict
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.routes.browser_annotations import _await_claimed_write
from omnigent.server.routes.device_auth import create_device_auth_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.test_device_auth import _build_accounts_app, _login_admin

CHROME_ORIGIN = "chrome-extension://" + "a" * 32
FIRST_PARTY_ORIGIN = "http://localhost:8000"


@pytest.mark.asyncio
async def test_cancelled_upload_waits_for_claimed_sql_write() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def write() -> None:
        started.set()
        assert release.wait(timeout=5)
        finished.set()

    operation = asyncio.create_task(_await_claimed_write(write))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert finished.is_set()


def test_pairing_code_is_not_issued_for_split_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_CHROME_ANNOTATIONS_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_CHROME_EXTENSION_ORIGIN", CHROME_ORIGIN)
    config = SimpleNamespace(
        cookie_secret=b"k" * 32,
        base_url="https://omni.example.test",
        session_cookie_name="__Host-ap_session",
    )
    provider = SimpleNamespace(_source="accounts", _accounts_config=config)
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/grants.db")
    app = FastAPI()
    app.include_router(
        create_device_auth_router(
            provider,
            store,
            annotation_storage_supported=False,  # type: ignore[arg-type]
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/oauth/annotations/authorize",
            json={"label": "Browser"},
            headers={"Origin": CHROME_ORIGIN},
        )
    assert response.status_code == 503
    assert response.json()["error"] == "annotation_storage_unsupported"


def test_app_disables_annotations_when_conversations_use_another_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_CHROME_ANNOTATIONS_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_CHROME_EXTENSION_ORIGIN", CHROME_ORIGIN)
    split_uri = f"sqlite:///{tmp_path}/other.db"
    generator = _build_accounts_app(tmp_path, monkeypatch, conversation_db_url=split_uri)
    client = next(generator)
    try:
        _login_admin(client)
        info = client.get("/v1/info")
        assert info.status_code == 200
        assert info.json()["browser_annotations_available"] is False
        request_code = client.post(
            "/oauth/annotations/authorize",
            json={"label": "Browser"},
            headers={"Origin": CHROME_ORIGIN},
        )
        assert request_code.status_code == 503
        assert request_code.json()["error"] == "annotation_storage_unsupported"
        write_receiver = client.put(
            "/v1/me/annotation-receiver",
            json={"session_id": None, "revision": 0},
            headers={"Origin": FIRST_PARTY_ORIGIN},
        )
        assert write_receiver.status_code == 503
        assert client.get("/v1/me/annotation-receiver").status_code == 503
    finally:
        generator.close()


def _session(
    database_uri: str,
    session_id: str,
    *,
    title: str,
    updated_at: int,
    grant_user: str,
    grant_level: int = 4,
) -> None:
    engine = get_or_create_engine(database_uri)
    with Session(engine) as session, session.begin():
        session.add(
            SqlConversation(
                id=session_id,
                root_conversation_id=session_id,
                title=title,
                created_at=updated_at,
                updated_at=updated_at,
            )
        )
        session.add(SqlConversationMetadata(id=session_id, kind=1))
        session.add(
            SqlSessionPermission(user_id=grant_user, conversation_id=session_id, level=grant_level)
        )


def test_receiver_cas_and_permission_or_archive_loss_do_not_revive(db_uri: str) -> None:
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("alice")
    receiver = AnnotationReceiverStore(db_uri)
    first = "a" * 32
    second = "b" * 32
    _session(db_uri, first, title="First", updated_at=10, grant_user="alice")
    _session(db_uri, second, title="Second", updated_at=20, grant_user="alice")

    assert receiver.read("alice").revision == 0
    assert receiver.set("alice", first, 0).revision == 1
    with pytest.raises(ReceiverConflict):
        receiver.set("alice", second, 0)
    assert receiver.set("alice", second, 1).session_id == second

    permissions.revoke("alice", second)
    permissions.grant("alice", second, 4)
    state = receiver.read("alice")
    assert (state.session_id, state.revision) == (None, 3)

    receiver.set("alice", first, state.revision)
    with Session(get_or_create_engine(db_uri)) as session, session.begin():
        session.execute(
            update(SqlConversation)
            .where(SqlConversation.id == first)
            .values(archived=True, archive_revision=1)
        )
    with Session(get_or_create_engine(db_uri)) as session, session.begin():
        session.execute(
            update(SqlConversation).where(SqlConversation.id == first).values(archived=False)
        )
    state = receiver.read("alice")
    assert (state.session_id, state.revision) == (None, 5)


def test_receiver_write_rejects_generation_rotated_after_authentication(db_uri: str) -> None:
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("alice")
    receiver = AnnotationReceiverStore(db_uri)
    session_id = "a" * 32
    _session(db_uri, session_id, title="Review", updated_at=10, grant_user="alice")
    with Session(get_or_create_engine(db_uri)) as session, session.begin():
        session.execute(
            update(SqlUser).where(SqlUser.id == "alice").values(account_generation="G1")
        )

    with account_authority_scope("alice", "G1"):
        with Session(get_or_create_engine(db_uri)) as session, session.begin():
            session.execute(
                update(SqlUser).where(SqlUser.id == "alice").values(account_generation="G2")
            )
        with pytest.raises(OmnigentError):
            receiver.set("alice", session_id, 0)

    assert receiver.read("alice").revision == 0
    with account_authority_scope("alice", "G2"):
        assert receiver.set("alice", session_id, 0).session_id == session_id


def test_picker_excludes_admin_only_sessions_and_pages_explicit_edits(db_uri: str) -> None:
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("admin", is_admin=True)
    permissions.ensure_user("owner")
    receiver = AnnotationReceiverStore(db_uri)
    older = "a" * 32
    newer = "b" * 32
    foreign = "c" * 32
    _session(db_uri, older, title="Project older", updated_at=10, grant_user="admin")
    _session(
        db_uri, newer, title="Project newer", updated_at=20, grant_user="admin", grant_level=2
    )
    _session(db_uri, foreign, title="Project foreign", updated_at=30, grant_user="owner")
    receiver.set("admin", older, 0)

    first_page = receiver.list_targets("admin", query="Project", limit=1)
    assert [row["id"] for row in first_page["sessions"]] == [newer]
    assert first_page["receiving_session_id"] == older
    assert first_page["receiving_session"]["id"] == older
    second_page = receiver.list_targets(
        "admin", query="Project", limit=1, cursor=first_page["next_cursor"]
    )
    assert [row["id"] for row in second_page["sessions"]] == [older]
    assert second_page["next_cursor"] is None


def test_browser_picker_and_first_party_receiver_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_CHROME_ANNOTATIONS_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_CHROME_EXTENSION_ORIGIN", CHROME_ORIGIN)
    generator = _build_accounts_app(tmp_path, monkeypatch)
    client = next(generator)
    try:
        database_uri = f"sqlite:///{tmp_path}/test.db"
        session_id = "d" * 32
        _session(
            database_uri,
            session_id,
            title="Review",
            updated_at=int(time.time()),
            grant_user="admin",
        )
        _login_admin(client)
        info = client.get("/v1/info")
        assert info.json()["browser_annotations_available"] is True
        assert info.json()["connected_apps_available"] is True
        state = client.get("/v1/me/annotation-receiver", params={"session_id": session_id})
        assert state.status_code == 200
        assert state.json()["target_editable"] is True
        assert (
            client.put(
                "/v1/me/annotation-receiver", json={"session_id": session_id, "revision": 0}
            ).status_code
            == 403
        )
        armed = client.put(
            "/v1/me/annotation-receiver",
            json={"session_id": session_id, "revision": 0},
            headers={"Origin": FIRST_PARTY_ORIGIN},
        )
        assert armed.status_code == 200, armed.text
        assert armed.json()["revision"] == 1
        assert (
            client.put(
                "/v1/me/annotation-receiver",
                json={"session_id": None, "revision": 0},
                headers={"Origin": FIRST_PARTY_ORIGIN},
            ).status_code
            == 409
        )

        origin = {"Origin": CHROME_ORIGIN}
        requested = client.post(
            "/oauth/annotations/authorize", json={"label": "Browser"}, headers=origin
        )
        assert requested.status_code == 200
        code = requested.json()
        _login_admin(client)
        assert (
            client.get("/oauth/device", params={"user_code": code["user_code"]}).status_code == 200
        )
        approved = client.post(
            "/oauth/device/approve",
            data={"user_code": code["user_code"]},
            headers={"Origin": FIRST_PARTY_ORIGIN},
        )
        assert approved.status_code == 200
        assert "Connected" in approved.text
        issued = client.post(
            "/oauth/annotations/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": code["device_code"],
            },
            headers=origin,
        )
        assert issued.status_code == 200
        bearer = {**origin, "Authorization": f"Bearer {issued.json()['access_token']}"}
        browser_cookies = dict(client.cookies)
        client.cookies.clear()
        dispatch = AsyncMock(side_effect=AssertionError("read-only request dispatched input"))
        monkeypatch.setattr(client.app.state.startup_inputs, "dispatch_annotation", dispatch)
        sessions_path = "/v1/browser-annotations/sessions"
        list_params = {"query": "Review", "limit": 1}
        listed = client.get(sessions_path, params=list_params, headers=bearer)
        assert listed.status_code == 200, listed.text
        assert listed.json()["sessions"][0]["id"] == session_id
        assert listed.json()["receiving_session_id"] == session_id
        assert "items" not in listed.json()["sessions"][0]
        listed_post = client.post(sessions_path, params=list_params, headers=bearer)
        assert listed_post.status_code == 200, listed_post.text
        assert listed_post.json() == listed.json()
        assert listed_post.headers["cache-control"] == "no-store"
        assert listed_post.headers["access-control-allow-origin"] == CHROME_ORIGIN
        preflight = client.options(
            sessions_path,
            headers={**origin, "Access-Control-Request-Method": "POST"},
        )
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-methods"] == "GET, POST, OPTIONS"
        for method in ("GET", "POST"):
            assert (
                client.request(
                    method, sessions_path, params={"query": "Absent"}, headers=bearer
                ).json()["sessions"]
                == []
            )
            assert (
                client.request(
                    method, sessions_path, params={"limit": 0}, headers=bearer
                ).status_code
                == 422
            )
            assert (
                client.request(
                    method, sessions_path, params={"cursor": "invalid"}, headers=bearer
                ).status_code
                == 400
            )
        dispatch.assert_not_called()
        client.cookies.update(browser_cookies)
        assert (
            client.put(
                "/v1/me/annotation-receiver",
                json={"session_id": None, "revision": 1},
                headers=bearer,
            ).status_code
            == 403
        )
        assert client.get("/v1/me/annotation-receiver").json()["session_id"] == session_id
        same_origin_bearer = {
            "Origin": FIRST_PARTY_ORIGIN,
            "Authorization": bearer["Authorization"],
        }
        assert (
            client.put(
                "/v1/me/annotation-receiver",
                json={"session_id": None, "revision": 1},
                headers=same_origin_bearer,
            ).status_code
            == 401
        )
        assert client.get("/v1/me/annotation-receiver").json()["session_id"] == session_id

        # Discovery may show an editable session whose runner is not currently live.
        # A deliberate Send to that exact target must fail before admission.
        offline_send = client.post(
            f"/v1/browser-annotations/sessions/{session_id}/batches",
            json={
                "version": 1,
                "batch_id": "e" * 32,
                "items": [
                    {
                        "capture_id": "f" * 32,
                        "captured_at": "2026-10-22T10:00:00Z",
                        "kind": "region",
                        "note": "Check the visible region",
                        "page": {"url": "https://example.test/review", "title": "Review"},
                        "target": {
                            "tag": "",
                            "selector": "",
                            "quote": "",
                            "prefix": "",
                            "suffix": "",
                            "aria_label": "",
                            "role": "",
                        },
                        "geometry": {
                            "rect": {"x": 1, "y": 2, "width": 20, "height": 10},
                            "viewport": {"width": 800, "height": 600},
                            "bitmap_scale": {"x": 1, "y": 1},
                        },
                    }
                ],
            },
            headers=bearer,
        )
        assert offline_send.status_code == 503, offline_send.text

        screenshot_fields = {"batch_id": "e" * 32, "capture_id": "f" * 32}
        bad_image = client.post(
            f"/v1/browser-annotations/sessions/{session_id}/screenshots",
            data={**screenshot_fields, "sha256": hashlib.sha256(b"invalid").hexdigest()},
            files={"file": ("capture.png", b"invalid", "image/png")},
            headers=bearer,
        )
        assert bad_image.status_code == 422
        assert bad_image.headers["access-control-allow-origin"] == CHROME_ORIGIN
        oversized = b"x" * (2 * 1024 * 1024 + 64 * 1024 + 1)
        too_large = client.post(
            f"/v1/browser-annotations/sessions/{session_id}/screenshots",
            data={**screenshot_fields, "sha256": hashlib.sha256(oversized).hexdigest()},
            files={"file": ("capture.png", oversized, "image/png")},
            headers=bearer,
        )
        assert too_large.status_code == 413
        assert too_large.headers["access-control-allow-origin"] == CHROME_ORIGIN
        huge_pixels = io.BytesIO()
        Image.new("1", (4001, 4000)).save(huge_pixels, format="PNG")
        pixel_bytes = huge_pixels.getvalue()
        too_many_pixels = client.post(
            f"/v1/browser-annotations/sessions/{session_id}/screenshots",
            data={**screenshot_fields, "sha256": hashlib.sha256(pixel_bytes).hexdigest()},
            files={"file": ("capture.png", pixel_bytes, "image/png")},
            headers=bearer,
        )
        assert too_many_pixels.status_code == 422
        assert too_many_pixels.headers["access-control-allow-origin"] == CHROME_ORIGIN
        image = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(image, format="PNG")
        image_bytes = image.getvalue()
        upload = client.post(
            f"/v1/browser-annotations/sessions/{session_id}/screenshots",
            data={**screenshot_fields, "sha256": hashlib.sha256(image_bytes).hexdigest()},
            files={"file": ("capture.png", image_bytes, "image/png")},
            headers=bearer,
        )
        assert upload.status_code == 200, upload.text
        assert upload.json()["sha256"] == hashlib.sha256(image_bytes).hexdigest()

        conversation_store = SqlAlchemyConversationStore(database_uri)
        grant = DeviceGrantStore(database_uri).get_by_id(issued.json()["grant_id"])
        assert grant is not None and grant.account_generation is not None
        batch = AnnotationBatch.model_validate_json(offline_send.request.content)
        batch.batch_id = "a" * 32
        batch.items[0].capture_id = "b" * 32
        receipt, created = conversation_store.admit_annotation_batch(
            "admin", grant.id, grant.account_generation, session_id, batch, target_live=True
        )
        assert created
        pending_ids = [item.id for item in conversation_store.list_pending_inputs(session_id)]
        assert pending_ids == [receipt["item_id"]]
        receiver_before = client.get("/v1/me/annotation-receiver").json()
        receipt_path = f"/v1/browser-annotations/batches/{batch.batch_id}"
        full_scope_bearer = {
            **origin,
            "Authorization": f"Bearer {browser_cookies['ap_session']}",
        }
        for method in ("GET", "POST"):
            for path in (sessions_path, receipt_path):
                assert client.request(method, path, headers=origin).status_code == 401
        client.cookies.clear()
        for method in ("GET", "POST"):
            read_list = client.request(method, sessions_path, params=list_params, headers=bearer)
            assert read_list.status_code == 200, read_list.text
            assert read_list.json()["sessions"][0]["id"] == session_id
            own_receipt = client.request(method, receipt_path, headers=bearer)
            assert own_receipt.status_code == 200, own_receipt.text
            assert own_receipt.json() == receipt
            assert own_receipt.headers["cache-control"] == "no-store"
            assert own_receipt.headers["access-control-allow-origin"] == CHROME_ORIGIN
            not_recorded = client.request(
                method, "/v1/browser-annotations/batches/" + "c" * 32, headers=bearer
            )
            assert not_recorded.status_code == 404
            assert not_recorded.json() == {"error": "not_recorded"}
            for path in (sessions_path, receipt_path):
                assert client.request(method, path, headers=full_scope_bearer).status_code == 401
                assert (
                    client.request(
                        method, path, headers={"Authorization": bearer["Authorization"]}
                    ).status_code
                    == 403
                )
                assert (
                    client.request(
                        method, path, headers={**bearer, "Origin": FIRST_PARTY_ORIGIN}
                    ).status_code
                    == 403
                )
        assert (
            conversation_store.get_annotation_receipt("admin", grant.id, batch.batch_id) == receipt
        )
        assert [
            item.id for item in conversation_store.list_pending_inputs(session_id)
        ] == pending_ids
        dispatch.assert_not_called()
        client.cookies.update(browser_cookies)
        assert client.get("/v1/me/annotation-receiver").json() == receiver_before
        assert (
            client.delete(
                f"/v1/me/connected-apps/{issued.json()['grant_id']}",
                headers={"Origin": FIRST_PARTY_ORIGIN},
            ).status_code
            == 200
        )
        after_revoke = client.post(
            f"/v1/browser-annotations/sessions/{session_id}/screenshots",
            data={**screenshot_fields, "sha256": hashlib.sha256(image_bytes).hexdigest()},
            files={"file": ("capture.png", image_bytes, "image/png")},
            headers=bearer,
        )
        assert after_revoke.status_code == 401
        assert after_revoke.headers["access-control-allow-origin"] == CHROME_ORIGIN
    finally:
        generator.close()
