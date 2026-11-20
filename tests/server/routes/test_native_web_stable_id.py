"""A native mirror names the web send it delivered only when the pending-input link is exact."""

from typing import Any
from unittest.mock import Mock

import pytest

from omnigent.entities import ConversationItem, MessageData
from omnigent.runtime import pending_inputs
from omnigent.server.routes._sessions.orchestration import (
    _persist_external_conversation_item,
    _settle_lost_runner_input,
    _settle_undelivered_native_input,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_WEB_ID = "a" * 32
_OTHER_WEB_ID = "b" * 32
_FORGED_ID = "c" * 32


def _mirror(
    text: str, *, source_id: str | None = "codex-record-1", **item_data: Any
) -> SessionEventInput:
    """A forwarder's external item for a mirrored user message."""
    return SessionEventInput.model_validate(
        {
            "type": "external_conversation_item",
            "data": {
                **({"source_id": source_id} if source_id is not None else {}),
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                    **item_data,
                },
                "response_id": "resp_mirror",
            },
        }
    )


async def _persist_mirrors(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    queued: list[tuple[str, str | None]],
    mirrors: list[SessionEventInput],
) -> tuple[dict[str, tuple[ConversationItem, MessageData]], list[dict[str, Any]]]:
    """Queue web sends, persist each mirror, and return the user items by text and receipts."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native")
    for text, stable_id in queued:
        pending_inputs.record(conv.id, [{"type": "input_text", "text": text}], stable_id=stable_id)
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    try:
        for mirror in mirrors:
            await _persist_external_conversation_item(
                conv.id, conv, mirror, store, created_by=None
            )
    finally:
        for pending in pending_inputs.snapshot_for(conv.id):
            pending_inputs.resolve(conv.id, pending["pending_id"])
    users = {
        item.data.content[0]["text"]: (item, item.data)
        for item in SqlAlchemyConversationStore(db_uri).list_items(conv.id, type="message").data
        if isinstance(item.data, MessageData) and item.data.role == "user"
    }
    receipts = [
        call.args[1]["data"]
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
    ]
    return users, receipts


@pytest.mark.asyncio
async def test_text_matched_mirror_names_its_web_send(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    users, receipts = await _persist_mirrors(
        db_uri,
        monkeypatch,
        [("deploy the fix", _OTHER_WEB_ID), ("ship it", _WEB_ID)],
        [_mirror("ship it")],
    )

    item, data = users["ship it"]
    assert item.id != _WEB_ID
    assert data.web_stable_id == _WEB_ID
    assert item.to_api_dict()["web_stable_id"] == _WEB_ID
    [receipt] = [receipt for receipt in receipts if receipt["item_id"] == item.id]
    assert receipt["data"]["web_stable_id"] == _WEB_ID


@pytest.mark.parametrize(
    "queued,mirror_text,source_id",
    [
        ([("ship it", _WEB_ID)], "something else", "codex-record-1"),
        ([], "ship it", "codex-record-1"),
        ([("ship it", _WEB_ID), ("ship it", _OTHER_WEB_ID)], "ship it", "codex-record-1"),
        ([("ship it", None)], "ship it", "codex-record-1"),
        ([("ship it", _WEB_ID)], "ship it", None),
    ],
    ids=[
        "fifo-fallback",
        "typed-in-terminal",
        "shared-text",
        "send-without-stable-id",
        "committed-under-web-id",
    ],
)
@pytest.mark.asyncio
async def test_mirror_without_an_exact_link_names_no_web_send(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    queued: list[tuple[str, str | None]],
    mirror_text: str,
    source_id: str | None,
) -> None:
    users, receipts = await _persist_mirrors(
        db_uri, monkeypatch, queued, [_mirror(mirror_text, source_id=source_id)]
    )

    item, data = users[mirror_text]
    assert "web_stable_id" not in data.model_dump()
    assert "web_stable_id" not in item.to_api_dict()
    assert "web_stable_id" not in receipts[-1]["data"]


@pytest.mark.asyncio
async def test_mirror_of_a_send_a_positional_drain_may_have_settled_names_no_web_send(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The unmatched first mirror drains by position and marks the rest uncertain.
    users, receipts = await _persist_mirrors(
        db_uri,
        monkeypatch,
        [("first", _OTHER_WEB_ID), ("ship it", _WEB_ID)],
        [
            _mirror("reworded first", source_id="codex-record-1"),
            _mirror("ship it", source_id="codex-record-2"),
        ],
    )

    _, data = users["ship it"]
    assert "web_stable_id" not in data.model_dump()
    assert all("web_stable_id" not in receipt["data"] for receipt in receipts)


@pytest.mark.parametrize(
    "queued,expected",
    [([], None), ([("ship it", _WEB_ID)], _WEB_ID)],
    ids=["typed-in-terminal", "text-matched"],
)
@pytest.mark.asyncio
async def test_forwarder_cannot_set_web_stable_id(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    queued: list[tuple[str, str | None]],
    expected: str | None,
) -> None:
    users, _ = await _persist_mirrors(
        db_uri, monkeypatch, queued, [_mirror("ship it", web_stable_id=_FORGED_ID)]
    )

    _, data = users["ship it"]
    assert data.web_stable_id == expected


@pytest.mark.parametrize(
    ("overlapping_send", "interrupted", "fifo_before_stop", "mirror_text"),
    [
        pytest.param(False, False, False, "ship it", id="manual-same-text-empty-queue"),
        pytest.param(
            True, False, False, "[Attached file: /tmp/first.txt]\n\nship it", id="overlapping-send"
        ),
        pytest.param(True, True, False, "ship it", id="stop-then-same-text-claim"),
        pytest.param(True, True, True, "ship it", id="fifo-then-stop-then-same-text-claim"),
    ],
)
@pytest.mark.asyncio
async def test_claimed_uncertain_attempt_cannot_be_confirmed_by_text(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    overlapping_send: bool,
    interrupted: bool,
    fifo_before_stop: bool,
    mirror_text: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native")
    prompt = {"type": "input_text", "text": "ship it"}
    content = [prompt]
    if not interrupted:
        content.append({"type": "input_file", "file_id": "file_first", "filename": "first.txt"})
    pending_id = pending_inputs.record(conv.id, content, stable_id=_WEB_ID, runner_managed=True)
    attempt_id = pending_inputs.delivery_attributes_for(conv.id, pending_id)["delivery_attempt_id"]
    assert isinstance(attempt_id, str)
    assert pending_inputs.claim_runner_input(conv.id, pending_id, "original")
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)

    try:
        if fifo_before_stop:
            positional = pending_inputs.record(
                conv.id, [{"type": "input_text", "text": "queued before the terminal input"}]
            )
            await _persist_external_conversation_item(
                conv.id, conv, _mirror("unrelated terminal input"), store
            )
            assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [
                pending_id
            ]
            assert any(
                call.args[1]["type"] == "session.input.consumed"
                and call.args[1]["data"]["cleared_pending_id"] == positional
                for call in publish.call_args_list
            )
        if interrupted:
            pending_inputs.mark_interrupted(conv.id, [pending_id])
        assert await _settle_lost_runner_input(
            store,
            conv.id,
            pending_id,
            "native_input_delivery_uncertain",
            claim_outcome=True,
            expected_attempt_id=attempt_id,
        )
        assert not pending_inputs.snapshot_for(conv.id)
        assert pending_inputs.matches_unconfirmed_input_text(conv.id, "ship it")
        if overlapping_send:
            other_content = [prompt]
            if not interrupted:
                other_content.append(
                    {"type": "input_file", "file_id": "file_second", "filename": "second.txt"}
                )
            other_pending_id = pending_inputs.record(
                conv.id,
                other_content,
                stable_id=_OTHER_WEB_ID,
                runner_managed=interrupted,
            )
            if interrupted:
                assert pending_inputs.claim_runner_input(conv.id, other_pending_id, "successor")
        item_id, replay = await _persist_external_conversation_item(
            conv.id, conv, _mirror(mirror_text, source_id="independent-terminal-record"), store
        )
        assert item_id and item_id not in {_WEB_ID, _OTHER_WEB_ID} and not replay
        [error] = [item for item in store.list_items(conv.id).data if item.type == "error"]
        assert error.data.model_dump()["delivery_attempt_id"] == attempt_id
        assert error.data.model_dump()["input_stable_id"] == _WEB_ID
        mirror = store.get_item(conv.id, item_id)
        assert mirror is not None
        assert "web_stable_id" not in mirror.to_api_dict()
        live = [call.args[1] for call in publish.call_args_list]
        [mirror_consumed] = [
            frame["data"]
            for frame in live
            if frame["type"] == "session.input.consumed" and frame["data"]["item_id"] == item_id
        ]
        assert "web_stable_id" not in mirror_consumed["data"]
        assert any(
            frame["type"] == "session.input.consumed"
            and frame["data"]["item_id"] == _WEB_ID
            and frame["data"]["data"].get("delivery_confirmed") is False
            for frame in live
        )
        assert any(
            frame["type"] == "response.error"
            and frame["error"]["delivery_attempt_id"] == attempt_id
            for frame in live
        )
    finally:
        for pending in pending_inputs.snapshot_for(conv.id):
            pending_inputs.resolve(conv.id, pending["pending_id"])


@pytest.mark.parametrize(
    "settlement",
    ["runner-uncertainty", "forward-rejected", "legacy-failure", "fifo", "skipped", "confirmed"],
)
@pytest.mark.asyncio
async def test_settlement_remembers_text_unless_delivery_is_confirmed(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, settlement: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native")
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", Mock())
    pending_id = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "ship it"}],
        stable_id=None if settlement == "fifo" else _WEB_ID,
        runner_managed=settlement == "runner-uncertainty",
    )
    try:
        if settlement == "runner-uncertainty":
            assert pending_inputs.claim_runner_input(conv.id, pending_id, "original")
            assert await _settle_lost_runner_input(
                store, conv.id, pending_id, "native_input_delivery_uncertain", claim_outcome=True
            )
        elif settlement == "legacy-failure":
            assert await _settle_undelivered_native_input(store, conv.id, None, _WEB_ID)
        elif settlement == "forward-rejected":
            assert pending_inputs.resolve(conv.id, pending_id) is not None
        else:
            text = "ship it" if settlement == "confirmed" else "another input"
            if settlement == "skipped":
                pending_inputs.record(conv.id, [{"type": "input_text", "text": text}])
            item_id, replay = await _persist_external_conversation_item(
                conv.id, conv, _mirror(text), store
            )
            assert item_id and not replay

        assert not pending_inputs.snapshot_for(conv.id)
        assert pending_inputs.matches_unconfirmed_input_text(conv.id, "ship it") is (
            settlement != "confirmed"
        )
    finally:
        for pending in pending_inputs.snapshot_for(conv.id):
            pending_inputs.resolve(conv.id, pending["pending_id"])


@pytest.mark.parametrize("source_id", ["retry-record", None], ids=["tagged", "source-less"])
@pytest.mark.asyncio
async def test_resend_after_confirmed_failure_has_no_echo_alias(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, source_id: str | None
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native")
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    content = [{"type": "input_text", "text": "ship it"}]
    failed_id = pending_inputs.record(conv.id, content, stable_id=_WEB_ID, runner_managed=True)
    try:
        assert await _settle_lost_runner_input(
            store, conv.id, failed_id, "native_input_runner_lost", claim_outcome=False
        )
        assert pending_inputs.matches_unconfirmed_input_text(conv.id, "ship it")
        retry_id = pending_inputs.record(
            conv.id, content, stable_id=_OTHER_WEB_ID, runner_managed=True
        )
        assert pending_inputs.claim_runner_input(conv.id, retry_id, "retry")
        item_id, replay = await _persist_external_conversation_item(
            conv.id, conv, _mirror("ship it", source_id=source_id), store
        )
        assert item_id and item_id not in {_WEB_ID, _OTHER_WEB_ID} and not replay
        mirror = store.get_item(conv.id, item_id)
        assert mirror is not None
        assert "web_stable_id" not in mirror.to_api_dict()
        [consumed] = [
            call.args[1]["data"]
            for call in publish.call_args_list
            if call.args[1]["type"] == "session.input.consumed"
            and call.args[1]["data"]["item_id"] == item_id
        ]
        assert "web_stable_id" not in consumed["data"]
    finally:
        for pending in pending_inputs.snapshot_for(conv.id):
            pending_inputs.resolve(conv.id, pending["pending_id"])
