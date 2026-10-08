"""Subagent status tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
import omnigent.harnesses.claude_native.main as claude_native
from omnigent.harnesses.claude_native.bridge import (
    ClaudeHookRecord,
    ClaudeTaskNotification,
    ClaudeTranscriptItem,
    TranscriptReadResult,
    record_hook_event,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _legacy_event_transport,
    _seed_subagent_on_disk,
    _start_recording_server,
    _task_notification_record,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["502", "request_error", "read_timeout"])
async def test_production_loop_recovers_child_after_prolonged_transient_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    """The live loop keeps a failed child item pending until AP recovers."""
    caplog.set_level(logging.CRITICAL, logger=forwarder.__name__)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="main-loop-recovery",
        agent_type="Explore",
        description="main loop transient recovery",
        tool_use_id="toolu_main_loop_recovery",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "main-loop-message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "recover me"}],
                },
            }
        ],
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-main-loop",
            "transcript_path": str(transcript_path),
        },
    )
    child_attempts = 0
    failed_attempts = 0
    delivered_source_ids: list[str] = []
    prolonged_outage = asyncio.Event()
    recovery_gate = asyncio.Event()
    delivered = asyncio.Event()
    recovery_scan = asyncio.Event()
    second_scan = asyncio.Event()
    outage = True
    restart_phase = False
    retry_gate_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal child_attempts, failed_attempts
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        is_batch = isinstance(body, list)
        is_child_item = isinstance(body, dict) and body.get("type") == "external_conversation_item"
        if is_batch or is_child_item:
            child_attempts += 1
            if outage:
                failed_attempts += 1
                if failed_attempts >= 30:
                    prolonged_outage.set()
                    await recovery_gate.wait()
                if failure == "request_error":
                    raise httpx.RequestError("AP unavailable", request=request)
                if failure == "read_timeout":
                    raise httpx.ReadTimeout("AP response lost", request=request)
                return httpx.Response(502, text="bad gateway")
            if is_batch:
                delivered_source_ids.extend(row["data"]["source_id"] for row in body)
            else:
                delivered_source_ids.append(body["data"]["source_id"])
            delivered.set()
            if is_batch:
                # The fork's history-recovery lane requires the recovery ack flags.
                return httpx.Response(
                    202,
                    json=[
                        {
                            "queued": False,
                            "item_id": row["data"]["source_id"],
                            "replayed": True,
                            "recovery": True,
                        }
                        for row in body
                    ],
                )
            return httpx.Response(
                202,
                json={
                    "queued": False,
                    "item_id": "recovered",
                    "replayed": True,
                    "recovery": True,
                },
            )
        if isinstance(body, dict) and body.get("type") == "external_subagent_start":
            return httpx.Response(202, json={"child_session_id": "conv_main_loop_child"})
        return httpx.Response(202, json={})

    original_retry_delay = forwarder._PostRetryTracker.retry_delay_s
    original_pending_prefix = forwarder._PostRetryTracker.has_pending_retry_prefix

    def release_child_backoff(self: forwarder._PostRetryTracker, key: str) -> float | None:
        nonlocal retry_gate_calls
        retry_gate_calls += 1
        if key.startswith(("subagent_batch:", "subagent_item:", "subagent_recovery:")):
            return None
        return original_retry_delay(self, key)

    def release_child_pending_prefix(self: forwarder._PostRetryTracker, prefix: str) -> bool:
        if prefix.startswith(("subagent_batch:", "subagent_item:")):
            return False
        return original_pending_prefix(self, prefix)

    monkeypatch.setattr(forwarder._PostRetryTracker, "retry_delay_s", release_child_backoff)
    monkeypatch.setattr(
        forwarder._PostRetryTracker,
        "has_pending_retry_prefix",
        release_child_pending_prefix,
    )

    async def skip_pane_signals(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(forwarder, "_forward_pane_signals", skip_pane_signals)

    async def passthrough_state(**kwargs: Any) -> Any:
        return kwargs["state"]

    monkeypatch.setattr(forwarder, "_forward_available_deltas", passthrough_state)
    monkeypatch.setattr(forwarder, "_forward_available_items", passthrough_state)
    monkeypatch.setattr(forwarder, "_forward_available_status_events", passthrough_state)

    async def skip_cost(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(forwarder, "_forward_session_cost", skip_cost)
    monkeypatch.setattr(forwarder, "_forward_model_from_status", skip_cost)
    original_scan = forwarder._forward_available_subagents

    async def observe_scan(**kwargs: Any) -> forwarder.SubagentForwardState:
        result = await original_scan(**kwargs)
        if restart_phase:
            second_scan.set()
        elif delivered.is_set():
            recovery_scan.set()
        return result

    monkeypatch.setattr(forwarder, "_forward_available_subagents", observe_scan)

    @contextlib.asynccontextmanager
    async def open_mock_client(*_args: Any, **_kwargs: Any) -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://ap"
        ) as client:
            yield client

    monkeypatch.setattr("omnigent.cli_auth.open_server_client", open_mock_client)

    async def run_forwarder() -> asyncio.Task[None]:
        return asyncio.create_task(
            forwarder.forward_claude_transcript_to_session(
                base_url="http://ap",
                headers={},
                session_id="conv_main_loop_parent",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.0,
            )
        )

    task = await run_forwarder()
    try:
        try:
            await asyncio.wait_for(prolonged_outage.wait(), timeout=30.0)
        except TimeoutError as exc:
            raise AssertionError(
                f"child attempts={child_attempts}, retry gate calls={retry_gate_calls}"
            ) from exc
        outage = False
        recovery_gate.set()
        await asyncio.wait_for(delivered.wait(), timeout=10.0)
        await asyncio.wait_for(recovery_scan.wait(), timeout=10.0)
        assert failed_attempts >= 30
        assert delivered_source_ids == ["main-loop-message:0:message"]

        restart_phase = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        task = await run_forwarder()
        await asyncio.wait_for(second_scan.wait(), timeout=10.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert delivered_source_ids == ["main-loop-message:0:message"]


@pytest.mark.asyncio
async def test_forwarder_ignores_subagent_stop_failure_hook(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A subagent's ``StopFailure`` must not flip the parent session failed.

    Claude Code subagents (spawned via the Agent tool for e.g. Explore)
    inherit the parent's hook settings and write to the same
    ``hooks.jsonl``. A subagent failing must not mark the *parent* turn
    failed — the parent is still running while it awaits the Agent tool
    result. Subagent transcripts live under a ``subagents/`` directory,
    which the forwarder uses to distinguish them from parent events.
    (Running/idle are no longer hook-derived; ``StopFailure`` →
    ``failed`` is the only mapped status left, so this is the surviving
    subagent-skip case.)
    """
    caplog.set_level(logging.INFO, logger=forwarder.__name__)
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_transcript = tmp_path / "session" / "subagents" / "agent-abc.jsonl"
    subagent_transcript.parent.mkdir(parents=True, exist_ok=True)
    subagent_transcript.write_text("", encoding="utf-8")

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "parent-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Subagent fails first — this must NOT surface as the parent failing.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "subagent-session",
            "transcript_path": str(subagent_transcript),
            "error": "server_error",
        },
    )
    # Parent turn fails — this SHOULD surface as the one failed edge.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "parent-session",
            "transcript_path": str(transcript_path),
        },
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # Exactly one status POST: the parent's failed. The subagent
        # StopFailure (recorded first) must be skipped, so no second
        # status POST ever arrives — the bounded wait below must time out.
        first = await _get_recorded_request(server)
        with pytest.raises(AssertionError):
            await _get_recorded_request(server, timeout_s=0.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    context = first["body"]["data"].pop("failure_context")
    assert context["native_session_id"] == "parent-session"
    assert first["body"] == {
        "type": "external_session_status",
        "data": {"status": "failed"},
    }
    observations = [
        r for r in caplog.records if getattr(r, "event_name", None) == "native_failure_observed"
    ]
    assert len(observations) == 1
    attrs = observations[0].attributes
    assert attrs["native_session_id"] == "subagent-session"
    assert attrs["native_parent_session_id"] == "parent-session"
    assert attrs["native_error_category"] == "server_error"
    assert attrs["failure_decision"] == "suppressed"
    assert attrs["suppression_reason"] == "foreign_native_session_id"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subagent_session_id", "subagent_transcript_name", "subagent_fields"),
    [
        # Background subagent: foreign session id, non-``subagents/`` path.
        pytest.param("bg-agent-session", "bg-agent.jsonl", {}, id="background-by-session-id"),
        # In-process subagent: parent's session id and path; only ``agent_id`` marks it.
        pytest.param(
            "parent-session",
            "session.jsonl",
            {
                "agent_id": "a4892977eed616593",
                "agent_type": "general-purpose",
                "error": "unknown",
                "last_assistant_message": 'API Error: 499 {"error_code":"CANCELLED","message":""}',
            },
            id="in-process-by-agent-id",
        ),
    ],
)
async def test_forwarder_ignores_subagent_stop_failure_without_subagents_path(
    tmp_path: Path,
    subagent_session_id: str,
    subagent_transcript_name: str,
    subagent_fields: dict[str, str],
) -> None:
    """
    A subagent's ``StopFailure`` must not flip the parent to ``failed``.

    The subsequent parent ``Stop`` acts as an anchor: the forwarder must
    emit exactly one POST (``idle``), proving it ran and that the
    subagent's ``StopFailure`` was silently skipped.
    """
    bridge_dir = tmp_path / "bridge"
    parent_transcript = tmp_path / "session.jsonl"
    parent_transcript.write_text("", encoding="utf-8")
    subagent_transcript = tmp_path / subagent_transcript_name
    subagent_transcript.write_text("", encoding="utf-8")

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "parent-session",
            "transcript_path": str(parent_transcript),
        },
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": subagent_session_id,
            "transcript_path": str(subagent_transcript),
            **subagent_fields,
        },
    )
    # Parent turn ends normally — anchor that proves the forwarder ran.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "parent-session",
            "transcript_path": str(parent_transcript),
        },
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        first = await _get_recorded_request(server)
        with pytest.raises(AssertionError):
            await _get_recorded_request(server, timeout_s=0.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # If the subagent StopFailure was wrongly forwarded, first would be
    # ``failed``; only the parent's ``idle`` should arrive.
    assert first["body"] == {
        "type": "external_session_status",
        "data": {"status": "idle", "background_task_count": 0, "turn_completed": True},
    }


@pytest.mark.asyncio
async def test_forwarder_parent_stop_failure_not_affected_by_background_session_check(
    tmp_path: Path,
) -> None:
    """
    A ``StopFailure`` carrying the parent's own session id is still
    forwarded as ``failed`` when the session id check is active.
    """
    bridge_dir = tmp_path / "bridge"
    parent_transcript = tmp_path / "session.jsonl"
    parent_transcript.write_text("", encoding="utf-8")

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "parent-session",
            "transcript_path": str(parent_transcript),
        },
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "parent-session",
            "transcript_path": str(parent_transcript),
        },
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        first = await _get_recorded_request(server)
        with pytest.raises(AssertionError):
            await _get_recorded_request(server, timeout_s=0.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    context = first["body"]["data"].pop("failure_context")
    assert context["native_agent_role"] == "session_agent"
    assert first["body"] == {
        "type": "external_session_status",
        "data": {"status": "failed"},
    }


def test_is_subagent_hook_record_rotation_race(tmp_path: Path) -> None:
    """
    A StopFailure with an old (pre-rotation) parent session id must NOT be
    classified as a subagent when the seen set includes that old id.
    """
    record = ClaudeHookRecord(
        event_cursor=1,
        byte_offset=100,
        event_name="StopFailure",
        claude_session_id="parent-old",
        transcript_path=tmp_path / "session.jsonl",
    )
    # Both old and new ids are seen — old is still a parent id.
    assert not forwarder._is_subagent_hook_record(
        record, parent_claude_session_ids={"parent-old", "parent-new"}
    )
    # Only the new id is seen — old id would be wrongly dropped without
    # the seen set.
    assert forwarder._is_subagent_hook_record(record, parent_claude_session_ids={"parent-new"})


def test_is_subagent_hook_record_empty_seen_set_uses_path(tmp_path: Path) -> None:
    """
    When the seen set is empty (no pin yet), the path check alone decides.
    """
    subagent_path = tmp_path / "session" / "subagents" / "agent-abc.jsonl"
    parent_path = tmp_path / "session.jsonl"

    # Subagent path → True (path check catches it).
    assert forwarder._is_subagent_hook_record(
        ClaudeHookRecord(
            event_cursor=1,
            byte_offset=50,
            event_name="StopFailure",
            claude_session_id="any",
            transcript_path=subagent_path,
        ),
        parent_claude_session_ids=set(),
    )
    # Non-subagent path → False (conservative).
    assert not forwarder._is_subagent_hook_record(
        ClaudeHookRecord(
            event_cursor=2,
            byte_offset=100,
            event_name="StopFailure",
            claude_session_id="any",
            transcript_path=parent_path,
        ),
        parent_claude_session_ids=set(),
    )
    # No path → False (conservative).
    assert not forwarder._is_subagent_hook_record(
        ClaudeHookRecord(
            event_cursor=3,
            byte_offset=150,
            event_name="StopFailure",
            claude_session_id="any",
            transcript_path=None,
        ),
        parent_claude_session_ids=set(),
    )


@pytest.mark.asyncio
async def test_forwarder_ignores_subagent_stop_hook(
    tmp_path: Path,
) -> None:
    """
    A subagent's ``Stop`` must not deliver the parent session as idle.

    Claude Code Task subagents inherit the parent's hook settings and write to
    the same ``hooks.jsonl``. A subagent finishing must NOT post ``idle`` for
    the parent — the parent turn is still running while it awaits the Agent
    tool result, and a parent ``idle`` triggers terminal sub-agent delivery.
    Subagent transcripts live under a ``subagents/`` directory, which the
    forwarder uses to skip them. We record a subagent ``Stop`` ahead of the
    parent ``Stop``: the one and only idle POST must be the parent's.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_transcript = tmp_path / "session" / "subagents" / "agent-abc.jsonl"
    subagent_transcript.parent.mkdir(parents=True, exist_ok=True)
    subagent_transcript.write_text("", encoding="utf-8")

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "parent-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Subagent stops first — this must NOT surface as the parent going idle.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "subagent-session",
            "transcript_path": str(subagent_transcript),
        },
    )
    # Parent turn ends — this SHOULD surface as the one idle edge.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "parent-session",
            "transcript_path": str(transcript_path),
        },
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # Exactly one status POST: the parent's idle. The subagent ``Stop``
        # (recorded first) must be skipped, so no second status POST arrives —
        # the bounded wait below must time out.
        first = await _get_recorded_request(server)
        with pytest.raises(AssertionError):
            await _get_recorded_request(server, timeout_s=0.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert first["body"] == {
        "type": "external_session_status",
        "data": {"status": "idle", "background_task_count": 0, "turn_completed": True},
    }


@pytest.mark.parametrize("supports_idle", [True, False], ids=["new-server", "old-server"])
async def test_subagent_idle_observation_preserves_timing_and_deduplication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    supports_idle: bool,
) -> None:
    """The same five-second gap emits once, resets on activity, and survives restart."""
    now = 1000.0
    monkeypatch.setattr(
        forwarder, "time", SimpleNamespace(time=lambda: now, monotonic=time.monotonic)
    )
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.touch()
    child_path = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="idle-worker",
        agent_type="Explore",
        description="long-running task",
        tool_use_id="toolu_idle",
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "idle-worker": forwarder.SubagentEntry(
                subagent_id="idle-worker", child_conversation_id="conv_child"
            )
        }
    )
    status_events: list[dict[str, Any]] = []
    capability = forwarder._SubagentStatusCapability()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/conv_child/events"
        body = json.loads(request.content)
        if isinstance(body, list):
            return httpx.Response(202, json=[{"item_id": "item"} for _ in body])
        status_events.append(body)
        if body["type"] == "subagent.status" and not supports_idle:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "invalid_input",
                        "message": "Unknown event type: 'subagent.status'. Allowed types: []",
                    }
                },
            )
        return httpx.Response(202, json={"queued": False})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:

        async def tick() -> None:
            nonlocal state
            state = await forwarder._forward_available_subagents(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                agent_name="claude-native-ui",
                start_retry_tracker=forwarder._PostRetryTracker(),
                item_retry_tracker=forwarder._PostRetryTracker(),
                status_retry_tracker=forwarder._PostRetryTracker(),
                status_capability=capability,
            )

        await tick()
        assert status_events == []  # No idle observation before the first activity.
        for cycle in range(2):
            with child_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "isSidechain": True,
                            "type": "assistant",
                            "uuid": f"message-{cycle}",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": f"working {cycle}"}],
                            },
                        }
                    )
                    + "\n"
                )
            await tick()
            assert status_events[-1] == {
                "type": "external_session_status",
                "data": {"status": "running"},
            }
            now += forwarder._SUBAGENT_IDLE_THRESHOLD_S
            await tick()
            count = len(status_events)
            assert count == cycle * 2 + 1
            now += 0.001
            await tick()
            if supports_idle or cycle == 0:
                count += 1
                assert status_events[-1] == {"type": "subagent.status", "data": {"idle": True}}
            await tick()
            state = forwarder._read_subagent_forward_state(bridge_dir)
            await tick()
            assert len(status_events) == count


async def test_subagent_idle_unsupported_cache_covers_other_children(tmp_path: Path) -> None:
    """One old-server rejection suppresses other children's idle events, not failures."""
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.touch()
    bridge_dir = tmp_path / "bridge"
    capability = forwarder._SubagentStatusCapability()
    state = forwarder.SubagentForwardState(subagents={})
    events: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        events.append(body)
        if body["type"] == "subagent.status":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "invalid_input",
                        "message": "Unknown event type: 'subagent.status'. Allowed types: []",
                    }
                },
            )
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for child_id in ("first", "second", "failed"):
            _seed_subagent_on_disk(
                transcript_path=transcript_path,
                subagent_id=child_id,
                agent_type="Explore",
                description="worker",
                tool_use_id=f"toolu_{child_id}",
            )
            state.subagents[child_id] = forwarder.SubagentEntry(
                subagent_id=child_id,
                child_conversation_id=f"conv_{child_id}",
                last_activity_ts=time.time() - 60,
                last_status="running",
                delivery_error="lost output" if child_id == "failed" else None,
            )
            state = await forwarder._forward_available_subagents(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                agent_name="claude-native-ui",
                start_retry_tracker=forwarder._PostRetryTracker(),
                item_retry_tracker=forwarder._PostRetryTracker(),
                status_retry_tracker=forwarder._PostRetryTracker(),
                status_capability=capability,
            )
    assert events == [
        {"type": "subagent.status", "data": {"idle": True}},
        {"type": "external_session_status", "data": {"status": "failed", "output": "lost output"}},
    ]


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (400, {"error": {"code": "invalid_input", "message": "Invalid idle payload"}}),
        (400, {"error": {"code": "invalid_input", "message": "Unknown event type: 'other'."}}),
        (400, {"error": {"code": "invalid_input", "message": None}}),
        (400, ["malformed error"]),
        (400, "not json"),
        (401, {"error": {"code": "unauthorized"}}),
        (403, {"error": {"code": "forbidden"}}),
        (503, {"error": {"code": "unavailable"}}),
    ],
)
async def test_subagent_idle_other_errors_are_not_suppressed(status: int, payload: Any) -> None:
    """A malformed request, auth failure, or outage must still reach normal retry handling."""
    events: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)

    capability = forwarder._SubagentStatusCapability()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                await capability.post_idle(client, session_id="conv_child")
    assert events == [{"type": "subagent.status", "data": {"idle": True}}] * 2


@pytest.mark.parametrize("previous_status", ["running", "idle", "failed"])
async def test_subagent_idle_observation_retries_and_resumes_existing_checkpoint(
    tmp_path: Path, previous_status: str
) -> None:
    """Only successful delivery advances dedupe; an idle checkpoint stays deduped."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.touch()
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="idle-worker",
        agent_type="Explore",
        description="long-running task",
        tool_use_id="toolu_idle",
    )
    forwarder._write_subagent_forward_state(
        bridge_dir,
        forwarder.SubagentForwardState(
            subagents={
                "idle-worker": forwarder.SubagentEntry(
                    subagent_id="idle-worker",
                    child_conversation_id="conv_child",
                    last_activity_ts=time.time() - 60,
                    last_status=previous_status,
                )
            }
        ),
    )
    state = forwarder._read_subagent_forward_state(bridge_dir)
    attempts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(json.loads(request.content))
        return httpx.Response(503 if len(attempts) == 1 else 202, json={})

    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for tick in range(3):
            state = await forwarder._forward_available_subagents(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                agent_name="claude-native-ui",
                start_retry_tracker=forwarder._PostRetryTracker(),
                item_retry_tracker=forwarder._PostRetryTracker(),
                status_retry_tracker=tracker,
            )
            if tick == 0 and previous_status != "idle":
                assert state.subagents["idle-worker"].last_status == previous_status
                assert forwarder._read_subagent_forward_state(bridge_dir) == state

    assert attempts == (
        []
        if previous_status in {"idle", "failed"}
        else [{"type": "subagent.status", "data": {"idle": True}}] * 2
    )
    # The terminal-status latch suppresses idle after a terminal status.
    assert state.subagents["idle-worker"].last_status == (
        "failed" if previous_status == "failed" else "idle"
    )


@pytest.mark.asyncio
async def test_parent_output_forwards_while_child_history_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck child request cannot prevent the next live parent poll."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="blocked-child",
        agent_type="Explore",
        description="blocked history",
        tool_use_id="toolu_blocked_child",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "blocked-child-item",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "old output"}],
                },
            }
        ],
    )
    forwarder._write_subagent_forward_state(
        bridge_dir,
        forwarder.SubagentForwardState(
            subagents={
                "blocked-child": forwarder.SubagentEntry(
                    subagent_id="blocked-child",
                    child_conversation_id="conv_blocked_child",
                )
            }
        ),
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    child_request_started = asyncio.Event()
    release_child = asyncio.Event()
    parent_item_forwarded = asyncio.Event()
    snapshot_updates: list[tuple[str, dict[str, str], bool]] = []

    class RecordingSnapshots:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> RecordingSnapshots:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def update(
            self,
            parent_id: str,
            children: dict[str, str],
            *,
            retired: bool = False,
        ) -> None:
            snapshot_updates.append((parent_id, children, retired))

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if isinstance(body, list):
            child_request_started.set()
            await release_child.wait()
            return httpx.Response(
                202,
                json=[
                    {"queued": False, "item_id": f"item-{index}"} for index, _ in enumerate(body)
                ],
            )
        if (
            request.url.path == "/v1/sessions/conv_parent/events"
            and isinstance(body, dict)
            and body.get("type") == "external_conversation_item"
        ):
            parent_item_forwarded.set()
        return httpx.Response(202, json={})

    @contextlib.asynccontextmanager
    async def open_mock_client(*_args: Any, **_kwargs: Any) -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://ap"
        ) as client:
            yield client

    monkeypatch.setattr("omnigent.cli_auth.open_server_client", open_mock_client)
    monkeypatch.setattr(forwarder, "NativeSubagentSnapshotPublisher", RecordingSnapshots)
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url="http://ap",
            headers={},
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        await asyncio.wait_for(child_request_started.wait(), timeout=2.0)
        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "fresh-parent-item",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "fresh output"}],
                        },
                    }
                )
                + "\n"
            )
        await asyncio.wait_for(parent_item_forwarded.wait(), timeout=1.0)
        assert snapshot_updates == []
    finally:
        release_child.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def _child_prompt_record(*, uuid: str, text: str, timestamp: str) -> dict[str, Any]:
    """Build the child's original spawn prompt (precedes every notification)."""
    return {
        "isSidechain": True,
        "type": "user",
        "uuid": uuid,
        "timestamp": timestamp,
        "message": {"role": "user", "content": text},
    }


def _child_resume_record(*, uuid: str, text: str, timestamp: str) -> dict[str, Any]:
    """Build a real-shape coordinator SendMessage resume record."""
    return {
        "isSidechain": True,
        "type": "user",
        "isMeta": True,
        "origin": {"kind": "coordinator"},
        "agentId": "a17316763c337191b",
        "promptId": f"prompt-{uuid}",
        "uuid": uuid,
        "timestamp": timestamp,
        "message": {"role": "user", "content": text},
    }


def _child_assistant_record(*, uuid: str, text: str, timestamp: str) -> dict[str, Any]:
    """Build a trailing assistant record (never a resume prompt)."""
    return {
        "isSidechain": True,
        "type": "assistant",
        "uuid": uuid,
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


@pytest.mark.parametrize("status", ["completed", "failed", "stopped", "killed"])
async def test_subagent_watcher_uses_correlated_terminal_notification(
    tmp_path: Path,
    status: str,
) -> None:
    """The parent task notification, not child silence, ends native work."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    terminal_output = f"terminal-{status}"
    transcript_path.write_text(
        json.dumps(
            _task_notification_record(
                tool_use_id="toolu_terminal",
                status=status,
                result=terminal_output,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="terminal1",
        agent_type="Explore",
        description="finish reliably",
        tool_use_id="toolu_terminal",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "assistant-opener",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I will inspect it."}],
                },
            },
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "assistant-tool",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_child_read",
                            "name": "Read",
                            "input": {"file_path": "spec.md"},
                        }
                    ],
                },
            },
            {
                "isSidechain": True,
                "type": "user",
                "uuid": "child-tool-result",
                "parentUuid": "assistant-tool",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_child_read",
                            "content": "spec contents",
                            "is_error": False,
                        }
                    ],
                },
            },
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "terminal1": forwarder.SubagentEntry(
                subagent_id="terminal1",
                child_conversation_id="conv_child_terminal",
                tool_use_id="toolu_terminal",
            )
        }
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler),
        base_url="http://ap",
    ) as client:
        result = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert status_posts == [{"status": status, "output": terminal_output}]
    child_state = result.subagents["terminal1"]
    assert child_state.terminal_status == status
    assert child_state.terminal_output == terminal_output
    assert child_state.terminal_replayed is False
    assert child_state.last_status == status


async def test_subagent_watcher_restores_compacted_local_terminal_metadata(
    tmp_path: Path,
) -> None:
    """A cold-resume transcript settles its child from compact-carried metadata."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    records = claude_native._claude_transcript_records_from_session_items(
        [
            {
                "type": "function_call",
                "call_id": "toolu_compact_terminal",
                "name": "Agent",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "toolu_compact_terminal",
                "output": "historical terminal output",
                "tool_status": "completed",
                "is_async": True,
            },
            {
                "type": "compaction",
                "summary": "summary",
                "token_count": 10,
                "compacted_messages": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "summary"}],
                    }
                ],
            },
        ],
        session_id="conv_parent",
        external_session_id="claude-session",
        cwd=tmp_path,
        bridge_dir=bridge_dir,
    )
    transcript_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="compact1",
        agent_type="Explore",
        description="historical child",
        tool_use_id="toolu_compact_terminal",
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "compact1": forwarder.SubagentEntry(
                subagent_id="compact1",
                child_conversation_id="conv_child_compact",
                tool_use_id="toolu_compact_terminal",
            )
        }
    )
    status_posts: list[dict[str, Any]] = []
    status_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_attempts
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
            status_attempts += 1
            if status_attempts == 1:
                return httpx.Response(503, json={"error": "runner reconnecting"})
        return httpx.Response(202, json={})

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
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        reloaded = forwarder._read_subagent_forward_state(bridge_dir)
        result = await forwarder._forward_available_subagents(
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

    assert status_posts == [
        {
            "status": "completed",
            "output": "historical terminal output",
            "replayed": True,
        },
        {
            "status": "completed",
            "output": "historical terminal output",
            "replayed": True,
        },
    ]
    assert first.subagents["compact1"].last_status is None
    assert reloaded.subagents["compact1"].terminal_replayed is True
    child_state = result.subagents["compact1"]
    assert child_state.terminal_status == "completed"
    assert child_state.terminal_output == "historical terminal output"
    assert child_state.terminal_replayed is True
    assert child_state.last_status == "completed"
    assert result.parent_byte_offset == transcript_path.stat().st_size


async def test_subagent_terminal_notification_waits_for_late_meta_registration(
    tmp_path: Path,
) -> None:
    """A parent terminal record is retained until its child meta file appears."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            _task_notification_record(
                task_id="late1",
                tool_use_id="toolu_late_meta",
                status="completed",
                result="late registration result",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True)
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_subagent_start":
            return httpx.Response(
                202,
                json={"child_session_id": "conv_child_late", "existing": False},
            )
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
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
        assert first.pending_terminal_notifications == {
            "late1": ("completed", "late registration result", False, None)
        }
        _seed_subagent_on_disk(
            transcript_path=transcript_path,
            subagent_id="late1",
            agent_type="Explore",
            description="late metadata",
            tool_use_id="toolu_late_meta",
        )
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder._read_subagent_forward_state(bridge_dir),
            agent_name="claude-native-ui",
            **trackers,
        )

    assert status_posts == [{"status": "completed", "output": "late registration result"}]
    assert second.pending_terminal_notifications == {}
    assert second.subagents["late1"].last_status == "completed"
    assert second.subagents["late1"].terminal_replayed is False


async def test_subagent_terminal_notification_matches_resumed_task_id(
    tmp_path: Path,
) -> None:
    """A completion under a new tool-use id still settles its task entry."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="resumed1",
        agent_type="Explore",
        description="resumed via SendMessage",
        tool_use_id="toolu_spawn_a",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "assistant-opener",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I will inspect it."}],
                },
            },
        ],
    )
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                _task_notification_record(
                    task_id="resumed1",
                    tool_use_id="toolu_sendmessage_b",
                    status="completed",
                    result="resumed completion",
                )
            )
            + "\n"
        )
    state = forwarder.SubagentForwardState(
        subagents={
            "resumed1": forwarder.SubagentEntry(
                subagent_id="resumed1",
                child_conversation_id="conv_child_resumed",
                tool_use_id="toolu_spawn_a",
            )
        }
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler),
        base_url="http://ap",
    ) as client:
        result = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert status_posts == [{"status": "completed", "output": "resumed completion"}]
    assert result.pending_terminal_notifications == {}
    child_state = result.subagents["resumed1"]
    assert child_state.terminal_status == "completed"
    assert child_state.terminal_output == "resumed completion"
    assert child_state.last_status == "completed"


async def test_subagent_old_tool_use_id_pending_key_drains_on_registration(
    tmp_path: Path,
) -> None:
    """A pre-task-id state file still settles its child on registration."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    (bridge_dir / "subagent_forwarder.json").write_text(
        json.dumps(
            {
                "subagents": {
                    "other1": {
                        "child_conversation_id": "conv_child_other",
                        "parent_subagent_id": None,
                        "tool_use_id": "toolu_other",
                        "byte_offset": 0,
                        "seen_source_ids": [],
                        "last_activity_ts": None,
                        "last_status": "running",
                        "delivery_error": None,
                        "quiet_terminal_output": None,
                        "terminal_status": None,
                        "terminal_output": None,
                        "terminal_replayed": False,
                        "activity_unverified": False,
                        "status_reconcile_pending": False,
                        "recovery_watermark": None,
                        "parent_recovery_watermark": None,
                        "recovery_after": None,
                        "recovery_seen_source_ids": [],
                    }
                },
                "parent_byte_offset": 0,
                "parent_line_cursor": 0,
                "pending_registration_watermarks": {},
                "pending_terminal_notifications": {
                    "toolu_old": {
                        "status": "completed",
                        "output": "old result",
                        "replayed": False,
                    }
                },
                "terminal_recovery_version": 1,
                "legacy_terminal_recovery_watermark": None,
                "updated_at": 0.0,
            }
        ),
        encoding="utf-8",
    )
    loaded = forwarder._read_subagent_forward_state(bridge_dir)
    assert loaded.terminal_recovery_version == 1
    assert loaded.pending_terminal_notifications == {
        "toolu_old": ("completed", "old result", False, None)
    }
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="old1",
        agent_type="Explore",
        description="late metadata, old pending key",
        tool_use_id="toolu_old",
    )
    status_posts: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_subagent_start":
            return httpx.Response(
                202,
                json={"child_session_id": "conv_child_old", "existing": False},
            )
        if body.get("type") == "external_session_status":
            status_posts.append((request.url.path, body["data"]))
        return httpx.Response(202, json={})

    trackers = {
        "start_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        "item_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        "status_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
    }
    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler), base_url="http://ap"
    ) as client:
        result = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=loaded,
            agent_name="claude-native-ui",
            **trackers,
        )

    assert (
        "/v1/sessions/conv_child_old/events",
        {"status": "completed", "output": "old result"},
    ) in (status_posts)
    assert result.pending_terminal_notifications == {}
    assert result.subagents["old1"].last_status == "completed"
    assert result.subagents["old1"].terminal_replayed is False
    # The unrelated legacy running entry without evidence is classified
    # unverified by the restored v2 three-way pass.
    assert result.subagents["other1"].last_status == "activity_unverified"


async def test_subagent_v2_recovery_heals_task_id_mismatch(tmp_path: Path) -> None:
    """One v2 pass settles a v1-stuck running child from task-id evidence."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="heal1",
        agent_type="Explore",
        description="stuck since the v1 pass",
        tool_use_id="toolu_spawn_a",
    )
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                _task_notification_record(
                    task_id="heal1",
                    tool_use_id="toolu_sendmessage_b",
                    status="completed",
                    result="healed completion",
                )
            )
            + "\n"
        )
    state = forwarder.SubagentForwardState(
        subagents={
            "heal1": forwarder.SubagentEntry(
                subagent_id="heal1",
                child_conversation_id="conv_child_heal",
                tool_use_id="toolu_spawn_a",
                last_status="running",
            )
        },
        terminal_recovery_version=1,
    )
    forwarder._write_subagent_forward_state(bridge_dir, state)
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler),
        base_url="http://ap",
    ) as client:
        trackers = {
            "start_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
            "item_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
            "status_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        }
        first = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder._read_subagent_forward_state(bridge_dir),
            agent_name="claude-native-ui",
            **trackers,
        )
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

    assert first.terminal_recovery_version == 2
    assert reloaded.terminal_recovery_version == 2
    assert second.terminal_recovery_version == 2
    child_state = first.subagents["heal1"]
    assert child_state.terminal_status == "completed"
    assert child_state.terminal_output == "healed completion"
    assert child_state.terminal_replayed is True
    assert child_state.last_status == "completed"
    assert status_posts == [
        {"status": "completed", "output": "healed completion", "replayed": True}
    ]


async def test_subagent_resume_reopens_terminal_on_user_prompt(
    tmp_path: Path,
) -> None:
    """A coordinator resume newer than the completion re-runs a settled child."""
    t0 = "2026-09-10T13:21:51.088Z"
    t1 = "2026-09-10T13:22:05.710Z"
    t2 = "2026-09-10T13:23:13.274Z"
    t3 = "2026-09-10T13:26:05.710Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            _task_notification_record(
                task_id="resume1",
                tool_use_id="toolu_spawn_a",
                status="completed",
                result="first completion",
                timestamp=t1,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    child_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="resume1",
        agent_type="Explore",
        description="resumable child",
        tool_use_id="toolu_spawn_a",
        transcript_records=[
            _child_prompt_record(
                uuid="spawn-prompt", text="Inspect the first host.", timestamp=t0
            ),
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "resume1": forwarder.SubagentEntry(
                subagent_id="resume1",
                child_conversation_id="conv_child_resume",
                tool_use_id="toolu_spawn_a",
            )
        }
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler),
        base_url="http://ap",
    ) as client:
        trackers = {
            "start_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
            "item_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
            "status_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        }
        settled = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            **trackers,
        )
        assert status_posts == [{"status": "completed", "output": "first completion"}]
        assert settled.subagents["resume1"].last_status == "completed"
        assert settled.subagents["resume1"].terminal_observed_at == t1

        with child_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _child_resume_record(
                        uuid="sendmessage-resume",
                        text=(
                            "The coordinator sent a message while you were working:\n"
                            "Resume where you stopped. Now check the second host too."
                        ),
                        timestamp=t2,
                    )
                )
                + "\n"
            )
        reopened = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=settled,
            agent_name="claude-native-ui",
            **trackers,
        )
        assert status_posts == [
            {"status": "completed", "output": "first completion"},
            {"status": "running"},
        ]
        assert reopened.subagents["resume1"].terminal_status is None
        assert reopened.subagents["resume1"].terminal_observed_at is None
        assert reopened.subagents["resume1"].last_status == "running"
        # The reopen rode the same batch checkpoint as the resume record, so
        # a checkpoint interruption (restart before the next poll) keeps it.
        persisted = forwarder._read_subagent_forward_state(bridge_dir).subagents["resume1"]
        assert persisted.terminal_status is None
        assert persisted.terminal_observed_at is None

        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _task_notification_record(
                        task_id="resume1",
                        tool_use_id="toolu_sendmessage_b",
                        status="completed",
                        result="second completion",
                        timestamp=t3,
                    )
                )
                + "\n"
            )
        completed = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=reopened,
            agent_name="claude-native-ui",
            **trackers,
        )

    assert status_posts == [
        {"status": "completed", "output": "first completion"},
        {"status": "running"},
        {"status": "completed", "output": "second completion"},
    ]
    assert completed.pending_terminal_notifications == {}
    assert completed.subagents["resume1"].terminal_status == "completed"
    assert completed.subagents["resume1"].terminal_observed_at == t3
    assert completed.subagents["resume1"].last_status == "completed"


async def test_subagent_late_registration_original_prompt_does_not_reopen(
    tmp_path: Path,
) -> None:
    """A parked completion survives the pre-registration prompt it predates."""
    t0 = "2026-09-10T13:21:51.088Z"
    t1 = "2026-09-10T13:22:05.710Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            _task_notification_record(
                task_id="lateb1",
                tool_use_id="toolu_late_spawn",
                status="completed",
                result="parked completion",
                timestamp=t1,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_subagent_start":
            return httpx.Response(
                202,
                json={"child_session_id": "conv_child_lateb", "existing": False},
            )
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
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
        assert first.pending_terminal_notifications == {
            "lateb1": ("completed", "parked completion", False, t1)
        }
        # The child's meta file appears late; its transcript holds only the
        # original spawn prompt, which is older than the parked completion.
        _seed_subagent_on_disk(
            transcript_path=transcript_path,
            subagent_id="lateb1",
            agent_type="Explore",
            description="late metadata",
            tool_use_id="toolu_late_spawn",
            transcript_records=[
                _child_prompt_record(uuid="spawn-prompt", text="Inspect the host.", timestamp=t0),
            ],
        )
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder._read_subagent_forward_state(bridge_dir),
            agent_name="claude-native-ui",
            **trackers,
        )

    assert status_posts == [{"status": "completed", "output": "parked completion"}]
    assert second.pending_terminal_notifications == {}
    assert second.subagents["lateb1"].terminal_status == "completed"
    assert second.subagents["lateb1"].last_status == "completed"


async def test_subagent_original_prompt_retry_after_503_does_not_reopen(
    tmp_path: Path,
) -> None:
    """A 503-retried spawn prompt is still older than the completion."""
    t0 = "2026-09-10T13:21:51.088Z"
    t1 = "2026-09-10T13:22:05.710Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            _task_notification_record(
                task_id="retry1",
                tool_use_id="toolu_retry",
                status="completed",
                result="completion",
                timestamp=t1,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="retry1",
        agent_type="Explore",
        description="flaky upload",
        tool_use_id="toolu_retry",
        transcript_records=[
            _child_prompt_record(uuid="spawn-prompt", text="Inspect the host.", timestamp=t0),
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "retry1": forwarder.SubagentEntry(
                subagent_id="retry1",
                child_conversation_id="conv_child_retry",
                tool_use_id="toolu_retry",
            )
        }
    )
    status_posts: list[dict[str, Any]] = []
    fail_items = True

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
            return httpx.Response(202, json={})
        if fail_items:
            return httpx.Response(503, text="unavailable")
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
            state=state,
            agent_name="claude-native-ui",
            **trackers,
        )
        assert status_posts == [{"status": "completed", "output": "completion"}]
        assert first.subagents["retry1"].terminal_status == "completed"
        fail_items = False
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=first,
            agent_name="claude-native-ui",
            **trackers,
        )

    # The retried prompt delivered without reopening: no running POST.
    assert status_posts == [{"status": "completed", "output": "completion"}]
    assert second.subagents["retry1"].terminal_status == "completed"
    assert second.subagents["retry1"].last_status == "completed"


async def test_subagent_same_poll_notification_and_resume_reopens(
    tmp_path: Path,
) -> None:
    """A resume already on disk with its completion still reopens by timestamp."""
    t0 = "2026-09-10T13:21:51.088Z"
    t1 = "2026-09-10T13:22:05.710Z"
    t2 = "2026-09-10T13:23:13.274Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            _task_notification_record(
                task_id="samepoll1",
                tool_use_id="toolu_spawn_a",
                status="completed",
                result="first completion",
                timestamp=t1,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="samepoll1",
        agent_type="Explore",
        description="resume raced the poll",
        tool_use_id="toolu_spawn_a",
        transcript_records=[
            _child_prompt_record(
                uuid="spawn-prompt", text="Inspect the first host.", timestamp=t0
            ),
            _child_resume_record(
                uuid="sendmessage-resume",
                text=(
                    "The coordinator sent a message while you were working:\n"
                    "Resume where you stopped."
                ),
                timestamp=t2,
            ),
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "samepoll1": forwarder.SubagentEntry(
                subagent_id="samepoll1",
                child_conversation_id="conv_child_samepoll",
                tool_use_id="toolu_spawn_a",
            )
        }
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
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
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert status_posts == [{"status": "running"}]
    assert result.subagents["samepoll1"].terminal_status is None
    assert result.subagents["samepoll1"].last_status == "running"


async def test_subagent_parked_row_loses_to_newer_live_notification(
    tmp_path: Path,
) -> None:
    """A live completion in the same poll wins over its parked predecessor."""
    t0 = "2026-09-10T13:21:51.088Z"
    t2 = "2026-09-10T13:26:05.710Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            _task_notification_record(
                task_id="parkwin1",
                tool_use_id="toolu_live",
                status="failed",
                result="live failure",
                timestamp=t2,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="parkwin1",
        agent_type="Explore",
        description="parked then notified",
        tool_use_id="toolu_live",
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "parkwin1": forwarder.SubagentEntry(
                subagent_id="parkwin1",
                child_conversation_id="conv_child_parkwin",
                tool_use_id="toolu_live",
            )
        },
        pending_terminal_notifications={
            "parkwin1": ("completed", "parked stale", False, t0),
        },
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
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
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert status_posts == [{"status": "failed", "output": "live failure"}]
    assert result.pending_terminal_notifications == {}
    assert result.subagents["parkwin1"].terminal_status == "failed"
    assert result.subagents["parkwin1"].terminal_output == "live failure"
    assert result.subagents["parkwin1"].terminal_observed_at == t2


async def test_subagent_task_id_parked_row_wins_over_legacy_tool_use_id_row(
    tmp_path: Path,
) -> None:
    """Both parked keys pop together; the task-id row settles the child."""
    t1 = "2026-09-10T13:22:05.710Z"
    t2 = "2026-09-10T13:23:13.274Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="parkboth1",
        agent_type="Explore",
        description="two parked rows",
        tool_use_id="toolu_legacy_spawn",
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "parkboth1": forwarder.SubagentEntry(
                subagent_id="parkboth1",
                child_conversation_id="conv_child_parkboth",
                tool_use_id="toolu_legacy_spawn",
            )
        },
        pending_terminal_notifications={
            "parkboth1": ("completed", "task row", False, t1),
            "toolu_legacy_spawn": ("failed", "legacy row", False, t2),
        },
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
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
            state=state,
            agent_name="claude-native-ui",
            **trackers,
        )
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=first,
            agent_name="claude-native-ui",
            **trackers,
        )

    assert status_posts == [{"status": "completed", "output": "task row"}]
    assert first.pending_terminal_notifications == {}
    assert first.subagents["parkboth1"].terminal_status == "completed"
    # The discarded legacy row never fires on a later poll.
    assert second.pending_terminal_notifications == {}
    assert second.subagents["parkboth1"].terminal_status == "completed"


async def test_subagent_reopened_running_post_retried_without_new_items(
    tmp_path: Path,
) -> None:
    """A 503 running POST after a reopen is retried on the next quiet poll."""
    t0 = "2026-09-10T13:21:51.088Z"
    t1 = "2026-09-10T13:22:05.710Z"
    t2 = "2026-09-10T13:23:13.274Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    child_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="retryrun1",
        agent_type="Explore",
        description="running retry",
        tool_use_id="toolu_retryrun",
        transcript_records=[
            _child_prompt_record(uuid="spawn-prompt", text="Inspect the host.", timestamp=t0),
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "retryrun1": forwarder.SubagentEntry(
                subagent_id="retryrun1",
                child_conversation_id="conv_child_retryrun",
                tool_use_id="toolu_retryrun",
                byte_offset=child_jsonl.stat().st_size,
                terminal_status="completed",
                terminal_output="first completion",
                terminal_replayed=False,
                terminal_observed_at=t1,
                last_status="completed",
            )
        }
    )
    status_posts: list[dict[str, Any]] = []
    fail_running = True

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            if fail_running:
                return httpx.Response(503, text="unavailable")
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    trackers = {
        "start_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        "item_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        "status_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
    }
    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler), base_url="http://ap"
    ) as client:
        with child_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _child_resume_record(
                        uuid="sendmessage-resume",
                        text=(
                            "The coordinator sent a message while you were working:\n"
                            "Resume where you stopped."
                        ),
                        timestamp=t2,
                    )
                )
                + "\n"
            )
        reopened = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            **trackers,
        )
        assert reopened.subagents["retryrun1"].terminal_status is None
        assert reopened.subagents["retryrun1"].last_status is None
        assert reopened.subagents["retryrun1"].status_reconcile_pending is True
        fail_running = False
        retried = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=reopened,
            agent_name="claude-native-ui",
            **trackers,
        )

    assert status_posts == [{"status": "running"}]
    assert retried.subagents["retryrun1"].last_status == "running"
    assert retried.subagents["retryrun1"].status_reconcile_pending is False


async def test_subagent_task_notification_prose_does_not_reopen_terminal(
    tmp_path: Path,
) -> None:
    """Tool-use-id-less notification prose is scaffolding, not a resume."""
    t1 = "2026-09-10T13:22:05.710Z"
    t2 = "2026-09-10T13:23:13.274Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    child_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="prose1",
        agent_type="Explore",
        description="nested prose",
        tool_use_id="toolu_prose",
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "prose1": forwarder.SubagentEntry(
                subagent_id="prose1",
                child_conversation_id="conv_child_prose",
                tool_use_id="toolu_prose",
                byte_offset=child_jsonl.stat().st_size,
                terminal_status="completed",
                terminal_output="first completion",
                terminal_replayed=False,
                terminal_observed_at=t1,
                last_status="completed",
            )
        }
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler), base_url="http://ap"
    ) as client:
        with child_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "isSidechain": True,
                        "type": "user",
                        "uuid": "nested-prose",
                        "timestamp": t2,
                        "message": {
                            "role": "user",
                            "content": (
                                "<task-notification>\n"
                                "<task-id>nested-task</task-id>\n"
                                "<status>completed</status>\n"
                                "<result>nested done</result>\n"
                                "</task-notification>"
                            ),
                        },
                    }
                )
                + "\n"
            )
        result = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert status_posts == []
    assert result.subagents["prose1"].terminal_status == "completed"
    assert result.subagents["prose1"].last_status == "completed"


async def test_subagent_plain_newer_user_prompt_reopens_terminal(
    tmp_path: Path,
) -> None:
    """A non-meta user prompt newer than the terminal reopens (non-coordinator path)."""
    t1 = "2026-09-10T13:22:05.710Z"
    t2 = "2026-09-10T13:23:13.274Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    child_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="plain1",
        agent_type="Explore",
        description="plain follow-up",
        tool_use_id="toolu_plain",
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "plain1": forwarder.SubagentEntry(
                subagent_id="plain1",
                child_conversation_id="conv_child_plain",
                tool_use_id="toolu_plain",
                byte_offset=child_jsonl.stat().st_size,
                terminal_status="completed",
                terminal_output="first completion",
                terminal_replayed=False,
                terminal_observed_at=t1,
                last_status="completed",
            )
        }
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler), base_url="http://ap"
    ) as client:
        with child_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _child_prompt_record(
                        uuid="follow-up",
                        text="Also check the second host.",
                        timestamp=t2,
                    )
                )
                + "\n"
            )
        result = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert status_posts == [{"status": "running"}]
    assert result.subagents["plain1"].terminal_status is None
    assert result.subagents["plain1"].last_status == "running"


def test_call_output_evidence_keeps_notification_timestamp() -> None:
    """A rebuilt result must not clobber a timestamped notification entry."""
    notification = ClaudeTaskNotification(
        task_id="ev1",
        tool_use_id="toolu_ev",
        status="completed",
        result="notified result",
        replayed=False,
        timestamp="2026-09-10T13:22:05.710Z",
    )
    result = TranscriptReadResult(
        byte_offset=0,
        line_cursor=0,
        current_response_id=None,
        items=[
            ClaudeTranscriptItem(
                source_id="call",
                item_type="function_call",
                data={"name": "Agent", "call_id": "toolu_ev"},
                response_id="resp",
            ),
            ClaudeTranscriptItem(
                source_id="output",
                item_type="function_call_output",
                data={
                    "call_id": "toolu_ev",
                    "output": "rebuilt result",
                    "tool_status": "completed",
                },
                response_id="resp",
            ),
        ],
        task_notifications=(notification,),
    )

    evidence = forwarder._structured_terminal_evidence(result)

    assert evidence["toolu_ev"] == (
        "completed",
        "notified result",
        "2026-09-10T13:22:05.710Z",
    )
    assert evidence["ev1"] == (
        "completed",
        "notified result",
        "2026-09-10T13:22:05.710Z",
    )


async def test_subagent_v2_recovery_keeps_notification_timestamp_for_resume(
    tmp_path: Path,
) -> None:
    """Recovery persists observed_at despite a same-key rebuilt result."""
    t1 = "2026-09-10T13:22:05.710Z"
    t2 = "2026-09-10T13:23:13.274Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "spawn-call",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_ev2",
                                    "name": "Agent",
                                    "input": {"description": "historical"},
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "spawn-result",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_ev2",
                                    "content": "historical result",
                                    "is_error": False,
                                }
                            ],
                        },
                        "toolUseResult": {
                            "status": "completed",
                            "isAsync": True,
                            "isError": False,
                        },
                    }
                ),
                json.dumps(
                    _task_notification_record(
                        task_id="ev2",
                        tool_use_id="toolu_ev2",
                        status="completed",
                        result="notified result",
                        timestamp=t1,
                    )
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="ev2",
        agent_type="Explore",
        description="evidence precedence",
        tool_use_id="toolu_ev2",
    )
    parent_size = transcript_path.stat().st_size
    parent_lines = transcript_path.read_text(encoding="utf-8").count("\n")
    state = forwarder.SubagentForwardState(
        subagents={
            "ev2": forwarder.SubagentEntry(
                subagent_id="ev2",
                child_conversation_id="conv_child_ev2",
                tool_use_id="toolu_ev2",
                last_status="running",
            )
        },
        parent_byte_offset=parent_size,
        parent_line_cursor=parent_lines,
        terminal_recovery_version=1,
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    trackers = {
        "start_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        "item_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        "status_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
    }
    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler), base_url="http://ap"
    ) as client:
        recovered = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            **trackers,
        )
        assert recovered.subagents["ev2"].terminal_status == "completed"
        assert recovered.subagents["ev2"].terminal_observed_at == t1
        child_jsonl = (
            transcript_path.parent / transcript_path.stem / "subagents" / "agent-ev2.jsonl"
        )
        with child_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _child_resume_record(
                        uuid="sendmessage-resume",
                        text=(
                            "The coordinator sent a message while you were working:\n"
                            "Resume where you stopped."
                        ),
                        timestamp=t2,
                    )
                )
                + "\n"
            )
        reopened = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=recovered,
            agent_name="claude-native-ui",
            **trackers,
        )

    assert status_posts[0] == {
        "status": "completed",
        "output": "notified result",
        "replayed": True,
    }
    assert status_posts[-1] == {"status": "running"}
    assert reopened.subagents["ev2"].terminal_status is None
    assert reopened.subagents["ev2"].last_status == "running"


async def test_subagent_resume_equal_instant_different_precision_does_not_reopen(
    tmp_path: Path,
) -> None:
    """The same instant in µs precision is not newer than its ms stamp."""
    t1 = "2026-09-10T13:26:05.710Z"
    t2 = "2026-09-10T13:26:05.710000Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    child_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="prec1",
        agent_type="Explore",
        description="precision tie",
        tool_use_id="toolu_prec",
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "prec1": forwarder.SubagentEntry(
                subagent_id="prec1",
                child_conversation_id="conv_child_prec",
                tool_use_id="toolu_prec",
                byte_offset=child_jsonl.stat().st_size,
                terminal_status="completed",
                terminal_output="first completion",
                terminal_replayed=False,
                terminal_observed_at=t1,
                last_status="completed",
            )
        }
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler), base_url="http://ap"
    ) as client:
        with child_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _child_resume_record(
                        uuid="sendmessage-resume",
                        text=(
                            "The coordinator sent a message while you were working:\n"
                            "Resume where you stopped."
                        ),
                        timestamp=t2,
                    )
                )
                + "\n"
            )
        result = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert status_posts == []
    assert result.subagents["prec1"].terminal_status == "completed"
    assert result.subagents["prec1"].last_status == "completed"


async def test_subagent_resume_submillisecond_precision_reopens(
    tmp_path: Path,
) -> None:
    """A strictly later instant reopens even without millisecond precision."""
    cases = [
        ("2026-09-10T13:26:05.710Z", "2026-09-10T13:26:05.710999Z"),
        ("2026-09-10T13:26:05Z", "2026-09-10T13:26:05.001Z"),
    ]
    for index, (observed_at, resume_at) in enumerate(cases):
        subagent_id = f"subms{index}"
        bridge_dir = tmp_path / f"bridge-{index}"
        transcript_path = tmp_path / f"session-{index}.jsonl"
        transcript_path.write_text("", encoding="utf-8")
        child_jsonl = _seed_subagent_on_disk(
            transcript_path=transcript_path,
            subagent_id=subagent_id,
            agent_type="Explore",
            description="sub-millisecond resume",
            tool_use_id="toolu_subms",
        )
        state = forwarder.SubagentForwardState(
            subagents={
                subagent_id: forwarder.SubagentEntry(
                    subagent_id=subagent_id,
                    child_conversation_id=f"conv_child_{subagent_id}",
                    tool_use_id="toolu_subms",
                    byte_offset=child_jsonl.stat().st_size,
                    terminal_status="completed",
                    terminal_output="first completion",
                    terminal_replayed=False,
                    terminal_observed_at=observed_at,
                    last_status="completed",
                )
            }
        )
        status_posts: list[dict[str, Any]] = []

        def handler(
            request: httpx.Request, _posts: list[dict[str, Any]] = status_posts
        ) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("type") == "external_session_status":
                _posts.append(body["data"])
            return httpx.Response(202, json={})

        async with httpx.AsyncClient(
            transport=_legacy_event_transport(handler), base_url="http://ap"
        ) as client:
            with child_jsonl.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        _child_resume_record(
                            uuid="sendmessage-resume",
                            text=(
                                "The coordinator sent a message while you were working:\n"
                                "Resume where you stopped."
                            ),
                            timestamp=resume_at,
                        )
                    )
                    + "\n"
                )
            result = await forwarder._forward_available_subagents(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                agent_name="claude-native-ui",
                start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            )

        assert status_posts == [{"status": "running"}]
        assert result.subagents[subagent_id].terminal_status is None
        assert result.subagents[subagent_id].last_status == "running"


async def test_subagent_garbage_timestamp_never_reorders(
    tmp_path: Path,
) -> None:
    """Unparsable timestamps are unknown: no reopen, no parked override."""
    t1 = "2026-09-10T13:22:05.710Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    child_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="garb1",
        agent_type="Explore",
        description="garbage timestamps",
        tool_use_id="toolu_garb",
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "garb1": forwarder.SubagentEntry(
                subagent_id="garb1",
                child_conversation_id="conv_child_garb",
                tool_use_id="toolu_garb",
                byte_offset=child_jsonl.stat().st_size,
                terminal_status="completed",
                terminal_output="settled",
                terminal_replayed=False,
                terminal_observed_at=t1,
                last_status="completed",
            )
        },
        pending_terminal_notifications={
            "garb1": ("failed", "garbage override", False, "not-a-timestamp"),
        },
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler), base_url="http://ap"
    ) as client:
        with child_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _child_resume_record(
                        uuid="sendmessage-resume",
                        text=(
                            "The coordinator sent a message while you were working:\n"
                            "Resume where you stopped."
                        ),
                        timestamp="also-not-a-timestamp",
                    )
                )
                + "\n"
            )
        result = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert status_posts == []
    assert result.pending_terminal_notifications == {}
    assert result.subagents["garb1"].terminal_status == "completed"
    assert result.subagents["garb1"].terminal_output == "settled"
    assert result.subagents["garb1"].last_status == "completed"


def test_parse_record_timestamp_precision_shapes() -> None:
    """The timestamp parser normalises every stamp shape Claude emits."""
    from datetime import datetime, timezone

    parse = forwarder._parse_record_timestamp
    assert parse(None) is None
    assert parse("") is None
    assert parse("not-a-timestamp") is None
    assert parse("2026-09-10T13:26:05.710Z") == datetime(
        2026, 9, 10, 13, 26, 5, 710000, tzinfo=timezone.utc
    )
    assert parse("2026-09-10T13:26:05.710000Z") == parse("2026-09-10T13:26:05.710Z")
    assert parse("2026-09-10T13:26:05Z") == datetime(2026, 9, 10, 13, 26, 5, tzinfo=timezone.utc)
    assert parse("2026-09-10T13:26:05.710+00:00") == parse("2026-09-10T13:26:05.710Z")
    assert forwarder._record_timestamp_is_newer(
        "2026-09-10T13:26:05.710999Z", "2026-09-10T13:26:05.710Z"
    )
    assert not forwarder._record_timestamp_is_newer(
        "2026-09-10T13:26:05.710000Z", "2026-09-10T13:26:05.710Z"
    )
    assert not forwarder._record_timestamp_is_newer("garbage", "2026-09-10T13:26:05.710Z")
    assert not forwarder._record_timestamp_is_newer("2026-09-10T13:26:05.710Z", None)


async def test_subagent_trailing_assistant_item_does_not_reopen_terminal(
    tmp_path: Path,
) -> None:
    """Assistant output racing the notification is history, not a resume."""
    t1 = "2026-09-10T13:22:05.710Z"
    t2 = "2026-09-10T13:23:13.274Z"
    t3 = "2026-09-10T13:23:14.001Z"
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            _task_notification_record(
                task_id="quiet1",
                tool_use_id="toolu_quiet",
                status="completed",
                result="quiet completion",
                timestamp=t1,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    child_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="quiet1",
        agent_type="Explore",
        description="quiet child",
        tool_use_id="toolu_quiet",
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "quiet1": forwarder.SubagentEntry(
                subagent_id="quiet1",
                child_conversation_id="conv_child_quiet",
                tool_use_id="toolu_quiet",
            )
        }
    )
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler),
        base_url="http://ap",
    ) as client:
        trackers = {
            "start_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
            "item_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
            "status_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        }
        settled = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            **trackers,
        )
        assert status_posts == [{"status": "completed", "output": "quiet completion"}]

        with child_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _child_assistant_record(
                        uuid="assistant-trailing",
                        text="Trailing output.",
                        timestamp=t2,
                    )
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "isSidechain": True,
                        "type": "user",
                        "uuid": "child-tool-result",
                        "parentUuid": "assistant-trailing",
                        "timestamp": t3,
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_child_read",
                                    "content": "spec contents",
                                    "is_error": False,
                                }
                            ],
                        },
                    }
                )
                + "\n"
            )
        after = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=settled,
            agent_name="claude-native-ui",
            **trackers,
        )

    assert status_posts == [{"status": "completed", "output": "quiet completion"}]
    assert after.subagents["quiet1"].terminal_status == "completed"
    assert after.subagents["quiet1"].last_status == "completed"


async def test_subagent_watcher_never_completes_from_tool_result_silence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trailing async receipt plus silence produces at most a badge, never a terminal status."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="async1",
        agent_type="Explore",
        description="long async task",
        tool_use_id="toolu_async_parent",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "assistant-opener-async",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Starting the check."}],
                },
            },
            {
                "isSidechain": True,
                "type": "user",
                "uuid": "async-tool-result",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_async_child",
                            "content": "Task is running in the background",
                            "is_error": False,
                        }
                    ],
                },
                "toolUseResult": {"status": "running", "isAsync": True},
            },
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "async1": forwarder.SubagentEntry(
                subagent_id="async1",
                child_conversation_id="conv_child_async",
                tool_use_id="toolu_async_parent",
            )
        }
    )
    now = [100.0]
    monkeypatch.setattr(forwarder.time, "time", lambda: now[0])
    statuses: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            statuses.append(body["data"]["status"])
        return httpx.Response(202, json={})

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
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        now[0] += forwarder._SUBAGENT_TERMINAL_QUIESCENCE_S + 1
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=first,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    posted_statuses = set(statuses)
    assert posted_statuses <= {"running"}
    assert not posted_statuses & {"idle", "completed", "failed", "stopped", "killed"}
    assert second.subagents["async1"].quiet_terminal_output is None


async def test_subagent_watcher_does_not_complete_from_assistant_text_silence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assistant text plus silence produces at most a badge, never a terminal status."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="legacy1",
        agent_type="Explore",
        description="legacy task",
        tool_use_id="toolu_legacy",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "assistant-final-legacy",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Final legacy result."}],
                },
            }
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "legacy1": forwarder.SubagentEntry(
                subagent_id="legacy1",
                child_conversation_id="conv_child_legacy",
                tool_use_id="toolu_legacy",
            )
        }
    )
    now = [200.0]
    monkeypatch.setattr(forwarder.time, "time", lambda: now[0])
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") == "external_session_status":
            status_posts.append(body["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_legacy_event_transport(handler),
        base_url="http://ap",
    ) as client:
        trackers = {
            "start_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
            "item_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
            "status_retry_tracker": forwarder._PostRetryTracker(base_delay_s=0.0),
        }
        first = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            **trackers,
        )
        now[0] += forwarder._SUBAGENT_TERMINAL_QUIESCENCE_S + 1
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=first,
            agent_name="claude-native-ui",
            **trackers,
        )

    posted_statuses = {entry["status"] for entry in status_posts}
    assert posted_statuses <= {"running"}
    assert not posted_statuses & {"idle", "completed", "failed", "stopped", "killed"}
    assert second.subagents["legacy1"].last_status in {"running", "idle"}


async def test_subagent_terminal_notification_retries_after_state_reload(
    tmp_path: Path,
) -> None:
    """A failed terminal POST is retried from durable correlated state."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            _task_notification_record(
                tool_use_id="toolu_restart",
                status="completed",
                result="Recovered result.",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="restart1",
        agent_type="Explore",
        description="retry after restart",
        tool_use_id="toolu_restart",
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "restart1": forwarder.SubagentEntry(
                subagent_id="restart1",
                child_conversation_id="conv_child_restart",
                tool_use_id="toolu_restart",
            )
        }
    )
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") != "external_session_status":
            return httpx.Response(202, json={})
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "runner reconnecting"})
        return httpx.Response(202, json={})

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
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
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

    assert attempts == 2
    assert first.subagents["restart1"].terminal_status == "completed"
    assert first.subagents["restart1"].last_status is None
    assert second.subagents["restart1"].last_status == "completed"


@pytest.mark.asyncio
async def test_transient_subagent_502_stays_pending_past_batch_budget(
    tmp_path: Path,
) -> None:
    """A child item stays pending past twelve attempts and recovers."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_id = "recoverable-502"
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id=subagent_id,
        agent_type="Explore",
        description="temporary outage",
        tool_use_id="toolu_recoverable_502",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "recoverable-message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "recoverable output"}],
                },
            }
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            subagent_id: forwarder.SubagentEntry(
                subagent_id=subagent_id,
                child_conversation_id="conv_recoverable_502",
            )
        }
    )
    outage = True
    batch_attempts = 0
    item_attempts = 0
    delivered: list[str] = []
    statuses: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal batch_attempts, item_attempts
        body = json.loads(request.content.decode("utf-8"))
        if isinstance(body, list):
            batch_attempts += 1
            if outage:
                return httpx.Response(502, text="bad gateway")
            delivered.extend(row["data"]["source_id"] for row in body)
            return httpx.Response(
                202,
                json=[{"queued": False, "item_id": row["data"]["source_id"]} for row in body],
            )
        if body.get("type") == "external_conversation_item":
            item_attempts += 1
            if outage:
                return httpx.Response(502, text="bad gateway")
            delivered.append(body["data"]["source_id"])
            return httpx.Response(202, json={"queued": False, "item_id": "recovered"})
        if body.get("type") in {"external_session_status", "subagent.status"}:
            statuses.append(body["data"])
        return httpx.Response(202, json={})

    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for _ in range(24):
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
        assert batch_attempts == forwarder._SUBAGENT_BATCH_MAX_TRANSIENT_ATTEMPTS
        assert item_attempts == 13
        assert delivered == []
        entry = state.subagents[subagent_id]
        assert entry.byte_offset == 0
        assert entry.seen_source_ids == ()
        assert entry.delivery_error is None
        assert not (bridge_dir / "dead_letter.jsonl").exists()
        state = forwarder._read_subagent_forward_state(bridge_dir)
        tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
        outage = False
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

    assert delivered == ["recoverable-message:0:message"]
    entry = state.subagents[subagent_id]
    assert entry.seen_source_ids == ("recoverable-message:0:message",)
    assert entry.delivery_error is None
    assert all(status.get("status") != "failed" for status in statuses)


@pytest.mark.asyncio
async def test_failed_child_observation_does_not_refresh_stale_running_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed child reads propagate after sibling checkpoints and let liveness expire."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    state = forwarder.SubagentForwardState(
        subagents={
            "unreadable": forwarder.SubagentEntry(
                subagent_id="unreadable",
                child_conversation_id="conv_unreadable",
                last_status="running",
            ),
            "sibling": forwarder.SubagentEntry(
                subagent_id="sibling",
                child_conversation_id="conv_sibling",
                last_status="running",
            ),
        }
    )
    forwarder._write_subagent_forward_state(bridge_dir, state)

    async def observe_child(**kwargs: Any) -> None:
        entry = kwargs["entry"]
        if entry.subagent_id == "unreadable":
            raise OSError("child transcript is unreadable")
        await kwargs["checkpoint"].put(replace(entry, seen_source_ids=("sibling-observed",)))

    monkeypatch.setattr(forwarder, "_forward_one_subagent", observe_child)
    snapshot_posts: list[dict[str, Any]] = []
    first_snapshot = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        snapshot_posts.append(json.loads(request.content.decode("utf-8"))["data"])
        first_snapshot.set()
        return httpx.Response(202)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        async with forwarder.NativeSubagentSnapshotPublisher(
            client,
            heartbeat_s=0.005,
            observation_timeout_s=35.0,
        ) as publisher:
            publisher.update("conv_parent", forwarder._native_subagent_snapshot(state))
            await asyncio.wait_for(first_snapshot.wait(), timeout=1.0)
            publisher._inventories["conv_parent"].changed_at -= 36.0

            for _ in range(3):
                try:
                    observed = await forwarder._forward_available_subagents(
                        client=client,
                        parent_session_id="conv_parent",
                        bridge_dir=bridge_dir,
                        transcript_path=transcript_path,
                        state=state,
                        agent_name="claude-native-ui",
                        start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                        item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                        status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                    )
                except OSError:
                    state = forwarder._read_subagent_forward_state(bridge_dir)
                else:
                    state = observed
                    publisher.update("conv_parent", forwarder._native_subagent_snapshot(state))
                await asyncio.sleep(0.01)

    assert len(snapshot_posts) == 1
    assert state.subagents["sibling"].seen_source_ids == ("sibling-observed",)
