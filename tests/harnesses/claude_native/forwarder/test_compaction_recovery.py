"""Compaction recovery tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    ClaudeTranscriptItem,
    record_hook_event,
)
from omnigent.harnesses.claude_native.forwarder import (
    CompactionForwardState,
    _acknowledge_compaction_completion,
    _handle_compact_summary_item,
    _maybe_persist_compaction_fallback,
    _note_precompact,
    _PostRetryTracker,
    _read_compaction_state,
    _reset_compaction_skip_stats,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _start_recording_server,
)

# ---------------------------------------------------------------------------
# _persist_native_compaction_item tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_native_compaction_item_posts_compaction_event(tmp_path: Path) -> None:
    """
    ``_persist_native_compaction_item`` queries the latest item and posts a compaction event.

    The function GETs ``/v1/sessions/{id}/items?limit=1&order=desc`` to
    find the most recent persisted item, reads post-compaction messages
    from the Claude session, then POSTs a ``compaction`` event using
    that item's id as ``last_item_id`` and the messages as
    ``compacted_messages``.
    """
    get_response = MagicMock()
    get_response.raise_for_status = MagicMock()
    get_response.json.return_value = {"data": [{"id": "item_123"}]}

    post_response = MagicMock()
    post_response.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get.return_value = get_response
    client.post.return_value = post_response

    # Build a fake message returned by get_session_messages.
    fake_msg = MagicMock()
    fake_msg.type = "assistant"
    fake_msg.message = {"content": [{"type": "text", "text": "hello"}]}

    bridge_dir = tmp_path / "bridge"

    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder.read_claude_session_id",
            return_value="claude-uuid-1",
        ),
        patch(
            "claude_agent_sdk.get_session_messages",
            return_value=[fake_msg],
        ),
    ):
        await forwarder._persist_native_compaction_item(
            client, session_id="conv_test", bridge_dir=bridge_dir
        )

    client.get.assert_called_once_with(
        "/v1/sessions/conv_test/items",
        params={"limit": 1, "order": "desc"},
    )
    client.post.assert_called_once()
    post_call = client.post.call_args
    assert post_call[0][0] == "/v1/sessions/conv_test/events"
    body = post_call[1]["json"] if "json" in post_call[1] else post_call[0][1]
    assert body["type"] == "compaction"
    assert body["data"]["last_item_id"] == "item_123"
    assert body["data"]["summary"] is not None
    assert body["data"]["model"] == "unknown"
    assert body["data"]["token_count"] == 0
    assert body["data"]["snapshot_source"] == "hook_fallback"
    # compacted_messages should contain the converted fake message.
    assert body["data"]["compacted_messages"] == [
        {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]


@pytest.mark.asyncio
async def test_persist_native_compaction_item_empty_items_uses_fallback(tmp_path: Path) -> None:
    """
    When no items exist, ``last_item_id`` falls back to a generated boundary id.

    If the session has no persisted items yet (e.g. the very first turn
    was compacted before anything was stored), the function generates
    ``compact_boundary_{session_id}`` as the boundary marker instead of
    crashing on an empty list.
    """
    get_response = MagicMock()
    get_response.raise_for_status = MagicMock()
    get_response.json.return_value = {"data": []}

    post_response = MagicMock()
    post_response.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get.return_value = get_response
    client.post.return_value = post_response

    bridge_dir = tmp_path / "bridge"

    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder.read_claude_session_id",
            return_value=None,
        ),
    ):
        await forwarder._persist_native_compaction_item(
            client, session_id="conv_empty", bridge_dir=bridge_dir
        )

    post_call = client.post.call_args
    body = post_call[1]["json"] if "json" in post_call[1] else post_call[0][1]
    assert body["data"]["last_item_id"].startswith("compact_boundary_")
    # No compacted_messages when claude_sid is None.
    assert "compacted_messages" not in body["data"]


@pytest.mark.asyncio
async def test_compaction_completed_triggers_persist(tmp_path: Path) -> None:
    """
    ``SessionStart source=compact`` triggers both status POST and item persistence.

    When the forwarder processes a ``SessionStart source=compact`` record
    (compaction completed), it must call ``_post_external_compaction_status``
    to surface the status AND ``_persist_native_compaction_item`` to write
    the compaction boundary item.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    # Initial SessionStart populates transcript_path.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    # PreCompact mints the pending token the completion signal consumes.
    # A real compaction always fires PreCompact before the compact
    # SessionStart; the hook path only persists when that token exists.
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "PreCompact", "session_id": "claude-session"},
    )
    # Post-compaction SessionStart — the completion signal.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "claude-session",
        },
    )
    server, thread, base_url = _start_recording_server()
    persist_called = asyncio.Event()

    async def _persist_side_effect(*args: Any, **kwargs: Any) -> None:
        persist_called.set()

    persist_mock = AsyncMock(side_effect=_persist_side_effect)
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item",
        persist_mock,
    ):
        task = asyncio.create_task(
            forwarder.forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id="conv_persist",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.01,
            )
        )
        try:
            # Wait for the compaction-completed status POST to arrive
            # (the leading PreCompact in_progress edge is skipped).
            request = None
            for _ in range(10):
                candidate = await _get_recorded_request(server)
                if (
                    candidate["body"].get("type") == "external_compaction_status"
                    and candidate["body"]["data"].get("status") == "completed"
                ):
                    request = candidate
                    break
            assert request is not None, "compaction-completed status was never posted"
            # Wait for _persist_native_compaction_item to be called
            # (it runs right after the POST in the same await chain).
            await asyncio.wait_for(persist_called.wait(), timeout=5.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

    # The recording server captured the compaction-completed status POST.
    assert request["body"]["type"] == "external_compaction_status"
    assert request["body"]["data"]["status"] == "completed"
    # _persist_native_compaction_item was called with the right session id.
    persist_mock.assert_called_once()
    call_kwargs = persist_mock.call_args
    assert call_kwargs[1]["session_id"] == "conv_persist"


@pytest.mark.asyncio
async def test_compaction_in_progress_does_not_persist(tmp_path: Path) -> None:
    """
    ``PreCompact`` (in_progress) does NOT call ``_persist_native_compaction_item``.

    Only compaction *completion* (``SessionStart source=compact``) writes
    the boundary item. ``PreCompact`` merely forwards the ``in_progress``
    status so the UI shows a spinner — there is no boundary to persist yet.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "PreCompact", "session_id": "claude-session"},
    )
    server, thread, base_url = _start_recording_server()
    persist_mock = AsyncMock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item",
        persist_mock,
    ):
        task = asyncio.create_task(
            forwarder.forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id="conv_no_persist",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.01,
            )
        )
        try:
            # Wait for the in_progress status POST to arrive.
            request = await _get_recorded_request(server)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

    assert request["body"]["type"] == "external_compaction_status"
    assert request["body"]["data"]["status"] == "in_progress"
    # _persist_native_compaction_item must NOT be called for in_progress.
    persist_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Durable compaction-boundary reconciliation (native resume/replay fix)
# ---------------------------------------------------------------------------


def _compact_summary_item(
    text: str = "compaction summary text",
    *,
    summary_uuid: str = "summary-uuid",
) -> ClaudeTranscriptItem:
    """
    Build a transcript item flagged as a Claude ``isCompactSummary`` record.

    :param text: The continuation-summary text carried by the item.
    :returns: A ``ClaudeTranscriptItem`` with ``is_compact_summary=True``.
    """
    return ClaudeTranscriptItem(
        source_id=f"{summary_uuid}:0:compact_summary",
        item_type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": text}]},
        response_id="resp_summary",
        is_compact_summary=True,
    )


def _persist_mock() -> AsyncMock:
    """
    Build an ``AsyncMock`` standing in for ``_persist_native_compaction_item``.

    :returns: An async mock that records calls and returns ``None``.
    """
    return AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_missing_compact_session_start_still_persists_from_transcript(
    tmp_path: Path,
) -> None:
    """
    A transcript ``isCompactSummary`` record persists the boundary alone.

    Reproduces the core bug: the flaky ``SessionStart source=compact`` hook
    never fires, so only the transcript summary is available. The transcript
    path must still persist exactly one compaction boundary (carrying the
    summary text) once a ``PreCompact`` token is pending.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    transcript = tmp_path / "session.jsonl"
    _write_compaction_transcript(transcript, summary="the summary")
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
    )

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        handled = await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_missing_hook",
            bridge_dir=bridge_dir,
            item=_compact_summary_item("the summary"),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert handled is True
    persist.assert_called_once()
    assert persist.call_args[1]["session_id"] == "conv_missing_hook"
    assert persist.call_args[1]["summary_override"] == "the summary"
    # Boundary marked persisted; pending cleared.
    state = forwarder._read_compaction_state(bridge_dir)
    assert state.pending is None
    assert 1 in state.persisted_seqs


@pytest.mark.asyncio
async def test_normal_hook_after_transcript_does_not_double_persist(tmp_path: Path) -> None:
    """
    The completion hook does not re-persist a boundary the transcript wrote.

    After the transcript path persists the boundary and marks the sequence
    done, a later ``SessionStart source=compact`` hook finds no consumable
    pending token, so ``_consume_pending_compaction`` returns ``None`` and no
    second boundary is written.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    transcript = tmp_path / "session.jsonl"
    _write_compaction_transcript(transcript)
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
    )

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        # Transcript path persists first.
        await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_dedupe",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )
    assert persist.call_count == 1

    # Hook path arrives later — the token is already consumed.
    seq = await forwarder._consume_pending_compaction(
        bridge_dir, claude_session_id="claude-1", transcript_path=None
    )
    assert seq is None


@pytest.mark.asyncio
async def test_failed_boundary_post_is_retried_not_consumed(tmp_path: Path) -> None:
    """
    A hard POST failure leaves the summary unconsumed for retry.

    ``_handle_compact_summary_item`` must return ``False`` (so the caller
    holds the transcript cursor before the summary record) and must NOT mark
    the sequence persisted, so the boundary is retried on a later poll rather
    than silently lost — which would make resume reload the full history.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=None
    )

    # A definitively-permanent 400 (not an ambiguous/network failure).
    request = httpx.Request("POST", "http://x/events")
    response = httpx.Response(400, request=request)
    failing = AsyncMock(
        side_effect=httpx.HTTPStatusError("bad", request=request, response=response)
    )

    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", failing
    ):
        handled = await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_retry",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert handled is False
    state = forwarder._read_compaction_state(bridge_dir)
    # Pending still set, nothing persisted — the summary will be retried.
    assert state.pending is not None
    assert state.pending.seq == 1
    assert state.persisted_seqs == ()


@pytest.mark.asyncio
async def test_restart_reattach_does_not_repersist_completed_boundary(tmp_path: Path) -> None:
    """
    An already-persisted boundary is never re-persisted after a rewind.

    Simulates a process restart / cursor rewind that re-reads a summary whose
    boundary already POSTed: ``persisted_seqs`` records the sequence, so
    ``_consume_pending_compaction`` returns ``None`` and
    ``_handle_compact_summary_item`` drops the record without persisting.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    # Durable state as it would exist after a completed compaction: seq 1
    # persisted, but a stale pending token for the same seq lingers (e.g.
    # crash between POST success and mark). The persisted set must win.
    forwarder._write_compaction_state(
        bridge_dir,
        forwarder.CompactionForwardState(
            pending=forwarder._PendingCompaction(seq=1, claude_session_id="claude-1"),
            last_seq=1,
            persisted_seqs=(1,),
        ),
    )

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        handled = await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_restart",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert handled is True
    persist.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_compactions_persist_distinct_boundaries(tmp_path: Path) -> None:
    """
    Two compaction cycles persist two distinct boundaries.

    Each ``PreCompact`` mints a fresh monotonic sequence, so a second
    compaction is not blocked by the first's ``persisted_seqs`` entry.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript = tmp_path / "session.jsonl"
    persist = _persist_mock()

    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        # First compaction.

        _write_compaction_transcript(transcript, summary_uuid="summary-first", summary="first")
        await forwarder._note_precompact(
            bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
        )
        await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_repeat",
            bridge_dir=bridge_dir,
            item=_compact_summary_item("first", summary_uuid="summary-first"),
            retry_tracker=forwarder._PostRetryTracker(),
        )
        # Second compaction, later in the same session.

        _write_compaction_transcript(transcript, summary_uuid="summary-second", summary="second")
        await forwarder._note_precompact(
            bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
        )
        await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_repeat",
            bridge_dir=bridge_dir,
            item=_compact_summary_item("second", summary_uuid="summary-second"),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert persist.call_count == 2
    state = forwarder._read_compaction_state(bridge_dir)
    assert state.pending is None
    assert set(state.persisted_seqs) == {1, 2}


@pytest.mark.asyncio
async def test_historical_summary_without_pending_is_skipped(tmp_path: Path) -> None:
    """
    An ``isCompactSummary`` record with no pending PreCompact is dropped.

    On a cold resume the transcript may contain a historical compact-summary
    record from a prior compaction with no live ``PreCompact`` token. It must
    not persist a spurious boundary, and must not be forwarded as a user
    bubble — ``_handle_compact_summary_item`` returns handled with no persist.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()  # no _note_precompact — no pending token
    from omnigent.harnesses.claude_native.forwarder import _write_compaction_state

    _write_compaction_state(
        bridge_dir,
        CompactionForwardState(
            last_seq=1,
            persisted_seqs=(1,),
            persisted_summary_ids=("summary-uuid",),
        ),
    )

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        handled = await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_historical",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert handled is True
    persist.assert_not_called()

    assert forwarder._read_compaction_state(bridge_dir).persisted_seqs == (1,)


@pytest.mark.asyncio
async def test_precompact_and_summary_same_poll_persists_boundary(tmp_path: Path) -> None:
    """
    P1-1: a PreCompact + summary first visible in one poll persists a boundary.

    The transcript forwarder (which consumes the ``isCompactSummary`` record)
    runs before the hook forwarder (which mints the ``PreCompact`` token)
    within a single poll. Without the pre-items prescan, a ``PreCompact`` and
    its summary that both first appear in the same poll would lose the
    boundary — the summary is consumed with no token yet minted.
    ``_prescan_precompact_edges`` mints the token first, so the summary that
    follows in the same poll finds it.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript = tmp_path / "session.jsonl"
    _write_compaction_transcript(transcript, summary="same-poll summary")
    # A PreCompact hook is written but the hook cursor has NOT advanced past
    # it yet (mirrors the same-poll ordering: hooks are forwarded AFTER items).
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "PreCompact",
            "session_id": "claude-1",
            "transcript_path": str(transcript),
        },
    )
    hook_state = await forwarder._ensure_hook_state(
        bridge_dir, start_at_end=False, session_id="conv_same_poll"
    )

    # No pending token before the prescan.
    assert forwarder._read_compaction_state(bridge_dir).pending is None

    # Prescan mints the token BEFORE the transcript summary is processed.
    await forwarder._prescan_precompact_edges(bridge_dir, hook_state)
    state = forwarder._read_compaction_state(bridge_dir)
    assert state.pending is not None
    assert state.pending.seq == 1

    # The summary in the same poll now finds the token and persists once.
    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        handled = await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_same_poll",
            bridge_dir=bridge_dir,
            item=_compact_summary_item("same-poll summary"),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert handled is True
    persist.assert_called_once()
    state = forwarder._read_compaction_state(bridge_dir)
    assert 1 in state.persisted_seqs
    assert state.pending is None


@pytest.mark.asyncio
async def test_prescan_is_idempotent_with_hook_phase(tmp_path: Path) -> None:
    """
    P1-1: the prescan and the main hook phase mint one token per PreCompact.

    Both scans see the same ``PreCompact`` record each poll. The
    ``event_cursor`` idempotency key must keep them converging on a single
    pending token — never two — so a re-mint cannot overwrite a token whose
    boundary is mid-persist.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "PreCompact", "session_id": "claude-1"},
    )
    hook_state = await forwarder._ensure_hook_state(
        bridge_dir, start_at_end=False, session_id="conv_idem"
    )

    # Prescan mints seq 1.
    await forwarder._prescan_precompact_edges(bridge_dir, hook_state)
    first = forwarder._read_compaction_state(bridge_dir)
    assert first.pending is not None and first.pending.seq == 1
    assert first.last_precompact_cursor == 1

    # The main hook phase would note the SAME edge (same event_cursor=1).
    # It must be a no-op: same seq, no second token.
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=None, event_cursor=1
    )
    second = forwarder._read_compaction_state(bridge_dir)
    assert second.pending is not None and second.pending.seq == 1
    assert second.last_seq == 1

    # A genuinely NEW PreCompact edge (higher cursor) mints the next seq.
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=None, event_cursor=2
    )
    third = forwarder._read_compaction_state(bridge_dir)
    assert third.pending is not None and third.pending.seq == 2
    assert third.last_precompact_cursor == 2


@pytest.mark.asyncio
async def test_standalone_completion_hook_persists_without_pending(tmp_path: Path) -> None:
    """
    P1-2: a compact SessionStart with no pending token still persists a boundary.

    Restores the legacy standalone-completion safety. When the
    ``PreCompact`` hook was dropped (or the forwarder attached after it
    fired) AND no transcript summary has persisted a boundary, the
    ``SessionStart source=compact`` completion hook must still persist
    exactly one boundary — otherwise resume reloads the full pre-compaction
    history. ``_claim_standalone_completion`` mints the sequence for it.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    # No _note_precompact, no persisted boundary — genuinely standalone.
    seq = await forwarder._claim_standalone_completion(bridge_dir)
    assert seq == 1
    state = forwarder._read_compaction_state(bridge_dir)
    # A pending token is installed so a later transcript summary reconciles
    # against the same sequence instead of double-persisting.
    assert state.pending is not None
    assert state.pending.seq == 1

    # After the caller persists and marks it done, the boundary is recorded.
    await forwarder._mark_compaction_persisted(bridge_dir, seq)
    final = forwarder._read_compaction_state(bridge_dir)
    assert 1 in final.persisted_seqs
    assert final.pending is None


@pytest.mark.asyncio
async def test_completion_hook_after_transcript_persist_is_absorbed(tmp_path: Path) -> None:
    """
    P1-2: a completion hook trailing a transcript-persisted boundary is absorbed.

    The transcript ``isCompactSummary`` path and the
    ``SessionStart source=compact`` hook are two completion signals for the
    SAME compaction. When the transcript path persists first it arms the
    completion-ack window; the trailing hook must be absorbed (return
    ``None``, no new sequence) rather than persist a spurious standalone
    boundary.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    transcript = tmp_path / "session.jsonl"
    _write_compaction_transcript(transcript)
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
    )

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        # Transcript path persists the boundary; arms expect_completion_ack.
        await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_absorb",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )
    assert persist.call_count == 1
    armed = forwarder._read_compaction_state(bridge_dir)
    assert armed.expect_completion_ack is True

    # The trailing completion hook finds no pending token and is absorbed.
    seq = await forwarder._consume_pending_compaction(
        bridge_dir, claude_session_id="claude-1", transcript_path=None
    )
    assert seq is None

    seq = await forwarder._acknowledge_compaction_completion(
        bridge_dir,
        claude_session_id="claude-1",
        transcript_path=str(transcript),
    )
    assert seq is None  # absorbed, NOT a new standalone boundary
    after = forwarder._read_compaction_state(bridge_dir)
    assert after.expect_completion_ack is False
    assert after.persisted_seqs == (1,)  # still exactly one boundary


@pytest.mark.asyncio
async def test_stale_completion_ack_does_not_swallow_a_later_boundary(
    tmp_path: Path,
) -> None:
    """
    P2-1: a completion ack is bound to its seq and is one-shot per boundary.

    The lost-boundary hazard: compaction A persists via the transcript path
    and arms ``expect_completion_ack``; A's own ``SessionStart source=compact``
    hook never fires (flaky), so the flag stays armed. A later compaction B's
    ``PreCompact`` is *also* dropped, then B's completion hook fires. With a
    bare unattributed flag, B's hook would be absorbed as A's stale ack and
    B's boundary lost.

    Binding the ack to a ``seq`` and making absorption one-shot fixes it: the
    window is consumed exactly once (the trailing hook for A), and any
    *further* standalone completion — B's — falls through to a fresh persist
    instead of being swallowed.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    transcript = tmp_path / "session.jsonl"
    _write_compaction_transcript(transcript)
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
    )

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        # Compaction A persists via the transcript path → arms the ack for A's seq.
        await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_p21",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )
    armed = forwarder._read_compaction_state(bridge_dir)
    assert armed.expect_completion_ack is True
    assert armed.expect_completion_ack_seq == 1  # bound to A's seq, not a bare bool
    assert armed.persisted_seqs == (1,)

    # A's own trailing completion hook arrives late and is absorbed (one-shot).
    absorbed = await forwarder._claim_standalone_completion(bridge_dir)
    assert absorbed is None
    after_absorb = forwarder._read_compaction_state(bridge_dir)
    assert after_absorb.expect_completion_ack is False
    assert after_absorb.expect_completion_ack_seq == 0  # window closed

    # Compaction B: its PreCompact was dropped too, so B arrives as a
    # standalone completion hook with NO pending token and NO armed ack. It
    # must persist a fresh boundary, not be swallowed as A's stale ack.
    b_seq = await forwarder._claim_standalone_completion(bridge_dir)
    assert b_seq == 2, "B's boundary must be persisted, not lost to a stale ack"
    final = forwarder._read_compaction_state(bridge_dir)
    assert final.pending is not None
    assert final.pending.seq == 2


@pytest.mark.asyncio
async def test_completion_ack_armed_for_unpersisted_seq_biases_to_persist(
    tmp_path: Path,
) -> None:
    """
    P2-1: an ack armed for a seq that is NOT persisted persists (bias-to-safe).

    If durable state is somehow armed (corrupt/partial write, or a legacy
    ``compaction_forwarder.json`` from before ``expect_completion_ack_seq``
    existed so the seq reads back as ``0``) the standalone path cannot prove
    the arriving hook is a duplicate. A lost boundary reloads the full
    pre-compaction history on resume — far worse than an at-most-once
    duplicate — so the path biases to persisting a fresh boundary rather than
    silently absorbing the hook.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    # Legacy/corrupt shape: flag armed but the seq it points at is not in
    # persisted_seqs (here it reads back as 0, mimicking an old state file).
    forwarder._write_compaction_state(
        bridge_dir,
        forwarder.CompactionForwardState(
            pending=None,
            last_seq=1,
            persisted_seqs=(),
            expect_completion_ack=True,
            expect_completion_ack_seq=0,
        ),
    )
    seq = await forwarder._claim_standalone_completion(bridge_dir)
    assert seq == 2, "bias-to-safe: persist rather than absorb an unprovable ack"
    state = forwarder._read_compaction_state(bridge_dir)
    assert state.pending is not None
    assert state.pending.seq == 2


def _write_compaction_transcript(
    path: Path,
    *,
    summary_uuid: str = "summary-uuid",
    summary: str = "compaction summary text",
) -> None:
    """Write the native boundary/summary shape emitted by Claude Code."""
    records = [
        {
            "type": "user",
            "uuid": "preserved-user",
            "parentUuid": None,
            "message": {"role": "user", "content": "preserved question"},
        },
        {
            "type": "assistant",
            "uuid": "preserved-assistant",
            "parentUuid": "preserved-user",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "preserved answer"}],
            },
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary-uuid",
            "parentUuid": "preserved-assistant",
            "compactMetadata": {
                "preservedMessages": {
                    "uuids": ["preserved-user", "preserved-assistant"],
                }
            },
        },
        {
            "type": "user",
            "uuid": summary_uuid,
            "parentUuid": "boundary-uuid",
            "isCompactSummary": True,
            "message": {"role": "user", "content": summary},
        },
    ]
    path.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")


@pytest.mark.asyncio
async def test_compact_summary_first_claims_generation_from_artifact(tmp_path: Path) -> None:
    """A durable summary wins even when its PreCompact hook has not appeared."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript = tmp_path / "session.jsonl"
    _write_compaction_transcript(transcript)
    with patch(
        "omnigent.harnesses.claude_native.forwarder.read_transcript_path",
        return_value=transcript,
    ):
        persist = _persist_mock()
        with patch(
            "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
        ):
            handled = await _handle_compact_summary_item(
                AsyncMock(),
                session_id="conv-summary-first",
                bridge_dir=bridge_dir,
                item=_compact_summary_item(),
                retry_tracker=_PostRetryTracker(),
            )

    assert handled is True
    assert persist.call_args.kwargs["snapshot_source"] == "transcript"
    assert persist.call_args.kwargs["compacted_messages_override"] == [
        {"type": "message", "role": "user", "content": "compaction summary text"},
        {"type": "message", "role": "user", "content": "preserved question"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "preserved answer"}],
        },
    ]


def test_compaction_snapshot_orders_shuffled_tools_and_excludes_sidechain(
    tmp_path: Path,
) -> None:
    """Preserved UUID metadata is membership-only; the main transcript chain orders it."""
    transcript = tmp_path / "session.jsonl"
    records = [
        {
            "type": "user",
            "uuid": "prompt",
            "parentUuid": None,
            "isSidechain": False,
            "message": {"role": "user", "content": "inspect the file"},
        },
        {
            "type": "assistant",
            "uuid": "tool-use",
            "parentUuid": "prompt",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_read",
                        "name": "Read",
                        "input": {"file_path": "/tmp/example"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "tool-result",
            "parentUuid": "tool-use",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_read",
                        "content": "contents",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "sidechain",
            "parentUuid": "prompt",
            "isSidechain": True,
            "message": {"role": "assistant", "content": "side investigation"},
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "parentUuid": "tool-result",
            "isSidechain": False,
            "compactMetadata": {
                "preservedMessages": {
                    # Deliberately reverse tool records and include a sidechain.
                    "uuids": ["tool-result", "sidechain", "prompt", "tool-use"],
                }
            },
        },
        {
            "type": "user",
            "uuid": "summary",
            "parentUuid": "boundary",
            "isSidechain": False,
            "isCompactSummary": True,
            "message": {"role": "user", "content": "summary context"},
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )

    summary_id, snapshot = forwarder._read_native_compaction_snapshot(
        transcript,
        "summary:0:compact_summary",
    )

    assert summary_id == "summary"
    assert [(message["role"], message["content"]) for message in snapshot] == [
        ("user", "summary context"),
        ("user", "inspect the file"),
        (
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "toolu_read",
                    "name": "Read",
                    "input": {"file_path": "/tmp/example"},
                }
            ],
        ),
        (
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_read",
                    "content": "contents",
                }
            ],
        ),
    ]


def _write_chain_records(path: Path, records: list[dict[str, Any]]) -> None:
    """
    Write raw transcript records to ``path`` as newline-delimited JSON.

    :param path: Transcript file to write.
    :param records: Records to serialize, one per line.
    :returns: None.
    """
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )


def _boundary_chain_records(*, chain_uuids: list[str]) -> list[dict[str, Any]]:
    """
    Build a boundary + summary transcript with an arbitrary parent chain.

    The summary's ``parentUuid`` points at the first UUID in
    ``chain_uuids``; each intermediate record points at the next one, and
    the last one points at the ``compact_boundary`` record. An empty
    ``chain_uuids`` means the summary's direct parent is the boundary.

    :param chain_uuids: UUIDs of intermediate records between summary and boundary.
    :returns: Transcript records ending in an ``isCompactSummary`` summary.
    """
    records: list[dict[str, Any]] = [
        {
            "type": "user",
            "uuid": "preserved-user",
            "parentUuid": None,
            "message": {"role": "user", "content": "preserved question"},
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "parentUuid": "preserved-user",
            "compactMetadata": {"preservedMessages": {"uuids": ["preserved-user"]}},
        },
    ]
    next_uuid = "boundary"
    for intermediate_uuid in reversed(chain_uuids):
        records.append(
            {
                "type": "attachment",
                "uuid": intermediate_uuid,
                "parentUuid": next_uuid,
            }
        )
        next_uuid = intermediate_uuid
    records.append(
        {
            "type": "user",
            "uuid": "summary",
            "parentUuid": next_uuid,
            "isCompactSummary": True,
            "message": {"role": "user", "content": "summary context"},
        }
    )
    return records


def test_compaction_snapshot_walks_through_interleaved_attachment(tmp_path: Path) -> None:
    """A summary -> attachment -> boundary chain resolves the boundary, not an error."""
    transcript = tmp_path / "session.jsonl"
    _write_chain_records(transcript, _boundary_chain_records(chain_uuids=["attachment-1"]))

    summary_id, snapshot = forwarder._read_native_compaction_snapshot(
        transcript,
        "summary:0:compact_summary",
    )

    assert summary_id == "summary"
    assert snapshot == [
        {"type": "message", "role": "user", "content": "summary context"},
        {"type": "message", "role": "user", "content": "preserved question"},
    ]


def test_compaction_snapshot_walks_through_multiple_intermediate_records(
    tmp_path: Path,
) -> None:
    """Two or more non-boundary records between summary and boundary still resolve."""
    transcript = tmp_path / "session.jsonl"
    records = _boundary_chain_records(chain_uuids=["mid-1", "mid-2", "mid-3"])
    type_by_uuid = {"mid-1": "attachment", "mid-2": "user", "mid-3": "assistant"}
    for record in records:
        record_uuid = record.get("uuid")
        if isinstance(record_uuid, str) and record_uuid in type_by_uuid:
            record["type"] = type_by_uuid[record_uuid]
    _write_chain_records(transcript, records)

    summary_id, snapshot = forwarder._read_native_compaction_snapshot(
        transcript,
        "summary:0:compact_summary",
    )

    assert summary_id == "summary"
    assert snapshot == [
        {"type": "message", "role": "user", "content": "summary context"},
        {"type": "message", "role": "user", "content": "preserved question"},
    ]


def test_compaction_snapshot_without_boundary_returns_summary_only(tmp_path: Path) -> None:
    """A parent chain with no boundary yields ``boundary=None`` instead of raising."""
    transcript = tmp_path / "session.jsonl"
    _write_chain_records(
        transcript,
        [
            {
                "type": "user",
                "uuid": "root",
                "parentUuid": None,
                "message": {"role": "user", "content": "root text"},
            },
            {
                "type": "attachment",
                "uuid": "mid",
                "parentUuid": "root",
            },
            {
                "type": "user",
                "uuid": "summary",
                "parentUuid": "mid",
                "isCompactSummary": True,
                "message": {"role": "user", "content": "summary context"},
            },
        ],
    )

    summary_id, snapshot = forwarder._read_native_compaction_snapshot(
        transcript,
        "summary:0:compact_summary",
    )

    assert summary_id == "summary"
    assert snapshot == [{"type": "message", "role": "user", "content": "summary context"}]


def test_compaction_snapshot_cyclic_parent_chain_terminates(tmp_path: Path) -> None:
    """A self-referential or looping ``parentUuid`` chain terminates without raising."""
    transcript = tmp_path / "session.jsonl"
    _write_chain_records(
        transcript,
        [
            {
                "type": "attachment",
                "uuid": "loop-a",
                "parentUuid": "loop-b",
            },
            {
                "type": "attachment",
                "uuid": "loop-b",
                "parentUuid": "loop-a",
            },
            {
                "type": "user",
                "uuid": "summary",
                "parentUuid": "loop-a",
                "isCompactSummary": True,
                "message": {"role": "user", "content": "summary context"},
            },
        ],
    )

    summary_id, snapshot = forwarder._read_native_compaction_snapshot(
        transcript,
        "summary:0:compact_summary",
    )

    assert summary_id == "summary"
    assert snapshot == [{"type": "message", "role": "user", "content": "summary context"}]


def test_compaction_snapshot_depth_limit_exceeded_terminates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent chain longer than the hop bound terminates without raising."""
    monkeypatch.setattr(forwarder, "_COMPACTION_BOUNDARY_CHAIN_MAX_HOPS", 4)
    transcript = tmp_path / "session.jsonl"
    _write_chain_records(
        transcript,
        _boundary_chain_records(chain_uuids=[f"mid-{index}" for index in range(10)]),
    )

    summary_id, snapshot = forwarder._read_native_compaction_snapshot(
        transcript,
        "summary:0:compact_summary",
    )

    assert summary_id == "summary"
    assert snapshot == [{"type": "message", "role": "user", "content": "summary context"}]


def test_compaction_snapshot_direct_boundary_parent_regression(tmp_path: Path) -> None:
    """A summary whose direct parent is the boundary still resolves exactly as before."""
    transcript = tmp_path / "session.jsonl"
    _write_chain_records(transcript, _boundary_chain_records(chain_uuids=[]))

    summary_id, snapshot = forwarder._read_native_compaction_snapshot(
        transcript,
        "summary:0:compact_summary",
    )

    assert summary_id == "summary"
    assert snapshot == [
        {"type": "message", "role": "user", "content": "summary context"},
        {"type": "message", "role": "user", "content": "preserved question"},
    ]


@pytest.mark.asyncio
async def test_hook_ack_waits_for_summary_durability(tmp_path: Path) -> None:
    """The compact SessionStart cannot persist stale history before the deadline."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript = tmp_path / "session.jsonl"
    await _note_precompact(
        bridge_dir,
        claude_session_id="claude-1",
        transcript_path=str(transcript),
    )
    await _acknowledge_compaction_completion(
        bridge_dir,
        claude_session_id="claude-1",
        transcript_path=str(transcript),
        now=10.0,
    )
    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        assert not await _maybe_persist_compaction_fallback(
            AsyncMock(),
            session_id="conv-hook-first",
            bridge_dir=bridge_dir,
            now=11.9,
        )
        _write_compaction_transcript(transcript)
        assert await _handle_compact_summary_item(
            AsyncMock(),
            session_id="conv-hook-first",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=_PostRetryTracker(),
        )

    persist.assert_called_once()
    assert persist.call_args.kwargs["snapshot_source"] == "transcript"


@pytest.mark.asyncio
async def test_fallback_is_superseded_once_by_durable_summary(tmp_path: Path) -> None:
    """A late authoritative summary replaces one marked fallback, exactly once."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript = tmp_path / "session.jsonl"
    await _note_precompact(
        bridge_dir,
        claude_session_id="claude-1",
        transcript_path=str(transcript),
    )
    await _acknowledge_compaction_completion(
        bridge_dir,
        claude_session_id="claude-1",
        transcript_path=str(transcript),
        now=10.0,
    )
    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        assert await _maybe_persist_compaction_fallback(
            AsyncMock(),
            session_id="conv-supersede",
            bridge_dir=bridge_dir,
            now=12.0,
        )
        assert not await _maybe_persist_compaction_fallback(
            AsyncMock(),
            session_id="conv-supersede",
            bridge_dir=bridge_dir,
            now=20.0,
        )
        _write_compaction_transcript(transcript)
        for _ in range(2):
            assert await _handle_compact_summary_item(
                AsyncMock(),
                session_id="conv-supersede",
                bridge_dir=bridge_dir,
                item=_compact_summary_item(),
                retry_tracker=_PostRetryTracker(),
            )

    assert [call.kwargs["snapshot_source"] for call in persist.await_args_list] == [
        "hook_fallback",
        "transcript",
    ]
    state = _read_compaction_state(bridge_dir)
    assert state.persisted_summary_ids == ("summary-uuid",)
    assert state.pending is None


@pytest.mark.asyncio
async def test_fallback_sdk_read_does_not_block_event_loop(tmp_path: Path) -> None:
    """A blocked synchronous SDK snapshot runs in a worker thread."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    await _note_precompact(
        bridge_dir,
        claude_session_id="claude-1",
        transcript_path=None,
    )
    await _acknowledge_compaction_completion(
        bridge_dir,
        claude_session_id="claude-1",
        transcript_path=None,
        now=10.0,
    )
    sdk_entered = threading.Event()
    sdk_release = threading.Event()

    def blocking_session_read(_session_id: str) -> list[Any]:
        sdk_entered.set()
        assert sdk_release.wait(timeout=5.0)
        return []

    persist = _persist_mock()
    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder.read_claude_session_id",
            return_value="claude-1",
        ),
        patch(
            "claude_agent_sdk.get_session_messages",
            side_effect=blocking_session_read,
        ),
        patch(
            "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item",
            persist,
        ),
    ):
        fallback = asyncio.create_task(
            _maybe_persist_compaction_fallback(
                AsyncMock(),
                session_id="conv-heartbeat",
                bridge_dir=bridge_dir,
                now=12.0,
            )
        )
        try:
            assert await asyncio.to_thread(sdk_entered.wait, 1.0)
            heartbeat = asyncio.Event()

            async def beat() -> None:
                await asyncio.sleep(0)
                heartbeat.set()

            heartbeat_task = asyncio.create_task(beat())
            await asyncio.wait_for(heartbeat.wait(), timeout=0.5)
            await heartbeat_task
        finally:
            sdk_release.set()
        assert await fallback is True

    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_transcript_summary_wins_during_threaded_fallback_read(
    tmp_path: Path,
) -> None:
    """A durable summary arriving during the SDK read prevents a stale fallback POST."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript = tmp_path / "session.jsonl"
    _write_compaction_transcript(transcript)
    await _note_precompact(
        bridge_dir,
        claude_session_id="claude-1",
        transcript_path=str(transcript),
    )
    await _acknowledge_compaction_completion(
        bridge_dir,
        claude_session_id="claude-1",
        transcript_path=str(transcript),
        now=10.0,
    )
    sdk_entered = threading.Event()
    sdk_release = threading.Event()

    def blocking_session_read(_session_id: str) -> list[Any]:
        sdk_entered.set()
        assert sdk_release.wait(timeout=5.0)
        return []

    persist = _persist_mock()
    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder.read_claude_session_id",
            return_value="claude-1",
        ),
        patch(
            "claude_agent_sdk.get_session_messages",
            side_effect=blocking_session_read,
        ),
        patch(
            "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item",
            persist,
        ),
    ):
        fallback = asyncio.create_task(
            _maybe_persist_compaction_fallback(
                AsyncMock(),
                session_id="conv-race",
                bridge_dir=bridge_dir,
                now=12.0,
            )
        )
        try:
            assert await asyncio.to_thread(sdk_entered.wait, 1.0)
            assert await _handle_compact_summary_item(
                AsyncMock(),
                session_id="conv-race",
                bridge_dir=bridge_dir,
                item=_compact_summary_item(),
                retry_tracker=_PostRetryTracker(),
            )
        finally:
            sdk_release.set()

        assert await fallback is False

    persist.assert_awaited_once()
    assert persist.call_args.kwargs["snapshot_source"] == "transcript"
    state = _read_compaction_state(bridge_dir)
    assert state.fallback_persisted_seq == 0
    assert state.persisted_summary_ids == ("summary-uuid",)


@pytest.mark.asyncio
async def test_concurrent_duplicate_summary_callbacks_persist_once(tmp_path: Path) -> None:
    """Concurrent callbacks serialize against the durable summary id."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript = tmp_path / "session.jsonl"
    _write_compaction_transcript(transcript)
    await _note_precompact(
        bridge_dir,
        claude_session_id="claude-1",
        transcript_path=str(transcript),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def persist_once(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await release.wait()

    persist = AsyncMock(side_effect=persist_once)
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        callbacks = [
            asyncio.create_task(
                _handle_compact_summary_item(
                    AsyncMock(),
                    session_id="conv-concurrent",
                    bridge_dir=bridge_dir,
                    item=_compact_summary_item(),
                    retry_tracker=_PostRetryTracker(),
                )
            )
            for _ in range(2)
        ]
        await entered.wait()
        release.set()
        assert await asyncio.gather(*callbacks) == [True, True]

    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_permanent_snapshot_read_error_persists_degraded_boundary(tmp_path: Path) -> None:
    """
    A structural transcript-read error persists a degraded boundary at once.

    ``ValueError`` from ``_read_native_compaction_snapshot`` means the
    summary record is not durable in a STATIC, append-only transcript file
    — retrying can never change that outcome. Routing this through the
    retry tracker would retry forever (its ``permanent`` verdict comes from
    an HTTP status, ``False`` for a ``ValueError``, and ``give_up`` is
    unreachable since ``max_transient_attempts`` defaults to ``None``), so
    the handler must judge permanence itself, persist a degraded boundary,
    and let the caller advance the transcript cursor past the record.
    """
    bridge_dir = tmp_path / "bridge"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    await _note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
    )

    persist = _persist_mock()
    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
        ),
        patch(
            "omnigent.harnesses.claude_native.forwarder._read_native_compaction_snapshot",
            side_effect=ValueError("compact summary not durable"),
        ),
    ):
        handled = await _handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_degraded",
            bridge_dir=bridge_dir,
            item=_compact_summary_item("the summary"),
            retry_tracker=_PostRetryTracker(),
        )

    assert handled is True
    persist.assert_called_once()
    assert persist.call_args.kwargs["summary_override"] == "the summary"
    assert persist.call_args.kwargs["compacted_messages_override"] is None
    # Deliberate: a failed transcript read does not mean the SDK snapshot is
    # gone, so the helper is still allowed to try that fallback.
    assert persist.call_args.kwargs["fallback_snapshot_loaded"] is False
    assert persist.call_args.kwargs["snapshot_source"] == "transcript_degraded"
    state = _read_compaction_state(bridge_dir)
    assert state.pending is None
    assert "summary-uuid" in state.persisted_summary_ids
    assert "summary-uuid" in state.degraded_summary_ids


@pytest.mark.asyncio
async def test_degraded_persist_failure_still_advances_cursor(tmp_path: Path) -> None:
    """
    The degraded persist itself failing still advances past the record.

    A stalled transcript stream is worse than one lost boundary — the
    handler must return ``True`` even when its own degraded-boundary POST
    raises, letting ``_note_forward_failure`` (already counted) carry
    visibility instead of stalling the cursor a second time.
    """
    bridge_dir = tmp_path / "bridge"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    await _note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
    )

    persist = AsyncMock(side_effect=httpx.HTTPError("boundary post also failed"))
    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
        ),
        patch(
            "omnigent.harnesses.claude_native.forwarder._read_native_compaction_snapshot",
            side_effect=ValueError("compact summary not durable"),
        ),
    ):
        handled = await _handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_degraded_fail",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=_PostRetryTracker(),
        )

    assert handled is True
    persist.assert_called_once()
    state = _read_compaction_state(bridge_dir)
    assert state.pending is None
    assert "summary-uuid" in state.persisted_summary_ids
    assert "summary-uuid" in state.degraded_summary_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read_exc",
    [
        KeyError("missing"),
        IndexError("out of range"),
        json.JSONDecodeError("bad json", "{", 1),
    ],
    ids=["KeyError", "IndexError", "JSONDecodeError"],
)
async def test_other_structural_exception_types_take_degraded_path(
    tmp_path: Path, read_exc: Exception
) -> None:
    """``KeyError`` / ``IndexError`` / ``json.JSONDecodeError`` are each permanent."""
    bridge_dir = tmp_path / "bridge"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    await _note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
    )

    persist = _persist_mock()
    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
        ),
        patch(
            "omnigent.harnesses.claude_native.forwarder._read_native_compaction_snapshot",
            side_effect=read_exc,
        ),
    ):
        handled = await _handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_degraded_types",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=_PostRetryTracker(),
        )

    assert handled is True
    assert persist.call_args.kwargs["snapshot_source"] == "transcript_degraded"
    state = _read_compaction_state(bridge_dir)
    assert "summary-uuid" in state.degraded_summary_ids


@pytest.mark.asyncio
async def test_non_permanent_exception_type_still_holds_cursor_for_retry(
    tmp_path: Path,
) -> None:
    """
    An exception outside the permanent set keeps the old hold-and-retry behavior.

    This is the "transcript may not yet be complete" case (e.g. a plain
    ``OSError`` mid-write) — retrying next poll CAN change the outcome, so
    the handler must not treat it as permanent.
    """
    bridge_dir = tmp_path / "bridge"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    await _note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
    )

    persist = _persist_mock()
    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
        ),
        patch(
            "omnigent.harnesses.claude_native.forwarder._read_native_compaction_snapshot",
            side_effect=OSError("transcript line incomplete"),
        ),
    ):
        handled = await _handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_transient",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=_PostRetryTracker(),
        )

    assert handled is False
    persist.assert_not_called()
    state = _read_compaction_state(bridge_dir)
    assert state.pending is not None
    assert state.persisted_summary_ids == ()
    assert state.degraded_summary_ids == ()


@pytest.mark.asyncio
async def test_http_error_from_persist_still_uses_retry_tracker_not_degraded(
    tmp_path: Path,
) -> None:
    """
    Regression: ``httpx.HTTPError`` still takes the pre-existing retry path.

    A real (valid) transcript reaches ``_persist_native_compaction_item``,
    which then raises an HTTP rejection — that stays on the ``except
    httpx.HTTPError`` branch (retry-and-hold), never the new structural-error
    degraded path, since the two branches are mutually exclusive by
    exception type.
    """
    bridge_dir = tmp_path / "bridge"
    transcript = tmp_path / "session.jsonl"
    _write_compaction_transcript(transcript)
    await _note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
    )

    request = httpx.Request("POST", "http://x/events")
    response = httpx.Response(400, request=request)
    failing = AsyncMock(
        side_effect=httpx.HTTPStatusError("bad", request=request, response=response)
    )

    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", failing
    ):
        handled = await _handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_http_regression",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=_PostRetryTracker(),
        )

    assert handled is False
    state = _read_compaction_state(bridge_dir)
    assert state.pending is not None
    assert state.persisted_summary_ids == ()
    assert state.degraded_summary_ids == ()


@pytest.mark.asyncio
async def test_ambiguous_boundary_post_marks_persisted_not_retried(
    tmp_path: Path,
) -> None:
    """
    An ambiguous boundary POST is treated as committed, not retried.

    A read timeout means the request was sent but the response was lost,
    so the server may already have committed the boundary. Retrying would
    risk persisting the same boundary twice, so the handler must mark the
    sequence persisted, clear the retry state, and report handled.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript = tmp_path / "session.jsonl"
    _write_compaction_transcript(transcript)
    await _note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
    )

    request = httpx.Request("POST", "http://x/events")
    failing = AsyncMock(side_effect=httpx.ReadTimeout("response lost", request=request))
    retry_tracker = _PostRetryTracker()

    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", failing
    ):
        handled = await _handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_ambiguous",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=retry_tracker,
        )

    assert handled is True
    assert retry_tracker.has_retry_state("compaction:1") is False
    state = _read_compaction_state(bridge_dir)
    assert state.pending is None
    assert 1 in state.persisted_seqs
    assert state.persisted_summary_ids == ("summary-uuid",)


@pytest.mark.asyncio
async def test_forward_available_items_advances_past_permanently_failed_compaction(
    tmp_path: Path,
) -> None:
    """
    A batch's compact-summary item that fails permanently still advances.

    End-to-end through ``_forward_available_items``: the transcript cursor
    must land past the compact-summary record (not stall at its
    batch-start position), and the summary must never be POSTed as an
    ``external_conversation_item`` chat bubble — only the degraded
    ``compaction`` boundary event.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "summary-uuid",
                "isCompactSummary": True,
                "message": {"role": "user", "content": "the compaction summary"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    await _note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript_path)
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )

    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """Answer the item-lookup GET and record every POST body."""
        if request.method == "GET":
            requests.append({"method": "GET"})
            return httpx.Response(200, json={"data": []})
        payload = json.loads(request.content.decode("utf-8"))
        requests.append({"method": "POST", "body": payload})
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    with patch(
        "omnigent.harnesses.claude_native.forwarder._read_native_compaction_snapshot",
        side_effect=ValueError("compact summary not durable"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            dedupe = forwarder._ForwardDedupeState()
            updated = await forwarder._forward_available_items(
                client=client,
                session_id="conv_degraded_batch",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )

    # Cursor advanced past the failed record instead of stalling at the
    # batch-start position (the A2 bug: a permanent failure used to make
    # the caller return the un-advanced ``updated`` state forever).
    assert updated.byte_offset == transcript_path.stat().st_size
    assert updated.line_cursor == 1
    post_bodies = [entry["body"] for entry in requests if entry["method"] == "POST"]
    assert [body["type"] for body in post_bodies] == ["compaction"]
    assert post_bodies[0]["data"]["snapshot_source"] == "transcript_degraded"
    persisted = json.loads((bridge_dir / "transcript_forwarder.json").read_text("utf-8"))
    assert persisted["byte_offset"] == transcript_path.stat().st_size
    state_after = _read_compaction_state(bridge_dir)
    assert "summary-uuid" in state_after.persisted_summary_ids
    assert "summary-uuid" in state_after.degraded_summary_ids


@pytest.mark.asyncio
async def test_forward_available_items_posts_side_channel_when_compact_summary_holds_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A3b: a compact-summary early return must not stall usage/model/title.

    ``_handle_compact_summary_item`` returning ``False`` (a hard persist
    failure or active backoff — see
    ``test_failed_boundary_post_is_retried_not_consumed``) makes
    ``_forward_transcript_item_batch`` ``return`` before its FIRST item is
    fully processed, long before the batch's own fall-through tail. Measured
    live this held the usage POST hostage every poll, pinning the web UI's
    context ring at 98-99% for hours. The usage / model / title POSTs have
    no dependency on the item batch completing, so they must still fire —
    the ``model`` and ``custom-title`` records here are never reached by the
    item loop (it returns on item #1), yet the transcript pre-scan already
    captured them onto ``result.latest_model`` / ``result.latest_custom_title``.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "summary-uuid",
                        "isCompactSummary": True,
                        "message": {"role": "user", "content": "the compaction summary"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "role": "assistant",
                            "model": "claude-sonnet-5",
                            "content": [{"type": "text", "text": "after compaction"}],
                        },
                    }
                ),
                json.dumps(
                    {"type": "custom-title", "customTitle": "post-compaction", "sessionId": "s1"}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    await _note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript_path)
    )
    monkeypatch.setattr(
        forwarder,
        "read_claude_context_state",
        lambda _bridge: {
            "context_window_size": 180_000,
            "current_usage": {"input_tokens": 5_000, "output_tokens": 200},
        },
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )

    # A definitively-permanent 400 makes ``_handle_compact_summary_item``
    # return ``False`` (same shape as ``test_failed_boundary_post_is_retried_not_consumed``)
    # — the exact early return the live incident hit every poll.
    request = httpx.Request("POST", "http://x/events")
    response = httpx.Response(400, request=request)
    failing_persist = AsyncMock(
        side_effect=httpx.HTTPStatusError("bad", request=request, response=response)
    )

    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item",
        failing_persist,
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            dedupe = forwarder._ForwardDedupeState()
            updated = await forwarder._forward_available_items(
                client=client,
                session_id="conv_stuck_ring",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )

    # A2's retry contract is unchanged: the cursor holds before the summary.
    assert updated.byte_offset == 0
    assert updated.seen_source_ids == ()

    post_types = [r["type"] for r in requests]
    assert "external_session_usage" in post_types, "usage POST must not be collateral damage"
    assert "external_model_change" in post_types, "model POST must not be collateral damage"
    assert "external_session_title" in post_types, "title POST must not be collateral damage"

    usage_post = next(r for r in requests if r["type"] == "external_session_usage")
    assert usage_post["data"]["context_tokens"] == 5_000
    assert usage_post["data"]["context_window"] == 180_000
    assert dedupe.usage is not None
    assert dedupe.context_window == 180_000

    model_post = next(r for r in requests if r["type"] == "external_model_change")
    assert model_post["data"] == {"model": "claude-sonnet-5"}
    assert dedupe.posted_model == "claude-sonnet-5"

    title_post = next(r for r in requests if r["type"] == "external_session_title")
    assert title_post["data"] == {"title": "post-compaction"}
    assert dedupe.posted_title == "post-compaction"


@pytest.mark.asyncio
async def test_forward_available_items_posts_usage_once_after_items_on_normal_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The ordinary fall-through path is unchanged by the A3b split.

    Regression guard: the usage/model/title tail still runs exactly once
    per poll, and still after the item POSTs — covering the early-return
    exits must not duplicate or reorder the normal path.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "u1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    monkeypatch.setattr(
        forwarder,
        "read_claude_context_state",
        lambda _bridge: {
            "context_window_size": 150_000,
            "current_usage": {"input_tokens": 10, "output_tokens": 1},
        },
    )
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dedupe = forwarder._ForwardDedupeState()
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_normal",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )

    usage_posts = [r for r in requests if r["type"] == "external_session_usage"]
    assert len(usage_posts) == 1
    # Usage still posts AFTER the item, never before or interleaved.
    item_index = next(
        i for i, r in enumerate(requests) if r["type"] == "external_conversation_item"
    )
    usage_index = next(i for i, r in enumerate(requests) if r["type"] == "external_session_usage")
    assert item_index < usage_index


@pytest.mark.asyncio
async def test_forward_available_items_side_channel_failure_does_not_corrupt_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A failing usage POST must not corrupt dedupe or the batch's own return.

    ``_post_forward_side_channel_updates`` guards its own work so a POST
    failure there is logged and swallowed rather than surfacing as the item
    batch's own exception, and the dedupe fields it guards (``usage`` /
    ``context_window``) are left unchanged so the next poll retries —
    exactly the pre-existing contract this refactor must preserve.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "u1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    monkeypatch.setattr(
        forwarder,
        "read_claude_context_state",
        lambda _bridge: {
            "context_window_size": 150_000,
            "current_usage": {"input_tokens": 10, "output_tokens": 1},
        },
    )

    def _handle_request(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        if payload["type"] == "external_session_usage":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dedupe = forwarder._ForwardDedupeState()
        updated = await forwarder._forward_available_items(
            client=client,
            session_id="conv_usage_fails",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )

    # The item batch's own outcome (the fall-through advance) is untouched
    # by the side-channel failure — no exception propagated out of it.
    assert updated.byte_offset == transcript_path.stat().st_size
    assert updated.seen_source_ids == ("u1:0:message",)
    # Dedupe fields the failed POST would have set stay behind, so the next
    # poll retries instead of believing a post that never landed.
    assert dedupe.usage is None
    assert dedupe.context_window is None


@pytest.mark.asyncio
async def test_forward_available_items_cancellation_skips_side_channel_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A cancelled poll must not start new usage/model/title POSTs.

    ``_post_forward_side_channel_updates`` runs from a ``finally`` guarding
    every NON-cancelled exit; ``asyncio.CancelledError`` is the one exit
    that must re-raise promptly instead of awaiting new POSTs.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "cancel-me",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": "hi"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    monkeypatch.setattr(
        forwarder,
        "read_claude_context_state",
        lambda _bridge: {
            "context_window_size": 150_000,
            "current_usage": {"input_tokens": 10, "output_tokens": 1},
        },
    )

    async def _cancelled_post(*_args: Any, **_kwargs: Any) -> None:
        """Simulate the poll being cancelled mid-item-POST."""
        raise asyncio.CancelledError

    monkeypatch.setattr(forwarder, "_post_external_conversation_item", _cancelled_post)

    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dedupe = forwarder._ForwardDedupeState()
        with pytest.raises(asyncio.CancelledError):
            await forwarder._forward_available_items(
                client=client,
                session_id="conv_cancel",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )

    assert requests == []
    assert dedupe.usage is None
    assert dedupe.posted_model is None


@pytest.mark.asyncio
async def test_ambiguous_authoritative_boundary_post_holds_cursor(tmp_path: Path) -> None:
    """
    An ambiguous authoritative POST does not consume the summary.

    The transcript snapshot is the authoritative compaction record. Without a
    confirmed successful POST, its cursor must remain before the summary so a
    later poll retries rather than silently losing resumable context.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript = tmp_path / "session.jsonl"
    _write_compaction_transcript(transcript)
    await _note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=str(transcript)
    )

    ambiguous = AsyncMock(side_effect=httpx.ReadError("connection dropped mid-response"))

    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", ambiguous
    ):
        handled = await _handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_ambiguous",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=_PostRetryTracker(),
        )

    assert handled is False
    state = _read_compaction_state(bridge_dir)
    assert state.persisted_seqs == ()
    assert state.pending is not None
    assert state.pending.seq == 1


@pytest.mark.asyncio
async def test_precompact_miss_is_claimed_from_authoritative_summary(tmp_path: Path) -> None:
    """
    P1-3: a summary skipped with no token and no boundary is counted as a miss.

    A durable ``isCompactSummary`` does not need a hook token. It claims a
    generation, persists, and records the missing PreCompact diagnostically.
    """
    _reset_compaction_skip_stats()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()  # no PreCompact, no persisted boundary
    transcript = tmp_path / "session.jsonl"
    _write_compaction_transcript(transcript)

    persist = _persist_mock()
    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
        ),
        patch(
            "omnigent.harnesses.claude_native.forwarder.read_transcript_path",
            return_value=transcript,
        ),
    ):
        handled = await _handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_miss",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=_PostRetryTracker(),
        )

    assert handled is True
    persist.assert_called_once()
    assert forwarder._compaction_skip_stats.precompact_miss == 1
    assert forwarder._compaction_skip_stats.expected_skip == 0

    # Re-reading the same summary is deterministic replay.
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        await _handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_miss",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=_PostRetryTracker(),
        )
    assert forwarder._compaction_skip_stats.precompact_miss == 1  # unchanged
    assert forwarder._compaction_skip_stats.expected_skip == 1
    persist.assert_called_once()


@pytest.mark.asyncio
async def test_standalone_hook_fallback_failure_retries_without_replaying_hook(
    tmp_path: Path,
) -> None:
    """
    A standalone completion is acknowledged before its bounded fallback.

    The hook cursor advances immediately because SessionStart is only an ack.
    A failed delayed fallback leaves the generation pending, and a later poll
    retries the same generation without replaying the hook.
    """
    bridge_dir = tmp_path / "bridge"
    # A lone compact SessionStart (no preceding PreCompact) — standalone.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "claude-standalone",
        },
    )
    start_state = forwarder.HookForwardState(event_cursor=0, byte_offset=0)

    request = httpx.Request("POST", "http://test/items")
    response = httpx.Response(503, request=request)
    failing = AsyncMock(
        side_effect=httpx.HTTPStatusError("boom", request=request, response=response)
    )

    async def _run_once(state: forwarder.HookForwardState) -> forwarder.HookForwardState:
        # The best-effort spinner status post is orthogonal to the durable
        # persist under test; stub it so the client mock stays quiet.
        with patch(
            "omnigent.harnesses.claude_native.forwarder._post_external_compaction_status",
            AsyncMock(return_value=None),
        ):
            return await forwarder._forward_available_status_events(
                client=AsyncMock(),
                session_id="conv_p22",
                bridge_dir=bridge_dir,
                state=state,
                retry_tracker=_PostRetryTracker(),
                dedupe=forwarder._ForwardDedupeState(),
                task_subjects={},
                task_statuses={},
                task_order=[],
            )

    # Poll 1: the hook only acknowledges and advances.
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", failing
    ):
        after_ack = await _run_once(start_state)
    assert failing.await_count == 0
    assert after_ack.event_cursor > start_state.event_cursor
    held = _read_compaction_state(bridge_dir)
    assert held.pending is not None
    assert held.acknowledged_at is not None
    minted_seq = held.pending.seq

    # The first fallback attempt fails and does not consume the generation.
    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", failing
        ),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await _maybe_persist_compaction_fallback(
            AsyncMock(),
            session_id="conv_p22",
            bridge_dir=bridge_dir,
            now=held.acknowledged_at + 2.0,
        )
    assert _read_compaction_state(bridge_dir).fallback_persisted_seq == 0

    # A later poll retries and records exactly one marked fallback.
    ok = _persist_mock()
    with patch("omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", ok):
        assert await _maybe_persist_compaction_fallback(
            AsyncMock(),
            session_id="conv_p22",
            bridge_dir=bridge_dir,
            now=held.acknowledged_at + 3.0,
        )
    assert ok.await_count == 1
    persisted = _read_compaction_state(bridge_dir)
    assert persisted.fallback_persisted_seq == minted_seq
    assert persisted.pending is not None
