"""Receiving-session persistence and the narrow browser session picker."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
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
from omnigent.server.annotation_receiver_store import AnnotationReceiverStore, ReceiverConflict
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.routes.device_auth import create_device_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.test_device_auth import _build_accounts_app, _login_admin

CHROME_ORIGIN = "chrome-extension://" + "a" * 32
FIRST_PARTY_ORIGIN = "http://localhost:8000"


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
        assert (
            client.get("/oauth/device", params={"user_code": code["user_code"]}).status_code == 200
        )
        approved = client.post(
            "/oauth/device/approve",
            data={"user_code": code["user_code"]},
            headers={"Origin": FIRST_PARTY_ORIGIN},
        )
        assert approved.status_code == 200
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
        listed = client.get("/v1/browser-annotations/sessions", headers=bearer)
        assert listed.status_code == 200, listed.text
        assert listed.json()["sessions"][0]["id"] == session_id
        assert listed.json()["receiving_session_id"] == session_id
        assert "items" not in listed.json()["sessions"][0]
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
    finally:
        generator.close()
