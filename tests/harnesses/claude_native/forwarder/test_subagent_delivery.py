"""Subagent delivery tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    ClaudeTranscriptItem,
    TranscriptReadResult,
    TranscriptRecordItems,
    record_hook_event,
)
from omnigent.runner.transports.ws_tunnel.event_delivery import (
    RunnerEventDispatcher,
    TunnelEventClient,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _legacy_event_transport,
    _seed_subagent_on_disk,
    _start_recording_server_with_responses,
    _subagent_drop_row,
    _task_notification_record,
)


async def test_subagent_watcher_forwards_transcript_items_to_child_session(
    tmp_path: Path,
) -> None:
    """
    After registering a sub-agent, the forwarder tails its
    ``.jsonl`` and POSTs an array of ``external_conversation_item`` events to
    the Omnigent child session id (not the parent's).
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="b6d8fff",
        agent_type="Explore",
        description="Trace data flow",
        tool_use_id="toolu_abc",
        # Real sub-agent transcripts carry ``isSidechain: true`` on
        # every record (that's how Claude marks them as belonging to
        # a child instead of the main thread). The parser's default
        # behavior strips sidechain records, so without this flag
        # the watcher silently posts zero items — pin the real shape
        # here so a regression to that behavior fails this test.
        transcript_records=[
            {
                "isSidechain": True,
                "type": "user",
                "uuid": "sa-user-1",
                "message": {"role": "user", "content": "go"},
            },
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "sa-assistant-1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "looking now"}],
                },
            },
        ],
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )

    def response_for(body: object) -> object:
        """Mint a known child id for the start event.

        :param body: Decoded request body.
        :returns: Response payload.
        """
        if isinstance(body, list):
            return [
                {"queued": False, "item_id": f"item-{index}"} for index, _event in enumerate(body)
            ]
        if isinstance(body, dict) and body.get("type") == "external_subagent_start":
            return {"queued": False, "child_session_id": "conv_child_beta", "existing": False}
        return {}

    server, _thread, base_url = _start_recording_server_with_responses(response_for)
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # We need the start event plus one event array addressed to the child.
        child_path = "/v1/sessions/conv_child_beta/events"
        batch: list[dict[str, Any]] | None = None
        for _ in range(40):
            req = await _get_recorded_request(server)
            if req["path"] == child_path and isinstance(req["body"], list):
                batch = req["body"]
                break
        assert batch is not None
        assert len(batch) == 2
        assert all(event["type"] == "external_conversation_item" for event in batch)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()


async def test_subagent_watcher_retries_failed_batch_from_checkpoint(
    tmp_path: Path,
) -> None:
    """
    A rejected child batch leaves its byte cursor behind and retries in order.

    The server deduplicates source ids, so an ambiguous response can safely
    retry the entire batch even if some entries were already applied. The local
    cursor advances only after the acknowledgement arrives.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="retry1",
        agent_type="Explore",
        description="retry item flow",
        tool_use_id="toolu_retry",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "user",
                "uuid": "sa-user-retry",
                "message": {"role": "user", "content": "go"},
            },
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "sa-assistant-retry",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                },
            },
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "retry1": forwarder.SubagentEntry(
                subagent_id="retry1",
                child_conversation_id="conv_child_retry",
            )
        }
    )
    posted_items: list[str] = []
    batch_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail the first batch and acknowledge its retry.

        :param request: Request issued by the forwarder.
        :returns: Canned Omnigent response.
        """
        nonlocal batch_attempts
        body = json.loads(request.content.decode("utf-8"))
        if not isinstance(body, list):
            return httpx.Response(202, json={})
        batch_attempts += 1
        for event in body:
            row = event["data"]
            item_data = row["item_data"]
            posted_items.append(f"{item_data['role']}:{item_data['content'][0]['text']}")
        if batch_attempts == 1:
            return httpx.Response(503, json={"error": "try again"})
        return httpx.Response(
            202,
            json=[{"queued": False, "item_id": f"item-{index}"} for index, _ in enumerate(body)],
        )

    item_retry_tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        first = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=item_retry_tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=first,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=item_retry_tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert posted_items == ["user:go", "assistant:done", "user:go", "assistant:done"]
    child_state = second.subagents["retry1"]
    assert child_state.byte_offset == subagent_jsonl.stat().st_size
    assert set(child_state.seen_source_ids) == {
        "sa-user-retry:0:message",
        "sa-assistant-retry:0:message",
    }


def test_subagent_batches_obey_count_and_exact_byte_limits() -> None:
    """Batching counts the complete UTF-8 JSON body and truncates one huge item."""
    assert forwarder.MAX_SUBAGENT_EVENT_BATCH_BYTES == 5 * 1024 * 1024

    def pending(index: int, text: str) -> forwarder._PendingSubagentItem:
        return forwarder._PendingSubagentItem(
            item=ClaudeTranscriptItem(
                source_id=f"source-{index}",
                item_type="function_call_output",
                data={"call_id": f"toolu_{index}", "output": text},
                response_id="resp_batch",
            ),
            checkpoint_after=index + 1,
        )

    tiny_batches = forwarder._partition_subagent_batches(
        [pending(index, "ok") for index in range(205)]
    )
    assert [len(batch) for batch in tiny_batches] == [100, 100, 5]

    large_batches = forwarder._partition_subagent_batches(
        [pending(1000, "€" * 1_000_000), pending(1001, "€" * 1_000_000)]
    )
    assert [len(batch) for batch in large_batches] == [1, 1]

    oversized = forwarder._partition_subagent_batches([pending(2000, "€" * 2_000_000)])
    assert len(oversized) == 1
    truncated_output = oversized[0][0].item.data["output"]
    assert isinstance(truncated_output, str)
    assert "content truncated by omnigent" in truncated_output
    for batch in [*tiny_batches, *large_batches, *oversized]:
        assert (
            len(forwarder._encoded_subagent_batch(batch))
            <= forwarder.MAX_SUBAGENT_EVENT_BATCH_BYTES
        )


def test_oversized_subagent_item_does_not_truncate_identifiers() -> None:
    """Batch fitting never rewrites schema-significant identifier fields."""
    name = "n" * forwarder.MAX_SUBAGENT_EVENT_BATCH_BYTES
    entry = forwarder._PendingSubagentItem(
        item=ClaudeTranscriptItem(
            source_id="oversized-name",
            item_type="function_call",
            data={"agent": "claude", "name": name, "arguments": "{}", "call_id": "call-1"},
            response_id="resp-name",
        )
    )

    fitted = forwarder._fit_subagent_item(entry)

    assert fitted.drop_reason is not None
    assert fitted.item.data["name"] == name


@pytest.mark.parametrize(
    ("field_name", "kind"),
    [("input", "input"), ("stdout", "output"), ("stderr", "output")],
)
def test_oversized_subagent_terminal_text_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    kind: str,
) -> None:
    """Large terminal commands and output are shrunk instead of dropped."""
    monkeypatch.setattr(forwarder, "MAX_SUBAGENT_EVENT_BATCH_BYTES", 1024)
    entry = forwarder._PendingSubagentItem(
        item=ClaudeTranscriptItem(
            source_id=f"oversized-terminal-{field_name}",
            item_type="terminal_command",
            data={"kind": kind, field_name: "x" * 2048},
            response_id="resp-terminal",
        )
    )

    fitted = forwarder._fit_subagent_item(entry)

    assert fitted.drop_reason is None
    assert fitted.item.data["kind"] == kind
    terminal_text = fitted.item.data[field_name]
    assert isinstance(terminal_text, str)
    assert "content truncated by omnigent" in terminal_text
    assert len(forwarder._encoded_subagent_batch([fitted])) <= 1024


def test_subagent_batch_partitioning_encodes_items_linearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Byte accounting never re-encodes the growing batch prefix."""

    entries = [
        forwarder._PendingSubagentItem(
            item=ClaudeTranscriptItem(
                source_id=f"linear-{index}",
                item_type="message",
                data={"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                response_id="resp_linear",
            )
        )
        for index in range(20)
    ]
    encoded_item_count = 0
    original_encode = forwarder._encoded_subagent_batch

    def record_encode(items: list[forwarder._PendingSubagentItem]) -> bytes:
        nonlocal encoded_item_count
        encoded_item_count += len(items)
        return original_encode(items)

    monkeypatch.setattr(forwarder, "_encoded_subagent_batch", record_encode)

    batches = forwarder._partition_subagent_batches(entries)

    assert batches == [entries]
    assert encoded_item_count == 2 * len(entries)


@pytest.mark.asyncio
async def test_subagent_batch_partitioning_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child-history JSON sizing does not block the live forwarding loop."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    (subagents_dir / "agent-worker.jsonl").write_text("", encoding="utf-8")
    entry = forwarder.SubagentEntry(
        subagent_id="worker",
        child_conversation_id="conv_child_worker",
    )
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={"worker": entry}),
    )
    event_loop_thread = threading.current_thread()
    partition_threads: list[threading.Thread] = []
    original_partition = forwarder._partition_subagent_batches

    def record_partition(
        items: list[forwarder._PendingSubagentItem],
    ) -> list[list[forwarder._PendingSubagentItem]]:
        partition_threads.append(threading.current_thread())
        return original_partition(items)

    def reject_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(forwarder, "_partition_subagent_batches", record_partition)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(reject_request),
        base_url="http://ap",
    ) as client:
        await forwarder._forward_one_subagent(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            subagents_dir=subagents_dir,
            entry=entry,
            agent_name="claude-native-ui",
            checkpoint=checkpoint,
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            batch_capability=forwarder._SessionEventBatchCapability(),
            status_capability=forwarder._SubagentStatusCapability(),
        )

    assert partition_threads
    assert all(thread is not event_loop_thread for thread in partition_threads)


@pytest.mark.asyncio
async def test_untruncatable_subagent_item_is_dead_lettered_and_checkpointed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One impossible item cannot livelock every later child-history poll."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    (subagents_dir / "agent-oversized.jsonl").write_text("{}\n", encoding="utf-8")
    item = ClaudeTranscriptItem(
        source_id="oversized-untruncatable",
        item_type="message",
        data={"x" * forwarder.MAX_SUBAGENT_EVENT_BATCH_BYTES: 1},
        response_id="resp_oversized",
    )
    read_result = TranscriptReadResult(
        line_cursor=1,
        byte_offset=3,
        current_response_id=None,
        items=[item],
        record_items=(TranscriptRecordItems(next_byte_offset=3, items=(item,)),),
    )
    monkeypatch.setattr(
        forwarder,
        "read_transcript_items_from_offset",
        lambda *args, **kwargs: read_result,
    )
    entry = forwarder.SubagentEntry(
        subagent_id="oversized",
        child_conversation_id="conv_child_oversized",
    )
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={"oversized": entry}),
    )

    def reject_request(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "type": "external_session_status",
            "data": {"status": "activity_unverified", "replayed": True},
        }
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(reject_request),
        base_url="http://ap",
    ) as client:
        await forwarder._forward_one_subagent(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            subagents_dir=subagents_dir,
            entry=entry,
            agent_name="claude-native-ui",
            checkpoint=checkpoint,
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            batch_capability=forwarder._SessionEventBatchCapability(),
            status_capability=forwarder._SubagentStatusCapability(),
        )

    updated = checkpoint.state.subagents["oversized"]
    assert updated.byte_offset == 3
    assert updated.seen_source_ids == (item.source_id,)
    dead_letter = json.loads(
        (bridge_dir / "dead_letter.jsonl").read_text(encoding="utf-8").strip()
    )
    assert dead_letter["payload"]["source_id"] == item.source_id
    assert "no truncatable text" in dead_letter["reason"]
    assert dead_letter["http_status"] == 413

    row = _subagent_drop_row(caplog)
    assert row["session_id"] == "conv_child_oversized"
    assert row["attributes"]["parent_session_id"] == "conv_parent"
    assert row["attributes"]["drop_reason"] == "oversized_item"
    assert row["attributes"]["item_count"] == "1"
    assert row["attributes"]["http_status"] == "413"
    assert row["attributes"]["response_id"] == "resp_oversized"
    assert "exception_type" not in row["attributes"]
    assert "x" * 100 not in json.dumps(row["attributes"])


@pytest.mark.asyncio
async def test_subagent_batches_fall_back_once_for_older_server(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A server that rejects event arrays receives individual events thereafter."""
    transcript = tmp_path / "parent.jsonl"
    transcript.write_text("", encoding="utf-8")
    entries = {}
    for child, indices in (("one", (1, 2)), ("two", (3, 4))):
        _seed_subagent_on_disk(
            transcript_path=transcript,
            subagent_id=child,
            agent_type="Explore",
            description="fallback",
            tool_use_id=f"toolu_{child}",
            transcript_records=[
                {
                    "isSidechain": True,
                    "type": "assistant",
                    "uuid": f"fallback-{index}",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": str(index)}],
                    },
                }
                for index in indices
            ],
        )
        entries[child] = forwarder.SubagentEntry(
            subagent_id=child, child_conversation_id=f"conv_child_{child}"
        )
    bridge_dir = tmp_path / "bridge"
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir, forwarder.SubagentForwardState(subagents=entries)
    )
    bodies: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if isinstance(body, list):
            return httpx.Response(
                422,
                json={
                    "detail": [
                        {
                            "type": "model_attributes_type",
                            "loc": ["body"],
                            "msg": "Input should be a valid dictionary",
                        }
                    ]
                },
            )
        return httpx.Response(202, json={"queued": False, "item_id": "item_fallback"})

    capability = forwarder._SessionEventBatchCapability()
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        for entry in entries.values():
            await forwarder._forward_one_subagent(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                subagents_dir=forwarder._subagents_dir_for_transcript(transcript),
                entry=entry,
                agent_name="claude-native-ui",
                checkpoint=checkpoint,
                item_retry_tracker=forwarder._PostRetryTracker(),
                status_retry_tracker=forwarder._PostRetryTracker(),
                batch_capability=capability,
                status_capability=forwarder._SubagentStatusCapability(),
            )

    assert capability.supported is False
    assert len([body for body in bodies if isinstance(body, list)]) == 1
    individual_texts = {
        body["data"]["item_data"]["content"][0]["text"]
        for body in bodies
        if isinstance(body, dict) and body["type"] == "external_conversation_item"
    }
    assert individual_texts == {"1", "2", "3", "4"}
    assert all(entry.byte_offset > 0 for entry in checkpoint.state.subagents.values())
    assert "does not accept session event arrays" in caplog.text


@pytest.mark.asyncio
async def test_subagent_batch_failure_resumes_at_first_unsent_record(tmp_path: Path) -> None:
    """A failed second batch keeps the durable cursor after record 100."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    records = [
        {
            "isSidechain": True,
            "type": "assistant",
            "uuid": f"sa-{index}",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"item {index}"}],
            },
        }
        for index in range(150)
    ]
    subagent_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="checkpoint",
        agent_type="Explore",
        description="large history",
        tool_use_id="toolu_checkpoint",
        transcript_records=records,
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "checkpoint": forwarder.SubagentEntry(
                subagent_id="checkpoint",
                child_conversation_id="conv_child_checkpoint",
            )
        }
    )
    attempts: list[list[str]] = []
    failed_second_batch = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed_second_batch
        body = json.loads(request.content.decode("utf-8"))
        if not isinstance(body, list):
            return httpx.Response(202, json={})
        source_ids = [event["data"]["source_id"] for event in body]
        attempts.append(source_ids)
        if source_ids[0].startswith("sa-100:") and not failed_second_batch:
            failed_second_batch = True
            return httpx.Response(503, json={"error": "retry"})
        return httpx.Response(
            202,
            json=[{"queued": False, "item_id": f"item-{index}"} for index, _ in enumerate(body)],
        )

    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        first = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        with subagent_jsonl.open("rb") as handle:
            expected_offset = sum(len(handle.readline()) for _ in range(100))
        assert first.subagents["checkpoint"].byte_offset == expected_offset
        persisted = forwarder._read_subagent_forward_state(bridge_dir)
        assert persisted.subagents["checkpoint"].byte_offset == expected_offset

        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=first,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert second.subagents["checkpoint"].byte_offset == subagent_jsonl.stat().st_size
    attempted_ids = [source_id for batch in attempts for source_id in batch]
    assert all(attempted_ids.count(f"sa-{index}:0:message") == 1 for index in range(100))
    assert all(attempted_ids.count(f"sa-{index}:0:message") == 2 for index in range(100, 150))


@pytest.mark.asyncio
async def test_permanent_batch_failure_redrives_items_individually(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poison event cannot discard valid siblings from a failed batch."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    (subagents_dir / "agent-redrive.jsonl").write_text("{}\n", encoding="utf-8")
    items = tuple(
        ClaudeTranscriptItem(
            source_id=source_id,
            item_type="message",
            data={"role": "assistant", "content": [{"type": "text", "text": source_id}]},
            response_id="resp_redrive",
        )
        for source_id in ("before-poison", "poison", "after-poison")
    )
    read_result = TranscriptReadResult(
        line_cursor=3,
        byte_offset=30,
        current_response_id=None,
        items=list(items),
        record_items=tuple(
            TranscriptRecordItems(next_byte_offset=(index + 1) * 10, items=(item,))
            for index, item in enumerate(items)
        ),
    )
    monkeypatch.setattr(
        forwarder,
        "read_transcript_items_from_offset",
        lambda *args, **kwargs: read_result,
    )
    entry = forwarder.SubagentEntry(
        subagent_id="redrive",
        child_conversation_id="conv_child_redrive",
    )
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={"redrive": entry}),
    )
    request_bodies: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        request_bodies.append(body)
        if isinstance(body, list):
            return httpx.Response(400, json={"error": "poison in batch"})
        if body["type"] == "external_conversation_item":
            if body["data"]["source_id"] == "poison":
                return httpx.Response(400, json={"error": "poison"})
            return httpx.Response(202, json={"queued": False, "item_id": "item-ok"})
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        await forwarder._forward_one_subagent(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            subagents_dir=subagents_dir,
            entry=entry,
            agent_name="claude-native-ui",
            checkpoint=checkpoint,
            item_retry_tracker=forwarder._PostRetryTracker(
                base_delay_s=0.0,
                max_permanent_attempts=1,
            ),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            batch_capability=forwarder._SessionEventBatchCapability(),
            status_capability=forwarder._SubagentStatusCapability(),
        )

    individual_source_ids = [
        body["data"]["source_id"]
        for body in request_bodies
        if isinstance(body, dict) and body.get("type") == "external_conversation_item"
    ]
    assert individual_source_ids == ["before-poison", "poison", "after-poison"]
    updated = checkpoint.state.subagents["redrive"]
    assert updated.byte_offset == 30
    assert updated.seen_source_ids == tuple(item.source_id for item in items)
    dead_letters = [
        json.loads(line)
        for line in (bridge_dir / "dead_letter.jsonl").read_text("utf-8").splitlines()
    ]
    assert [record["payload"]["source_id"] for record in dead_letters] == ["poison"]


@pytest.mark.asyncio
async def test_individual_redrive_honors_not_confirmed_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fallback item is retried before a not-confirmed 503 is dead-lettered."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    (subagents_dir / "agent-retry.jsonl").write_text("{}\n", encoding="utf-8")
    item = ClaudeTranscriptItem(
        source_id="retry-not-confirmed",
        item_type="message",
        data={"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        response_id="resp-retry",
    )
    read_result = TranscriptReadResult(
        line_cursor=1,
        byte_offset=10,
        current_response_id=None,
        items=[item],
        record_items=(TranscriptRecordItems(next_byte_offset=10, items=(item,)),),
    )
    monkeypatch.setattr(
        forwarder,
        "read_transcript_items_from_offset",
        lambda *args, **kwargs: read_result,
    )
    entry = forwarder.SubagentEntry(
        subagent_id="retry",
        child_conversation_id="conv_child_retry",
    )
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={"retry": entry}),
    )
    batch_attempts = 0
    individual_attempts = 0
    forward_successes = 0

    def note_forward_success() -> None:
        nonlocal forward_successes
        forward_successes += 1

    monkeypatch.setattr(forwarder, "_note_forward_success", note_forward_success)
    monkeypatch.setattr(forwarder, "_publish_subagent_status", AsyncMock())

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal batch_attempts, individual_attempts
        body = json.loads(request.content.decode("utf-8"))
        if isinstance(body, dict) and body.get("type") == "external_session_status":
            return httpx.Response(202, json={})
        if isinstance(body, list):
            batch_attempts += 1
        else:
            individual_attempts += 1
        return httpx.Response(
            503,
            json={"error": "subagent_delivery_not_confirmed"},
        )

    retry_tracker = forwarder._PostRetryTracker(
        base_delay_s=0.0,
        max_permanent_attempts=1,
        max_not_confirmed_attempts=2,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for _ in range(3):
            await forwarder._forward_one_subagent(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                subagents_dir=subagents_dir,
                entry=checkpoint.state.subagents["retry"],
                agent_name="claude-native-ui",
                checkpoint=checkpoint,
                item_retry_tracker=retry_tracker,
                status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                batch_capability=forwarder._SessionEventBatchCapability(),
                status_capability=forwarder._SubagentStatusCapability(),
            )

    assert batch_attempts == 2
    assert individual_attempts == 2
    assert forward_successes == 0
    assert checkpoint.state.subagents["retry"].byte_offset == 10
    dead_letters = [
        json.loads(line)
        for line in (bridge_dir / "dead_letter.jsonl").read_text("utf-8").splitlines()
    ]
    assert [record["payload"]["source_id"] for record in dead_letters] == [item.source_id]
    assert dead_letters[0]["reason"] == "delivery not confirmed after retries"

    row = _subagent_drop_row(caplog)
    assert row["session_id"] == "conv_child_retry"
    assert row["attributes"]["drop_reason"] == "delivery_not_confirmed"
    assert row["attributes"]["http_status"] == "503"
    assert row["attributes"]["attempts"] == "2"
    assert row["attributes"]["exception_type"] == "HTTPStatusError"


@pytest.mark.asyncio
async def test_subagent_batch_backoff_survives_new_tail_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Appending later items cannot reset backoff for the failing head item."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    (subagents_dir / "agent-backoff.jsonl").write_text("{}\n", encoding="utf-8")

    def transcript_item(source_id: str) -> ClaudeTranscriptItem:
        return ClaudeTranscriptItem(
            source_id=source_id,
            item_type="message",
            data={"role": "assistant", "content": [{"type": "text", "text": source_id}]},
            response_id="resp-backoff",
        )

    first = transcript_item("first")
    second = transcript_item("second")
    current_result = TranscriptReadResult(
        line_cursor=1,
        byte_offset=10,
        current_response_id=None,
        items=[first],
        record_items=(TranscriptRecordItems(next_byte_offset=10, items=(first,)),),
    )
    read_calls = 0

    def read_items(*args: object, **kwargs: object) -> TranscriptReadResult:
        nonlocal read_calls
        read_calls += 1
        return current_result

    monkeypatch.setattr(forwarder, "read_transcript_items_from_offset", read_items)
    entry = forwarder.SubagentEntry(
        subagent_id="backoff",
        child_conversation_id="conv_child_backoff",
    )
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={"backoff": entry}),
    )
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(502, text="unavailable")

    retry_tracker = forwarder._PostRetryTracker(base_delay_s=60.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await forwarder._forward_one_subagent(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            subagents_dir=subagents_dir,
            entry=entry,
            agent_name="claude-native-ui",
            checkpoint=checkpoint,
            item_retry_tracker=retry_tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            batch_capability=forwarder._SessionEventBatchCapability(),
            status_capability=forwarder._SubagentStatusCapability(),
        )
        current_result = TranscriptReadResult(
            line_cursor=2,
            byte_offset=20,
            current_response_id=None,
            items=[first, second],
            record_items=(
                TranscriptRecordItems(next_byte_offset=10, items=(first,)),
                TranscriptRecordItems(next_byte_offset=20, items=(second,)),
            ),
        )
        await forwarder._forward_one_subagent(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            subagents_dir=subagents_dir,
            entry=entry,
            agent_name="claude-native-ui",
            checkpoint=checkpoint,
            item_retry_tracker=retry_tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            batch_capability=forwarder._SessionEventBatchCapability(),
            status_capability=forwarder._SubagentStatusCapability(),
        )

    assert requests == 1
    assert read_calls == 1


@pytest.mark.asyncio
async def test_subagent_cleanup_swallows_finished_worker_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rotation cleanup cannot re-raise an already-finished worker error."""

    async def fail() -> forwarder.SubagentForwardState:
        raise RuntimeError("worker failed")

    task = asyncio.create_task(fail())
    await asyncio.sleep(0)

    await forwarder._cancel_subagent_forward_task(task)

    assert "worker failed during cleanup" in caplog.text


@pytest.mark.asyncio
async def test_subagent_history_drains_eight_children_concurrently(tmp_path: Path) -> None:
    """Independent child conversations are concurrent while each stays ordered."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    entries: dict[str, forwarder.SubagentEntry] = {}
    for index in range(16):
        subagent_id = f"parallel-{index}"
        _seed_subagent_on_disk(
            transcript_path=transcript_path,
            subagent_id=subagent_id,
            agent_type="Explore",
            description="parallel backlog",
            tool_use_id=f"toolu_parallel_{index}",
            transcript_records=[
                {
                    "isSidechain": True,
                    "type": "assistant",
                    "uuid": f"parallel-message-{index}",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": str(index)}],
                    },
                }
            ],
        )
        entries[subagent_id] = forwarder.SubagentEntry(
            subagent_id=subagent_id,
            child_conversation_id=f"conv_child_{index}",
        )

    active = 0
    maximum_active = 0
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        body = json.loads(request.content.decode("utf-8"))
        if not isinstance(body, list):
            return httpx.Response(202, json={})
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 8:
            release.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return httpx.Response(
            202,
            json=[{"queued": False, "item_id": f"item-{index}"} for index, _ in enumerate(body)],
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await asyncio.wait_for(
            forwarder._forward_available_subagents(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=forwarder.SubagentForwardState(subagents=entries),
                agent_name="claude-native-ui",
                start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            ),
            timeout=3.0,
        )
    assert maximum_active == 8


@pytest.mark.asyncio
async def test_concurrent_subagent_502s_recover_without_phantom_completion(
    tmp_path: Path,
) -> None:
    """A failed fan-out retries every child before any child can finish idle."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    old_activity = time.time() - forwarder._SUBAGENT_IDLE_THRESHOLD_S - 60
    entries: dict[str, forwarder.SubagentEntry] = {}
    for index in range(5):
        subagent_id = f"recover-{index}"
        _seed_subagent_on_disk(
            transcript_path=transcript_path,
            subagent_id=subagent_id,
            agent_type="Explore",
            description="concurrent retry",
            tool_use_id=f"toolu_recover_{index}",
            transcript_records=[
                {
                    "isSidechain": True,
                    "type": "assistant",
                    "uuid": f"recover-message-{index}",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": str(index)}],
                    },
                }
            ],
        )
        entries[subagent_id] = forwarder.SubagentEntry(
            subagent_id=subagent_id,
            child_conversation_id=f"conv_recover_{index}",
            last_activity_ts=old_activity,
            last_status="running",
        )

    attempts: dict[str, int] = {}
    statuses: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        child_id = request.url.path.split("/")[-2]
        if isinstance(body, list):
            attempts[child_id] = attempts.get(child_id, 0) + 1
            if attempts[child_id] == 1:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(202, json=[{"item_id": f"item-{child_id}"}])
        if body.get("type") in {"external_session_status", "subagent.status"}:
            statuses.append((child_id, body))
        return httpx.Response(202, json={})

    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    state = forwarder.SubagentForwardState(subagents=entries)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        assert statuses == []
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        quiet_entries = {
            subagent_id: replace(entry, last_activity_ts=old_activity)
            for subagent_id, entry in state.subagents.items()
        }
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents=quiet_entries),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert set(attempts.values()) == {2}
    assert sorted(child_id for child_id, _ in statuses) == [f"conv_recover_{i}" for i in range(5)]
    assert [body for _, body in statuses] == [
        {"type": "subagent.status", "data": {"idle": True}}
    ] * 5
    assert all(entry.delivery_error is None for entry in state.subagents.values())


@pytest.mark.asyncio
async def test_subagent_item_drop_writes_dead_letter(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A permanently-rejected sub-agent transcript item is dead-lettered (#1120).

    Drives the real ``_forward_available_subagents`` drop path: the
    ``external_subagent_start`` POST succeeds, the child item POST is rejected
    with a permanent 400 (and the item tracker exhausts on the first failure),
    so the dropped item is appended to ``{bridge_dir}/dead_letter.jsonl`` instead
    of being silently lost.

    :param tmp_path: Pytest temp dir for the bridge dir and transcript.
    """
    forwarder._reset_forward_health()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="dl1",
        agent_type="Explore",
        description="dead-letter item flow",
        tool_use_id="toolu_dl",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "sa-assistant-dl",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "lost"}],
                },
            },
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        """Accept the start POST; permanently reject the child item POST.

        :param request: Request issued by the forwarder.
        :returns: Canned Omnigent response.
        """
        body = json.loads(request.content.decode("utf-8"))
        if isinstance(body, dict) and body.get("type") == "external_subagent_start":
            return httpx.Response(
                200, json={"child_session_id": "conv_child_dl", "existing": False}
            )
        if isinstance(body, list) or body.get("type") == "external_conversation_item":
            return httpx.Response(400, json={"error": "nope"})
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(
                base_delay_s=0.0, max_permanent_attempts=1
            ),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    forwarder._reset_forward_health()
    dl_path = bridge_dir / "dead_letter.jsonl"
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "conv_child_dl"
    assert record["event_type"] == "external_conversation_item"
    assert record["payload"]["item_data"]["content"][0]["text"] == "lost"

    row = _subagent_drop_row(caplog)
    assert row["session_id"] == "conv_child_dl"
    assert row["attributes"]["parent_session_id"] == "conv_parent"
    assert row["attributes"]["drop_reason"] == "permanent_http_failure"
    assert row["attributes"]["http_status"] == "400"
    assert row["attributes"]["attempts"] == "1"
    assert row["attributes"]["exception_type"] == "HTTPStatusError"
    assert "lost" not in json.dumps(row["attributes"])


@pytest.mark.asyncio
async def test_subagent_start_drop_writes_dead_letter(tmp_path: Path) -> None:
    """
    A permanently-rejected sub-agent START is dead-lettered (#1120).

    :param tmp_path: Pytest temp dir for the bridge dir and transcript.
    """
    forwarder._reset_forward_health()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="dlstart1",
        agent_type="Explore",
        description="dead-letter start flow",
        tool_use_id="toolu_dlstart",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        """Permanently reject the sub-agent start POST.

        :param request: Request issued by the forwarder.
        :returns: Canned Omnigent response.
        """
        return httpx.Response(400, json={"error": "nope"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(
                base_delay_s=0.0, max_permanent_attempts=1
            ),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    forwarder._reset_forward_health()
    dl_path = bridge_dir / "dead_letter.jsonl"
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "conv_parent"
    assert record["event_type"] == "external_subagent_start"
    assert record["payload"]["subagent_id"] == "dlstart1"
    assert record["payload"]["agent_type"] == "Explore"


@pytest.mark.asyncio
async def test_timed_out_batch_is_split_not_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch whose POST never got a response is re-driven item by item.

    A read timeout on a 100-item batch is usually the batch's own size against
    the flat post timeout, so retrying the same payload cannot clear it. The
    server never rejected the items, so they must be split rather than
    dead-lettered.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    (subagents_dir / "agent-split.jsonl").write_text("{}\n", encoding="utf-8")
    items = [
        ClaudeTranscriptItem(
            source_id=f"item-{index}",
            item_type="message",
            data={"role": "assistant", "content": [{"type": "text", "text": f"m{index}"}]},
            response_id="resp-split",
        )
        for index in range(3)
    ]
    read_result = TranscriptReadResult(
        line_cursor=1,
        byte_offset=30,
        current_response_id=None,
        items=items,
        record_items=(TranscriptRecordItems(next_byte_offset=30, items=tuple(items)),),
    )
    monkeypatch.setattr(
        forwarder,
        "read_transcript_items_from_offset",
        lambda *args, **kwargs: read_result,
    )
    entry = forwarder.SubagentEntry(
        subagent_id="split",
        child_conversation_id="conv_child_split",
    )
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={"split": entry}),
    )
    batch_attempts = 0
    individual_source_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal batch_attempts
        body = json.loads(request.content.decode("utf-8"))
        if isinstance(body, list):
            batch_attempts += 1
            raise httpx.ReadTimeout("batch too large for the post budget", request=request)
        if isinstance(body, dict) and body.get("type") == "external_conversation_item":
            individual_source_ids.append(body["data"]["source_id"])
        return httpx.Response(204)

    retry_tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    monkeypatch.setattr(forwarder, "_SUBAGENT_BATCH_MAX_TRANSIENT_ATTEMPTS", 2)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for _ in range(3):
            await forwarder._forward_one_subagent(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                subagents_dir=subagents_dir,
                entry=checkpoint.state.subagents["split"],
                agent_name="claude-native-ui",
                checkpoint=checkpoint,
                item_retry_tracker=retry_tracker,
                status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                batch_capability=forwarder._SessionEventBatchCapability(),
                status_capability=forwarder._SubagentStatusCapability(),
            )

    assert batch_attempts == 2
    assert individual_source_ids == [item.source_id for item in items]
    assert not (bridge_dir / "dead_letter.jsonl").exists()
    updated = checkpoint.state.subagents["split"]
    assert updated.byte_offset == 30
    assert updated.seen_source_ids == tuple(item.source_id for item in items)


async def test_subagent_watcher_replay_does_not_reopen_historical_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold history advances its cursor; only a newly accepted item starts work."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="replay1",
        agent_type="Explore",
        description="historical child",
        tool_use_id="toolu_replay",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "user",
                "uuid": "historical-user",
                "message": {"role": "user", "content": "old prompt"},
            },
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "historical-assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "old answer"}],
                },
            },
        ],
    )
    monkeypatch.setattr(forwarder, "_SUBAGENT_RECOVERY_BATCH_ITEMS", 1)
    item_requests: list[dict[str, Any]] = []
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        event_type = body.get("type")
        if event_type == "external_subagent_start":
            return httpx.Response(
                202,
                json={"child_session_id": "conv_child_replay", "existing": True},
            )
        if event_type == "external_conversation_item":
            item_requests.append(body["data"])
            if "recovery_after" in body["data"]:
                return httpx.Response(
                    202,
                    json={
                        "item_id": f"server-old-{len(item_requests)}",
                        "replayed": True,
                        "recovery": True,
                    },
                )
            return httpx.Response(202, json={"replayed": False})
        if event_type == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    now = [1_000.0]
    monkeypatch.setattr(forwarder.time, "time", lambda: now[0])
    trackers = {
        "start_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        "item_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        "status_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
    }
    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler),
        base_url="http://ap",
    ) as client:
        first = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            **trackers,
        )

        first_entry = first.subagents["replay1"]
        assert first_entry.byte_offset == 0
        assert first_entry.recovery_watermark == subagent_jsonl.stat().st_size
        assert first_entry.recovery_after == "server-old-1"
        assert first_entry.last_activity_ts is None
        assert first_entry.last_status is None
        assert status_posts == []

        reloaded = forwarder._read_subagent_forward_state(bridge_dir)
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=reloaded,
            agent_name="claude-native-ui",
            **trackers,
        )
        second_entry = second.subagents["replay1"]
        assert second_entry.byte_offset == subagent_jsonl.stat().st_size
        assert second_entry.recovery_watermark is None
        assert second_entry.last_activity_ts is None
        assert second_entry.last_status is None
        assert status_posts == []

        with subagent_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "isSidechain": True,
                        "type": "assistant",
                        "uuid": "live-assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "new live output"}],
                        },
                    }
                )
                + "\n"
            )
        now[0] = 2_000.0
        third = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=second,
            agent_name="claude-native-ui",
            **trackers,
        )

    third_entry = third.subagents["replay1"]
    assert third_entry.byte_offset == subagent_jsonl.stat().st_size
    assert third_entry.last_activity_ts == 2_000.0
    assert third_entry.last_status == "running"
    assert status_posts == [{"status": "running"}]
    assert [request["source_id"] for request in item_requests] == [
        "historical-user:0:message",
        "historical-assistant:0:message",
        "live-assistant:0:message",
    ]
    assert [request.get("recovery_after", "live") for request in item_requests] == [
        None,
        "server-old-1",
        "live",
    ]


async def test_subagent_history_recovery_accepts_skipped_reasoning_without_advancing_chain(
    tmp_path: Path,
) -> None:
    """A Server-skipped legacy thought is seen but does not invent a chain cursor."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    child_path = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="skippedreasoning1",
        agent_type="Explore",
        description="legacy reasoning child",
        tool_use_id="toolu_skipped_reasoning",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "historical-before",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "old leading answer"}],
                },
            },
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "historical-thinking",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "old private thought"}],
                },
            },
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "historical-answer",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "old visible answer"}],
                },
            },
        ],
    )
    recovery_requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_subagent_start":
            return httpx.Response(
                202,
                json={"child_session_id": "conv_child_skipped_reasoning", "existing": True},
            )
        if body.get("type") == "external_conversation_item":
            recovery_requests.append(body["data"])
            if body["data"]["item_type"] == "reasoning":
                return httpx.Response(
                    202,
                    json={
                        "queued": False,
                        "item_id": None,
                        "replayed": True,
                        "recovery": True,
                        "skipped": True,
                    },
                )
            return httpx.Response(
                202,
                json={
                    "item_id": f"server-visible-{len(recovery_requests)}",
                    "replayed": True,
                    "recovery": True,
                },
            )
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler), base_url="http://ap"
    ) as client:
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    entry = state.subagents["skippedreasoning1"]
    assert entry.byte_offset == child_path.stat().st_size
    assert entry.recovery_watermark is None
    assert entry.recovery_after is None
    assert entry.recovery_seen_source_ids == ()
    assert [request["source_id"] for request in recovery_requests] == [
        "historical-before:0:message",
        "historical-thinking:0:reasoning",
        "historical-answer:0:message",
    ]
    assert [request["recovery_after"] for request in recovery_requests] == [
        None,
        "server-visible-1",
        "server-visible-1",
    ]


@pytest.mark.parametrize(
    ("item_type", "response_body"),
    [
        (
            "reasoning",
            {
                "queued": False,
                "item_id": "unexpected-id",
                "replayed": True,
                "recovery": True,
                "skipped": True,
            },
        ),
        (
            "reasoning",
            {
                "queued": True,
                "item_id": None,
                "replayed": True,
                "recovery": True,
                "skipped": True,
            },
        ),
        ("reasoning", {"item_id": None, "replayed": True, "recovery": True}),
        (
            "reasoning",
            {"queued": False, "replayed": True, "recovery": True, "skipped": True},
        ),
        (
            "message",
            {
                "queued": False,
                "item_id": None,
                "replayed": True,
                "recovery": True,
                "skipped": True,
            },
        ),
    ],
)
async def test_subagent_history_recovery_rejects_malformed_skipped_ack(
    item_type: str,
    response_body: dict[str, Any],
) -> None:
    """Only the exact skipped acknowledgement can suppress a historical item."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[response_body])

    item = ClaudeTranscriptItem(
        source_id=f"historical-item:0:{item_type}",
        item_type=item_type,
        data={"agent": "claude-native-ui", "summary": [], "content": []},
        response_id="resp_history",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        with pytest.raises(RuntimeError, match="did not confirm the chain"):
            await forwarder._post_external_recovery_item(
                client,
                session_id="conv_child",
                item=item,
                recovery_after=None,
            )


async def test_subagent_history_recovery_409_keeps_cursor_for_retry(tmp_path: Path) -> None:
    """A chain mismatch never skips or dead-letters historical child output."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    child_path = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="mismatch1",
        agent_type="Explore",
        description="existing child",
        tool_use_id="toolu_mismatch",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "historical-mismatch",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "old result"}],
                },
            }
        ],
    )
    recovery_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal recovery_attempts
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_subagent_start":
            return httpx.Response(
                202,
                json={"child_session_id": "conv_child_mismatch", "existing": True},
            )
        if body.get("type") == "external_conversation_item":
            recovery_attempts += 1
            if recovery_attempts == 1:
                return httpx.Response(409, json={"error": "recovery chain mismatch"})
            return httpx.Response(
                202,
                json={"item_id": "server-retried", "replayed": True, "recovery": True},
            )
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler), base_url="http://ap"
    ) as client:
        first = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(
                base_delay_s=0.0, max_permanent_attempts=1
            ),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        reloaded = forwarder._read_subagent_forward_state(bridge_dir)
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=reloaded,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    entry = first.subagents["mismatch1"]
    assert entry.byte_offset == 0
    assert entry.recovery_watermark == child_path.stat().st_size
    assert entry.recovery_after is None
    assert entry.recovery_seen_source_ids == ()
    assert not (bridge_dir / "dead_letter.jsonl").exists()
    assert recovery_attempts == 2
    assert second.subagents["mismatch1"].byte_offset == child_path.stat().st_size
    assert second.subagents["mismatch1"].recovery_watermark is None


def _seed_recovery_child(
    tmp_path: Path,
    *,
    subagent_id: str,
    child_id: str,
) -> tuple[Path, Path, forwarder.SubagentEntry]:
    """Create one existing child frozen at a recovery watermark."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    child_path = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id=subagent_id,
        agent_type="Explore",
        description="history recovery",
        tool_use_id=f"toolu_{subagent_id}",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": f"historical-{subagent_id}",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "old answer"}],
                },
            }
        ],
    )
    entry = forwarder.SubagentEntry(
        subagent_id=subagent_id,
        child_conversation_id=child_id,
        recovery_watermark=child_path.stat().st_size,
    )
    return bridge_dir, child_path, entry


async def _recover_once(
    client: httpx.AsyncClient,
    *,
    bridge_dir: Path,
    entry: forwarder.SubagentEntry,
    child_path: Path,
    tracker: forwarder._PostRetryTracker,
) -> forwarder.SubagentEntry | None:
    """Run one frozen-history reconciliation pass over ``entry``."""
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={entry.subagent_id: entry}),
    )
    return await forwarder._recover_subagent_history(
        client=client,
        entry=entry,
        jsonl_path=child_path,
        agent_name="claude-native-ui",
        checkpoint=checkpoint,
        item_retry_tracker=tracker,
    )


async def test_subagent_recovery_post_is_http_array_with_item_id_ack() -> None:
    """Recovery posts a one-element array over HTTP; the tunnel ack has no item id."""
    bodies: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            202,
            json=[
                {
                    "queued": False,
                    "item_id": "server-recovered",
                    "replayed": True,
                    "recovery": True,
                }
            ],
        )

    frames: list[str] = []

    async def send(frame: str) -> None:
        frames.append(frame)

    dispatcher = RunnerEventDispatcher()
    dispatcher.ready(send)
    item = ClaudeTranscriptItem(
        source_id="historical-item:0:message",
        item_type="message",
        data={"role": "assistant", "content": [{"type": "text", "text": "old"}]},
        response_id="resp_history",
    )
    async with TunnelEventClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
        event_dispatcher=dispatcher,
    ) as client:
        item_id = await forwarder._post_external_recovery_item(
            client,
            session_id="conv_child",
            item=item,
            recovery_after="server-prior",
        )

    assert item_id == "server-recovered"
    assert len(bodies) == 1
    assert isinstance(bodies[0], list)
    assert len(bodies[0]) == 1
    assert bodies[0][0]["type"] == "external_conversation_item"
    assert bodies[0][0]["data"]["recovery_after"] == "server-prior"
    assert frames == []


async def test_subagent_history_recovery_parks_after_permanent_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permanently rejected history item is parked, never retried per poll."""
    bridge_dir, child_path, entry = _seed_recovery_child(
        tmp_path, subagent_id="park1", child_id="conv_child_park"
    )
    posts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(403, json={"detail": "forbidden"})

    real_monotonic = time.monotonic
    clock_offset = [0.0]
    monkeypatch.setattr(forwarder.time, "monotonic", lambda: real_monotonic() + clock_offset[0])
    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:

        async def poll() -> forwarder.SubagentEntry | None:
            return await _recover_once(
                client,
                bridge_dir=bridge_dir,
                entry=entry,
                child_path=child_path,
                tracker=tracker,
            )

        for _ in range(20):
            assert await poll() is None
        assert posts == 3
        assert entry.recovery_watermark == child_path.stat().st_size
        assert entry.byte_offset == 0
        assert not (bridge_dir / "dead_letter.jsonl").exists()

        clock_offset[0] += forwarder._SUBAGENT_RECOVERY_PARK_S + 1.0
        assert await poll() is None
        assert posts == 4
        assert await poll() is None
        assert posts == 4


async def test_subagent_history_recovery_parks_unconfirmed_ack(tmp_path: Path) -> None:
    """An unconfirmed 202 chain is bounded like a permanent rejection."""
    bridge_dir, child_path, entry = _seed_recovery_child(
        tmp_path, subagent_id="unconfirmed1", child_id="conv_child_unconfirmed"
    )
    posts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(202, json=[{"queued": False}])

    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:

        async def poll() -> forwarder.SubagentEntry | None:
            return await _recover_once(
                client,
                bridge_dir=bridge_dir,
                entry=entry,
                child_path=child_path,
                tracker=tracker,
            )

        for _ in range(5):
            assert await poll() is None
        assert posts == 3
        assert await poll() is None

    assert posts == 3
    assert entry.recovery_watermark == child_path.stat().st_size
    assert not (bridge_dir / "dead_letter.jsonl").exists()


@pytest.mark.parametrize(
    ("status_code", "response_body"),
    [
        (503, {"detail": "unavailable"}),
        (503, {"error": "subagent_delivery_not_confirmed"}),
        (409, {"detail": "conflict"}),
    ],
)
async def test_subagent_history_transient_exhaustion_is_never_parked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status_code: int,
    response_body: dict[str, str],
) -> None:
    """An exhausted transient failure keeps the backoff ceiling, never the long park."""
    bridge_dir, child_path, entry = _seed_recovery_child(
        tmp_path, subagent_id="transient1", child_id="conv_child_transient"
    )
    monkeypatch.setattr(forwarder, "_HTTP_POST_RETRY_MAX_DELAY_S", 0.0)
    posts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(status_code, json=response_body)

    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    items = forwarder.read_transcript_items_from_offset(
        child_path,
        0,
        start_line=0,
        agent_name="claude-native-ui",
        current_response_id=None,
        include_sidechains=True,
        end_offset=entry.recovery_watermark,
    ).items
    assert len(items) == 1
    retry_key = f"subagent_recovery:{entry.child_conversation_id}:{items[0].source_id}"
    caplog.set_level(logging.DEBUG, logger=forwarder._logger.name)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:

        async def poll() -> forwarder.SubagentEntry | None:
            return await _recover_once(
                client,
                bridge_dir=bridge_dir,
                entry=entry,
                child_path=child_path,
                tracker=tracker,
            )

        for _ in range(forwarder._SUBAGENT_BATCH_MAX_TRANSIENT_ATTEMPTS + 8):
            assert await poll() is None

    assert posts == forwarder._SUBAGENT_BATCH_MAX_TRANSIENT_ATTEMPTS + 8
    retry_delay_s = tracker.retry_delay_s(retry_key)
    assert retry_delay_s is None or retry_delay_s < forwarder._SUBAGENT_RECOVERY_PARK_S
    assert entry.recovery_watermark == child_path.stat().st_size
    assert entry.byte_offset == 0
    assert not (bridge_dir / "dead_letter.jsonl").exists()
    records = [record for record in caplog.records if record.name == forwarder._logger.name]
    assert len([record for record in records if record.exc_info]) == 1
    assert not [record for record in records if "reconciliation parked" in record.getMessage()]


async def test_subagent_history_recovery_logs_one_traceback_before_parking(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only the first blocked attempt logs a traceback; parking logs one WARN."""
    bridge_dir, child_path, entry = _seed_recovery_child(
        tmp_path, subagent_id="log1", child_id="conv_child_log"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "forbidden"})

    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    caplog.set_level(logging.DEBUG, logger=forwarder._logger.name)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:

        async def poll() -> forwarder.SubagentEntry | None:
            return await _recover_once(
                client,
                bridge_dir=bridge_dir,
                entry=entry,
                child_path=child_path,
                tracker=tracker,
            )

        for _ in range(20):
            assert await poll() is None

    records = [record for record in caplog.records if record.name == forwarder._logger.name]
    traced = [record for record in records if record.exc_info]
    assert len(traced) == 1
    assert traced[0].levelno == logging.WARNING
    assert "reconciliation held" in traced[0].getMessage()
    parked = [record for record in records if "reconciliation parked" in record.getMessage()]
    assert len(parked) == 1
    assert parked[0].levelno == logging.WARNING
    assert parked[0].exc_info is None


async def test_subagent_history_partial_eof_becomes_live_only_after_newline(
    tmp_path: Path,
) -> None:
    """A partial row at the frozen EOF stays outside history and is retried live."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    child_path = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="partial1",
        agent_type="Explore",
        description="partial boundary",
        tool_use_id="toolu_partial",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "historical-complete",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "old complete"}],
                },
            }
        ],
    )
    complete_end = child_path.stat().st_size
    live_row = json.dumps(
        {
            "isSidechain": True,
            "type": "assistant",
            "uuid": "live-after-partial",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "new complete"}],
            },
        }
    )
    split_at = len(live_row) // 2
    with child_path.open("a", encoding="utf-8") as handle:
        handle.write(live_row[:split_at])
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_subagent_start":
            return httpx.Response(
                202,
                json={"child_session_id": "conv_child_partial", "existing": True},
            )
        if body.get("type") == "external_conversation_item":
            posted.append(body["data"])
            if "recovery_after" in body["data"]:
                return httpx.Response(
                    202,
                    json={"item_id": "server-old", "replayed": True, "recovery": True},
                )
            return httpx.Response(202, json={"replayed": False})
        return httpx.Response(202, json={})

    trackers = {
        "start_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        "item_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        "status_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
    }
    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler), base_url="http://ap"
    ) as client:
        first = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            **trackers,
        )
        assert first.subagents["partial1"].byte_offset == complete_end
        assert [item["source_id"] for item in posted] == ["historical-complete:0:message"]

        with child_path.open("a", encoding="utf-8") as handle:
            handle.write(live_row[split_at:] + "\n")
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=first,
            agent_name="claude-native-ui",
            **trackers,
        )

    assert second.subagents["partial1"].byte_offset == child_path.stat().st_size
    assert second.subagents["partial1"].last_status == "running"
    assert [item["source_id"] for item in posted] == [
        "historical-complete:0:message",
        "live-after-partial:0:message",
    ]
    assert "recovery_after" not in posted[1]


async def test_subagent_cold_parent_xml_terminal_is_replayed(tmp_path: Path) -> None:
    """A legacy start response makes its frozen parent terminal historical."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            _task_notification_record(
                tool_use_id="toolu_parent_history",
                status="completed",
                result="old terminal",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="parenthistory1",
        agent_type="Explore",
        description="legacy existing child",
        tool_use_id="toolu_parent_history",
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_subagent_start":
            # Missing ``existing`` is the conservative legacy-Server path.
            return httpx.Response(202, json={"child_session_id": "conv_child_parent_history"})
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler), base_url="http://ap"
    ) as client:
        result = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert status_posts == [{"status": "completed", "output": "old terminal", "replayed": True}]
    entry = result.subagents["parenthistory1"]
    assert entry.terminal_replayed is True
    assert entry.last_status == "completed"
    assert entry.parent_recovery_watermark is None
    assert result.parent_byte_offset == transcript_path.stat().st_size


async def test_parent_recovery_marks_only_the_existing_child_terminal_replayed(
    tmp_path: Path,
) -> None:
    """An old child baseline cannot swallow a new sibling's real completion."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "".join(
            json.dumps(
                _task_notification_record(
                    tool_use_id=tool_use_id,
                    status="completed",
                    result=result,
                )
            )
            + "\n"
            for tool_use_id, result in (
                ("toolu_old_a", "old A terminal"),
                ("toolu_new_b", "new B terminal"),
            )
        ),
        encoding="utf-8",
    )
    for subagent_id, tool_use_id in (
        ("a-old", "toolu_old_a"),
        ("b-new", "toolu_new_b"),
    ):
        _seed_subagent_on_disk(
            transcript_path=transcript_path,
            subagent_id=subagent_id,
            agent_type="Explore",
            description=subagent_id,
            tool_use_id=tool_use_id,
        )
    status_posts: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_subagent_start":
            subagent_id = body["data"]["subagent_id"]
            return httpx.Response(
                202,
                json={
                    "child_session_id": f"conv_child_{subagent_id}",
                    "existing": subagent_id == "a-old",
                },
            )
        if body.get("type") == "external_session_status":
            status_posts.append((request.url.path, body["data"]))
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler), base_url="http://ap"
    ) as client:
        result = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert sorted(status_posts, key=lambda item: item[0]) == [
        (
            "/v1/sessions/conv_child_a-old/events",
            {"status": "completed", "output": "old A terminal", "replayed": True},
        ),
        (
            "/v1/sessions/conv_child_b-new/events",
            {"status": "completed", "output": "new B terminal"},
        ),
    ]
    assert result.subagents["a-old"].terminal_replayed is True
    assert result.subagents["b-new"].terminal_replayed is False


@pytest.mark.asyncio
async def test_subagent_cancellation_drains_started_state_write_before_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled old scan cannot overwrite the empty rotation checkpoint."""
    bridge_dir = tmp_path / "bridge"
    old_state = forwarder.SubagentForwardState(
        subagents={
            "old": forwarder.SubagentEntry(
                subagent_id="old",
                child_conversation_id="conv_old_child",
            )
        }
    )
    empty_state = forwarder.SubagentForwardState(subagents={})
    write_started = threading.Event()
    release_write = threading.Event()
    old_write_finished = threading.Event()
    original_write = forwarder._write_subagent_forward_state

    def delayed_write(path: Path, state: forwarder.SubagentForwardState) -> None:
        if state.subagents:
            write_started.set()
            assert release_write.wait(timeout=2.0)
        original_write(path, state)
        if state.subagents:
            old_write_finished.set()

    monkeypatch.setattr(forwarder, "_write_subagent_forward_state", delayed_write)

    async def write_old_state() -> forwarder.SubagentForwardState:
        await forwarder._write_subagent_forward_state_async(bridge_dir, old_state)
        return old_state

    task = asyncio.create_task(write_old_state())
    assert await asyncio.to_thread(write_started.wait, 1.0)
    task.cancel()

    async def release_later() -> None:
        await asyncio.sleep(0.05)
        release_write.set()

    release_task = asyncio.create_task(release_later())
    await forwarder._cancel_subagent_forward_task(task)
    await forwarder._write_subagent_forward_state_async(bridge_dir, empty_state)
    await release_task
    assert await asyncio.to_thread(old_write_finished.wait, 1.0)

    assert forwarder._read_subagent_forward_state(bridge_dir) == empty_state


async def test_subagent_watcher_retry_skips_previously_posted_items(
    tmp_path: Path,
) -> None:
    """
    Retrying a failed child item does not re-post earlier child items.

    The sub-agent watcher intentionally leaves ``byte_offset`` behind
    when a later item fails, so the next poll re-reads the same JSONL
    window. This test pins the durable ``seen_source_ids`` guard: item
    A succeeds, item B fails once, and the retry must post only B.
    Without that guard Omnigent live subscribers can see item A synced back
    twice; the server no longer receives a ``source_id`` key that can
    dedupe the post on AP's side.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="retry1",
        agent_type="Explore",
        description="retry item flow",
        tool_use_id="toolu_retry",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "user",
                "uuid": "sa-user-retry",
                "message": {"role": "user", "content": "go"},
            },
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "sa-assistant-retry",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                },
            },
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "retry1": forwarder.SubagentEntry(
                subagent_id="retry1",
                child_conversation_id="conv_child_retry",
            )
        }
    )
    posted_items: list[str] = []
    attempts_by_item: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail the assistant item once and accept everything else.

        :param request: Request issued by the forwarder.
        :returns: Canned Omnigent response.
        """
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") != "external_conversation_item":
            return httpx.Response(202, json={})
        item_data = body["data"]["item_data"]
        role = item_data["role"]
        text = item_data["content"][0]["text"]
        item_key = f"{role}:{text}"
        posted_items.append(item_key)
        attempts_by_item[item_key] = attempts_by_item.get(item_key, 0) + 1
        if item_key == "assistant:done" and attempts_by_item[item_key] == 1:
            return httpx.Response(503, json={"error": "try again"})
        return httpx.Response(202, json={})

    item_retry_tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler),
        base_url="http://ap",
    ) as client:
        first = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=item_retry_tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=first,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=item_retry_tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert posted_items == ["user:go", "assistant:done", "assistant:done"]
    child_state = second.subagents["retry1"]
    assert child_state.byte_offset == subagent_jsonl.stat().st_size
    assert set(child_state.seen_source_ids) == {
        "sa-user-retry:0:message",
        "sa-assistant-retry:0:message",
    }


@pytest.mark.parametrize("replayed,terminal", [(False, False), (True, False), (True, True)])
async def test_batched_child_items_preserve_replay_and_terminal_truth(
    tmp_path: Path, replayed: bool, terminal: bool
) -> None:
    """Batch acknowledgements must not reopen history or overwrite a native terminal."""
    transcript = tmp_path / "parent.jsonl"
    transcript.write_text("", encoding="utf-8")
    child_path = _seed_subagent_on_disk(
        transcript_path=transcript,
        subagent_id="batch-child",
        agent_type="Explore",
        description="one child",
        tool_use_id="toolu_batch",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "batch-answer",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Answer"}],
                },
            }
        ],
    )
    if terminal:
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _task_notification_record(
                        tool_use_id="toolu_batch", status="completed", result="done"
                    )
                )
                + "\n"
            )
    pending = {"toolu_not_registered": ("completed", "keep pending", False, None)}
    state = forwarder.SubagentForwardState(
        subagents={
            "batch-child": forwarder.SubagentEntry(
                subagent_id="batch-child",
                child_conversation_id="conv_batch",
                tool_use_id="toolu_batch",
            )
        },
        terminal_recovery_version=1,
        pending_terminal_notifications=pending,
    )
    statuses: list[dict[str, Any]] = []
    batches: list[list[dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if isinstance(body, list):
            batches.append(body)
            return httpx.Response(
                200,
                json=[{"item_id": f"item-{i}", "replayed": replayed} for i in range(len(body))],
            )
        if body["type"] == "external_session_status":
            statuses.append(body["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        result = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=tmp_path / "bridge",
            transcript_path=transcript,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(),
            item_retry_tracker=forwarder._PostRetryTracker(),
            status_retry_tracker=forwarder._PostRetryTracker(),
        )
    assert len(batches) == 1
    assert result.subagents["batch-child"].byte_offset == child_path.stat().st_size
    assert result.pending_terminal_notifications == pending
    assert result.parent_byte_offset == transcript.stat().st_size
    if terminal:
        assert statuses == [{"status": "completed", "output": "done"}]
    elif replayed:
        assert statuses == []
        assert result.subagents["batch-child"].last_activity_ts is None
    else:
        assert statuses == [{"status": "running"}]
