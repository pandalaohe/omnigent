"""Durable admission, staging, and saved-input dispatch for Chrome batches."""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlAnnotationCapture,
    SqlAnnotationGrantOwner,
    SqlConversation,
    SqlDeviceGrant,
    SqlFile,
    SqlSessionPermission,
    SqlUser,
)
from omnigent.db.enum_codecs import encode_device_grant_status
from omnigent.db.utils import get_or_create_engine
from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.annotation_batch import (
    INTRO,
    AnnotationBatch,
    AnnotationFailure,
    canonical_digest,
    render_annotation_batch,
)
from omnigent.server.attachment_archive_cleanup import AttachmentArchiveCleanup
from omnigent.server.host_registry import HostRegistry
from omnigent.server.schemas import SessionEventInput
from omnigent.server.startup_inputs import (
    ManagedStartupInputs,
    StartupInputContext,
    reconcile_accepted_startup_input,
)
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.test_browser_annotations import _session

SESSION_ID = "a" * 32
GRANT_ID = "browser-grant"
USER_ID = "alice"
GENERATION = "G1"


def _batch(
    batch_id: str = "b" * 32, *, screenshot: dict[str, str] | None = None
) -> AnnotationBatch:
    return AnnotationBatch.model_validate(
        {
            "version": 1,
            "batch_id": batch_id,
            "items": [
                {
                    "capture_id": "c" * 32,
                    "captured_at": "2026-10-22T10:00:00Z",
                    "kind": "element",
                    "note": "Please inspect this control",
                    "page": {
                        "url": "https://example.test/review?private=ignored#fragment",
                        "title": "Review",
                    },
                    "target": {
                        "tag": "button",
                        "selector": "#submit",
                        "quote": "Send",
                        "prefix": "",
                        "suffix": "",
                        "aria_label": "Send",
                        "role": "button",
                    },
                    "geometry": {
                        "rect": {"x": 10, "y": 20, "width": 100, "height": 40},
                        "viewport": {"width": 800, "height": 600},
                        "bitmap_scale": {"x": 1.5, "y": 1.5},
                    },
                    **({"screenshot": screenshot} if screenshot else {}),
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("with_screenshot", "bitmap_scale", "valid"),
    [
        (False, None, True),
        (True, None, False),
        (False, {"x": 1.5, "y": 1.5}, True),
        (True, {"x": 1.5, "y": 1.5}, True),
    ],
)
def test_bitmap_scale_is_required_but_nullable_only_without_screenshot(
    with_screenshot: bool, bitmap_scale: dict[str, float] | None, valid: bool
) -> None:
    screenshot = {"file_id": "d" * 32, "sha256": "f" * 64} if with_screenshot else None
    payload = _batch(screenshot=screenshot).model_dump(mode="json")
    payload["items"][0]["geometry"]["bitmap_scale"] = bitmap_scale
    if valid:
        assert AnnotationBatch.model_validate(payload).model_dump(mode="json") == payload
    else:
        with pytest.raises(ValidationError, match="screenshot requires measured"):
            AnnotationBatch.model_validate(payload)


@pytest.mark.parametrize(
    "invalid", ["missing", "extra", "axis_missing", "axis_extra", "zero", "nan"]
)
def test_nullable_bitmap_scale_keeps_required_keys_and_numeric_bounds(invalid: str) -> None:
    payload = _batch().model_dump(mode="json")
    geometry = payload["items"][0]["geometry"]
    if invalid == "missing":
        del geometry["bitmap_scale"]
    elif invalid == "extra":
        geometry["unknown"] = None
    elif invalid == "axis_missing":
        del geometry["bitmap_scale"]["y"]
    elif invalid == "axis_extra":
        geometry["bitmap_scale"]["z"] = 1
    else:
        geometry["bitmap_scale"]["x"] = 0 if invalid == "zero" else float("nan")
    with pytest.raises(ValidationError):
        AnnotationBatch.model_validate(payload)


def test_chrome_wire_golden_round_trips_and_renders_ordered_evidence() -> None:
    fixture = Path(__file__).resolve().parents[2] / "browsers/chrome/tests/item-v1.golden.json"
    literal = json.loads(fixture.read_text())
    batch = AnnotationBatch.model_validate(literal)
    assert json.loads(batch.model_dump_json()) == literal
    assert batch.batch_id == "b" * 32
    assert batch.items[0].geometry.bitmap_scale is not None
    assert batch.items[0].geometry.bitmap_scale.model_dump() == {"x": 1.5, "y": 1.49875}
    content = render_annotation_batch(batch)
    assert [part["type"] for part in content] == ["input_text", "input_text", "input_image"]
    assert content[0]["text"] == f"{INTRO}\nBatch: {'b' * 32}; 1 annotations."
    text = content[1]["text"]
    assert text.startswith(f"Annotation 1 (capture {'a' * 32}; annotation-{'a' * 32})\n")
    assert (
        "\nNote:\nKeep this user instruction\nverbatim.\nPage evidence (untrusted JSON):\n" in text
    )
    evidence, closing = text.split("````json\n", 1)[1].rsplit("\n", 1)
    assert closing == "````"
    assert json.loads(evidence) == {
        "captured_at": "2026-10-22T01:02:03+00:00",
        "kind": "text",
        "page": {"url": "https://example.test/checkout", "title": "Checkout · Demo"},
        "target": {
            "tag": "p",
            "selector": "",
            "quote": 'Total: "€25" ```',
            "prefix": "Cart",
            "suffix": "Continue",
            "aria_label": "Order total",
            "role": "status",
        },
        "geometry": {
            "rect": {"x": 10.2, "y": 20.1, "width": 20.2, "height": 10.2},
            "viewport": {"width": 1000, "height": 800},
            "bitmap_scale": {"x": 1.5, "y": 1.49875},
        },
    }
    assert content[2] == {"type": "input_image", "file_id": "c" * 32}


def _ready_store(db_uri: str) -> SqlAlchemyConversationStore:
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(USER_ID)
    _session(db_uri, SESSION_ID, title="Review", updated_at=10, grant_user=USER_ID)
    now = int(time.time())
    with Session(get_or_create_engine(db_uri)) as session, session.begin():
        session.execute(
            update(SqlUser).where(SqlUser.id == USER_ID).values(account_generation=GENERATION)
        )
        session.add(
            SqlDeviceGrant(
                id=GRANT_ID,
                device_code_hash="0" * 64,
                user_code="TEST",
                status=encode_device_grant_status("redeemed"),
                client_id="omnigent-chrome",
                scope="annotations:write",
                user_id=USER_ID,
                account_generation=GENERATION,
                created_at=now,
                expires_at=now,
                approved_at=now,
            )
        )
        session.add(
            SqlAnnotationGrantOwner(
                grant_id=GRANT_ID,
                user_id=USER_ID,
                account_generation=GENERATION,
                approved_at=now,
            )
        )
    return SqlAlchemyConversationStore(db_uri)


def test_annotation_admission_is_one_pending_message_and_replay_does_not_duplicate(
    db_uri: str,
) -> None:
    store = _ready_store(db_uri)
    batch = _batch()
    receipt, created = store.admit_annotation_batch(
        USER_ID, GRANT_ID, GENERATION, SESSION_ID, batch, target_live=True
    )
    assert created and receipt["dispatch_phase"] == "ready"
    pending = store.list_pending_inputs(SESSION_ID)
    assert len(pending) == 1
    assert pending[0].id == receipt["item_id"]
    assert pending[0].data.content[0]["text"].startswith(INTRO)
    assert "private=ignored" not in str(pending[0].data.content)
    same, created = store.admit_annotation_batch(
        USER_ID, GRANT_ID, GENERATION, SESSION_ID, batch, target_live=False
    )
    assert not created and same["item_id"] == receipt["item_id"]
    assert len(store.list_pending_inputs(SESSION_ID)) == 1
    changed = batch.model_copy(deep=True)
    changed.items[0].note = "Different instruction"
    with pytest.raises(AnnotationFailure, match="idempotency_conflict"):
        store.admit_annotation_batch(
            USER_ID, GRANT_ID, GENERATION, SESSION_ID, changed, target_live=True
        )
    assert canonical_digest(SESSION_ID, batch) != canonical_digest(SESSION_ID, changed)


def test_annotation_admission_rolls_back_receipt_and_capture_with_input(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _ready_store(db_uri)
    original = store._insert_annotation_pending

    def fail_after_checks(*args: object) -> None:
        raise RuntimeError("injected pending insert failure")

    monkeypatch.setattr(store, "_insert_annotation_pending", fail_after_checks)
    with pytest.raises(RuntimeError, match="injected pending insert failure"):
        store.admit_annotation_batch(
            USER_ID, GRANT_ID, GENERATION, SESSION_ID, _batch(), target_live=True
        )
    assert store.get_annotation_receipt(USER_ID, GRANT_ID, "b" * 32) is None
    assert store.list_pending_inputs(SESSION_ID) == []
    with Session(get_or_create_engine(db_uri)) as session:
        assert session.scalar(select(SqlAnnotationCapture.capture_id)) is None
    monkeypatch.setattr(store, "_insert_annotation_pending", original)
    assert store.admit_annotation_batch(
        USER_ID, GRANT_ID, GENERATION, SESSION_ID, _batch(), target_live=True
    )[1]


def test_expired_screenshot_can_be_restaged_with_same_message_identity(db_uri: str) -> None:
    store = _ready_store(db_uri)
    first_file = "d" * 32
    second_file = "e" * 32
    digest = "f" * 64
    store.stage_annotation_screenshot(
        USER_ID,
        GRANT_ID,
        GENERATION,
        SESSION_ID,
        "b" * 32,
        "c" * 32,
        digest,
        first_file,
        256,
        "image/png",
    )
    original = _batch(screenshot={"file_id": first_file, "sha256": digest})
    with Session(get_or_create_engine(db_uri)) as session, session.begin():
        session.execute(update(SqlAnnotationCapture).values(expires_at=int(time.time()) - 1))
    with pytest.raises(AnnotationFailure) as error:
        store.admit_annotation_batch(
            USER_ID, GRANT_ID, GENERATION, SESSION_ID, original, target_live=True
        )
    assert (error.value.status, error.value.capture_ids) == (410, ("c" * 32,))
    assert store.get_annotation_receipt(USER_ID, GRANT_ID, "b" * 32) is None
    staged, retired = store.stage_annotation_screenshot(
        USER_ID,
        GRANT_ID,
        GENERATION,
        SESSION_ID,
        "b" * 32,
        "c" * 32,
        digest,
        second_file,
        256,
        "image/png",
    )
    assert staged["file_id"] == second_file and retired == first_file
    restaged = _batch(screenshot={"file_id": second_file, "sha256": digest})
    assert canonical_digest(SESSION_ID, original) == canonical_digest(SESSION_ID, restaged)
    assert store.admit_annotation_batch(
        USER_ID, GRANT_ID, GENERATION, SESSION_ID, restaged, target_live=True
    )[1]


@pytest.mark.asyncio
async def test_saved_annotation_dispatch_uses_pending_id_and_confirms_consumed_input(
    db_uri: str,
) -> None:
    store = _ready_store(db_uri)
    batch = _batch()
    receipt, _ = store.admit_annotation_batch(
        USER_ID, GRANT_ID, GENERATION, SESSION_ID, batch, target_live=True
    )
    service = ManagedStartupInputs(store, HostRegistry())
    calls: list[str] = []

    async def dispatch(
        context: StartupInputContext, session_id: str, event: SessionEventInput
    ) -> dict[str, bool | str | None]:
        assert (
            store.get_annotation_receipt(USER_ID, GRANT_ID, batch.batch_id)["dispatch_phase"]
            == "dispatching"
        )
        assert await context.claim(store, session_id)
        calls.append(context.input_id)
        store.append(
            session_id,
            [
                NewConversationItem(
                    type="message",
                    response_id="turn_accepted",
                    stable_id=context.input_id,
                    replaces_pending_id=context.input_id,
                    annotation_delivery_confirmed=True,
                    data=MessageData(role="user", content=event.data["content"]),
                    created_by=USER_ID,
                )
            ],
        )
        return {"item_id": context.input_id}

    service.dispatch = dispatch
    await service.dispatch_annotation(
        SimpleNamespace(state=SimpleNamespace()),
        SESSION_ID,
        str(receipt["item_id"]),
        USER_ID,
        GRANT_ID,
        GENERATION,
        batch.batch_id,
        target_live=lambda _: True,
    )
    assert calls == [receipt["item_id"]]
    assert (
        store.get_annotation_receipt(USER_ID, GRANT_ID, batch.batch_id)["dispatch_phase"]
        == "confirmed"
    )
    await service.dispatch_annotation(
        SimpleNamespace(state=SimpleNamespace()),
        SESSION_ID,
        str(receipt["item_id"]),
        USER_ID,
        GRANT_ID,
        GENERATION,
        batch.batch_id,
        target_live=lambda _: True,
        explicit_retry=True,
    )
    assert calls == [receipt["item_id"]]


def test_pending_replacement_requires_annotation_delivery_confirmation(db_uri: str) -> None:
    store = _ready_store(db_uri)
    batch = _batch()
    receipt, _ = store.admit_annotation_batch(
        USER_ID, GRANT_ID, GENERATION, SESSION_ID, batch, target_live=True
    )
    assert (
        store.begin_annotation_dispatch(
            USER_ID, GRANT_ID, GENERATION, batch.batch_id, target_live=True
        )
        is not None
    )
    pending = store.get_item(SESSION_ID, str(receipt["item_id"]))
    assert pending is not None
    store.append(
        SESSION_ID,
        [
            NewConversationItem(
                type="message",
                response_id="resp_not_received",
                stable_id=pending.id,
                replaces_pending_id=pending.id,
                data=pending.data,
                created_by=USER_ID,
            )
        ],
    )
    after = store.get_annotation_receipt(USER_ID, GRANT_ID, batch.batch_id)
    assert after is not None and after["dispatch_phase"] == "dispatching"


@pytest.mark.asyncio
async def test_annotation_dispatch_cas_separates_known_unsent_from_unknown_forward(
    db_uri: str,
) -> None:
    store = _ready_store(db_uri)
    batch = _batch()
    receipt, _ = store.admit_annotation_batch(
        USER_ID, GRANT_ID, GENERATION, SESSION_ID, batch, target_live=True
    )
    service = ManagedStartupInputs(store, HostRegistry())
    item_id = str(receipt["item_id"])
    forwarded: list[str] = []

    async def forward_then_fail(*_: object, **__: object) -> str:
        assert (
            store.get_annotation_receipt(USER_ID, GRANT_ID, batch.batch_id)["dispatch_phase"]
            == "dispatching"
        )
        forwarded.append(item_id)
        raise ConnectionError("lost runner acknowledgement")

    # No callback exists yet: the CAS is undone to ready and requires user retry.
    await service.dispatch_annotation(
        SimpleNamespace(state=SimpleNamespace()),
        SESSION_ID,
        item_id,
        USER_ID,
        GRANT_ID,
        GENERATION,
        batch.batch_id,
        target_live=lambda _: True,
    )
    deferred = store.get_annotation_receipt(USER_ID, GRANT_ID, batch.batch_id)
    assert deferred is not None
    assert (deferred["dispatch_phase"], deferred["retry_required"]) == ("ready", True)
    service._deliver = forward_then_fail  # type: ignore[method-assign]
    with pytest.raises(ConnectionError, match="lost runner acknowledgement"):
        await service.dispatch_annotation(
            SimpleNamespace(state=SimpleNamespace()),
            SESSION_ID,
            item_id,
            USER_ID,
            GRANT_ID,
            GENERATION,
            batch.batch_id,
            target_live=lambda _: True,
            explicit_retry=True,
        )
    assert forwarded == [item_id]
    assert (
        store.get_annotation_receipt(USER_ID, GRANT_ID, batch.batch_id)["dispatch_phase"]
        == "dispatching"
    )
    store.mark_annotation_uncertain(SESSION_ID, item_id)
    await service.dispatch_annotation(
        SimpleNamespace(state=SimpleNamespace()),
        SESSION_ID,
        item_id,
        USER_ID,
        GRANT_ID,
        GENERATION,
        batch.batch_id,
        target_live=lambda _: True,
        explicit_retry=True,
    )
    assert forwarded == [item_id]
    assert (
        store.get_annotation_receipt(USER_ID, GRANT_ID, batch.batch_id)["dispatch_phase"]
        == "uncertain"
    )


@pytest.mark.asyncio
async def test_generic_startup_reconciliation_does_not_consume_annotation(
    db_uri: str,
) -> None:
    store = _ready_store(db_uri)
    batch = _batch()
    receipt, _ = store.admit_annotation_batch(
        USER_ID, GRANT_ID, GENERATION, SESSION_ID, batch, target_live=True
    )
    await reconcile_accepted_startup_input(
        store,
        SimpleNamespace(
            input_accepted=True,
            proof_source="codex",
            input_id=receipt["item_id"],
            attempt_id="accepted",
            session_id=SESSION_ID,
        ),  # type: ignore[arg-type]
    )
    assert [item.id for item in store.list_pending_inputs(SESSION_ID)] == [receipt["item_id"]]
    assert (
        store.get_annotation_receipt(USER_ID, GRANT_ID, batch.batch_id)["dispatch_phase"]
        == "ready"
    )


def test_old_grant_lookup_is_owner_scoped_and_absence_is_definitive_only_after_close(
    db_uri: str,
) -> None:
    store = _ready_store(db_uri)
    assert (
        store.get_old_annotation_receipt(USER_ID, GENERATION, GRANT_ID, "b" * 32)["state"]
        == "unknown_record"
    )
    with pytest.raises(AnnotationFailure):
        store.get_old_annotation_receipt("other", GENERATION, GRANT_ID, "b" * 32)
    with Session(get_or_create_engine(db_uri)) as session, session.begin():
        session.execute(
            update(SqlDeviceGrant)
            .where(SqlDeviceGrant.id == GRANT_ID)
            .values(status=encode_device_grant_status("revoked"))
        )
    assert (
        store.get_old_annotation_receipt(USER_ID, GENERATION, GRANT_ID, "b" * 32)["state"]
        == "not_admitted"
    )


def test_periodic_staging_cleanup_rechecks_claim_and_preserves_committed_file(
    db_uri: str,
) -> None:
    store = _ready_store(db_uri)
    file_id = "d" * 32
    digest = "f" * 64
    store.stage_annotation_screenshot(
        USER_ID,
        GRANT_ID,
        GENERATION,
        SESSION_ID,
        "b" * 32,
        "c" * 32,
        digest,
        file_id,
        256,
        "image/png",
    )
    with Session(get_or_create_engine(db_uri)) as session, session.begin():
        session.execute(update(SqlAnnotationCapture).values(expires_at=int(time.time()) - 1))
    [candidate] = store.list_expired_annotation_staging()
    deleted: list[str] = []

    def fail_delete(_: str) -> None:
        raise OSError("blob deletion failed")

    with pytest.raises(OSError, match="blob deletion failed"):
        store.retire_expired_annotation_staging(candidate, fail_delete)
    with Session(get_or_create_engine(db_uri)) as session:
        assert (
            session.get(SqlAnnotationCapture, (0, USER_ID, GRANT_ID, "c" * 32)).file_id == file_id
        )
    assert store.retire_expired_annotation_staging(candidate, deleted.append)
    assert deleted == [file_id]
    assert not store.retire_expired_annotation_staging(candidate, deleted.append)
    with Session(get_or_create_engine(db_uri)) as session:
        assert session.get(SqlFile, (0, file_id)) is None

    replacement = "e" * 32
    store.stage_annotation_screenshot(
        USER_ID,
        GRANT_ID,
        GENERATION,
        SESSION_ID,
        "b" * 32,
        "c" * 32,
        digest,
        replacement,
        256,
        "image/png",
    )
    store.admit_annotation_batch(
        USER_ID,
        GRANT_ID,
        GENERATION,
        SESSION_ID,
        _batch(screenshot={"file_id": replacement, "sha256": digest}),
        target_live=True,
    )
    with Session(get_or_create_engine(db_uri)) as session, session.begin():
        session.execute(update(SqlAnnotationCapture).values(expires_at=int(time.time()) - 1))
    assert store.list_expired_annotation_staging() == []
    assert not store.retire_expired_annotation_staging(
        (0, USER_ID, GRANT_ID, "c" * 32, replacement), deleted.append
    )
    with Session(get_or_create_engine(db_uri)) as session:
        assert session.get(SqlFile, (0, replacement)) is not None


def test_reserved_screenshot_survives_blob_write_crash_and_retries_without_orphan(
    db_uri: str,
) -> None:
    store = _ready_store(db_uri)
    content = b"validated image bytes"
    digest = hashlib.sha256(content).hexdigest()
    blobs: dict[str, bytes] = {}
    reservation, needed = store.reserve_annotation_screenshot(
        USER_ID,
        GRANT_ID,
        GENERATION,
        SESSION_ID,
        "b" * 32,
        "c" * 32,
        digest,
        blobs.pop,
    )
    assert needed
    file_id = str(reservation["file_id"])
    with pytest.raises(AnnotationFailure) as error:
        store.admit_annotation_batch(
            USER_ID,
            GRANT_ID,
            GENERATION,
            SESSION_ID,
            _batch(screenshot={"file_id": file_id, "sha256": digest}),
            target_live=True,
        )
    assert error.value.status == 410
    with Session(get_or_create_engine(db_uri)) as session:
        assert session.get(SqlFile, (0, file_id)) is None

    def crash_after_put(key: str, value: bytes) -> None:
        blobs[key] = value
        raise RuntimeError("crash after blob put")

    with pytest.raises(RuntimeError, match="crash after blob put"):
        store.complete_annotation_screenshot(
            USER_ID,
            GRANT_ID,
            GENERATION,
            SESSION_ID,
            "b" * 32,
            "c" * 32,
            digest,
            file_id,
            content,
            "image/png",
            crash_after_put,
        )
    with Session(get_or_create_engine(db_uri)) as session:
        capture = session.get(SqlAnnotationCapture, (0, USER_ID, GRANT_ID, "c" * 32))
        assert capture is not None and capture.upload_state == "reserved"
        assert session.get(SqlFile, (0, file_id)) is None
    same, needed = store.reserve_annotation_screenshot(
        USER_ID,
        GRANT_ID,
        GENERATION,
        SESSION_ID,
        "b" * 32,
        "c" * 32,
        digest,
        blobs.pop,
    )
    assert needed and same["file_id"] == file_id
    ready = store.complete_annotation_screenshot(
        USER_ID,
        GRANT_ID,
        GENERATION,
        SESSION_ID,
        "b" * 32,
        "c" * 32,
        digest,
        file_id,
        content,
        "image/png",
        blobs.__setitem__,
    )
    assert ready["file_id"] == file_id and blobs[file_id] == content
    assert store.admit_annotation_batch(
        USER_ID,
        GRANT_ID,
        GENERATION,
        SESSION_ID,
        _batch(screenshot={"file_id": file_id, "sha256": digest}),
        target_live=True,
    )[1]


@pytest.mark.asyncio
async def test_existing_file_cleanup_tick_retires_expired_annotation_staging(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _ready_store(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    file_id = "d" * 32
    artifacts.put(file_id, b"staged bytes")
    store.stage_annotation_screenshot(
        USER_ID,
        GRANT_ID,
        GENERATION,
        SESSION_ID,
        "b" * 32,
        "c" * 32,
        "f" * 64,
        file_id,
        12,
        "image/png",
    )
    with Session(get_or_create_engine(db_uri)) as session, session.begin():
        session.execute(update(SqlAnnotationCapture).values(expires_at=int(time.time()) - 1))
    monkeypatch.setattr(
        "omnigent.server.attachment_archive_cleanup.attachment_archive_cleanup_days",
        lambda: 0,
    )
    cleanup = AttachmentArchiveCleanup(
        conversation_store=store,
        file_store=SqlAlchemyFileStore(db_uri),
        artifact_store=artifacts,
        annotation_staging_cleanup=True,
    )
    monkeypatch.setattr(cleanup, "_write_summary", lambda _: None)
    await cleanup.sweep_once()
    assert not artifacts.exists(file_id)
    assert store.list_expired_annotation_staging() == []


@pytest.mark.parametrize(
    "lost, status",
    [("grant", 401), ("permission", 403), ("archive", 409)],
)
def test_annotation_write_rechecks_authority_after_authentication(
    db_uri: str, lost: str, status: int
) -> None:
    store = _ready_store(db_uri)
    with Session(get_or_create_engine(db_uri)) as session, session.begin():
        if lost == "grant":
            session.execute(
                update(SqlDeviceGrant)
                .where(SqlDeviceGrant.id == GRANT_ID)
                .values(status=encode_device_grant_status("revoked"))
            )
        elif lost == "permission":
            permission = session.get(SqlSessionPermission, (0, USER_ID, SESSION_ID))
            assert permission is not None
            session.delete(permission)
        else:
            session.execute(
                update(SqlConversation)
                .where(SqlConversation.id == SESSION_ID)
                .values(archived=True)
            )
    with pytest.raises(AnnotationFailure) as error:
        store.admit_annotation_batch(
            USER_ID, GRANT_ID, GENERATION, SESSION_ID, _batch(), target_live=True
        )
    assert error.value.status == status
    assert store.get_annotation_receipt(USER_ID, GRANT_ID, "b" * 32) is None
    assert store.list_pending_inputs(SESSION_ID) == []
    with pytest.raises(AnnotationFailure) as upload_error:
        store.stage_annotation_screenshot(
            USER_ID,
            GRANT_ID,
            GENERATION,
            SESSION_ID,
            "b" * 32,
            "c" * 32,
            "f" * 64,
            "d" * 32,
            256,
            "image/png",
        )
    assert upload_error.value.status == status
    with Session(get_or_create_engine(db_uri)) as session:
        assert session.get(SqlFile, (0, "d" * 32)) is None


def test_annotation_write_rejects_rotated_account_generation(db_uri: str) -> None:
    store = _ready_store(db_uri)
    with Session(get_or_create_engine(db_uri)) as session, session.begin():
        session.execute(
            update(SqlUser).where(SqlUser.id == USER_ID).values(account_generation="G2")
        )
    with pytest.raises(OmnigentError) as error:
        store.admit_annotation_batch(
            USER_ID, GRANT_ID, GENERATION, SESSION_ID, _batch(), target_live=True
        )
    assert error.value.code == ErrorCode.UNAUTHORIZED
    assert store.get_annotation_receipt(USER_ID, GRANT_ID, "b" * 32) is None
    assert store.list_pending_inputs(SESSION_ID) == []


@pytest.mark.parametrize("mutation, expected", [("revoke", 401), ("archive", 409)])
def test_inflight_authority_mutation_linearizes_before_annotation_admission(
    db_uri: str, mutation: str, expected: int
) -> None:
    store = _ready_store(db_uri)
    writer_started = Event()
    admission_started = Event()
    release_writer = Event()
    engine = get_or_create_engine(db_uri)

    def change_authority() -> None:
        with Session(engine) as session, session.begin():
            if mutation == "revoke":
                session.execute(
                    update(SqlDeviceGrant)
                    .where(SqlDeviceGrant.id == GRANT_ID)
                    .values(status=encode_device_grant_status("revoked"))
                )
            else:
                session.execute(
                    update(SqlConversation)
                    .where(SqlConversation.id == SESSION_ID)
                    .values(archived=True)
                )
            writer_started.set()
            assert release_writer.wait(timeout=5)

    def admit() -> None:
        admission_started.set()
        store.admit_annotation_batch(
            USER_ID, GRANT_ID, GENERATION, SESSION_ID, _batch(), target_live=True
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(change_authority)
        assert writer_started.wait(timeout=5)
        attempted = executor.submit(admit)
        try:
            assert admission_started.wait(timeout=5)
            time.sleep(0.05)
        finally:
            release_writer.set()
        with pytest.raises(AnnotationFailure) as error:
            attempted.result(timeout=5)
        writer.result(timeout=5)
    assert error.value.status == expected
    assert store.get_annotation_receipt(USER_ID, GRANT_ID, "b" * 32) is None
