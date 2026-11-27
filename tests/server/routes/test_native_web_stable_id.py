"""A native mirror names the web send it delivered only when the pending-input link is exact."""

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent._wrapper_labels import CLAUDE_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
from omnigent.entities import ConversationItem, MessageData
from omnigent.errors import OmnigentError, WrongReplicaError
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
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.routes._sessions.helpers import _publish_session_superseded
from omnigent.server.routes._sessions.orchestration import (
    _persist_external_conversation_item,
    _record_native_input_lifecycle,
    _settle_lost_runner_input,
    _settle_undelivered_native_input,
)
from omnigent.server.routes.sessions import create_sessions_router, routes_events
from omnigent.server.schemas import (
    SessionEventInput,
    SessionInputDismissedEvent,
    SessionInputSendingEvent,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.routes.test_shell_permission_gate import _StubPermissionStore

_WEB_ID = "a" * 32
_OTHER_WEB_ID = "b" * 32
_FORGED_ID = "c" * 32
_LITERAL_ATTACHMENT_MARKER = "[Attached: /opt/work/status.txt]"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-native", "codex-native", "pi-native"])
async def test_pending_input_dismissal_is_display_only_after_native_handoff(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native")
    permissions = _StubPermissionStore()
    editor, viewer = "editor@example.com", "viewer@example.com"
    permissions.add_grant(editor, conv.id, LEVEL_EDIT)
    permissions.add_grant(viewer, conv.id, LEVEL_READ)
    app = FastAPI()
    app.include_router(
        create_sessions_router(
            conversation_store=store,
            agent_store=Mock(),
            permission_store=permissions,  # type: ignore[arg-type]
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def handle_error(_request: Request, error: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=error.http_status, content={"error": error.message})

    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    content = [{"type": "input_text", "text": "queued prompt"}]
    pending_id = pending_inputs.record(
        conv.id,
        content,
        created_by=editor,
        stable_id=_WEB_ID,
        native_harness=harness,
        runner_managed=harness == "claude-native",
    )
    other_id = pending_inputs.record(
        conv.id, [{"type": "input_text", "text": "another prompt"}], stable_id=_OTHER_WEB_ID
    )
    path = f"/v1/sessions/{conv.id}/pending-inputs/{pending_id}"
    attrs = pending_inputs.delivery_attributes_for(conv.id, pending_id)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            pending_inputs.mark_delivery_stage(conv.id, pending_id, "forward_accepted")
            if harness == "claude-native":
                assert pending_inputs.claim_runner_input(conv.id, pending_id, "a" * 32)
            _record_native_input_lifecycle(conv.id, {"type": "response.completed", **attrs})
            response = await client.delete(path, headers={"X-Forwarded-Email": editor})
            assert response.status_code == 409, response.text
            assert "can_dismiss" not in pending_inputs.snapshot_for(conv.id)[0]
            if harness == "pi-native":
                # Its inbox enqueue can complete before the agent accepts the input.
                return

            _record_native_input_lifecycle(
                conv.id,
                {"type": "response.completed", "input_received": True, **attrs},
            )
            eligible = SessionInputSendingEvent.model_validate(publish.call_args.args[1])
            assert eligible.can_dismiss and eligible.pending_id == pending_id
            assert pending_inputs.snapshot_for(conv.id)[0]["can_dismiss"] is True
            response = await client.delete(path, headers={"X-Forwarded-Email": viewer})
            assert response.status_code == 403, response.text
            assert pending_inputs.snapshot_for(conv.id)[0]["pending_id"] == pending_id

            if harness == "codex-native":
                wrong_replica = WrongReplicaError(
                    "session is on another replica", host_id="c" * 32
                )
                with monkeypatch.context() as replica:
                    replica.setattr(
                        routes_events,
                        "_raise_if_runner_on_another_replica",
                        AsyncMock(side_effect=wrong_replica),
                    )
                    response = await client.delete(path, headers={"X-Forwarded-Email": editor})
                assert response.status_code == wrong_replica.http_status, response.text
                assert pending_inputs.snapshot_for(conv.id)[0]["pending_id"] == pending_id

            publish.reset_mock()
            for _ in range(2):
                response = await client.delete(path, headers={"X-Forwarded-Email": editor})
                assert response.status_code == 204, response.text
            assert publish.call_count == 1
            dismissed = SessionInputDismissedEvent.model_validate(publish.call_args.args[1])
            assert dismissed.pending_id == pending_id
            assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [
                other_id
            ]

        # Other mirrors can arrive before this message's own exact mirror.
        await _persist_external_conversation_item(
            conv.id, conv, _mirror("another prompt", source_id="other-send"), store
        )
        await _persist_external_conversation_item(
            conv.id,
            conv,
            _mirror("typed in the terminal", source_id="other-terminal-prompt"),
            store,
        )
        assert pending_inputs.pending_ids(conv.id) == [pending_id]

        item_id, _ = await _persist_external_conversation_item(
            conv.id, conv, _mirror("queued prompt"), store
        )
        assert item_id is not None
        saved = store.get_item(conv.id, item_id)
        assert saved is not None
        assert saved.created_by == editor
        assert isinstance(saved.data, MessageData) and saved.data.content == content
        assert pending_inputs.pending_ids(conv.id) == []
    finally:
        pending_inputs.resolve(conv.id, pending_id)
        pending_inputs.resolve(conv.id, other_id)


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
    db_uri: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        users, receipts = await _persist_mirrors(
            db_uri,
            monkeypatch,
            [("deploy the fix", _OTHER_WEB_ID), ("ship it", _WEB_ID)],
            [_mirror("ship it"), _mirror("typed in the terminal", source_id="terminal-record")],
        )

    item, data = users["ship it"]
    assert item.id != _WEB_ID
    assert data.web_stable_id == _WEB_ID
    assert item.to_api_dict()["web_stable_id"] == _WEB_ID
    [receipt] = [receipt for receipt in receipts if receipt["item_id"] == item.id]
    assert receipt["data"]["web_stable_id"] == _WEB_ID
    assert receipt["cleared_pending_id"] is not None
    terminal_item, _ = users["typed in the terminal"]
    observed = {
        attrs["item_id"]: attrs
        for record in caplog.records
        if getattr(record, "event_name", None) == "native_input_consumed_published"
        for attrs in [cast(dict[str, Any], getattr(record, "attributes", {}))]
    }
    assert observed[item.id]["matched_pending_id"] == receipt["cleared_pending_id"]
    assert observed[item.id]["input_stable_id"] == _WEB_ID
    assert observed[terminal_item.id]["matched_pending_id"] is None
    assert "input_stable_id" not in observed[terminal_item.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("echo_text", "newer_web_text"),
    [
        pytest.param("Check the status.", None, id="None"),
        pytest.param("Check the status.", "Another message.", id="Another message."),
        pytest.param("Check the status.", "Check the status.", id="Check the status."),
        pytest.param(_LITERAL_ATTACHMENT_MARKER, "Another message.", id="literal-marker-fifo"),
        pytest.param(
            _LITERAL_ATTACHMENT_MARKER, _LITERAL_ATTACHMENT_MARKER, id="literal-marker-text"
        ),
    ],
)
async def test_later_same_text_terminal_input_cannot_settle_old_claim(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, echo_text: str, newer_web_text: str | None
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(labels={WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE})
    now = 0.0
    monkeypatch.setattr(pending_inputs, "_now", lambda: now)
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    pending_id = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": echo_text}],
        stable_id=_WEB_ID,
        runner_managed=True,
    )
    assert pending_inputs.claim_runner_input(conv.id, pending_id, "claim_web")
    newer_id = None
    try:
        now = 30 * 60.0
        if newer_web_text is not None:
            newer_id = pending_inputs.record(
                conv.id, [{"type": "input_text", "text": newer_web_text}], stable_id=_OTHER_WEB_ID
            )
        item_id, _ = await _persist_external_conversation_item(
            conv.id, conv, _mirror(echo_text, source_id="terminal-record"), store
        )
        [terminal] = store.list_items(conv.id).data
        assert terminal.id == item_id
        assert isinstance(terminal.data, MessageData)
        assert terminal.data.web_stable_id is None
        assert pending_inputs.pending_ids(conv.id) == (
            [pending_id, newer_id] if newer_id is not None else [pending_id]
        )
        [echo] = [
            call.args[1]["data"]
            for call in publish.call_args_list
            if call.args[1]["type"] == "session.input.consumed"
        ]
        assert echo["item_id"] == item_id
        assert echo.get("cleared_pending_id") is None

        now = 7201.0
        assert await _settle_lost_runner_input(
            store, conv.id, pending_id, "native_input_lease_expired", expired=True
        )
        assert pending_id not in pending_inputs.pending_ids(conv.id)
        [error] = store.list_items(conv.id, type="error").data
        assert error.data.model_dump()["code"] == "native_input_delivery_uncertain"
    finally:
        pending_inputs.resolve(conv.id, pending_id)
        if newer_id is not None:
            pending_inputs.resolve(conv.id, newer_id)


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
    first_pending_id = pending_inputs.record(conv.id, content, stable_id=_WEB_ID)
    second_pending_id = pending_inputs.record(conv.id, content, stable_id=_OTHER_WEB_ID)

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
            assert item.status == "completed"
            assert "web_stable_id" not in item.to_api_dict()
        frames = [call.args[1] for call in publish.call_args_list]
        receipts = [frame["data"] for frame in frames if frame["type"] == "session.input.consumed"]
        assert [receipt["item_id"] for receipt in receipts] == [first_id, second_id]
        assert [receipt["cleared_pending_id"] for receipt in receipts] == [
            first_pending_id,
            second_pending_id,
        ]
        assert all("web_stable_id" not in receipt["data"] for receipt in receipts)
        # Queue order settles the bubbles without proving which send made each echo.
        assert all(receipt["data"].get("delivery_confirmed") is False for receipt in receipts)
        assert not any(
            frame["type"] == "response.error"
            or (frame["type"] == "response.output_item.done" and frame["item"]["type"] == "error")
            for frame in frames
        )
        assert store.list_items(conv.id, type="error").data == []
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_text", "new_text"),
    [
        pytest.param("/review", "/review", id="same-short-name"),
        pytest.param("/example:review", "/review", id="late-qualified-name"),
        pytest.param("/review", "/example:review", id="late-short-name"),
    ],
)
async def test_late_slash_command_mirror_leaves_unclaimed_resend_queued(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, old_text: str, new_text: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(labels={WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE})
    now = 0.0
    monkeypatch.setattr(pending_inputs, "_now", lambda: now)
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", Mock())
    original = pending_inputs.record(
        conv.id, [{"type": "input_text", "text": old_text}], runner_managed=True
    )
    assert pending_inputs.claim_runner_input(conv.id, original, "original")

    now = pending_inputs._CLAIMED_TEXT_MATCH_TIMEOUT_S
    resend = pending_inputs.record(conv.id, [{"type": "input_text", "text": new_text}])
    try:
        item_id, replay = await _persist_external_conversation_item(
            conv.id,
            conv,
            _command_mirror(
                "slash_command",
                {
                    "agent": "claude-native-ui",
                    "kind": "skill",
                    "name": "example:review",
                    "arguments": "",
                },
            ),
            store,
        )
        assert item_id and not replay
        assert pending_inputs.pending_ids(conv.id) == [original, resend]
    finally:
        pending_inputs.resolve(conv.id, original)
        pending_inputs.resolve(conv.id, resend)


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


@pytest.mark.parametrize(
    "case",
    [
        "lease-expired",
        "runner-lost",
        "relay-uncertain",
        "relay-undelivered",
        "input-only-failure",
        "user-input-expired",
    ],
)
@pytest.mark.asyncio
async def test_runtime_notice_settlement_emits_no_unconfirmed_card_while_user_input_still_warns(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    """Internal runtime notices retire quietly while real unconfirmed user inputs still warn."""
    from omnigent.server.routes._sessions import orchestration

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native", harness_override="claude-native")
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    now = 0.0
    monkeypatch.setattr(pending_inputs, "_now", lambda: now)
    is_user = case == "user-input-expired"
    text = (
        "please check the failing test"
        if is_user
        else "[System: sub-agent worker finished (completed) — 1 result waiting in inbox.]"
    )
    stable_id = (
        _WEB_ID
        if case in {"relay-undelivered", "input-only-failure", "user-input-expired"}
        else None
    )
    pending_id = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": text}],
        stable_id=stable_id,
        runner_managed=case not in {"relay-undelivered", "input-only-failure"},
        runtime_notice=not is_user,
    )
    attempt_id = str(
        pending_inputs.delivery_attributes_for(conv.id, pending_id)["delivery_attempt_id"]
    )

    def _stream_runner(events: list[dict[str, Any]]) -> httpx.AsyncClient:
        body = (
            'data: {"type": "session.heartbeat"}\n\n'
            + "".join(f"data: {json.dumps(item)}\n\n" for item in events)
            + "data: [DONE]\n\n"
        )
        return httpx.AsyncClient(
            base_url="http://runner",
            transport=httpx.MockTransport(
                lambda _req: httpx.Response(
                    200,
                    text=body,
                    headers={"content-type": "text/event-stream"},
                )
            ),
        )

    try:
        with caplog.at_level("INFO"):
            if case in {"lease-expired", "user-input-expired"}:
                assert pending_inputs.claim_runner_input(conv.id, pending_id, "claim_1")
                # Late mirror arrives past the 12-minute window, leaving the claim active.
                now = 729.0
                await _persist_external_conversation_item(
                    conv.id, conv, _mirror(text, source_id="late-mirror"), store
                )
                assert pending_inputs.runner_lease_remaining(conv.id, pending_id) is not None
                now = 7201.0
                assert await _settle_lost_runner_input(
                    store, conv.id, pending_id, "native_input_lease_expired", expired=True
                )
            elif case == "runner-lost":
                assert await _settle_lost_runner_input(
                    store, conv.id, pending_id, "native_input_runner_lost"
                )
            elif case == "relay-uncertain":
                assert pending_inputs.claim_runner_input(conv.id, pending_id, "claim_1")
                failure = {
                    "type": "response.failed",
                    "response": {
                        "id": "resp_uncertain",
                        "error": {
                            "code": "native_input_delivery_uncertain",
                            "message": "This message may have arrived.",
                        },
                    },
                    "pending_id": pending_id,
                    "delivery_attempt_id": attempt_id,
                }
                # Replay the same failure twice to verify retired notices stay silent on retry.
                async with _stream_runner([dict(failure), dict(failure)]) as client:
                    await orchestration._relay_runner_stream_once(conv.id, client, store)
            elif case == "relay-undelivered":
                failure = {
                    "type": "response.failed",
                    "response": {
                        "id": "resp_undelivered",
                        "error": {
                            "code": "native_input_rejected",
                            "message": "paste refused",
                            "undelivered": True,
                        },
                    },
                    "input_stable_id": stable_id,
                    "pending_id": pending_id,
                    "delivery_attempt_id": attempt_id,
                }
                async with _stream_runner([dict(failure), dict(failure)]) as client:
                    await orchestration._relay_runner_stream_once(conv.id, client, store)
            elif case == "input-only-failure":
                # Build with the runner's real _prompt_wait_expired_failure constructor.
                from omnigent.runner.app import _prompt_wait_expired_failure

                event = _prompt_wait_expired_failure(
                    {
                        "type": "message",
                        "content": [{"type": "input_text", "text": text}],
                        "stable_id": stable_id,
                        "pending_id": pending_id,
                        "delivery_attempt_id": attempt_id,
                    }
                )
                async with _stream_runner([dict(event), dict(event)]) as client:
                    await orchestration._relay_runner_stream_once(conv.id, client, store)

        assert pending_inputs.runner_lease_remaining(conv.id, pending_id) is None
        errors = store.list_items(conv.id, type="error").data
        failed_messages = [
            item
            for item in store.list_items(conv.id, type="message").data
            if item.status == "failed"
        ]
        published_errors = [
            call.args[1]
            for call in publish.call_args_list
            if call.args[1].get("type") == "response.error"
            or (
                call.args[1].get("type") == "response.failed"
                and (
                    call.args[1].get("error") is not None
                    or (
                        isinstance(call.args[1].get("response"), dict)
                        and call.args[1]["response"].get("error") is not None
                    )
                )
            )
            or (
                call.args[1].get("type") == "response.output_item.done"
                and isinstance(call.args[1].get("item"), dict)
                and call.args[1]["item"].get("type") == "error"
            )
        ]
        if is_user:
            assert [err.data.model_dump()["code"] for err in errors] == [
                "native_input_delivery_uncertain"
            ]
            assert len(failed_messages) == 1
            assert len(published_errors) == 1
        else:
            assert errors == []
            assert failed_messages == []
            assert published_errors == []
            assert any(
                getattr(rec, "event_name", None) == "native_input_settled"
                and cast(dict[str, Any], getattr(rec, "attributes", {})).get("outcome")
                == "runtime_notice_retired"
                for rec in caplog.records
            )
    finally:
        pending_inputs.resolve(conv.id, pending_id)


@pytest.mark.parametrize(
    ("source", "expected_runtime_notice"),
    [
        ("runner-subagent-wake", True),
        ("runner-clear-recovery", True),
        ("runner-is-meta-timer", True),
        ("server-synthetic-top-level", True),
        ("human-spoof-attempt", False),
        ("runner-agent-session-header", False),
        ("runner-orchestration-source-header", False),
        ("runner-workflow-answer", False),
        ("post-system-message-notice", True),
        ("post-system-message-confirm", False),
    ],
)
@pytest.mark.asyncio
async def test_post_event_tags_only_trusted_internal_runtime_notices(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    expected_runtime_notice: bool,
) -> None:
    """Only trusted internal notices receive ``_runtime_notice``; human and agent sends do not."""
    from types import SimpleNamespace

    from fastapi import FastAPI, Response

    from omnigent.entities.workflow_questions import workflow_answer_input_id
    from omnigent.runner.identity import (
        ORCHESTRATION_SOURCE_HEADER,
        RUNNER_TUNNEL_TOKEN_HEADER,
        token_bound_runner_id,
    )
    from omnigent.server.peer_sweeper import PeerSweeper
    from omnigent.server.routes._sessions import orchestration
    from omnigent.server.routes.sessions import create_sessions_router
    from omnigent.server.session_collab import AGENT_SESSION_HEADER

    store = SqlAlchemyConversationStore(db_uri)
    token = "test-runner-tunnel-token"
    runner_id = token_bound_runner_id(token)
    conv = store.create_conversation(
        title="TopLevel", harness_override="claude-native", runner_id=runner_id
    )

    captured: list[bool] = []

    async def fake_dispatch(
        _session_id: str,
        _conv: Any,
        body: SessionEventInput,
        *_args: Any,
        **_kwargs: Any,
    ) -> orchestration._SessionEventDispatchResult:
        captured.append(body.data.get("_runtime_notice") is True)
        return orchestration._SessionEventDispatchResult(item_id=None, pending_id=None)

    monkeypatch.setattr(orchestration, "_dispatch_session_event_to_runner", fake_dispatch)
    monkeypatch.setattr(
        orchestration, "_get_runner_client", lambda *_a, **_k: _async_val(object())
    )
    monkeypatch.setattr(orchestration, "_ensure_runner_relay", lambda *_a, **_k: None)

    if source in {"post-system-message-notice", "post-system-message-confirm"}:
        await orchestration._post_system_message(
            conv.id,
            "[System: child session waiting for approval]",
            conversation_store=store,
            runner_router=None,
            confirm_delivery=(source == "post-system-message-confirm"),
        )
        assert captured == [expected_runtime_notice]
        return

    app = FastAPI()
    app.state.conversation_store = store
    app.state.permission_store = None
    app.state.standing_approval_service = SimpleNamespace(first_message=lambda _conv, b: b)
    router = create_sessions_router(
        store,
        cast(Any, object()),
        runner_tunnel_tokens={token},
    )
    app.include_router(router, prefix="/v1")
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_events._get_runner_client",
        lambda *_a, **_k: _async_val(object()),
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_events._dispatch_session_event_to_runner",
        fake_dispatch,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_events._ensure_runner_relay_ready",
        lambda *_a, **_k: _async_val(None),
    )
    monkeypatch.setattr(
        "omnigent.server.session_choice_runtime.refuse_if_stopped",
        lambda *_a, **_k: None,
    )

    if source == "server-synthetic-top-level":
        post_event_route = next(
            route.endpoint
            for route in router.routes
            if getattr(route, "path", "") == "/sessions/{session_id}/events"
            and "POST" in getattr(route, "methods", set())
        )
        req = PeerSweeper._synthetic_request(conv.id, app, runtime_notice=True)
        await post_event_route(
            session_id=conv.id,
            request=req,
            response=Response(),
            body=SessionEventInput(
                type="message",
                data={
                    "role": "user",
                    "content": [{"type": "input_text", "text": "[System: context] rotate soon"}],
                },
            ),
        )
        assert captured == [True]
        return

    headers: dict[str, str] = {}
    data: dict[str, Any] = {
        "role": "user",
        "content": [{"type": "input_text", "text": "[System: sub-agent done]"}],
    }
    if source == "runner-subagent-wake":
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = token
    elif source == "runner-clear-recovery":
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = token
        data = {
            "role": "user",
            "stable_id": _WEB_ID,
            "content": [{"type": "input_text", "text": "[System: Clear accepted. Handover.]"}],
        }
    elif source == "runner-is-meta-timer":
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = token
        data = {
            "role": "user",
            "is_meta": True,
            "content": [{"type": "input_text", "text": "internal wake without bracket prefix"}],
        }
    elif source == "human-spoof-attempt":
        data = {
            "role": "user",
            "_runtime_notice": True,
            "is_meta": True,
            "content": [{"type": "input_text", "text": "[System: spoofed by user]"}],
        }
    elif source == "runner-agent-session-header":
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = token
        headers[AGENT_SESSION_HEADER] = "agent"
    elif source == "runner-orchestration-source-header":
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = token
        headers[ORCHESTRATION_SOURCE_HEADER] = conv.id
    elif source == "runner-workflow-answer":
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = token
        answer_id = "elicit_journal_" + "d" * 32
        data = {
            "role": "user",
            "workflow_answer": answer_id,
            "stable_id": workflow_answer_input_id(answer_id),
            "content": [{"type": "input_text", "text": "[System: user answered card]"}],
        }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as client:
        resp = await client.post(
            f"/v1/sessions/{conv.id}/events",
            json={"type": "message", "data": data},
            headers=headers,
        )
    assert resp.status_code in (200, 202), resp.text
    assert captured == [expected_runtime_notice]


async def _async_val(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_retired_runtime_notice_stable_id_does_not_suppress_new_user_attempt(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reusing a retired notice's stable_id on a new user attempt still warns on failure."""
    from omnigent.runner.app import _prompt_wait_expired_failure
    from omnigent.server.routes._sessions import orchestration

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native", harness_override="claude-native")
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)

    notice_pid = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "[System: Clear accepted. Handover.]"}],
        stable_id=_WEB_ID,
        runner_managed=True,
        runtime_notice=True,
    )
    assert await _settle_lost_runner_input(store, conv.id, notice_pid, "native_input_runner_lost")
    assert store.list_items(conv.id, type="error").data == []

    # A later real user send reusing the same stable_id creates a distinct delivery_attempt_id.
    user_pid = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "real user message"}],
        stable_id=_WEB_ID,
    )
    user_attempt = str(
        pending_inputs.delivery_attributes_for(conv.id, user_pid)["delivery_attempt_id"]
    )
    event = _prompt_wait_expired_failure(
        {
            "type": "message",
            "content": [{"type": "input_text", "text": "real user message"}],
            "stable_id": _WEB_ID,
            "pending_id": user_pid,
            "delivery_attempt_id": user_attempt,
        }
    )
    body = (
        'data: {"type": "session.heartbeat"}\n\n'
        + f"data: {json.dumps(event)}\n\n"
        + "data: [DONE]\n\n"
    )
    try:
        async with httpx.AsyncClient(
            base_url="http://runner",
            transport=httpx.MockTransport(
                lambda _req: httpx.Response(
                    200, text=body, headers={"content-type": "text/event-stream"}
                )
            ),
        ) as client:
            await orchestration._relay_runner_stream_once(conv.id, client, store)

        errors = store.list_items(conv.id, type="error").data
        assert [err.data.model_dump()["code"] for err in errors] == ["native_prompt_wait_expired"]
        assert any(
            call.args[1].get("type") == "response.output_item.done"
            and isinstance(call.args[1].get("item"), dict)
            and call.args[1]["item"].get("type") == "error"
            for call in publish.call_args_list
        )
    finally:
        pending_inputs.resolve(conv.id, user_pid)


@pytest.mark.asyncio
async def test_old_retired_notice_replay_does_not_settle_new_user_input_with_same_stable_id(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replaying a retired notice's failure leaves a newer same-stable_id user send queued."""
    from omnigent.runner.app import _prompt_wait_expired_failure
    from omnigent.server.routes._sessions import orchestration

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native", harness_override="claude-native")
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)

    notice_pid = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "[System: Clear accepted. Handover.]"}],
        stable_id=_WEB_ID,
        runner_managed=True,
        runtime_notice=True,
    )
    notice_attempt = str(
        pending_inputs.delivery_attributes_for(conv.id, notice_pid)["delivery_attempt_id"]
    )
    assert await _settle_lost_runner_input(store, conv.id, notice_pid, "native_input_runner_lost")
    assert store.list_items(conv.id, type="error").data == []

    # A new live user input reuses the same stable_id under a fresh delivery_attempt_id.
    user_pid = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "live user send"}],
        stable_id=_WEB_ID,
    )
    user_attempt = str(
        pending_inputs.delivery_attributes_for(conv.id, user_pid)["delivery_attempt_id"]
    )
    assert user_attempt != notice_attempt
    replayed_notice_failure = _prompt_wait_expired_failure(
        {
            "type": "message",
            "content": [{"type": "input_text", "text": "[System: Clear accepted. Handover.]"}],
            "stable_id": _WEB_ID,
            "pending_id": notice_pid,
            "delivery_attempt_id": notice_attempt,
        }
    )
    body = (
        'data: {"type": "session.heartbeat"}\n\n'
        + f"data: {json.dumps(replayed_notice_failure)}\n\n"
        + "data: [DONE]\n\n"
    )
    try:
        async with httpx.AsyncClient(
            base_url="http://runner",
            transport=httpx.MockTransport(
                lambda _req: httpx.Response(
                    200, text=body, headers={"content-type": "text/event-stream"}
                )
            ),
        ) as client:
            await orchestration._relay_runner_stream_once(conv.id, client, store)

        # The newer user input stays queued untouched and no error card is emitted.
        [still_pending] = pending_inputs.snapshot_for(conv.id)
        assert still_pending["pending_id"] == user_pid
        assert (
            pending_inputs.delivery_attributes_for(conv.id, user_pid)["delivery_attempt_id"]
            == user_attempt
        )
        assert store.list_items(conv.id, type="error").data == []
        assert store.list_items(conv.id, type="message").data == []
        assert not any(
            call.args[1].get("type") == "response.error"
            or (
                call.args[1].get("type") == "response.output_item.done"
                and isinstance(call.args[1].get("item"), dict)
                and call.args[1]["item"].get("type") == "error"
            )
            for call in publish.call_args_list
        )
    finally:
        pending_inputs.resolve(conv.id, user_pid)


@pytest.mark.asyncio
async def test_mirror_hold_serializes_with_input_only_runtime_notice_failure(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An input-only failure arriving while a mirror holds a runtime notice emits no card."""
    import asyncio

    from omnigent.server.routes._sessions import orchestration

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native", harness_override="claude-native")
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", Mock())
    text = "[System: sub-agent finished]"
    pending_id = pending_inputs.record(
        conv.id, [{"type": "input_text", "text": text}], runtime_notice=True
    )
    attempt_id = str(
        pending_inputs.delivery_attributes_for(conv.id, pending_id)["delivery_attempt_id"]
    )
    entered_append = asyncio.Event()
    release_append = asyncio.Event()
    loop = asyncio.get_running_loop()
    orig_append = store.append

    def blocking_append(cid: str, items: Any) -> Any:
        loop.call_soon_threadsafe(entered_append.set)
        fut = asyncio.run_coroutine_threadsafe(release_append.wait(), loop)
        fut.result(timeout=5.0)
        return orig_append(cid, items)

    monkeypatch.setattr(store, "append", blocking_append)
    failure = {
        "type": "response.failed",
        "input_only": True,
        "response": {
            "id": "resp_" + "3" * 32,
            "error": {
                "code": "native_prompt_wait_expired",
                "message": "The terminal prompt went unanswered.",
                "undelivered": True,
            },
        },
        "pending_id": pending_id,
        "delivery_attempt_id": attempt_id,
        "input_content": [{"type": "input_text", "text": text}],
    }
    try:
        mirror_task = asyncio.create_task(
            _persist_external_conversation_item(
                conv.id, conv, _mirror(text, source_id="mirror-hold"), store
            )
        )
        await entered_append.wait()
        failure_task = asyncio.create_task(
            orchestration._settle_input_only_failure(store, conv.id, failure, "resp_" + "3" * 32)
        )
        await asyncio.sleep(0.05)
        release_append.set()
        await asyncio.gather(mirror_task, failure_task)
        assert store.list_items(conv.id, type="error").data == []
    finally:
        release_append.set()
        pending_inputs.resolve(conv.id, pending_id)


def test_scheduled_timer_wake_and_native_event_carry_runtime_notice_flag(db_uri: str) -> None:
    """Timer wakes set ``_runtime_notice`` and forward ``runtime_notice`` to the native runner."""
    from types import SimpleNamespace

    from omnigent.server.routes._sessions.orchestration import _build_native_terminal_message_event
    from omnigent.server.scheduled.fire import _task_event

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Native", harness_override="claude-native")

    timer_event = _task_event(
        cast(Any, SimpleNamespace(kind="timer", prompt="[System: timer t1 fired]"))
    )
    assert timer_event.data.get("_runtime_notice") is True
    runner_timer = _build_native_terminal_message_event(conv, timer_event)
    assert runner_timer.get("runtime_notice") is True

    prompt_event = _task_event(
        cast(
            Any,
            SimpleNamespace(kind="prompt", prompt="run nightly report", source_session_id=None),
        )
    )
    assert "_runtime_notice" not in prompt_event.data
    runner_prompt = _build_native_terminal_message_event(conv, prompt_event)
    assert "runtime_notice" not in runner_prompt

    peer_event = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "peer_message_id": "peer_1",
            "_runtime_notice": True,
            "content": [{"type": "input_text", "text": "[Peer message] hello"}],
        },
    )
    assert "runtime_notice" not in _build_native_terminal_message_event(conv, peer_event)
