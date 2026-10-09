"""Native hook notices are durable UI metadata, never model input."""

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.db.db_models import SqlConversationItem
from omnigent.entities.conversation import NON_CONTENT_ITEM_TYPES, ErrorData
from omnigent.harnesses.codex_native import forwarder as fwd

# Real Codex hook run ids are "<event>:<index>:<hooks file path>".
_PATH_RUN_ID = (
    "session-start:3:/home/user/.omnigent/codex-native/"
    "0123456789abcdef0123456789abcdef/codex-home/hooks.json"
)
_RESPONSE_ID_MAX_CHARS = SqlConversationItem.__table__.c.response_id.type.length


async def _forward(
    tmp_path, entries, *, thread="parent", run_id="hook-1", replay=False, status_message=None
):
    """Exercise real event routing and HTTP payload generation."""
    client = AsyncMock()
    client.post.return_value = httpx.Response(200)
    state = fwd._CodexForwarderState()
    state.subagents_by_thread["child"] = "child-session"
    run: dict[str, object] = {"id": run_id, "entries": entries}
    if status_message is not None:
        run["statusMessage"] = status_message
    await fwd._handle_event(
        client,
        session_id="parent-session",
        bridge_dir=tmp_path,
        event={
            "method": "hook/completed",
            "params": {
                "threadId": thread,
                "run": run,
            },
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "parent-session"),
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="parent",
        forwarder_state=state,
        is_replay=replay,
    )
    return client.post.call_args_list


@pytest.mark.asyncio
async def test_marked_observer_run_is_softened_to_an_info_notice(tmp_path: Path) -> None:
    """The observer's own hook failure is informational; a user hook's is not."""
    from omnigent.harnesses.codex_native.app_server import CODEX_TOOL_OBSERVER_STATUS_MESSAGE

    observer_posts = await _forward(
        tmp_path,
        [{"kind": "error", "text": "hook timed out after 3s"}],
        status_message=CODEX_TOOL_OBSERVER_STATUS_MESSAGE,
    )
    assert len(observer_posts) == 1
    observer = ErrorData.model_validate(observer_posts[0].kwargs["json"]["data"]["item_data"])
    assert observer.level == "info"
    assert observer.code == "codex_tool_observer"
    assert "hook timed out after 3s" in observer.message
    assert "not affected" in observer.message

    user_posts = await _forward(
        tmp_path,
        [{"kind": "error", "text": "hook timed out after 3s"}],
        run_id="hook-user",
        status_message="running lint",
    )
    assert len(user_posts) == 1
    user = ErrorData.model_validate(user_posts[0].kwargs["json"]["data"]["item_data"])
    assert user.level == "error"
    assert user.code == "codex_hook_error"


@pytest.mark.asyncio
async def test_hook_notices_are_non_content_metadata(tmp_path: Path) -> None:
    """Warnings and errors render without creating input or failing a turn."""
    posts = await _forward(
        tmp_path,
        [
            {"kind": "warning", "text": "Maintenance needs attention"},
            {"kind": "error", "text": "Hook failed"},
            {"kind": "context", "text": "MODEL_CONTEXT_MUST_NOT_BE_MIRRORED"},
            {"kind": "feedback", "text": "MODEL_FEEDBACK_MUST_NOT_BE_MIRRORED"},
            {"kind": "stop", "text": "STOP_MUST_NOT_CHANGE_TURN_STATUS"},
        ],
    )
    assert len(posts) == 2
    for call, kind, level, message in zip(
        posts,
        ("warning", "error"),
        ("info", "error"),
        ("Maintenance needs attention", "Hook failed"),
        strict=True,
    ):
        assert call.args == ("/v1/sessions/parent-session/events",)
        payload = call.kwargs["json"]
        assert payload["type"] == "external_conversation_item"
        data = payload["data"]
        assert data["item_type"] == "error"
        assert data["item_type"] in NON_CONTENT_ITEM_TYPES
        notice = ErrorData.model_validate(data["item_data"])
        assert (notice.source, notice.code, notice.level, notice.message) == (
            "harness",
            f"codex_hook_{kind}",
            level,
            message,
        )
        assert data["source_id"]


@pytest.mark.asyncio
async def test_hook_notice_replay_and_child_routing(tmp_path: Path) -> None:
    """Replays reuse durable keys; parent and child run IDs cannot collide."""
    entries = [{"kind": "warning", "text": "Attention"}]
    original = await _forward(tmp_path, entries)
    replay = await _forward(tmp_path, entries, replay=True)
    child = await _forward(tmp_path, entries, thread="child")
    assert len(original) == len(replay) == len(child) == 1
    assert original[0].kwargs["json"] == replay[0].kwargs["json"]
    assert child[0].args == ("/v1/sessions/child-session/events",)
    assert (
        child[0].kwargs["json"]["data"]["source_id"]
        != original[0].kwargs["json"]["data"]["source_id"]
    )
    assert await _forward(tmp_path, entries, thread="stale") == []


@pytest.mark.asyncio
async def test_hook_notice_response_id_fits_storage_for_path_run_ids(tmp_path: Path) -> None:
    """A run id carrying the hooks file path still yields a storable, stable id."""
    entries = [{"kind": "warning", "text": "Attention"}]
    original = await _forward(tmp_path, entries, run_id=_PATH_RUN_ID)
    replay = await _forward(tmp_path, entries, run_id=_PATH_RUN_ID, replay=True)
    other_run = await _forward(
        tmp_path, entries, run_id=_PATH_RUN_ID.replace("session-start:3:", "session-start:4:")
    )
    response_id = original[0].kwargs["json"]["data"]["response_id"]
    assert len(_PATH_RUN_ID) > _RESPONSE_ID_MAX_CHARS
    assert len(response_id) <= _RESPONSE_ID_MAX_CHARS
    assert "hooks.json" not in response_id
    assert replay[0].kwargs["json"]["data"]["response_id"] == response_id
    assert other_run[0].kwargs["json"]["data"]["response_id"] != response_id


@pytest.mark.asyncio
@pytest.mark.parametrize("entries", [[], None, {}, [None, {}, {"kind": "warning", "text": "  "}]])
async def test_empty_or_malformed_hook_entries_are_quiet(tmp_path: Path, entries) -> None:
    """Healthy hooks and malformed entries do not create empty banners."""
    assert await _forward(tmp_path, entries) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "thread,run_id", [(None, "h"), ("", "h"), ("parent", ""), ("parent", None)]
)
async def test_hook_without_durable_identity_is_ignored(tmp_path: Path, thread, run_id) -> None:
    """Incomplete identities cannot produce colliding notices."""
    assert (
        await _forward(
            tmp_path, [{"kind": "warning", "text": "Attention"}], thread=thread, run_id=run_id
        )
        == []
    )
