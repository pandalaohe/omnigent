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
    for tier in NON_FULL_TIERS:
        assert (
            decision(tier, "bash", {"command": "echo full > ../control/tier"}, host).rule_id
            == "control-path"
        )


@pytest.mark.parametrize(
    "edits", [[None], [{"oldText": "x"}], [{"oldText": 3, "newText": "x"}], "bad"]
)
def test_malformed_batch_edits_are_protocol_errors(edits: object) -> None:
    for tier in NON_FULL_TIERS:
        assert (
            decision(tier, "sys_os_edit", {"path": "a.py", "edits": edits}).action == Verdict.DENY
        )


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


def test_windows_drive_unc_ads_device_and_system() -> None:
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
    for path in (r"\\?\C:\work\project\a.py", r"C:\work\project\a.py:stream", r"D:a.py"):
        assert (
            decision("auto", "write", {"path": path, "content": "x"}, host).action == Verdict.DENY
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
