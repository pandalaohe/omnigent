"""Host-side tool classification and shared five-tier permission decisions.

The caller must bind session, host and revision before using a decision. This
module never treats model-supplied context or annotations as host facts.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import io
import json
import math
import ntpath
import os
import re
import shlex
import stat
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypeAlias
from urllib.parse import urlsplit

PermissionTier: TypeAlias = Literal["ask", "plan", "accept-edits", "auto", "full"]
DenialSource: TypeAlias = Literal["policy", "permission mode", "sandbox", "rule"]
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
    DELEGATE = "delegate"
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


class NativeSupportStatus(StrEnum):
    ATTESTED = "attested_native"
    FAILED = "failed_native"
    OUTSIDE = "outside_native_support"


@dataclass(frozen=True)
class NativeCallBinding:
    scope_id: str
    installation_id: str
    registry_generation: str
    name: str
    arguments_digest: str
    tier_revision: int


@dataclass(frozen=True)
class NativeInputSnapshot:
    path: str
    resolved_path: str | None
    sha256: str | None


@dataclass(frozen=True)
class NativeSupport:
    binding: NativeCallBinding
    status: NativeSupportStatus
    action: Literal["ordinary", "read", "control", "delegate"] = "ordinary"
    reason: str = ""
    outside_kind: str | None = None
    inputs: tuple[NativeInputSnapshot, ...] = ()
    semantics_digest: str = ""


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
    native_tools: Mapping[str, str] = field(default_factory=dict)
    native_binding: NativeCallBinding | None = None
    native_support: NativeSupport | None = None
    local_execution: bool = True
    agy_delegation_status: Literal["proven", "missing", "failed"] = "missing"
    registry_tool_name: str | None = None
    acp_bridge_tool_name: str | None = None
    readable_control_file: tuple[str, str] | None = None  # (owning control, resolved file)
    readable_control_subtrees: tuple[str, ...] = ()


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
    resolved_control_kinds: tuple[str, ...] = ()
    resolved_readable_subtrees: tuple[tuple[str, str, str], ...] = ()
    protected_exact_paths: tuple[str, ...] = ()
    protected_prefix_paths: tuple[str, ...] = ()
    command_paths: tuple[str, ...] = ()
    command_write_paths: tuple[str, ...] = ()
    command_delete_paths: tuple[str, ...] = ()
    target_text: str = ""
    recursive_read_paths: tuple[str, ...] = ()
    command_read_only: bool = False
    command_uncertain: bool = False
    command_opaque: bool = False
    hidden_script: bool = False
    cwd_confirmed: bool = True
    ads_write: str = ""
    gate_fault: str = ""
    unbound_write: bool = False
    native_status: NativeSupportStatus | None = None
    unseen_tool: bool = False


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
# Skill and note trees a read may enter; the guard's own files stay closed.
_READABLE_CONTROL_DIRECTORIES = (".pi", ".agents")
# Directories whose own .json children can carry keys, under home and workspace.
_AGENT_ROOT_PARTS = ((".pi",), (".pi", "agent"), (".agents",))
# A file name carrying one of these can hold a key wherever it sits.
_KEY_NAME_WORDS = ("auth", "credential", "secret", "token", "apikey", "accounts", "models")
_RECURSIVE_READS = ("grep_search", "find_by_name", "grep", "find")
_PI_OPTIONAL_READ_PATH = frozenset(("grep", "find", "ls"))
_PI_PATH_TOOLS = frozenset(("read", "write", "edit", "grep", "find", "ls"))
_PI_BUILTIN_TOOLS = frozenset(
    ("read", "write", "edit", "bash", "powershell", "grep", "find", "ls", "tool_search")
)
_PI_UNICODE_SPACES = re.compile(r"[\u00A0\u2000-\u200A\u202F\u205F\u3000]")
_PI_WINDOWS_SHELL_PATH = re.compile(r"^/(?:mnt/|cygdrive/)?([a-z])(?:/(.*))?$", re.IGNORECASE)
_PI_AM_PM_SPACE = re.compile(r" (AM|PM)\.", re.IGNORECASE)
_POWERSHELL_PROVIDER_PATH = re.compile(r"^(?:env|variable|function|alias):[^:]*$", re.IGNORECASE)
_POWERSHELL_WRITE_CMDLETS = frozenset(
    (
        "set-content",
        "sc",
        "add-content",
        "ac",
        "out-file",
        "clear-content",
        "clc",
        "new-item",
        "ni",
        "set-item",
        "si",
        "clear-item",
        "cli",
        "set-acl",
        "set-itemproperty",
        "sp",
        "clear-itemproperty",
        "clp",
        "remove-itemproperty",
        "rp",
    )
)
# Shell words that read every file under their operand.
_RECURSIVE_SHELL_READS = ("rg", "ag", "ack", "grep", "egrep", "fgrep", "find")
_TRUSTED_VARIABLES = ("HOME", "TMPDIR")
# Past this many reachable cwds a relative target stops being bound.
_MAX_CWDS = 4
_COMMAND_WORDS = re.compile(r"[^\s'\"|;&()<>]+")
# A delete stays refused while its target cannot be bound; other writes proceed.
_DELETE_COMMANDS = ("rm", "rmdir", "unlink", "del", "erase", "rd", "remove-item", "ri", "srm")
_FIND_ACTIONS = (
    "-delete",
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
    "-fls",
    "-fprint",
    "-fprint0",
    "-fprintf",
)
# Commands whose operands are only read, so a control-path mention may proceed.
_READ_ONLY_COMMANDS = frozenset(
    (
        "ag",
        "basename",
        "bat",
        "cat",
        "cut",
        "date",
        "diff",
        "dirname",
        "du",
        "echo",
        "egrep",
        "fgrep",
        "file",
        "grep",
        "head",
        "jq",
        "less",
        "ls",
        "md5sum",
        "more",
        "nl",
        "od",
        "printf",
        "pwd",
        "readlink",
        "realpath",
        "rg",
        "sed",
        "sha256sum",
        "shasum",
        "stat",
        "tail",
        "tr",
        "tree",
        "type",
        "uniq",
        "wc",
        "which",
        "xxd",
    )
)
_CRITICAL_SYSTEM_ROOTS = ("/etc", "/usr", "/bin", "/sbin", "/boot", "/root")
_USER_HOME_ROOTS = ("/home", "/Users")
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
_EXEC_COMMAND = {
    "bash": "command",
    "powershell": "command",
    "sys_os_shell": "command",
    "run_command": "CommandLine",
}
_READ_NO_PATH = frozenset(
    (
        "sys_session_list",
        "sys_session_get_info",
        "sys_session_get_history",
        "sys_session_get_startup_observation_v1",
        "sys_standing_approval_read",
        "sys_agent_list",
        "sys_agent_get",
        "sys_list_models",
        "sys_advise_models",
        "sys_policy_registry",
        "sys_instruction_layers_read",
        "sys_project_list",
        "sys_project_settings_read",
        "sys_session_choice_read",
        "sys_scheduled_task_list",
        "sys_flow_list",
        "sys_host_load",
        "sys_host_restart",
        "sys_terminal_request_access",
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
        "sys_approval_answer",
        "sys_session_send",
        "sys_session_message",
        "sys_session_open",
        "sys_session_rename",
        "sys_session_update",
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
        "sys_session_choice_update",
        "sys_quota_floor",
        "sys_add_policy",
        "sys_session_share",
        "sys_scheduled_task_create",
        "sys_scheduled_task_update",
        "sys_scheduled_task_delete",
        "browser_navigate",
        "browser_fill_saved_login",
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
_NATIVE_GRANT_TOOLS = frozenset(("ask_permission", "ask_custom_permission"))
_NATIVE_SUBAGENT_TOOLS = frozenset(("subagent", "invoke_subagent"))
_NATIVE_BOOTSTRAP_ERRORS = frozenset(
    (
        "Native bootstrap support is missing, malformed or stale",
        "Native launch inputs or bootstrap changed; re-prove the call",
        "Native call has no acknowledged attestation",
    )
)
_POWERSHELL_PROVIDER_PREFIX = re.compile(
    r"^(?:hklm|hkcu|hkcr|hku|hkcc|registry|env|variable|function|alias|cert|wsman):",
    re.IGNORECASE,
)
_POWERSHELL_STREAM_WRITE_CMDLETS = frozenset(
    ("set-content", "sc", "add-content", "ac", "out-file", "clear-content", "clc")
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
        *_PI_OPTIONAL_READ_PATH,
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
_TRUSTED_EXPANSION = re.compile(r"\$(?:\{(HOME|TMPDIR)\}|(HOME|TMPDIR))(?![A-Za-z0-9_])")
_WRAPPER_OPTION = re.compile(r"/c|-[a-z]*c[a-z]*|-command")
_DARWIN_TEMP_ROOT = re.compile(r"^(/(?:private/)?var/folders/[^/]+/[^/]+/T)(?:/|$)")
# Redirections bind a write target; the rest only feed the command.
_REDIRECTIONS = (">", ">>", ">|", "&>", "&>>", ">&", "<", "<<", "<<-", "<<<", "<&", "<>")
_POSIX_OPERATORS = re.compile(
    r"&>>|&>|>>|>\||>&|<<<|<<-|<<|<&|<>|<\(|>\(|&&|\|\||\|&|;;&|;&|;;|[;&|<>()\n]"
)
_WRITE_REDIRECTIONS = (">", ">>", ">|", "&>", "&>>", "<>")
_UNBOUND_DELETE = "A delete needs a bindable target: an unresolved name could delete a tree"


def normalize_permission_tier(raw: object, *, harness: str, legacy: bool) -> PermissionTier:
    from omnigent.acp_cli_harnesses import is_acp_harness

    acp = is_acp_harness(harness)
    if harness not in ("pi", "pi-native", "antigravity-native") and not acp:
        raise ValueError("Unknown harness")
    if legacy and raw is None:
        return "ask"
    if legacy and harness == "antigravity-native" and isinstance(raw, str) and raw in _LEGACY_AG:
        return "auto"
    if not isinstance(raw, str) or raw not in PERMISSION_TIERS:
        raise ValueError("Missing permission tier")
    if acp and raw == "full":
        raise ValueError("Full permission is unavailable for ACP host tiers")
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


def _canonical_json(value: object) -> str:
    """Code-point keys, scalar UTF-8 strings and exact finite binary64 decimals.

    Keep this native proof format paired with native_support.cjs. Authorization
    leases retain BoundToolCall's separate serialization.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError("Native number is not finite binary64") from exc
        if not math.isfinite(number) or (isinstance(value, int) and int(number) != value):
            raise ValueError("Native number is not exact binary64")
        numerator, denominator = number.as_integer_ratio()
        if denominator == 1:
            return str(numerator)
        places = denominator.bit_length() - 1
        digits = str(abs(numerator) * 5**places).zfill(places + 1)
        return ("-" if numerator < 0 else "") + digits[:-places] + "." + digits[-places:]
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return (
            "{"
            + ",".join(
                _canonical_json(key) + ":" + _canonical_json(value[key]) for key in sorted(value)
            )
            + "}"
        )
    raise ValueError("Native proof requires JSON values")


def _native_digest(arguments: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(arguments).encode("utf-8")).hexdigest()


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


def _windows_absolute(path: str, context: TrustedHostContext, *, expand_env: bool = True) -> str:
    if _DEVICE.match(path):
        raise ValueError("Windows device path")
    path = path.replace("/", "\\")
    if path.startswith("~\\") or path == "~":
        path = ntpath.join(context.home, path[2:] if path != "~" else "")

    if expand_env:

        def expand(match: re.Match[str]) -> str:
            name = match.group(1).upper()
            env = {key.upper(): value for key, value in context.environment.items()}
            if name in ("USERPROFILE", "HOME"):
                return context.home
            if name in env:
                return env[name]
            raise ValueError(f"Unknown Windows path variable: {name}")

        path = _WINDOWS_ENV.sub(expand, path)
        if "$" in path or "%" in path:
            raise ValueError(f"Unresolved Windows path variable: {path}")
    drive, tail = ntpath.splitdrive(path)
    if not drive and path.startswith("\\") and not path.startswith("\\\\"):
        raise ValueError("Root-relative Windows path without drive")
    if drive and len(drive) == 2 and not tail.startswith("\\"):
        current = context.drive_cwds.get(drive.upper())
        if not current:
            raise ValueError("Missing drive cwd")
        path = ntpath.join(current, tail)
    elif not drive:
        if not context.cwd:
            raise ValueError("Unconfirmed working directory")
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
    if path.startswith("/"):
        return path
    if not context.cwd:
        raise ValueError("Unconfirmed working directory")
    return os.path.join(context.cwd, path)


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


def _map_windows_shell_path(path: str) -> str:
    """Convert Git Bash / MSYS / Cygwin / WSL drive paths to Windows drive paths."""
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return path
    match = _PI_WINDOWS_SHELL_PATH.match(path)
    if not match:
        return path
    drive = match.group(1).upper()
    suffix = (match.group(2) or "").replace("/", "\\")
    return f"{drive}:\\{suffix}"


def _normalize_pi_path(path: str, *, windows: bool) -> str:
    """Mirror Pi's resolveToCwd preprocessing before host path binding."""
    normalized = _PI_UNICODE_SPACES.sub(" ", path).removeprefix("@")
    if windows:
        normalized = _map_windows_shell_path(normalized)
    if normalized.lower().startswith("file://"):
        raise ValueError("File URI paths are not supported")
    if not normalized:
        raise ValueError("Missing path")
    return normalized


def _lexical_pi_path(path: str, context: TrustedHostContext) -> str:
    """Match Node path.resolve lexical dot-segment collapse before symlink resolution."""
    windows = context.platform == "windows"
    normalized = _normalize_pi_path(path, windows=windows)
    absolute = _absolute(normalized, context)
    if windows:
        return ntpath.normpath(absolute)
    collapsed = os.path.normpath(absolute)
    return "/" + collapsed.lstrip("/") if collapsed.startswith("//") else collapsed


def _pi_read_fallback_variants(path: str) -> tuple[str, ...]:
    """Mirror Pi's resolveReadPath fallback order when the initial spelling is absent."""
    nfd = unicodedata.normalize("NFD", path)
    candidates = (
        _PI_AM_PM_SPACE.sub("\u202f\\1.", path),
        nfd,
        path.replace("'", "\u2019"),
        nfd.replace("'", "\u2019"),
    )
    return tuple(dict.fromkeys(variant for variant in candidates if variant != path))


def _resolve_literal_windows_pi_path(normalized: str, context: TrustedHostContext) -> ResolvedPath:
    """Resolve a Windows Pi path literally (skipping only %VAR% expansion), matching Pi 1.5.0."""
    literal = ntpath.normpath(_windows_absolute(normalized, context, expand_env=False))
    result = context.resolver(literal)
    if not isinstance(result, ResolvedPath) or result.kind not in (
        "file",
        "new-file",
        "directory",
        "unknown",
    ):
        raise ValueError("Invalid host path resolution")
    if not ntpath.isabs(result.path):
        raise ValueError("Host resolution returned a relative path")
    final = _windows_absolute(result.path, context, expand_env=False)
    return ResolvedPath(ntpath.normpath(final), result.kind)


def _resolve_pi_path(
    path: str, context: TrustedHostContext, *, read_fallbacks: bool = False
) -> tuple[ResolvedPath, str, ResolvedPath | None]:
    """Bind OS and Pi lexical/literal/fallback targets independently so no error skips a guard."""
    windows = context.platform == "windows"
    normalized = _normalize_pi_path(path, windows=windows)
    physical: ResolvedPath | None = None
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        physical = _resolve(normalized, context)
    lexical = ""
    lexical_resolved: ResolvedPath | None = None
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        lexical = _lexical_pi_path(path, context)
        lexical_resolved = _resolve(lexical, context)
    literal_resolved: ResolvedPath | None = None
    if windows and "%" in normalized:
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            literal_resolved = _resolve_literal_windows_pi_path(normalized, context)
    primary = physical or literal_resolved or lexical_resolved
    if primary is None:
        return ResolvedPath(path, "unknown"), path, None
    secondary = lexical_resolved
    bound_text = lexical or primary.path
    if literal_resolved is not None and literal_resolved != primary:
        secondary = literal_resolved
        bound_text = literal_resolved.path
    if read_fallbacks and (secondary or primary).kind == "new-file" and lexical:
        for variant in _pi_read_fallback_variants(lexical):
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                candidate = _resolve(variant, context)
                if candidate.kind != "new-file":
                    secondary = candidate
                    bound_text = variant
                    break
    alternate = secondary if secondary is not None and secondary != primary else None
    return primary, bound_text, alternate


def _validate_optional_number(
    arguments: Mapping[str, object],
    key: str,
    *,
    allow_zero: bool = False,
    integer_only: bool = False,
) -> None:
    if key not in arguments:
        return
    value = arguments[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Invalid {key}")
    if (value < 0 if allow_zero else value <= 0) or (
        integer_only and isinstance(value, float) and not value.is_integer()
    ):
        raise ValueError(f"Invalid {key}")


def _validate_optional_bool(arguments: Mapping[str, object], key: str) -> None:
    if key in arguments and not isinstance(arguments[key], bool):
        raise ValueError(f"Invalid {key}")


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


def _is_windows_ads_path(path: str, *, allow_ps_provider: bool = False) -> bool:
    """True when a Windows path names an NTFS alternate data stream after the drive prefix."""
    normalized = path.replace("/", "\\")
    if allow_ps_provider and _POWERSHELL_PROVIDER_PREFIX.match(normalized):
        return False
    _, tail = ntpath.splitdrive(normalized)
    return ":" in tail


def _bind_file_target(
    path: str,
    context: TrustedHostContext,
    *,
    pi_path: bool = False,
    read_fallbacks: bool = False,
) -> tuple[ResolvedPath, str, ResolvedPath | None]:
    """Bind a validated string file path; binding failures degrade to kind='unknown'."""
    if "\x00" in path:
        raise ValueError("Missing or malformed path")
    windows = context.platform == "windows"
    if pi_path:
        # Structural Pi path rejections (file:// URI, empty after @) stay invalid.
        _normalize_pi_path(path, windows=windows)
        try:
            return _resolve_pi_path(path, context, read_fallbacks=read_fallbacks)
        except (OSError, RuntimeError, ValueError):
            return ResolvedPath(path, "unknown"), path, None
    try:
        return _resolve(path, context), path, None
    except (OSError, RuntimeError, ValueError):
        return ResolvedPath(path, "unknown"), path, None


def _checked_agy_delegation(arguments: Mapping[str, object], context: TrustedHostContext) -> bool:
    """Only the tested native self-agent schema may reuse delegation evidence."""
    if set(arguments) - {"TypeName", "Prompt", "Role", "Model", "Workspace"}:
        return False
    if (
        arguments.get("TypeName") != "self"
        or not isinstance(arguments.get("Prompt"), str)
        or not arguments["Prompt"]
    ):
        return False
    if any(
        key in arguments and not isinstance(arguments[key], str)
        for key in ("Role", "Model", "Workspace")
    ):
        return False
    workspace = arguments.get("Workspace")
    if workspace is None or workspace == "inherit":
        return True
    return isinstance(workspace, str) and _resolve(workspace, context) == _resolve(
        context.workspace, context
    )


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
        native = None
        try:
            if (
                context.native_binding is not None
                or context.native_support is not None
                or name in context.native_tools
            ):
                from omnigent.permission_tiers.native_support import adapt_native_call

                native = adapt_native_call(name, arguments, digest, context)
        except (OSError, RuntimeError, TypeError, ValueError):
            native = ToolIntent(
                ToolKind.UNKNOWN,
                name,
                digest,
                context,
                detail="Native support proof could not be checked",
                native_status=NativeSupportStatus.FAILED,
            )
        if native is not None and (
            context.local_execution or native.native_status == NativeSupportStatus.FAILED
        ):
            return native
        if not context.local_execution:
            if name == context.registry_tool_name:
                return ToolIntent(ToolKind.OTHER, name, digest, context, detail="registry-tool")
            return ToolIntent(
                ToolKind.UNKNOWN,
                name,
                digest,
                context,
                status="unknown",
                native_status=native.native_status if native is not None else None,
            )
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
        if name == "update_steps":
            # AG's native progress tool updates only the current turn's checklist.
            return ToolIntent(ToolKind.OTHER, name, digest, context, detail="framework-control")
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
        if name == "sys_host_restart":
            dry_run = arguments.get("dry_run", True)
            if not isinstance(dry_run, bool):
                raise ValueError("dry_run must be a boolean")
            return ToolIntent(ToolKind.READ if dry_run else ToolKind.OTHER, name, digest, context)
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
            target_resolved, _, _ = _bind_file_target(target, context)
            return ToolIntent(ToolKind.OTHER, name, digest, context, target=target_resolved)
        if name in _READ_PATH:
            path = _required_text(arguments, _READ_PATH[name])
            target, bound_text, alternate = _bind_file_target(
                path, context, pi_path=(name in _PI_PATH_TOOLS), read_fallbacks=(name == "read")
            )
            return ToolIntent(
                ToolKind.READ,
                name,
                digest,
                context,
                target=target,
                source=alternate,
                target_text=bound_text,
            )
        if name in _PI_OPTIONAL_READ_PATH:
            if name in ("grep", "find"):
                _required_text(arguments, "pattern")
            if name == "grep":
                if "glob" in arguments and not isinstance(arguments["glob"], str):
                    raise ValueError("Invalid glob")
                _validate_optional_bool(arguments, "ignoreCase")
                _validate_optional_bool(arguments, "literal")
                _validate_optional_number(arguments, "context", allow_zero=True)
            _validate_optional_number(arguments, "limit")
            if "path" not in arguments or arguments["path"] == "":
                path = "."
            elif isinstance(arguments["path"], str):
                path = arguments["path"]
            else:
                raise ValueError("Invalid path")
            target, bound_text, alternate = _bind_file_target(path, context, pi_path=True)
            return ToolIntent(
                ToolKind.READ,
                name,
                digest,
                context,
                target=target,
                source=alternate,
                target_text=bound_text,
            )
        if name == "tool_search":
            if not re.sub(r"[\s\ufeff]+", "", _required_text(arguments, "query")):
                raise ValueError("Empty tool_search query")
            _validate_optional_number(arguments, "limit", integer_only=True)
            return ToolIntent(ToolKind.READ, name, digest, context, detail="tool-search")
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
            path = _required_text(arguments, _EDIT_PATH[name])
            ads_write = (
                f"Windows alternate data stream: {path}"
                if context.platform == "windows" and _is_windows_ads_path(path)
                else ""
            )
            target, bound_text, alternate = _bind_file_target(
                path, context, pi_path=(name in _PI_PATH_TOOLS)
            )
            return ToolIntent(
                ToolKind.EDIT,
                name,
                digest,
                context,
                target=target,
                source=alternate,
                target_text=bound_text,
                ads_write=ads_write,
            )
        if name in _EXEC_COMMAND:
            target = None
            command_opaque = False
            opaque_detail = ""
            if name == "bash" and context.platform == "windows" and not _windows_posix(context):
                context = replace(context, shell="bash")
            elif name == "powershell" and context.platform == "windows":
                context = replace(context, shell="pwsh")
            elif name == "run_command":
                command = _required_text(arguments, _EXEC_COMMAND[name])
                if context.platform == "windows":
                    context = replace(context, shell="pwsh")
                raw_cwd = _required_text(arguments, "Cwd") if "Cwd" in arguments else None
                if arguments.get("RunPersistent") or arguments.get("RequestedTerminalID"):
                    return ToolIntent(
                        ToolKind.EXEC,
                        name,
                        digest,
                        context,
                        command=command,
                        status="unknown",
                        command_opaque=True,
                        cwd_confirmed=False,
                        detail="Persistent terminal working directory cannot be confirmed",
                    )
                cwd_confirmed = True
                if raw_cwd is not None:
                    try:
                        resolved_cwd = _resolve(raw_cwd, context)
                        if resolved_cwd.kind != "directory":
                            raise ValueError("Command Cwd must be a confirmed directory")
                        target = resolved_cwd
                        context = replace(context, cwd=target.path)
                        if context.platform == "windows":
                            drive = ntpath.splitdrive(target.path)[0]
                            if len(drive) == 2:
                                context = replace(
                                    context,
                                    drive_cwds={**context.drive_cwds, drive.upper(): target.path},
                                )
                    except (OSError, RuntimeError, ValueError) as exc:
                        command_opaque = True
                        cwd_confirmed = False
                        opaque_detail = str(exc)
                return ToolIntent(
                    ToolKind.EXEC,
                    name,
                    digest,
                    context,
                    command=command,
                    target=target,
                    command_opaque=command_opaque,
                    cwd_confirmed=cwd_confirmed,
                    detail=opaque_detail,
                )
            return ToolIntent(
                ToolKind.EXEC,
                name,
                digest,
                context,
                command=_required_text(arguments, _EXEC_COMMAND[name]),
                target=target,
                command_opaque=command_opaque,
                detail=opaque_detail,
            )
        if name == "invoke_subagent":
            if context.agy_delegation_status == "failed":
                return ToolIntent(
                    ToolKind.UNKNOWN,
                    name,
                    digest,
                    context,
                    detail="Antigravity child permission coverage failed; verify again",
                    native_status=NativeSupportStatus.FAILED,
                )
            if context.agy_delegation_status == "proven" and _checked_agy_delegation(
                arguments, context
            ):
                return ToolIntent(ToolKind.DELEGATE, name, digest, context)
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
                _bind_file_target(_required_text(arguments, "NotebookPath"), context)[0]
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
            ToolKind.UNKNOWN,
            name,
            digest,
            context,
            status="unknown",
            detail="Unknown tool schema",
            unseen_tool=not name.startswith("sys_") or name == context.acp_bridge_tool_name,
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
        resolved_controls = tuple(_resolve(path, context) for path in context.control_paths)
        controls = tuple(item.path for item in resolved_controls)
        control_kinds = tuple(item.kind for item in resolved_controls)
        readable_subtrees = tuple(
            (_absolute(path, context), resolved.path, resolved.kind)
            for path in context.readable_control_subtrees
            for resolved in (_resolve(path, context),)
        )
        exact, prefixes = _protected_paths(context)
        protected_exact = tuple(_resolve(path, context).path for path in exact)
        protected_prefix = tuple(_resolve(path, context).path for path in prefixes)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return replace(intent, gate_fault=str(exc) or type(exc).__name__)
    bound = replace(
        intent,
        workspace_path=workspace,
        resolved_control_paths=controls,
        resolved_control_kinds=control_kinds,
        resolved_readable_subtrees=readable_subtrees,
        protected_exact_paths=protected_exact,
        protected_prefix_paths=protected_prefix,
    )
    if not bound.command:
        return bound
    command_text = bound.command
    command_opaque = bound.command_opaque
    hidden_script = bound.hidden_script
    opaque_detail = bound.detail if command_opaque else ""
    try:
        command_text = _command_text(bound.command)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        command_opaque = True
        hidden_script = True
        opaque_detail = opaque_detail or str(exc)
    powershell_command = bool(
        command_text
        and bound.name in ("run_command", "powershell")
        and context.platform == "windows"
    )
    version_query = powershell_command and is_powershell_version_query(command_text)
    provider_seen: list[bool] = []
    paths: tuple[str, ...] = ()
    recursive_paths: tuple[str, ...] = ()
    command_context = (
        bound.context if bound.cwd_confirmed else replace(bound.context, cwd="", drive_cwds={})
    )
    if not version_query:
        try:
            paths, recursive_paths, paths_opaque = _command_paths(
                command_text,
                command_context,
                agy_powershell=powershell_command,
                provider_seen=provider_seen,
            )
            if paths_opaque:
                command_opaque = True
                opaque_detail = opaque_detail or paths_opaque
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            command_opaque = True
            opaque_detail = opaque_detail or str(exc)
    writes = _ShellWrites()
    writes_failed_detail = ""
    try:
        writes = _shell_writes(
            command_text,
            command_context,
            0,
            windows_posix_executor=_windows_posix(command_context),
        )
    except _UnboundShellWrite as exc:
        writes = _ShellWrites(unbound_write=str(exc), read_only=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        command_opaque = True
        writes_failed_detail = str(exc)
        writes = _ShellWrites(read_only=False)
    if writes.opaque:
        command_opaque = True
        opaque_detail = opaque_detail or writes.opaque
    if writes_failed_detail:
        opaque_detail = opaque_detail or writes_failed_detail
    if writes.opaque_stdin:
        hidden_script = True
    unbound_reason = writes.unbound_delete or writes.unbound_write
    return replace(
        bound,
        command=command_text,
        command_paths=paths,
        recursive_read_paths=recursive_paths,
        command_write_paths=writes.paths,
        command_delete_paths=writes.deletes,
        command_read_only=writes.read_only,
        command_uncertain=powershell_command
        and not version_query
        and (bool(re.search(r"[$`]|\[[\w.]+\]::", command_text)) or bool(provider_seen)),
        command_opaque=command_opaque,
        hidden_script=hidden_script,
        ads_write=writes.ads_write or bound.ads_write,
        status="unknown" if unbound_reason or writes.opaque_stdin else bound.status,
        detail=unbound_reason or writes.opaque_stdin or opaque_detail or bound.detail,
        unbound_write=bool(unbound_reason),
    )


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
_HEREDOC_SHELLS = ("sh", "bash", "zsh", "dash")
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
            end, inner, _, _ = _shell_scan(
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


def _shell_scan(
    text: str,
    index: int = 0,
    closer: str = "",
    *,
    binding_paths: bool = False,
    keep_comments: bool = False,
) -> tuple[int, str, list[str], bool]:
    """Return end, executable text, substitution bodies and whether all heredocs closed.

    Quoted heredoc data is omitted unless a shell interpreter reads it. Quotes in
    unquoted heredoc bodies are literal; case patterns do not close substitutions.
    For path binding, leave an unresolved word in place of each substitution.
    """
    kept: list[str] = []
    bodies: list[str] = []
    quote = ""
    depth = 0
    cases = 0
    start = index
    command_start = index
    heredocs: list[tuple[str, bool, bool, bool]] = []
    heredocs_closed = True
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
                found = False
                while index < len(text):
                    newline = text.find("\n", index)
                    line_end = len(text) if newline < 0 else newline
                    line = text[index:line_end]
                    next_index = len(text) if newline < 0 else newline + 1
                    if (line.lstrip("\t") if tabs else line) == terminator:
                        body_end = index
                        index = next_index
                        found = True
                        break
                    index = next_index
                    body_end = index
                heredocs_closed = heredocs_closed and found
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
            return index, "".join(kept), bodies, heredocs_closed and not heredocs
        if not quote and char == "#" and (index == start or text[index - 1] in " \t\n;&|()<>"):
            newline = text.find("\n", index)
            comment_end = len(text) if newline < 0 else newline
            if keep_comments:
                kept.append(text[index:comment_end])
            index = comment_end
            continue
        if closer == ")" and not quote:
            if _word_at(text, index, "case", start) and _command_position(text, index, start):
                cases += 1
            elif cases and _word_at(text, index, "esac", start):
                cases -= 1
            elif char in "()":
                if char == ")" and not depth and not cases:
                    return index, "".join(kept), bodies, heredocs_closed and not heredocs
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
        elif not quote and text.startswith("<<<", index):
            kept.append("<<<")
            index += 3
            continue
        elif not quote and text.startswith("<<", index):
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
            end, body, _, _ = _shell_scan(
                text, index + (1 if backtick else 2), "`" if backtick else ")"
            )
            if end >= len(text):
                break  # unterminated: the shell rejects the command
            if binding_paths and text.startswith("$((", index):
                bodies.extend(_shell_scan(body, binding_paths=True)[2])
            else:
                bodies.append(re.sub(r"\\([`$\\])", r"\1", body) if backtick else body)
            kept.append("$?" if binding_paths else text[index : end + 1])
            index = end + 1
            continue
        kept.append(char)
        index += 1
    return index, "".join(kept), bodies, heredocs_closed and not heredocs


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


_GIT_GLOBAL_FLAGS = (
    "-p",
    "--paginate",
    "-P",
    "--no-pager",
    "--bare",
    "--no-replace-objects",
    "--literal-pathspecs",
    "--glob-pathspecs",
    "--noglob-pathspecs",
    "--icase-pathspecs",
    "--no-optional-locks",
)


def _cmd_raw_token_uncheckable(raw: str) -> bool:
    """True when a cmd token hides a newline or single-quoted operator/metacharacter."""
    if raw != "\n" and ("\r" in raw or "\n" in raw):
        return True
    if "'" not in raw:
        return False
    parts = re.findall(r'"[^"]*"|\'[^\']*\'|[^"\']+', raw)
    if "".join(parts) != raw:
        return True
    unquoted_single = False
    has_double = False
    for part in parts:
        if part.startswith('"'):
            has_double = True
        elif part.startswith("'"):
            unquoted_single = True
            if any(char in part for char in '&|<>;^%!"'):
                return True
        elif "'" in part:
            return True
    return unquoted_single and has_double


def _windows_raw_token_unsafe(raw: str, dialect: str) -> bool:
    """True when shell-specific syntax could hide expansions, escapes or operators."""
    if dialect not in ("pwsh", "powershell", "cmd", *_HEREDOC_SHELLS):
        return True
    if dialect == "cmd":
        # _windows_command_paths already checked cmd quoting; single quotes are split below.
        return any(char in raw for char in "$`^!{}\r\n%")
    parts = re.findall(r'"[^"]*"|\'[^\']*\'|[^"\']+', raw)
    if "".join(parts) != raw:
        return True
    for part in parts:
        if part.startswith("'"):
            continue
        if part.startswith('"'):
            if any(char in part for char in "$`") or (dialect in _HEREDOC_SHELLS and "\\" in part):
                return True
        elif any(char in part for char in "$`^!{}\r\n") or (
            dialect in _HEREDOC_SHELLS and "\\" in part
        ):
            return True
    return False


def _expand_cmd_single_quotes(
    tokens: list[str], raw_tokens: list[str]
) -> tuple[list[str], list[str]]:
    """Split unquoted `'...'` tokens on whitespace because cmd.exe does not quote `'`."""
    expanded_tokens: list[str] = []
    expanded_raws: list[str] = []
    for token, raw in zip(tokens, raw_tokens, strict=True):
        if "'" in raw and '"' not in raw:
            split = raw.split()
            expanded_tokens.extend(split)
            expanded_raws.extend(split)
        else:
            expanded_tokens.append(token)
            expanded_raws.append(raw)
    return expanded_tokens, expanded_raws


def _conservative_operands(
    tokens: list[str],
    *,
    keep_after_double_dash: bool = False,
    skip: set[int] | frozenset[int] = frozenset(),
) -> list[str]:
    """Keep non-option tokens, plus post-`--` operands when a command takes pathspecs."""
    after_dash = False
    operands: list[str] = []
    for index, value in enumerate(tokens):
        if keep_after_double_dash and not after_dash and value == "--":
            after_dash = True
            continue
        if index in skip:
            continue
        if after_dash or not value.startswith("-"):
            operands.append(value)
    return operands


def _git_subcommand(
    tokens: list[str],
) -> tuple[str, int, list[str], set[int], set[int], list[str]] | None:
    """Locate a git subcommand after recognized global options, tracking -C steps."""
    candidates = tokens.copy()
    chdirs: list[str] = []
    chdir_indices: set[int] = set()
    files: set[int] = set()
    index = 1
    while index < len(tokens):
        value = tokens[index]
        option, equals, attached = value.partition("=")
        if value == "-C":
            index += 1
            if index == len(tokens):
                return None
            chdirs.append(tokens[index])
            chdir_indices.add(index)
            index += 1
        elif value.startswith("-C") and len(value) > 2:
            chdirs.append(value[2:])
            chdir_indices.add(index)
            index += 1
        elif option in ("--git-dir", "--work-tree"):
            if not equals:
                index += 1
                if index == len(tokens):
                    return None
                attached = tokens[index]
            candidates[index] = attached
            files.add(index)
            index += 1
        elif value in _GIT_GLOBAL_FLAGS:
            index += 1
        elif value.startswith("-"):
            return None
        else:
            return value, index + 1, chdirs, chdir_indices, files, candidates
    return None


def _posix_raw_has_escape(raw: str) -> bool:
    """True when a POSIX token uses a backslash that changes shell lexing."""
    quote = ""
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote == "'":
            if char == "'":
                quote = ""
        elif quote == '"':
            if char == "\\":
                if index + 1 >= len(raw) or raw[index + 1] in ("\\", '"', "$", "`", "\n"):
                    return True
                index += 1
            elif char == '"':
                quote = ""
        elif char == "\\":
            return True
        elif char in ("'", '"'):
            quote = char
        index += 1
    return False


def _windows_path_operands(
    tokens: list[str], raw_tokens: list[str], shell: str, *, posix_executor: bool
) -> tuple[list[str], list[str]]:
    """Exclude established data roles; unknown syntax keeps conservative candidates."""
    dialect = _command_name(shell)
    if posix_executor and dialect in _HEREDOC_SHELLS:
        offset = 0
        while offset < len(raw_tokens) and (
            raw_tokens[offset] in _COMMAND_LEADS
            or re.match(r"[A-Za-z_][A-Za-z0-9_]*=", raw_tokens[offset])
        ):
            offset += 1
        tokens, raw_tokens = tokens[offset:], raw_tokens[offset:]
        if tokens[:1] == ["echo"]:
            return [], tokens[:1]
        if (
            len(tokens) > 1
            and tokens[0] == "printf"
            and not re.search(r"[$`*?\[{}]", raw_tokens[1])
            and not _script_word(raw_tokens[1]).startswith("-")
        ) or (len(tokens) == 2 and tokens[0] == "date" and tokens[1].startswith("+%")):
            return [], tokens[:1]
    if not tokens:
        return [], []
    if dialect in _HEREDOC_SHELLS and tokens[0] == "echo":
        if all(not re.search(r"\$(?!\?)|[`{}\r\n]", raw) for raw in raw_tokens[1:]):
            return [], tokens[:1]
    original_tokens = tokens
    command = tokens[0].lower().removesuffix(".exe")
    relaxed_quotes = command == "git" or tokens[0].lower() in ("echo", "write-output")
    if dialect == "cmd":
        split_tokens, split_raws = _expand_cmd_single_quotes(tokens, raw_tokens)
    else:
        split_tokens, split_raws = tokens, raw_tokens
    if relaxed_quotes:
        tokens, raw_tokens = split_tokens, split_raws
    git_info = _git_subcommand(tokens) if command == "git" else None
    git_chdirs = git_info[2] if git_info is not None else []
    git_chdir_indices = git_info[3] if git_info is not None else frozenset()
    git_prefix_files = git_info[4] if git_info is not None else frozenset()
    git_candidates = git_info[5] if git_info is not None else tokens
    if relaxed_quotes:
        conservative = _conservative_operands(
            git_candidates,
            keep_after_double_dash=command == "git",
            skip=git_chdir_indices,
        )
        if git_prefix_files:
            conservative = list(
                dict.fromkeys(
                    [*(git_candidates[i] for i in sorted(git_prefix_files)), *conservative]
                )
            )
        if dialect == "cmd":
            conservative = list(
                dict.fromkeys(
                    [
                        *conservative,
                        *_conservative_operands(
                            original_tokens, keep_after_double_dash=command == "git"
                        ),
                    ]
                )
            )
    else:
        conservative = _conservative_operands(original_tokens)
        if dialect == "cmd" and split_tokens != original_tokens:
            conservative = list(
                dict.fromkeys([*conservative, *_conservative_operands(split_tokens)])
            )
    if (
        any(_windows_raw_token_unsafe(raw, dialect) for raw in raw_tokens)
        if relaxed_quotes
        else dialect not in ("pwsh", "powershell", "cmd", *_HEREDOC_SHELLS)
        or any(
            any(char in raw for char in "$`^!{}\r\n")
            or (dialect == "cmd" and any(char in raw for char in "%'"))
            or (dialect in _HEREDOC_SHELLS and _posix_raw_has_escape(raw))
            for raw in raw_tokens
        )
    ):
        return git_chdirs, conservative
    if tokens[0].lower() in ("echo", "write-output"):
        return [], (
            conservative if any(value.startswith("-") for value in tokens[1:]) else tokens[:1]
        )
    candidates = tokens.copy()
    chdirs: list[str] = []
    chdir_indices: set[int] = set()
    files: set[int] = set()
    if command == "get-date":
        start = 1
        data_options = ("-Format", "-format")
        file_options = ()
        flags = ()
    elif command in ("grep", "egrep", "fgrep", "rg"):
        start = 1
        data_options = ("-e", "--regexp")
        file_options = ("-f", "--file")
        flags = (
            "-i",
            "--ignore-case",
            "-n",
            "-r",
            "-R",
            "-E",
            "-F",
            "-l",
            "-q",
            "--fixed-strings",
        )
    elif command == "git" and git_info is not None and git_info[0] in ("commit", "tag"):
        subcommand, start, chdirs, chdir_indices, files, candidates = git_info
        data_options = ("-m", "--message")
        file_options = ("-F", "--file")
        flags = (
            (
                "-a",
                "--all",
                "-s",
                "--signoff",
                "--no-signoff",
                "--amend",
                "--no-edit",
                "-n",
                "--no-verify",
                "--no-gpg-sign",
                "--allow-empty",
                "--allow-empty-message",
                "-o",
                "--only",
                "-i",
                "--include",
                "--reset-author",
                "-q",
                "--quiet",
                "-v",
                "--verbose",
            )
            if subcommand == "commit"
            else (
                "-a",
                "--annotate",
                "-s",
                "--sign",
                "--no-sign",
                "-f",
                "--force",
                "-d",
                "--delete",
                "-v",
                "--verify",
                "-l",
                "--list",
            )
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
        return git_chdirs, conservative
    data = set()
    certain = True
    options = True
    explicit_pattern = False
    positionals = []
    index = start
    while index < len(tokens):
        value = tokens[index]
        option, equals, attached = value.partition("=")
        if options and value == "--":
            options = False
        elif not options and command == "git":
            files.add(index)
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
                explicit_pattern = command in ("grep", "egrep", "fgrep", "rg") or explicit_pattern
            elif command != "curl" or (
                urlsplit(attached).scheme in ("http", "https") and urlsplit(attached).hostname
            ):
                data.add(index)
                explicit_pattern = True
        elif (
            options
            and command == "git"
            and value.startswith("-")
            and not value.startswith("--")
            and len(value) > 2
        ):
            pos = 1
            while pos < len(value):
                short = f"-{value[pos]}"
                if short in flags:
                    pos += 1
                elif short in data_options:
                    if pos + 1 == len(value):
                        index += 1
                        if index == len(tokens):
                            certain = False
                        else:
                            data.add(index)
                    else:
                        data.add(index)
                    break
                elif short in file_options:
                    if pos + 1 == len(value):
                        index += 1
                        if index == len(tokens):
                            certain = False
                        else:
                            candidates[index] = tokens[index]
                            files.add(index)
                    else:
                        candidates[index] = value[pos + 1 :]
                        files.add(index)
                    break
                else:
                    certain = False
                    break
        elif options and any(
            len(option) == 2 and value.startswith(option) and len(value) > 2
            for option in file_options
        ):
            candidates[index] = value[2:]
            files.add(index)
            explicit_pattern = command in ("grep", "egrep", "fgrep", "rg") or explicit_pattern
        elif options and value.startswith("-"):
            certain &= value in flags
        elif command in ("grep", "egrep", "fgrep", "rg"):
            positionals.append(index)
        elif command == "curl":
            url = urlsplit(value)
            if url.scheme in ("http", "https") and url.hostname:
                data.add(index)
        index += 1
    # Explicit patterns apply to all positionals, even those preceding the option.
    if positionals and not explicit_pattern:
        data.add(positionals[0])
    selected = [
        value
        for index, value in enumerate(candidates)
        if index not in chdir_indices
        and (not certain or index not in data)
        and (index in files or not value.startswith("-"))
    ]
    if not certain and dialect == "cmd" and command == "git":
        selected = list(dict.fromkeys([*selected, *conservative]))
    return chdirs, selected


def is_powershell_version_query(command: str) -> bool:
    """Recognize an isolated read of PowerShell's built-in version metadata."""
    return (
        re.fullmatch(
            r"\s*\$PSVersionTable(?:\.PSVersion(?:\.ToString\(\))?)?\s*",
            command,
            re.IGNORECASE,
        )
        is not None
    )


class _UnresolvedWindowsVariable(ValueError):
    """A shell operand depends on a value unavailable to the host policy."""


_WINDOWS_SHELL_TOKENS = (
    r"&>>|&>|>>|>\||>&|<<<|<<-|<<|<&|<>|&&|\|\||\|&|[;&|<>()\n]|"
    r"""(?:"[^"]*"|'[^']*'|[^\s"'|;&<>()])+"""
)
_CMD_SHELL_TOKENS = (
    r"&>>|&>|>>|>\||>&|<<<|<<-|<<|<&|<>|&&|\|\||\|&|[;&|<>()\n]|"
    r"""(?:"[^"]*"|[^\s"|;&<>()])+"""
)


def _windows_command_paths(
    command: str,
    context: TrustedHostContext,
    depth: int,
    *,
    posix_executor: bool,
    assigned: frozenset[str] = frozenset(),
    agy_powershell: bool = False,
    provider_seen: list[bool] | None = None,
) -> tuple[tuple[str, ...], str]:
    if depth > 3:
        return (), "Shell wrapper nesting cannot be checked"
    paths: list[str] = []
    opaque = ""
    windows_posix = posix_executor and _windows_posix(context)
    if windows_posix:
        assigned |= _shell_assignments(command, context)
        end, outer, bodies, heredocs_closed = _shell_scan(command, binding_paths=True)
        if end != len(command) or not heredocs_closed:
            opaque = opaque or "Uncheckable POSIX shell expansion"
        else:
            command = outer
        for body in bodies:
            sub_paths, sub_opaque = _windows_command_paths(
                body, context, depth + 1, posix_executor=True, assigned=assigned
            )
            paths.extend(sub_paths)
            opaque = opaque or sub_opaque
    raw_tokens = []
    end = 0
    for match in re.finditer(_WINDOWS_SHELL_TOKENS, command):
        if command[end : match.start()].strip():
            opaque = opaque or "Uncheckable Windows shell quoting"
        raw_tokens.append(match[0])
        end = match.end()
    if command[end:].strip():
        opaque = opaque or "Uncheckable Windows shell quoting"
    tokens = [
        re.sub(
            r""""([^"]*)"|'([^']*)'""",
            lambda match: next(value for value in match.groups() if value is not None),
            raw,
        )
        for raw in raw_tokens
    ]
    if _command_name(context.shell) == "cmd":
        if any(_cmd_raw_token_uncheckable(raw) for raw in raw_tokens):
            opaque = opaque or "Uncheckable Windows shell quoting"
    segments: list[list[str]] = [[]]
    raw_segments: list[list[str]] = [[]]
    candidates: list[tuple[str, TrustedHostContext]] = []
    redirect_candidates: list[tuple[str, TrustedHostContext]] = []
    index = 0
    while index < len(tokens):
        raw = raw_tokens[index]
        brace = windows_posix and raw in ("{", "}") and not segments[-1]
        if brace or (raw and all(char in ";&|()\n" for char in raw)):
            segments.append([])
            raw_segments.append([])
        elif raw in (">", ">>", ">|", "&>", "&>>", ">&", "<", "<<", "<<-", "<<<", "<&", "<>"):
            index += 1
            if index == len(tokens) or re.fullmatch(r"[;&|<>()\n]+", raw_tokens[index]):
                opaque = opaque or "Missing shell redirection target"
                if index < len(tokens):
                    segments.append([])
                    raw_segments.append([])
                continue
            redirect_candidates.append((tokens[index], context))
        else:
            segments[-1].append(tokens[index])
            raw_segments[-1].append(raw)
        index += 1
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
            inner_paths, inner_opaque = _windows_command_paths(
                inner,
                replace(context, shell=shell),
                depth + 1,
                posix_executor=posix_executor,
                assigned=assigned,
                agy_powershell=agy_powershell and shell in ("powershell", "pwsh"),
                provider_seen=provider_seen,
            )
            paths.extend(inner_paths)
            opaque = opaque or inner_opaque
            candidates.extend(
                (value, context) for value in segment[:option_index] if not value.startswith("-")
            )
            if shell in _HEREDOC_SHELLS:
                candidates.extend((value, context) for value in segment[option_index + 2 :])
        else:
            if agy_powershell and _command_name(context.shell) == "pwsh":
                candidates.extend(
                    (option[1], context)
                    for token in segment
                    if (
                        option := re.fullmatch(
                            r"-(?:path|literalpath|filepath|destination):(.+)", token, re.I
                        )
                    )
                )
                if (
                    len(segment) == 3
                    and segment[0].lower() in ("git", "git.exe")
                    and segment[1] == "show"
                    and re.fullmatch(r"[A-Za-z0-9_][\w./~^@{}-]+:[^:]+", segment[2])
                ):
                    # A git tree object is addressed within the repository, not as an OS path.
                    candidates.append((segment[0], context))
                    continue
                if (
                    len(segment) == 6
                    and segment[0].casefold() == "where-object"
                    and raw_segment[1] == "{"
                    and re.fullmatch(r"\$_(?:\.\w+)?", raw_segment[2])
                    and segment[3].casefold() in ("-match", "-notmatch", "-eq", "-ne", "-like")
                    and raw_segment[4].startswith(("'", '"'))
                    and raw_segment[5] == "}"
                ):
                    candidates.append((segment[0], context))
                    continue
                # In expression position, a bare .NET member has no filesystem operand.
                # The complete command still requires approval.
                static_member = r"\[[\w.]+\]::\w+"
                if re.fullmatch(static_member, raw_segment[0]):
                    if (
                        len(raw_segment) == 3
                        and raw_segment[1] == "="
                        and re.fullmatch(static_member, raw_segment[2])
                    ):
                        continue
                    segment, raw_segment = segment[1:], raw_segment[1:]
            try:
                git_chdirs, segment_candidates = _windows_path_operands(
                    segment, raw_segment, context.shell, posix_executor=posix_executor
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                opaque = opaque or str(exc)
                git_chdirs = ()
                segment_candidates = tuple(
                    value for value in segment[1:] if value and not value.startswith("-")
                )
            target_context = context
            for chdir_token in git_chdirs:
                if not chdir_token:
                    continue
                try:
                    expanded_dir = _expand_trusted(chdir_token, target_context, assigned)
                    if expanded_dir is None:
                        raise _UnresolvedWindowsVariable(
                            f"Unresolved Windows path variable: {chdir_token}"
                        )
                    resolved_dir = _resolve(expanded_dir, target_context).path
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    opaque = opaque or str(exc)
                    break
                paths.append(resolved_dir)
                target_context = replace(target_context, cwd=resolved_dir)
                drive = ntpath.splitdrive(resolved_dir)[0]
                if len(drive) == 2:
                    target_context = replace(
                        target_context,
                        drive_cwds={**target_context.drive_cwds, drive.upper(): resolved_dir},
                    )
            candidates.extend((value, target_context) for value in segment_candidates)
    for is_redirect, group in ((True, redirect_candidates), (False, candidates)):
        for token, token_context in group:
            if not token:
                continue
            try:
                try:
                    expanded = _expand_trusted(token, token_context, assigned)
                except _UnresolvedWindowsVariable:
                    if not agy_powershell or "$" not in token:
                        raise
                    expanded = None
                if expanded is None:
                    if agy_powershell and re.search(r"[$`]", token):
                        if _DEVICE.match(token):
                            raise ValueError("Windows device path")
                        without_scope = re.sub(
                            r"\$(?:env|global|local|private|script|using):", "$", token, flags=re.I
                        )
                        if ":" in ntpath.splitdrive(without_scope)[1]:
                            raise ValueError("Windows alternate data stream")
                        continue
                    raise _UnresolvedWindowsVariable(f"Unresolved Windows path variable: {token}")
                token = expanded
                if not is_redirect:
                    if agy_powershell and _POWERSHELL_PROVIDER_PATH.match(token):
                        if provider_seen is not None:
                            provider_seen.append(True)
                        continue
                    if windows_posix:
                        token = _map_windows_shell_path(token)
                paths.append(_resolve(token, token_context).path)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                opaque = opaque or str(exc)
    return tuple(paths), opaque


def _write_tokens(
    command: str, *, windows: bool, cmd_shell: bool = False
) -> tuple[list[str], list[str]]:
    """Tokens plus their original text, for binding shell write targets."""
    if windows:
        pattern = _CMD_SHELL_TOKENS if cmd_shell else _WINDOWS_SHELL_TOKENS
        quote_pattern = r'"([^"]*)"' if cmd_shell else r""""([^"]*)"|'([^']*)'"""
        raw_tokens = re.findall(pattern, command)
        tokens = [
            re.sub(
                quote_pattern,
                lambda match: next(value for value in match.groups() if value is not None),
                token,
            )
            for token in raw_tokens
        ]
        return tokens, raw_tokens
    stream = io.StringIO(command)
    lexer = shlex.shlex(stream, posix=True, punctuation_chars=";&|<>()\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lookahead = getattr(lexer, "_pushback_chars", None)
    if lookahead is None:
        raise ValueError("Shell token positions unavailable")
    tokens: list[str] = []
    raw_tokens: list[str] = []
    prev_token_end = -1
    while True:
        # Exclude shlex's lookahead to retain each operator's original quoting.
        start = stream.tell() - len(lookahead)
        token = lexer.get_token()
        if token is None:
            break
        end = stream.tell() - len(lookahead)
        pos = start
        saw_comment_newline = False
        while pos < end:
            while pos < end and command[pos] in " \t\r":
                pos += 1
            if pos < end and command[pos] == "#":
                newline = command.find("\n", pos, end)
                if newline >= 0:
                    saw_comment_newline = True
                    pos = newline + 1
                    continue
            break
        if saw_comment_newline:
            if not (tokens and _redirect_parts(raw_tokens[-1]) is not None):
                tokens.append("\n")
                raw_tokens.append("\n")
            prev_token_end = -1
        token_start = pos
        token_end = end
        while token_end > token_start and command[token_end - 1] in " \t\r":
            token_end -= 1
        raw = command[token_start:token_end]
        adjacent = prev_token_end == token_start
        if (
            token
            and all(char in ";|&<>()\n" for char in token)
            and raw.startswith(token)
            and (len(raw) == len(token) or raw[len(token)] == "#")
        ):
            parts = _POSIX_OPERATORS.findall(token)
            for part_index, part in enumerate(parts):
                if (
                    part_index == 0
                    and adjacent
                    and tokens
                    and part in _REDIRECTIONS
                    and raw_tokens[-1].isascii()
                    and raw_tokens[-1].isdigit()
                ):
                    tokens[-1] = raw_tokens[-1] + part
                    raw_tokens[-1] = raw_tokens[-1] + part
                    continue
                tokens.append(part)
                raw_tokens.append(part)
            prev_token_end = token_start + len(token)
            continue
        if (
            adjacent
            and tokens
            and raw == "-"
            and tokens[-1].endswith("<<")
            and raw_tokens[-1] == tokens[-1]
            and (len(tokens[-1]) == 2 or (tokens[-1][:-2].isascii() and tokens[-1][:-2].isdigit()))
        ):
            tokens[-1] += "-"
            raw_tokens[-1] += "-"
            prev_token_end = token_end
            continue
        tokens.append(token)
        raw_tokens.append(raw)
        prev_token_end = token_end
    return tokens, raw_tokens


def _command_name(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")


def _windows_posix(context: TrustedHostContext) -> bool:
    return context.platform == "windows" and _command_name(context.shell) in _HEREDOC_SHELLS


def _command_offset(tokens: list[str], offset: int = 0) -> int:
    """Offset of the command word after assignments and command-lead keywords."""
    while offset < len(tokens) and (
        tokens[offset] in ("if", "then", "else", "elif", "do", "!", "{", "command", "builtin")
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[offset], re.DOTALL)
    ):
        execution = tokens[offset] == "command"
        offset += 1
        if execution:
            while offset < len(tokens) and tokens[offset] == "-p":
                offset += 1
            if offset < len(tokens) and tokens[offset] == "--":
                offset += 1
    return offset


def _takes_shell_option_arg(option: str) -> bool:
    return option in ("--rcfile", "--init-file") or (
        len(option) >= 2
        and option[0] in ("-", "+")
        and option[-1] in ("o", "O")
        and option[1:].isalpha()
    )


def _wrapped_command(
    tokens: list[str], raw_tokens: list[str], index: int, *, writes_only: bool
) -> tuple[str, str] | None:
    """The shell dialect and inner command when a token opens `-c` execution."""
    shell = _command_name(tokens[index])
    if shell not in (*_HEREDOC_SHELLS, "cmd", "powershell", "pwsh"):
        return None
    option_index = index + 1
    while option_index < len(tokens) - 1:
        option = tokens[option_index]
        if shell in _HEREDOC_SHELLS:
            if option in ("--", "-"):
                return None
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", option):
                return shell, tokens[option_index + 1]
            if _takes_shell_option_arg(option):
                option_index += 2
                continue
            if not option.startswith(("-", "+")):
                return None
            option_index += 1
            continue
        lowered = option.lower()
        if not _WRAPPER_OPTION.fullmatch(lowered):
            if not lowered.startswith(("-", "/")):
                return None
            option_index += 1
            continue
        rest = tokens[option_index + 1 :]
        keep = raw_tokens[option_index + 1 :] if writes_only and len(rest) > 1 else rest
        return shell, " ".join(keep)
    return None


def _command_paths(
    command: str,
    context: TrustedHostContext,
    *,
    powershell: bool = False,
    depth: int = 0,
    agy_powershell: bool = False,
    provider_seen: list[bool] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """Bind direct path operands per token/segment; retain bindable evidence alongside opacity."""
    if depth > 3:
        return (), (), "Shell wrapper nesting cannot be checked"
    if context.platform == "windows":
        win_paths, win_opaque = _windows_command_paths(
            command,
            context,
            depth,
            posix_executor=_windows_posix(context),
            agy_powershell=agy_powershell,
            provider_seen=provider_seen,
        )
        return win_paths, (), win_opaque
    command = command.replace("\\\n", "")
    opaque = ""
    try:
        tokens, _raw = _write_tokens(command, windows=powershell)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        opaque = str(exc)
        tokens = _COMMAND_WORDS.findall(command)
    paths: list[str] = []
    recursive_paths: list[str] = []
    for index, _token in enumerate(tokens):
        wrapped = _wrapped_command(tokens, tokens, index, writes_only=False)
        if wrapped is None:
            continue
        shell, inner = wrapped
        inner_paths, inner_recursive, inner_opaque = _command_paths(
            inner,
            replace(context, shell=shell),
            powershell=shell in ("powershell", "pwsh"),
            depth=depth + 1,
        )
        paths.extend(inner_paths)
        recursive_paths.extend(inner_recursive)
        opaque = opaque or inner_opaque
    segments: list[list[str]] = [[]]
    for token in tokens:
        if not token or token in (";", "&&", "||", "|", ">", ">>", "<", "(", ")", "\n"):
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token)
    for segment in segments:
        offset = _command_offset(segment)
        reading = _command_name(segment[offset]) if offset < len(segment) else ""
        for token in segment:
            if token.startswith("-"):
                continue
            if token == "$HOME" or token.startswith("$HOME/"):
                token = context.home + token[5:]
            elif token == "${HOME}" or token.startswith("${HOME}/"):
                token = context.home + token[7:]
            if "$" in token or "=" in token:
                continue
            try:
                # Globs stay literal so a read of a globbed directory is still visible.
                resolved_path = _resolve(token, context).path
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                opaque = opaque or str(exc)
                continue
            paths.append(resolved_path)
            if reading in _RECURSIVE_SHELL_READS:
                recursive_paths.append(resolved_path)
    return tuple(paths), tuple(recursive_paths), opaque


@dataclass(frozen=True)
class _ShellWrites:
    """Write targets bound from one shell command, the deletes, and whether it only reads."""

    paths: tuple[str, ...] = ()
    deletes: tuple[str, ...] = ()
    unbound_delete: str = ""
    read_only: bool = True
    opaque_stdin: str = ""
    unbound_write: str = ""
    opaque: str = ""
    ads_write: str = ""

    def merged(self, other: _ShellWrites) -> _ShellWrites:
        return _ShellWrites(
            self.paths + other.paths,
            self.deletes + other.deletes,
            self.unbound_delete or other.unbound_delete,
            self.read_only and other.read_only,
            self.opaque_stdin or other.opaque_stdin,
            self.unbound_write or other.unbound_write,
            self.opaque or other.opaque,
            self.ads_write or other.ads_write,
        )


_COMPOUND_CLOSERS = {
    "(": ")",
    "{": "}",
    "if": "fi",
    "for": "done",
    "while": "done",
    "until": "done",
    "case": "esac",
}
_STDIN_REDIRECTIONS = ("<", "<<", "<<-", "<<<", "<&", "<>")
_STDIN_SCRIPT_PATHS = ("/dev/stdin", "/dev/fd/0", "/proc/self/fd/0")


def _redirect_parts(raw: str) -> tuple[str | None, str] | None:
    if raw in _REDIRECTIONS:
        return None, raw
    digits = len(raw) - len(raw.lstrip("0123456789"))
    if digits and raw[digits:] in _REDIRECTIONS:
        return raw[:digits], raw[digits:]
    return None


def _is_stdin_fd(fd: str | None) -> bool:
    if fd is None:
        return True
    # POSIX/Bash treat any all-zero ASCII decimal descriptor (0, 00, ...) as fd 0.
    return bool(fd) and fd.isascii() and fd.isdigit() and all(char == "0" for char in fd)


def _shell_segments(
    tokens: list[str],
    raw_tokens: list[str],
    *,
    windows: bool,
    posix_stdin: bool = False,
    inherited_stdin: bool = False,
) -> list[tuple[list[str], list[str], list[str], str, bool]]:
    """Split a token stream into commands with redirect targets and leading operator."""
    segments: list[tuple[list[str], list[str], list[str], str, bool]] = []
    segment: list[str] = []
    raws: list[str] = []
    redirects: list[str] = []
    operator = ""
    stdin_connected = inherited_stdin
    group_inputs: list[tuple[bool, int, str]] = [(inherited_stdin, 0, "")]
    closed_group: int | None = None
    index = 0
    while index < len(tokens):
        raw = raw_tokens[index]
        if raw.startswith("#") and "\n" in raw:
            segments.append((segment, raws, redirects, operator, stdin_connected))
            if segment or redirects:
                stdin_connected = group_inputs[-1][0]
            segment, raws, redirects, operator = [], [], [], "\n"
            raw = raw.rsplit("\n", 1)[-1]
        if (
            raw.startswith(tokens[index])
            and tokens[index]
            and all(char in ";|&<>()\n" for char in tokens[index])
        ):
            raw = tokens[index]
        brace = posix_stdin and raw in ("{", "}") and not segment
        redirect = _redirect_parts(raw)
        if brace or (raw and all(char in ";|&()\n" for char in raw)):
            segments.append((segment, raws, redirects, operator, stdin_connected))
            closed_group = None
            if posix_stdin:
                if raw in ("(", "{"):
                    group_inputs.append((stdin_connected, len(segments), _COMPOUND_CLOSERS[raw]))
                elif raw in (")", "}") and len(group_inputs) > 1 and group_inputs[-1][2] == raw:
                    _, closed_group, _ = group_inputs.pop()
                if raw != "\n" or segment or redirects:
                    stdin_connected = group_inputs[-1][0] or raw in ("|", "|&")
            segment, raws, redirects = [], [], []
            operator = raw
        elif redirect is not None:
            fd, op = redirect
            if index + 1 >= len(tokens):
                raise ValueError("Missing shell redirection target")
            index += 1
            destination = tokens[index]
            if posix_stdin and op in _STDIN_REDIRECTIONS and _is_stdin_fd(fd):
                stdin_connected = True
                if closed_group is not None:
                    for group_index in range(closed_group, len(segments)):
                        group_segment = segments[group_index]
                        segments[group_index] = (*group_segment[:4], True)
                if raw_tokens[index] == "<(":
                    # Parse process-substitution bodies separately so they do not mask
                    # the outer command as a script-file operand.
                    depth = 1
                    case_states: list[tuple[int, str]] = []
                    after_case_pattern = False
                    inner_start = index + 1
                    while index + 1 < len(tokens) and depth > 0:
                        index += 1
                        current = raw_tokens[index]
                        can_start_command = (
                            after_case_pattern
                            or index == inner_start
                            or raw_tokens[index - 1]
                            in (
                                ";",
                                ";;",
                                ";&",
                                ";;&",
                                "&&",
                                "||",
                                "|",
                                "|&",
                                "(",
                                "<(",
                                ">(",
                                "{",
                                "\n",
                                "then",
                                "do",
                                "else",
                                "elif",
                            )
                        )
                        after_case_pattern = False
                        if (
                            current == "case"
                            and (not case_states or case_states[-1] != (depth, "pattern"))
                            and can_start_command
                        ):
                            case_states.append((depth, "header"))
                            continue
                        if case_states and case_states[-1][0] == depth:
                            case_depth, state = case_states[-1]
                            if state == "header":
                                if current == "in":
                                    case_states[-1] = (case_depth, "pattern")
                                continue
                            if state == "pattern":
                                if current == "esac":
                                    case_states.pop()
                                elif current == ")":
                                    case_states[-1] = (case_depth, "body")
                                    after_case_pattern = True
                                continue
                            if current == "esac":
                                case_states.pop()
                                continue
                            if current in (";;", ";&", ";;&"):
                                case_states[-1] = (case_depth, "pattern")
                                continue
                        if current in ("(", "<(", ">("):
                            depth += 1
                        elif current == ")":
                            depth -= 1
                    if depth != 0:
                        raise ValueError("Unterminated process substitution")
                    segments.extend(
                        _shell_segments(
                            tokens[inner_start:index],
                            raw_tokens[inner_start:index],
                            windows=windows,
                            posix_stdin=posix_stdin,
                            inherited_stdin=False,
                        )
                    )
                    index += 1
                    continue
            if op in _WRITE_REDIRECTIONS or (
                op == ">&" and not destination.isdigit() and destination != "-"
            ):
                if destination != "/dev/null" or (windows and not posix_stdin):
                    redirects.append(destination)
        else:
            if posix_stdin and not segment:
                if raw in ("if", "for", "while", "until", "case"):
                    group_inputs.append((stdin_connected, len(segments), _COMPOUND_CLOSERS[raw]))
                elif raw in ("fi", "done", "esac") and group_inputs[-1][2] == raw:
                    _, closed_group, _ = group_inputs.pop()
                    stdin_connected = group_inputs[-1][0]
            segment.append(tokens[index])
            raws.append(raw)
        index += 1
    segments.append((segment, raws, redirects, operator, stdin_connected))
    return segments


def _stdin_shell_offset(tokens: list[str]) -> int:
    offset = _command_offset(tokens)
    while offset < len(tokens):
        wrapper = _command_name(tokens[offset])
        if wrapper == "env":
            offset += 1
            while offset < len(tokens) and tokens[offset].startswith("-"):
                option = tokens[offset]
                offset += 1
                if option == "--":
                    break
                if option in ("-u", "-C", "--unset", "--chdir"):
                    offset += 1
        elif wrapper == "exec":
            offset += 1
            while offset < len(tokens) and tokens[offset].startswith("-"):
                option = tokens[offset]
                offset += 1
                if option == "--":
                    break
                if option == "-a":
                    offset += 1
        elif wrapper == "sudo":
            offset += 1
            while offset < len(tokens) and tokens[offset].startswith("-"):
                option = tokens[offset]
                offset += 1
                if option == "--":
                    break
                if option in (
                    "-u",
                    "-g",
                    "-C",
                    "-D",
                    "-h",
                    "-p",
                    "-r",
                    "-t",
                    "-U",
                    "--user",
                    "--group",
                ):
                    offset += 1
        elif wrapper == "time":
            offset += 1
            while offset < len(tokens) and tokens[offset] in ("-p", "--"):
                option = tokens[offset]
                offset += 1
                if option == "--":
                    break
        else:
            break
        offset = _command_offset(tokens, offset)
    return offset


def _reads_stdin_script(
    tokens: list[str], raw_tokens: list[str], offset: int, connected: bool
) -> bool:
    if offset >= len(tokens):
        return False
    command = _command_name(tokens[offset])
    if command in ("source", "."):
        arguments = iter(zip(tokens[offset + 1 :], raw_tokens[offset + 1 :], strict=True))
        first = next(arguments, None)
        if first is not None and first[0] == "--":
            first = next(arguments, None)
        if first is None:
            return False
        operand, raw = first
        return raw == "<(" or (connected and operand in _STDIN_SCRIPT_PATHS)
    if command not in _HEREDOC_SHELLS:
        return False
    if _wrapped_command(tokens, raw_tokens, offset, writes_only=True) is not None:
        return False
    arguments = iter(zip(tokens[offset + 1 :], raw_tokens[offset + 1 :], strict=True))
    for option, raw in arguments:
        if option in ("--", "-"):
            following = next(arguments, None)
            if following is None:
                return connected
            operand, operand_raw = following
            return operand_raw == "<(" or (connected and operand in _STDIN_SCRIPT_PATHS)
        if raw == "<(":
            return True
        if not option.startswith(("-", "+")):
            return connected and option in _STDIN_SCRIPT_PATHS
        if _takes_shell_option_arg(option):
            next(arguments, None)
        if re.fullmatch(r"-[A-Za-z]*s[A-Za-z]*", option):
            return True
    return connected


def _reads_only(segment: list[str]) -> bool:
    """Whether one command only reads: a read-only word, or find without an action."""
    offset = _command_offset(segment)
    if offset >= len(segment):
        return False
    name = _command_name(segment[offset])
    if name == "find":
        return not any(value.split("=", 1)[0] in _FIND_ACTIONS for value in segment[offset + 1 :])
    return name in _READ_ONLY_COMMANDS


def _delete_segment(tokens: list[str]) -> bool:
    """A command deletes when it names a delete command or passes find -delete."""
    for offset, token in enumerate(tokens):
        command = _command_name(token)
        if command in _DELETE_COMMANDS:
            return True
        if command == "find" and any(
            value.split("=", 1)[0] == "-delete" for value in tokens[offset + 1 :]
        ):
            return True
    return False


def _shell_assignments(command: str, context: TrustedHostContext) -> frozenset[str]:
    return frozenset(
        name
        for name in (*_TRUSTED_VARIABLES, *context.environment)
        if (
            re.search(r"\b" + re.escape(name) + r"\s*=", command, re.I)
            if context.platform == "windows"
            else name + "=" in command
        )
    )


def _expand_trusted(
    token: str, context: TrustedHostContext, assigned: frozenset[str]
) -> str | None:
    """Expand the trusted variables a target may name; None when it stays unresolved."""
    if context.platform == "windows":
        dialect = ntpath.basename(context.shell).lower().removesuffix(".exe")
        pattern = (
            _WINDOWS_ENV
            if dialect == "cmd"
            else re.compile(r"\$env:([A-Za-z_][A-Za-z0-9_]*)", re.I)
            if dialect in ("pwsh", "powershell")
            else re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
        )
        values = dict(context.environment)
        if dialect in ("cmd", "pwsh", "powershell"):
            values = {name.upper(): value for name, value in values.items()}

        def windows_expand(match: re.Match[str]) -> str:
            name = next(value for value in match.groups() if value is not None)
            if dialect in ("cmd", "pwsh", "powershell"):
                name = name.upper()
            if name in assigned or not values.get(name):
                raise _UnresolvedWindowsVariable(f"Unknown Windows path variable: {name}")
            return values[name]

        expanded = pattern.sub(windows_expand, token)
        return None if "$" in expanded or "`" in expanded else expanded
    values = {
        "HOME": context.home,
        "TMPDIR": (context.environment.get("TMPDIR") or "").rstrip("/"),
    }

    def expand(match: re.Match[str]) -> str:
        name = match[1] or match[2]
        if name in assigned or not values[name]:
            raise ValueError(name)
        return values[name]

    try:
        expanded = _TRUSTED_EXPANSION.sub(expand, token)
    except ValueError:
        return None
    return None if not expanded or "$" in expanded or "`" in expanded else expanded


def _bound_targets(
    token: str,
    cwds: frozenset[str],
    context: TrustedHostContext,
    assigned: frozenset[str],
    *,
    windows_posix: bool = False,
) -> tuple[str, ...] | None:
    """Resolve a write target under every cwd it can name; None when it is unresolved."""
    if windows_posix and re.match(r"^(?:~|\$HOME|\$\{HOME\})(?:/|$)", token):
        # MSYS can use a home different from the Windows host context.
        return None
    try:
        expanded = _expand_trusted(token, context, assigned)
    except _UnresolvedWindowsVariable:
        if _command_name(context.shell) == "pwsh":
            return None
        raise
    if expanded is None:
        return None
    if context.platform == "windows":
        drive, tail = ntpath.splitdrive(expanded)
        anchored = expanded.startswith("\\\\") or (bool(drive) and tail.startswith("\\"))
    else:
        anchored = expanded == "~" or expanded.startswith(("/", "~/"))
    if anchored:
        return (_resolve(expanded, context).path,)
    if not cwds or len(cwds) > _MAX_CWDS:
        return None
    return tuple(
        dict.fromkeys(_resolve(expanded, replace(context, cwd=cwd)).path for cwd in sorted(cwds))
    )


def _shell_writes(
    command: str,
    context: TrustedHostContext,
    depth: int,
    *,
    powershell: bool = False,
    untrusted: frozenset[str] = frozenset(),
    known: bool = True,
    windows_posix_executor: bool = False,
    inherited_stdin: bool = False,
) -> _ShellWrites:
    """Bind write targets, resolving each under every cwd its command can run in."""
    if depth > 3:
        raise ValueError("Shell wrapper nesting cannot be checked")
    windows = context.platform == "windows"
    windows_posix = windows_posix_executor and _windows_posix(context)
    posix_stdin = (not windows and not powershell) or windows_posix
    command = command.replace("\\\n", "")
    scan_end, scanned_command, scan_bodies, heredocs_closed = _shell_scan(
        command, keep_comments=True
    )
    tokens, raw_tokens = _write_tokens(
        scanned_command
        if (not windows and not powershell) and scan_end == len(command) and heredocs_closed
        else command,
        windows=windows or powershell,
        cmd_shell=windows and not powershell and _command_name(context.shell) == "cmd",
    )
    assigned = untrusted | _shell_assignments(command, context)
    nested = _ShellWrites()
    for body in scan_bodies:
        try:
            nested = nested.merged(
                _shell_writes(
                    body,
                    context,
                    depth + 1,
                    powershell=powershell,
                    untrusted=assigned,
                    windows_posix_executor=windows_posix_executor,
                    inherited_stdin=inherited_stdin,
                )
            )
        except _UnboundShellWrite as exc:
            nested = nested.merged(_ShellWrites(unbound_write=str(exc), read_only=False))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            nested = nested.merged(_ShellWrites(opaque=str(exc), read_only=False))
    cwds: frozenset[str] = frozenset((context.cwd,)) if known else frozenset()
    segments = _shell_segments(
        tokens,
        raw_tokens,
        windows=windows,
        posix_stdin=posix_stdin,
        inherited_stdin=inherited_stdin,
    )
    paths: list[str] = []
    deletes: list[str] = []
    unbound = ""
    unbound_write = ""
    opaque = ""
    ads_write = ""
    opaque_stdin = ""
    read_only = True
    for position, (segment, raws, redirects, _operator, stdin_connected) in enumerate(segments):
        if not segment and not redirects:
            continue
        read_only = read_only and not redirects and _reads_only(segment)
        shell_offset = _stdin_shell_offset(segment) if posix_stdin else len(segment)
        if _reads_stdin_script(segment, raws, shell_offset, stdin_connected):
            opaque_stdin = "Shell script from standard input cannot be bound"
        if posix_stdin and stdin_connected:
            for raw in raws:
                if "$(" not in raw and "`" not in raw:
                    continue
                for body in _shell_scan(raw)[2]:
                    try:
                        nested = nested.merged(
                            _shell_writes(
                                body,
                                context,
                                depth + 1,
                                powershell=powershell,
                                untrusted=assigned,
                                windows_posix_executor=windows_posix_executor,
                                inherited_stdin=True,
                            )
                        )
                    except _UnboundShellWrite as exc:
                        nested = nested.merged(
                            _ShellWrites(unbound_write=str(exc), read_only=False)
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        nested = nested.merged(_ShellWrites(opaque=str(exc), read_only=False))
        deleting = _delete_segment(segment)
        try:
            operands: Iterable[str] = (
                _write_operands(
                    segment,
                    raws,
                    posix_shell=not windows and not powershell,
                    bsd_sed=context.platform == "darwin",
                )
                if segment
                else ()
            )
        except _UnboundShellWrite as exc:
            unbound_write = unbound_write or str(exc)
            read_only = False
            operands = ()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            opaque = opaque or str(exc)
            read_only = False
            operands = ()
        if windows and segment:
            head_idx = _command_offset(segment)
            seg_head = _command_name(segment[head_idx]) if head_idx < len(segment) else ""
            if seg_head in _POWERSHELL_STREAM_WRITE_CMDLETS:
                skip_next = False
                for arg, raw_arg in zip(
                    segment[head_idx + 1 :], raws[head_idx + 1 :], strict=False
                ):
                    if skip_next:
                        skip_next = False
                        continue
                    if raw_arg.startswith(("'", '"')):
                        continue
                    folded = arg.casefold()
                    if folded == "-stream" or folded.startswith(("-stream:", "-stream=")):
                        ads_write = (
                            ads_write or f"Windows alternate data stream: {seg_head} -Stream"
                        )
                        break
                    if folded in {
                        "-value",
                        "-path",
                        "-literalpath",
                        "-encoding",
                        "-delimiter",
                        "-filter",
                        "-include",
                        "-exclude",
                        "-credential",
                        "-width",
                    }:
                        skip_next = True
        for target in (*redirects, *operands):
            if windows and _is_windows_ads_path(target, allow_ps_provider=True):
                ads_write = ads_write or f"Windows alternate data stream: {target}"
                continue
            if deleting and ("{" in target or ".." in target.replace("\\", "/").split("/")):
                unbound = unbound or _UNBOUND_DELETE
                continue
            try:
                bound = _bound_targets(
                    target, cwds, context, assigned, windows_posix=windows_posix
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                if deleting:
                    unbound = unbound or _UNBOUND_DELETE
                else:
                    unbound_write = unbound_write or str(exc)
                    read_only = False
                continue
            if bound is None:
                if windows_posix and not deleting:
                    unbound_write = unbound_write or "Shell write target cannot be bound"
                    read_only = False
                    continue
                expansion = re.search(r"[$`]", target)
                literal = target[: expansion.start()] if expansion else ""
                if literal.strip("/\\"):
                    try:
                        paths.extend(_bound_targets(literal, cwds, context, frozenset()) or ())
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        opaque = opaque or str(exc)
                if deleting and not unbound:
                    unbound = _UNBOUND_DELETE
                continue
            paths.extend(bound)
            if deleting:
                deletes.extend(bound)
        for index in range(len(segment)):
            wrapped = _wrapped_command(segment, raws, index, writes_only=True)
            if wrapped is None:
                continue
            inner_shell, inner = wrapped
            for cwd in sorted(cwds) or (context.cwd,):
                try:
                    nested = nested.merged(
                        _shell_writes(
                            inner,
                            replace(context, cwd=cwd, shell=inner_shell),
                            depth + 1,
                            powershell=inner_shell in ("powershell", "pwsh"),
                            untrusted=assigned,
                            known=bool(cwds),
                            windows_posix_executor=windows_posix_executor,
                            inherited_stdin=stdin_connected and index == shell_offset,
                        )
                    )
                except _UnboundShellWrite as exc:
                    nested = nested.merged(_ShellWrites(unbound_write=str(exc), read_only=False))
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    nested = nested.merged(_ShellWrites(opaque=str(exc), read_only=False))
            break
        chained = (
            _operator in ("", "&&")
            and position + 1 < len(segments)
            and segments[position + 1][3] == "&&"
        )
        offset = _command_offset(segment)
        if offset < len(segment) and _command_name(segment[offset]) in (
            "cd",
            "pushd",
            "popd",
            "set-location",
        ):
            arguments = segment[offset + 1 :]
            while arguments and arguments[0].lower() in ("-l", "-p", "-@", "-e", "--"):
                arguments = arguments[1:]
            if _command_name(segment[offset]) == "popd" or (arguments and arguments[0] == "-"):
                cwds = frozenset()
            else:
                try:
                    bound = _bound_targets(
                        arguments[0] if arguments else "~",
                        cwds,
                        context,
                        assigned,
                        windows_posix=windows_posix,
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    opaque = opaque or str(exc)
                    bound = None
                if not bound:
                    cwds = frozenset()
                else:
                    cwds = frozenset(bound) if chained else cwds | frozenset(bound)
    return _ShellWrites(
        tuple(paths),
        tuple(deletes),
        unbound,
        read_only,
        opaque_stdin,
        unbound_write,
        opaque,
        ads_write,
    ).merged(nested)


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
        if command in ("cp", "copy", "copy-item", "cpi", "install", "ln"):
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
                ) or (command in ("copy-item", "cpi") and value.lower() == "-destination"):
                    return options[index + 1 : index + 2] or conservative
                if command in ("cp", "install", "ln") and value.startswith("--target-directory="):
                    return [value.split("=", 1)[1]]
                if command in ("cp", "install", "ln") and value.startswith("-t") and value != "-t":
                    return [value[2:]]
                if command in ("copy-item", "cpi") and value.lower().startswith("-destination:"):
                    return [value.split(":", 1)[1]]
            if any(
                value.startswith("-") and value not in ("-T", "--no-target-directory", "--")
                for value in options
            ):
                return conservative
            return operands[-1:]
        if command in _POWERSHELL_WRITE_CMDLETS:
            if offset:
                return conservative
            base_target: str | None = None
            item_name: str | None = None
            for index, value in enumerate(arguments):
                lowered = value.lower()
                if lowered in ("-path", "-literalpath", "-filepath"):
                    if index + 1 >= len(arguments):
                        return conservative
                    base_target = arguments[index + 1]
                elif lowered.startswith(("-path:", "-literalpath:", "-filepath:")):
                    base_target = value.split(":", 1)[1]
                elif command in ("new-item", "ni") and lowered == "-name":
                    if index + 1 >= len(arguments):
                        return conservative
                    item_name = arguments[index + 1]
                elif command in ("new-item", "ni") and lowered.startswith("-name:"):
                    item_name = value.split(":", 1)[1]
            if command in ("new-item", "ni") and item_name is not None:
                if base_target is None:
                    base_target = operands[0] if operands else "."
                sep = "\\" if "\\" in base_target or "\\" in item_name else "/"
                combined = (
                    item_name
                    if ntpath.isabs(item_name) or item_name.startswith("/")
                    else base_target.rstrip("/\\") + sep + item_name.lstrip("/\\")
                )
                return [combined, base_target]
            if base_target is not None:
                return [base_target]
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
        if command == "find" and any(
            value.split("=", 1)[0] in _FIND_ACTIONS for value in arguments
        ):
            # A delete acts on every match under find's search roots: the operands before the
            # first predicate, or `.` when it names none. A predicate argument is never a target.
            if _delete_segment(tokens):
                roots = []
                for value in conservative[offset + 1 :]:
                    if value.startswith(("-", "(", "!")):
                        break
                    roots.append(value)
                return roots or ["."]
            # Other actions (-exec, -fprint) keep non-pattern values so targets stay visible.
            pattern_predicates = {
                "-name",
                "-iname",
                "-path",
                "-ipath",
                "-wholename",
                "-iwholename",
                "-regex",
                "-iregex",
            }
            kept: list[str] = []
            skip_next = False
            for raw_arg, value in zip(arguments, conservative[offset + 1 :], strict=False):
                if skip_next:
                    skip_next = False
                    continue
                if raw_arg in pattern_predicates:
                    skip_next = True
                    continue
                if not value.startswith("-"):
                    kept.append(value)
            return kept
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
            "unlink",
            "remove-item",
            "ri",
            "move-item",
            "mi",
            "move",
            "rename-item",
            "rni",
            "ren",
        ):
            # The program name and options are not operands; after -- everything is.
            operands = conservative[offset + 1 :]
            if "--" in operands:
                stop = operands.index("--")
                return [
                    value for value in operands[:stop] if not value.startswith("-")
                ] + operands[stop + 1 :]
            return [value for value in operands if not value.startswith("-")]
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


def _delete_refusal(path: str, why: str) -> str:
    return f"Deleting {path} is refused: {why}"


def _depth(value: str, parent: str, windows: bool) -> int | None:
    """Levels below parent, or None when value is outside it."""
    if windows:
        target = ntpath.normcase(ntpath.normpath(value))
        root = ntpath.normcase(ntpath.normpath(parent))
        separator = "\\"
    else:
        target, root = os.path.normpath(value), os.path.normpath(parent)
        separator = "/"
    if target == root:
        return 0
    prefix = root.rstrip(separator) + separator
    if not target.startswith(prefix):
        return None
    return len([part for part in target[len(prefix) :].split(separator) if part])


def _critical_delete(path: str, context: TrustedHostContext) -> str:
    """Say why deleting this bound path stays refused, or '' when it may proceed.

    System and credential trees are refused at any depth; a home directory only
    down to one level below it, so a project inside one still works.
    """
    if context.platform == "windows":
        drive, tail = ntpath.splitdrive(path)
        if not tail.strip("\\"):
            return _delete_refusal(path, "it is a volume root")
        environment = {key.upper(): value for key, value in context.environment.items()}
        for name in ("SYSTEMROOT", "WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            root = environment.get(name)
            if root and _depth(path, root, True) is not None:
                return _delete_refusal(path, f"it is inside {root}")
        for root, levels in (
            (ntpath.join(drive + "\\", "Users"), 2),
            (_resolve(context.home, context).path, 1),
        ):
            depth = _depth(path, root, True)
            if depth is not None and depth <= levels:
                return _delete_refusal(path, "it is a home directory or one level below it")
        return ""
    if path == "/":
        return _delete_refusal(path, "it is the filesystem root")
    for root in _CRITICAL_SYSTEM_ROOTS:
        if _depth(path, _resolve(root, context).path, False) is not None:
            return _delete_refusal(path, f"it is inside {root}")
    for root, levels in ((context.home, 1), *((value, 2) for value in _USER_HOME_ROOTS)):
        depth = _depth(path, _resolve(root, context).path, False)
        if depth is not None and depth <= levels:
            return _delete_refusal(path, "it is a home directory or one level below it")
    depth = _depth(path, _resolve("/var/tmp", context).path, False)
    if depth is not None:
        return "" if depth else _delete_refusal(path, "it is a temporary root")
    temporary = _DARWIN_TEMP_ROOT.match(path)
    if temporary:
        if path == temporary[1]:
            return _delete_refusal(path, "it is a temporary root")
        return ""
    if _under(path, _resolve("/var", context).path, False):
        return _delete_refusal(path, "/var is refused outside its temporary roots")
    return ""


def _key_name(name: str) -> bool:
    """A file name that can carry a key, refused wherever it sits."""
    folded = name.casefold()
    return (
        any(word in folded for word in _KEY_NAME_WORDS)
        or folded == ".env"
        or folded.startswith(".env.")
        or folded.endswith(".env")
    )


def _key_read(path: str, roots: tuple[str, ...], windows: bool, *, covers: bool = False) -> bool:
    """A key-bearing name, a .json in an agent root, or a glob/recursive read over one."""
    separator = "\\" if windows else "/"
    folder, _, name = path.rpartition(separator)
    if _key_name(name):
        return True
    roots = tuple(root.casefold() for root in roots) if windows else roots
    if ".json" in name.casefold() and (folder.casefold() if windows else folder) in roots:
        return True
    prefix = path if covers else re.split(r"[*?\[]", path, maxsplit=1)[0]
    if prefix == path and not covers:
        return False
    return any(_under(root, prefix.rstrip(separator), windows) for root in roots)


def _credential_refusal(reason: str, path: str) -> str:
    return f"{reason} Target: {path}"


def _names_key_file(command: str) -> str:
    """The first file name in the command text that can carry a key, or ''."""
    for word in _COMMAND_WORDS.findall(command):
        if "." in word and _key_name(word.replace("\\", "/").rsplit("/", 1)[-1]):
            return word
    return ""


def _allowed_control_subtrees(
    control: str,
    original: str,
    configured: tuple[tuple[str, str, str], ...],
    context: TrustedHostContext,
    windows: bool,
) -> tuple[tuple[str, str], ...]:
    """Target-scoped readable directory subtrees strictly tied to their owning control root."""
    joined = ntpath.join if windows else os.path.join
    norm = (lambda value: ntpath.normcase(ntpath.normpath(value))) if windows else os.path.normpath
    sep = "\\" if windows else "/"
    orig_norm = norm(_absolute(original, context))
    ctrl_norm = norm(control)
    subtrees: list[tuple[str, str]] = []
    for lexical_sub, resolved_sub, kind in configured:
        if kind != "directory":
            continue
        lex_norm = norm(lexical_sub)
        res_norm = norm(resolved_sub)
        if lex_norm == orig_norm and res_norm == ctrl_norm:
            subtrees.append((lexical_sub, resolved_sub))
            continue
        if _under(lex_norm, orig_norm, windows) and lex_norm != orig_norm:
            rel = lex_norm[len(orig_norm) :].lstrip("\\/")
            if not rel or ".." in rel.replace("\\", "/").split("/"):
                continue
            expected = norm(joined(ctrl_norm, rel.replace("/", sep) if windows else rel))
            if res_norm == expected:
                subtrees.append((lexical_sub, resolved_sub))
            continue
        if _under(orig_norm, lex_norm, windows) and orig_norm != lex_norm:
            rel = orig_norm[len(lex_norm) :].lstrip("\\/")
            if not rel or ".." in rel.replace("\\", "/").split("/"):
                continue
            expected_ctrl = norm(joined(res_norm, rel.replace("/", sep) if windows else rel))
            if ctrl_norm == expected_ctrl:
                subtrees.append((lexical_sub, resolved_sub))
    return tuple(dict.fromkeys(subtrees))


def _control_mentions_inside_subtrees(
    command: str,
    control: str,
    original: str,
    subtrees: tuple[str, ...],
    roots: tuple[str, ...],
    context: TrustedHostContext,
    windows: bool,
) -> bool:
    """True when every literal mention of control in command lies strictly inside subtrees."""
    if not subtrees:
        return False
    if any(_under(control, sub, windows) for sub in subtrees):
        return True
    haystack = command.casefold().replace("/", "\\") if windows else command
    forms: list[str] = []
    for base in (control, _absolute(original, context)):
        for form in _path_forms(base, context):
            forms.append(form.casefold().replace("/", "\\") if windows else form)
    forms = sorted(set(forms), key=len, reverse=True)
    found = False
    for form in forms:
        pattern = re.compile(
            r"(?<![\w./~\\-])" + re.escape(form) + r"(?![\w.-])([^\s'\"|;&()<>]*)"
        )
        for match in pattern.finditer(haystack):
            found = True
            suffix = match.group(1)
            if ".." in suffix.replace("\\", "/").split("/"):
                return False
            candidate = control.rstrip("/\\") + suffix
            if not any(_under(candidate, sub, windows) for sub in subtrees):
                return False
            if _key_read(candidate, roots, windows):
                return False
    return found


def _control_refusal(
    intent: ToolIntent,
    control: str,
    original: str,
    context: TrustedHostContext,
    windows: bool,
    reasons: tuple[str, str],
    *,
    control_kind: str = "",
) -> tuple[Literal["", "deny", "ask"], str]:
    """Refuse writes and key-bearing reads; allow skills, notes, extensions and D4 subtrees."""
    control_reason, credential_reason = reasons
    joined = ntpath.join if windows else os.path.join
    readable = any(
        _under(control, joined(root, name), windows)
        for root in (context.home, context.workspace)
        for name in _READABLE_CONTROL_DIRECTORIES
    )
    subtree_pairs = _allowed_control_subtrees(
        control, original, intent.resolved_readable_subtrees, context, windows
    )
    subtrees = tuple(resolved_sub for _, resolved_sub in subtree_pairs)
    lexical_subtrees = tuple(lexical_sub for lexical_sub, _ in subtree_pairs)
    roots = tuple(
        joined(root, *parts).rstrip("\\" if windows else "/")
        for root in (context.home, context.workspace)
        for parts in _AGENT_ROOT_PARTS
    )
    target = intent.target
    if intent.kind == ToolKind.READ and target is not None:
        recursive = intent.name in _RECURSIVE_READS
        if (
            not recursive
            and target.kind == "file"
            and context.readable_control_file == (original, target.path)
        ):
            return "", ""
        try:
            typed = _absolute(intent.target_text, context) if intent.target_text else ""
        except ValueError:
            typed = ""
        home_control = any(
            _under(control, joined(context.home, name), windows)
            for name in _READABLE_CONTROL_DIRECTORIES
        )
        workspace = intent.workspace_path.path if intent.workspace_path is not None else ""
        resolved_targets = [target.path]
        if intent.source is not None:
            resolved_targets.append(intent.source.path)
        check_paths = [typed or target.path, *resolved_targets]
        deduped = list(dict.fromkeys(check_paths))
        resolved_in_subtrees = bool(subtrees) and all(
            any(_under(path, sub, windows) for sub in subtrees) for path in resolved_targets
        )
        typed_in_subtrees = not typed or any(
            _under(typed, sub, windows) for sub in (*lexical_subtrees, *subtrees)
        )
        all_in_subtrees = resolved_in_subtrees and typed_in_subtrees
        control_inside_subtrees = any(_under(control, sub, windows) for sub in subtrees)
        try:
            orig_abs = _absolute(original, context)
        except ValueError:
            orig_abs = control
        for path in deduped:
            under_control = _under(path, control, windows) or _under(path, orig_abs, windows)
            covers_control = recursive and (
                _under(control, path, windows) or _under(orig_abs, path, windows)
            )
            if not (under_control or covers_control):
                continue
            if not readable:
                if not all_in_subtrees or (covers_control and not control_inside_subtrees):
                    return "deny", control_reason
                if _key_read(path, roots, windows, covers=recursive):
                    return "deny", _credential_refusal(credential_reason, path)
                continue
            if (
                intent.name in ("grep", "find")
                and control_kind == "new-file"
                and not under_control
                and not home_control
                and workspace
                and _under(control, workspace, windows)
                and _under(path, workspace, windows)
            ):
                continue
            if _key_read(path, roots, windows, covers=recursive):
                return "deny", _credential_refusal(credential_reason, path)
        return "", ""
    if not intent.command:
        targets = [
            p.path for p in (target, intent.source) if p is not None and p.kind != "unknown"
        ]
        if intent.target_text:
            with contextlib.suppress(ValueError):
                targets.append(_absolute(intent.target_text, context))
        try:
            orig_abs = _absolute(original, context)
        except ValueError:
            orig_abs = control
        return (
            ("deny", control_reason)
            if any(_under(p, control, windows) or _under(p, orig_abs, windows) for p in targets)
            else ("", "")
        )
    mentioned = any(
        _mentions_path(intent.command, form, context)
        for form in (control, _absolute(original, context))
    )
    named = _names_key_file(intent.command)
    if mentioned and named:
        return "deny", _credential_refusal(credential_reason, named)
    for candidate in intent.command_write_paths:
        if _under(candidate, control, windows):
            return "deny", control_reason
    recursive_paths = frozenset(intent.recursive_read_paths)
    for candidate in intent.command_paths:
        if not _under(candidate, control, windows):
            continue
        candidate_allowed = intent.command_read_only and (
            readable or any(_under(candidate, sub, windows) for sub in subtrees)
        )
        if not candidate_allowed:
            return "deny", control_reason
        if _key_read(candidate, roots, windows) or (
            candidate in recursive_paths and _key_read(candidate, roots, windows, covers=True)
        ):
            return "deny", _credential_refusal(credential_reason, candidate)
    if mentioned:
        mention_allowed = intent.command_read_only and (
            readable
            or _control_mentions_inside_subtrees(
                intent.command, control, original, subtrees, roots, context, windows
            )
        )
        if not mention_allowed:
            if intent.command_opaque:
                return "ask", control_reason
            return "deny", control_reason
    return "", ""


def _validated_rules(
    rules: RelaxedRules,
) -> tuple[list[tuple[str, str, str, re.Pattern[str]]], list[str], list[str], str, str, str]:
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
    if not all(
        isinstance(reasons.get(key), str) for key in ("control", "protected", "credential")
    ):
        raise ValueError("Malformed path reasons")
    return (
        [compiled[rule_id] for rule_id in active],
        protected["exact"],
        protected["prefix"],
        reasons["control"],
        reasons["protected"],
        reasons["credential"],
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


def _relaxed_auto_allow(category: str) -> TierDecision:
    return TierDecision(Verdict.ALLOW, f"relaxed-auto:{category}")


def _unknown_edit_target_is_ambiguous_or_critical(path: str, *, windows: bool) -> bool:
    if not path.strip():
        return True
    if windows and (
        "%" in path
        or "$" in path
        or bool(re.match(r"^(?:\\\\[?.]\\|//[?.]/|\\\?\?\\)", path))
        or bool(re.match(r"^[A-Za-z]:(?![/\\])", path))
    ):
        return True
    from omnigent.server.child_approvals import _critical_target

    return _critical_target(path)


def decide_tier(tier: PermissionTier, intent: ToolIntent, rules: RelaxedRules) -> TierDecision:
    """Return a pure decision; bad rules or gate faults deny, uncertainty asks or allows per D3."""
    try:
        if tier not in PERMISSION_TIERS:
            raise ValueError("Unknown permission tier")
        if intent.status == "invalid":
            return TierDecision(
                Verdict.DENY, f"Malformed or unresolved tool call: {intent.detail}"
            )
        if intent.gate_fault:
            return TierDecision(Verdict.DENY, f"Permission check unavailable: {intent.gate_fault}")
        if tier == "full":
            return TierDecision(Verdict.ALLOW)
        active, _, _, control_reason, protected_reason, credential_reason = _validated_rules(rules)
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
            return TierDecision(
                Verdict.DENY, "Permission check unavailable: Missing trusted Windows system root"
            )
        if intent.workspace_path is None or not intent.resolved_control_paths:
            raise ValueError("Missing bound host paths")
        # D2 positive matches run before any uncertainty outcome.
        if intent.name in _NATIVE_GRANT_TOOLS:
            return TierDecision(
                Verdict.DENY,
                intent.detail
                or "Native permission grants cannot replace the session permission tier",
            )
        if intent.native_status == NativeSupportStatus.FAILED and (
            intent.name not in _NATIVE_SUBAGENT_TOOLS
            or intent.detail in _NATIVE_BOOTSTRAP_ERRORS
            or bool(context.native_tools.get(intent.name))
        ):
            return TierDecision(
                Verdict.DENY,
                intent.detail or "Native support proof failed",
                "native-proof",
            )
        if intent.ads_write:
            return TierDecision(
                Verdict.DENY, f"Windows alternate data stream write is refused: {intent.ads_write}"
            )
        target = intent.target
        control_kinds = (
            intent.resolved_control_kinds
            if len(intent.resolved_control_kinds) == len(intent.resolved_control_paths)
            else ("",) * len(intent.resolved_control_paths)
        )
        opaque_control_mention = ""
        for original, path, control_kind in zip(
            context.control_paths, intent.resolved_control_paths, control_kinds, strict=True
        ):
            action, refusal = _control_refusal(
                intent,
                path,
                original,
                context,
                windows,
                (control_reason, credential_reason),
                control_kind=control_kind,
            )
            if action == "deny":
                return TierDecision(Verdict.DENY, refusal, "control-path")
            if action == "ask" and not opaque_control_mention:
                opaque_control_mention = refusal
        if intent.kind == ToolKind.EDIT and target is not None:
            for bound_target in (
                p for p in (target, intent.source) if p is not None and p.kind != "unknown"
            ):
                is_protected = bound_target.path in intent.protected_exact_paths or any(
                    _under(bound_target.path, value, windows)
                    for value in intent.protected_prefix_paths
                )
                if is_protected and tier == "auto":
                    return TierDecision(
                        Verdict.DENY, f"{protected_reason}: {bound_target.path}", "protected-path"
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
            for candidate in intent.command_delete_paths:
                refusal = _critical_delete(candidate, context)
                if refusal:
                    return TierDecision(Verdict.DENY, refusal, "critical-delete")
            for rule_id, name, reason, pattern in active:
                if pattern.search(intent.command):
                    explanation = _explanation(intent.command, context.platform, rules.signatures)
                    return TierDecision(Verdict.DENY, f"{name}: {explanation or reason}", rule_id)
        # D3 step 3: tool_search under plan -> DENY; READ/DELEGATE/framework -> ALLOW.
        if intent.detail == "tool-search" and tier == "plan":
            return TierDecision(Verdict.DENY, "Plan tier cannot activate deferred tools")
        if intent.kind in (ToolKind.READ, ToolKind.DELEGATE) or intent.detail in (
            "framework-question",
            "framework-control",
        ):
            return TierDecision(Verdict.ALLOW)
        # D3 step 4: plan denies everything else.
        if tier == "plan":
            return TierDecision(
                Verdict.DENY, "Plan tier permits only known reads and user questions"
            )
        # Step 5: severe-set guards stay ASK; other uncertainty relaxes to ALLOW under auto.
        if opaque_control_mention:
            return TierDecision(Verdict.ASK, opaque_control_mention, "control-path")
        if intent.native_status == NativeSupportStatus.FAILED or (
            intent.name == "subagent" and intent.native_status is None
        ):
            if tier == "auto":
                return _relaxed_auto_allow("native-proof")
            return TierDecision(
                Verdict.ASK,
                "Native support proof is missing"
                if intent.native_status is None
                else intent.detail or "Native support proof failed",
                "native-proof",
            )
        if intent.unbound_write:
            return TierDecision(
                Verdict.ASK,
                intent.detail or "Write or delete target cannot be bound",
                "unbound-write",
            )
        if intent.hidden_script:
            return TierDecision(Verdict.ASK, intent.detail or "Command requires one-time approval")
        if intent.kind != ToolKind.READ and any(
            p is not None and p.kind == "unknown" for p in (target, intent.source)
        ):
            if tier == "auto":
                unknown_paths = [
                    p.path
                    for p in (target, intent.source)
                    if p is not None and p.kind == "unknown"
                ]
                if not any(
                    _unknown_edit_target_is_ambiguous_or_critical(path, windows=windows)
                    for path in unknown_paths
                ):
                    return _relaxed_auto_allow("unknown-target")
            return TierDecision(Verdict.ASK, "Target type or final path cannot be confirmed")
        if intent.status == "unknown" or intent.kind == ToolKind.UNKNOWN:
            if tier == "auto" and intent.detail != "Interactive terminal input cannot be checked":
                return _relaxed_auto_allow("unknown-tool")
            return TierDecision(
                Verdict.ASK, intent.detail or "Unknown tool needs one-time approval"
            )
        # Step 6: opaque or uncertain command -> auto ALLOW (apply_auto_floor checks severe set).
        if intent.command_opaque or intent.command_uncertain:
            if tier == "auto":
                return _relaxed_auto_allow(
                    "opaque-command" if intent.command_opaque else "uncertain-command"
                )
            return TierDecision(Verdict.ASK, intent.detail or "Command requires one-time approval")
        # D3 step 7: ask / accept-edits / auto.
        if tier == "ask":
            return TierDecision(Verdict.ASK, "This tool call needs one-time approval")
        if tier == "accept-edits":
            bound_targets = tuple(p for p in (target, intent.source) if p is not None)
            if (
                intent.kind == ToolKind.EDIT
                and target
                and all(
                    item.kind in ("file", "new-file")
                    and _under(item.path, intent.workspace_path.path, windows)
                    for item in bound_targets
                )
            ):
                return TierDecision(Verdict.ALLOW)
            return TierDecision(Verdict.ASK, "This tool call needs one-time approval")
        return TierDecision(Verdict.ALLOW)
    except Exception as exc:  # noqa: BLE001 - a gate must deny on any check failure
        return TierDecision(Verdict.DENY, f"Permission check unavailable: {type(exc).__name__}")


def format_denial_reason(
    source: DenialSource,
    reason: str = "",
    *,
    detail: str | None = None,
) -> str:
    """Prefix a denial message with the layer that denied it."""
    text = reason.strip()
    if source == "rule" and detail:
        label = f"rule [{detail}]"
    elif detail:
        label = f"{source} ({detail})"
    else:
        label = source
    exact_prefix = f"Denied by {label}:"
    exact_empty = f"Denied by {label}."
    if text == exact_empty or text.startswith(exact_prefix):
        return text
    return f"{exact_prefix} {text}" if text else exact_empty


def format_tier_denial(
    decision: TierDecision,
    *,
    tier: PermissionTier | None = None,
) -> str:
    """Format a permission-tier denial with its rule or permission-mode source."""
    if decision.rule_id:
        return format_denial_reason("rule", decision.reason, detail=decision.rule_id)
    return format_denial_reason("permission mode", decision.reason, detail=tier)


__all__ = [
    "AGY_PERMISSION_TIER_LABEL_KEY",
    "DEFAULT_PERMISSION_TIER",
    "NON_FULL_TIERS",
    "PERMISSION_TIERS",
    "PERMISSION_TIERS_CAPABILITY",
    "PI_PERMISSION_TIER_LABEL_KEY",
    "DenialSource",
    "NativeCallBinding",
    "NativeInputSnapshot",
    "NativeSupport",
    "NativeSupportStatus",
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
    "format_denial_reason",
    "format_tier_denial",
    "load_rules",
    "normalize_permission_tier",
    "plan_visible_tool_names",
    "resolve_local_path",
]
