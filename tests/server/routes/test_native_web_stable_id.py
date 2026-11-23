"""A native mirror names the web send it delivered only when the pending-input link is exact."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from omnigent._wrapper_labels import CLAUDE_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
from omnigent.entities import ConversationItem, MessageData
from omnigent.harnesses.claude_native.bridge import (
    _claim_message_delivery,
    read_transcript_items_from_offset,
)
from omnigent.harnesses.claude_native.forwarder import _post_external_conversation_item
from omnigent.inner import claude_native_executor
from omnigent.inner.executor import ExecutorConfig, TurnComplete
from omnigent.native.input_diagnostics import input_delivery_scope
from omnigent.runner.app_support import _resolve_forwarded_message_content
from omnigent.runtime import pending_inputs
from omnigent.runtime.harnesses._executor_adapter import _normalize_message_content
from omnigent.server.routes._sessions.helpers import _publish_session_superseded
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


@pytest.mark.parametrize("attachment_kind", ["image", "file", "partial-failure"])
@pytest.mark.asyncio
async def test_claude_midturn_attachment_echo_consumes_at_its_transcript_position(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, attachment_kind: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(
        title="Native", labels={WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE}
    )
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    before: dict[str, object] = {"type": "input_text", "text": "See this screenshot."}
    after: dict[str, object] = {"type": "input_text", "text": "Check the red dot."}
    monkeypatch.setattr(
        "omnigent.inner.native_attachments.attachments_root", lambda: tmp_path / "attachments"
    )
    monkeypatch.setattr(
        "omnigent.inner.native_attachments.attachment_cache_dir",
        lambda _bridge_dir: tmp_path / "attachments" / ("c" * 32),
    )
    filename, mime = (
        ("status.png", "image/png")
        if attachment_kind != "file"
        else ("status.zip", "application/zip")
    )
    upload: dict[str, object] = {
        "type": "input_file" if attachment_kind == "file" else "input_image",
        "file_id": "file_upload",
        "filename": filename,
    }
    other_uploads: list[dict[str, object]] = (
        [{"type": "input_image", "file_id": "file_missing", "filename": "missing.png"}]
        if attachment_kind == "partial-failure"
        else []
    )
    content = [before, upload, *other_uploads, after]
    older = pending_inputs.record(conv.id, [{"type": "input_text", "text": "Unclaimed"}])
    # Same authored text and attachment position, but a different attempt and upload.
    unread = pending_inputs.record(
        conv.id,
        [before, {**upload, "file_id": "file_unread"}, *other_uploads, after],
        runner_managed=True,
    )
    read = pending_inputs.record(conv.id, content, stable_id=_WEB_ID, runner_managed=True)
    assert pending_inputs.claim_runner_input(conv.id, unread, "claim_unread")
    assert pending_inputs.claim_runner_input(conv.id, read, "claim_read")

    def respond(request: httpx.Request) -> httpx.Response:
        if "file_missing" in request.url.path:
            return httpx.Response(404)
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"\0", headers={"Content-Type": mime})
        return httpx.Response(
            200, json={"name": filename, "content_type": mime, "metadata": {"bytes": 1}}
        )

    attempt = str(pending_inputs.delivery_attributes_for(conv.id, read)["delivery_attempt_id"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://test"
    ) as client:
        resolved = await _resolve_forwarded_message_content(
            content, session_id=conv.id, server_client=client, delivery_attempt_id=attempt
        )
    prompt = claude_native_executor._content_to_text(
        _normalize_message_content(resolved), tmp_path, attempt_id=attempt
    )
    assert f"omnigent-upload-{attempt}-0" in prompt
    if other_uploads:
        assert (
            f"[Attachment missing.png could not be loaded] [omnigent-upload-{attempt}-1]" in prompt
        )

    queued = f'<pasted_content id="paste">\n{prompt}\n</pasted_content id="paste">'
    queue_record = {"type": "queue-operation", "sessionId": "claude-session", "content": queued}

    def assistant(uuid: str, text: str) -> dict[str, Any]:
        return {
            "type": "assistant",
            "uuid": uuid,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }

    async def persist(request: httpx.Request) -> httpx.Response:
        item_id, replayed = await _persist_external_conversation_item(
            conv.id, conv, SessionEventInput.model_validate_json(request.content), store
        )
        return httpx.Response(202, json={"item_id": item_id, "replayed": replayed})

    transcript = tmp_path / "session.jsonl"
    records = [
        assistant("previous", "Prior output"),
        {**queue_record, "operation": "enqueue"},
    ]
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(persist), base_url="https://test"
    ) as client:
        first = read_transcript_items_from_offset(
            transcript, 0, start_line=0, agent_name="claude-native-ui"
        )
        for item in first.items:
            await _post_external_conversation_item(client, session_id=conv.id, item=item)
        assert pending_inputs.pending_ids(conv.id) == [older, unread, read]

        records = [
            {
                **queue_record,
                "operation": "remove",
                "reason": "absorbed_mid_turn",
                "commandUuid": "command",
                "deliveryId": "delivery",
            },
            {
                "type": "attachment",
                "uuid": "drained",
                "isSidechain": False,
                "attachment": {
                    "type": "queued_command",
                    "prompt": queued,
                    "commandMode": "prompt",
                    "source_uuid": "command",
                    "delivery_id": "delivery",
                    "origin": {"kind": "human"},
                },
            },
            assistant("next", "New output"),
        ]
        with transcript.open("a") as file:
            file.writelines(json.dumps(record) + "\n" for record in records)
        second = read_transcript_items_from_offset(
            transcript,
            first.byte_offset,
            start_line=first.line_cursor,
            agent_name="claude-native-ui",
            current_response_id=first.current_response_id,
        )
        for item in second.items:
            await _post_external_conversation_item(client, session_id=conv.id, item=item)

    assert pending_inputs.pending_ids(conv.id) == [older, unread]
    persisted = store.list_items(conv.id, order="asc").data
    assert len(persisted) == 3
    receipts = [call.args[1] for call in publish.call_args_list]
    assert [event["type"] for event in receipts] == [
        "response.output_item.done",
        "session.input.consumed",
        "response.output_item.done",
    ]
    assert receipts[1]["data"]["item_id"] == persisted[1].id
    assert [receipts[i]["item"]["id"] for i in (0, 2)] == [persisted[0].id, persisted[2].id]
    assert receipts[1]["data"]["cleared_pending_id"] == read
    assert receipts[1]["data"]["data"]["content"] == content
    assert receipts[1]["data"]["data"]["web_stable_id"] == _WEB_ID


@pytest.mark.parametrize(
    "echo",
    ["inline", "foreign-attempt", "text-only", "leading-attachment", "wrong-failure-ordinal"],
)
@pytest.mark.asyncio
async def test_claude_inline_attachment_echo_cannot_consume_unread_claim(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, echo: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native", harness_override="claude-native")
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    before = "See this screenshot."
    after = "Check the red dot."
    content = [
        {"type": "input_text", "text": before},
        {"type": "input_image", "file_id": "file_unread", "filename": "same-name.png"},
        {"type": "input_text", "text": after},
    ]
    unread = pending_inputs.record(conv.id, content, stable_id=_WEB_ID, runner_managed=True)
    assert pending_inputs.claim_runner_input(conv.id, unread, "claim_unread")
    attempt = pending_inputs.delivery_attributes_for(conv.id, unread)["delivery_attempt_id"]
    path = f"/opt/work/attachments/{'c' * 32}/"
    marker = f"[Attached: {path}same-name.png]"
    prompt = {
        "inline": f"{before}\n\n{marker}\n\n{after}",
        "foreign-attempt": (
            f"{before}\n\n[Attached: {path}omnigent-upload-{'d' * 32}-0-same-name.png]\n\n{after}"
        ),
        "text-only": f"{before}\n\n{after}",
        "leading-attachment": f"{marker}\n\n{before}\n\n{after}",
        "wrong-failure-ordinal": (
            f"{before}\n\n[Attachment same-name.png could not be loaded] "
            f"[omnigent-upload-{attempt}-1]\n\n{after}"
        ),
    }[echo]

    item_id, replay = await _persist_external_conversation_item(
        conv.id, conv, _mirror(prompt), store
    )

    assert item_id and not replay
    item = store.get_item(conv.id, item_id)
    assert item is not None and isinstance(item.data, MessageData)
    assert item.data.content == [{"type": "input_text", "text": prompt}]
    assert "web_stable_id" not in item.to_api_dict()
    assert pending_inputs.pending_ids(conv.id) == [unread]
    [receipt] = [
        call.args[1]["data"]
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
    ]
    assert receipt["item_id"] == item_id
    assert receipt.get("cleared_pending_id") is None


@pytest.mark.parametrize("files", [("image",), ("zip",), ("zip", "image"), ("image", "zip")])
@pytest.mark.asyncio
async def test_claimed_claude_image_only_mirror_consumes_its_own_send(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: tuple[str, ...]
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(
        title="Native", labels={WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE}
    )
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    monkeypatch.setattr(
        "omnigent.inner.native_attachments.attachments_root", lambda: tmp_path / "attachments"
    )
    blocks = {
        "image": {"type": "input_image", "filename": "image.png", "file_id": "file_image"},
        "zip": {"type": "input_file", "filename": "archive.zip", "file_id": "file_zip"},
    }
    uploads = [blocks[name] for name in files]
    earlier = pending_inputs.record(conv.id, [{"type": "input_text", "text": "held input"}])
    pending_id = pending_inputs.record(conv.id, uploads, stable_id=_WEB_ID, runner_managed=True)
    attrs = pending_inputs.delivery_attributes_for(conv.id, pending_id)
    prompts: list[str] = []

    async def claim(_deadline: float) -> bool:
        return pending_inputs.claim_runner_input(conv.id, pending_id, "claim")

    def inject(_bridge_dir: Path, *, content: str) -> bool:
        _claim_message_delivery()
        prompts.append(content)
        return True

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        filename, mime = (
            ("archive.zip", "application/zip")
            if "file_zip" in path
            else ("image.png", "image/png")
        )
        if path.endswith("/content"):
            return httpx.Response(200, content=b"\0", headers={"Content-Type": mime})
        return httpx.Response(
            200,
            json={"name": filename, "content_type": mime, "metadata": {"bytes": 1}},
        )

    monkeypatch.setattr(claude_native_executor, "inject_user_message", inject)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://example.test"
        ) as client:
            resolved = await _resolve_forwarded_message_content(
                uploads,
                session_id=conv.id,
                server_client=client,
                delivery_attempt_id=str(attrs["delivery_attempt_id"]),
            )
        with input_delivery_scope(attrs):
            [result] = [
                event
                async for event in claude_native_executor.ClaudeNativeExecutor(tmp_path).run_turn(
                    [
                        {
                            "role": "user",
                            "content": _normalize_message_content(resolved),
                        }
                    ],
                    [],
                    "",
                    config=ExecutorConfig(claim_native_delivery=claim),
                )
            ]
        assert isinstance(result, TurnComplete) and result.input_received
        [prompt] = prompts
        references = [line for line in prompt.splitlines() if line.strip()]
        assert len(references) == len(files)
        for reference, name in zip(references, files, strict=True):
            submitted = Path(reference.removeprefix("[Attached: ").partition("]")[0])
            assert submitted.read_bytes() == b"\0"
            assert submitted.name.endswith(blocks[name]["filename"])
            if name == "zip":
                assert submitted.name == "archive.zip"

        mirror = _mirror(prompt, source_id="claude-image-record")
        item_id, replay = await _persist_external_conversation_item(conv.id, conv, mirror, store)
        assert item_id and not replay
        item = store.get_item(conv.id, item_id)
        assert item is not None and isinstance(item.data, MessageData)
        assert item.data.content == uploads
        assert item.to_api_dict()["web_stable_id"] == _WEB_ID
        [receipt] = [
            call.args[1]["data"]
            for call in publish.call_args_list
            if call.args[1]["type"] == "session.input.consumed"
        ]
        assert receipt["data"]["web_stable_id"] == _WEB_ID
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [earlier]
        assert await _persist_external_conversation_item(conv.id, conv, mirror, store) == (
            item_id,
            True,
        )
        assert not await _settle_lost_runner_input(
            store, conv.id, pending_id, "native_input_lease_expired", expired=True
        )
        late_id, _ = await _persist_external_conversation_item(
            conv.id, conv, _mirror(prompt, source_id="late-claude-image-record"), store
        )
        assert late_id is not None
        late = store.get_item(conv.id, late_id)
        assert late is not None and "web_stable_id" not in late.to_api_dict()
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [earlier]
    finally:
        for entry in pending_inputs.snapshot_for(conv.id):
            pending_inputs.resolve(conv.id, entry["pending_id"])


@pytest.mark.parametrize("incomplete", ["wrong-ordinal", "load-failure"])
@pytest.mark.asyncio
async def test_incomplete_claude_attachment_echo_leaves_queue_untouched(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, incomplete: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native", harness_override="claude-native")
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", Mock())
    earlier = pending_inputs.record(conv.id, [{"type": "input_text", "text": "held input"}])
    pending_id = pending_inputs.record(
        conv.id,
        [{"type": "input_image", "file_id": "file_image"}],
        stable_id=_WEB_ID,
        runner_managed=True,
    )
    assert pending_inputs.claim_runner_input(conv.id, pending_id, "claim")
    attempt = pending_inputs.delivery_attributes_for(conv.id, pending_id)["delivery_attempt_id"]
    ordinal = 1 if incomplete == "wrong-ordinal" else 0
    prompt = (
        f"[Attached: /opt/work/attachments/{'a' * 32}/omnigent-upload-{attempt}-{ordinal}-s.png]"
    )
    if incomplete == "load-failure":
        prompt += "\n\n[Attachment another.png could not be loaded]"

    await _persist_external_conversation_item(conv.id, conv, _mirror(prompt), store)

    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [
        earlier,
        pending_id,
    ]
    for queued_id in (earlier, pending_id):
        drained = pending_inputs.resolve(conv.id, queued_id)
        assert drained is not None and not drained.uncertain
    assert all(item.type != "error" for item in store.list_items(conv.id).data)


@pytest.mark.asyncio
async def test_literal_old_claude_attachment_path_keeps_exact_text_receipt(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native", harness_override="claude-native")
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", Mock())
    literal = f"[Attached: /opt/work/attachments/{'a' * 32}/omnigent-upload-{'b' * 32}-0-s.png]"
    content = [{"type": "input_text", "text": literal}]
    pending_inputs.record(conv.id, content, stable_id=_WEB_ID)
    unrelated = pending_inputs.record(
        conv.id, [{"type": "input_image", "file_id": "file_other"}], runner_managed=True
    )
    assert pending_inputs.claim_runner_input(conv.id, unrelated, "other-claim")
    try:
        item_id, replay = await _persist_external_conversation_item(
            conv.id, conv, _mirror(literal), store
        )
        assert item_id and not replay
        item = store.get_item(conv.id, item_id)
        assert item is not None and item.to_api_dict()["web_stable_id"] == _WEB_ID
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [
            unrelated
        ]
    finally:
        for entry in pending_inputs.snapshot_for(conv.id):
            pending_inputs.resolve(conv.id, entry["pending_id"])


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


@pytest.mark.parametrize("attached", [False, True], ids=["text", "attached"])
@pytest.mark.parametrize(
    "second_source_id", [None, "tagged-echo"], ids=["co-queued-source-less", "tagged-second"]
)
@pytest.mark.asyncio
async def test_same_text_mirrors_keep_legacy_ids_only_for_co_queued_source_less_sends(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    second_source_id: str | None,
    attached: bool,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native")
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    content: list[dict[str, Any]] = [{"type": "input_text", "text": "ship it"}]
    mirror_text = "ship it"
    if attached:
        content.insert(0, {"type": "input_image", "file_id": "file_x", "filename": "x.png"})
        mirror_text = "[Attached: /tmp/x.png]\n\nship it"
    pending_inputs.record(conv.id, content, stable_id=_WEB_ID)
    pending_inputs.record(conv.id, content, stable_id=_OTHER_WEB_ID)

    try:
        first_id, replay = await _persist_external_conversation_item(
            conv.id, conv, _mirror(mirror_text, source_id=None), store
        )
        assert first_id == _WEB_ID and not replay
        assert pending_inputs.matches_unconfirmed_input_text(conv.id, "ship it")

        second_id, replay = await _persist_external_conversation_item(
            conv.id, conv, _mirror(mirror_text, source_id=second_source_id), store
        )
        assert second_id != first_id and not replay
        assert (second_id == _OTHER_WEB_ID) is (second_source_id is None)
        for item_id in (first_id, second_id):
            item = store.get_item(conv.id, item_id)
            assert item is not None
            assert "web_stable_id" not in item.to_api_dict()
        receipts = [
            call.args[1]["data"]
            for call in publish.call_args_list
            if call.args[1]["type"] == "session.input.consumed"
        ]
        assert [receipt["item_id"] for receipt in receipts] == [first_id, second_id]
        assert all("web_stable_id" not in receipt["data"] for receipt in receipts)
        assert all(receipt["data"].get("delivery_confirmed") is not False for receipt in receipts)
        assert not pending_inputs.snapshot_for(conv.id)
    finally:
        for pending in pending_inputs.snapshot_for(conv.id):
            pending_inputs.resolve(conv.id, pending["pending_id"])


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
    [
        "runner-uncertainty",
        "forward-rejected",
        "legacy-failure",
        "fifo",
        "skipped",
        "superseded",
        "confirmed",
    ],
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
        elif settlement == "superseded":
            target = store.create_conversation(title="New native session")
            _publish_session_superseded(conv.id, target.id)
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


_COMMAND_MIRRORS = pytest.mark.parametrize(
    ("text", "item_type", "item_data"),
    [
        (
            "/model sonnet",
            "slash_command",
            {
                "agent": "claude-native-ui",
                "kind": "command",
                "name": "model",
                "arguments": "sonnet",
            },
        ),
        ("! pwd", "terminal_command", {"kind": "input", "input": "pwd"}),
    ],
    ids=["slash", "shell"],
)


def _command_mirror(item_type: str, item_data: dict[str, Any]) -> SessionEventInput:
    return SessionEventInput(
        type="external_conversation_item",
        data={
            "item_type": item_type,
            "item_data": item_data,
            "response_id": "resp_command",
            "source_id": "command-echo",
        },
    )


@_COMMAND_MIRRORS
@pytest.mark.asyncio
async def test_command_resend_after_stop_leaves_its_echo_ambiguous(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    item_type: str,
    item_data: dict[str, Any],
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native")
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", Mock())
    stopped = pending_inputs.record(conv.id, [{"type": "input_text", "text": text}])
    pending_inputs.mark_interrupted(conv.id, [stopped])
    resend = pending_inputs.record(conv.id, [{"type": "input_text", "text": text}])
    try:
        item_id, replay = await _persist_external_conversation_item(
            conv.id, conv, _command_mirror(item_type, item_data), store
        )
        assert item_id and not replay
        # The live resend takes the mirror, which may still be the stopped send's echo.
        assert pending_inputs.resolve(conv.id, resend) is None
        assert pending_inputs.matches_unconfirmed_input_text(conv.id, text)
    finally:
        pending_inputs.resolve(conv.id, stopped)
        pending_inputs.resolve(conv.id, resend)


@_COMMAND_MIRRORS
@pytest.mark.parametrize("prior_state", ["plain", "uncertain", "interrupted"])
@pytest.mark.asyncio
async def test_command_mirror_remembers_text_when_delivery_is_uncertain(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    item_type: str,
    item_data: dict[str, Any],
    prior_state: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native")
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", Mock())
    pending_id = pending_inputs.record(conv.id, [{"type": "input_text", "text": text}])
    if prior_state == "uncertain":
        pending_inputs.mark_uncertain(conv.id)
    elif prior_state == "interrupted":
        pending_inputs.mark_interrupted(conv.id, [pending_id])
    mirror = SessionEventInput(
        type="external_conversation_item",
        data={
            "item_type": item_type,
            "item_data": item_data,
            "response_id": "resp_command",
            "source_id": "command-echo",
        },
    )
    try:
        item_id, replay = await _persist_external_conversation_item(conv.id, conv, mirror, store)
        assert item_id and not replay
        assert not pending_inputs.pending_ids(conv.id)
        assert pending_inputs.resolve(conv.id, pending_id) is None
        assert pending_inputs.matches_unconfirmed_input_text(conv.id, text) is (
            prior_state != "plain"
        )
    finally:
        pending_inputs.resolve(conv.id, pending_id)


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
