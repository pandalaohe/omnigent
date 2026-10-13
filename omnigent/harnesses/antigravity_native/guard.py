"""Standalone, stdlib-only PreToolUse guard for agy."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text")
    return value


def _under(target: str, parent: str) -> bool:
    return target == parent or target.startswith(parent.rstrip(os.sep) + os.sep)


def _realpath(path: str) -> str:
    # strict=False resolves existing parent symlinks even for a new file.
    return str(Path(path).expanduser().resolve(strict=False))


def _path_forms(path: str) -> set[str]:
    forms = {path, os.path.expanduser(path), _realpath(path)}
    homes = {str(Path.home()), str(Path.home().resolve(strict=False))}
    for form in tuple(forms):
        for home in homes:
            if _under(form, home):
                suffix = form[len(home) :]
                forms.update({"~" + suffix, "$HOME" + suffix})
    return forms


def _error(exc: Exception) -> dict[str, str]:
    return {
        "decision": "deny",
        "reason": (
            f"Omnigent guard error: {type(exc).__name__} — switch the permission tier to reset it"
        ),
    }


def _ares_explanation(command: str) -> str | None:
    try:
        signatures = _object(
            json.loads(Path(__file__).with_name("ares_signatures.json").read_text())
        )["signatures"]
        if not isinstance(signatures, list):
            return None
        platform = "windows" if sys.platform == "win32" else sys.platform
        ranked = sorted(
            signatures,
            key=lambda entry: (
                {"high": 0, "medium": 1, "low": 2}[entry["severity"]],
                -entry["priority"],
            ),
        )
        for signature in ranked:
            platforms = signature["platforms"]
            if (
                not isinstance(platforms, list)
                or any(value not in ("all", "darwin", "linux", "windows") for value in platforms)
                or not isinstance(signature["ignore_case"], bool)
            ):
                raise ValueError("Malformed ARES signature")
            if "all" not in platforms and platform not in platforms:
                continue
            pattern = re.compile(
                signature["pattern"], re.IGNORECASE if signature["ignore_case"] else 0
            )
            if pattern.search(command):
                for field in (
                    "id",
                    "name",
                    "root_cause",
                    "destructive_impact",
                    "safe_alternative",
                ):
                    _text(signature[field])
                return (
                    f"ARES {signature['id']} {signature['name']}: {signature['root_cause']} "
                    f"Impact: {signature['destructive_impact']} "
                    f"Safer: {signature['safe_alternative']}"
                )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, re.error):
        return None
    return None


def decide(
    payload: Mapping[str, object],
    tier: str,
    rules: Mapping[str, object],
    control_paths: Sequence[str],
) -> dict[str, str]:
    """Return an explicit decision, failing closed on malformed input or rules."""
    try:
        auto = tier in ("auto-strict", "auto-standard", "auto-relaxed")
        if not auto and tier not in ("ask", "plan", "accept-edits"):
            raise ValueError("Unknown guarded tier")
        levels = _object(rules["levels"])
        rule_list = rules["rules"]
        if not isinstance(rule_list, list):
            raise ValueError("Expected rule list")
        compiled = {}
        for value in rule_list:
            rule = _object(value)
            compiled[_text(rule["id"])] = (
                rule,
                re.compile(_text(rule["pattern"]), re.IGNORECASE if rule["ignore_case"] else 0),
            )
            _text(rule["name"])
            _text(rule["reason"])
        for ids in levels.values():
            if not isinstance(ids, list) or any(rule_id not in compiled for rule_id in ids):
                raise ValueError("Invalid level rule ids")
        active = levels[tier if auto else "ask-family"]
        protected = _object(rules["protected_paths"])
        reasons = _object(rules["path_reasons"])
        exact = [_realpath(_text(path)) for path in protected["exact"]]
        prefixes = [_realpath(_text(path)) for path in protected["prefix"]]
        _text(reasons["control"])
        _text(reasons["protected"])

        call = _object(payload["toolCall"])
        name = _text(call["name"])
        args = _object(call["args"])
        command = ""
        target = _text(args["TargetFile"]) if "TargetFile" in args else ""
        if name == "run_command":
            command = _text(args["CommandLine"])
        elif name == "call_mcp_tool":
            server = _text(args["ServerName"])
            tool = _text(args["ToolName"])
            # Only omnigent's command tools carry a command or target the
            # guard understands; other servers' Arguments are opaque. Absent
            # or empty means nothing to check (ask); malformed fails closed.
            if server == "omnigent" and tool in (
                "sys_os_shell",
                "sys_terminal_send",
                "sys_os_write",
                "sys_os_edit",
            ):
                arguments = args.get("Arguments")
                if arguments:
                    mcp_args = _object(
                        json.loads(arguments) if isinstance(arguments, str) else arguments
                    )
                    if tool == "sys_os_shell":
                        command = _text(mcp_args["command"])
                    elif tool == "sys_terminal_send":
                        command = _text(mcp_args.get("text", ""))
                    else:
                        target = _text(mcp_args["path"])
        if target:
            expanded = Path(target).expanduser()
            if not expanded.is_absolute():
                workspaces = payload.get("workspacePaths", [])
                cwd = args.get("Cwd")
                if cwd is None and isinstance(workspaces, list) and workspaces:
                    cwd = workspaces[0]
                expanded = Path(_text(cwd) if cwd is not None else os.getcwd()) / expanded
            target = _realpath(str(expanded))

        def hit(rule_id: str, name: str, reason: str) -> dict[str, str]:
            return {
                "decision": "deny" if auto else "force_ask",
                "reason": (
                    f"Omnigent guard ({tier}): {name} [{rule_id}]. {reason} "
                    "If the task really needs this, stop and tell the user "
                    "it needs a looser permission tier."
                ),
            }

        for path in control_paths:
            if (target and _under(target, _realpath(path))) or (
                command and any(form in command for form in _path_forms(path))
            ):
                return hit("control-path", "Omnigent control path", reasons["control"])
        for rule_id in active:
            rule, pattern = compiled[rule_id]
            if command and pattern.search(command):
                return hit(rule_id, rule["name"], _ares_explanation(command) or rule["reason"])
        if (
            auto
            and target
            and (target in exact or any(_under(target, prefix) for prefix in prefixes))
        ):
            return hit("protected-path", "Protected write target", reasons["protected"])
        return {"decision": "ask"}
    except Exception as exc:
        return _error(exc)


def main(argv: Sequence[str] | None = None) -> int:
    """Read stdin and always print exactly one JSON decision."""
    try:
        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        parser.add_argument("--tier-file")
        args, unknown = parser.parse_known_args(argv)
        if unknown or not args.tier_file:
            raise ValueError("Expected --tier-file")
        tier_file = Path(args.tier_file)
        payload = _object(json.load(sys.stdin))
        tier = tier_file.read_text(encoding="utf-8").strip()
        rules = _object(json.loads(Path(__file__).with_name("ares_rules.json").read_text()))
        bridge_dir = tier_file.parent
        result = decide(
            payload,
            tier,
            rules,
            [
                str(bridge_dir),
                str(bridge_dir / "agy-home" / ".gemini"),
                str(Path(__file__).resolve()),
                str(Path(__file__).with_name("ares_rules.json").resolve()),
                str(Path(__file__).with_name("ares_signatures.json").resolve()),
            ],
        )
    except Exception as exc:
        result = _error(exc)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
