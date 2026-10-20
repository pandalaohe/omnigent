"""Physical identities and checkpointed metering without transcript retention."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from omnigent.usage_export.outbox import UsageOutbox
from omnigent.usage_export.transcripts import (
    claude_measurement,
    claude_physical_identity,
    codex_measurement,
)


def _codex(thread: str, response: str, input_tokens: int) -> dict:
    return {
        "type": "token_usage_record",
        "timestamp": "2026-10-20T00:00:00Z",
        "payload": {
            "thread_id": thread,
            "response_id": response,
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 2,
                "reasoning_output_tokens": 1,
            },
            "thread_token_usage": {"input_tokens": 9999},
        },
    }


def _rows(spool: UsageOutbox) -> list[dict]:
    with sqlite3.connect(spool.path) as db:
        return [
            json.loads(row[0])
            for row in db.execute("SELECT payload FROM observations ORDER BY sequence")
        ]


def test_clone_ignores_inherited_calls_and_commits_checkpoint_once(tmp_path: Path) -> None:
    spool = UsageOutbox(tmp_path / "spool")
    physical = {
        "harness_family": "codex",
        "identity_namespace": "codex/rollout/v1",
        "thread_kind": "main",
    }
    parent = tmp_path / "parent.jsonl"
    parent.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "parent"}})
        + "\n"
        + json.dumps(_codex("parent", "parent-call", 100))
        + "\n"
    )
    clone = tmp_path / "clone.jsonl"
    clone.write_text(
        parent.read_text().replace('"id": "parent"', '"id": "clone"')
        + json.dumps(_codex("clone", "clone-call", 20))
        + "\n"
    )
    for source in [parent, clone]:
        spool.register_source(source, "session", "codex-native", physical)
        spool.scan(source)
        UsageOutbox(tmp_path / "spool").scan(source)
    rows = _rows(spool)
    assert [row["input_tokens"] for row in rows] == [100, 20]
    assert [row["physical_session_id"] for row in rows] == ["parent", "clone"]


def test_claude_parent_child_identity_and_usage_fragments(tmp_path: Path) -> None:
    child = claude_physical_identity(Path("/opt/work/projects/parent/subagents/agent-child.jsonl"))
    assert child["physical_session_id"] == "parent"
    assert child["physical_subagent_id"] == "child"
    record = {
        "type": "assistant",
        "sessionId": "observed-child",
        "requestId": "request-1",
        "timestamp": "2026-10-20T00:00:00Z",
        "message": {
            "id": "message-1",
            "model": "claude",
            "content": "do-not-store-transcript-content",
            "usage": {
                "input_tokens": 3,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 1,
                "output_tokens": 4,
            },
        },
    }
    spool = UsageOutbox(tmp_path / "spool")
    spool.append("session", "claude-sdk", child, record)
    spool.append("session", "claude-sdk", child, record)
    record["message"]["usage"]["output_tokens"] = 5
    spool.append("session", "claude-sdk", child, record)
    rows = _rows(spool)
    assert [row["output_tokens"] for row in rows] == [4, 5]
    assert rows[0]["transcript_session_id"] == "observed-child"
    assert rows[0]["input_tokens"] == 3
    assert rows[0]["cached_input_tokens"] == 2
    assert rows[0]["cache_write_input_tokens"] == 1
    assert "do-not-store-transcript-content" not in str(rows)


def test_final_scan_drains_more_than_live_batch_before_home_cleanup(tmp_path: Path) -> None:
    spool = UsageOutbox(tmp_path / "spool")
    source = tmp_path / "large.jsonl"
    source.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "main"}})
        + "\n"
        + "\n".join(json.dumps(_codex("main", f"r-{i}", 1)) for i in range(1002))
        + "\n"
    )
    spool.register_source(source, "session", "codex-sdk", {"thread_kind": "main"})
    spool.scan(source, final=True)
    source.unlink()
    assert len(_rows(spool)) == 1002


def test_partial_line_and_corrupt_record_never_advance_past_usage(tmp_path: Path) -> None:
    spool = UsageOutbox(tmp_path / "spool")
    path = tmp_path / "partial.jsonl"
    path.write_text(json.dumps(_codex("main", "r", 1)))
    spool.register_source(path, "session", "codex-native", {"physical_session_id": "main"})
    spool.scan(path)
    assert _rows(spool) == []
    with path.open("a") as file:
        file.write("\n{bad\n")
    with pytest.raises(ValueError):
        spool.scan(path)
    assert _rows(spool) == []


def test_unknown_legacy_identity_and_cache_values_are_not_invented() -> None:
    assert (
        codex_measurement(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 100}},
                },
            }
        )
        is None
    )
    assert (
        claude_measurement({"type": "assistant", "message": {"usage": {"input_tokens": 1}}})
        is None
    )
    record = _codex("main", "r", 1)
    record["payload"]["usage"]["cached_input_tokens"] = True
    measurement = codex_measurement(record)
    assert measurement is not None
    assert measurement["input_tokens"] is None
    assert measurement["cached_input_tokens"] is None
    assert measurement["usage_status"] == "partial"


def test_capture_is_off_and_native_resume_has_an_explicit_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.runtime import capture_codex_home

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("OMNIGENT_USAGE_EXPORT_CAPTURE", raising=False)
    home = tmp_path / "home"
    capture_codex_home(home, "session", "codex-native")
    assert not (tmp_path / "runtime").exists()
    path = home / "sessions" / "rollout-main.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "main"}})
        + "\n"
        + json.dumps(_codex("main", "old", 100))
        + "\n"
    )
    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1")
    capture_codex_home(home, "session", "codex-native", "main", baseline_existing=True)
    with path.open("a") as file:
        file.write(json.dumps(_codex("main", "new", 20)) + "\n")
    capture_codex_home(home, "session", "codex-native", "main")
    spool = UsageOutbox(tmp_path / "runtime" / "usage-export")
    assert [row["input_tokens"] for row in _rows(spool)] == [20]


@pytest.mark.asyncio
async def test_sdk_mirror_is_persisted_before_peer_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from omnigent.inner.claude_sdk_executor import _ClaudePeerReceiptStore

    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "runtime"))
    owner = SimpleNamespace(
        _query_slots={}, _observe_peer_mirror=AsyncMock(side_effect=RuntimeError("routing failed"))
    )
    store = _ClaudePeerReceiptStore(owner, "session")
    with pytest.raises(RuntimeError, match="routing failed"):
        await store.append(
            {"session_id": "parent", "project_key": "project", "subpath": "subagents/agent-child"},
            [
                {
                    "type": "assistant",
                    "sessionId": "observed-child",
                    "message": {
                        "id": "response-1",
                        "usage": {"input_tokens": 2, "output_tokens": 3},
                    },
                }
            ],
        )
    rows = _rows(UsageOutbox(tmp_path / "runtime" / "usage-export"))
    assert len(rows) == 1
    assert rows[0]["physical_session_id"] == "parent"
    assert rows[0]["physical_subagent_id"] == "child"
    assert rows[0]["physical_transcript_path"] is None


def test_sdk_cleanup_persists_usage_before_removing_its_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner.codex_executor import _CodexAppServerSession

    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "runtime"))
    home = tmp_path / "codex-home"
    source = home / "sessions" / "rollout-main.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "main"}})
        + "\n"
        + json.dumps(_codex("main", "r", 5))
        + "\n"
    )
    session = _CodexAppServerSession(
        codex_path="/opt/bin/codex", cwd=None, env={}, tool_executor=None
    )
    session._codex_home_dir = home
    stat = home.stat()
    session._codex_home_identity = (stat.st_dev, stat.st_ino)
    session._usage_session_id = "session"
    session._cleanup_process_cwd()
    assert not home.exists()
    assert [
        row["input_tokens"] for row in _rows(UsageOutbox(tmp_path / "runtime" / "usage-export"))
    ] == [5]


@pytest.mark.asyncio
async def test_claude_capture_uses_frozen_fork_and_cold_resume_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.claude_native.forwarder import (
        _ensure_state_for_transcript,
        _seed_fork_transcript_forward_state,
    )
    from omnigent.harnesses.claude_native.main import (
        ClaudeResumeTranscriptResolution,
        _prepare_cold_resume_forward_state,
    )
    from omnigent.usage_export.runtime import (
        baseline_claude_transcripts,
        capture_claude_transcript,
    )

    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "runtime"))
    old = {"type": "assistant", "message": {"id": "old", "usage": {"input_tokens": 100}}}
    new = {"type": "assistant", "message": {"id": "new", "usage": {"input_tokens": 20}}}
    for fork in (True, False):
        path = tmp_path / f"session-{fork}.jsonl"
        path.write_text(json.dumps(old) + "\n")
        prefix = path.stat().st_size
        child = path.parent / path.stem / "subagents" / "agent-old-child.jsonl"
        child.parent.mkdir(parents=True)
        child.write_text(json.dumps(old) + "\n")
        bridge = tmp_path / f"bridge-{fork}"
        bridge.mkdir()
        if fork:
            await _seed_fork_transcript_forward_state(
                bridge_dir=bridge, transcript_path=path, session_id=f"session-{fork}"
            )
        else:
            _prepare_cold_resume_forward_state(
                bridge_dir=bridge,
                session_id=f"session-{fork}",
                resolution=ClaudeResumeTranscriptResolution(
                    path, reused_local=False, synthesized=True
                ),
            )
        with path.open("a") as file:
            file.write(json.dumps(new) + "\n")
        with child.open("a") as file:
            file.write(json.dumps(new) + "\n")
        fresh = child.with_name("agent-new-child.jsonl")
        fresh.write_text(json.dumps(new) + "\n")
        baseline_claude_transcripts(path, f"session-{fork}")
        state = await _ensure_state_for_transcript(
            bridge_dir=bridge,
            state=None,
            transcript_path=path,
            start_at_end=True,
            start_at_offset=None if fork else prefix,
            session_id=f"session-{fork}",
        )
        capture_claude_transcript(path, f"session-{fork}", start_offset=state.byte_offset)
        capture_claude_transcript(path, f"session-{fork}", start_offset=path.stat().st_size)
    rows = _rows(UsageOutbox(tmp_path / "runtime" / "usage-export"))
    assert [row["input_tokens"] for row in rows] == [20] * 6
    assert sum(row["physical_subagent_id"] is not None for row in rows) == 4


def test_inherited_claude_children_need_a_frozen_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.runtime import capture_claude_transcript

    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "runtime"))
    path = tmp_path / "parent.jsonl"
    path.write_text("{}\n")
    with pytest.raises(ValueError, match="usage_inherited_baseline_missing"):
        capture_claude_transcript(path, "session", start_offset=3)
    assert _rows(UsageOutbox(tmp_path / "runtime" / "usage-export")) == []


def test_claude_compaction_and_tool_results_keep_the_enclosing_turn(tmp_path: Path) -> None:
    spool = UsageOutbox(tmp_path / "spool")
    physical = {"physical_session_id": "parent", "physical_subagent_id": None}
    records = [
        {"type": "user", "message": {"content": "go"}},
        {"type": "assistant", "message": {"id": "before", "usage": {"input_tokens": 2}}},
        {"type": "user", "isCompactSummary": True, "message": {"content": "summary"}},
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "assistant", "message": {"id": "after", "usage": {"input_tokens": 3}}},
        {"type": "system", "subtype": "turn_duration", "timestamp": "2026-10-20T00:00:05Z"},
    ]
    for record in records:
        UsageOutbox(tmp_path / "spool").append("session", "claude-sdk", physical, record)
    rows = _rows(spool)
    assert len(rows) == 4
    assert [row.get("ended_at") for row in rows] == [
        None,
        None,
        "2026-10-20T00:00:05Z",
        "2026-10-20T00:00:05Z",
    ]
    assert {row["logical_call_id"] for row in rows[2:]} == {"after", "before"}


@pytest.mark.parametrize("harness", ["codex-native", "claude-sdk"])
def test_terminal_receipt_revises_only_its_physical_turn_and_survives_restart(
    tmp_path: Path, harness: str
) -> None:
    spool = UsageOutbox(tmp_path / "spool")
    physical = {"physical_session_id": "parent", "physical_subagent_id": None}
    child = {**physical, "physical_subagent_id": "child"}
    codex = harness.startswith("codex")
    first = (
        _codex("parent", "first", 7)
        if codex
        else {"type": "assistant", "message": {"id": "first", "usage": {"input_tokens": 7}}}
    )
    if codex:
        first["payload"]["turn_id"] = "turn-1"
    spool.append("session", harness, physical, first)
    spool.append("session", harness, child, first)
    terminal = {
        "type": "event_msg" if codex else "system",
        "subtype": "turn_duration",
        "timestamp": "2026-10-20T00:00:05Z",
        "payload": {"type": "task_complete", "turn_id": "turn-1"},
    }
    spool = UsageOutbox(tmp_path / "spool")
    spool.append("session", harness, physical, terminal)
    spool.append("session", harness, physical, terminal)
    rows = _rows(spool)
    assert len(rows) == 3
    assert rows[-1]["producer_unit_id"] == rows[0]["producer_unit_id"]
    assert rows[-1]["ended_at"] == "2026-10-20T00:00:05Z"
    assert rows[1].get("ended_at") is None
    if not codex:
        spool.append(
            "session", harness, physical, {"type": "user", "message": {"content": "next"}}
        )
        first["message"]["id"] = "second"
    else:
        first["payload"].update(response_id="second", turn_id="turn-2")
    spool.append("session", harness, physical, first)
    assert _rows(spool)[-1].get("ended_at") is None


def test_retry_has_bounded_full_jitter_and_honors_server_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.usage_export.runtime import retry_delay

    monkeypatch.setattr("omnigent.usage_export.runtime.random.uniform", lambda low, high: high)
    assert [retry_delay(i) for i in range(9)] == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert retry_delay(0, "3600") == 3600
    for invalid in ("0", "3601", "999999999", "-1", "n/a"):
        assert retry_delay(0, invalid) == 5
    monkeypatch.setattr("omnigent.usage_export.runtime.random.uniform", lambda low, high: low)
    assert retry_delay(99) == 1
    assert retry_delay(99, "25") == 25
