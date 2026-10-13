"""Guard decisions pinned to the permission-tier design scenarios."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.harnesses.antigravity_native import guard

RULES_PATH = Path(guard.__file__).with_name("ares_rules.json")
RULES = json.loads(RULES_PATH.read_text())
CONTROL = ["/opt/work/bridge", "/opt/work/harness/guard.py", "/opt/work/harness/ares_rules.json"]


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def command(text: str) -> dict:
    return {"toolCall": {"name": "run_command", "args": {"CommandLine": text, "Cwd": "/opt/work"}}}


def edit(path: str) -> dict:
    return {"toolCall": {"name": "replace_file_content", "args": {"TargetFile": path}}}


def mcp(tool: str, arguments: dict | str, server: str = "omnigent") -> dict:
    return {
        "toolCall": {
            "name": "call_mcp_tool",
            "args": {"ServerName": server, "ToolName": tool, "Arguments": arguments},
        }
    }


@pytest.mark.parametrize(
    ("tier", "text", "decision", "rule_id"),
    [
        ("auto-relaxed", "mkfs.ext4 /dev/disk4", "deny", "disk-destroy"),
        ("auto-relaxed", "rm -rf node_modules", "ask", None),
        ("auto-standard", "rm -rf node_modules", "deny", "fs-recursive-delete"),
        ("auto-strict", "rm -r build", "deny", None),
        ("auto-standard", "rm -r build", "deny", "fs-recursive-delete"),
        ("auto-relaxed", "rm -r build", "ask", None),
        ("auto-strict", 'rm "-x" -r build', "deny", "strict-delete"),
        ("auto-standard", 'rm "-x" -r build', "ask", None),
        ("auto-strict", "diskutil eraseDisk APFS X disk4", "deny", "macos-disk-destroy"),
        ("auto-standard", "diskutil eraseDisk APFS X disk4", "deny", "macos-disk-destroy"),
        ("auto-relaxed", "diskutil eraseDisk APFS X disk4", "deny", "macos-disk-destroy"),
        ("auto-strict", 'rm -rf "$HOME"', "deny", None),
        ("auto-standard", 'rm -rf "$HOME"', "deny", None),
        ("auto-relaxed", 'rm -rf "$HOME"', "deny", None),
        ("ask", "git push --force", "force_ask", "git-destructive"),
        ("plan", "git push --force", "force_ask", "git-destructive"),
        ("accept-edits", "git push --force", "force_ask", "git-destructive"),
    ],
)
def test_command_decision_table(tier: str, text: str, decision: str, rule_id: str | None) -> None:
    result = guard.decide(command(text), tier, RULES, CONTROL)
    assert result["decision"] == decision, (tier, text, result)
    if rule_id:
        assert f"[{rule_id}]" in result["reason"], result
        assert result["reason"].endswith(
            "If the task really needs this, stop and tell the user "
            "it needs a looser permission tier."
        )
    elif decision == "ask":
        assert result == {"decision": "ask"}


def test_disk_reason_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert guard.decide(command("mkfs.ext4 /dev/disk4"), "auto-relaxed", RULES, CONTROL) == {
        "decision": "deny",
        "reason": (
            "Omnigent guard (auto-relaxed): 磁盘/卷破坏 [disk-destroy]. "
            "ARES ARES-218 重建文件系统或交换区: "
            "命令可能在目标设备上创建文件系统或交换区，需区分实际写入与预览选项。 "
            "Impact: 实际写入可能替换原有元数据，使目标分区的数据无法正常访问；"
            "恢复取决于写入范围及备份。 "
            "Safer: 确认设备、用途和参数是否只读，验证备份；"
            "调试优先使用专用测试镜像而非真实业务磁盘。 "
            "If the task really needs this, stop and tell the user "
            "it needs a looser permission tier."
        ),
    }


@pytest.mark.parametrize("path", ["/opt/work/src/a.py", "/tmp/new/a.py"])
def test_normal_write_passes(path: str) -> None:
    assert guard.decide(edit(path), "auto-relaxed", RULES, CONTROL) == {"decision": "ask"}


@pytest.mark.parametrize("tier", ["auto-strict", "auto-standard", "auto-relaxed"])
@pytest.mark.parametrize("path", ["/", "~/.ssh/authorized_keys", "/etc/new/config"])
def test_protected_write(tier: str, path: str) -> None:
    result = guard.decide(edit(path), tier, RULES, CONTROL)
    assert result["decision"] == "deny"
    assert "[protected-path]" in result["reason"]


@pytest.mark.parametrize("tier", ["ask", "plan", "accept-edits"])
def test_ask_family_protected_write_stays_native(tier: str) -> None:
    assert guard.decide(edit("~/.ssh/authorized_keys"), tier, RULES, CONTROL) == {
        "decision": "ask"
    }


def test_symlink_protected_new_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    protected = home / ".ssh"
    protected.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    alias = tmp_path / "alias"
    alias.symlink_to(protected, target_is_directory=True)
    result = guard.decide(
        edit(str(alias / "new" / "authorized_keys")), "auto-relaxed", RULES, CONTROL
    )
    assert result["decision"] == "deny"
    assert "[protected-path]" in result["reason"]


@pytest.mark.parametrize(
    "tier,decision",
    [
        ("auto-relaxed", "deny"),
        ("ask", "force_ask"),
        ("plan", "force_ask"),
        ("accept-edits", "force_ask"),
    ],
)
@pytest.mark.parametrize(
    "payload",
    [
        command("echo ask > /opt/work/bridge/permission_tier"),
        edit("/opt/work/bridge/permission_tier"),
        edit("/opt/work/bridge/agy-home/.gemini/antigravity-cli/settings.json"),
        mcp(
            "sys_os_write",
            {"path": "/opt/work/bridge/agy-home/.gemini/config/hooks.json", "content": "{}"},
        ),
        command("cat /opt/work/harness/guard.py"),
    ],
)
def test_control_tamper(payload: dict, tier: str, decision: str) -> None:
    result = guard.decide(payload, tier, RULES, CONTROL)
    assert result["decision"] == decision
    assert "[control-path]" in result["reason"]


@pytest.mark.parametrize("prefix", ["~", "$HOME", "raw", "real"])
def test_control_command_path_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    home = tmp_path / "home"
    bridge = home / "bridge"
    bridge.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    alias = tmp_path / "alias"
    alias.symlink_to(bridge, target_is_directory=True)
    path = {"~": "~/bridge", "$HOME": "$HOME/bridge", "raw": str(alias), "real": str(bridge)}[
        prefix
    ]
    result = guard.decide(
        command(f"echo ask > {path}/permission_tier"), "auto-relaxed", RULES, [str(alias)]
    )
    assert result["decision"] == "deny"
    assert "[control-path]" in result["reason"]


@pytest.mark.parametrize("encoded", [False, True])
def test_omnigent_shell_extraction(encoded: bool) -> None:
    arguments = {"command": "rm -rf ~"}
    payload = mcp("sys_os_shell", json.dumps(arguments) if encoded else arguments)
    result = guard.decide(payload, "auto-relaxed", RULES, CONTROL)
    assert result["decision"] == "deny"
    assert "[critical-delete]" in result["reason"]


def test_mcp_non_command_and_read_pass() -> None:
    for payload in [
        mcp("sys_session_list", {}),
        mcp("sys_os_shell", {"command": "rm -rf ~"}, "other"),
        {"toolCall": {"name": "view_file", "args": {"AbsolutePath": "/etc/hosts"}}},
    ]:
        assert guard.decide(payload, "auto-relaxed", RULES, CONTROL) == {"decision": "ask"}


def test_terminal_literal_text_and_os_edit() -> None:
    result = guard.decide(
        mcp("sys_terminal_send", {"text": "mkfs.ext4 /dev/disk4", "keys": "Enter"}),
        "auto-relaxed",
        RULES,
        CONTROL,
    )
    assert result["decision"] == "deny"
    assert "[disk-destroy]" in result["reason"]
    assert guard.decide(
        mcp("sys_terminal_send", {"keys": "Enter"}), "auto-relaxed", RULES, CONTROL
    ) == {"decision": "ask"}
    result = guard.decide(
        mcp("sys_os_edit", {"path": "/etc/hosts", "oldText": "a", "newText": "b"}),
        "auto-relaxed",
        RULES,
        CONTROL,
    )
    assert "[protected-path]" in result["reason"]


def mcp_raw(args: dict) -> dict:
    return {"toolCall": {"name": "call_mcp_tool", "args": args}}


@pytest.mark.parametrize(
    "args",
    [
        # A no-arg omnigent tool without usable Arguments extracts nothing.
        {"ServerName": "omnigent", "ToolName": "sys_session_list"},
        {"ServerName": "omnigent", "ToolName": "sys_session_list", "Arguments": None},
        {"ServerName": "omnigent", "ToolName": "sys_session_list", "Arguments": ""},
        {"ServerName": "omnigent", "ToolName": "sys_os_shell"},
        {"ServerName": "omnigent", "ToolName": "sys_os_shell", "Arguments": None},
        {"ServerName": "omnigent", "ToolName": "sys_os_shell", "Arguments": ""},
        # Another server's Arguments are opaque even when malformed.
        {"ServerName": "other", "ToolName": "sys_os_shell"},
        {"ServerName": "other", "ToolName": "shell", "Arguments": "{not json"},
    ],
)
def test_mcp_missing_or_empty_arguments_asks(args: dict) -> None:
    assert guard.decide(mcp_raw(args), "auto-relaxed", RULES, CONTROL) == {"decision": "ask"}


def test_mcp_malformed_arguments_fail_closed() -> None:
    result = guard.decide(mcp("sys_os_shell", "{not json"), "auto-relaxed", RULES, CONTROL)
    assert result["decision"] == "deny"
    assert result["reason"].startswith("Omnigent guard error:")


def test_all_rules_compile_and_level_ids_exist() -> None:
    ids = set()
    for rule in RULES["rules"]:
        re.compile(rule["pattern"], re.IGNORECASE if rule["ignore_case"] else 0)
        assert rule["id"] not in ids
        ids.add(rule["id"])
    for level, members in RULES["levels"].items():
        assert set(members) <= ids, (level, set(members) - ids)


def run_cli(script: Path, tier_file: Path, payload: str, *args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-I", str(script), "--tier-file", str(tier_file), *args],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert len(completed.stdout.splitlines()) == 1, completed.stdout
    result = json.loads(completed.stdout)
    assert result and result.get("decision"), completed.stdout
    return result


@pytest.mark.parametrize(
    "case,error",
    [
        ("missing-tier", "FileNotFoundError"),
        ("unknown-tier", "ValueError"),
        ("bad-json", "JSONDecodeError"),
        ("bad-payload", "ValueError"),
        ("missing-args", "KeyError"),
        ("missing-rules", "FileNotFoundError"),
        ("bad-rules", "JSONDecodeError"),
        ("bad-pattern", "PatternError"),
        ("cli-args", "ArgumentError"),
    ],
)
def test_cli_fails_closed(tmp_path: Path, case: str, error: str) -> None:
    script = tmp_path / "guard.py"
    shutil.copyfile(guard.__file__, script)
    rules = tmp_path / "ares_rules.json"
    shutil.copyfile(RULES_PATH, rules)
    tier = tmp_path / "permission_tier"
    if case != "missing-tier":
        tier.write_text("unknown" if case == "unknown-tier" else "auto-relaxed\n")
    payload = json.dumps(command("echo safe"))
    if case == "bad-json":
        payload = "not json"
    elif case == "bad-payload":
        payload = "[]"
    elif case == "missing-args":
        payload = '{"toolCall":{"name":"run_command","args":{}}}'
    elif case == "missing-rules":
        rules.unlink()
    elif case == "bad-rules":
        rules.write_text("not json")
    elif case == "bad-pattern":
        invalid = json.loads(rules.read_text())
        invalid["rules"][0]["pattern"] = "["
        rules.write_text(json.dumps(invalid))
        # Python 3.12 calls re.PatternError re.error.
        error = "error" if sys.version_info < (3, 13) else error
    args = ("--tier-file",) if case == "cli-args" else ()
    assert run_cli(script, tier, payload, *args) == {
        "decision": "deny",
        "reason": f"Omnigent guard error: {error} — switch the permission tier to reset it",
    }


def test_cli_pass_is_never_empty(tmp_path: Path) -> None:
    tier = tmp_path / "permission_tier"
    tier.write_text("auto-relaxed\n")
    assert run_cli(Path(guard.__file__), tier, json.dumps(mcp("sys_session_list", {}))) == {
        "decision": "ask"
    }


def test_built_wheel_contains_and_runs_guard(tmp_path: Path) -> None:
    import zipfile

    root = Path(__file__).resolve().parents[3]
    source = tmp_path / "source"
    source.mkdir()
    for filename in ["pyproject.toml", "README.md"]:
        shutil.copyfile(root / filename, source / filename)
    package = Path("omnigent/harnesses/antigravity_native")
    for directory in [Path("omnigent"), Path("omnigent/harnesses"), package]:
        (source / directory).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / directory / "__init__.py", source / directory / "__init__.py")
    for filename in ["guard.py", "ares_rules.json", "ares_signatures.json"]:
        shutil.copyfile(root / package / filename, source / package / filename)
    wheels = tmp_path / "wheels"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            str(source),
            "--outdir",
            str(wheels),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheel = next(wheels.glob("*.whl"))
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        assert str(package / "ares_rules.json") in archive.namelist()
        assert str(package / "ares_signatures.json") in archive.namelist()
        archive.extractall(installed)
    tier = tmp_path / "permission_tier"
    tier.write_text("auto-relaxed\n")
    result = run_cli(
        installed / package / "guard.py", tier, json.dumps(command("mkfs.ext4 /dev/disk4"))
    )
    assert result["decision"] == "deny"
    assert "[disk-destroy]" in result["reason"]
    assert "ARES-218" in result["reason"]


def test_control_path_home_forms_with_symlinked_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_home = tmp_path / "real-home"
    bridge = real_home / "bridge"
    bridge.mkdir(parents=True)
    home_link = tmp_path / "home-link"
    home_link.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home_link))
    for path in ["~/bridge/permission_tier", "$HOME/bridge/permission_tier"]:
        result = guard.decide(command(f"echo ask > {path}"), "auto-relaxed", RULES, [str(bridge)])
        assert result["decision"] == "deny"
        assert "[control-path]" in result["reason"]


@pytest.mark.parametrize(
    "text,tier,decision,signature",
    [
        ("rm -rf node_modules", "auto-standard", "deny", "ARES-232"),
        ("git push --force", "ask", "force_ask", "ARES-223"),
        ("mkfs.ext4 /dev/disk4", "auto-relaxed", "deny", "ARES-218"),
    ],
)
def test_ares_signature_reason(text, tier, decision, signature, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    result = guard.decide(command(text), tier, RULES, CONTROL)
    assert result["decision"] == decision
    assert signature in result["reason"]
    assert "Impact: " in result["reason"] and "Safer: " in result["reason"]


def test_no_signature_falls_back_and_windows_excluded(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    for text, rule_id in [
        ("diskutil eraseDisk APFS X disk4", "macos-disk-destroy"),
        (r"reg delete HKLM\SOFTWARE\Example /f", "system-tamper"),
    ]:
        rule = next(rule for rule in RULES["rules"] if rule["id"] == rule_id)
        result = guard.decide(command(text), "auto-standard", RULES, CONTROL)
        assert result["decision"] == "deny"
        assert rule["reason"] in result["reason"]
        assert "ARES-205" not in result["reason"]


@pytest.mark.parametrize("failure", [OSError, ValueError])
def test_unreadable_signatures_preserve_rule_decision(monkeypatch, failure):
    def fail_read(*args, **kwargs):
        raise failure("signature unavailable")

    monkeypatch.setattr(Path, "read_text", fail_read)
    rule = next(rule for rule in RULES["rules"] if rule["id"] == "fs-recursive-delete")
    result = guard.decide(command("rm -rf node_modules"), "auto-standard", RULES, [])
    assert result["decision"] == "deny"
    assert rule["reason"] in result["reason"]


def test_miss_and_path_hit_do_not_open_signatures(monkeypatch):
    def fail_read(*args, **kwargs):
        pytest.fail("signature file opened without a command-rule hit")

    monkeypatch.setattr(Path, "read_text", fail_read)
    assert guard.decide(command("echo safe"), "auto-standard", RULES, []) == {"decision": "ask"}
    assert (
        "[protected-path]"
        in guard.decide(edit("/etc/hosts"), "auto-standard", RULES, [])["reason"]
    )
    assert (
        "[control-path]"
        in guard.decide(edit("/opt/work/bridge/settings.json"), "ask", RULES, CONTROL)["reason"]
    )


def test_all_signature_patterns_and_own_cases():
    signatures = json.loads(Path(guard.__file__).with_name("ares_signatures.json").read_text())[
        "signatures"
    ]
    assert len(signatures) == 214
    for signature in signatures:
        pattern = re.compile(
            signature["pattern"], re.IGNORECASE if signature["ignore_case"] else 0
        )
        for positive in signature.get("test_cases", {}).get("positive", []):
            assert pattern.search(positive), (signature["id"], positive)
        for negative in signature.get("test_cases", {}).get("negative", []):
            assert not pattern.search(negative), (signature["id"], negative)


@pytest.mark.parametrize("invalid", [{"platforms": "all"}, {"ignore_case": 1}, {"pattern": "["}])
def test_malformed_signature_preserves_per_rule_reason(monkeypatch, invalid):
    signature = {
        "id": "broken",
        "name": "broken",
        "severity": "high",
        "priority": 1,
        "platforms": ["all"],
        "pattern": "rm",
        "ignore_case": True,
        "root_cause": "cause",
        "destructive_impact": "impact",
        "safe_alternative": "safer",
        **invalid,
    }
    monkeypatch.setattr(
        Path, "read_text", lambda *a, **kw: json.dumps({"signatures": [signature]})
    )
    result = guard.decide(command("rm -rf node_modules"), "auto-standard", RULES, [])
    assert result["decision"] == "deny"
    assert "ARES" not in result["reason"]
    assert "[fs-recursive-delete]" in result["reason"]
