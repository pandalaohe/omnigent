"""Compaction tests for Codex forwarder."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    codex_home_for_bridge_dir,
    write_bridge_state,
)
from omnigent.harnesses.codex_native.forwarder import _persist_codex_compaction_item
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)


@pytest.mark.asyncio
async def test_compaction_status_posts_and_dedupes_consecutive() -> None:
    """
    Compaction status mirrors as external_compaction_status, deduped (#1255).

    Codex may signal completion via both a ``contextCompaction`` item and a
    ``thread/compacted`` notification; consecutive identical statuses must
    not double-post (the spinner would flicker).
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await fwd._post_compaction_status(client, "conv_x", "in_progress", forwarder_state=state)
    await fwd._post_compaction_status(client, "conv_x", "completed", forwarder_state=state)
    # Duplicate completion (e.g. item then notification) is suppressed.
    await fwd._post_compaction_status(client, "conv_x", "completed", forwarder_state=state)

    assert [post[1] for post in client.posts] == [
        {"type": "external_compaction_status", "data": {"status": "in_progress"}},
        {"type": "external_compaction_status", "data": {"status": "completed"}},
    ]
    assert state.compaction_status_posted == "completed"


@pytest.mark.asyncio
async def test_completed_context_compaction_item_clears_spinner() -> None:
    """
    A completed ``contextCompaction`` item posts compaction-completed.

    It is a status edge, not transcript history, so it must clear the
    spinner without being appended as a conversation item.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    state.compaction_status_posted = "in_progress"

    await fwd._handle_completed_item(
        client,
        "conv_x",
        {
            "threadId": "thread_1",
            "turnId": "turn_1",
            "item": {"type": "contextCompaction", "id": "item_c"},
        },
        forwarder_state=state,
    )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {"type": "external_compaction_status", "data": {"status": "completed"}},
        )
    ]


# ---------------------------------------------------------------------------
# _persist_codex_compaction_item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_codex_compaction_item_posts_uuid_window_id(tmp_path: Path) -> None:
    """Codex's UUID window id is posted with the compaction checkpoint."""
    import json as _json

    codex_home = codex_home_for_bridge_dir(tmp_path)
    rollout = codex_home / "sessions" / "2026" / "09" / "05" / "rollout-thread_1.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        _json.dumps(
            {
                "type": "compacted",
                "payload": {
                    "replacement_history": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hi"}],
                        }
                    ],
                    "window_id": "01a070e2-2665-7d62-9b74-973decf239b7",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_codex",
            socket_path="ws://127.0.0.1:9999",
            thread_id="thread_1",
            codex_home=str(codex_home),
            cwd="/tmp/workspace",
        ),
    )
    get_resp = MagicMock()
    get_resp.json.return_value = {"data": [{"id": "item_codex"}]}
    get_resp.raise_for_status = MagicMock()

    client = MagicMock()
    client.get = AsyncMock(return_value=get_resp)

    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=post_resp)

    await _persist_codex_compaction_item(
        client,
        session_id="conv_codex",
        bridge_dir=tmp_path,
        thread_id="thread_1",
    )

    client.post.assert_called_once()
    _url, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["type"] == "compaction"
    assert body["data"]["last_item_id"] == "item_codex"
    assert "Codex" in body["data"]["summary"]
    assert body["data"]["window_id"] == "01a070e2-2665-7d62-9b74-973decf239b7"
    assert body["data"]["source_id"] == "thread_1:01a070e2-2665-7d62-9b74-973decf239b7", (
        "the boundary names its rollout window so a replay stores one item"
    )
    assert body["data"]["compacted_messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_persist_codex_compaction_item_names_the_item_without_a_rollout_window() -> None:
    """With no rollout window, the completed item's id names the boundary."""
    empty_resp = MagicMock()
    empty_resp.json.return_value = {"data": []}
    empty_resp.raise_for_status = MagicMock()

    client = MagicMock()
    client.get = AsyncMock(return_value=empty_resp)

    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=post_resp)

    await _persist_codex_compaction_item(
        client, session_id="conv_codex", thread_id="thread_1", item_id="item_c"
    )

    _url, kwargs = client.post.call_args
    assert kwargs["json"]["data"]["source_id"] == "thread_1:item_c"
    assert "window_id" not in kwargs["json"]["data"]


@pytest.mark.asyncio
async def test_persist_codex_compaction_item_without_identity_posts_as_before() -> None:
    """An unnamed boundary keeps today's payload: no source id, no dedupe."""
    empty_resp = MagicMock()
    empty_resp.json.return_value = {"data": []}
    empty_resp.raise_for_status = MagicMock()

    client = MagicMock()
    client.get = AsyncMock(return_value=empty_resp)

    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=post_resp)

    await _persist_codex_compaction_item(client, session_id="conv_codex")

    _url, kwargs = client.post.call_args
    assert "source_id" not in kwargs["json"]["data"]


@pytest.mark.parametrize(
    ("thread_id", "window_id", "item_id", "expected"),
    [
        ("thread_1", "01a070e2", "item_c", "thread_1:01a070e2"),
        ("thread_1", 2, "item_c", "thread_1:2"),
        ("thread_1", None, "item_c", "thread_1:item_c"),
        ("thread_1", "   ", "item_c", "thread_1:item_c"),
        (None, "01a070e2", "item_c", None),
        ("thread_1", None, None, None),
    ],
)
def test_codex_compaction_source_id_prefers_the_rollout_window(
    thread_id: str | None,
    window_id: object,
    item_id: str | None,
    expected: str | None,
) -> None:
    """Both Codex paths read the same rollout entry, so the window id wins."""
    assert (
        fwd._codex_compaction_source_id(thread_id, window_id=window_id, item_id=item_id)
        == expected
    )


def test_compaction_persist_failure_reason_includes_server_body() -> None:
    """A rejected compaction persist must name the server's reason.

    ``raise_for_status`` reports only the status and URL, so a 400 on this POST
    left no way to tell which field the server objected to — the payload is
    assembled from Codex's rollout, so the answer is only in the response body.
    """
    request = httpx.Request("POST", "https://example.invalid/v1/sessions/conv_x/events")
    response = httpx.Response(
        400,
        request=request,
        text='{"error_code":"INVALID_PARAMETER_VALUE","message":"last_item_id not found"}',
    )
    exc = httpx.HTTPStatusError("400 Bad Request", request=request, response=response)

    reason = fwd._compaction_persist_failure_reason(exc)

    assert "400" in reason
    assert "last_item_id not found" in reason


def test_compaction_persist_failure_reason_handles_non_http_errors() -> None:
    """A non-HTTP failure still gets a one-line reason rather than an empty string."""
    assert fwd._compaction_persist_failure_reason(RuntimeError("boom")) == "RuntimeError: boom"


@pytest.mark.asyncio
async def test_persist_codex_compaction_item_empty_items_fallback() -> None:
    """When no items exist, last_item_id falls back to compact_boundary_ prefix."""
    empty_resp = MagicMock()
    empty_resp.json.return_value = {"data": []}
    empty_resp.raise_for_status = MagicMock()

    client = MagicMock()
    client.get = AsyncMock(return_value=empty_resp)

    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=post_resp)

    await _persist_codex_compaction_item(client, session_id="conv_codex")

    client.post.assert_called_once()
    _url, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["data"]["last_item_id"].startswith("compact_boundary_")
    assert "compacted_messages" not in body["data"]


@pytest.mark.parametrize("window_id", [2, "01a070e2-2665-7d62-9b74-973decf239b7"])
def test_read_compacted_history_extracts_replacement_history_and_window_id(
    tmp_path: Path,
    window_id: int | str,
) -> None:
    """_read_compacted_history returns replacement_history and window_id."""
    import json as _json

    rollout = tmp_path / "rollout.jsonl"
    lines = [
        _json.dumps({"type": "session_meta", "payload": {"id": "abc"}}),
        _json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user"}}),
        _json.dumps(
            {
                "type": "compacted",
                "payload": {
                    "message": "summary",
                    "replacement_history": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hi"}],
                        },
                        {
                            "type": "compaction",
                            "encrypted_content": "gAAAA_test_token",
                        },
                    ],
                    "window_id": window_id,
                },
            }
        ),
    ]
    rollout.write_text("\n".join(lines) + "\n")

    result = fwd._read_compacted_history(rollout)

    assert result is not None
    assert result["window_id"] == window_id
    assert len(result["replacement_history"]) == 2
    assert result["replacement_history"][0]["type"] == "message"
    assert result["replacement_history"][0]["role"] == "user"
    assert result["replacement_history"][1]["type"] == "compaction"
    assert result["replacement_history"][1]["encrypted_content"] == "gAAAA_test_token"


def test_read_compacted_history_returns_none_for_no_compacted_entry(
    tmp_path: Path,
) -> None:
    """_read_compacted_history returns None when no Compacted entry exists."""
    import json as _json

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(_json.dumps({"type": "session_meta", "payload": {"id": "abc"}}) + "\n")

    assert fwd._read_compacted_history(rollout) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_reason", [None, "summary", "incomplete", "newer_turn", "top_level"])
async def test_no_summary_child_rollover_restores_current_turn_once(
    tmp_path: Path, skip_reason: str | None
) -> None:
    """Recover the newest assignment and steering, without reviving an older task."""
    from omnigent.harnesses.codex_native.context_recovery import ChildContextRecovery

    rollout = tmp_path / "rollout.jsonl"
    payload = {
        "message": "",
        "window_id": "window-new",
        "resume_metadata": {"last_started_turn_id": "turn-new"},
        "retained_context": {
            "user_messages_incomplete": False,
            "user_messages": [
                {"turn_id": "turn-old", "text": "Review the obsolete task."},
                {
                    "turn_id": "turn-new",
                    "text": (
                        "Standing approval (this parent session): install.\nBuild the new task."
                    ),
                },
                {"turn_id": "turn-new", "text": "Limit changes to the parser."},
            ],
        },
    }
    if skip_reason == "summary":
        payload["message"] = "Ordinary summary compaction."
    elif skip_reason == "incomplete":
        payload["retained_context"]["user_messages_incomplete"] = True
    elif skip_reason == "newer_turn":
        payload["resume_metadata"]["last_started_turn_id"] = "turn-ended"
    rollout.write_text(json.dumps({"type": "compacted", "payload": payload}) + "\n")
    client = MagicMock()
    client.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"parent_session_id": None if skip_reason == "top_level" else "parent"},
            request=httpx.Request("GET", "http://test"),
        )
    )
    native = MagicMock()
    native.request = AsyncMock(return_value={"result": {"turnId": "turn-new"}})
    recovery = ChildContextRecovery()
    args = {
        "client": client,
        "codex_client": native,
        "session_id": "child",
        "bridge_dir": tmp_path,
        "rollout": rollout,
        "thread_id": "thread",
        "turn_id": "turn-new",
    }
    await recovery.restore(**args, model_resumed=False)
    # Both completion forms and a reconnected forwarder see the same window.
    await recovery.restore(**args, model_resumed=False)
    await ChildContextRecovery().restore(**args, model_resumed=False)
    if skip_reason:
        native.request.assert_not_awaited()
        return
    native.request.assert_awaited_once()
    method, sent = native.request.call_args.args
    assert method == "turn/steer"
    assert sent["expectedTurnId"] == "turn-new"
    text = sent["input"][0]["text"]
    assert "Build the new task." in text
    assert "Limit changes to the parser." in text
    assert "obsolete task" not in text
    assert "Standing approval" not in text
    assert "do not grant or renew permissions" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["compaction", "transport", "stale"])
async def test_no_summary_recovery_retries_only_proven_compaction_refusal(
    tmp_path: Path, failure: str
) -> None:
    """A refusal waits for model progress and includes steering added in the meantime."""
    from omnigent.harnesses.codex_native.app_server import CodexAppServerResponseError
    from omnigent.harnesses.codex_native.context_recovery import ChildContextRecovery

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "compacted",
                "payload": {
                    "message": "",
                    "window_id": "window-new",
                    "resume_metadata": {"last_started_turn_id": "turn-new"},
                    "retained_context": {
                        "user_messages_incomplete": False,
                        "user_messages": [
                            {"turn_id": "turn-new", "text": "Implement the current task."},
                        ],
                    },
                },
            }
        )
        + "\n"
    )
    response = httpx.Response(
        200, json={"parent_session_id": "parent"}, request=httpx.Request("GET", "http://test")
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    native = MagicMock()
    error = (
        ConnectionError("Reply lost after submission")
        if failure == "transport"
        else CodexAppServerResponseError({"code": -32600, "message": "no active turn to steer"})
        if failure == "stale"
        else CodexAppServerResponseError(
            {
                "code": -32600,
                "message": "cannot steer a compact turn",
                "data": {
                    "codexErrorInfo": {"activeTurnNotSteerable": {"turnKind": "compact"}},
                },
            }
        )
    )
    native.request = AsyncMock(side_effect=[error, {"result": {"turnId": "turn-new"}}])
    recovery = ChildContextRecovery()
    args = {
        "client": client,
        "codex_client": native,
        "session_id": "child",
        "bridge_dir": tmp_path,
        "rollout": rollout,
        "thread_id": "thread",
        "turn_id": "turn-new",
    }
    await recovery.restore(**args, model_resumed=False)
    await recovery.restore(**args, model_resumed=False)
    assert native.request.await_count == 1
    with rollout.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Cancel implementation; report only."}
                        ],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-new"},
                    },
                }
            )
            + "\n"
        )
    await recovery.restore(**args, model_resumed=True)
    if failure != "compaction":
        await ChildContextRecovery().restore(**args, model_resumed=True)
        assert native.request.await_count == 1
        return
    assert native.request.await_count == 2
    assert (
        "Cancel implementation; report only."
        in native.request.call_args.args[1]["input"][0]["text"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_turn", ["turn", "resumed-turn"])
async def test_reconnected_forwarder_retries_refused_recovery_on_model_event(
    tmp_path: Path, retry_turn: str
) -> None:
    """Reconnect retries through the event entry without replaying accepted input."""
    from tests.harnesses.codex_native.session._support import _forwarder_context

    home = codex_home_for_bridge_dir(tmp_path)
    rollout = home / "sessions" / "rollout-thread.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "compacted",
                "payload": {
                    "message": "",
                    "window_id": "window",
                    "resume_metadata": {"last_started_turn_id": "turn"},
                    "retained_context": {
                        "user_messages_incomplete": False,
                        "user_messages": [
                            {
                                "turn_id": "turn",
                                "text": "Cancel edits. Report the current findings.",
                            },
                        ],
                    },
                },
            }
        )
        + "\n"
    )
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="child",
            socket_path="ws://127.0.0.1:9999",
            thread_id="thread",
            codex_home=str(home),
            cwd="/opt/work/project",
            active_turn_id="turn",
        ),
    )
    (tmp_path / "context-recovery.json").write_text(
        json.dumps(
            {
                "session_id": "child",
                "thread_id": "thread",
                "window_id": "window",
                "refused": True,
                "source_turn_id": "turn",
            }
        )
    )
    if retry_turn != "turn":
        with rollout.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "INTERMEDIATE_CANCEL: stop all edits.",
                                }
                            ],
                            "internal_chat_message_metadata_passthrough": {
                                "turn_id": "intermediate"
                            },
                        },
                    }
                )
                + "\n"
            )
            stream.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "New scope: report only; no edits."}
                            ],
                            "internal_chat_message_metadata_passthrough": {"turn_id": retry_turn},
                        },
                    }
                )
                + "\n"
            )
    native = MagicMock()
    native.request = AsyncMock(return_value={"result": {"turnId": "turn"}})
    state = fwd._CodexForwarderState(active_parent_turn_id="turn", mcp_startup_settled=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"parent_session_id": "parent", "data": []})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        args = {
            **_forwarder_context(client, tmp_path, session_id="child"),
            "forwarder_state": state,
            "codex_client": native,
            "expected_thread_id": "thread",
        }
        event = {
            "method": "item/started",
            "params": {
                "threadId": "thread",
                "turnId": retry_turn,
                "item": {"type": "reasoning", "id": "reasoning"},
            },
        }
        await fwd._handle_event(client, **args, event=event, is_replay=True)
        native.request.assert_not_awaited()
        await fwd._handle_event(client, **args, event=event)
        await fwd._handle_event(client, **args, event=event)
        await fwd._handle_event(
            client,
            **args,
            event={
                "method": "thread/compacted",
                "params": {"threadId": "thread"},
            },
        )
    native.request.assert_awaited_once()
    assert "Cancel edits." in native.request.call_args.args[1]["input"][0]["text"]
    if retry_turn != "turn":
        assert native.request.call_args.args[1]["expectedTurnId"] == retry_turn
        notice = native.request.call_args.args[1]["input"][0]["text"]
        assert "New scope: report only" in notice
        assert notice.index("INTERMEDIATE_CANCEL") < notice.index("New scope: report only")
