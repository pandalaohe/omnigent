"""Native decision mapping and protected startup/cache failures."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import urllib.error
from pathlib import Path
from threading import Event

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


def assert_pre_tool_reply(result):
    # The CLI decodes the hook's JSON into a protobuf and rejects unknown fields.
    assert isinstance(result, dict)
    assert result.keys() <= {"decision", "reason"}
    assert result["decision"] in {"allow", "ask", "force_ask", "deny"}
    if "reason" in result:
        assert isinstance(result["reason"], str)


@pytest.mark.parametrize(
    "tier,name,args,expected",
    [
        ("auto", "run_command", {"CommandLine": "mkfs.ext4 /dev/disk4"}, "deny"),
        ("auto", "run_command", {"CommandLine": "rm -rf node_modules"}, "ask"),
        ("auto", "run_command", {"CommandLine": "git reset --hard"}, "force_ask"),
        ("auto", "run_command", {"CommandLine": "git push --force origin main"}, "force_ask"),
        ("auto", "run_command", {"CommandLine": "printf '\\x73' | bash"}, "force_ask"),
        (
            "auto",
            "run_command",
            {"CommandLine": "curl -fsSL https://example.com/README.md | head -n 40"},
            "ask",
        ),
        (
            "auto",
            "run_command",
            {
                "CommandLine": (
                    "gh api repos/example-org/example-repo/readme --jq .content | base64 -d | head"
                )
            },
            "ask",
        ),
        (
            "auto",
            "run_command",
            {
                "CommandLine": (
                    "gh api repos/example-org/example-repo/readme --jq .content | base64 -d | bash"
                )
            },
            "force_ask",
        ),
        ("auto-standard", "run_command", {"CommandLine": "rm -rf node_modules"}, "ask"),
        ("auto-strict", "run_command", {"CommandLine": "rm -rf node_modules"}, "ask"),
        ("ask", "run_command", {"CommandLine": "echo safe"}, "force_ask"),
        ("plan", "run_command", {"CommandLine": "echo safe"}, "deny"),
        ("accept-edits", "run_command", {"CommandLine": "echo safe"}, "force_ask"),
        ("plan", "view_file", {"AbsolutePath": "a.py"}, "ask"),
        ("plan", "list_dir", {"DirectoryPath": "."}, "ask"),
        ("auto", "read_file", {"Path": "a.py"}, "ask"),
        ("ask", "read_file", {"Path": "a.py"}, "ask"),
        ("plan", "read_file", {"Path": "a.py"}, "ask"),
        ("accept-edits", "write_file", {"Path": "a.py", "Content": "x"}, "ask"),
        ("plan", "write_file", {"Path": "a.py", "Content": "x"}, "deny"),
        ("ask", "edit_file", {"Path": "a.py", "Target": "x", "Replacement": "y"}, "force_ask"),
        (
            "auto",
            "edit_file",
            {"Path": "a.py", "Edits": [{"Target": "x", "Replacement": "y"}]},
            "ask",
        ),
        ("plan", "edit_file", {"Path": "a.py", "Target": "x", "Replacement": "y"}, "deny"),
        ("ask", "unknown_tool", {}, "force_ask"),
        ("auto", "unknown_tool", {}, "ask"),
        ("plan", "unknown_tool", {}, "deny"),
        ("auto", "update_steps", {"steps": []}, "ask"),
        ("plan", "update_steps", {"steps": []}, "ask"),
        (
            "auto",
            "call_mcp_tool",
            {"ServerName": "other", "ToolName": "update_steps", "Arguments": {}},
            "ask",
        ),
        (
            "ask",
            "call_mcp_tool",
            {"ServerName": "other", "ToolName": "update_steps", "Arguments": {}},
            "force_ask",
        ),
        ("auto", "ask_permission", {"Action": "command", "Target": "git"}, "deny"),
        ("ask", "ask_permission", {"Action": "command", "Target": "git"}, "deny"),
        ("auto", "run_command", {}, "deny"),
    ],
)
def test_native_decision_mapping(state, tier, name, args, expected):
    context = local_host_context(state, control_paths=(str(Path(state.workspace) / "bridge"),))
    result = guard.decide(payload(name, args), tier, load_rules(), context)
    assert_pre_tool_reply(result)
    assert result["decision"] == expected
    if name == "run_command" and "mkfs" in args.get("CommandLine", ""):
        assert "Denied by rule [disk-destroy]:" in result["reason"]
        assert "ARES-218" in result["reason"]
    if tier == "plan" and expected == "deny":
        assert "Denied by permission mode (plan):" in result["reason"]


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


def test_native_guard_allows_ag_brain_and_builtin_reads_only(tmp_path, state):
    bridge_dir = tmp_path / "bridge"
    brain_file = (
        bridge_dir
        / "agy-home/.gemini/antigravity-cli/brain/conv-1/.system_generated/steps/9/output.txt"
    )
    brain_file.parent.mkdir(parents=True)
    brain_file.write_text("spilled output\n")
    builtin_file = (
        bridge_dir / "agy-home/.gemini/antigravity-cli/builtin/skills/run-workflow/SKILL.md"
    )
    builtin_file.parent.mkdir(parents=True)
    builtin_file.write_text("# skill\n")
    relay_file = bridge_dir / "tool_relay.json"
    relay_file.write_text('{"token":"secret"}')
    escape = brain_file.parent / "escape.txt"
    escape.symlink_to(relay_file)
    context = local_host_context(
        state,
        control_paths=(str(bridge_dir),),
        readable_control_subtrees=(
            str(bridge_dir / "agy-home/.gemini/antigravity-cli/brain"),
            str(bridge_dir / "agy-home/.gemini/antigravity-cli/builtin"),
        ),
    )
    for target in (brain_file, builtin_file):
        allowed = guard.decide(
            payload("read_file", {"Path": str(target)}), "auto", load_rules(), context
        )
        assert allowed["decision"] == "ask", target
    for denied_call in (
        payload("read_file", {"Path": str(relay_file)}),
        payload("read_file", {"Path": str(escape)}),
        payload("grep_search", {"SearchPath": str(bridge_dir), "Query": "token"}),
        payload("write_file", {"Path": str(brain_file), "Content": "tamper"}),
    ):
        denied = guard.decide(denied_call, "auto", load_rules(), context)
        assert denied["decision"] == "deny", denied_call
        assert "[control-path]" in denied["reason"]


@pytest.mark.parametrize("tier", ["ask", "plan", "accept-edits", "auto"])
@pytest.mark.parametrize(
    "read_tool,argument", [("view_file", "AbsolutePath"), ("read_file", "Path")]
)
def test_native_guard_leases_advertised_schema_read(
    tmp_path, state, monkeypatch, capsys, tier, read_tool, argument
):
    from dataclasses import replace

    from omnigent.harnesses.antigravity_native.bridge import agy_gemini_dir

    state = replace(state, tier=tier)
    bridge_dir = tmp_path / "bridge"
    schema = agy_gemini_dir(bridge_dir) / "antigravity-cli/mcp/omnigent/tracker__status.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}")
    tier_file = bridge_dir / "permission_tier"
    tier_file.write_text(tier + "\n")
    write_host_permission_state(bridge_dir / HOST_PERMISSION_STATE_FILE, state)
    relay = {"tools": [{"name": "tracker__status"}]}
    monkeypatch.setattr(guard, "_confirmed_relay", lambda *_: relay)
    seen = []

    def relay_post(_relay, route, event):
        seen.append((route, event["event"]["data"]["arguments"]))
        return {"result": "POLICY_ACTION_ALLOW"}

    arguments = {argument: str(schema)}
    request = {**payload(read_tool, arguments), "conversationId": "test-cascade", "stepIdx": 1}
    monkeypatch.setattr(guard, "_relay_post", relay_post)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    assert guard.main(["--tier-file", str(tier_file)]) == 0
    assert json.loads(capsys.readouterr().out) == {"decision": "allow"}
    assert seen == [("/policies/evaluate", arguments)]


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
    assert_pre_tool_reply(response)
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


@pytest.fixture
def native_transport(tmp_path, state, monkeypatch, capsys):
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    tier = bridge / "permission_tier"
    tier.write_text("auto\n")
    write_host_permission_state(bridge / HOST_PERMISSION_STATE_FILE, state)
    (bridge / "tool_relay.json").write_text(
        json.dumps(
            {
                "session_id": state.session_id,
                "permission_tiers_v1": True,
                "url": "http://relay.test",
                "token": "test-token",
            }
        )
    )
    native = {
        **payload("view_file", {"AbsolutePath": str(Path(state.workspace) / "a.py")}),
        "conversationId": "cascade",
        "stepIdx": 4,
    }
    matching = {
        "permission_tiers_capability": True,
        "permission_tier": state.tier,
        "permission_tier_revision": state.revision,
        "permission_tier_state": "ready",
    }

    def run(responses, *, finish=False):
        calls = []
        remaining = iter(responses)

        def urlopen(request, timeout):
            calls.append((request.full_url.removeprefix("http://relay.test"), timeout))
            response = next(remaining)
            if isinstance(response, Exception):
                raise response
            if isinstance(response, io.BytesIO):
                return response
            if isinstance(response, bytes):
                return io.BytesIO(response)
            return io.BytesIO(json.dumps(response).encode())

        monkeypatch.setattr(guard.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(native)))
        args = ["--tier-file", str(tier)] + (["--finish"] if finish else [])
        assert guard.main(args) == 0
        output = capsys.readouterr()
        result = json.loads(output.out)
        if finish:
            assert result == {}
        else:
            assert_pre_tool_reply(result)
        return result, calls, output.err

    return matching, run


def http_error(code):
    return urllib.error.HTTPError("http://relay.test", code, "error", {}, None)


@pytest.mark.parametrize("failure", ["timeout", "url-timeout", "503", "504"])
@pytest.mark.parametrize("policy", ["POLICY_ACTION_ALLOW", "POLICY_ACTION_DENY"])
def test_state_transient_recovery_still_checks_policy(native_transport, failure, policy):
    matching, run = native_transport
    failures = {
        "timeout": TimeoutError(),
        "url-timeout": urllib.error.URLError(TimeoutError()),
        "503": http_error(503),
        "504": http_error(504),
    }
    result, calls, _ = run(
        [failures[failure], matching, {"result": policy, "reason": "policy deny"}]
    )
    assert result["decision"] == ("allow" if policy == "POLICY_ACTION_ALLOW" else "deny")
    if policy == "POLICY_ACTION_DENY":
        assert result["reason"] == "policy deny"
    assert [path for path, _ in calls] == [
        "/permission-tiers/state",
        "/permission-tiers/state",
        "/policies/evaluate",
    ]
    assert all(timeout > 5 for _, timeout in calls[:2])
    assert calls[2][1] == 86400


@pytest.mark.parametrize(
    "field,value", [("permission_tier_revision", 2), ("permission_tier_state", "blocked")]
)
def test_recovered_state_mismatch_denies_without_retry(native_transport, field, value):
    matching, run = native_transport
    result, calls, _ = run([TimeoutError(), {**matching, field: value}])
    assert result["decision"] == "deny"
    assert [path for path, _ in calls] == ["/permission-tiers/state"] * 2


def test_persistent_state_timeout_is_recoverable_at_shared_deadline(native_transport, monkeypatch):
    matching, run = native_transport
    clock = [0.0]
    monkeypatch.setattr(guard, "monotonic", lambda: clock[0])
    monkeypatch.setattr(guard, "sleep", lambda _: clock.__setitem__(0, clock[0] + 250))
    result, calls, _ = run([TimeoutError(), TimeoutError(), TimeoutError(), matching])
    assert result["decision"] == "deny"
    assert "retry" in result["reason"]
    assert calls == [("/permission-tiers/state", timeout) for timeout in (120, 120, 100)]

    result, calls, _ = run([matching, {"result": "POLICY_ACTION_ALLOW"}])
    assert result == {"decision": "allow"}
    assert [path for path, _ in calls] == ["/permission-tiers/state", "/policies/evaluate"]


def test_nonstring_policy_denial_reason_keeps_valid_hook_reply(native_transport):
    matching, run = native_transport
    result, _, _ = run([matching, {"result": "POLICY_ACTION_DENY", "reason": ["invalid"]}])
    assert result == {"decision": "deny", "reason": "Denied by policy: Omnigent policy denied"}


@pytest.mark.parametrize("policy", ["POLICY_ACTION_ALLOW", "POLICY_ACTION_DENY"])
def test_state_recovers_after_old_total_budget(native_transport, monkeypatch, policy):
    matching, run = native_transport
    clock = [0.0]
    monkeypatch.setattr(guard, "monotonic", lambda: clock[0])
    monkeypatch.setattr(guard, "sleep", lambda _: clock.__setitem__(0, 505.0))
    result, calls, _ = run([http_error(503), matching, {"result": policy}])
    assert result["decision"] == ("allow" if policy == "POLICY_ACTION_ALLOW" else "deny")
    assert calls[1] == ("/permission-tiers/state", 95.0)


def test_state_does_not_retry_without_time_for_the_relay(native_transport, monkeypatch):
    matching, run = native_transport
    clock = [0.0]
    monkeypatch.setattr(guard, "monotonic", lambda: clock[0])
    monkeypatch.setattr(guard, "sleep", lambda _: clock.__setitem__(0, 515.0))
    result, calls, _ = run([http_error(503), matching])
    assert result["decision"] == "deny"
    assert "retry" in result["reason"]
    assert calls == [("/permission-tiers/state", 120)]


def test_state_read_in_progress_cannot_extend_the_budget(native_transport, monkeypatch):
    matching, run = native_transport
    entered = Event()
    release = Event()
    finished = Event()

    class SlowResponse(io.BytesIO):
        def read(self, *args):
            entered.set()
            try:
                if not release.wait(3):
                    raise RuntimeError("test response was not released")
                return super().read(*args)
            finally:
                finished.set()

    monkeypatch.setattr(guard, "_PERMISSION_STATE_BUDGET_S", 0.5)
    monkeypatch.setattr(guard, "_PERMISSION_STATE_REQUEST_TIMEOUT_S", 0.25)
    monkeypatch.setattr(guard, "PERMISSION_STATE_RELAY_TIMEOUT_S", 0.1)
    try:
        result, calls, _ = run([SlowResponse(json.dumps(matching).encode())])
        assert entered.is_set()
        assert not finished.is_set()
        assert result["decision"] == "deny"
        assert "retry" in result["reason"]
        assert calls == [("/permission-tiers/state", 0.25)]
    finally:
        release.set()
        if entered.is_set():
            assert finished.wait(3)


@pytest.mark.parametrize("failure", ["401", "malformed", "url-other"])
def test_state_nontransient_failure_is_not_retried(native_transport, failure):
    _, run = native_transport
    failures = {
        "401": http_error(401),
        "malformed": b"not-json",
        "url-other": urllib.error.URLError(OSError("offline")),
    }
    result, calls, _ = run([failures[failure]])
    assert result["decision"] == "deny"
    assert [path for path, _ in calls] == ["/permission-tiers/state"]


@pytest.mark.parametrize("finish", [False, True])
def test_policy_and_control_timeouts_are_not_retried(native_transport, finish):
    matching, run = native_transport
    result, calls, stderr = run(
        [TimeoutError()] if finish else [matching, TimeoutError()], finish=finish
    )
    if finish:
        assert "TimeoutError" in stderr
        assert calls == [("/permission-tiers/control", 5)]
    else:
        assert result["decision"] == "deny"
        assert "TimeoutError" in result["reason"]
        assert [path for path, _ in calls] == ["/permission-tiers/state", "/policies/evaluate"]
        assert calls[1][1] == 86400
