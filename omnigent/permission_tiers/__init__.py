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
    "sys_os_read": "path",
    "view_file": "AbsolutePath",
    "list_dir": "DirectoryPath",
}
_EDIT_PATH = {
    "write": "path",
    "edit": "path",
    "sys_os_write": "path",
    "sys_os_edit": "path",
    "write_to_file": "TargetFile",
    "replace_file_content": "TargetFile",
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
        "list_comments",
        "load_skill",
        "read_skill_file",
        "browser_snapshot",
        "browser_screenshot",
        "sys_terminal_read",
        "sys_terminal_list",
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
    )
)
_QUESTION_TOOLS = frozenset(("ask_user", "ask_user_async", "request_user_input"))
_CONTROL_TOOLS = frozenset(("report_result", "sys_read_inbox", "manage_todo_list"))


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
            if name in ("edit", "sys_os_edit"):
                pair = all(isinstance(arguments.get(key), str) for key in ("oldText", "newText"))
                edits = arguments.get("edits")
                batch = isinstance(edits, list) and all(
                    isinstance(edit, dict)
                    and all(isinstance(edit.get(key), str) for key in ("oldText", "newText"))
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
            return ToolIntent(
                ToolKind.EXEC,
                name,
                digest,
                context,
                command=_command_text(_required_text(arguments, _EXEC_COMMAND[name])),
            )
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
            command_paths=_command_paths(intent.command, context) if intent.command else (),
        )
        try:
            return replace(
                bound,
                command_write_paths=(
                    _command_paths(intent.command, context, writes_only=True)
                    if intent.command
                    else ()
                ),
            )
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
    paths = []
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
        index = 0
        while index < len(tokens):
            raw = raw_tokens[index]
            if raw.startswith("#") and "\n" in raw:
                segments.append(segment)
                segment = []
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
            elif raw in (">", ">>", ">|", "&>", "&>>", ">&", "<", "<<", "<<<", "<&", "<>"):
                if index + 1 >= len(tokens):
                    raise ValueError("Missing shell redirection target")
                index += 1
                destination = tokens[index]
                if raw in (">", ">>", ">|", "&>", "&>>", "<>") or (
                    raw == ">&" and not destination.isdigit() and destination != "-"
                ):
                    candidates.append(destination)
            else:
                segment.append(tokens[index])
            index += 1
        segments.append(segment)
        cwd_changed = False
        for segment in segments:
            candidates.extend(_write_operands(segment))
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
        if writes_only and "$" in token:
            raise _UnboundShellWrite("Shell write target expansion cannot be bound")
        if "$" in token or (not writes_only and any(char in token for char in "*?=")):
            continue
        paths.append(_resolve(token, context).path)
    return tuple(paths)


def _write_operands(tokens: list[str]) -> list[str]:
    """Select reliable direct targets; retain all locations for unbound mutations."""
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
        if command == "sed":
            return (
                conservative
                if any(value.startswith(("-i", "--in-place")) for value in arguments)
                else []
            )
        if command in (
            "rm",
            "mv",
            "tee",
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
            if target and _under(target.path, path, windows):
                return TierDecision(Verdict.DENY, control_reason, "control-path")
            if intent.command and any(
                _mentions_path(intent.command, form, context)
                for form in (path, _absolute(original, context))
            ):
                return TierDecision(Verdict.DENY, control_reason, "control-path")
            if any(_under(candidate, path, windows) for candidate in intent.command_paths):
                return TierDecision(Verdict.DENY, control_reason, "control-path")
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
