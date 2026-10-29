"""Native proof is independent of verified extension provenance and tool names."""

import hashlib
import json
import os
import re
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
    _native_digest,
)
from tests.harnesses.test_permission_tiers import decision, posix_host

ACTION_CASES = json.loads(Path(__file__).with_name("pi_native_action_cases.json").read_text())


def assert_package_action_domain(package_root: Path) -> None:
    source = (package_root / "src/shared/types.js").read_text()
    declaration = re.search(r"export const SUBAGENT_ACTIONS = (\[[^;]+\]);", source)
    assert declaration, "Package action declaration needs review"
    assert set(json.loads(declaration[1])) == {
        row["action"] for row in ACTION_CASES if row["public"]
    }, "Package public action domain changed"
    dispatcher = (package_root / "src/runs/foreground/subagent-executor.js").read_text()
    literals = set(re.findall(r'(?<![\w.])action\s*===?\s*"([^"\n]+)"', dispatcher))
    assert literals <= {row["action"] for row in ACTION_CASES}, "Unclassified dispatcher action"


@pytest.mark.parametrize("surface", ["public", "dispatcher"])
def test_package_action_domain_guard_rejects_unclassified_action(tmp_path, surface):
    declarations = tmp_path / "src/shared/types.js"
    dispatcher = tmp_path / "src/runs/foreground/subagent-executor.js"
    declarations.parent.mkdir(parents=True)
    dispatcher.parent.mkdir(parents=True)
    actions = [row["action"] for row in ACTION_CASES if row["public"]]
    declarations.write_text("export const SUBAGENT_ACTIONS = " + json.dumps(actions) + ";")
    dispatcher.write_text('if (action === "append-step") {}')
    assert_package_action_domain(tmp_path)
    if surface == "public":
        declarations.write_text(
            "export const SUBAGENT_ACTIONS = " + json.dumps([*actions, "new-launch"]) + ";"
        )
    else:
        dispatcher.write_text('if (action === "new-launch") {}')
    with pytest.raises(AssertionError):
        assert_package_action_domain(tmp_path)


def test_native_canonical_digest_keeps_authorization_lease_serialization():
    from omnigent.server.permission_tiers import BoundToolCall

    arguments = {"translations": {"10": "十", "2": "二"}, "value": 1.0}
    binding = BoundToolCall.bind(
        session_id="fixture",
        host_id="fixture",
        call_id="fixture",
        name="read",
        arguments=arguments,
        tier_revision=3,
    )
    lease_json = b'{"translations":{"10":"\\u5341","2":"\\u4e8c"},"value":1.0}'
    assert binding.arguments_digest == hashlib.sha256(lease_json).hexdigest()
    assert _native_digest(arguments) != binding.arguments_digest


def test_native_support_node_consumers(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for native support consumer tests")
    script = (
        Path(__file__).resolve().parents[2]
        / "omnigent/resources/pi_native/native_support.test.cjs"
    )
    result = subprocess.run(
        [node, "--test", str(script), str(script.with_name("scoped_client.test.cjs"))],
        capture_output=True,
        text=True,
        timeout=45,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "OMNIGENT_NATIVE_DIGEST_CASES": json.dumps(
                [
                    {"name": name, "input": value, "expected": _native_digest(value)}
                    for name, value in [
                        ("integer-like keys", {"translations": {"10": "十", "2": "二"}}),
                        (
                            "code-point key order",
                            {"\ue000": "private-use", "😀": "astral", "中文": "内容"},
                        ),
                        (
                            "nested integer keys",
                            {"items": [{"20": True, "3": False}, None, "\n\t"]},
                        ),
                        ("integral float", {"number": 1.0, "zero": -0.0}),
                        ("fraction and exponent", {"decimal": 0.1, "small": 1e-7, "large": 1e20}),
                        ("exact subnormal", {"number": float.fromhex("0x0.0000000000001p-1022")}),
                        ("large integer-like name", {"4294967295": [], "0": {}, "01": None}),
                    ]
                ],
                ensure_ascii=False,
            ),
            "OMNIGENT_NATIVE_ACTION_CASES": json.dumps(ACTION_CASES),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_launch_gate_removal_is_detected_by_action_admission_contract(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for native support consumer tests")
    workspace = Path(__file__).resolve().parents[2]
    resources = workspace / "omnigent/resources/pi_native"
    copied = tmp_path / "omnigent/resources/pi_native"
    copied.mkdir(parents=True)
    for source in resources.iterdir():
        if source.is_file() and source.suffix in (".cjs", ".js", ".mjs"):
            shutil.copyfile(source, copied / source.name)
    rules = tmp_path / "omnigent/permission_tiers/native_support_rules.json"
    rules.parent.mkdir(parents=True)
    shutil.copyfile(workspace / "omnigent/permission_tiers/native_support_rules.json", rules)
    target = copied / "native_support.cjs"
    source = target.read_text()
    start = source.index('    if(event.toolName==="subagent" && "action" in normalized) {')
    end = source.index("    const unsupported=outsideAction", start)
    target.write_text(source[:start] + source[end:])
    result = subprocess.run(
        [
            node,
            "--test",
            "--test-name-pattern",
            "the whole package action domain",
            str(copied / "native_support.test.cjs"),
        ],
        capture_output=True,
        text=True,
        timeout=45,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "OMNIGENT_NATIVE_ACTION_CASES": json.dumps(ACTION_CASES),
        },
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        "Removing only the action gate must violate explicit launch admission"
    )
    assert "ERR_ASSERTION" in output and "effective launch proof" in output, output


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


@pytest.mark.parametrize(
    "sharing",
    [
        {"options": {"share": False}},
        {"share": False},
        {"share": False, "options": {"share": False}},
    ],
)
def test_false_sharing_normalization_reaches_python_tier_without_rebinding(sharing):
    arguments = {"agent": "reader", "task": "read", "async": False, **sharing}
    host = _host("subagent", arguments, action="delegate", native_tools={"subagent": ""})
    for tier in ("auto", "ask", "accept-edits", "plan"):
        result = decision(tier, "subagent", arguments, host)
        assert result.action == Verdict.ALLOW
        assert result.rule_id != "native-proof"
    changed = {**arguments, "task": "changed"}
    assert decision("auto", "subagent", changed, host).rule_id == "native-proof"
    omitted = {key: value for key, value in arguments.items() if key not in ("share", "options")}
    assert decision("auto", "subagent", omitted, host).rule_id == "native-proof"
    assert host.native_binding.arguments_digest == _native_digest(arguments)


@pytest.mark.parametrize(
    "sharing",
    [
        {"options": {"share": True}},
        {"share": False, "options": {"share": True}},
        {"options": {"share": None}},
    ],
)
def test_native_sharing_normalization_rejects_true_conflicts_and_malformed_values(sharing):
    arguments = {"agent": "reader", "task": "read", "async": False, **sharing}
    host = _host("subagent", arguments, action="delegate", native_tools={"subagent": ""})
    assert decision("auto", "subagent", arguments, host).rule_id == "native-proof"


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
@pytest.mark.parametrize("kind", ["external_cli", "remote", "gist", "schedule", "management"])
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
