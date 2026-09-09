"""Recover native hook snapshots without inventing a completed compaction."""

from itertools import pairwise
from pathlib import Path

import pytest

from omnigent.claude_native import _claude_transcript_records_from_session_items


def _records(items):
    return _claude_transcript_records_from_session_items(
        items,
        session_id="fixture-conversation",
        external_session_id="fixture-native",
        cwd=Path("/fixture"),
        bridge_dir=Path("/fixture-bridge"),
    )


@pytest.mark.parametrize("source", [None, "hook_fallback"])
def test_legacy_hook_snapshot_keeps_context_without_false_boundary(source):
    snapshot = {
        "type": "compaction",
        "summary": "[Claude Code compaction — context was compacted in the terminal]",
        "token_count": 0,
        "snapshot_source": source,
        "compacted_messages": [
            {"type": "message", "role": "user", "content": f"history {i}"} for i in range(500)
        ],
    }
    records = _records(
        [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "old prefix"}],
            },
            snapshot,
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "continue"}],
            },
        ]
    )
    assert len(records) == 501
    assert all(record["type"] == "user" for record in records)
    assert records[0]["parentUuid"] is None
    for previous, current in pairwise(records):
        assert current["parentUuid"] == previous["uuid"]
    assert "context was compacted" not in str(records)
    assert "old prefix" not in str(records)


def test_authoritative_snapshot_replaces_fallback_and_keeps_new_tail():
    records = _records(
        [
            {
                "type": "compaction",
                "snapshot_source": "hook_fallback",
                "compacted_messages": [
                    {"type": "message", "role": "user", "content": "raw history"}
                ],
            },
            {
                "type": "compaction",
                "snapshot_source": "transcript",
                "summary": "actual summary",
                "compacted_messages": [
                    {"type": "message", "role": "user", "content": "actual summary"}
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "new request"}],
            },
        ]
    )
    assert len(records) == 3
    assert records[0]["subtype"] == "compact_boundary"
    assert "actual summary" in str(records[1])
    assert "new request" in str(records[2])
    assert "raw history" not in str(records)


@pytest.mark.parametrize(
    "summary,expected_boundary", [("actual summary", True), ("unrelated", False)]
)
def test_legacy_snapshot_requires_matching_summary(summary, expected_boundary):
    records = _records(
        [
            {
                "type": "compaction",
                "summary": summary,
                "compacted_messages": [
                    {"type": "message", "role": "user", "content": "actual summary"}
                ],
            }
        ]
    )
    assert (
        any(record.get("subtype") == "compact_boundary" for record in records) == expected_boundary
    )
