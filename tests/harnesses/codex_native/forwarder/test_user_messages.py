"""User messages tests for Codex forwarder."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.harnesses.codex_native import bridge
from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)


@pytest.mark.parametrize(
    "content,expected",
    [
        ([{"type": "image", "url": "data:image/png;base64,AAAA"}], True),
        ([{"type": "input_file", "file_data": "data:application/pdf;base64,AAAA"}], True),
        ([{"type": "text", "text": "hi"}, {"type": "image", "url": "data:x"}], True),
        ([{"type": "text", "text": "only text"}], False),
        ([], False),
        ("not a list", False),
    ],
)
def test_user_message_has_file_content(content: object, expected: bool) -> None:
    """
    Detect a non-text (image/file) block in a Codex ``userMessage``.

    Drives the gate that decides whether a text-less ``userMessage`` is a
    real image-bearing message that must be persisted. ``True`` for any
    block whose ``type`` is not ``"text"``, else ``False``. A wrong result
    re-opens the image-only regression (text-less image skipped → dropped
    bubble + pending-FIFO bleed) or makes text-only messages post twice.
    """
    assert fwd._user_message_has_file_content({"content": content}) is expected


@pytest.mark.asyncio
async def test_post_user_message_image_only_posts_empty_content() -> None:
    """
    An image-only ``userMessage`` is posted with EMPTY Omnigent content.

    Regression guard for the image-only bleed/ordering bug: the forwarder
    must post the user item (so the server drains the pending-input FIFO
    entry and folds the image in by file_id). The posted content is empty
    — the base64 ``data:`` URL Codex echoes must NOT be written into text.
    A bail here would drop the user bubble and leak the pending entry into
    the next message.
    """
    client = _RecordingClient()
    item = {
        "type": "userMessage",
        "content": [{"type": "image", "url": "data:image/png;base64,AAAA"}],
    }

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert len(client.posts) == 1, "image-only userMessage must still be posted"
    _url, body = client.posts[0]
    item_data = body["data"]["item_data"]
    assert item_data["role"] == "user"
    # Empty content: the image is supplied server-side from the pending
    # entry; echoing Codex's base64 url here would re-introduce the freeze.
    assert item_data["content"] == []


@pytest.mark.asyncio
async def test_post_user_message_text_posts_input_text() -> None:
    """A text ``userMessage`` posts an ``input_text`` block (unchanged path)."""
    client = _RecordingClient()
    item = {"type": "userMessage", "content": [{"type": "text", "text": "hello"}]}

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert body["data"]["item_data"]["content"] == [{"type": "input_text", "text": "hello"}]


@pytest.mark.asyncio
async def test_post_user_message_truly_empty_is_skipped() -> None:
    """
    A ``userMessage`` with neither text nor a file block is not posted.

    Without this guard the forwarder would emit spurious empty user
    bubbles. A failure (a post recorded) means the empty-skip branch broke.
    """
    client = _RecordingClient()
    item = {"type": "userMessage", "content": []}

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert client.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("source_error", [False, True])
async def test_automatic_echo_carries_recorded_identity_without_inventing_a_marker(
    tmp_path, accepted, source_error, monkeypatch
):
    state = bridge.CodexNativeBridgeState(
        session_id="child",
        socket_path="/opt/work/app.sock",
        thread_id="thread",
        codex_home=str(tmp_path / "home"),
    )
    bridge.write_bridge_state(tmp_path, state)
    input_id = "a" * 32
    bridge.record_retry_input(
        tmp_path,
        session_id="child",
        thread_id="thread",
        input_id=input_id,
        turn_id="turn" if accepted else None,
    )
    client = _RecordingClient()
    if source_error:

        def unavailable(*_args, **_kwargs):
            raise OSError("source record unavailable")

        monkeypatch.setattr(fwd, "record_retry_source", unavailable)
    params = {"threadId": "thread", "turnId": "turn"}
    for native_id, client_id in (("vendor-automatic", input_id), ("human-item", None)):
        posted = await fwd._post_user_message(
            client,
            "child",
            params,
            {
                "type": "userMessage",
                "id": native_id,
                "clientId": client_id,
                "content": [{"type": "text", "text": "retry"}],
            },
            bridge_dir=tmp_path,
        )
        assert posted is (not source_error or client_id is None)
    if source_error:
        [(_, human_body)] = client.posts
        assert "input_id" not in human_body["data"]
        assert "auto_retry" not in human_body["data"]["item_data"]
        return
    automatic, human = [body["data"] for _, body in client.posts]
    assert automatic["input_id"] == input_id
    assert automatic["input_turn_id"] == "codex_turn"
    assert automatic["source_id"] == "thread:turn:vendor-automatic"
    assert "auto_retry" not in automatic["item_data"]
    assert "input_id" not in human and "input_turn_id" not in human


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["session", "thread", "turn", "text", "missing", "mismatch"])
async def test_unverified_echo_keeps_its_vendor_source_without_a_marker(tmp_path, fault):
    bridge.write_bridge_state(
        tmp_path,
        bridge.CodexNativeBridgeState(
            session_id="child",
            socket_path="/opt/work/app.sock",
            thread_id="thread",
            codex_home=str(tmp_path / "home"),
            retry_inputs={"a" * 32: "turn"},
        ),
    )
    client = _RecordingClient()
    posted = await fwd._post_user_message(
        client,
        "other" if fault == "session" else "child",
        {
            "threadId": "other" if fault == "thread" else "thread",
            "turnId": "other" if fault == "turn" else "turn",
        },
        {
            "id": "a" * 32,
            "clientId": None
            if fault == "missing"
            else ("b" * 32 if fault == "mismatch" else "a" * 32),
            "content": [{"type": "text", "text": "other" if fault == "text" else "retry"}],
        },
        bridge_dir=tmp_path,
    )
    assert posted is True
    [(_, body)] = client.posts
    assert "input_id" not in body["data"] and "input_turn_id" not in body["data"]
    assert "auto_retry" not in body["data"]["item_data"]
    assert body["data"]["source_id"].endswith(":" + "a" * 32)


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_unrelated_positional_history_is_not_lost_to_an_unbound_retry(tmp_path, pending):
    bridge.write_bridge_state(
        tmp_path,
        bridge.CodexNativeBridgeState(
            session_id="child",
            socket_path="/opt/work/app.sock",
            thread_id="thread",
            codex_home=str(tmp_path / "home"),
            retry_inputs={"a" * 32: None if pending else "auto-turn"},
        ),
    )

    async def snapshot(method, params):
        return {
            "result": {
                "thread": {
                    "turns": [
                        {
                            "id": "historical-turn",
                            "items": [
                                {
                                    "id": "item-1",
                                    "type": "userMessage",
                                    "content": [
                                        {"type": "text", "text": "Historical human input"}
                                    ],
                                },
                            ],
                        }
                    ]
                }
            }
        }

    client = _RecordingClient()
    state = fwd._CodexForwarderState(
        codex_client=SimpleNamespace(request=snapshot),
        child_usage_baseline_dir=tmp_path,
    )
    await fwd._ensure_user_message_posted(
        client,
        "child",
        {"threadId": "thread", "turnId": "historical-turn"},
        state,
    )
    [(_, body)] = client.posts
    assert body["data"]["item_data"]["content"] == [
        {"type": "input_text", "text": "Historical human input"}
    ]
    assert "input_id" not in body["data"]


@pytest.mark.asyncio
async def test_retry_recovery_keeps_vendor_source_and_same_turn_human_retry(tmp_path):
    bridge.write_bridge_state(
        tmp_path,
        bridge.CodexNativeBridgeState(
            session_id="child",
            socket_path="/opt/work/app.sock",
            thread_id="thread",
            codex_home=str(tmp_path / "home"),
            retry_inputs={"a" * 32: "auto-turn"},
        ),
    )
    first = {
        "id": "vendor-item",
        "clientId": "a" * 32,
        "type": "userMessage",
        "content": [{"type": "text", "text": "retry"}],
    }

    async def snapshot(method, params):
        return {"result": {"thread": {"turns": [{"id": "auto-turn", "items": [first]}]}}}

    client = _RecordingClient()
    state = fwd._CodexForwarderState(
        codex_client=SimpleNamespace(request=snapshot),
        child_usage_baseline_dir=tmp_path,
    )
    params = {"threadId": "thread", "turnId": "auto-turn"}
    await fwd._ensure_user_message_posted(client, "child", params, state)
    for item in (first, {**first, "id": "item-2", "clientId": None}):
        await fwd._handle_completed_item(
            client,
            "child",
            {**params, "item": item},
            forwarder_state=state,
            bridge_dir=tmp_path,
        )
    automatic, human = [body["data"] for _, body in client.posts]
    assert automatic["input_id"] == "a" * 32
    assert automatic["source_id"] == "thread:auto-turn:vendor-item"
    assert "input_id" not in human
    assert human["source_id"] == "thread:auto-turn:item-2"
    assert first["id"] == "vendor-item"


@pytest.mark.parametrize(
    ("echoing_replay", "copies", "expected_duplicates"),
    [
        (True, ["resumed", "live"], [False, True]),
        (True, ["live", "resumed"], [False, True]),
        (True, ["resumed", "resumed", "live", "live"], [False, False, True, True]),
        (True, ["resumed", "live", "live"], [False, True, False]),
        (False, ["resumed", "live"], [False, False]),
        (False, ["live", "resumed", "resumed"], [False, True, False]),
    ],
    ids=[
        "replay_then_echo",
        "echo_then_replay",
        "repeated_input",
        "identical_steer_after_echo",
        "late_join_then_identical_steer",
        "live_then_late_join_replay",
    ],
)
def test_pair_user_message_copy_matches_each_resumed_input_once(
    echoing_replay: bool, copies: list[str], expected_duplicates: list[bool]
) -> None:
    """Each resumed copy pairs with at most one live copy of the same input."""
    state = fwd._CodexForwarderState(parent_session_id="conv_123")
    if echoing_replay:
        state.resume_echo_thread_ids.add("thread_123")
    key = ("thread_123", "turn_123", '[{"text": "Run the tests again.", "type": "text"}]')

    duplicates = [state.pair_user_message_copy(key, resumed=copy == "resumed") for copy in copies]

    assert duplicates == expected_duplicates


def test_user_message_copy_balance_keeps_unmatched_copies_until_turn_ends() -> None:
    """Paired copies and a finished turn's copies are not retained."""
    state = fwd._CodexForwarderState(parent_session_id="conv_123")
    state.resume_echo_thread_ids.add("thread_123")
    paired = ("thread_123", "turn_123", "digest_paired")
    unmatched = ("thread_123", "turn_123", "digest_unmatched")
    other_turn = ("thread_123", "turn_456", "digest_unmatched")

    state.pair_user_message_copy(paired, resumed=True)
    state.pair_user_message_copy(paired, resumed=False)
    state.pair_user_message_copy(unmatched, resumed=False)
    state.pair_user_message_copy(other_turn, resumed=False)
    assert state.user_message_copy_balance == {unmatched: 1, other_turn: 1}

    state.forget_user_message_copies("turn_123")
    assert state.user_message_copy_balance == {other_turn: 1}
