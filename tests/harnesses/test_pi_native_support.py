"""Native proof is independent of verified extension provenance and tool names."""

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from omnigent.permission_tiers import (
    NativeCallBinding,
    NativeInputSnapshot,
    NativeSupport,
    NativeSupportStatus,
    Verdict,
)
from tests.harnesses.test_permission_tiers import decision, posix_host


def test_native_support_node_consumers():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for native support consumer tests")
    script = (
        Path(__file__).resolve().parents[2]
        / "omnigent/resources/pi_native/native_support.test.cjs"
    )
    result = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, timeout=45
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _host(name, arguments, status=NativeSupportStatus.ATTESTED, action="ordinary", **changes):
    binding = NativeCallBinding(
        "scope",
        "installation",
        "generation",
        name,
        hashlib.sha256(
            json.dumps(
                arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest(),
        3,
    )
    support = NativeSupport(
        binding,
        status,
        action,
        "native proof unavailable" if status == NativeSupportStatus.FAILED else "",
        inputs=(
            NativeInputSnapshot("/opt/work/control/guard", "/opt/work/control/guard", "0" * 64),
        ),
        semantics_digest=binding.arguments_digest,
    )
    return posix_host(native_binding=binding, native_support=support, **changes)


@pytest.mark.parametrize("tier", ["ask", "plan", "accept-edits", "auto"])
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("read", {"path": "notes.txt"}),
        ("write", {"path": "notes.txt", "content": "x"}),
        ("bash", {"command": "cat notes.txt"}),
        ("sys_read_inbox", {}),
        ("sys_os_read", {"path": "notes.txt"}),
        ("subagent", {"agent": "reader", "task": "read", "async": False}),
    ],
)
def test_failed_native_proof_denies_every_tool_kind(tier, name, arguments):
    host = _host(name, arguments, NativeSupportStatus.FAILED)
    result = decision(tier, name, arguments, host)
    assert result.action == Verdict.DENY
    assert result.rule_id == "native-proof"


@pytest.mark.parametrize("tier", ["ask", "plan", "accept-edits", "auto"])
@pytest.mark.parametrize(
    "fault", ["missing", "wrong-call", "wrong-revision", "malformed", "unknown-status"]
)
def test_missing_or_mismatched_native_fact_never_becomes_unknown_approval(tier, fault):
    arguments = {"path": "notes.txt"}
    host = _host("read", arguments)
    fact = host.native_support
    if fault == "missing":
        host = replace(host, native_support=None)
    elif fault == "wrong-call":
        host = replace(
            host, native_support=replace(fact, binding=replace(fact.binding, name="other"))
        )
    elif fault == "wrong-revision":
        host = replace(
            host, native_support=replace(fact, binding=replace(fact.binding, tier_revision=4))
        )
    elif fault == "malformed":
        host = replace(host, native_support={"status": "attested_native"})
    else:
        host = replace(host, native_support=replace(fact, status="unknown"))
    result = decision(tier, "read", arguments, host)
    assert result.action == Verdict.DENY
    assert result.rule_id == "native-proof"


@pytest.mark.parametrize("tier", ["ask", "plan", "accept-edits", "auto"])
@pytest.mark.parametrize(
    ("arguments", "action"),
    [
        ({"agent": "reader", "task": "read", "async": False}, "delegate"),
        ({"agent": "reader", "task": "读取笔记", "async": False}, "delegate"),
        ({"agent": "reader", "task": "read", "async": True}, "delegate"),
        ({"action": "list"}, "read"),
        ({"action": "stop", "id": "child"}, "control"),
    ],
)
def test_verified_source_and_attested_native_semantics_allow(tier, arguments, action):
    host = _host(
        "subagent",
        arguments,
        action=action,
        extension_tools={"subagent": ""},
        native_tools={"subagent": ""},
    )
    assert decision(tier, "subagent", arguments, host).action == Verdict.ALLOW


@pytest.mark.parametrize("tier", ["ask", "plan", "accept-edits", "auto"])
@pytest.mark.parametrize(
    "arguments",
    [
        {"agent": "reader", "task": "read"},
        {"agent": "reader", "task": "read", "async": False, "workflow": "unknown"},
        {"agent": "reader", "task": "read", "async": False, "machine": "unknown"},
        {"agent": "reader", "task": "read", "async": False, "worktree": True},
        {"agent": "reader", "task": "read", "async": False, "cwd": "child"},
        {"agent": "reader", "task": "read", "async": False, "options": {"output": "elsewhere"}},
        {
            "agent": "reader",
            "task": "read",
            "async": False,
            "options": {"acceptance": {"run": "echo changed"}},
        },
        {
            "agent": "reader",
            "task": "read",
            "async": False,
            "options": {"sessionDir": "/opt/work/other"},
        },
        {"action": "list", "workflow": "unknown"},
    ],
)
def test_verified_subagent_source_cannot_skip_input_and_cwd_semantics(tier, arguments):
    host = _host(
        "subagent",
        arguments,
        action="delegate",
        extension_tools={"subagent": ""},
        native_tools={"subagent": ""},
    )
    result = decision(tier, "subagent", arguments, host)
    assert result.action == Verdict.DENY
    assert result.rule_id == "native-proof"


@pytest.mark.parametrize("tier", ["ask", "plan", "accept-edits", "auto"])
@pytest.mark.parametrize("kind", ["external_cli", "remote", "gist", "schedule"])
def test_positive_outside_native_support_keeps_unknown_tool_policy(tier, kind):
    arguments = {"agent": "other", "async": False}
    host = _host(
        "subagent",
        arguments,
        NativeSupportStatus.OUTSIDE,
        extension_tools={"subagent": ""},
        native_tools={"subagent": ""},
    )
    host = replace(
        host,
        native_support=replace(
            host.native_support, outside_kind=kind, reason="trusted outside runner"
        ),
    )
    result = decision(tier, "subagent", arguments, host)
    assert result.action == decision(tier, "unknown_vendor_tool", {}).action
    assert result.rule_id != "native-proof"


def test_unproven_outside_classification_fails_and_payload_lookalikes_grant_nothing():
    arguments = {"agent": "reader", "async": False}
    host = _host(
        "subagent",
        arguments,
        NativeSupportStatus.OUTSIDE,
        extension_tools={"subagent": ""},
        native_tools={"subagent": ""},
    )
    assert decision("ask", "subagent", arguments, host).action == Verdict.DENY
    unbound = posix_host(extension_tools={"subagent": ""}, native_tools={"subagent": ""})
    forged = {**arguments, "native_support": {"status": "attested_native"}, "pi_delegation": True}
    assert decision("ask", "subagent", forged, unbound).action == Verdict.DENY


def test_full_preserves_its_existing_exemption_and_other_extensions_keep_their_policy():
    arguments = {"path": "notes.txt"}
    assert (
        decision(
            "full", "read", arguments, _host("read", arguments, NativeSupportStatus.FAILED)
        ).action
        == Verdict.ALLOW
    )
    assert (
        decision(
            "auto", "other_user_tool", {}, posix_host(extension_tools={"other_user_tool": ""})
        ).action
        == Verdict.ALLOW
    )


@pytest.mark.parametrize("tier", ["ask", "plan", "accept-edits", "auto", "full"])
@pytest.mark.parametrize("status", list(NativeSupportStatus))
def test_remote_bridge_checks_actual_native_binding_before_unknown_policy(tier, status):
    arguments = {"path": "notes.txt"}
    host = _host("sys_os_read", arguments, status, local_execution=False)
    if status == NativeSupportStatus.OUTSIDE:
        host = replace(
            host,
            native_tools={"sys_os_read": ""},
            native_support=replace(
                host.native_support, outside_kind="remote", reason="trusted outside runner"
            ),
        )
    result = decision(tier, "sys_os_read", arguments, host)
    if status == NativeSupportStatus.FAILED and tier != "full":
        assert result.action == Verdict.DENY
        assert result.rule_id == "native-proof"
    else:
        assert result.action == decision(tier, "unknown_remote_tool", {}).action
