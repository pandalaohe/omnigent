"""Physical identities and checkpointed metering without transcript retention."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
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


def _terminal_evidence(physical_session_id: str, physical_subagent_id: str | None) -> dict:
    return {
        "physical_session_id": physical_session_id,
        "physical_subagent_id": physical_subagent_id,
        "evidence": "terminal_record",
        "source_ref": "end-1",
    }


def _rows(spool: UsageOutbox) -> list[dict]:
    with sqlite3.connect(spool.path) as db:
        return [
            json.loads(row[0])
            for row in db.execute("SELECT payload FROM observations ORDER BY sequence")
        ]


@pytest.mark.parametrize("input_tokens", ["bad", -1, True])
def test_agy_missing_response_id_preserves_unknown_and_dedups_native_step(
    tmp_path: Path, input_tokens: object
) -> None:
    directory = tmp_path / "spool"
    spool = UsageOutbox(directory)
    physical = {
        "harness_family": "antigravity",
        "identity_namespace": "antigravity/cascade/v1",
        "physical_session_id": "cascade-1",
        "thread_kind": "main",
    }
    record = {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_DONE",
        "metadata": {
            "executionId": "native-turn",
            "sourceTrajectoryStepInfo": {"trajectoryId": "trajectory-1", "stepIndex": 2},
            "modelUsage": {"inputTokens": input_tokens, "outputTokens": "2"},
        },
    }
    spool.append("session", "antigravity-native", physical, record)
    UsageOutbox(directory).append("session", "antigravity-native", physical, record)
    rows = _rows(spool)
    assert len(rows) == 1
    assert rows[0]["logical_call_id"] is None and rows[0]["source_refs"] == []
    assert rows[0]["call_identity_coverage"] == "none"
    assert rows[0]["input_tokens"] is None and rows[0]["cached_input_tokens"] is None
    assert rows[0]["output_tokens"] == 2 and rows[0]["usage_status"] == "partial"
    assert rows[0]["turn_id"] == "native-turn"


def test_agy_retry_usage_is_content_free_and_stable_across_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export import runtime

    spool = UsageOutbox(tmp_path / "spool")
    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1")
    monkeypatch.setattr(runtime, "outbox", lambda: spool)
    usage = {
        "inputTokens": "10",
        "cacheReadTokens": "0",
        "cacheWriteTokens": "0",
        "outputTokens": "2",
        "thinkingOutputTokens": "0",
        "responseId": "final",
    }
    record = {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_ERROR",
        "metadata": {
            "executionId": "turn",
            "sourceTrajectoryStepInfo": {"trajectoryId": "native", "stepIndex": 2},
            "modelUsage": usage,
            "retryInfos": [{"usage": {**usage, "responseId": "retry", "outputTokens": "1"}}],
        },
        "plannerResponse": {"response": "never-retain"},
    }
    for _ in range(2):
        runtime.capture_antigravity_step("session", "cascade", record, thread_kind="subagent")
    rows = _rows(spool)
    assert len(rows) == 2
    assert {row["logical_call_id"] for row in rows} == {"final", "retry"}
    assert sum(row["output_tokens"] for row in rows) == 3
    assert all(
        row["physical_session_id"] == "cascade" and row["thread_kind"] == "subagent"
        for row in rows
    )
    assert b"never-retain" not in spool.path.read_bytes()


def test_pi_native_resume_baseline_and_cache_warm_are_content_free(tmp_path: Path) -> None:
    spool = UsageOutbox(tmp_path / "spool")
    root = tmp_path / "pi-sessions"
    root.mkdir()
    source = root / "physical.jsonl"
    header = {"type": "session", "id": "physical-1", "timestamp": "2026-10-20T00:00:00Z"}
    old = {
        "type": "message",
        "id": "old",
        "timestamp": "2026-10-20T00:00:01Z",
        "message": {"role": "assistant", "usage": {"input": 100, "output": 1}},
    }
    source.write_text(json.dumps(header) + "\n" + json.dumps(old) + "\n")
    spool.register_pi_root(root, "session")
    response = {
        "type": "message",
        "id": "entry-1",
        "timestamp": "2026-10-20T00:00:02Z",
        "message": {
            "role": "assistant",
            "model": "model-1",
            "responseId": "r-1",
            "content": "must-not-be-stored",
            "usage": {"input": 3, "output": 2, "cacheRead": 4, "cacheWrite": 5},
        },
    }
    warm = {
        "type": "usage",
        "id": "warm-1",
        "kind": "cache_warm",
        "timestamp": "2026-10-20T00:00:03Z",
        "model": "model-1",
        "note": "must-not-be-stored",
        "usage": {"input": 0, "output": 0, "cacheRead": 8, "cacheWrite": 2},
    }
    compact = {
        "type": "compaction",
        "id": "compact-1",
        "timestamp": "2026-10-20T00:00:04Z",
        "summary": "must-not-be-stored",
        "usage": {"input": 1, "output": 3, "cacheRead": 0, "cacheWrite": 0},
    }
    with source.open("a") as file:
        file.write(
            json.dumps(response) + "\n" + json.dumps(warm) + "\n" + json.dumps(compact) + "\n"
        )
    spool.scan_registered_sources()
    clone = root / "clone.jsonl"
    clone.write_text(
        json.dumps({**header, "id": "physical-2"})
        + "\n"
        + json.dumps(old)
        + "\n"
        + json.dumps(response)
        + "\n"
        + json.dumps(warm)
        + "\n"
        + json.dumps(compact)
        + "\n"
    )
    UsageOutbox(tmp_path / "spool").scan_registered_sources()
    rows = _rows(spool)
    assert [row["producer_unit_id"] for row in rows] != ["old"]
    assert [
        (row["input_tokens"], row["cached_input_tokens"], row["cache_write_input_tokens"])
        for row in rows
    ] == [(3, 4, 5), (0, 8, 2), (1, 0, 0)]
    assert rows[0]["logical_call_id"] == "r-1"
    assert rows[0]["physical_session_id"] == "physical-1"
    assert rows[1]["logical_call_id"] is None
    assert "must-not-be-stored" not in str(rows)


def test_pi_native_reconnect_before_scan_retains_new_file_usage(tmp_path: Path) -> None:
    root = tmp_path / "pi-sessions"
    root.mkdir()
    spool_dir = tmp_path / "spool"
    UsageOutbox(spool_dir).register_pi_root(root, "session")
    source = root / "physical.jsonl"
    source.write_text(
        json.dumps({"type": "session", "id": "physical-1"})
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "entry-1",
                "timestamp": "2026-10-20T00:00:02Z",
                "message": {
                    "role": "assistant",
                    "responseId": "r-1",
                    "usage": {"input": 3, "output": 2, "cacheRead": 1, "cacheWrite": 0},
                },
            }
        )
        + "\n"
    )
    reconnected = UsageOutbox(spool_dir)
    reconnected.register_pi_root(root, "session")
    reconnected.scan_registered_sources()
    UsageOutbox(spool_dir).scan_registered_sources()
    rows = _rows(reconnected)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 3
    assert rows[0]["logical_call_id"] == "r-1"


def test_pi_rpc_message_preserves_unknown_counters_and_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.runtime import capture_pi_rpc_message

    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "runtime"))
    message = {
        "role": "assistant",
        "responseId": "r-1",
        "model": "model-1",
        "content": "must-not-be-stored",
        "usage": {"input": 2, "output": 3},
    }
    capture_pi_rpc_message("session", message)
    capture_pi_rpc_message("session", message)
    rows = _rows(UsageOutbox(tmp_path / "runtime" / "usage-export"))
    assert len(rows) == 1
    assert rows[0]["cached_input_tokens"] is None
    assert rows[0]["cache_write_input_tokens"] is None
    assert rows[0]["usage_status"] == "partial"
    assert "must-not-be-stored" not in str(rows)


def _deferred(spool: UsageOutbox) -> list[dict]:
    with sqlite3.connect(spool.path) as db:
        return [
            json.loads(row[0]) for row in db.execute("SELECT payload FROM deferred_measurements")
        ]


@pytest.mark.parametrize("harness", [None, "pi-native"])
def test_pi_filtered_final_scan_discovers_sources_and_keeps_cutover_origins(
    tmp_path: Path, harness: str | None
) -> None:
    from omnigent.usage_export.cli import _pending_calls

    spool = UsageOutbox(tmp_path / "spool")
    root = tmp_path / "pi-sessions"
    root.mkdir()
    spool.register_pi_root(root, "session")
    source = root / "physical.jsonl"
    header = {"type": "session", "id": "physical-1"}
    entry = {
        "type": "message",
        "id": "before",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": "assistant", "usage": {"input": 3, "output": 2}},
    }
    source.write_text(json.dumps(header) + "\n" + json.dumps(entry) + "\n")
    spool.scan_registered_sources(harness="codex-native", final=True)
    assert not spool.has_source(source)
    spool.scan_registered_sources(harness=harness)
    with sqlite3.connect(spool.path) as db:
        db.execute(
            "UPDATE producers SET binding=?",
            (
                json.dumps(
                    {
                        "host_id": "host",
                        "session_id": "session",
                        "harness": "pi-native",
                        "agent_id": "agent",
                    }
                ),
            ),
        )
    checkpoint = spool.checkpoint("host", "pi-native", 1)
    before = spool.domain_report("pi-native")
    assert before["units"][0]["origin"] == {
        "path": str(source),
        "position": source.stat().st_size,
    }
    assert _pending_calls(before, checkpoint, 1767225660)[0]["state"] == "unsent"

    clone = root / "clone.jsonl"
    # The final scan must discover new files, pass the live 1000-line limit,
    # accept a last complete JSON value without a newline, and dedupe copies.
    clone.write_text(
        json.dumps({**header, "id": "physical-2"})
        + "\n"
        + json.dumps(entry)
        + "\n"
        + "{}\n" * 1001
        + json.dumps({**entry, "id": "after", "timestamp": "2026-01-01T00:02:00Z"})
    )
    UsageOutbox(tmp_path / "spool").scan_registered_sources(harness=harness, final=True)
    spool.scan_registered_sources(harness=harness, final=True)
    report = spool.domain_report("pi-native")
    assert len(report["units"]) == 2
    assert [call["state"] for call in _pending_calls(report, checkpoint, 1767225660)] == [
        "unsent",
        "unadmitted_after_hold",
    ]
    assert report["units"][1]["origin"] == {
        "path": str(clone),
        "position": clone.stat().st_size,
    }


@pytest.mark.parametrize("harness", ["pi-native", "pi"])
def test_pi_unadmitted_usage_invalidates_hold_and_refuses_new_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    from omnigent.usage_export.cutover import UsageCutoverStore
    from omnigent.usage_export.models import UsageObservation
    from omnigent.usage_export.store import UsageEventStore, UsageExportError

    uri = f"sqlite:///{tmp_path / 'usage.db'}"
    feed = UsageEventStore(uri)
    cutover = UsageCutoverStore(uri)
    spool = UsageOutbox(tmp_path / "spool")
    monkeypatch.setattr("omnigent.usage_export.cutover._now", lambda: 1767225600)
    cutover.create_hold("alice", "host", harness, 1)
    with pytest.raises(UsageExportError) as refused:
        cutover.admit_turn(
            "alice",
            admission_id="refused",
            session_id="session",
            host_id="host",
            harness=harness,
            source="web",
        )
    assert refused.value.code == "usage_cutover_hold"
    spool.append(
        "session",
        harness,
        {
            "harness_family": "pi",
            "identity_namespace": "pi/session/v1" if harness == "pi-native" else "pi/rpc/v1",
            "thread_kind": "main",
            "physical_session_id": "physical-1",
        },
        {
            "type": "message",
            "id": "after",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "assistant", "usage": {"input": 3, "output": 2}},
        },
    )
    event = feed.append(
        "alice",
        UsageObservation.model_validate(
            {
                **_rows(spool)[0],
                "session_id": "session",
                "agent_id": "agent",
                "host_id": "host",
                "harness": harness,
            }
        ),
    )
    assert event["sequence"] == 1
    assert event["call_started_not_before"] is None
    assert cutover.hold_status("alice", "host", harness)["state"] == "invalidated"
    with pytest.raises(UsageExportError, match="not_drained"):
        cutover.submit_drain("alice", "host", harness, 1, {})


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
    record["message"]["usage"]["input_tokens"] = None
    spool.append("session", "claude-sdk", child, record)
    assert _rows(spool) == []
    rows = _deferred(spool)
    assert [row["output_tokens"] for row in rows] == [5]
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
    rows = _deferred(UsageOutbox(tmp_path / "runtime" / "usage-export"))
    assert len(rows) == 1
    assert rows[0]["physical_session_id"] == "parent"
    assert rows[0]["physical_subagent_id"] == "child"
    assert rows[0]["physical_transcript_path"] is None


@pytest.mark.asyncio
async def test_sdk_hook_proves_parent_and_child_paths_after_pending_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.runtime import capture_claude_hook_path, capture_claude_mirror

    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "runtime"))
    project = tmp_path / "projects" / "project"
    project.mkdir(parents=True)
    parent = project / "parent.jsonl"
    parent.write_text("")
    child = project / "parent" / "subagents" / "agent-child.jsonl"
    child.parent.mkdir(parents=True)
    child.write_text("")
    record = {
        "type": "assistant",
        "sessionId": "observed-child",
        "message": {
            "id": "response-child",
            "content": "private-content-must-not-persist",
            "usage": {"input_tokens": 2, "output_tokens": 3},
        },
    }
    capture_claude_mirror(
        "run",
        {"session_id": "parent", "subpath": "subagents/agent-child"},
        [record],
        internal=False,
    )
    spool = UsageOutbox(tmp_path / "runtime" / "usage-export")
    assert _deferred(spool)[0]["physical_transcript_path"] is None

    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/usage-producers":
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    **payload,
                    "agent_id": "agent",
                    "host_id": "host",
                    "parent_session_id": None,
                },
            )
        return httpx.Response(200, json={"event_id": "event"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://unit.test"
    ) as client:
        await UsageOutbox(spool.path.parent).drain(client)
        assert requests == []
        capture_claude_hook_path(
            "run",
            {
                "hook_event_name": "SubagentStop",
                "session_id": "parent",
                "agent_id": "child",
                "transcript_path": str(parent),
                "agent_transcript_path": str(child),
            },
        )
        assert _rows(UsageOutbox(spool.path.parent))[0]["physical_transcript_path"] == str(
            child.resolve()
        )
        await UsageOutbox(spool.path.parent).drain(client)
    assert [request.url.path for request in requests] == [
        "/v1/usage-producers",
        "/v1/usage-observations",
        "/v1/usage-observations",
    ]
    sent = json.loads(requests[-1].content)["observation"]
    assert sent["physical_session_id"] == "parent"
    assert sent["physical_subagent_id"] == "child"
    assert sent["transcript_session_id"] == "observed-child"
    assert sent["physical_transcript_path"] == str(child.resolve())
    assert "private-content-must-not-persist" not in str(sent)

    capture_claude_hook_path(
        "run",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "parent",
            "transcript_path": str(parent),
        },
    )
    capture_claude_mirror(
        "run",
        {"session_id": "parent"},
        [{**record, "message": {**record["message"], "id": "response-parent"}}],
        internal=False,
    )
    assert _rows(UsageOutbox(spool.path.parent))[-1]["physical_transcript_path"] == str(
        parent.resolve()
    )


def test_sdk_conflicting_hook_path_keeps_mirror_identity_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.runtime import capture_claude_hook_path, capture_claude_mirror

    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "runtime"))
    child = tmp_path / "projects" / "project" / "parent" / "subagents" / "agent-other.jsonl"
    child.parent.mkdir(parents=True)
    child.write_text("")
    capture_claude_hook_path(
        "run",
        {
            "hook_event_name": "SubagentStop",
            "session_id": "parent",
            "agent_id": "child",
            "agent_transcript_path": str(child),
        },
    )
    child = child.with_name("agent-child.jsonl")
    child.write_text("")
    capture_claude_hook_path(
        "run",
        {
            "hook_event_name": "SubagentStop",
            "session_id": "parent",
            "agent_id": "child",
            "agent_transcript_path": str(child),
        },
    )
    capture_claude_mirror(
        "run",
        {"session_id": "parent", "subpath": "subagents/agent-child"},
        [{"type": "assistant", "message": {"id": "response", "usage": {"input_tokens": 1}}}],
        internal=False,
    )
    assert (
        _deferred(UsageOutbox(tmp_path / "runtime" / "usage-export"))[0][
            "physical_transcript_path"
        ]
        is None
    )


def test_sdk_path_proof_uses_canonical_identity_across_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.runtime import capture_claude_hook_path, capture_claude_mirror

    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "runtime"))
    project = tmp_path / "projects" / "project"
    project.mkdir(parents=True)
    parent = project / "parent.jsonl"
    parent.write_text("")
    alias_dir = tmp_path / "project-alias"
    alias_dir.symlink_to(project, target_is_directory=True)
    record = {"type": "assistant", "message": {"id": "call", "usage": {"input_tokens": 1}}}
    capture_claude_hook_path(
        "run",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "parent",
            "transcript_path": str(alias_dir / "parent.jsonl"),
        },
    )
    capture_claude_mirror("run", {"session_id": "parent"}, [record], internal=False)
    spool = UsageOutbox(tmp_path / "runtime" / "usage-export")
    rows = _rows(spool) + _deferred(spool)
    assert rows[0]["physical_transcript_path"] == str(parent.resolve())

    wrong = project / "other.jsonl"
    wrong.write_text("")
    alias_file = project / "parent-alias.jsonl"
    alias_file.symlink_to(wrong)
    capture_claude_hook_path(
        "other-run",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "parent",
            "transcript_path": str(alias_file),
        },
    )
    capture_claude_mirror("other-run", {"session_id": "parent"}, [record], internal=False)
    rows = _rows(UsageOutbox(tmp_path / "runtime" / "usage-export"))
    assert rows[-1]["physical_transcript_path"] is None

    capture_claude_hook_path(
        "third-run",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "parent",
            "transcript_path": "relative/parent.jsonl",
        },
    )
    capture_claude_mirror("third-run", {"session_id": "parent"}, [record], internal=False)
    rows = _rows(UsageOutbox(tmp_path / "runtime" / "usage-export"))
    assert rows[-1]["physical_transcript_path"] is None


@pytest.mark.asyncio
async def test_sdk_drain_rechecks_path_after_producer_registration(
    tmp_path: Path,
) -> None:
    spool = UsageOutbox(tmp_path / "spool")
    project = tmp_path / "projects" / "project"
    project.mkdir(parents=True)
    parent = project / "parent.jsonl"
    other = project / "other.jsonl"
    parent.write_text("")
    other.write_text("")
    spool.bind_claude_path("run", "parent", None, str(parent.resolve()))
    spool.append(
        "run",
        "claude-sdk",
        {"physical_session_id": "parent", "physical_subagent_id": None},
        {"type": "assistant", "message": {"id": "response", "usage": {"input_tokens": 1}}},
    )
    paths = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/usage-producers":
            spool.bind_claude_path("run", "parent", None, str(other.resolve()))
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    **payload,
                    "agent_id": "agent",
                    "host_id": "host",
                    "parent_session_id": None,
                },
            )
        pytest.fail("conflicted SDK observation was sent")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://unit.test"
    ) as client:
        await spool.drain(client)
    assert paths == ["/v1/usage-producers"]
    assert len(_rows(spool)) == 1


@pytest.mark.asyncio
async def test_sdk_drain_serializes_conflict_with_observation_post(tmp_path: Path) -> None:
    spool = UsageOutbox(tmp_path / "spool")
    project = tmp_path / "projects" / "project"
    project.mkdir(parents=True)
    parent = project / "parent.jsonl"
    other = project / "other.jsonl"
    parent.write_text("")
    other.write_text("")
    spool.bind_claude_path("run", "parent", None, str(parent.resolve()))
    spool.append(
        "run",
        "claude-sdk",
        {"physical_session_id": "parent", "physical_subagent_id": None},
        {"type": "assistant", "message": {"id": "response", "usage": {"input_tokens": 1}}},
    )
    conflict_task = None

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal conflict_task
        if request.url.path == "/v1/usage-producers":
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    **payload,
                    "agent_id": "agent",
                    "host_id": "host",
                    "parent_session_id": None,
                },
            )
        conflict_task = asyncio.create_task(
            asyncio.to_thread(spool.bind_claude_path, "run", "parent", None, str(other.resolve()))
        )
        await asyncio.sleep(0.05)
        assert not conflict_task.done()
        return httpx.Response(200, json={"event_id": "event"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://unit.test"
    ) as client:
        await spool.drain(client)
    assert conflict_task is not None
    await asyncio.wait_for(conflict_task, timeout=5)
    assert _rows(spool) == []
    with sqlite3.connect(spool.path) as db:
        assert db.execute("SELECT conflicted FROM claude_path_proofs").fetchone()[0] == 1


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
    spool = UsageOutbox(tmp_path / "runtime" / "usage-export")
    rows = _rows(spool) + _deferred(spool)
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
        {
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": "2026-10-20T00:00:05Z",
            "uuid": "end-1",
        },
    ]
    for record in records:
        UsageOutbox(tmp_path / "spool").append("session", "claude-sdk", physical, record)
    rows = _rows(spool)
    assert len(rows) == 4
    assert [row.get("terminal") for row in rows] == [
        None,
        None,
        [_terminal_evidence("parent", None)],
        [_terminal_evidence("parent", None)],
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
        "uuid": "end-1",
        "type": "event_msg" if codex else "system",
        "subtype": "turn_duration",
        "timestamp": "2026-10-20T00:00:05Z",
        "payload": {"type": "task_complete", "turn_id": "turn-1"},
    }
    spool = UsageOutbox(tmp_path / "spool")
    spool.append("session", harness, physical, terminal)
    spool.append("session", harness, physical, terminal)
    rows = _rows(spool)
    assert len(rows) == 2
    assert rows[-1]["producer_unit_id"] == rows[0]["producer_unit_id"]
    assert rows[-1]["terminal"] == [_terminal_evidence("parent", None)]
    assert _deferred(spool)[0].get("terminal") is None
    if not codex:
        spool.append(
            "session", harness, physical, {"type": "user", "message": {"content": "next"}}
        )
        first["message"]["id"] = "second"
    else:
        first["payload"].update(response_id="second", turn_id="turn-2")
    spool.append("session", harness, physical, first)
    assert _rows(spool)[-1].get("terminal") is None


@pytest.mark.asyncio
async def test_first_observation_waits_for_codex_turn_start_and_carries_server_ack(
    tmp_path: Path,
) -> None:
    spool = UsageOutbox(tmp_path / "spool")
    spool.begin_admission(
        "session", "codex-native", {"admission_id": "admitted", "route_check": "match"}
    )
    record = _codex("main", "response", 5)
    record["payload"]["turn_id"] = "turn-1"
    spool.append(
        "session", "codex-native", {"physical_session_id": "main", "thread_kind": "main"}, record
    )
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if request.url.path == "/v1/usage-producers":
            return httpx.Response(
                200,
                json={**body, "agent_id": "agent", "host_id": "host", "parent_session_id": None},
            )
        assert body["observation"]["admission_id"] == "admitted"
        assert body["observation"]["route_check"] == "match"
        return httpx.Response(
            200,
            json={
                "event_id": "a" * 32,
                "original_event_id": "a" * 32,
                "sequence": 9,
                "revision": 1,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://omni.test"
    ) as client:
        await spool.drain(client)
        assert requests == []
        assert _rows(spool) == []
        assert spool.domain_report("codex-native")["pending_observations"] == 1
        spool.link_admission(
            "session", "codex-native", "main", None, "turn-1", "admitted", route_check="match"
        )
        spool.finish_admission("session", "codex-native")
        await spool.drain(client)
    assert len(requests) == 2
    report = UsageOutbox(tmp_path / "spool").domain_report("codex-native")
    assert report["pending_observations"] == 0
    assert report["units"][0]["server_ack"]["sequence"] == 9


def test_live_route_comparison_ignores_authentication_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.runtime import (
        begin_admitted_turn,
        check_live_route,
        outbox,
        routing_attestation,
    )

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1")
    route = routing_attestation("claude-sdk", {"ANTHROPIC_API_KEY": "test-one"})
    assert route == routing_attestation("claude-sdk", {"ANTHROPIC_API_KEY": "test-two"})
    assert "test-one" not in str(route)
    begin_admitted_turn("session", "claude-sdk", {"admission_id": "admitted", "route": route})
    check_live_route("session", "claude-sdk", {"ANTHROPIC_API_KEY": "test-one"})
    assert outbox().pending_admission("session", "claude-sdk")["route_check"] == "match"
    check_live_route(
        "session", "claude-sdk", {"ANTHROPIC_BASE_URL": "https://gateway.example.test/anthropic"}
    )
    assert outbox().pending_admission("session", "claude-sdk")["route_check"] == "mismatch"


@pytest.mark.parametrize(
    "stamp, linked", [("2026-01-01T00:00:05Z", True), ("2026-01-01T00:00:20Z", False)]
)
@pytest.mark.asyncio
async def test_child_links_only_to_an_enclosing_parent_span(
    tmp_path: Path, stamp: str, linked: bool
) -> None:
    spool = UsageOutbox(tmp_path / "spool")
    parent = {"physical_session_id": "parent", "thread_kind": "main"}
    spool.link_admission(
        "session", "codex-native", "parent", None, "turn-parent", "admitted", route_check="match"
    )
    spool.append(
        "session",
        "codex-native",
        parent,
        {
            "type": "turn_context",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"turn_id": "turn-parent"},
        },
    )
    spool.append(
        "session",
        "codex-native",
        parent,
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:10Z",
            "payload": {"type": "task_complete", "turn_id": "turn-parent"},
        },
    )
    record = _codex("child", "child-response", 3)
    record["timestamp"] = stamp
    record["payload"]["turn_id"] = "child-turn"
    spool.append(
        "session",
        "codex-native",
        {
            "physical_session_id": "child",
            "parent_physical_session_id": "parent",
            "thread_kind": "subagent",
        },
        record,
    )
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/usage-producers":
            return httpx.Response(
                200,
                json={**body, "agent_id": "agent", "host_id": "host", "parent_session_id": None},
            )
        sent.append(body["observation"])
        return httpx.Response(200, json={"event_id": "a" * 32})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://omni.test"
    ) as client:
        await spool.drain(client)
    assert len(sent) == 1
    assert sent[0].get("admission_id") == ("admitted" if linked else None)
    assert not sent[0].get("terminal")


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


def test_native_gate_correlation_survives_capture_off_and_child_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.claude_native.bridge import (
        read_hook_events_since_with_position,
        record_hook_event,
    )

    bridge = tmp_path / "bridge"
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
    parent = {"session_id": "parent", "hook_event_name": "UserPromptSubmit"}
    record_hook_event(bridge, {**parent, "admission_id": "admitted"})
    record_hook_event(bridge, parent)
    record_hook_event(
        bridge, {"session_id": "child", "hook_event_name": "Stop", "agent_id": "child"}
    )
    record_hook_event(bridge, {"session_id": "parent", "hook_event_name": "Stop"})
    records = read_hook_events_since_with_position(bridge, 0).records
    assert records[2].usage_admission_id is None
    assert records[3].usage_admission_id == "admitted"


@pytest.mark.asyncio
async def test_native_terminal_closes_only_its_bound_admission(tmp_path: Path) -> None:
    from omnigent.harnesses.codex_native.bridge import (
        CodexNativeBridgeState,
        pending_usage_completions,
        record_usage_admission,
        update_active_turn_id,
        write_bridge_state,
    )
    from omnigent.harnesses.codex_native.forwarder import _queue_usage_terminal

    bridge = tmp_path / "bridge"
    write_bridge_state(
        bridge, CodexNativeBridgeState("session", "/opt/work/socket", "thread", "/opt/work/home")
    )
    record_usage_admission(bridge, "admitted")

    def event(method, turn):
        return {"method": method, "params": {"threadId": "thread", "turn": {"id": turn}}}

    await _queue_usage_terminal(bridge, event("turn/started", "new"))
    update_active_turn_id(bridge, "new")
    await _queue_usage_terminal(bridge, event("turn/completed", "old"))
    assert pending_usage_completions(bridge) == []
    await _queue_usage_terminal(bridge, event("turn/completed", "new"))
    assert pending_usage_completions(bridge) == [("session", "admitted")]


@pytest.mark.asyncio
async def test_preparation_publishes_effective_codex_route_and_retains_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.usage_export.runtime import check_live_route, outbox, prepare_capture

    monkeypatch.setenv("OMNIGENT_USAGE_EXPORT_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider="openai"\n')
    check_live_route("session", "codex-native", {}, codex_home=home)
    published = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/usage-producers":
            return httpx.Response(
                200,
                json={**body, "host_id": "host", "agent_id": "agent", "parent_session_id": None},
            )
        published.append(body)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://omni.test"
    ) as client:
        await prepare_capture(client, "session", "codex-native")
        effective = outbox().live_routes()[0]
        assert published[-1] == effective
        monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong.example.test/runner-only")
        await prepare_capture(client, "session", "codex-native")
        assert published[-1] == effective
        (home / "config.toml").write_text(
            'model_provider="gateway"\n[model_providers.gateway]\nbase_url="https://gateway.example.test/account-a/v1"\n'
        )
        check_live_route("session", "codex-native", {}, codex_home=home)
        await prepare_capture(client, "session", "codex-native")
        assert published[-1] == outbox().live_routes()[0]
        assert published[-1] != effective


def test_route_fingerprint_distinguishes_gateway_paths_without_auth_material() -> None:
    from omnigent.usage_export.runtime import routing_attestation

    def route(url):
        return routing_attestation("claude-sdk", {"ANTHROPIC_BASE_URL": url})

    first = route("https://gateway.example.test/account-a/anthropic")
    assert first != route("https://gateway.example.test/account-b/anthropic")
    assert first == route(
        "https://user:pass@gateway.example.test/account-a/anthropic?key=secret#frag"
    )
    assert "secret" not in str(first) and "pass" not in str(first)
