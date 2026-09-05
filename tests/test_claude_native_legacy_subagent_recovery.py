"""Cold recovery for legacy Claude-native sub-agent status snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent import claude_native_forwarder as forwarder


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")


def _legacy_fixture(tmp_path: Path, *, terminal_status: str = "completed") -> tuple[Path, Path]:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "parent.jsonl"
    _write_jsonl(
        transcript_path,
        [
            {
                "type": "assistant",
                "uuid": "call",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_legacy",
                            "name": "Agent",
                            "input": {"prompt": "historical"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "uuid": "result",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_legacy",
                            "content": "historical result",
                            "is_error": terminal_status == "failed",
                        }
                    ],
                },
                "toolUseResult": {
                    "status": terminal_status,
                    "isAsync": True,
                    "isError": terminal_status == "failed",
                },
            },
        ],
    )
    child_dir = transcript_path.parent / transcript_path.stem / "subagents"
    _write_jsonl(child_dir / "agent-old.jsonl", [])
    (child_dir / "agent-old.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "old", "toolUseId": "toolu_new"}),
        encoding="utf-8",
    )
    (bridge_dir / "subagent_forwarder.json").write_text(
        json.dumps(
            {
                "subagents": {
                    "old": {
                        "child_conversation_id": "conv_child",
                        "tool_use_id": "toolu_legacy",
                        "byte_offset": 0,
                        "last_status": "running",
                    }
                },
                "parent_byte_offset": transcript_path.stat().st_size,
                "parent_line_cursor": 2,
            }
        ),
        encoding="utf-8",
    )
    return bridge_dir, transcript_path


async def _run_once(
    bridge_dir: Path,
    transcript_path: Path,
    handler: httpx.AsyncBaseTransport,
) -> forwarder.SubagentForwardState:
    state = forwarder._read_subagent_forward_state(bridge_dir)
    async with httpx.AsyncClient(transport=handler, base_url="http://server") as client:
        return await forwarder._forward_available_subagents(
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


@pytest.mark.asyncio
async def test_legacy_eof_running_replays_only_correlated_terminal_status(tmp_path: Path) -> None:
    bridge_dir, transcript_path = _legacy_fixture(tmp_path)
    posted: list[dict[str, Any]] = []

    async def record(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={})

    result = await _run_once(bridge_dir, transcript_path, httpx.MockTransport(record))

    assert posted == [
        {
            "type": "external_session_status",
            "data": {
                "status": "completed",
                "output": "historical result",
                "replayed": True,
            },
        }
    ]
    assert result.parent_byte_offset == transcript_path.stat().st_size
    assert result.parent_line_cursor == 2
    entry = result.subagents["old"]
    assert entry.last_status == "completed"
    assert entry.status_reconcile_pending is False
    assert entry.tool_use_id == "toolu_legacy"  # current meta belongs to a newer dispatch


@pytest.mark.asyncio
async def test_legacy_terminal_post_failure_stays_pending_for_retry(tmp_path: Path) -> None:
    bridge_dir, transcript_path = _legacy_fixture(tmp_path, terminal_status="failed")

    async def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    result = await _run_once(bridge_dir, transcript_path, httpx.MockTransport(unavailable))
    entry = result.subagents["old"]
    assert entry.terminal_status == "failed"
    assert entry.terminal_replayed is True
    assert entry.last_status == "running"
    assert entry.status_reconcile_pending is True
    persisted = forwarder._read_subagent_forward_state(bridge_dir).subagents["old"]
    assert persisted.status_reconcile_pending is True


@pytest.mark.asyncio
async def test_legacy_running_without_terminal_evidence_becomes_unverified(tmp_path: Path) -> None:
    bridge_dir, transcript_path = _legacy_fixture(tmp_path)
    transcript_path.write_text("", encoding="utf-8")
    posted: list[dict[str, Any]] = []

    async def record(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={})

    result = await _run_once(bridge_dir, transcript_path, httpx.MockTransport(record))

    assert posted == [
        {
            "type": "external_session_status",
            "data": {"status": "activity_unverified", "replayed": True},
        }
    ]
    entry = result.subagents["old"]
    assert entry.activity_unverified is True
    assert entry.last_status == "activity_unverified"
    assert entry.status_reconcile_pending is False


@pytest.mark.asyncio
async def test_legacy_meta_correlation_is_recovered_before_terminal_scan(tmp_path: Path) -> None:
    bridge_dir, transcript_path = _legacy_fixture(tmp_path)
    state_path = bridge_dir / "subagent_forwarder.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    raw["subagents"]["old"]["tool_use_id"] = None
    state_path.write_text(json.dumps(raw), encoding="utf-8")
    meta_path = transcript_path.parent / transcript_path.stem / "subagents" / "agent-old.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["toolUseId"] = "toolu_legacy"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    posted: list[dict[str, Any]] = []

    async def record(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={})

    result = await _run_once(bridge_dir, transcript_path, httpx.MockTransport(record))
    assert result.subagents["old"].tool_use_id == "toolu_legacy"
    assert posted[0]["data"]["status"] == "completed"


@pytest.mark.asyncio
async def test_legacy_terminal_reconcile_does_not_require_child_jsonl(tmp_path: Path) -> None:
    bridge_dir, transcript_path = _legacy_fixture(tmp_path)
    (transcript_path.parent / transcript_path.stem / "subagents" / "agent-old.jsonl").unlink()
    posted: list[dict[str, Any]] = []

    async def record(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={})

    result = await _run_once(bridge_dir, transcript_path, httpx.MockTransport(record))
    assert posted[0]["data"]["status"] == "completed"
    assert result.subagents["old"].status_reconcile_pending is False


@pytest.mark.asyncio
async def test_legacy_terminal_reconcile_does_not_require_subagents_directory(
    tmp_path: Path,
) -> None:
    bridge_dir, transcript_path = _legacy_fixture(tmp_path)
    child_dir = transcript_path.parent / transcript_path.stem / "subagents"
    for path in child_dir.iterdir():
        path.unlink()
    child_dir.rmdir()
    posted: list[dict[str, Any]] = []

    async def record(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={})

    result = await _run_once(bridge_dir, transcript_path, httpx.MockTransport(record))
    assert posted[0]["data"]["status"] == "completed"
    assert result.subagents["old"].status_reconcile_pending is False


@pytest.mark.asyncio
async def test_missing_parent_transcript_does_not_complete_one_time_scan(tmp_path: Path) -> None:
    bridge_dir, transcript_path = _legacy_fixture(tmp_path)
    transcript_path.unlink()
    state = forwarder._read_subagent_forward_state(bridge_dir)
    result = await forwarder._prepare_legacy_subagent_terminal_recovery(
        bridge_dir=bridge_dir,
        transcript_path=transcript_path,
        state=state,
        agent_name="claude-native-ui",
    )
    assert result.terminal_recovery_version == 0
    assert result.subagents["old"].status_reconcile_pending is False


@pytest.mark.asyncio
async def test_partial_parent_record_retries_after_it_becomes_complete(tmp_path: Path) -> None:
    bridge_dir, transcript_path = _legacy_fixture(tmp_path)
    complete = transcript_path.read_text(encoding="utf-8")
    transcript_path.write_text(complete.rstrip("\n"), encoding="utf-8")
    state = forwarder._read_subagent_forward_state(bridge_dir)
    deferred = await forwarder._prepare_legacy_subagent_terminal_recovery(
        bridge_dir=bridge_dir,
        transcript_path=transcript_path,
        state=state,
        agent_name="claude-native-ui",
    )
    assert deferred.terminal_recovery_version == 0
    assert deferred.legacy_terminal_recovery_watermark is None

    transcript_path.write_text(complete, encoding="utf-8")
    posted: list[dict[str, Any]] = []

    async def record(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={})

    recovered = await _run_once(bridge_dir, transcript_path, httpx.MockTransport(record))
    assert recovered.terminal_recovery_version == 1
    assert posted[0]["data"]["status"] == "completed"


def test_terminal_evidence_rejects_read_and_async_launched() -> None:
    items = [
        forwarder.ClaudeTranscriptItem(
            source_id="call-read",
            item_type="function_call",
            data={"name": "Read", "call_id": "read"},
            response_id=None,
        ),
        forwarder.ClaudeTranscriptItem(
            source_id="read-result",
            item_type="function_call_output",
            data={"call_id": "read", "tool_status": "completed", "output": "contents"},
            response_id=None,
        ),
        forwarder.ClaudeTranscriptItem(
            source_id="call-agent",
            item_type="function_call",
            data={"name": "Agent", "call_id": "agent"},
            response_id=None,
        ),
        forwarder.ClaudeTranscriptItem(
            source_id="agent-result",
            item_type="function_call_output",
            data={"call_id": "agent", "tool_status": "async_launched", "output": "started"},
            response_id=None,
        ),
    ]
    result = forwarder.TranscriptReadResult(
        byte_offset=0,
        line_cursor=0,
        current_response_id=None,
        items=items,
    )
    assert forwarder._structured_terminal_evidence(result) == {}
