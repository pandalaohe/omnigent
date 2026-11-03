"""Exercise the native vendor entry points against the shared pending record."""

import io
import json
import time

import pytest

from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.harnesses.claude_native import hook as claude_hook
from omnigent.harnesses.claude_native.bridge import record_hook_event
from omnigent.harnesses.codex_native import hook as codex_hook
from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState, write_bridge_state
from omnigent.native.auto_retry import (
    AutoRetryGuard,
    auto_retry_scope,
    get_snapshot,
    observe_turn,
    prepare_submission,
    read_receipt,
)


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("human_first", [True, False])
@pytest.mark.parametrize("gate_failed", [None, "lock", "epoch"])
def test_real_prompt_hooks_preserve_non_retry_human_input(
    tmp_path, monkeypatch, capsys, harness, human_first, gate_failed
):
    if harness == "claude":
        monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
        monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "claude-native")
        tmp_path = claude_bridge.prepare_bridge_dir("child", workspace=tmp_path)
        record_hook_event(tmp_path, {"hook_event_name": "SessionStart", "session_id": "native"})
    else:
        write_bridge_state(
            tmp_path,
            CodexNativeBridgeState(
                session_id="child",
                thread_id="native",
                socket_path=str(tmp_path / "rpc.sock"),
                codex_home=str(tmp_path / "codex-home"),
            ),
        )
    snapshot = observe_turn(tmp_path, "child", "failed-turn", idle=True)
    guard = AutoRetryGuard(
        session_id="child",
        chain_id="c" * 32,
        input_id="a" * 32,
        expected_turn_id="failed-turn",
        expected_input_generation=3,
        expected_native_generation=snapshot.generation,
        admission_deadline=time.time() + 60,
        attempt=1,
        of=3,
    )
    with auto_retry_scope(guard):
        prepare_submission(tmp_path, native_idle=True)

    def submit(text, turn):
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "native",
            "prompt": text,
            "turn_id": turn,
        }
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        entry = claude_hook.main if harness == "claude" else codex_hook.main
        args = ["--bridge-dir", str(tmp_path)]
        assert entry(args if harness == "claude" else ["evaluate-policy", *args]) == 0
        output = capsys.readouterr().out
        return json.loads(output) if output else {}

    if not human_first:
        assert submit("retry", "auto").get("decision") != "block"
        assert read_receipt(tmp_path, guard.input_id).state == "admitted"
    with monkeypatch.context() as failure:
        if gate_failed == "lock":

            def locked(*args):
                raise OSError("native state lock unavailable")

            failure.setattr("omnigent.native.auto_retry._lock", locked)
        elif gate_failed == "epoch":
            from omnigent.native import auto_retry

            original_write = auto_retry.atomic_write_json

            def failed_epoch(path, value):
                if path.name == "auto-retry-intervention.json":
                    raise OSError("replacement unavailable")
                return original_write(path, value)

            failure.setattr(auto_retry, "atomic_write_json", failed_epoch)
        assert submit("please handle the replacement", "human").get("decision") != "block"
    assert read_receipt(tmp_path, guard.input_id).reason == "superseded_by_human_input"
    human_snapshot = get_snapshot(tmp_path, "child")
    if human_first:
        assert submit("retry", "auto")["decision"] == "block"
        assert get_snapshot(tmp_path, "child") == human_snapshot
    assert read_receipt(tmp_path, guard.input_id).reason == "superseded_by_human_input"
    if harness == "claude":
        records = [
            json.loads(line)["payload"]
            for line in (tmp_path / "hooks.jsonl").read_text().splitlines()
        ]
        human = next(
            item for item in records if item.get("prompt") == "please handle the replacement"
        )
        assert "auto_retry" not in human
        assert human["terminal_identity"]["input_id"] != guard.input_id


def test_pending_record_isolated_from_foreign_codex_child_hook(tmp_path, monkeypatch):
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="child",
            thread_id="native",
            socket_path=str(tmp_path / "rpc.sock"),
            codex_home=str(tmp_path / "codex-home"),
        ),
    )
    before = observe_turn(tmp_path, "child", "failed-turn", idle=True)
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "foreign",
        "agent_id": "agent",
        "prompt": "new work",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert codex_hook.main(["evaluate-policy", "--bridge-dir", str(tmp_path)]) == 0
    assert get_snapshot(tmp_path, "child") == before
