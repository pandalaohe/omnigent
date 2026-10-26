"""Public shared permission-tier decisions, without a harness process."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest

from omnigent.permission_tiers import (
    AGY_PERMISSION_TIER_LABEL_KEY,
    DEFAULT_PERMISSION_TIER,
    NON_FULL_TIERS,
    PERMISSION_TIERS,
    PERMISSION_TIERS_CAPABILITY,
    PI_PERMISSION_TIER_LABEL_KEY,
    RelaxedRules,
    ResolvedPath,
    TrustedHostContext,
    Verdict,
    adapt_tool_call,
    decide_tier,
    load_rules,
    normalize_permission_tier,
    plan_visible_tool_names,
    resolve_local_path,
)

RULES = load_rules()


def posix_host(**changes: object) -> TrustedHostContext:
    def resolve(path: str) -> ResolvedPath:
        return ResolvedPath(path, "directory" if path == "/opt/work/project" else "new-file")

    base = TrustedHostContext(
        host_id="test-host",
        platform="linux",
        cwd="/opt/work/project",
        workspace="/opt/work/project",
        home="/opt/home",
        shell="sh",
        control_paths=("/opt/work/control",),
        resolver=resolve,
    )
    return replace(base, **changes)


def decision(tier: str, name: str, args: dict, host: TrustedHostContext | None = None):
    return decide_tier(tier, adapt_tool_call(name, args, host or posix_host()), RULES)


def test_canonical_values_and_legacy_migration() -> None:
    assert PERMISSION_TIERS == ("ask", "plan", "accept-edits", "auto", "full")
    assert PERMISSION_TIERS[:-1] == NON_FULL_TIERS
    assert DEFAULT_PERMISSION_TIER == "auto"
    assert PERMISSION_TIERS_CAPABILITY == "permission_tiers_v1"
    assert PI_PERMISSION_TIER_LABEL_KEY == "omnigent.pi.permission_tier"
    assert AGY_PERMISSION_TIER_LABEL_KEY == "omnigent.antigravity_native.permission_tier"
    for old in ("auto-strict", "auto-standard", "auto-relaxed"):
        assert normalize_permission_tier(old, harness="antigravity-native", legacy=True) == "auto"
    assert normalize_permission_tier(None, harness="pi-native", legacy=True) == "ask"
    with pytest.raises(ValueError):
        normalize_permission_tier("auto-strict", harness="pi", legacy=False)
    with pytest.raises(ValueError):
        normalize_permission_tier(None, harness="pi", legacy=False)
    with pytest.raises(ValueError):
        normalize_permission_tier({}, harness="antigravity-native", legacy=True)


def test_read_plan_edit_and_single_call_ask() -> None:
    assert decision("ask", "read", {"path": "src/a.py"}).action == Verdict.ALLOW
    assert decision("plan", "write", {"path": "src/a.py", "content": "x"}).action == Verdict.DENY
    assert decision("plan", "bash", {"command": "git status"}).action == Verdict.DENY
    assert decision("plan", "third_party", {"readOnlyHint": True}).action == Verdict.DENY
    assert decision("plan", "ask_user", {"question": "Continue?"}).action == Verdict.ALLOW
    first = decision("ask", "write", {"path": "src/a.py", "content": "x"})
    second = decision("ask", "write", {"path": "src/a.py", "content": "x"})
    assert first.action == second.action == Verdict.ASK


def test_plan_visible_tools_only_include_registered_known_safe_names() -> None:
    names = (
        "read",
        "list_dir",
        "ask_user",
        "report_result",
        "sys_terminal_read",
        "read",
        "bash",
        "write",
        "third_party_read",
    )
    assert plan_visible_tool_names(names) == (
        "read",
        "list_dir",
        "ask_user",
        "report_result",
        "sys_terminal_read",
    )


@pytest.mark.parametrize(
    "name",
    [
        "sys_session_list",
        "sys_session_get_info",
        "sys_session_get_history",
        "sys_agent_list",
        "sys_agent_get",
        "sys_list_models",
        "sys_advise_models",
        "sys_policy_registry",
        "sys_instruction_layers_read",
        "sys_project_settings_read",
        "sys_scheduled_task_list",
        "sys_flow_list",
        "list_comments",
        "load_skill",
        "read_skill_file",
        "browser_snapshot",
        "browser_screenshot",
        "sys_terminal_read",
        "sys_terminal_list",
        "sys_child_cap",
    ],
)
def test_framework_reads_allow_every_tier_and_are_plan_visible(name: str) -> None:
    for tier in PERMISSION_TIERS:
        assert decision(tier, name, {}).action == Verdict.ALLOW
    assert plan_visible_tool_names((name, name)) == (name,)


@pytest.mark.parametrize(
    "name",
    [
        "sys_session_create",
        "sys_session_send",
        "sys_session_open",
        "sys_session_rename",
        "sys_session_archive",
        "sys_session_unarchive",
        "sys_session_close",
        "sys_cancel_task",
        "sys_cancel_async",
        "sys_flow_cancel",
        "update_comment",
        "sys_session_handover",
        "sys_timer_set",
        "sys_timer_cancel",
        "sys_instruction_layer_update",
        "sys_project_settings_update",
        "sys_add_policy",
        "sys_session_share",
        "sys_scheduled_task_create",
        "sys_scheduled_task_update",
        "sys_scheduled_task_delete",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "sys_terminal_close",
    ],
)
def test_framework_operations_and_settings_follow_approved_tiers(name: str) -> None:
    assert decision("ask", name, {}).action == Verdict.ASK
    assert decision("plan", name, {}).action == Verdict.DENY
    assert decision("accept-edits", name, {}).action == Verdict.ASK
    assert decision("auto", name, {}).action == Verdict.ALLOW
    assert decision("full", name, {}).action == Verdict.ALLOW
    assert plan_visible_tool_names((name,)) == ()


@pytest.mark.parametrize("args", [{"operation": "read"}, {"operation": "write", "todoList": []}])
def test_session_task_list_control_allows_every_tier(args: dict) -> None:
    for tier in PERMISSION_TIERS:
        assert decision(tier, "manage_todo_list", args).action == Verdict.ALLOW
    assert plan_visible_tool_names(("manage_todo_list",)) == ("manage_todo_list",)


def test_panel_presentation_uses_operational_tiers_with_bound_target() -> None:
    args = {"path": "report.html"}
    intent = adapt_tool_call("open_in_panel", args, posix_host())
    assert intent.target == ResolvedPath("/opt/work/project/report.html", "new-file")
    assert decision("ask", "open_in_panel", args).action == Verdict.ASK
    assert decision("plan", "open_in_panel", args).action == Verdict.DENY
    assert decision("accept-edits", "open_in_panel", args).action == Verdict.ASK
    assert decision("auto", "open_in_panel", args).action == Verdict.ALLOW
    assert decision("full", "open_in_panel", args).action == Verdict.ALLOW
    assert plan_visible_tool_names(("open_in_panel",)) == ()


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"path": None},
        {"path": 1},
        {"path": False},
        {"path": ""},
        {"path": "\x00"},
        {"path": "../report.html"},
        {"path": "./report.html"},
        {"path": "reports//index.html"},
        {"path": "reports\\index.html"},
    ],
)
def test_panel_presentation_rejects_missing_or_malformed_paths(args: dict) -> None:
    for tier in PERMISSION_TIERS:
        assert decision(tier, "open_in_panel", args).action == Verdict.DENY


def test_panel_presentation_rejects_unconfirmed_target_paths() -> None:
    def unresolved(path: str) -> ResolvedPath:
        return ResolvedPath(path, "directory" if path == "/opt/work/project" else "unknown")

    for tier in NON_FULL_TIERS:
        assert (
            decision(
                tier, "open_in_panel", {"path": "report.html"}, posix_host(resolver=unresolved)
            ).action
            == Verdict.DENY
        )
    assert (
        decision(
            "full", "open_in_panel", {"path": "report.html"}, posix_host(resolver=unresolved)
        ).action
        == Verdict.ALLOW
    )

    def failed_resolution(path: str) -> ResolvedPath:
        raise OSError("Cannot resolve target")

    for tier in PERMISSION_TIERS:
        assert (
            decision(
                tier,
                "open_in_panel",
                {"path": "report.html"},
                posix_host(resolver=failed_resolution),
            ).action
            == Verdict.DENY
        )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("report.html", "/opt/work/project/report.html"),
        ("~/report.html", "/opt/work/project/~/report.html"),
    ],
)
def test_panel_relative_paths_use_workspace_and_preserve_literal_tilde(
    path: str, expected: str
) -> None:
    host = posix_host(cwd="/opt/work/control")
    intent = adapt_tool_call("open_in_panel", {"path": path}, host)
    assert intent.target == ResolvedPath(expected, "new-file")
    assert decide_tier("auto", intent, RULES).action == Verdict.ALLOW


@pytest.mark.parametrize("cap", [1, 20, "none"])
def test_child_cap_changes_use_operational_tiers(cap: object) -> None:
    args = {"cap": cap}
    assert decision("ask", "sys_child_cap", args).action == Verdict.ASK
    assert decision("plan", "sys_child_cap", args).action == Verdict.DENY
    assert decision("accept-edits", "sys_child_cap", args).action == Verdict.ASK
    assert decision("auto", "sys_child_cap", args).action == Verdict.ALLOW
    assert decision("full", "sys_child_cap", args).action == Verdict.ALLOW


@pytest.mark.parametrize("cap", [None, True, False, 0, -1, 1.5, "1", "NONE", "", [], {}])
def test_malformed_child_cap_never_falls_back_to_read(cap: object) -> None:
    for tier in PERMISSION_TIERS:
        assert decision(tier, "sys_child_cap", {"cap": cap}).action == Verdict.DENY


def test_checkin_read_and_writes_use_distinct_tiers() -> None:
    assert plan_visible_tool_names(("sys_child_checkin",)) == ("sys_child_checkin",)
    for tier in PERMISSION_TIERS:
        assert decision(tier, "sys_child_checkin", {"action": "read"}).action == Verdict.ALLOW
        assert (
            decision(tier, "sys_child_checkin", {"action": "read", "reset": True}).action
            == Verdict.DENY
        )
    for action in ("set", "follow_up_add", "follow_up_remove"):
        args: dict[str, object] = {"action": action}
        if action == "follow_up_add":
            args.update(text="Recheck", after_minutes=15)
        elif action == "follow_up_remove":
            args["id"] = "1234abcd"
        assert decision("ask", "sys_child_checkin", args).action == Verdict.ASK
        assert decision("plan", "sys_child_checkin", args).action == Verdict.DENY
        assert decision("accept-edits", "sys_child_checkin", args).action == Verdict.ASK
        assert decision("auto", "sys_child_checkin", args).action == Verdict.ALLOW
        assert decision("full", "sys_child_checkin", args).action == Verdict.ALLOW


@pytest.mark.parametrize("encode_arguments", [False, True], ids=["object", "json"])
@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("sys_session_list", {}, (Verdict.ALLOW,) * 5),
        ("browser_snapshot", {}, (Verdict.ALLOW,) * 5),
        (
            "sys_session_send",
            {"session_id": "child-session", "message": "Inspect the task"},
            (Verdict.ASK, Verdict.DENY, Verdict.ASK, Verdict.ALLOW, Verdict.ALLOW),
        ),
        (
            "sys_instruction_layer_update",
            {},
            (Verdict.ASK, Verdict.DENY, Verdict.ASK, Verdict.ALLOW, Verdict.ALLOW),
        ),
        ("sys_child_cap", {}, (Verdict.ALLOW,) * 5),
        (
            "sys_child_cap",
            {"cap": "none"},
            (Verdict.ASK, Verdict.DENY, Verdict.ASK, Verdict.ALLOW, Verdict.ALLOW),
        ),
        ("sys_child_cap", {"cap": None}, (Verdict.DENY,) * 5),
        ("manage_todo_list", {"operation": "read"}, (Verdict.ALLOW,) * 5),
        (
            "manage_todo_list",
            {"operation": "write", "todoList": []},
            (Verdict.ALLOW,) * 5,
        ),
        (
            "open_in_panel",
            {"path": "report.html"},
            (Verdict.ASK, Verdict.DENY, Verdict.ASK, Verdict.ALLOW, Verdict.ALLOW),
        ),
        ("open_in_panel", {}, (Verdict.DENY,) * 5),
        (
            "open_in_panel",
            {"path": "/opt/work/control/report.html"},
            (Verdict.DENY, Verdict.DENY, Verdict.DENY, Verdict.DENY, Verdict.ALLOW),
        ),
    ],
)
def test_antigravity_mcp_calls_classify_the_omnigent_target(
    name: str, args: dict, expected: tuple[Verdict, ...], encode_arguments: bool
) -> None:
    wrapper = {
        "ServerName": "omnigent",
        "ToolName": name,
        "Arguments": json.dumps(args) if encode_arguments else args,
    }
    for tier, action in zip(PERMISSION_TIERS, expected, strict=True):
        assert decision(tier, "call_mcp_tool", wrapper).action == action


@pytest.mark.parametrize(
    "name",
    [
        "sys_agent_download",
        "sys_call_async",
        "sys_flow_start",
        "sys_terminal_launch",
        "sys_future_read",
        "browser_future_read",
        "web_search",
        "third_party",
    ],
)
def test_deferred_and_unregistered_tools_keep_unknown_tiers(name: str) -> None:
    assert decision("ask", name, {}).action == Verdict.ASK
    assert decision("plan", name, {}).action == Verdict.DENY
    assert decision("accept-edits", name, {}).action == Verdict.ASK
    assert decision("auto", name, {}).action == Verdict.DENY
    assert decision("full", name, {}).action == Verdict.ALLOW
    assert plan_visible_tool_names((name,)) == ()
    assert (
        decision(
            "auto",
            "call_mcp_tool",
            {"ServerName": "omnigent", "ToolName": name, "Arguments": {}},
        ).action
        == Verdict.DENY
    )


def test_third_party_mcp_server_cannot_reuse_known_omnigent_names() -> None:
    for name in ("sys_session_list", "sys_session_send"):
        args = {"ServerName": "third-party", "ToolName": name, "Arguments": {}}
        assert decision("ask", "call_mcp_tool", args).action == Verdict.ASK
        assert decision("plan", "call_mcp_tool", args).action == Verdict.DENY
        assert decision("accept-edits", "call_mcp_tool", args).action == Verdict.ASK
        assert decision("auto", "call_mcp_tool", args).action == Verdict.DENY
        assert decision("full", "call_mcp_tool", args).action == Verdict.ALLOW


@pytest.mark.parametrize("name", ["sys_session_list", "sys_session_send"])
def test_framework_calls_preserve_fail_closed_host_and_rule_checks(name: str) -> None:
    for tier in NON_FULL_TIERS:
        for host in (posix_host(control_paths=()), posix_host(host_id="")):
            assert decision(tier, name, {}, host).action == Verdict.DENY
        intent = adapt_tool_call(name, {}, posix_host())
        assert decide_tier(tier, intent, RelaxedRules({})).action == Verdict.DENY


def test_auto_uses_only_fixed_relaxed_membership() -> None:
    ordinary = decision("auto", "bash", {"command": "rm -rf node_modules"})
    dangerous = decision("auto", "bash", {"command": "echo ok; mkfs.ext4 /dev/disk4"})
    assert ordinary.action == Verdict.ALLOW
    assert dangerous.action == Verdict.DENY
    assert dangerous.rule_id == "disk-destroy"
    assert "ARES" in dangerous.reason


def test_control_path_and_protected_path() -> None:
    assert decision("auto", "write", {"path": "src/a.py", "content": "x"}).action == Verdict.ALLOW
    for tier in NON_FULL_TIERS:
        assert (
            decision(tier, "write", {"path": "/opt/work/control/tier", "content": "full"}).rule_id
            == "control-path"
        )
    assert (
        decision("auto", "write", {"path": "/etc/hosts", "content": "x"}).rule_id
        == "protected-path"
    )
    assert decision("ask", "write", {"path": "/etc/hosts", "content": "x"}).action == Verdict.ASK
    assert decision("auto", "bash", {"command": "echo x > /etc/hosts"}).rule_id == "protected-path"
    assert decision("auto", "bash", {"command": "cat /etc/hosts"}).action == Verdict.ALLOW
    for tier in NON_FULL_TIERS:
        assert (
            decision(tier, "bash", {"command": "echo full > /opt/work/control/tier"}).rule_id
            == "control-path"
        )


@pytest.mark.parametrize(
    "command",
    [
        "cat /opt/work/control*/tier",
        "rm -rf /opt/work/control*",
        "cat /opt/work/control${suffix}/tier",
    ],
)
def test_control_path_expansions_remain_conservatively_blocked(command: str) -> None:
    for tier in NON_FULL_TIERS:
        assert decision(tier, "bash", {"command": command}).rule_id == "control-path"


@pytest.mark.parametrize(
    "command",
    [
        "env cp source /usr/local/probe",
        "sudo cp source /usr/local/probe",
        "X=1 cp source /usr/local/probe",
        "if true; then cp source /usr/local/probe; fi",
        "cd /usr/local; cp source probe",
        "X=1 cd /usr/local; cp source probe",
        "if true; then cd /usr/local; fi; cp source probe",
        "command cd /usr/local; cp source probe",
        "! cd /usr/local; cp source probe",
        "command -p cd /usr/local; cp source probe",
        "command -- cd /usr/local; cp source probe",
        "builtin cd /usr/local; cp source probe",
        "chmod 700 /usr${suffix}",
    ],
)
def test_unbound_mutation_structure_does_not_allow_protected_writes(command: str) -> None:
    assert decision("auto", "bash", {"command": command}).action == Verdict.DENY


def test_shell_continued_write_target_does_not_bind_as_workspace_file() -> None:
    command = "echo x > " + "\\\n" + "/usr/local/probe"
    assert decision("auto", "bash", {"command": command}).action == Verdict.DENY


@pytest.mark.parametrize(
    "command",
    [
        "cd build && echo x > report.log",
        "cd $D && echo x > out.log",
        "cd ~/project && echo x > out.log",
        "cd /opt/work/* && echo x > out.log",
        "cd - && echo x > out.log",
        "pushd /opt/work/project && echo x > out.log",
        "popd && echo x > out.log",
        "cd /a; echo x > o",
        "cd /a || echo x > o",
        "cd /a | tee out.log",
        "cd /a & echo x > o",
        "(cd /a && echo x > o)",
        "echo x > out.log && cd /a",
        "cd /opt/work/project && echo x > out.log && cd /a",
        "cd /opt/work/project && echo x > out.log; echo x > other.log",
        "cd /opt/work/project && tee $OUTPUT",
        "cd /opt/work/project && echo x > `printf /usr/local/r`",
        "cd /opt/work/project && printf x | tee `printf /usr/local/r`",
        "echo x > `printf /usr/local/r`",
        'echo x > "$OUTPUT"',
        "echo x > " + "\\\n" + "report.log",
    ],
)
def test_legal_unbound_shell_writes_retain_approval_and_full_tiers(command: str) -> None:
    assert decision("ask", "bash", {"command": command}).action == Verdict.ASK
    assert decision("plan", "bash", {"command": command}).action == Verdict.DENY
    assert decision("accept-edits", "bash", {"command": command}).action == Verdict.ASK
    result = decision("auto", "bash", {"command": command})
    assert result.action == Verdict.DENY
    assert result.reason.endswith(
        "write with absolute literal paths: no cd except a literal absolute "
        "cd /dir && prefix, no tee / redirect to $ targets"
    )
    assert decision("full", "bash", {"command": command}).action == Verdict.ALLOW

    def failed_resolution(path: str) -> ResolvedPath:
        raise OSError("Host path resolution failed")

    for tier in PERMISSION_TIERS:
        assert (
            decision(
                tier, "bash", {"command": command}, posix_host(resolver=failed_resolution)
            ).action
            == Verdict.DENY
        )


@pytest.mark.parametrize(
    "control",
    [
        "cd build && echo x > /opt/work/control/tier",
        "cd /opt/work && echo x > control/tier",
        "cd /opt/work && printf x | tee control/tier",
    ],
)
def test_unbound_shell_writes_preserve_common_control_and_ares_checks(control: str) -> None:
    for tier in NON_FULL_TIERS:
        assert decision(tier, "bash", {"command": control}).rule_id == "control-path"
    dangerous = "cd build && echo x > report.log; mkfs.ext4 /dev/disk4"
    assert decision("auto", "bash", {"command": dangerous}).rule_id == "disk-destroy"


@pytest.mark.parametrize(
    "command",
    [
        "cmd 2>/dev/null",
        "cmd >/dev/null",
        "cmd >>/dev/null",
        "cmd &>/dev/null",
        "cmd &>>/dev/null",
        "cmd 2>&1 >/dev/null",
        "cd /opt/work/project && grep x a.py 2>/dev/null",
        "cd /opt/work/project && echo x > /opt/work/project/out.log",
        "cd /opt/work/project && echo x > out.log",
        "cd /opt/work/project && pytest -q | tee /opt/work/project/tmp/log.txt",
        "cd /opt/work && cd /opt/work/project && echo x > out.log",
        ".venv/bin/python script.py 2>&1",
        ".venv/bin/python script.py > report.log 2>&1",
        "/opt/homebrew/bin/python script.py > report.log",
        "/usr/bin/python script.py > report.log",
        "cat /usr/share/example > local.txt",
        "cp /usr/share/example local.txt",
        "cat /usr/share/example | tee local.txt",
        "sed 's/a/b/' /usr/share/example > local.txt",
        '''/usr/bin/python -c "print('> /usr/local/report')"''',
        "printf '%s' '>' /usr/share/example",
        "echo /usr/share/example 2>&1",
        "cp local.txt /usr-local/report",
        "rm -rf ./build/bin",
        "echo x > /opt/work/control-other/report",
        "echo 'a'\"b\"",
        r"echo x > report\ with\ spaces.log",
        """/usr/bin/python -c "print('a>b')" 2>&1""",
        """env /usr/bin/python -c "print('> /usr/local/report')" 2>&1""",
        "if true; then cat /usr/share/example; fi",
        "cd /usr/local; cat /usr/share/example",
        "echo 'env cp /usr/local/probe' 2>&1",
        "chmod 700 /usr-other",
        "cp -- source local.txt",
        "cp -- -tlocal local.txt",
        "command -v cd; cp source probe",
        "command -V cd; cp source probe",
        "command -p -v cd; cp source probe",
        "command cd /usr/local; cat /usr/share/example",
    ],
)
def test_shell_reads_and_workspace_writes_do_not_mutate_protected_locations(command: str) -> None:
    assert decision("auto", "sys_os_shell", {"command": command}).action == Verdict.ALLOW


@pytest.mark.parametrize(
    ("command", "destination"),
    [
        ("echo x >/usr/local/report", "/usr/local/report"),
        ("echo x >> ~/.ssh/config", "/opt/home/.ssh/config"),
        ("cat local.txt 2>/usr/local/errors", "/usr/local/errors"),
        ("cp local.txt /usr/local/report", "/usr/local/report"),
        ("cp -T source /usr/local/probe", "/usr/local/probe"),
        ("cp -t/usr/local source", "/usr/local"),
        ("cp -- -tlocal /usr/local/probe", "/usr/local/probe"),
        ("mv /usr/local/report local.txt", "/usr/local/report"),
        ("rm /usr/local/report", "/usr/local/report"),
        ("sed -i '' 's/a/b/' /usr/local/report", "/usr/local/report"),
        ("tee /usr/local/report", "/usr/local/report"),
        ("tee -a /usr/local/report", "/usr/local/report"),
        ("cd /opt/work/project && echo x > /usr/local/r", "/usr/local/r"),
        ("cd /usr/local && echo x > r", "/usr/local/r"),
        ("cd /usr/local && pytest -q | tee -a r", "/usr/local/r"),
        ("env -C /usr/local tee report", "/usr/local"),
        ("env --chdir=/usr/local tee report", "/usr/local"),
        ("cd /opt/work/project && env -C /usr/local tee report", "/usr/local"),
        ("cd /opt/work/project && env --chdir=/usr/local tee report", "/usr/local"),
        ("pwsh -Command 'tee -FilePath:/usr/local/report'", "/usr/local/report"),
        ("chmod 600 ~/.ssh/config", "/opt/home/.ssh/config"),
        ("chmod 700 /usr*", "/usr*"),
        ("echo x > ../../../../usr/local/report", "/usr/local/report"),
        ("rm /usr/local/*", "/usr/local/*"),
        ("echo x > /usr/local/report=a", "/usr/local/report=a"),
        ("cat /usr/share/example;\necho x > /usr/local/report", "/usr/local/report"),
        ("echo x ># note\n/usr/local/probe", "/usr/local/probe"),
        ("echo x > /usr/local/probe # note", "/usr/local/probe"),
        ("echo ok\nrm /usr/local/probe", "/usr/local/probe"),
        ("echo ok # note\nrm /usr/local/probe", "/usr/local/probe"),
    ],
)
def test_shell_protected_write_denials_name_the_bound_destination(
    command: str, destination: str
) -> None:
    result = decision("auto", "sys_os_shell", {"command": command})
    assert result.action == Verdict.DENY
    assert result.rule_id == "protected-path"
    assert destination in result.reason


@pytest.mark.parametrize(
    ("command", "destinations"),
    [
        ("cmd 2>/dev/null", ()),
        ("cmd >/dev/null 2>&1", ()),
        ("cd /opt/work/project && grep x a.py 2>/dev/null", ()),
        ("echo x >/dev/null > out.log", ("/opt/work/project/out.log",)),
        ("echo x > /dev/null-other", ("/dev/null-other",)),
        ("cd /opt/work/other && echo x > out.log", ("/opt/work/other/out.log",)),
        (
            "cd '/opt/work/project with spaces' && echo x > out.log",
            ("/opt/work/project with spaces/out.log",),
        ),
        (
            "cd /opt/work/other && echo x > /opt/work/project/out.log",
            ("/opt/work/project/out.log",),
        ),
        (
            "cd /opt/work && cd /opt/work/project && echo x > out.log && echo y > next.log",
            ("/opt/work/project/out.log", "/opt/work/project/next.log"),
        ),
        (
            "cd /opt/work/other && pytest -q | tee -a --output-error=warn log.txt",
            ("/opt/work/other/log.txt",),
        ),
        (
            "cd /opt/work/other && pytest -q | tee -- /opt/work/project/tmp/log.txt",
            ("/opt/work/project/tmp/log.txt",),
        ),
        (
            "cd /opt/work/other && sh -c 'echo x > out.log'",
            ("/opt/work/other/out.log",),
        ),
    ],
)
def test_shell_write_destinations_bind_without_sink_commands_or_options(
    command: str, destinations: tuple[str, ...]
) -> None:
    intent = adapt_tool_call("sys_os_shell", {"command": command}, posix_host())
    assert intent.status == "ok"
    assert intent.command_write_paths == destinations
    assert decide_tier("auto", intent, RULES).action == Verdict.ALLOW


@pytest.mark.parametrize("command", ["cp", "mv", "install", "ln"])
@pytest.mark.parametrize("prefix", ["", "env "])
@pytest.mark.parametrize(
    "option",
    [
        "-t {path}",
        "-t{path}",
        "-vt {path}",
        "-vt{path}",
        "--target-directory {path}",
        "--target-directory={path}",
    ],
)
@pytest.mark.parametrize("path", ["/usr/local/probe", "~/.ssh/probe", "/opt/work/project/probe"])
def test_shell_target_directory_option_forms(
    command: str, prefix: str, option: str, path: str
) -> None:
    result = decision(
        "auto", "bash", {"command": f"{prefix}{command} {option.format(path=path)} source"}
    )
    assert result.action == (
        Verdict.ALLOW if path.startswith("/opt/work/project/") else Verdict.DENY
    )
    if result.action == Verdict.DENY:
        assert result.rule_id == "protected-path"


@pytest.mark.parametrize(
    "command",
    [
        "Copy-Item source -Destination:{path}",
        "Move-Item source -Destination:{path}",
        "Set-Content -Path:{path} x",
        "Add-Content -LiteralPath:{path} x",
        "Out-File -FilePath:{path} x",
        "Remove-Item -Path:{path}",
    ],
)
@pytest.mark.parametrize("path", ["/usr/local/probe", "~/.ssh/probe", "/opt/work/project/probe"])
@pytest.mark.parametrize("quote", ["", '"'])
def test_powershell_colon_path_option_forms(command: str, path: str, quote: str) -> None:
    result = decision(
        "auto",
        "bash",
        {"command": "pwsh -Command '" + command.format(path=quote + path + quote) + "'"},
    )
    assert result.action == (
        Verdict.ALLOW if path.startswith("/opt/work/project/") else Verdict.DENY
    )
    if result.action == Verdict.DENY:
        assert result.rule_id == "protected-path"


@pytest.mark.parametrize("command", ["install", "ln"])
@pytest.mark.parametrize("prefix", ["", "env "])
@pytest.mark.parametrize(
    "option",
    [
        "-t {path}",
        "-t{path}",
        "-vt {path}",
        "-vt{path}",
        "--target-directory {path}",
        "--target-directory={path}",
    ],
)
def test_install_and_link_only_write_the_target_directory(
    command: str, prefix: str, option: str
) -> None:
    target = option.format(path="/opt/work/project/output")
    result = decision(
        "auto", "bash", {"command": f"{prefix}{command} {target} /usr/local/read-only"}
    )
    assert result.action == Verdict.ALLOW


@pytest.mark.parametrize("command", ['echo x >"/us"r/local/report', r"echo x >/us\r/local/report"])
def test_shell_write_paths_resolve_quoted_and_escaped_components(command: str) -> None:
    result = decision("auto", "bash", {"command": command})
    assert result.action == Verdict.DENY
    assert result.rule_id == "protected-path"
    assert "/usr/local/report" in result.reason


@pytest.mark.parametrize("shell", ["sh -c", "bash -lc", "pwsh -Command", "cmd /c"])
def test_shell_wrapper_write_targets_are_separate_from_read_sources(shell: str) -> None:
    assert (
        decision(
            "auto", "bash", {"command": f'{shell} "cat /usr/share/example > local.txt"'}
        ).action
        == Verdict.ALLOW
    )
    result = decision("auto", "bash", {"command": f'{shell} "echo x > /usr/local/report"'})
    assert result.rule_id == "protected-path"
    assert "/usr/local/report" in result.reason


def test_pi_1_3_0_advertised_edit_batch_obeys_existing_edit_tiers() -> None:
    # Pi 1.3.0 core/tools/edit.js advertises path + edits[{oldText, newText}].
    args = {
        "path": "src/a.py",
        "edits": [
            {"oldText": "enabled = False", "newText": "enabled = True"},
            {"oldText": "limit = 1", "newText": "limit = 2"},
        ],
    }
    assert decision("ask", "edit", args).action == Verdict.ASK
    assert decision("plan", "edit", args).action == Verdict.DENY
    assert decision("accept-edits", "edit", args).action == Verdict.ALLOW
    assert decision("auto", "edit", args).action == Verdict.ALLOW
    assert decision("full", "edit", args).action == Verdict.ALLOW
    assert (
        decision("accept-edits", "edit", {**args, "path": "/opt/outside/a.py"}).action
        == Verdict.ASK
    )
    for tier in NON_FULL_TIERS:
        assert (
            decision(tier, "edit", {**args, "path": "/opt/work/control/a.py"}).rule_id
            == "control-path"
        )


def test_accept_edits_final_path_and_kind() -> None:
    assert (
        decision("accept-edits", "write", {"path": "src/a.py", "content": "x"}).action
        == Verdict.ALLOW
    )
    assert (
        decision(
            "accept-edits", "write", {"path": "/opt/work/project-other/a", "content": "x"}
        ).action
        == Verdict.ASK
    )

    def escaped(path: str) -> ResolvedPath:
        if path.endswith("link/a.py"):
            return ResolvedPath("/opt/outside/a.py", "file")
        return ResolvedPath(path, "directory" if path == "/opt/work/project" else "new-file")

    assert (
        decision(
            "accept-edits",
            "write",
            {"path": "link/a.py", "content": "x"},
            posix_host(resolver=escaped),
        ).action
        == Verdict.ASK
    )

    def unresolved(path: str) -> ResolvedPath:
        return ResolvedPath(path, "unknown")

    assert (
        decision(
            "accept-edits",
            "write",
            {"path": "a.py", "content": "x"},
            posix_host(resolver=unresolved),
        ).action
        == Verdict.DENY
    )


def test_decision_reuses_host_bound_paths_without_resolving_again() -> None:
    calls = []

    def resolve(path: str) -> ResolvedPath:
        calls.append(path)
        return ResolvedPath(path, "directory" if path == "/opt/work/project" else "new-file")

    intent = adapt_tool_call(
        "write", {"path": "src/a.py", "content": "x"}, posix_host(resolver=resolve)
    )
    count_after_adapt = len(calls)
    assert count_after_adapt > 0
    assert decide_tier("accept-edits", intent, RULES).action == Verdict.ALLOW
    assert decide_tier("auto", intent, RULES).action == Verdict.ALLOW
    assert len(calls) == count_after_adapt


def test_local_new_file_resolves_existing_parent_and_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    control = tmp_path / "control"
    control.mkdir()
    host = posix_host(
        cwd=str(workspace),
        workspace=str(workspace),
        home=str(tmp_path),
        control_paths=(str(control),),
        resolver=resolve_local_path,
    )
    assert (
        decision("accept-edits", "write", {"path": "new/sub/a", "content": "x"}, host).action
        == Verdict.ALLOW
    )
    assert (
        decision("accept-edits", "write", {"path": "link/new/a", "content": "x"}, host).action
        == Verdict.ASK
    )
    alias = tmp_path / "control-alias"
    alias.symlink_to(control, target_is_directory=True)
    host = replace(host, control_paths=(str(alias),))
    assert (
        decision("auto", "bash", {"command": f"echo full > {alias}/tier"}, host).rule_id
        == "control-path"
    )
    for tier in NON_FULL_TIERS:
        assert (
            decision(tier, "open_in_panel", {"path": str(alias / "report.html")}, host).rule_id
            == "control-path"
        )


def test_final_link_parent_and_symlink_home_protections(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside"
    (outside / "child").mkdir(parents=True)
    (workspace / "link").symlink_to(outside / "child", target_is_directory=True)
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    home_link = tmp_path / "home-link"
    home_link.symlink_to(real_home, target_is_directory=True)
    host = posix_host(
        cwd=str(workspace),
        workspace=str(workspace),
        home=str(home_link),
        control_paths=(str(tmp_path / "control"),),
        resolver=resolve_local_path,
    )
    assert (
        decision("accept-edits", "write", {"path": "link/../a", "content": "x"}, host).action
        == Verdict.ASK
    )
    assert (
        decision("auto", "write", {"path": "~/.ssh/config", "content": "x"}, host).rule_id
        == "protected-path"
    )
    (workspace / "keys").symlink_to(real_home / ".ssh", target_is_directory=True)
    result = decision("auto", "bash", {"command": "echo x > keys/config"}, host)
    assert result.rule_id == "protected-path"
    assert str(real_home / ".ssh" / "config") in result.reason
    for tier in NON_FULL_TIERS:
        assert (
            decision(tier, "bash", {"command": "echo full > ../control/tier"}, host).rule_id
            == "control-path"
        )


@pytest.mark.parametrize(
    "edits", [None, [None], [{"oldText": "x"}], [{"oldText": 3, "newText": "x"}], "bad"]
)
@pytest.mark.parametrize("name", ["edit", "sys_os_edit"])
def test_malformed_batch_edits_are_protocol_errors(name: str, edits: object) -> None:
    for tier in PERMISSION_TIERS:
        assert decision(tier, name, {"path": "a.py", "edits": edits}).action == Verdict.DENY


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'echo full > ../control/tier'",
        'bash -lc "echo full > ../control/tier"',
        "pwsh -Command 'Set-Content ../control/tier full'",
        'cmd /c "echo full > ../control/tier"',
    ],
)
def test_supported_shell_wrappers_bind_inner_control_paths(command: str) -> None:
    for tier in NON_FULL_TIERS:
        assert decision(tier, "bash", {"command": command}).rule_id == "control-path"


@pytest.mark.parametrize(
    ("option_command", "expected"),
    [
        (r"Set-Content -Value x C:\Windows\report", Verdict.DENY),
        (r"Set-Content -Path:C:\Windows\report x", Verdict.DENY),
        (r"Copy-Item source -Destination:C:\Windows\report", Verdict.DENY),
        (r"Remove-Item -LiteralPath:C:\Windows\probe", Verdict.DENY),
        (r"tee -FilePath:C:\Windows\report", Verdict.DENY),
        (r"Set-Content -Value x C:\work\project\report", Verdict.ALLOW),
        (r"Set-Content -Path:C:\work\project\report x", Verdict.ALLOW),
        (r"Copy-Item source -Destination:C:\Windows-old\report", Verdict.ALLOW),
    ],
)
def test_windows_drive_unc_ads_device_and_system(option_command: str, expected: Verdict) -> None:
    def resolve(path: str) -> ResolvedPath:
        return ResolvedPath(
            path, "directory" if path.lower() == "c:\\work\\project" else "new-file"
        )

    host = TrustedHostContext(
        host_id="windows-host",
        platform="windows",
        cwd=r"C:\work\project",
        workspace=r"C:\work\project",
        home=r"C:\Users\sample",
        shell="pwsh",
        control_paths=(r"C:\work\control",),
        resolver=resolve,
        drive_cwds={"C:": r"C:\work\project"},
        environment={"SystemRoot": r"C:\Windows"},
    )
    assert (
        decision("accept-edits", "write", {"path": r"c:src\a.py", "content": "x"}, host).action
        == Verdict.ALLOW
    )
    assert (
        decision(
            "accept-edits", "write", {"path": r"C:\work\project-other\a.py", "content": "x"}, host
        ).action
        == Verdict.ASK
    )
    assert (
        decision(
            "auto",
            "write",
            {"path": r"%SystemRoot%\System32\drivers\etc\hosts", "content": "x"},
            host,
        ).rule_id
        == "protected-path"
    )
    assert (
        decision("auto", "write", {"path": r"\\server\share\a.py", "content": "x"}, host).action
        == Verdict.ALLOW
    )
    for path in (
        r"C:\Users\sample\AppData\Roaming\Microsoft\Credentials\a",
        r"C:\Program Files\Application\a",
    ):
        assert (
            decision(
                "auto", "run_command", {"CommandLine": f'Set-Content "{path}" x'}, host
            ).rule_id
            == "protected-path"
        )
    for command in (
        r"Get-Content C:\Windows\example > local.txt",
        r"Copy-Item C:\Windows\example local.txt",
        r"Set-Content C:\Windows-old\report x",
        r'Write-Output ">" C:\Windows\example',
    ):
        assert (
            decision("auto", "run_command", {"CommandLine": command}, host).action == Verdict.ALLOW
        )
    for command in (
        r"Get-Content local.txt > C:\Windows\report",
        r"Copy-Item local.txt -Destination C:\Windows\report",
    ):
        result = decision("auto", "run_command", {"CommandLine": command}, host)
        assert result.rule_id == "protected-path"
        assert r"C:\Windows\report" in result.reason
    for path in (r"\\?\C:\work\project\a.py", r"C:\work\project\a.py:stream", r"D:a.py"):
        assert (
            decision("auto", "write", {"path": path, "content": "x"}, host).action == Verdict.DENY
        )
    assert (
        decision("auto", "run_command", {"CommandLine": option_command}, host).action == expected
    )


def test_terminal_and_malformed_calls_fail_closed() -> None:
    for args in (
        {"terminal": "t", "session": "s", "keys": "Up Enter"},
        {"terminal": "t", "session": "s", "text": "rm ", "keys": ""},
    ):
        assert decision("auto", "sys_terminal_send", args).action == Verdict.DENY
        assert decision("plan", "sys_terminal_send", args).action == Verdict.DENY
        assert decision("ask", "sys_terminal_send", args).action == Verdict.ASK
        assert decision("accept-edits", "sys_terminal_send", args).action == Verdict.ASK
    for tier in NON_FULL_TIERS:
        assert (
            decision(
                tier,
                "call_mcp_tool",
                {"ServerName": "omnigent", "ToolName": "sys_os_shell", "Arguments": "{bad"},
            ).action
            == Verdict.DENY
        )
        assert decision(tier, "write", {"path": "a.py"}).action == Verdict.DENY


def test_encoded_command_requires_valid_complete_payload() -> None:
    encoded = base64.b64encode("mkfs.ext4 /dev/disk4".encode("utf-16-le")).decode()
    valid = adapt_tool_call(
        "bash", {"command": "powershell -EncodedCommand " + encoded}, posix_host()
    )
    assert "mkfs.ext4" in valid.command
    assert decide_tier("auto", valid, RULES).action == Verdict.DENY
    malformed = adapt_tool_call(
        "bash", {"command": "powershell -EncodedCommand 'bad'"}, posix_host()
    )
    assert malformed.status == "invalid"
    for tier in NON_FULL_TIERS:
        assert decide_tier(tier, malformed, RULES).action == Verdict.DENY


def test_rule_errors_deny_and_signature_errors_keep_rule_reason() -> None:
    intent = adapt_tool_call("bash", {"command": "mkfs.ext4 /dev/disk4"}, posix_host())
    bad_rules = RelaxedRules({**RULES.rules, "levels": {"auto-relaxed": ["missing"]}})
    for tier in NON_FULL_TIERS:
        assert decide_tier(tier, intent, bad_rules).action == Verdict.DENY
    bad_paths = RelaxedRules({**RULES.rules, "protected_paths": {"exact": [], "prefix": []}})
    assert decide_tier("auto", intent, bad_paths).action == Verdict.DENY
    result = decide_tier("auto", intent, RelaxedRules(RULES.rules, {"signatures": "bad"}))
    assert result.action == Verdict.DENY
    assert result.rule_id == "disk-destroy"
    assert "Formats or wipes" in result.reason
