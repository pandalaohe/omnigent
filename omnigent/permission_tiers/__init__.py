"""Host-side tool classification and shared five-tier permission decisions.

The caller must bind session, host and revision before using a decision. This
module never treats model-supplied context or annotations as host facts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import ntpath
import os
import re
import shlex
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypeAlias
from urllib.parse import urlsplit

PermissionTier: TypeAlias = Literal["ask", "plan", "accept-edits", "auto", "full"]
PERMISSION_TIERS: tuple[PermissionTier, ...] = ("ask", "plan", "accept-edits", "auto", "full")
NON_FULL_TIERS: tuple[PermissionTier, ...] = PERMISSION_TIERS[:-1]
DEFAULT_PERMISSION_TIER: PermissionTier = "auto"
PI_PERMISSION_TIER_LABEL_KEY = "omnigent.pi.permission_tier"
AGY_PERMISSION_TIER_LABEL_KEY = "omnigent.antigravity_native.permission_tier"
PERMISSION_TIERS_CAPABILITY = "permission_tiers_v1"


class ToolKind(StrEnum):
    READ = "read"
    EDIT = "edit"
    DELETE = "delete"
    EXEC = "exec"
    OTHER = "other"
    UNKNOWN = "unknown"


class Verdict(StrEnum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"


@dataclass(frozen=True)
class TierDecision:
    action: Verdict
    reason: str = ""
    rule_id: str | None = None


@dataclass(frozen=True)
class ResolvedPath:
    """Final path and type returned by the actual executing host.

    kind is file, new-file, directory, or unknown. A remote host adapter must
    resolve symlinks/junctions and the nearest existing parent before returning.
    """

    path: str
    kind: Literal["file", "new-file", "directory", "unknown"]


@dataclass(frozen=True)
class TrustedHostContext:
    host_id: str
    platform: Literal["darwin", "linux", "windows"]
    cwd: str
    workspace: str
    home: str
    shell: str
    control_paths: tuple[str, ...]
    resolver: Callable[[str], ResolvedPath]
    drive_cwds: Mapping[str, str] = field(default_factory=dict)
    environment: Mapping[str, str] = field(default_factory=dict)
    extension_tools: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolIntent:
    kind: ToolKind
    name: str
    arguments_digest: str
    context: TrustedHostContext
    command: str = ""
    target: ResolvedPath | None = None
    source: ResolvedPath | None = None
    status: Literal["ok", "unknown", "invalid"] = "ok"
    detail: str = ""
    workspace_path: ResolvedPath | None = None
    resolved_control_paths: tuple[str, ...] = ()
    protected_exact_paths: tuple[str, ...] = ()
    protected_prefix_paths: tuple[str, ...] = ()
    command_paths: tuple[str, ...] = ()
    command_write_paths: tuple[str, ...] = ()
    unbound_write: bool = False


@dataclass(frozen=True)
class RelaxedRules:
    rules: Mapping[str, object]
    signatures: Mapping[str, object] | None = None


_LEGACY_AG = frozenset(("auto-strict", "auto-standard", "auto-relaxed"))
_RELAXED_IDS = (
    "disk-destroy",
    "raw-device-write",
    "cred-destroy-or-dump",
    "backup-wipe",
    "db-destroy",
    "remote-exec",
    "critical-delete",
    "fork-bomb",
    "macos-disk-destroy",
    "macos-critical-delete",
)
_PROTECTED_EXACT = ("/",)
_PROTECTED_PREFIX = (
    "/etc",
    "/private/etc",
    "/System",
    "/Library",
    "/usr",
    "/bin",
    "/sbin",
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/Library/Keychains",
)
_READ_PATH = {
    "read": "path",
    "read_file": "Path",
    "sys_os_read": "path",
    "view_file": "AbsolutePath",
    "list_dir": "DirectoryPath",
    "grep_search": "SearchPath",
    "find_by_name": "SearchDirectory",
}
_EDIT_PATH = {
    "write": "path",
    "edit": "path",
    "write_file": "Path",
    "edit_file": "Path",
    "sys_os_write": "path",
    "sys_os_edit": "path",
    "write_to_file": "TargetFile",
    "replace_file_content": "TargetFile",
    "multi_replace_file_content": "TargetFile",
}
_EXEC_COMMAND = {"bash": "command", "sys_os_shell": "command", "run_command": "CommandLine"}
_READ_NO_PATH = frozenset(
    (
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
        "sys_host_load",
        "list_comments",
        "load_skill",
        "read_skill_file",
        "browser_snapshot",
        "browser_screenshot",
        "sys_terminal_read",
        "sys_terminal_list",
        "command_status",
        "search_web",
        "search_marketplace",
        "list_permissions",
        "list_plugin_accounts",
    )
)
# These operations retain server authorization and must not bypass the plan gate.
_OPERATIONAL_TOOLS = frozenset(
    (
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
        "define_subagent",
        "schedule",
        "send_message",
    )
)
_QUESTION_TOOLS = frozenset(("ask_user", "ask_user_async", "request_user_input", "ask_question"))
_CONTROL_TOOLS = frozenset(
    (
        "report_result",
        "sys_read_inbox",
        "manage_todo_list",
        "finish",
        "wait",
        "wait_5_seconds",
        "report_browser_outcome",
    )
)
# Native tools whose effects extend beyond a checkable path or command.
_AGY_UNCHECKABLE = {
    "send_command_input": "Interactive terminal input cannot be checked",
    "run_workflow": "Workflow scripts cannot be checked as a shell command",
    "sed_file": "Sed expressions may access paths or execute commands beyond the target file",
    "notebook_execution": "Notebook execution cannot be checked as a shell command",
    "invoke_subagent": "Subagent tool execution cannot be bound to this permission check",
    "generate_image": "Image output and input paths cannot yet be bound",
    "read_resource": "MCP resource access is not a verified filesystem read",
    "list_resources": "MCP resource access is not a verified filesystem read",
    "ask_permission": "Native permission grants cannot replace the session permission tier",
    "ask_custom_permission": "Native permission grants cannot replace the session permission tier",
}
_AGY_ACTIONS = {
    "notebook_edit": {
        "list": ToolKind.READ,
        "get": ToolKind.READ,
        "get_with_outputs": ToolKind.READ,
        "add": ToolKind.EDIT,
        "update": ToolKind.EDIT,
        "delete": ToolKind.EDIT,
        "create": ToolKind.EDIT,
    },
    "manage_task": {"list": ToolKind.READ, "status": ToolKind.READ, "kill": ToolKind.OTHER},
    "manage_subagents": {
        "list": ToolKind.READ,
        "list_peers": ToolKind.READ,
        "kill": ToolKind.OTHER,
        "kill_all": ToolKind.OTHER,
    },
    "manage_inbox": {"list": ToolKind.READ, "read": ToolKind.READ},
    "webview_browser": {
        **dict.fromkeys(("snapshot", "get_state", "list-tabs"), ToolKind.READ),
        **dict.fromkeys(
            (
                "open",
                "navigate",
                "click",
                "fill",
                "type",
                "press",
                "hover",
                "scroll",
                "select_option",
                "wait",
                "back",
                "forward",
                "reload",
                "new-tab",
                "activate-tab",
                "close-tab",
                "batch",
            ),
            ToolKind.OTHER,
        ),
    },
}
_AGY_CHECKED_FIELDS: dict[str, str] = {
    key.casefold(): key
    for key in (
        "Path",
        "Content",
        "Target",
        "Replacement",
        "Edits",
        "Cwd",
        "RunPersistent",
        "RequestedTerminalID",
        "CommandLine",
        "NotebookPath",
        "Action",
        "AbsolutePath",
        "DirectoryPath",
        "SearchPath",
        "SearchDirectory",
        "TargetFile",
        "Url",
        "ServerName",
        "ToolName",
        "Arguments",
        "script",
    )
}


def _validate_agy_argument_keys(name: str, arguments: Mapping[str, object]) -> None:
    path_key = _READ_PATH.get(name) or _EDIT_PATH.get(name)
    if (
        path_key in (None, "path")
        and name not in _AGY_ACTIONS
        and name not in ("run_command", "read_url_content", "call_mcp_tool")
    ):
        return
    seen = set()
    for key in arguments:
        if not isinstance(key, str):
            raise ValueError("Native tool argument keys must be strings")
        folded = key.casefold()
        expected = (
            folded
            if name == "webview_browser" and folded in ("action", "url")
            else _AGY_CHECKED_FIELDS.get(folded)
        )
        if folded in seen or (expected is not None and key != expected):
            raise ValueError(f"Ambiguous or noncanonical native argument: {key}")
        seen.add(folded)


def plan_visible_tool_names(names: Iterable[str]) -> tuple[str, ...]:
    """Keep only registered, framework-known read/question/control tools.

    This filters the model's tool list; the execution gate still checks every
    actual call, including direct extension ``executeTool`` calls.
    """
    allowed = {
        *_READ_PATH,
        *_READ_NO_PATH,
        *_QUESTION_TOOLS,
        *_CONTROL_TOOLS,
        "sys_child_cap",
        "sys_child_checkin",
    }
    return tuple(dict.fromkeys(name for name in names if name in allowed))


_DEVICE = re.compile(r"^(?:\\\\[?.]\\|\\[?.]\\)")
_WINDOWS_ENV = re.compile(r"%([A-Za-z][A-Za-z0-9_]*)%")
_ENCODED = re.compile(
    r"(?i)\b(?:powershell|pwsh)(?:\.exe)?\b[^\r\n]*?\s-(?:encodedcommand|enc|e)\s+([^\s'\";&|]+)"
)
_ENCODED_OPTION = re.compile(
    r"(?i)\b(?:powershell|pwsh)(?:\.exe)?\b[^\r\n]*?\s-(?:encodedcommand|enc|e)\b"
)


def normalize_permission_tier(raw: object, *, harness: str, legacy: bool) -> PermissionTier:
    if harness not in ("pi", "pi-native", "antigravity-native"):
        raise ValueError("Unknown harness")
    if legacy and raw is None:
        return "ask"
    if legacy and harness == "antigravity-native" and isinstance(raw, str) and raw in _LEGACY_AG:
        return "auto"
    if not isinstance(raw, str) or raw not in PERMISSION_TIERS:
        raise ValueError("Missing permission tier")
    return raw


def load_rules() -> RelaxedRules:
    """Load package-owned resources; corrupt explanations may fall back later."""
    directory = Path(__file__).parent
    rules = json.loads((directory / "ares_rules.json").read_text(encoding="utf-8"))
    try:
        signatures = json.loads((directory / "ares_signatures.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        signatures = None
    return RelaxedRules(rules, signatures)


def _digest(arguments: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def resolve_local_path(path: str) -> ResolvedPath:
    """Resolve on the executing host; never call this for a remote host."""
    final = Path(path).resolve(strict=False)
    try:
        mode = final.stat().st_mode
    except FileNotFoundError:
        parent = final.parent
        while True:
            try:
                mode = parent.stat().st_mode
                break
            except FileNotFoundError:
                if parent == parent.parent:
                    raise
                parent = parent.parent
        return ResolvedPath(str(final), "new-file" if stat.S_ISDIR(mode) else "unknown")
    if stat.S_ISREG(mode):
        kind = "file"
    elif stat.S_ISDIR(mode):
        kind = "directory"
    else:
        kind = "unknown"
    return ResolvedPath(str(final), kind)


def _windows_absolute(path: str, context: TrustedHostContext) -> str:
    if _DEVICE.match(path):
        raise ValueError("Windows device path")
    path = path.replace("/", "\\")
    if path.startswith("~\\") or path == "~":
        path = ntpath.join(context.home, path[2:] if path != "~" else "")

    def expand(match: re.Match[str]) -> str:
        name = match.group(1).upper()
        env = {key.upper(): value for key, value in context.environment.items()}
        if name in ("USERPROFILE", "HOME"):
            return context.home
        if name in ("SYSTEMROOT", "WINDIR") and name in env:
            return env[name]
        raise ValueError("Unknown Windows path variable")

    path = _WINDOWS_ENV.sub(expand, path)
    if "$" in path or "%" in path:
        raise ValueError("Unresolved Windows path variable")
    drive, tail = ntpath.splitdrive(path)
    if not drive and path.startswith("\\") and not path.startswith("\\\\"):
        raise ValueError("Root-relative Windows path without drive")
    if drive and len(drive) == 2 and not tail.startswith("\\"):
        current = context.drive_cwds.get(drive.upper())
        if not current:
            raise ValueError("Missing drive cwd")
        path = ntpath.join(current, tail)
    elif not drive:
        path = ntpath.join(context.cwd, path)
    drive, _ = ntpath.splitdrive(path)
    if not drive or (len(drive) == 2 and not ntpath.isabs(path)):
        raise ValueError("Unanchored Windows path")
    if ":" in path[len(drive) :]:
        raise ValueError("Windows alternate data stream")
    if any(
        part.rstrip(" .").upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        for part in path[len(drive) :].split("\\")
        if part
    ):
        raise ValueError("Windows device name")
    return path


def _absolute(path: str, context: TrustedHostContext) -> str:
    if not path or "\x00" in path:
        raise ValueError("Missing or malformed path")
    if context.platform == "windows":
        return _windows_absolute(path, context)
    if path == "~" or path.startswith("~/"):
        path = context.home + path[1:]
    if "$" in path:
        raise ValueError("Unresolved path variable")
    return path if path.startswith("/") else os.path.join(context.cwd, path)


def _resolve(path: str, context: TrustedHostContext) -> ResolvedPath:
    absolute = _absolute(path, context)
    result = context.resolver(absolute)
    if not isinstance(result, ResolvedPath) or result.kind not in (
        "file",
        "new-file",
        "directory",
        "unknown",
    ):
        raise ValueError("Invalid host path resolution")
    if (context.platform == "windows" and not ntpath.isabs(result.path)) or (
        context.platform != "windows" and not result.path.startswith("/")
    ):
        raise ValueError("Host resolution returned a relative path")
    final = _absolute(result.path, context)
    final = ntpath.normpath(final) if context.platform == "windows" else os.path.normpath(final)
    return ResolvedPath(final, result.kind)


def _required_text(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing {key}")
    return value


def _command_text(command: str) -> str:
    chunks = [command]
    current = [command]
    for _ in range(3):
        following = []
        for chunk in current:
            matches = _ENCODED.findall(chunk)
            if len(_ENCODED_OPTION.findall(chunk)) != len(matches):
                raise ValueError("Uncheckable encoded command")
            for encoded in matches:
                if len(encoded) > 65536:
                    raise ValueError("Encoded command too large")
                try:
                    following.append(base64.b64decode(encoded, validate=True).decode("utf-16-le"))
                except (binascii.Error, ValueError, UnicodeError) as exc:
                    raise ValueError("Invalid encoded command") from exc
        if not following:
            return "\n".join(chunks)
        chunks.extend(following)
        current = following
    raise ValueError("Encoded command nesting too deep")


def _adapt_unbound_call(
    name: str, arguments: Mapping[str, object], context: TrustedHostContext
) -> ToolIntent:
    """Classify a known schema using facts supplied by the executing host."""
    if (
        not isinstance(name, str)
        or not isinstance(arguments, Mapping)
        or not isinstance(context, TrustedHostContext)
    ):
        return ToolIntent(
            ToolKind.UNKNOWN,
            str(name),
            "",
            context,
            status="invalid",
            detail="Malformed tool call",
        )
    try:
        digest = _digest(arguments)
        if name in context.extension_tools:
            reason = context.extension_tools[name]
            return ToolIntent(
                ToolKind.UNKNOWN if reason else ToolKind.OTHER,
                name,
                digest,
                context,
                status="unknown" if reason else "ok",
                detail=reason or "user-extension",
            )
        if name == "call_mcp_tool":
            _validate_agy_argument_keys(name, arguments)
            server = _required_text(arguments, "ServerName")
            tool = _required_text(arguments, "ToolName")
            if server != "omnigent":
                return ToolIntent(
                    ToolKind.UNKNOWN,
                    name,
                    digest,
                    context,
                    status="unknown",
                    detail="Unregistered MCP tool",
                )
            embedded = arguments.get("Arguments", {})
            if isinstance(embedded, str):
                embedded = json.loads(embedded)
            if not isinstance(embedded, dict):
                raise ValueError("MCP Arguments must be an object")
            name, arguments = tool, embedded
        _validate_agy_argument_keys(name, arguments)
        if name == "open_in_panel":
            path = _required_text(arguments, "path")
            if any(
                not part or part in (".", "..") or "\\" in part or "\x00" in part
                for part in path.lstrip("/").split("/")
            ):
                raise ValueError("Panel path must be a plain file path")
            # Artifact-relative paths use the workspace, with no home expansion.
            target = (
                "/" + path.lstrip("/")
                if path.startswith("/")
                else context.workspace.rstrip("/\\") + "/" + path
            )
            return ToolIntent(
                ToolKind.OTHER, name, digest, context, target=_resolve(target, context)
            )
        if name in _READ_PATH:
            return ToolIntent(
                ToolKind.READ,
                name,
                digest,
                context,
                target=_resolve(_required_text(arguments, _READ_PATH[name]), context),
            )
        if name in _EDIT_PATH:
            if name in ("write", "sys_os_write") and not isinstance(arguments.get("content"), str):
                raise ValueError("Missing content")
            if name == "write_file" and not isinstance(arguments.get("Content"), str):
                raise ValueError("Missing Content")
            if name in ("edit", "sys_os_edit", "edit_file"):
                keys = ("Target", "Replacement") if name == "edit_file" else ("oldText", "newText")
                pair = all(isinstance(arguments.get(key), str) for key in keys)
                edits = arguments.get("Edits" if name == "edit_file" else "edits")
                batch = isinstance(edits, list) and all(
                    isinstance(edit, dict) and all(isinstance(edit.get(key), str) for key in keys)
                    for edit in edits
                )
                if not (pair or batch):
                    raise ValueError("Missing or malformed edit content")
                if edits is not None and not batch:
                    raise ValueError("Malformed batch edit content")
            return ToolIntent(
                ToolKind.EDIT,
                name,
                digest,
                context,
                target=_resolve(_required_text(arguments, _EDIT_PATH[name]), context),
            )
        if name in _EXEC_COMMAND:
            target = None
            if name == "run_command":
                if arguments.get("RunPersistent") or arguments.get("RequestedTerminalID"):
                    return ToolIntent(
                        ToolKind.UNKNOWN,
                        name,
                        digest,
                        context,
                        status="unknown",
                        detail="Persistent terminal working directory cannot be confirmed",
                    )
                if "Cwd" in arguments:
                    target = _resolve(_required_text(arguments, "Cwd"), context)
                    if target.kind != "directory":
                        raise ValueError("Command Cwd must be a confirmed directory")
                    context = replace(context, cwd=target.path)
            return ToolIntent(
                ToolKind.EXEC,
                name,
                digest,
                context,
                command=_command_text(_required_text(arguments, _EXEC_COMMAND[name])),
                target=target,
            )
        reason = _AGY_UNCHECKABLE.get(name)
        if name == "manage_task" and arguments.get("Action") == "send_input":
            reason = "Interactive terminal input cannot be checked"
        if name == "webview_browser" and "script" in arguments:
            reason = "Browser script effects cannot be checked"
        if reason:
            return ToolIntent(
                ToolKind.UNKNOWN, name, digest, context, status="unknown", detail=reason
            )
        if name == "read_url_content":
            url = urlsplit(_required_text(arguments, "Url"))
            if url.scheme not in ("http", "https") or not url.hostname:
                raise ValueError("Only HTTP(S) URL content reads can be checked")
            return ToolIntent(ToolKind.READ, name, digest, context)
        if name in _AGY_ACTIONS:
            key = "action" if name == "webview_browser" else "Action"
            action = _required_text(arguments, key)
            kind = _AGY_ACTIONS[name].get(action)
            if kind is None:
                raise ValueError(f"Unknown {name} action")
            target = (
                _resolve(_required_text(arguments, "NotebookPath"), context)
                if name == "notebook_edit"
                else None
            )
            return ToolIntent(kind, name, digest, context, target=target)
        if name == "sys_terminal_send":
            _required_text(arguments, "terminal")
            _required_text(arguments, "session")
            if any(
                key in arguments and not isinstance(arguments[key], str)
                for key in ("text", "keys")
            ):
                raise ValueError("Malformed terminal input")
            return ToolIntent(
                ToolKind.UNKNOWN,
                name,
                digest,
                context,
                status="unknown",
                detail="Interactive terminal input cannot be checked",
            )
        if name in _READ_NO_PATH:
            return ToolIntent(ToolKind.READ, name, digest, context)
        if name == "sys_child_cap":
            if "cap" not in arguments:
                return ToolIntent(ToolKind.READ, name, digest, context)
            cap = arguments["cap"]
            if not ((type(cap) is int and cap > 0) or cap == "none"):
                raise ValueError("Child cap must be a positive integer or 'none'")
            return ToolIntent(ToolKind.OTHER, name, digest, context)
        if name == "sys_child_checkin":
            from omnigent.tools.builtins.child_checkin import validate_child_checkin_arguments

            action = validate_child_checkin_arguments(arguments)
            kind = ToolKind.READ if action == "read" else ToolKind.OTHER
            return ToolIntent(kind, name, digest, context)
        if name in _OPERATIONAL_TOOLS:
            return ToolIntent(ToolKind.OTHER, name, digest, context)
        if name in _QUESTION_TOOLS:
            return ToolIntent(ToolKind.OTHER, name, digest, context, detail="framework-question")
        if name in _CONTROL_TOOLS:
            return ToolIntent(ToolKind.OTHER, name, digest, context, detail="framework-control")
        return ToolIntent(
            ToolKind.UNKNOWN, name, digest, context, status="unknown", detail="Unknown tool schema"
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return ToolIntent(ToolKind.UNKNOWN, name, "", context, status="invalid", detail=str(exc))


def adapt_tool_call(
    name: str, arguments: Mapping[str, object], context: TrustedHostContext
) -> ToolIntent:
    """Bind all filesystem facts on the executing host before pure decision."""
    intent = _adapt_unbound_call(name, arguments, context)
    if intent.status == "invalid":
        return intent
    try:
        workspace = _resolve(context.workspace, context)
        if workspace.kind != "directory":
            raise ValueError("Workspace cannot be confirmed")
        controls = tuple(_resolve(path, context).path for path in context.control_paths)
        exact, prefixes = _protected_paths(context)
        bound = replace(
            intent,
            workspace_path=workspace,
            resolved_control_paths=controls,
            protected_exact_paths=tuple(_resolve(path, context).path for path in exact),
            protected_prefix_paths=tuple(_resolve(path, context).path for path in prefixes),
            command_paths=_command_paths(intent.command, intent.context) if intent.command else (),
        )
        try:
            return replace(
                bound,
                command_write_paths=(
                    _command_paths(intent.command, intent.context, writes_only=True)
                    if intent.command
                    else ()
                ),
            )
        except _UnboundSedWrite as exc:
            return replace(bound, status="unknown", detail=str(exc), unbound_write=True)
        except _UnboundShellWrite as exc:
            return replace(bound, status="unknown", detail=str(exc))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return replace(intent, status="invalid", detail=str(exc))


def _protected_paths(context: TrustedHostContext) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if context.platform != "windows":
        return _PROTECTED_EXACT, _PROTECTED_PREFIX
    environment = {key.upper(): value for key, value in context.environment.items()}
    root = environment.get("SYSTEMROOT") or environment.get("WINDIR")
    prefixes = [
        ntpath.join(context.home, suffix)
        for suffix in (
            r".ssh",
            r".aws",
            r".gnupg",
            r"AppData\Roaming\Microsoft\Credentials",
            r"AppData\Local\Microsoft\Credentials",
        )
    ]
    if root:
        drive = ntpath.splitdrive(root)[0]
        prefixes.extend(
            (
                root,
                environment.get("PROGRAMFILES", drive + r"\Program Files"),
                environment.get("PROGRAMFILES(X86)", drive + r"\Program Files (x86)"),
            )
        )
    return (), tuple(prefixes)


class _UnboundShellWrite(ValueError):
    """Legal shell execution whose write locations cannot be bound reliably."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            f"{reason}; write with absolute literal paths: no cd except a literal absolute "
            "cd /dir && prefix, no tee / redirect to $ targets"
        )


class _UnboundSedWrite(_UnboundShellWrite):
    """A sed script writes, executes or is read from a file."""

    def __init__(self, reason: str) -> None:
        ValueError.__init__(self, f"{reason}; use the edit or write tool, or the full tier")


_HEREDOC = re.compile(r"<<(-?)[ \t]*([^\s;&|<>()]+)")
# Same POSIX interpreter names the engine's existing `-c` recursion recognises.
_HEREDOC_SHELLS = ("sh", "bash", "zsh")
_COMMAND_LEADS = ("then", "do", "else", "elif", "{", "!", "time")


def _word_at(text: str, index: int, word: str, start: int) -> bool:
    """An unquoted `word` begins at index as a whole shell word."""
    return (
        text.startswith(word, index)
        and (index == start or text[index - 1] in " \t\n;&|()")
        and (index + len(word) == len(text) or text[index + len(word)] in " \t\n;&|()")
    )


def _command_position(text: str, index: int, start: int) -> bool:
    before = text[start:index].rstrip(" \t")
    if not before or before[-1] in ";&|()\n":
        return True
    return re.split(r"[\s;&|()]+", before)[-1] in _COMMAND_LEADS


def _heredoc_substitutions(body: str) -> list[str]:
    """Substitutions the shell expands in an unquoted heredoc body; quotes there are literal."""
    bodies = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            index += 2
            continue
        if char == "`" or body.startswith("$(", index):
            backtick = char == "`"
            end, inner, _ = _shell_scan(
                body, index + (1 if backtick else 2), "`" if backtick else ")"
            )
            if end >= len(body):
                break
            bodies.append(re.sub(r"\\([`$\\])", r"\1", inner) if backtick else inner)
            index = end + 1
            continue
        index += 1
    return bodies


def _heredoc_runs_shell(text: str, start: int) -> bool:
    """Recognize dequoted interpreter words across a simple command's redirections."""
    stream = io.StringIO(text[start:])
    lexer = shlex.shlex(stream, posix=True, punctuation_chars=";&|<>()\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lookahead = getattr(lexer, "_pushback_chars", None)
    if lookahead is None:
        raise ValueError("Shell token positions unavailable")
    redirect = False
    while True:
        position = stream.tell() - len(lookahead)
        word = lexer.get_token()
        end = stream.tell() - len(lookahead)
        raw = text[start + position : start + end].strip(" \t\r")
        if word is None or raw.startswith("#"):
            return False
        if word and raw.startswith(word) and all(char in ";&|()\n" for char in word):
            return False
        if redirect:
            redirect = False
        elif raw in (">", ">>", ">|", "&>", "&>>", ">&", "<", "<<", "<<-", "<<<", "<&", "<>"):
            redirect = True
        elif word.rsplit("/", 1)[-1] in _HEREDOC_SHELLS:
            return True


def _shell_scan(text: str, index: int = 0, closer: str = "") -> tuple[int, str, list[str]]:
    """Return the end, executable shell text and direct substitution bodies.

    Quoted heredoc data is omitted unless a shell interpreter reads it. Quotes in
    unquoted heredoc bodies are literal; case patterns do not close substitutions.
    """
    kept: list[str] = []
    bodies: list[str] = []
    quote = ""
    depth = 0
    cases = 0
    start = index
    command_start = index
    heredocs: list[tuple[str, bool, bool, bool]] = []
    while index < len(text):
        char = text[index]
        if quote == "'":
            quote = "" if char == "'" else quote
            kept.append(char)
            index += 1
            continue
        if quote == "$'":
            width = 2 if char == "\\" else 1
            quote = "" if char == "'" else quote
            kept.append(text[index : index + width])
            index += width
            continue
        if char == "\\":
            kept.append(text[index : index + 2])
            index += 2
            continue
        if char == "\n" and heredocs and not quote:
            kept.append(char)
            index += 1
            for terminator, tabs, quoted, interpreter in heredocs:
                body_start = body_end = index
                while index < len(text):
                    line_end = text.find("\n", index)
                    line_end = len(text) if line_end < 0 else line_end
                    line = text[index:line_end]
                    body_end, index = index, line_end + 1
                    if (line.lstrip("\t") if tabs else line) == terminator:
                        break
                    body_end = index
                body = text[body_start:body_end]
                if not quoted:
                    bodies.extend(_heredoc_substitutions(body))
                if interpreter:
                    kept.append(body)
                    bodies.extend(_shell_scan(body)[2])
            heredocs = []
            command_start = index
            continue
        if closer == "`" and char == "`":
            return index, "".join(kept), bodies
        if not quote and char == "#" and (index == start or text[index - 1] in " \t\n;&|()<>"):
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
            continue
        if closer == ")" and not quote:
            if _word_at(text, index, "case", start) and _command_position(text, index, start):
                cases += 1
            elif cases and _word_at(text, index, "esac", start):
                cases -= 1
            elif char in "()":
                if char == ")" and not depth and not cases:
                    return index, "".join(kept), bodies
                if not (char == ")" and not depth):
                    depth += 1 if char == "(" else -1
        if not quote and char in ";&|()\n":
            command_start = index + 1
        if not quote and text.startswith("$'", index):
            quote = "$'"
            kept.append("$'")
            index += 2
            continue
        if char == "'" and not quote:
            quote = "'"
        elif char == '"':
            quote = "" if quote else '"'
        elif not quote and text.startswith("<<", index) and not text.startswith("<<<", index):
            match = _HEREDOC.match(text, index)
            if match:
                heredocs.append(
                    (
                        re.sub(r"['\"\\\\]", "", match[2]),
                        bool(match[1]),
                        any(c in match[2] for c in "'\"\\\\"),
                        _heredoc_runs_shell(text, command_start),
                    )
                )
                kept.append(match[0])
                index = match.end()
                continue
        elif (char == "`" and closer != "`") or text.startswith("$(", index):
            backtick = char == "`"
            end, body, _ = _shell_scan(
                text, index + (1 if backtick else 2), "`" if backtick else ")"
            )
            if end >= len(text):
                break  # unterminated: the shell rejects the command
            bodies.append(re.sub(r"\\([`$\\])", r"\1", body) if backtick else body)
            kept.append(text[index : end + 1])
            index = end + 1
            continue
        kept.append(char)
        index += 1
    return index, "".join(kept), bodies


def _expansion_end(raw: str, index: int) -> int:
    """End of a shell expansion starting at index, else 0."""
    if raw[index] == "`":
        return min(_shell_scan(raw, index + 1, "`")[0] + 1, len(raw))
    if raw[index] != "$" or index + 1 >= len(raw):
        return 0
    following = raw[index + 1]
    if following == "(":
        return min(_shell_scan(raw, index + 2, ")")[0] + 1, len(raw))
    if following == "{":
        close = raw.find("}", index)
        return len(raw) if close < 0 else close + 1
    if following.isalpha() or following == "_":
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", raw[index + 1 :])
        return index + 1 + (match.end() if match else 1)
    return index + 2 if following in "0123456789@*#?$!-" else 0


_ANSI_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "n": "\n",
    "t": "\t",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}


def _ansi_escape(raw: str, index: int) -> tuple[int, str]:
    match = re.match(
        r"\\([0-7]{1,3}|x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|U[0-9a-fA-F]{1,8}|c.)", raw[index:]
    )
    if match:
        value = match[1]
        if value[0] == "c":
            code = 127 if value[1] == "?" else ord(value[1].upper()) & 31
        else:
            code = int(value, 8) & 255 if value[0].isdigit() else int(value[1:], 16)
        if code <= 0x10FFFF:
            return index + match.end(), chr(code)
    following = raw[index + 1]
    return index + 2, _ANSI_ESCAPES.get(following, "\\" + following)


def _script_word(raw: str) -> str:
    """Dequote one raw shell word as the program receives it; each expansion becomes 0.

    Literal text keeps its characters wherever it is quoted; only a real expansion (outside
    single quotes) is replaced, since its value is not resolved.
    """
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote == "'":
            quote = "" if char == "'" else quote
            out.append("" if char == "'" else char)
            index += 1
        elif quote == "$'":
            if char == "\\" and index + 1 < len(raw):
                index, value = _ansi_escape(raw, index)
                out.append(value)
            else:
                quote = "" if char == "'" else quote
                out.append("" if char == "'" else char)
                index += 1
        elif char == "\\":
            following = raw[index + 1 : index + 2]
            if quote == '"' and following not in '$`"\\\n':
                out.append(char)
                index += 1
            else:
                out.append("" if following == "\n" else following)
                index += 2
        elif not quote and raw.startswith("$'", index):
            quote = "$'"
            index += 2
        elif char == "'" and not quote:
            quote = "'"
            index += 1
        elif char == '"':
            quote = "" if quote else '"'
            index += 1
        elif end := _expansion_end(raw, index):
            out.append("0")
            index = end
        else:
            out.append(char)
            index += 1
    return "".join(out)


_SED_LONG = (
    "debug",
    "expression",
    "file",
    "follow-symlinks",
    "help",
    "in-place",
    "line-length",
    "null-data",
    "zero-terminated",
    "quiet",
    "silent",
    "posix",
    "regexp-extended",
    "sandbox",
    "separate",
    "unbuffered",
    "binary",
    "version",
)


def _sed_reading(args: list[str], *, bsd: bool) -> tuple[bool, list[tuple[int, str]], bool]:
    """One implementation's option reading: (from_file, scripts, in_place)."""
    scripts: list[tuple[int, str]] = []
    operands: list[tuple[int, str]] = []
    in_place = False
    index = 0
    finished = True
    while index < len(args):
        value = args[index]
        if value == "--":
            operands.extend((i, args[i]) for i in range(index + 1, len(args)))
            break
        if value.startswith("--") and len(value) > 2:
            name, eq, rest = value[2:].partition("=")
            if bsd:
                finished = False
                break
            if "file".startswith(name):
                return True, scripts, in_place
            if "expression".startswith(name):
                if not eq:
                    index += 1
                    if index >= len(args):
                        finished = False
                        break
                    rest = args[index]
                scripts.append((index, rest))
            elif "in-place".startswith(name):
                in_place = True
            elif "line-length".startswith(name) and not eq:
                index += 1
            elif not any(option.startswith(name) for option in _SED_LONG):
                finished = False
                break
            index += 1
            continue
        if value.startswith("-") and len(value) > 1:
            flags = "EaHlnru" if bsd else "nrEsuzb"
            position = 1
            stop = False
            while position < len(value):
                char = value[position]
                rest = value[position + 1 :]
                if char == "f":
                    return True, scripts, in_place
                if char == "e":
                    if not rest:
                        index += 1
                        if index >= len(args):
                            stop = True
                            break
                        rest = args[index]
                    scripts.append((index, rest))
                    break
                if char == "i" or (bsd and char == "I"):
                    in_place = True
                    if bsd and not rest:
                        index += 1
                    break
                if char == "l" and not bsd:
                    if not rest:
                        index += 1
                    break
                if char not in flags:
                    stop = True
                    break
                position += 1
            if stop:
                finished = False
                break
            index += 1
            continue
        if bsd:
            operands.extend((i, args[i]) for i in range(index, len(args)))
            break
        operands.append((index, value))
        index += 1
    if finished and not scripts:
        scripts = operands[:1]
    return False, scripts, in_place


# Whitespace sed skips at grammar boundaries: C isspace() without newline (BSD EATSPACE).
_SED_BLANK = " \t\v\f\r"
_SED_SINGLE = set("=dDgGhHnNpPxzF")
_SED_NUMERIC = set("lLqQ")
_SED_LABEL = set(":btT")
_SED_TEXT = set("aic")
_SED_FILE = set("rR")


def _sed_delimited(script: str, index: int, delimiter: str, regex: bool) -> int:
    """Return the index after the closing delimiter, or -1 when unterminated."""
    while index < len(script):
        char = script[index]
        if char == "\\":
            index += 2
            continue
        if char == "\n" and delimiter != "\n":
            return -1
        if regex and char == "[":
            # BSD skips bracket expressions; GNU-valid bodies split identically.
            end = index + 1
            if end < len(script) and script[end] == "^":
                end += 1
            if end < len(script) and script[end] == "]":
                end += 1
            while end < len(script) and script[end] != "]":
                if script.startswith(("[:", "[.", "[="), end):
                    close = script.find(script[end + 1] + "]", end + 2)
                    end = close + 2 if close >= 0 else len(script)
                else:
                    end += 1
            if end >= len(script):
                return -1
            index = end + 1
            continue
        if char == delimiter:
            return index + 1
        index += 1
    return -1


def _sed_address(script: str, index: int) -> int:
    """Skip zero to two addresses; -1 when a regex address is unterminated."""
    for part in range(2):
        while index < len(script) and script[index] in _SED_BLANK:
            index += 1
        if index >= len(script):
            return index
        char = script[index]
        if char.isdigit():
            while index < len(script) and script[index].isdigit():
                index += 1
            if index < len(script) and script[index] == "~":
                index += 1
                while index < len(script) and script[index].isdigit():
                    index += 1
        elif char == "$":
            index += 1
        elif char in "/\\":
            delimiter = "/"
            if char == "\\":
                if index + 1 >= len(script):
                    return -1
                delimiter = script[index + 1]
                index += 1
            index = _sed_delimited(script, index + 1, delimiter, True)
            if index < 0:
                return -1
            while index < len(script) and script[index] in "IM":
                index += 1
        elif part == 1 and char in "+~":
            index += 1
            while index < len(script) and script[index].isdigit():
                index += 1
        elif part == 1:
            return -1
        else:
            return index
        while index < len(script) and script[index] in _SED_BLANK:
            index += 1
        if part == 0 and index < len(script) and script[index] == ",":
            index += 1
            continue
        return index
    return index


def _sed_script_writes(script: str) -> bool:
    """Find a writing command before both GNU and BSD sed would reject the script."""
    index = 0
    length = len(script)
    while index < length:
        char = script[index]
        if char in _SED_BLANK + "\n;":
            index += 1
            continue
        index = _sed_address(script, index)
        if index < 0:
            return False
        while index < length and script[index] in _SED_BLANK + "!":
            index += 1
        if index >= length:
            return False
        command = script[index]
        index += 1
        if command in "wWe":
            return True
        if command == "#":
            newline = script.find("\n", index)
            index = length if newline < 0 else newline
            continue
        if command in "{}":
            continue
        if command in _SED_TEXT or command in _SED_FILE:
            # Text and file names run to the end of the line (backslash-newline continues).
            while index < length and script[index] != "\n":
                index += 2 if script[index] == "\\" else 1
            continue
        if command in _SED_LABEL:
            while index < length and script[index] not in ";\n":
                index += 1
            continue
        if command == "v":
            while index < length and script[index] not in ";\n}":
                index += 1
            continue
        if command in ("s", "y"):
            if index >= length or script[index] in "\\\n":
                return False
            delimiter = script[index]
            index = _sed_delimited(script, index + 1, delimiter, command == "s")
            if index < 0:
                return False
            index = _sed_delimited(script, index, delimiter, False)
            if index < 0:
                return False
            if command == "s":
                while index < length and (
                    script[index] in "gpiImMe" + _SED_BLANK or script[index].isdigit()
                ):
                    if script[index] == "e":
                        return True
                    index += 1
                if index < length and script[index] == "w":
                    return True
        elif command in _SED_NUMERIC:
            while index < length and (script[index] in _SED_BLANK or script[index].isdigit()):
                index += 1
        elif command not in _SED_SINGLE:
            return False
        while index < length and script[index] in _SED_BLANK:
            index += 1
        if index < length and script[index] not in ";\n}#":
            return False
    return False


def _windows_path_operands(tokens: list[str], raw_tokens: list[str], shell: str) -> list[str]:
    """Exclude established data roles; unknown syntax keeps conservative candidates."""
    if not tokens:
        return []
    conservative = [value for value in tokens if not value.startswith("-")]
    dialect = ntpath.basename(shell).lower().removesuffix(".exe")
    if dialect not in ("pwsh", "powershell", "cmd", "sh", "bash", "zsh") or any(
        any(char in raw for char in "$`^!{}\r\n")
        or (dialect == "cmd" and any(char in raw for char in "%'"))
        or (dialect in _HEREDOC_SHELLS and "\\" in raw)
        for raw in raw_tokens
    ):
        return conservative
    command = tokens[0].lower().removesuffix(".exe")
    if tokens[0].lower() in ("echo", "write-output"):
        return conservative if any(value.startswith("-") for value in tokens[1:]) else tokens[:1]
    if command == "git" and tokens[1:2] == ["commit"]:
        start = 2
        data_options = ("-m", "--message")
        file_options = ("-F", "--file")
        flags = (
            "-a",
            "--all",
            "-s",
            "--signoff",
            "--amend",
            "--no-edit",
            "--no-verify",
            "--allow-empty",
            "--allow-empty-message",
            "-q",
            "--quiet",
            "-v",
            "--verbose",
        )
    elif command == "curl":
        start = 1
        data_options = ("--url",)
        file_options = ("-o", "--output", "-T", "--upload-file", "-K", "--config")
        flags = (
            "-L",
            "--location",
            "-s",
            "--silent",
            "-S",
            "--show-error",
            "-f",
            "--fail",
            "-I",
            "--head",
            "-v",
            "--verbose",
        )
    elif command == "find" and not any(
        value in ("-exec", "-execdir", "-ok", "-okdir") for value in tokens[1:]
    ):
        start = 1
        data_options = ("-printf",)
        file_options = (
            "-fprint",
            "-fprint0",
            "-fprintf",
            "-fls",
            "-name",
            "-path",
            "-type",
            "-maxdepth",
            "-mindepth",
        )
        flags = ("-print", "-print0", "-ls", "-a", "-and", "-o", "-or", "-not")
    else:
        return conservative
    candidates = tokens.copy()
    data = set()
    files = set()
    certain = True
    options = True
    index = start
    while index < len(tokens):
        value = tokens[index]
        option, equals, attached = value.partition("=")
        if options and value == "--":
            options = False
        elif options and option in (*data_options, *file_options):
            if not equals:
                index += 1
                if index == len(tokens):
                    certain = False
                    break
                attached = tokens[index]
            if option in file_options:
                candidates[index] = attached
                files.add(index)
            elif command != "curl" or (
                urlsplit(attached).scheme in ("http", "https") and urlsplit(attached).hostname
            ):
                data.add(index)
        elif options and any(
            len(option) == 2 and value.startswith(option) and len(value) > 2
            for option in file_options
        ):
            candidates[index] = value[2:]
            files.add(index)
        elif options and value.startswith("-"):
            certain &= value in flags
        elif command == "curl":
            url = urlsplit(value)
            if url.scheme in ("http", "https") and url.hostname:
                data.add(index)
        index += 1
    return [
        value
        for index, value in enumerate(candidates)
        if (not certain or index not in data) and (index in files or not value.startswith("-"))
    ]


def _windows_command_paths(
    command: str, context: TrustedHostContext, depth: int
) -> tuple[str, ...]:
    raw_tokens = []
    end = 0
    pattern = (
        r"&>>|&>|>>|>\||>&|<<<|<<|<&|<>|&&|\|\||[;&|<>()\n]|"
        r"""(?:"[^"]*"|'[^']*'|[^\s"'|;&<>()])+"""
    )
    for match in re.finditer(pattern, command):
        if command[end : match.start()].strip():
            raise ValueError("Uncheckable Windows shell quoting")
        raw_tokens.append(match[0])
        end = match.end()
    if command[end:].strip():
        raise ValueError("Uncheckable Windows shell quoting")
    tokens = [
        re.sub(
            r""""([^"]*)"|'([^']*)'""",
            lambda match: next(value for value in match.groups() if value is not None),
            raw,
        )
        for raw in raw_tokens
    ]
    segments: list[list[str]] = [[]]
    raw_segments: list[list[str]] = [[]]
    candidates = []
    index = 0
    while index < len(tokens):
        raw = raw_tokens[index]
        if raw and all(char in ";&|()\n" for char in raw):
            segments.append([])
            raw_segments.append([])
        elif raw in (">", ">>", ">|", "&>", "&>>", ">&", "<", "<<", "<<<", "<&", "<>"):
            index += 1
            if index == len(tokens) or re.fullmatch(r"[;&|<>()\n]+", raw_tokens[index]):
                raise ValueError("Missing shell redirection target")
            candidates.append(tokens[index])
        else:
            segments[-1].append(tokens[index])
            raw_segments[-1].append(raw)
        index += 1
    paths = []
    for segment, raw_segment in zip(segments, raw_segments, strict=True):
        if not segment:
            continue
        shell = segment[0].lower().removesuffix(".exe")
        option_index = 1
        allowed = (
            ("/d", "/s")
            if shell == "cmd"
            else ("-noprofile", "-noninteractive", "-nologo")
            if shell in ("powershell", "pwsh")
            else ()
        )
        while option_index < len(segment) and segment[option_index].lower() in allowed:
            option_index += 1
        if option_index < len(segment) - 1 and (
            (shell == "cmd" and segment[option_index].lower() == "/c")
            or (
                shell in ("powershell", "pwsh")
                and segment[option_index].lower() in ("-command", "-c")
            )
            or (
                shell in _HEREDOC_SHELLS and re.fullmatch(r"-[a-z]*c[a-z]*", segment[option_index])
            )
        ):
            inner = (
                " ".join(raw_segment[option_index + 1 :])
                if shell in ("cmd", "powershell", "pwsh") and len(segment) > option_index + 2
                else segment[option_index + 1]
            )
            paths.extend(_command_paths(inner, replace(context, shell=shell), depth=depth + 1))
            candidates.extend(
                value for value in segment[:option_index] if not value.startswith("-")
            )
            if shell in _HEREDOC_SHELLS:
                candidates.extend(segment[option_index + 2 :])
        else:
            candidates.extend(_windows_path_operands(segment, raw_segment, context.shell))
    for token in candidates:
        if token:
            paths.append(_resolve(token, context).path)
    return tuple(paths)


def _command_paths(
    command: str,
    context: TrustedHostContext,
    *,
    powershell: bool = False,
    depth: int = 0,
    writes_only: bool = False,
) -> tuple[str, ...]:
    """Bind direct path operands; this does not interpret scripts or terminal state."""
    if depth > 3:
        raise ValueError("Shell wrapper nesting cannot be checked")
    if context.platform == "windows" and not writes_only:
        return _windows_command_paths(command, context, depth)
    raw_tokens = []
    if writes_only:
        if context.platform == "windows" or powershell:
            raw_tokens = re.findall(r"""[;&|<>()]+|(?:"[^"]*"|'[^']*'|[^\s"'|;&<>()])+""", command)
            tokens = [
                re.sub(
                    r""""([^"]*)"|'([^']*)'""",
                    lambda match: next(value for value in match.groups() if value is not None),
                    token,
                )
                for token in raw_tokens
            ]
        else:
            stream = io.StringIO(command)
            lexer = shlex.shlex(stream, posix=True, punctuation_chars=";&|<>()\n")
            lexer.whitespace_split = True
            lexer.whitespace = " \t\r"
            tokens = []
            lookahead = getattr(lexer, "_pushback_chars", None)
            if lookahead is None:
                raise ValueError("Shell token positions unavailable")
            while True:
                # Exclude shlex's lookahead to retain each operator's original quoting.
                start = stream.tell() - len(lookahead)
                token = lexer.get_token()
                if token is None:
                    break
                end = stream.tell() - len(lookahead)
                tokens.append(token)
                raw_tokens.append(command[start:end].strip(" \t\r"))
    elif context.platform == "windows" or powershell:
        tokens = [
            next(value for value in match if value)
            for match in re.findall(r""""([^"]+)"|'([^']+)'|([^\s|;&<>()]+)""", command)
        ]
    else:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    if (
        writes_only
        and context.platform != "windows"
        and not powershell
        and len(tokens) > 3
        and raw_tokens[0] == "cd"
        and tokens[1].startswith("/")
        and not any(char in raw_tokens[1] for char in "$`\\~*?[{}\r\n")
        and raw_tokens[2] == "&&"
    ):
        paths = _command_paths(
            " ".join(raw_tokens[3:]),
            replace(context, cwd=_resolve(os.path.normpath(tokens[1]), context).path),
            depth=depth,
            writes_only=True,
        )
        if paths and any(
            "\n" in raw
            or (raw and all(char in ";|&()" for char in raw) and raw not in ("&&", "|"))
            for raw in raw_tokens[3:]
        ):
            raise _UnboundShellWrite("Shell cwd changes outside an && chain cannot be bound")
        return paths
    paths = []
    if writes_only and context.platform != "windows" and not powershell:
        for body in _shell_scan(command)[2]:
            try:
                paths.extend(_command_paths(body, context, depth=depth + 1, writes_only=True))
            except _UnboundShellWrite:
                raise
            except ValueError:
                continue
    for index, token in enumerate(tokens):
        shell = token.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        if shell not in ("sh", "bash", "zsh", "cmd", "powershell", "pwsh"):
            continue
        for option_index in range(index + 1, len(tokens) - 1):
            option = tokens[option_index].lower()
            supported = (
                option in ("-command", "-c")
                if shell in ("powershell", "pwsh")
                else option == "/c"
                if shell == "cmd"
                else re.fullmatch(r"-[a-z]*c[a-z]*", option) is not None
            )
            if supported:
                inner = (
                    " ".join(
                        raw_tokens[option_index + 1 :]
                        if writes_only and len(tokens) > option_index + 2
                        else tokens[option_index + 1 :]
                    )
                    if shell in ("cmd", "powershell", "pwsh")
                    else tokens[option_index + 1]
                )
                paths.extend(
                    _command_paths(
                        inner,
                        context,
                        powershell=shell in ("powershell", "pwsh"),
                        depth=depth + 1,
                        writes_only=writes_only,
                    )
                )
                break
            if not option.startswith(("-", "/")):
                break
    if writes_only:
        candidates = []
        segments = []
        segment = []
        raw_segments = []
        raw_segment = []
        index = 0
        while index < len(tokens):
            raw = raw_tokens[index]
            if raw.startswith("#") and "\n" in raw:
                segments.append(segment)
                segment = []
                raw_segments.append(raw_segment)
                raw_segment = []
                raw = raw.rsplit("\n", 1)[-1]
            if (
                raw.startswith(tokens[index])
                and tokens[index]
                and all(char in ";|&<>()\n" for char in tokens[index])
            ):
                raw = tokens[index]
            if raw and all(char in ";|&()\n" for char in raw):
                segments.append(segment)
                segment = []
                raw_segments.append(raw_segment)
                raw_segment = []
            elif raw in (">", ">>", ">|", "&>", "&>>", ">&", "<", "<<", "<<<", "<&", "<>"):
                if index + 1 >= len(tokens):
                    raise ValueError("Missing shell redirection target")
                index += 1
                destination = tokens[index]
                if raw in (">", ">>", ">|", "&>", "&>>", "<>") or (
                    raw == ">&" and not destination.isdigit() and destination != "-"
                ):
                    if destination != "/dev/null" or context.platform == "windows":
                        candidates.append(destination)
            else:
                segment.append(tokens[index])
                raw_segment.append(raw)
            index += 1
        segments.append(segment)
        raw_segments.append(raw_segment)
        cwd_changed = False
        for segment, raw_segment in zip(segments, raw_segments, strict=True):
            candidates.extend(
                _write_operands(
                    segment,
                    raw_segment,
                    posix_shell=context.platform != "windows" and not powershell,
                    bsd_sed=context.platform == "darwin",
                )
            )
            prefix = 0
            while prefix < len(segment) and (
                segment[prefix] in ("if", "then", "else", "elif", "do", "!", "command", "builtin")
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[prefix], re.DOTALL)
            ):
                execution_command = segment[prefix] == "command"
                prefix += 1
                if execution_command:
                    while prefix < len(segment) and segment[prefix] == "-p":
                        prefix += 1
                    if prefix < len(segment) and segment[prefix] == "--":
                        prefix += 1
            if prefix < len(segment) and segment[prefix].lower() in (
                "cd",
                "pushd",
                "popd",
                "set-location",
            ):
                cwd_changed = True
        if (candidates or paths) and cwd_changed:
            raise _UnboundShellWrite("Shell cwd changes mixed with writes cannot be bound")
        tokens = candidates
    for token in tokens:
        if writes_only and any(char in token for char in "\r\n"):
            raise _UnboundShellWrite("Shell write target with a newline cannot be bound")
        if (
            not token
            or token.startswith("-")
            or token in (";", "&&", "||", "|", ">", ">>", "<", "(", ")")
        ):
            continue
        if context.platform != "windows":
            if token == "$HOME" or token.startswith("$HOME/"):
                token = context.home + token[5:]
            elif token == "${HOME}" or token.startswith("${HOME}/"):
                token = context.home + token[7:]
        if writes_only and any(char in token for char in "$`"):
            raise _UnboundShellWrite("Shell write target expansion cannot be bound")
        if "$" in token or (not writes_only and any(char in token for char in "*?=")):
            continue
        paths.append(_resolve(token, context).path)
    return tuple(paths)


def _write_operands(
    tokens: list[str], raw_tokens: list[str], *, posix_shell: bool, bsd_sed: bool
) -> list[str]:
    """Select reliable direct targets; retain all locations for unbound mutations."""
    # Every sed token first: operand selection below returns at the first write command.
    for offset, token in enumerate(tokens):
        name = token.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        if name in ("sed", "gsed"):
            words = [_script_word(raw) for raw in raw_tokens[offset + 1 :]]
            for bsd in (False, True) if bsd_sed and name == "sed" else (False,):
                from_file, scripts, _ = _sed_reading(words, bsd=bsd)
                if from_file or any(_sed_script_writes(text) for _, text in scripts):
                    raise _UnboundSedWrite("Sed script writes, executes or is read from a file")
    conservative = [
        value.split(":" if ":" in value else "=", 1)[1]
        if value.startswith("-") and (":" in value or "=" in value)
        else value
        for value in tokens
    ]
    for offset, token in enumerate(tokens):
        command = token.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        arguments = tokens[offset + 1 :]
        if command in ("cp", "mv", "install", "ln"):
            # Retain attached targets even when a prefix prevents precise binding.
            stop = arguments.index("--") if "--" in arguments else len(arguments)
            for index, value in enumerate(arguments[:stop], start=offset + 1):
                attached = re.fullmatch(r"-[A-Za-z]*?t(.*)", value)
                if attached:
                    conservative[index] = attached[1]
                    arguments[index - offset - 1] = "-t" + attached[1]
        operands = [value for value in arguments if value and not value.startswith("-")]
        if command in ("cp", "copy", "copy-item", "install", "ln"):
            if offset and command not in ("install", "ln"):
                return conservative
            options = arguments
            if command in ("cp", "install", "ln") and "--" in arguments:
                stop = arguments.index("--")
                options = arguments[:stop]
                operands = [value for value in options if value and not value.startswith("-")]
                operands.extend(arguments[stop + 1 :])
            for index, value in enumerate(options):
                if (
                    command in ("cp", "install", "ln") and value in ("-t", "--target-directory")
                ) or (command == "copy-item" and value.lower() == "-destination"):
                    return options[index + 1 : index + 2] or conservative
                if command in ("cp", "install", "ln") and value.startswith("--target-directory="):
                    return [value.split("=", 1)[1]]
                if command in ("cp", "install", "ln") and value.startswith("-t") and value != "-t":
                    return [value[2:]]
                if command == "copy-item" and value.lower().startswith("-destination:"):
                    return [value.split(":", 1)[1]]
            if any(
                value.startswith("-") and value not in ("-T", "--no-target-directory", "--")
                for value in options
            ):
                return conservative
            return operands[-1:]
        if command in ("set-content", "add-content", "out-file"):
            if offset:
                return conservative
            for index, value in enumerate(arguments):
                if value.lower() in ("-path", "-literalpath", "-filepath"):
                    return arguments[index + 1 : index + 2] or conservative
                if value.lower().startswith(("-path:", "-literalpath:", "-filepath:")):
                    return [value.split(":", 1)[1]]
            return (
                conservative if any(value.startswith("-") for value in arguments) else operands[:1]
            )
        if command in ("sed", "gsed"):
            if any(value.startswith(("-i", "--in-place")) for value in arguments) or any(
                _sed_reading(arguments, bsd=bsd)[2]
                for bsd in ((False, True) if bsd_sed and command == "sed" else (False,))
            ):
                return conservative
            continue
        if command == "tee":
            if offset or not posix_shell:
                return conservative
            stop = arguments.index("--") if "--" in arguments else len(arguments)
            return [value for value in arguments[:stop] if value and not value.startswith("-")] + (
                arguments[stop + 1 :]
            )
        if command in (
            "rm",
            "mv",
            "chmod",
            "chown",
            "del",
            "erase",
            "rd",
            "rmdir",
            "remove-item",
            "move-item",
        ):
            return conservative
    return []


def _under(target: str, parent: str, windows: bool) -> bool:
    if windows:
        target, parent = (
            ntpath.normcase(ntpath.normpath(target)),
            ntpath.normcase(ntpath.normpath(parent)),
        )
        separator = "\\"
    else:
        target, parent = os.path.normpath(target), os.path.normpath(parent)
        separator = "/"
    return target == parent or target.startswith(parent.rstrip(separator) + separator)


def _path_forms(path: str, context: TrustedHostContext) -> set[str]:
    forms = {path}
    if context.platform == "windows":
        forms.add(path.replace("\\", "/"))
        if _under(path, context.home, True):
            suffix = path[len(context.home) :]
            forms.update({"%USERPROFILE%" + suffix, "%HOME%" + suffix})
        system_root = context.environment.get("SystemRoot") or context.environment.get(
            "SYSTEMROOT"
        )
        if system_root and _under(path, system_root, True):
            forms.add("%SystemRoot%" + path[len(system_root) :])
    elif _under(path, context.home, False):
        suffix = path[len(context.home) :]
        forms.update({"~" + suffix, "$HOME" + suffix, "${HOME}" + suffix})
    return forms


def _mentions_path(command: str, path: str, context: TrustedHostContext) -> bool:
    if context.platform == "windows":
        command = command.casefold().replace("/", "\\")
    for form in _path_forms(path, context):
        if context.platform == "windows":
            form = form.casefold().replace("/", "\\")
        if re.search(r"(?<![\w./~\\-])" + re.escape(form) + r"(?![\w.-])", command):
            return True
    return False


def _validated_rules(
    rules: RelaxedRules,
) -> tuple[list[tuple[str, str, str, re.Pattern[str]]], list[str], list[str], str, str]:
    document = rules.rules
    if not isinstance(document, Mapping) or document.get("schema") != 1:
        raise ValueError("Invalid ARES rule document")
    definitions = document.get("rules")
    levels = document.get("levels")
    protected = document.get("protected_paths")
    reasons = document.get("path_reasons")
    if (
        not isinstance(definitions, list)
        or not isinstance(levels, dict)
        or not isinstance(protected, dict)
        or not isinstance(reasons, dict)
    ):
        raise ValueError("Invalid ARES rule sections")
    compiled = {}
    for value in definitions:
        if (
            not isinstance(value, dict)
            or not all(
                isinstance(value.get(key), str) for key in ("id", "name", "pattern", "reason")
            )
            or type(value.get("ignore_case")) is not bool
        ):
            raise ValueError("Malformed ARES rule")
        rule_id = value["id"]
        if rule_id in compiled:
            raise ValueError("Duplicate ARES rule")
        compiled[rule_id] = (
            rule_id,
            value["name"],
            value["reason"],
            re.compile(value["pattern"], re.IGNORECASE if value["ignore_case"] else 0),
        )
    active = levels.get("auto-relaxed")
    if (
        not isinstance(active, list)
        or tuple(active) != _RELAXED_IDS
        or any(rule_id not in compiled for rule_id in active)
    ):
        raise ValueError("Invalid relaxed rule membership")
    for key in ("exact", "prefix"):
        if not isinstance(protected.get(key), list) or any(
            not isinstance(path, str) for path in protected[key]
        ):
            raise ValueError("Malformed protected paths")
    if (
        tuple(protected["exact"]) != _PROTECTED_EXACT
        or tuple(protected["prefix"]) != _PROTECTED_PREFIX
    ):
        raise ValueError("Changed protected paths")
    if not all(isinstance(reasons.get(key), str) for key in ("control", "protected")):
        raise ValueError("Malformed path reasons")
    return (
        [compiled[rule_id] for rule_id in active],
        protected["exact"],
        protected["prefix"],
        reasons["control"],
        reasons["protected"],
    )


def _explanation(
    command: str, platform: str, signatures: Mapping[str, object] | None
) -> str | None:
    try:
        entries = signatures["signatures"] if signatures is not None else None
        if not isinstance(entries, list):
            return None
        ranked = sorted(
            enumerate(entries),
            key=lambda pair: (
                {"high": 0, "medium": 1, "low": 2}[pair[1]["severity"]],
                -pair[1]["priority"],
                pair[0],
            ),
        )
        for _, entry in ranked:
            platforms = entry["platforms"]
            if (
                not isinstance(platforms, list)
                or not all(isinstance(value, str) for value in platforms)
                or type(entry["ignore_case"]) is not bool
            ):
                return None
            if "all" not in platforms and platform not in platforms:
                continue
            pattern = re.compile(entry["pattern"], re.IGNORECASE if entry["ignore_case"] else 0)
            if pattern.search(command):
                fields = ("id", "name", "root_cause", "destructive_impact", "safe_alternative")
                if not all(isinstance(entry.get(key), str) for key in fields):
                    return None
                return (
                    f"ARES {entry['id']} {entry['name']}: {entry['root_cause']} "
                    f"Impact: {entry['destructive_impact']} Safer: {entry['safe_alternative']}"
                )
    except (KeyError, TypeError, ValueError, re.error):
        return None
    return None


def decide_tier(tier: PermissionTier, intent: ToolIntent, rules: RelaxedRules) -> TierDecision:
    """Return a pure decision; bad rules or path facts deny controlled calls."""
    try:
        if tier not in PERMISSION_TIERS:
            raise ValueError("Unknown permission tier")
        if intent.status == "invalid":
            return TierDecision(
                Verdict.DENY, f"Malformed or unresolved tool call: {intent.detail}"
            )
        if tier == "full":
            return TierDecision(Verdict.ALLOW)
        active, _, _, control_reason, protected_reason = _validated_rules(rules)
        context = intent.context
        if context.platform not in ("darwin", "linux", "windows"):
            raise ValueError("Unknown host platform")
        if not all(
            isinstance(value, str) and value
            for value in (
                context.host_id,
                context.cwd,
                context.workspace,
                context.home,
                context.shell,
            )
        ):
            raise ValueError("Incomplete host context")
        if not context.control_paths:
            raise ValueError("Missing control paths")
        windows = context.platform == "windows"
        if (
            windows
            and tier == "auto"
            and intent.kind in (ToolKind.EDIT, ToolKind.EXEC)
            and not (
                context.environment.get("SystemRoot") or context.environment.get("SYSTEMROOT")
            )
        ):
            raise ValueError("Missing trusted Windows system root")
        target = intent.target
        if target is not None and target.kind == "unknown":
            return TierDecision(Verdict.DENY, "Target type or final path cannot be confirmed")
        if intent.workspace_path is None or not intent.resolved_control_paths:
            raise ValueError("Missing bound host paths")
        for original, path in zip(
            context.control_paths, intent.resolved_control_paths, strict=True
        ):
            if target and (
                _under(target.path, path, windows)
                or (
                    intent.name in ("grep_search", "find_by_name")
                    and _under(path, target.path, windows)
                )
            ):
                return TierDecision(Verdict.DENY, control_reason, "control-path")
            if intent.command and any(
                _mentions_path(intent.command, form, context)
                for form in (path, _absolute(original, context))
            ):
                return TierDecision(Verdict.DENY, control_reason, "control-path")
            if any(
                _under(candidate, path, windows)
                for candidate in (*intent.command_paths, *intent.command_write_paths)
            ):
                return TierDecision(Verdict.DENY, control_reason, "control-path")
        if intent.unbound_write:
            return TierDecision(Verdict.DENY, intent.detail, "unbound-write")
        if intent.kind == ToolKind.EDIT and target is not None:
            is_protected = target.path in intent.protected_exact_paths or any(
                _under(target.path, value, windows) for value in intent.protected_prefix_paths
            )
            if is_protected and tier == "auto":
                return TierDecision(
                    Verdict.DENY, f"{protected_reason}: {target.path}", "protected-path"
                )
        if intent.kind == ToolKind.EXEC and tier == "auto":
            for candidate in intent.command_write_paths:
                if any(
                    _under(candidate, path, windows) or _mentions_path(candidate, path, context)
                    for path in intent.protected_prefix_paths
                ):
                    return TierDecision(
                        Verdict.DENY, f"{protected_reason}: {candidate}", "protected-path"
                    )
            for rule_id, name, reason, pattern in active:
                if pattern.search(intent.command):
                    explanation = _explanation(intent.command, context.platform, rules.signatures)
                    return TierDecision(Verdict.DENY, f"{name}: {explanation or reason}", rule_id)
        if intent.kind == ToolKind.READ or intent.detail in (
            "framework-question",
            "framework-control",
        ):
            return TierDecision(Verdict.ALLOW)
        if tier == "plan":
            return TierDecision(
                Verdict.DENY, "Plan tier permits only known reads and user questions"
            )
        if intent.status == "unknown" or intent.kind == ToolKind.UNKNOWN:
            if tier == "auto":
                return TierDecision(
                    Verdict.DENY, intent.detail or "This tool cannot yet be checked"
                )
            return TierDecision(
                Verdict.ASK, intent.detail or "Unknown tool needs one-time approval"
            )
        if tier == "ask":
            return TierDecision(Verdict.ASK, "This tool call needs one-time approval")
        if tier == "accept-edits":
            if (
                intent.kind == ToolKind.EDIT
                and target
                and target.kind in ("file", "new-file")
                and _under(target.path, intent.workspace_path.path, windows)
            ):
                return TierDecision(Verdict.ALLOW)
            return TierDecision(Verdict.ASK, "This tool call needs one-time approval")
        return TierDecision(Verdict.ALLOW)
    except Exception as exc:  # noqa: BLE001 - a gate must deny on any check failure
        return TierDecision(Verdict.DENY, f"Permission check failed: {type(exc).__name__}")


__all__ = [
    "AGY_PERMISSION_TIER_LABEL_KEY",
    "DEFAULT_PERMISSION_TIER",
    "NON_FULL_TIERS",
    "PERMISSION_TIERS",
    "PERMISSION_TIERS_CAPABILITY",
    "PI_PERMISSION_TIER_LABEL_KEY",
    "PermissionTier",
    "RelaxedRules",
    "ResolvedPath",
    "TierDecision",
    "ToolIntent",
    "ToolKind",
    "TrustedHostContext",
    "Verdict",
    "adapt_tool_call",
    "decide_tier",
    "load_rules",
    "normalize_permission_tier",
    "plan_visible_tool_names",
    "resolve_local_path",
]
