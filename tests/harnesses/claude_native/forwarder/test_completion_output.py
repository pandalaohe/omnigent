"""A Stop arriving between transcript polls must deliver its final report."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from omnigent.entities import ConversationItem, MessageData, PagedList
from omnigent.harnesses.claude_native import bridge, forwarder
from omnigent.server.routes._sessions.orchestration import (
    _enrich_terminal_status_with_subagent_output,
)
from omnigent.stores.conversation_store import ConversationStore


@pytest.mark.asyncio
@pytest.mark.parametrize("followup", ["absent", "mirrored", "post-retry"])
@pytest.mark.parametrize(
    "final_text",
    ["DONE: tests passed.", "Final report. " * 500, ""],
    ids=["final", "long-final", "empty-final"],
)
async def test_stop_delivers_final_output_before_transcript_catches_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    final_text: str,
    followup: str,
) -> None:
    """Exercise the real transcript, hook and status-enrichment functions offline."""
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    bridge_dir = tmp_path / "bridge"
    transcript = tmp_path / "session.jsonl"
    stored: list[ConversationItem] = []
    deliveries: list[dict] = []
    reject_followup_tail = followup == "post-retry"
    store = Mock(spec=ConversationStore)
    store.list_items.side_effect = lambda *args, **kwargs: PagedList(data=list(reversed(stored)))

    def append_message(role: str, source_id: str, text: str) -> None:
        with transcript.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": role,
                        "uuid": source_id,
                        "message": {"role": role, "content": [{"type": "text", "text": text}]},
                    }
                )
                + "\n"
            )

    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data = body["data"]
        if body["type"] == "external_conversation_item" and data["item_type"] == "message":
            if reject_followup_tail and data["source_id"].startswith("progress-2-tail:"):
                return httpx.Response(503)
            stored.append(
                ConversationItem(
                    id=data["source_id"],
                    type="message",
                    status="completed",
                    response_id=data["response_id"],
                    created_at=len(stored) + 1,
                    data=MessageData(**data["item_data"]),
                )
            )
        elif body["type"] == "external_session_status" and data["status"] == "idle":
            enriched = await _enrich_terminal_status_with_subagent_output(
                data, "idle", "conv_child", store
            )
            deliveries.append(enriched)
        return httpx.Response(200, json={})

    append_message("user", "user-1", "Implement the fix.")
    append_message("assistant", "progress-1", "Checking documentation before handing back.")
    bridge.record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-child",
            "transcript_path": str(transcript),
        },
    )
    bridge.record_hook_event(
        bridge_dir,
        {"hook_event_name": "UserPromptSubmit", "session_id": "claude-child"},
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript, line_cursor=0, byte_offset=0
    )
    dedupe = forwarder._ForwardDedupeState()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://ap"
    ) as client:

        async def forward_items() -> None:
            nonlocal state
            state = await forwarder._forward_available_items(
                client=client,
                session_id="conv_child",
                bridge_dir=bridge_dir,
                agent_name="developer",
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )

        await forward_items()
        first_response_id = state.current_response_id
        assert first_response_id is not None
        # Both records arrive after this poll's transcript snapshot, before its hooks phase.
        append_message("assistant", "final-1", final_text)
        bridge.record_hook_event(
            bridge_dir,
            {
                "hook_event_name": "Stop",
                "session_id": "claude-child",
                "transcript_path": str(transcript),
                "last_assistant_message": final_text,
            },
        )
        (_, stop) = bridge.read_hook_events_since_with_position(bridge_dir, 1).records
        assert stop.terminal_turn_id is not None and stop.terminal_input_id is not None
        if followup != "absent":
            bridge.record_hook_event(
                bridge_dir,
                {"hook_event_name": "UserPromptSubmit", "session_id": "claude-child"},
            )
            append_message("user", "user-2", "Continue with the follow-up.")
            append_message("assistant", "progress-2", "Progress on my next reply.")
            append_message("assistant", "progress-2-tail", "More progress.")
            await forward_items()
            restored = forwarder._read_forward_state(bridge_dir)
            assert restored is not None and restored == state
            assert restored.current_response_id not in (None, first_response_id)
            if reject_followup_tail:
                assert restored.byte_offset < stop.stop_transcript_offset
            state = restored
        hook_state = await forwarder._ensure_hook_state(
            bridge_dir, start_at_end=False, session_id="conv_child"
        )

        async def forward_hooks() -> None:
            nonlocal hook_state
            hook_state = await forwarder._forward_available_status_events(
                client=client,
                session_id="conv_child",
                bridge_dir=bridge_dir,
                state=hook_state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
                task_subjects={},
                task_statuses={},
                task_order=[],
                response_id=state.current_response_id,
                transcript_state=state,
            )

        await forward_hooks()
        assert len(deliveries) == 1
        delivery = deliveries[0]
        assert delivery["output"] == final_text
        assert delivery["response_id"] == first_response_id
        assert delivery["turn_id"] == stop.terminal_turn_id
        assert delivery["input_id"] == stop.terminal_input_id
        if followup != "absent":
            assert delivery["superseded_response"] is True
            assert dedupe.pending_settled_response_id is None
        else:
            assert "superseded_response" not in delivery
        # The later transcript poll must not cause another completion delivery.
        reject_followup_tail = False
        await forward_items()
        await forward_hooks()
        assert len(deliveries) == 1
        if final_text:
            assert any(
                isinstance(item.data, MessageData)
                and any(block.get("text") == final_text for block in item.data.content)
                for item in stored
            )


@pytest.mark.asyncio
async def test_stop_offset_inside_first_assistant_record_keeps_its_own_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    bridge_dir = tmp_path / "bridge"
    transcript = tmp_path / "session.jsonl"
    prompt = {"type": "user", "uuid": "prompt", "message": {"role": "user", "content": "hi"}}
    reply = {
        "type": "assistant",
        "uuid": "reply",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
    }
    start = forwarder.TranscriptForwardState(transcript, 0, byte_offset=0)
    reply_json = json.dumps(reply)
    transcript.write_text(json.dumps(prompt) + "\n" + reply_json[:10], encoding="utf-8")
    bridge.record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-child",
            "transcript_path": str(transcript),
            "last_assistant_message": "Done",
        },
    )
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(reply_json[10:] + "\n")
    [stop] = bridge.read_hook_events_since_with_position(bridge_dir, 0).records
    parsed = forwarder._read_transcript_items_for_state(start, "developer")
    state = forwarder.TranscriptForwardState(
        transcript,
        parsed.line_cursor,
        byte_offset=parsed.byte_offset,
        current_response_id=parsed.current_response_id,
        response_boundaries=forwarder._response_boundaries(start, parsed),
    )
    assert state.current_response_id is not None
    assert forwarder._response_at_stop(stop, state) == (state.current_response_id, False)
