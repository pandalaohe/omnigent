"""Corpus replay regression guard for the Pi and Antigravity permission boundary."""

from __future__ import annotations

import errno
import json
import ntpath
import os
import re
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.os_env import _assert_within_reach
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.permission_tiers import (
    NON_FULL_TIERS,
    NativeCallBinding,
    NativeInputSnapshot,
    NativeSupport,
    NativeSupportStatus,
    PermissionTier,
    ResolvedPath,
    TierDecision,
    TrustedHostContext,
    _native_digest,
    adapt_tool_call,
    decide_tier,
    load_rules,
)
from omnigent.permission_tiers.host import apply_auto_floor

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "permission_boundary_corpus.jsonl"
_WIN_INVALID_CHARS = frozenset('*?<>|"')
_WINDOWS_WORKSPACE = r"D:\work\repo"
_WINDOWS_HOME = r"C:\Users\sample"
_WINDOWS_SITE_PACKAGE = (
    r"C:\Users\sample\AppData\Roaming\uv\tools\omnigent\Lib\site-packages\omnigent"
)
_POSIX_WORKSPACE = "/opt/work/repo"
_POSIX_HOME = "/Users/sample"
_POSIX_SITE_PACKAGE = "/opt/venv/lib/python3.12/site-packages/omnigent"
_AG_BRIDGE_DIR = f"{_POSIX_HOME}/.omnigent/antigravity-native/own-bridge-0001"
_AG_BRAIN_DIR = f"{_AG_BRIDGE_DIR}/agy-home/.gemini/antigravity-cli/brain"
_AG_BUILTIN_DIR = f"{_AG_BRIDGE_DIR}/agy-home/.gemini/antigravity-cli/builtin"
_RULES = load_rules()


def _load_fixture_rows() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _windows_resolver(value: str) -> ResolvedPath:
    _drive, tail = ntpath.splitdrive(value)
    for part in tail.replace("/", "\\").split("\\"):
        if any(char in _WIN_INVALID_CHARS for char in part):
            raise OSError(123, "The filename, directory name, or volume label syntax is incorrect")
    normalized = ntpath.normpath(value)
    lower = normalized.casefold()
    if lower in {
        _WINDOWS_WORKSPACE.casefold(),
        _WINDOWS_HOME.casefold(),
        rf"{_WINDOWS_HOME}\.omnigent".casefold(),
        rf"{_WINDOWS_HOME}\.omnigent\acp-permissions\own-bridge-0001".casefold(),
        rf"{_WINDOWS_HOME}\.omnigent\pi-native\own-bridge-0001".casefold(),
        _WINDOWS_SITE_PACKAGE.casefold(),
        rf"{_WINDOWS_SITE_PACKAGE}\permission_tiers".casefold(),
    }:
        return ResolvedPath(normalized, "directory")
    if lower in {
        rf"{_WINDOWS_WORKSPACE}\.pi".casefold(),
        rf"{_WINDOWS_WORKSPACE}\.agents".casefold(),
    }:
        return ResolvedPath(normalized, "new-file")
    return ResolvedPath(normalized, "file")


def _posix_resolver(value: str) -> ResolvedPath:
    for part in value.split("/"):
        if len(part.encode("utf-8")) > 255:
            raise OSError(errno.ENAMETOOLONG, "File name too long", value)
    normalized = os.path.normpath(value)
    if normalized in {
        _POSIX_WORKSPACE,
        _POSIX_HOME,
        f"{_POSIX_HOME}/.omnigent",
        _AG_BRIDGE_DIR,
        _AG_BRAIN_DIR,
        _AG_BUILTIN_DIR,
        f"{_POSIX_HOME}/.omnigent/pi-native/own-bridge-0001",
        _POSIX_SITE_PACKAGE,
        f"{_POSIX_SITE_PACKAGE}/permission_tiers",
    }:
        return ResolvedPath(normalized, "directory")
    if normalized in {f"{_POSIX_WORKSPACE}/.pi", f"{_POSIX_WORKSPACE}/.agents"}:
        return ResolvedPath(normalized, "new-file")
    return ResolvedPath(normalized, "file")


def _context_for_row(row: dict[str, Any]) -> TrustedHostContext:
    platform = row["platform"]
    harness = row["harness"]
    tool = row["tool"]
    args = row["args"]

    if platform == "windows":
        bridge_kind = "acp-permissions" if harness == "acp" else "pi-native"
        bridge_dir = rf"{_WINDOWS_HOME}\.omnigent\{bridge_kind}\own-bridge-0001"
        controls = [
            bridge_dir,
            rf"{_WINDOWS_SITE_PACKAGE}\permission_tiers",
            rf"{_WINDOWS_SITE_PACKAGE}\inner\pi_executor.py",
            rf"{_WINDOWS_SITE_PACKAGE}\harnesses\claude_native\bridge.py",
        ]
        if harness in ("pi", "pi-native"):
            controls.extend(
                [
                    rf"{_WINDOWS_HOME}\.pi",
                    rf"{_WINDOWS_HOME}\.agents",
                    rf"{_WINDOWS_WORKSPACE}\.pi",
                    rf"{_WINDOWS_WORKSPACE}\.agents",
                ]
            )
        return TrustedHostContext(
            host_id="win-host-01",
            platform="windows",
            cwd=_WINDOWS_WORKSPACE,
            workspace=_WINDOWS_WORKSPACE,
            home=_WINDOWS_HOME,
            shell=row["shell"],
            control_paths=tuple(controls),
            readable_control_subtrees=(_WINDOWS_SITE_PACKAGE,),
            resolver=_windows_resolver,
            drive_cwds={"D:": _WINDOWS_WORKSPACE, "C:": _WINDOWS_HOME},
            environment={
                "SystemRoot": r"C:\Windows",
                "SYSTEMROOT": r"C:\Windows",
                "WINDIR": r"C:\Windows",
                "ProgramFiles": r"C:\Program Files",
                "ProgramFiles(x86)": r"C:\Program Files (x86)",
                "USERPROFILE": _WINDOWS_HOME,
                "LOCALAPPDATA": rf"{_WINDOWS_HOME}\AppData\Local",
            },
        )

    bridge_kind = "antigravity-native" if harness == "antigravity-native" else "pi-native"
    bridge_dir = f"{_POSIX_HOME}/.omnigent/{bridge_kind}/own-bridge-0001"
    controls = [
        bridge_dir,
        f"{_POSIX_SITE_PACKAGE}/permission_tiers",
    ]
    if harness in ("pi", "pi-native"):
        controls.extend(
            [
                f"{_POSIX_HOME}/.pi",
                f"{_POSIX_HOME}/.agents",
                f"{_POSIX_WORKSPACE}/.pi",
                f"{_POSIX_WORKSPACE}/.agents",
            ]
        )
    subtrees = [_POSIX_SITE_PACKAGE]
    if harness == "antigravity-native":
        subtrees.extend([_AG_BRAIN_DIR, _AG_BUILTIN_DIR])
    native_tools: dict[str, str] = {}
    native_binding: NativeCallBinding | None = None
    native_support: NativeSupport | None = None
    if row.get("native_proof") == "proven":
        native_binding = NativeCallBinding(
            scope_id="scope-01",
            installation_id="install-01",
            registry_generation="gen-01",
            name=tool,
            arguments_digest=_native_digest(args),
            tier_revision=1,
        )
        native_tools = {tool: ""}
        native_support = NativeSupport(
            binding=native_binding,
            status=NativeSupportStatus.ATTESTED,
            action="read",
            reason="Attested native guide read",
            inputs=(NativeInputSnapshot(path=_POSIX_WORKSPACE, resolved_path=None, sha256=None),),
            semantics_digest="a" * 64,
        )
    elif row.get("native_proof") == "failed":
        native_tools = {tool: ""}
    return TrustedHostContext(
        host_id="mac-host-01",
        platform="darwin",
        cwd=_POSIX_WORKSPACE,
        workspace=_POSIX_WORKSPACE,
        home=_POSIX_HOME,
        shell=row["shell"],
        control_paths=tuple(controls),
        readable_control_subtrees=tuple(subtrees),
        resolver=_posix_resolver,
        native_tools=native_tools,
        native_binding=native_binding,
        native_support=native_support,
    )


def _evaluate_row(row: dict[str, Any], tier: PermissionTier) -> tuple[TierDecision, Any]:
    context = _context_for_row(row)
    intent = adapt_tool_call(row["tool"], row["args"], context)
    decision = decide_tier(tier, intent, _RULES)
    return apply_auto_floor(tier, intent, decision, floor="narrow"), intent


def test_corpus_fixture_counts_and_neutrality() -> None:
    raw_text = FIXTURE_PATH.read_text(encoding="utf-8")
    assert not re.search(r"/Users/(?!sample\b)", raw_text)
    assert not re.search(r"C:\\\\Users\\\\(?!sample\b)", raw_text)
    assert not re.search(r"/Volumes/", raw_text)
    assert not re.search(r"\b[0-9a-f]{32}\b", raw_text)

    rows = _load_fixture_rows()
    corpus_rows = [row for row in rows if row["kind"] == "corpus"]
    assert len(corpus_rows) == 269
    by_category: dict[str, int] = {}
    for row in corpus_rows:
        by_category[row["category"]] = by_category.get(row["category"], 0) + 1
    assert by_category["opaque_command"] >= 33
    assert by_category["control_path_read"] >= 3
    assert by_category["encoded_pipe_to_shell"] >= 2
    assert by_category["pi_native_subagent_proof"] >= 2
    assert by_category["already_fixed_regression"] >= 5
    assert by_category["os_env_outside_read"] >= 2
    assert by_category["worktree_remove_guard"] >= 1
    assert by_category["hidden_script_previously_ran"] >= 1


@pytest.mark.parametrize("tier", NON_FULL_TIERS)
def test_corpus_replay_matches_boundary_rules(tier: PermissionTier) -> None:
    rows = _load_fixture_rows()
    mismatches: list[str] = []
    for row in rows:
        decision, intent = _evaluate_row(row, tier)
        if intent.gate_fault:
            mismatches.append(f"{row['id']} [{tier}] gate_fault={intent.gate_fault}")
            continue
        actual = decision.action.value.lower()
        expected = row["expected"][tier]
        if actual != expected:
            mismatches.append(
                f"{row['id']} ({row['category']}, {row['harness']} {row['tool']}) "
                f"[{tier}]: expected={expected} actual={actual} reason={decision.reason!r}"
            )
    assert not mismatches, f"{len(mismatches)} corpus mismatch(es) on tier={tier}:\n" + "\n".join(
        mismatches[:40]
    )


def test_corpus_os_env_outside_workspace_reads_stay_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside" / "Manage-HostService.ps1"
    outside.parent.mkdir(parents=True)
    outside.write_text("# fixture\n", encoding="utf-8")
    sandbox = SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )
    rows = [row for row in _load_fixture_rows() if row["layer"] == "os_env"]
    assert len(rows) == 2
    for row in rows:
        target = tmp_path / "outside" / Path(row["args"]["path"].replace("\\", "/")).name
        target.write_text("# fixture\n", encoding="utf-8")
        with pytest.raises(PermissionError, match="outside the environment root"):
            _assert_within_reach(workspace, sandbox, target, need_write=False)
