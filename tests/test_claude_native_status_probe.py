"""Read-only Claude-native sub-agent status proof."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnigent import claude_native_status_probe as probe


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")


def _build_probe_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    meta_tool_use_id: str = "toolu_current",
    state_tool_use_id: str | None = "toolu_current",
    with_terminal_evidence: bool = True,
) -> tuple[Path, Path]:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "claude-session.jsonl"
    records: list[dict[str, Any]] = [
        {
            "type": "assistant",
            "uuid": "call",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_current",
                        "name": "Agent",
                        "input": {"prompt": "historical prompt"},
                    }
                ],
            },
        }
    ]
    if with_terminal_evidence:
        records.append(
            {
                "type": "user",
                "uuid": "result",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_current",
                            "content": "done",
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
        )
    _write_jsonl(transcript_path, records)
    _write_json(
        bridge_dir / "bridge.json",
        {
            "bridge_id": "bridge-current",
            "active_session_id": "parent-current",
        },
    )
    _write_json(
        bridge_dir / "state.json",
        {
            "claude_session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    _write_json(
        bridge_dir / "subagent_forwarder.json",
        {
            "subagents": {
                "child-native": {
                    "child_conversation_id": "child-server",
                    "tool_use_id": state_tool_use_id,
                    "byte_offset": 0,
                    "last_status": "running",
                }
            }
        },
    )
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    _write_json(
        subagents_dir / "agent-child-native.meta.json",
        {
            "agentType": "Explore",
            "description": "inspect",
            "toolUseId": meta_tool_use_id,
        },
    )
    monkeypatch.setattr(probe, "bridge_dir_for_bridge_id", lambda _bridge_id: bridge_dir)
    return bridge_dir, transcript_path


def test_probe_returns_terminal_only_for_current_structured_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bridge_dir, _transcript_path = _build_probe_fixture(tmp_path, monkeypatch)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    result = probe.probe_native_subagent_status(
        parent_session_id="parent-current",
        bridge_id="bridge-current",
    )

    assert result["parent_session_id"] == "parent-current"
    assert result["claude_session_id"] == "claude-session"
    assert result["parent_complete_byte_offset"] > 0
    assert result["children"] == [
        {
            "server_session_id": "child-server",
            "subagent_id": "child-native",
            "tool_use_id": "toolu_current",
            "status": "terminal",
            "terminal_status": "completed",
            "reason": "structured_parent_terminal_evidence",
            "evidence": {
                "claude_session_id": "claude-session",
                "parent_complete_byte_offset": result["parent_complete_byte_offset"],
                "subagent_id": "child-native",
                "meta_tool_use_id": "toolu_current",
                "evidence_key": result["children"][0]["evidence"]["evidence_key"],
            },
        }
    ]
    assert len(result["children"][0]["evidence"]["evidence_key"]) == 64
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    ("fixture_kwargs", "reason"),
    [
        ({"state_tool_use_id": None}, "missing_tool_use_id"),
        (
            {"meta_tool_use_id": "toolu_replaced"},
            "tool_use_id_mismatch",
        ),
        (
            {"with_terminal_evidence": False},
            "no_structured_terminal_evidence",
        ),
    ],
)
def test_probe_keeps_uncertain_child_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_kwargs: dict[str, object],
    reason: str,
) -> None:
    _build_probe_fixture(tmp_path, monkeypatch, **fixture_kwargs)  # type: ignore[arg-type]

    result = probe.probe_native_subagent_status(
        parent_session_id="parent-current",
        bridge_id="bridge-current",
    )

    child = result["children"][0]
    assert child["status"] == "unverified"
    assert child["terminal_status"] is None
    assert child["reason"] == reason


def test_probe_rejects_bridge_bound_to_another_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _build_probe_fixture(tmp_path, monkeypatch)

    with pytest.raises(probe.NativeSubagentProbeError) as caught:
        probe.probe_native_subagent_status(
            parent_session_id="parent-old",
            bridge_id="bridge-current",
        )

    assert caught.value.http_status == 409
    assert caught.value.code == "native_parent_identity_mismatch"


def test_probe_rejects_parent_rotation_during_child_correlation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge_dir, _transcript_path = _build_probe_fixture(tmp_path, monkeypatch)
    original_read_meta = probe._read_subagent_meta

    def rotate_parent(meta_path: Path) -> dict[str, str] | None:
        meta = original_read_meta(meta_path)
        _write_json(
            bridge_dir / "bridge.json",
            {
                "bridge_id": "bridge-current",
                "active_session_id": "parent-replacement",
            },
        )
        return meta

    monkeypatch.setattr(probe, "_read_subagent_meta", rotate_parent)

    with pytest.raises(probe.NativeSubagentProbeError) as caught:
        probe.probe_native_subagent_status(
            parent_session_id="parent-current",
            bridge_id="bridge-current",
        )

    assert caught.value.http_status == 409
    assert caught.value.code == "native_parent_identity_changed"


def test_probe_rejects_parent_append_during_child_correlation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bridge_dir, transcript_path = _build_probe_fixture(tmp_path, monkeypatch)
    original_read_meta = probe._read_subagent_meta

    def append_parent(meta_path: Path) -> dict[str, str] | None:
        meta = original_read_meta(meta_path)
        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "progress", "uuid": "new"}) + "\n")
        return meta

    monkeypatch.setattr(probe, "_read_subagent_meta", append_parent)

    with pytest.raises(probe.NativeSubagentProbeError) as caught:
        probe.probe_native_subagent_status(
            parent_session_id="parent-current",
            bridge_id="bridge-current",
        )

    assert caught.value.http_status == 409
    assert caught.value.code == "native_parent_identity_changed"


def test_probe_rejects_forwarder_registration_change_during_child_correlation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge_dir, _transcript_path = _build_probe_fixture(tmp_path, monkeypatch)
    original_read_meta = probe._read_subagent_meta

    def change_registration(meta_path: Path) -> dict[str, str] | None:
        meta = original_read_meta(meta_path)
        state_path = bridge_dir / "subagent_forwarder.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["subagents"]["child-native"]["tool_use_id"] = "toolu_replaced"
        _write_json(state_path, state)
        return meta

    monkeypatch.setattr(probe, "_read_subagent_meta", change_registration)

    with pytest.raises(probe.NativeSubagentProbeError) as caught:
        probe.probe_native_subagent_status(
            parent_session_id="parent-current",
            bridge_id="bridge-current",
        )

    assert caught.value.http_status == 409
    assert caught.value.code == "native_parent_identity_changed"
