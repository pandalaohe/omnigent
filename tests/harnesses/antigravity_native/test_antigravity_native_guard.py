"""Native decision mapping and protected startup/cache failures."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.harnesses.antigravity_native import guard
from omnigent.permission_tiers import load_rules
from omnigent.permission_tiers.host import (
    HOST_PERMISSION_STATE_FILE,
    HostPermissionState,
    local_host_context,
    write_host_permission_state,
)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    return HostPermissionState("session", "host", str(workspace), "auto", 1, "ready")


def payload(name, args):
    return {"toolCall": {"name": name, "args": args}}


@pytest.mark.parametrize(
    "tier,name,args,expected",
    [
        ("auto", "run_command", {"CommandLine": "mkfs.ext4 /dev/disk4"}, "deny"),
        ("auto", "run_command", {"CommandLine": "rm -rf node_modules"}, "ask"),
        ("auto-standard", "run_command", {"CommandLine": "rm -rf node_modules"}, "ask"),
        ("auto-strict", "run_command", {"CommandLine": "rm -rf node_modules"}, "ask"),
        ("ask", "run_command", {"CommandLine": "echo safe"}, "force_ask"),
        ("plan", "run_command", {"CommandLine": "echo safe"}, "deny"),
        ("accept-edits", "run_command", {"CommandLine": "echo safe"}, "force_ask"),
        ("plan", "view_file", {"AbsolutePath": "a.py"}, "ask"),
        ("plan", "list_dir", {"DirectoryPath": "."}, "ask"),
        ("ask", "unknown_tool", {}, "force_ask"),
        ("auto", "unknown_tool", {}, "deny"),
        ("auto", "run_command", {}, "deny"),
    ],
)
def test_native_decision_mapping(state, tier, name, args, expected):
    context = local_host_context(state, control_paths=(str(Path(state.workspace) / "bridge"),))
    result = guard.decide(payload(name, args), tier, load_rules(), context)
    assert result["decision"] == expected
    if name == "run_command" and "mkfs" in args.get("CommandLine", ""):
        assert "[disk-destroy]" in result["reason"]
        assert "ARES-218" in result["reason"]


@pytest.mark.parametrize("tier", ["ask", "plan", "accept-edits", "auto"])
def test_control_writes_cannot_be_approved(state, tier):
    control = str(Path(state.workspace) / "bridge")
    context = local_host_context(state, control_paths=(control,))
    result = guard.decide(
        payload(
            "write_to_file", {"TargetFile": control + "/permission_tier", "CodeContent": "full"}
        ),
        tier,
        load_rules(),
        context,
    )
    assert result["decision"] == "deny"
    assert "[control-path]" in result["reason"]


def test_unknown_mcp_is_not_a_verified_bridge(state):
    call = payload(
        "call_mcp_tool", {"ServerName": "omnigent", "ToolName": "custom", "Arguments": {}}
    )
    assert not guard._delegated_bridge_call(call, {"tools": [{"name": "sys_os_read"}]})
    assert guard._delegated_bridge_call(call, {"tools": [{"name": "custom"}]})
    call["toolCall"]["args"]["ServerName"] = "other"
    assert not guard._delegated_bridge_call(call, {"tools": [{"name": "custom"}]})


@pytest.mark.parametrize(
    "case", ["missing", "corrupt", "mismatch", "blocked", "relay-missing", "relay-symlink"]
)
def test_cli_cache_failure_is_explicit_denial(tmp_path, state, case):
    from dataclasses import replace

    tier = tmp_path / "permission_tier"
    tier.write_text("auto\n")
    cache = tmp_path / HOST_PERMISSION_STATE_FILE
    if case != "missing":
        write_host_permission_state(
            cache, replace(state, status="blocked" if case == "blocked" else "ready")
        )
    if case == "corrupt":
        cache.write_text("[]")
    if case == "mismatch":
        tier.write_text("full\n")
    if case == "relay-symlink":
        relay = tmp_path / "elsewhere.json"
        relay.write_text("{}")
        (tmp_path / "tool_relay.json").symlink_to(relay)
    result = subprocess.run(
        [sys.executable, "-I", str(Path(guard.__file__)), "--tier-file", str(tier)],
        input=json.dumps(payload("run_command", {"CommandLine": "echo safe"})),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    response = json.loads(result.stdout)
    assert response["decision"] == "deny"
    assert response["reason"].startswith("Permission check unavailable:")


def test_main_checks_server_before_delegating(tmp_path, state, monkeypatch, capsys):
    tier = tmp_path / "permission_tier"
    tier.write_text("auto\n")
    write_host_permission_state(tmp_path / HOST_PERMISSION_STATE_FILE, state)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                payload(
                    "call_mcp_tool",
                    {"ServerName": "omnigent", "ToolName": "sys_os_shell", "Arguments": {}},
                )
            )
        ),
    )

    def unavailable(*_):
        raise OSError("offline")

    monkeypatch.setattr(guard, "_confirmed_relay", unavailable)
    assert guard.main(["--tier-file", str(tier)]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "deny"


def test_all_signature_patterns_and_own_cases():
    signatures = load_rules().signatures["signatures"]
    assert len(signatures) == 214
    for signature in signatures:
        pattern = re.compile(
            signature["pattern"], re.IGNORECASE if signature["ignore_case"] else 0
        )
        for text in signature.get("test_cases", {}).get("positive", []):
            assert pattern.search(text), (signature["id"], text)
        for text in signature.get("test_cases", {}).get("negative", []):
            assert not pattern.search(text), (signature["id"], text)


@pytest.mark.parametrize("tier", ["auto", "full"])
@pytest.mark.parametrize(
    "action",
    [
        "POLICY_ACTION_ALLOW",
        "POLICY_ACTION_UNSPECIFIED",
        "POLICY_ACTION_DENY",
        "POLICY_ACTION_ASK",
        None,
        True,
    ],
)
def test_native_policy_transport_includes_full_and_fails_closed(state, monkeypatch, tier, action):
    from dataclasses import replace

    state = replace(state, tier=tier)
    native = {
        **payload("view_file", {"AbsolutePath": "中文.py"}),
        "conversationId": "cascade",
        "stepIdx": 4,
    }
    events = []

    def relay_post(_relay, path, event):
        events.append((path, event))
        return {"result": action, "reason": "independent denial"}

    monkeypatch.setattr(guard, "_relay_post", relay_post)
    if action in ("POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"):
        assert guard._lease_native_call(native, state, {}) == {
            "decision": "ask" if tier == "full" else "allow"
        }
    elif action == "POLICY_ACTION_DENY":
        assert guard._lease_native_call(native, state, {}) == {
            "decision": "deny",
            "reason": "independent denial",
        }
    else:
        with pytest.raises(ValueError, match="did not confirm permission"):
            guard._lease_native_call(native, state, {})
    assert len(events) == 1
    path, request = events[0]
    assert path == "/policies/evaluate"
    assert request["event"] == {
        "type": "PHASE_TOOL_CALL",
        "data": {
            "name": "view_file",
            "arguments": {"AbsolutePath": "中文.py"},
            "call_id": guard._native_call_id(native, state),
            "expected_permission_tier_revision": 1,
        },
        "context": {"harness": "antigravity-native"},
    }
    from omnigent.server.routes._sessions.common import _EVALUATE_HOOK_ELICITATION_ID_RE

    assert _EVALUATE_HOOK_ELICITATION_ID_RE.fullmatch(request["_omnigent_elicitation_id"])


def test_post_tool_use_releases_only_matching_native_call(tmp_path, state, monkeypatch, capsys):
    tier = tmp_path / "permission_tier"
    tier.write_text("auto\n")
    write_host_permission_state(tmp_path / HOST_PERMISSION_STATE_FILE, state)
    native = {
        **payload("view_file", {"AbsolutePath": "a.py"}),
        "conversationId": "cascade",
        "stepIdx": 4,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(native)))
    monkeypatch.setattr(guard, "_relay_config", lambda *_: {})
    events = []
    monkeypatch.setattr(
        guard, "_relay_post", lambda _r, _p, event: events.append(event) or {"finished": True}
    )
    assert guard.main(["--tier-file", str(tier), "--finish"]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert events == [
        {
            "type": "permission_tier_finish",
            "call_id": guard._native_call_id(native, state),
            "expected_revision": state.revision,
        }
    ]


@pytest.mark.parametrize("changed", [False, True])
def test_full_native_entry_keeps_independent_policy_without_fixed_rules(
    tmp_path, state, monkeypatch, capsys, changed
):
    from dataclasses import replace

    state = replace(state, tier="full")
    tier = tmp_path / "permission_tier"
    tier.write_text("full\n")
    write_host_permission_state(tmp_path / HOST_PERMISSION_STATE_FILE, state)
    native = {
        **payload("view_file", {"AbsolutePath": "a.py"}),
        "conversationId": "cascade",
        "stepIdx": 4,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(native)))
    monkeypatch.setattr(guard, "_confirmed_relay", lambda *_: {})

    def no_fixed_rules():
        raise AssertionError("full must not load tier rules")

    monkeypatch.setattr(guard, "load_rules", no_fixed_rules)
    calls = []

    def deny(_relay, path, event):
        calls.append((path, event))
        if changed:
            write_host_permission_state(
                tmp_path / HOST_PERMISSION_STATE_FILE, replace(state, status="blocked")
            )
            return {"result": "POLICY_ACTION_ALLOW"}
        return {"result": "POLICY_ACTION_DENY", "reason": "independent deny"}

    monkeypatch.setattr(guard, "_relay_post", deny)
    assert guard.main(["--tier-file", str(tier)]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["decision"] == "deny"
    if changed:
        assert response["reason"].startswith("Permission check unavailable:")
    else:
        assert response["reason"] == "independent deny"
    assert len(calls) == 1
    assert calls[0][0] == "/policies/evaluate"
