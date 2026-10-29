"""Canonical Codex launch options and private config-file profile materialization."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shlex
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import tomlkit
from tomlkit.exceptions import ParseError, TOMLKitError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.git_commands import run_git
from omnigent.runtime.filesystem_registry import (
    _anchored_git_env,
    _git_common_dir,
    detect_git_root,
    linked_worktree_git_dir,
)

_logger = logging.getLogger(__name__)

_CODEX_CONFIG_PATHS = (
    "agents.*.config_file",
    "experimental_compact_prompt_file",
    "log_dir",
    "model_catalog_json",
    "model_instructions_file",
    "model_providers.*.auth.cwd",
    "sandbox_workspace_write.writable_roots.[]",
    "skills.config.[].path",
    "sqlite_home",
    "js_repl_node_path",
    "js_repl_node_module_dirs.[]",
    "profiles.*.experimental_compact_prompt_file",
    "profiles.*.model_catalog_json",
    "profiles.*.model_instructions_file",
    "profiles.*.js_repl_node_path",
    "profiles.*.js_repl_node_module_dirs.[]",
    *(
        f"otel.{exporter}.{transport}.tls.{field}"
        for exporter in ("exporter", "metrics_exporter", "trace_exporter")
        for transport in ("otlp-http", "otlp-grpc")
        for field in ("ca-certificate", "client-certificate", "client-private-key")
    ),
)

_MISSING = object()

# Config keys whose values are safe to log verbatim. Every other ``-c`` value is
# masked because launch args may carry credentials or private endpoints.
_LOGGABLE_CONFIG_KEYS = frozenset(
    {
        "approval_policy",
        "approvals_reviewer",
        "default_permissions",
        "model",
        "model_provider",
        "model_reasoning_effort",
        "profile",
        "sandbox_mode",
    }
)
_ENV_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
# An option with its value attached by ``=`` (e.g. ``--remote=ws://...``), so
# the value can be redacted without treating the whole token as opaque.
_ATTACHED_OPTION = re.compile(r"^(--?[A-Za-z0-9][A-Za-z0-9-]*)=(.*)$", re.DOTALL)


def absolute_codex_path(value: str, base: Path) -> str:
    """Match Codex's lexical path normalization without resolving symlinks."""
    home_relative = value == "~" or value.startswith(("~/", f"~{os.sep}"))
    expanded = os.path.expanduser(value) if home_relative else value
    return os.path.abspath(os.path.join(base, expanded))


def _resolve_profile_paths(config: dict[str, Any], source_home: Path) -> None:
    """Normalize Codex 0.155 typed path fields, not symbolic permission keys."""

    def resolve(value: Any, segments: list[str]) -> Any:
        if not segments:
            return absolute_codex_path(value, source_home) if isinstance(value, str) else value
        segment, *remaining = segments
        if segment == "[]" and isinstance(value, list):
            return [resolve(item, remaining) for item in value]
        if isinstance(value, dict):
            for key in list(value) if segment == "*" else (segment,):
                if key in value:
                    value[key] = resolve(value[key], remaining)
        return value

    for path in _CODEX_CONFIG_PATHS:
        resolve(config, path.split("."))


# Codex options that consume the following token as their value, so a reserved
# token appearing as one of those values (e.g. ``--model app-server``) is NOT a
# subcommand/flag and must not be rejected.
_CODEX_VALUE_FLAGS = frozenset(
    {
        "-c",
        "--config",
        "-s",
        "--sandbox",
        "-a",
        "--ask-for-approval",
        "-p",
        "--profile",
        "--add-dir",
        "-m",
        "--model",
        "-C",
        "--cd",
        "-i",
        "--image",
        "--local-provider",
        "--enable",
        "--disable",
        # Names an env var holding the remote auth token; unlike the reserved
        # ``--remote`` transport flag it takes a value and is caller-settable.
        "--remote-auth-token-env",
    }
)

# App-server/transport tokens the runner owns: it starts the Codex app-server
# (``codex app-server --listen``) and appends the TUI ``--remote`` attach
# itself. A caller that passes any of these as pass-through args produces an
# incoherent argv — e.g. ``codex app-server … --remote`` (app-server rejects
# ``--remote`` on every codex version, clap exits 2) — so the thread never
# starts. ``--remote-control`` is app-server-only and equally invalid on the TUI.
_RESERVED_CODEX_TRANSPORT_TOKENS = frozenset(
    {"app-server", "--listen", "--remote", "--remote-control"}
)


def reserved_codex_transport_arg(args: Sequence[str]) -> str | None:
    """Return the first pass-through Codex arg that selects or attaches the runner-owned transport.

    Scoped to Codex (the runner owns ``app-server``/``--listen``/``--remote``);
    option *values* are never flagged (``--model app-server`` passes), and tokens
    after a ``--`` prompt separator are left alone.

    :param args: Caller pass-through Codex CLI args (``terminal_launch_args``).
    :returns: The reserved token, e.g. ``"app-server"``, or ``None``.
    """
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        if arg.split("=", 1)[0] in _CODEX_VALUE_FLAGS:
            # Skip the option and its value (an attached ``=`` form carries its own).
            index += 1 if "=" in arg else 2
            continue
        token = arg.split("=", 1)[0]
        if token in _RESERVED_CODEX_TRANSPORT_TOKENS:
            return token
        index += 1
    return None


class CodexTransportArgsError(OmnigentError, ValueError):
    """Saved launch arguments conflict with the transport Omnigent manages."""

    def __init__(self, token: str) -> None:
        super().__init__(
            f"terminal_launch_args may not contain the runner-owned Codex transport "
            f"token {token!r}. Start a new Codex session without that argument; "
            "Omnigent starts the app-server and attaches the terminal automatically.",
            code=ErrorCode.HARNESS_NOT_CONFIGURED,
        )


def reject_reserved_codex_transport_args(args: Sequence[str]) -> None:
    """Reject pass-through Codex args that select or attach the runner-owned transport.

    :param args: Caller pass-through Codex CLI args (``terminal_launch_args``).
    :raises ValueError: when :func:`reserved_codex_transport_arg` finds a
        reserved token.
    """
    token = reserved_codex_transport_arg(args)
    if token is not None:
        raise CodexTransportArgsError(token)


def canonical_codex_launch_args(args: Sequence[str]) -> list[str]:
    """Expand aliases and attached short values without interpreting option values."""
    value_flags = _CODEX_VALUE_FLAGS
    canonical: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            canonical.extend(args[index:])
            break
        if arg == "--yolo":
            canonical.append("--dangerously-bypass-approvals-and-sandbox")
        elif arg == "--not-so-yolo":
            canonical.append("--approve-for-me")
        elif arg in value_flags:
            canonical.append(arg)
            if index + 1 < len(args):
                index += 1
                canonical.append(args[index])
        elif (
            arg.startswith(("-c", "-s", "-a", "-p", "-C"))
            and len(arg) > 2
            and not arg.startswith(("-c=", "-s=", "-a="))
        ):
            canonical.extend((arg[:2], arg[2:].removeprefix("=")))
        elif arg.startswith(("--profile=", "--add-dir=", "--cd=")):
            canonical.extend(arg.split("=", 1))
        else:
            canonical.append(arg)
        index += 1
    return canonical


def codex_config_profile(args: Sequence[str]) -> str | None:
    """Read the file-profile selector, rejecting ambiguous or missing selectors."""
    canonical = canonical_codex_launch_args(args)
    profile: str | None = None
    index = 0
    while index < len(canonical):
        arg = canonical[index]
        if arg == "--":
            break
        if arg in {"--profile", "-p"}:
            if profile is not None or index + 1 == len(canonical):
                raise ValueError("Codex requires exactly one value for --profile")
            profile = canonical[index + 1]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
                raise ValueError("Invalid Codex config profile name")
        if arg in _CODEX_VALUE_FLAGS:
            index += 1
        index += 1
    return profile


def without_codex_config_profile(args: Sequence[str]) -> list[str]:
    canonical = canonical_codex_launch_args(args)
    profile = codex_config_profile(canonical)
    if profile is None:
        return canonical
    for index, arg in enumerate(canonical):
        if arg in {"--profile", "-p"} and canonical[index + 1] == profile:
            return [*canonical[:index], *canonical[index + 2 :]]
    return canonical


def codex_config_string(raw_value: str) -> str:
    """Read a ``-c key=value`` value as a string, tolerating Codex's unquoted form."""
    try:
        value = tomlkit.parse(f"value = {raw_value}")["value"]
    except Exception:  # noqa: BLE001 - Codex accepts some unquoted CLI config values.
        value = raw_value.strip().strip('"').strip("'")
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class CodexPermissionStance:
    """Which parts of the permission stance Codex launch args choose explicitly.

    :param approval_policy: An approval policy or a full bypass is chosen
        (``-a``, ``approval_policy``, ``--dangerously-bypass-approvals-and-sandbox``).
    :param sandbox: A sandbox is chosen (``-s``, ``sandbox_mode``).
    :param reviewer: A reviewer is chosen (``--approve-for-me``,
        ``approvals_reviewer``).
    :param permission_profile: A permission or config profile is chosen
        (``default_permissions``, ``--profile``).
    :param full_access_profile: The last ``default_permissions`` is the legacy
        ``":danger-full-access"`` profile.
    """

    approval_policy: bool
    sandbox: bool
    reviewer: bool
    permission_profile: bool
    full_access_profile: bool

    @property
    def explicit(self) -> bool:
        """Whether any part of the stance is chosen."""
        return self.approval_policy or self.sandbox or self.reviewer or self.permission_profile


def codex_permission_stance(args: Sequence[str]) -> CodexPermissionStance:
    """Scan Codex launch args for the permission stance they choose.

    Reads the canonical form (aliases and attached short values such as
    ``-sread-only`` expanded), stops at a ``--`` prompt separator and consumes
    ``-c`` / ``--config`` assignments, so it sees exactly what Codex would.

    :param args: Codex pass-through launch args, e.g.
        ``["-aon-request", "-c", 'sandbox_mode="read-only"']``.
    :returns: The explicit stance parts.
    :raises ValueError: when the args hold an invalid ``--profile`` selector.
    """
    canonical = canonical_codex_launch_args(args)
    full_access_profile = False
    permission_profile = codex_config_profile(canonical) is not None
    reviewer = False
    sandbox = False
    approval_policy = "--dangerously-bypass-approvals-and-sandbox" in canonical
    index = 0
    while index < len(canonical):
        arg = canonical[index]
        if arg == "--":
            break
        assignment: str | None = None
        if arg == "--approve-for-me":
            reviewer = True
        elif arg in {"--ask-for-approval", "-a"} or arg.startswith(("--ask-for-approval=", "-a=")):
            approval_policy = True
        elif arg in {"--sandbox", "-s"} or arg.startswith(("--sandbox=", "-s=")):
            sandbox = True
        elif arg in {"--config", "-c"} and index + 1 < len(canonical):
            index += 1
            assignment = canonical[index]
        elif arg.startswith(("--config=", "-c=")):
            assignment = arg.split("=", 1)[1]
        if assignment is not None:
            key, _, raw_value = assignment.partition("=")
            key = key.strip()
            if key == "approval_policy":
                approval_policy = True
            elif key == "sandbox_mode":
                sandbox = True
            elif key == "approvals_reviewer":
                reviewer = True
            elif key == "default_permissions":
                permission_profile = True
                full_access_profile = codex_config_string(raw_value) == ":danger-full-access"
        index += 1
    return CodexPermissionStance(
        approval_policy=approval_policy,
        sandbox=sandbox,
        reviewer=reviewer,
        permission_profile=permission_profile,
        full_access_profile=full_access_profile,
    )


def codex_launch_args_set_permission(args: Sequence[str]) -> bool:
    """Whether Codex launch args explicitly choose a permission stance.

    True for an approval policy, sandbox, reviewer, permission or config
    profile, bypass flag or ``--approve-for-me`` (see
    :func:`codex_permission_stance`).

    :param args: Codex pass-through launch args, e.g. ``["-sread-only"]``.
    :returns: Whether any stance part is chosen.
    :raises ValueError: when the args hold an invalid ``--profile`` selector.
    """
    return codex_permission_stance(args).explicit


def normalize_codex_permission_launch_args(
    terminal_launch_args: Sequence[str] | None,
) -> list[str]:
    """Complete permission launch args into their effective stance.

    Two normalizations:

    - Legacy Full Access args (a ``default_permissions=":danger-full-access"``
      profile with no approval policy) gain ``approval_policy="never"``.
    - The bare default stance — no approval policy, sandbox, permission
      profile, reviewer, or bypass flag — gains
      ``approvals_reviewer="auto_review"``. Codex's built-in default routes
      escalated approvals (e.g. an out-of-workspace write) to the human,
      which parks web turns on an approval card even though the default
      permission mode is presented as automatic. ``auto_review`` keeps the
      workspace-write sandbox and approval boundaries but lets Codex's
      automatic reviewer settle eligible requests instead of prompting.
      Any explicit approval/sandbox/reviewer/profile choice wins untouched.
    """
    args = canonical_codex_launch_args(terminal_launch_args or ())
    stance = codex_permission_stance(args)
    if stance.full_access_profile and not stance.approval_policy:
        args.extend(["-c", 'approval_policy="never"'])
    elif not stance.explicit:
        args.extend(["-c", 'approvals_reviewer="auto_review"'])
    return args


def _codex_resume_permission_params(
    terminal_launch_args: Sequence[str] | None, permission_profile: str | None = None
) -> dict[str, Any]:
    """Convert persisted Codex permission args into thread-resume overrides."""
    params: dict[str, Any] = {}
    typed_params: dict[str, Any] = {}
    args = normalize_codex_permission_launch_args(terminal_launch_args)
    index = 0
    approve_for_me = False
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        value: str | None = None
        if arg == "--approve-for-me":
            approve_for_me = True
        elif arg == "--dangerously-bypass-approvals-and-sandbox":
            typed_params.update(approvalPolicy="never", sandbox="danger-full-access")
        elif arg in {"--ask-for-approval", "-a", "--sandbox", "-s"}:
            if index + 1 < len(args):
                value = args[index + 1]
                index += 1
            if value is not None:
                field = "approvalPolicy" if arg in {"--ask-for-approval", "-a"} else "sandbox"
                typed_params[field] = value
        elif arg.startswith(("--ask-for-approval=", "-a=")):
            typed_params["approvalPolicy"] = arg.split("=", 1)[1]
        elif arg.startswith(("--sandbox=", "-s=")):
            typed_params["sandbox"] = arg.split("=", 1)[1]
        elif arg in {"--config", "-c"} and index + 1 < len(args):
            index += 1
            key, _, value = args[index].partition("=")
            _set_codex_resume_config_param(params, key, value)
        elif arg.startswith(("--config=", "-c=")):
            key, _, value = arg.split("=", 1)[1].partition("=")
            _set_codex_resume_config_param(params, key, value)
        index += 1
    if approve_for_me:
        if "sandbox" in typed_params:
            raise ValueError("--approve-for-me conflicts with sandbox and bypass flags")
        params.update(
            approvalsReviewer="auto_review", approvalPolicy="on-request", sandbox="workspace-write"
        )
    params.update(typed_params)
    if "permissions" in params:
        params.pop("sandbox", None)
    if (
        permission_profile
        and (
            params.get("permissions") == ":workspace"
            or (
                "permissions" not in params
                and params.get("sandbox", "workspace-write") == "workspace-write"
            )
        )
        and codex_config_profile(args) is None
    ):
        params.pop("sandbox", None)
        params["permissions"] = permission_profile
    return params


_CODEX_RESUME_PERMISSION_CONFIG_FIELDS = {
    "approval_policy": "approvalPolicy",
    "approvals_reviewer": "approvalsReviewer",
    "default_permissions": "permissions",
    "sandbox_mode": "sandbox",
}


def _set_codex_resume_config_param(params: dict[str, Any], key: str, raw_value: str) -> bool:
    key = key.strip()
    field = _CODEX_RESUME_PERMISSION_CONFIG_FIELDS.get(key)
    if field is not None:
        value = codex_config_string(raw_value)
        if not value:
            return False
        params[field] = value
        return True
    if key.split(".", 1)[0] not in {"sandbox_workspace_write", "network", "permissions"}:
        return False
    try:
        config_value = tomlkit.parse(f"value = {raw_value}").unwrap()["value"]
    except tomlkit.exceptions.TOMLKitError:
        config_value = raw_value.strip().strip("\"'")
    try:
        json.dumps(config_value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    config = cast(dict[str, Any], params.setdefault("config", {}))
    _set_codex_resume_config_value(config, key, config_value)
    return True


def _set_codex_resume_config_value(config: dict[str, Any], key: str, value: object) -> None:
    """Preserve CLI order without overlapping keys in the app-server's unordered config map."""
    for existing_key in list(config):
        if existing_key.startswith(f"{key}."):
            del config[existing_key]
    segments = key.split(".")
    for prefix_length in range(1, len(segments)):
        ancestor = ".".join(segments[:prefix_length])
        if ancestor not in config:
            continue
        target = config
        for segment in (ancestor, *segments[prefix_length:-1]):
            if not isinstance(target.get(segment), dict):
                target[segment] = {}
            target = cast(dict[str, Any], target[segment])
        target[segments[-1]] = value
        return
    config[key] = value


PROFILE_ID = "omnigent-workspace"
MIN_PROFILE_CODEX_VERSION = (0, 139, 0)
MIN_NATIVE_PROFILE_CODEX_VERSION = (0, 162, 0)


@dataclass(frozen=True)
class CodexWorkspaceProfile:
    """Overrides that define and select the generated profile."""

    profile_id: str
    config_overrides: tuple[str, ...]
    writable_roots: tuple[str, ...] = ()
    network_access: bool = False


def _workspace_profile_options(args: Sequence[str]) -> Iterator[tuple[str, ...]]:
    canonical = canonical_codex_launch_args(args)
    index = 0
    while index < len(canonical):
        arg = canonical[index]
        if arg == "--":
            yield tuple(canonical[index:])
            return
        option, separator, value = arg.partition("=")
        if option in _CODEX_VALUE_FLAGS:
            if separator:
                yield option, value
            elif index + 1 < len(canonical):
                index += 1
                yield option, canonical[index]
            else:
                yield (arg,)
        else:
            yield (arg,)
        index += 1


def codex_stance_is_workspace_write(
    args: Sequence[str], *, configured_sandbox_mode: str | None, bypass_sandbox: bool
) -> bool:
    """Use the resume parser's typed-selector precedence for the launch stance."""
    if bypass_sandbox or codex_config_profile(args) is not None:
        return False
    for option in _workspace_profile_options(args):
        if option[0] == "--dangerously-bypass-approvals-and-sandbox":
            return False
        if option[0] in {"--sandbox", "-s"} and option[1:] != ("workspace-write",):
            return False
    try:
        params = _codex_resume_permission_params(args)
    except ValueError:
        return False
    if "permissions" in params:
        return params["permissions"] == ":workspace"
    return params.get("sandbox", configured_sandbox_mode or "workspace-write") == "workspace-write"


def _git_profile_config_is_safe(top: Path, opened: Sequence[Path]) -> bool:
    def read_config(*args: str, missing_ok: bool = False) -> str:
        result = run_git(
            ["git", "-C", str(top), "config", *args],
            capture_output=True,
            env=_anchored_git_env(),
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0 and not (missing_ok and result.returncode == 1):
            raise ValueError("Git config could not be read")
        return result.stdout

    local_files = {opened[0] / "config", *(directory / "config.worktree" for directory in opened)}
    for config_file in local_files:
        if config_file.exists() and read_config(
            "--no-includes",
            "--file",
            str(config_file),
            "--get-regexp",
            r"^include(if\..*)?\.path$",
            missing_ok=True,
        ):
            return False
    records = read_config("--null", "--list", "--show-origin").split("\0")
    for index in range(0, len(records) - 1, 2):
        origin, assignment = records[index : index + 2]
        key, _, value = assignment.partition("\n")
        targets = [path for directory in opened for path in (directory, directory.resolve())]
        words = [value, *shlex.split(value)]
        for word in words:
            if any(character.isspace() for character in word):
                nested_words = shlex.split(word)
                if nested_words != [word]:
                    words.extend(nested_words)
        relative_targets = [Path(absolute_codex_path(word, top)).resolve() for word in words]
        if (
            re.search(r"(?<![\w.-])\.git(?![\w.-])", value)
            or re.search(r"\$\{?GIT_(?:COMMON_)?DIR", value)
            or (
                "rev-parse" in value
                and re.search(r"--(?:git-dir|git-common-dir|absolute-git-dir|git-path)\b", value)
            )
            or any(str(directory) in value for directory in targets)
            or any(
                target.is_relative_to(directory)
                for target in relative_targets
                for directory in targets
            )
        ):
            return False
        if re.fullmatch(r"include(if\..*)?\.path", key, re.IGNORECASE):
            source = Path(origin.removeprefix("file:"))
            target = Path(absolute_codex_path(value, source.parent)).resolve()
            if any(target.is_relative_to(directory) for directory in opened):
                return False
    return True


def codex_workspace_profile(
    workspace: Path,
    *,
    launch_args: Sequence[str],
    server_overrides: Sequence[str],
    private_config: Mapping[str, Any],
    codex_version: tuple[int, int, int] | None,
    workspace_write_stance: bool | None = None,
    windows_sdk: bool = False,
) -> CodexWorkspaceProfile | None:
    """Gate and build a repository permission profile; failures keep legacy behavior."""
    windows = os.name == "nt" and windows_sdk

    def decline(reason: str) -> None:
        if windows:
            _logger.warning("Codex Windows workspace permission profile disabled: %s", reason)

    try:
        if (
            (os.name != "posix" and not windows)
            or codex_version is None
            or codex_version < ((0, 163, 0) if windows else MIN_PROFILE_CODEX_VERSION)
        ):
            return decline("unsupported platform or Codex version")
        if private_config.get("default_permissions", ":workspace") != ":workspace":
            return decline("a custom permission profile is configured")
        options = list(_workspace_profile_options(launch_args))
        layers = [dict(private_config)]
        for tokens in [*(("-c", override) for override in server_overrides), *options]:
            if len(tokens) == 2 and tokens[0] in {"-c", "--config"}:
                key = tokens[1].partition("=")[0]
                if key.strip().split(".", 1)[0].strip("\"'") != "permissions":
                    continue
                layers.append(tomlkit.parse(tokens[1]).unwrap())
        for layer in layers:
            permissions = layer.get("permissions", {})
            if (isinstance(permissions, dict) and PROFILE_ID in permissions) or any(
                key == f"permissions.{PROFILE_ID}" or key.startswith(f"permissions.{PROFILE_ID}.")
                for key in layer
            ):
                return decline("generated permission profile name is already configured")
        for tokens in options:
            if len(tokens) == 2 and tokens[0] in {"-c", "--config"}:
                key, _, value = tokens[1].partition("=")
                if (
                    key.strip() == "default_permissions"
                    and codex_config_string(value) != ":workspace"
                ):
                    return decline("launch args select a custom permission profile")
        if workspace_write_stance is False or (
            workspace_write_stance is None
            and not codex_stance_is_workspace_write(
                launch_args,
                configured_sandbox_mode=private_config.get("sandbox_mode"),
                bypass_sandbox=False,
            )
        ):
            return decline("effective stance is not workspace-write")
        effective_cwd, additional_roots = codex_workspace_roots(launch_args, workspace)
        top = detect_git_root(effective_cwd)
        if top is None:
            return decline("workspace is not a repository")
        common = _git_common_dir(top)
        opened = [common]
        git_dir = None
        if (top / ".git").is_file():
            git_dir = linked_worktree_git_dir(top)
            if (
                git_dir is None
                or git_dir.resolve().parent != (common / "worktrees").resolve()
                or not all((common / name).exists() for name in ("HEAD", "objects", "refs"))
            ):
                return decline("unsupported repository layout")
            opened.append(git_dir)
        if windows:
            if (top / ".gitmodules").exists() or (common / "modules").exists():
                return decline("repository has submodules")
            result = run_git(
                [
                    "git",
                    "config",
                    "--no-includes",
                    "--file",
                    str(common / "config"),
                    "--type=bool",
                    "--get",
                    "extensions.worktreeConfig",
                ],
                capture_output=True,
                env=_anchored_git_env(),
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode not in (0, 1):
                return decline("common worktreeConfig could not be read")
            if result.stdout.strip() == "true":
                return decline("extensions.worktreeConfig is enabled")
            # Git ignores a missing config.worktree while worktreeConfig is disabled.
            # Other missing deny targets are unsafe.
            protected = [common / name for name in ("hooks", "config")]
            if git_dir is not None:
                protected.extend(git_dir / name for name in ("commondir", "gitdir"))
            if any(not path.exists() for path in protected):
                return decline("a protected Git path is missing")
        if not _git_profile_config_is_safe(top, opened):
            return decline("Git configuration references writable metadata")
        effective_config = {
            "sandbox_workspace_write": copy.deepcopy(
                private_config.get("sandbox_workspace_write", {})
            )
        }
        for tokens in [*(("-c", override) for override in server_overrides), *options]:
            if len(tokens) != 2 or tokens[0] not in {"-c", "--config"}:
                continue
            key, _, raw_value = tokens[1].partition("=")
            key = key.strip()
            if key == "sandbox_workspace_write" or key.startswith("sandbox_workspace_write."):
                value = tomlkit.parse(f"v = {raw_value}").unwrap()["v"]
                _set_codex_resume_config_value(effective_config, key, value)
        sandbox = effective_config["sandbox_workspace_write"]
        if not isinstance(sandbox, dict):
            return decline("invalid workspace sandbox configuration")
        roots = sandbox.get("writable_roots", [])
        if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
            return decline("invalid writable roots")
        if any(not Path(root).is_absolute() and not root.startswith("~") for root in roots):
            return decline("relative writable roots")
        writable_roots = [
            *(absolute_codex_path(root, effective_cwd) for root in roots),
            *additional_roots,
        ]
        # Preserve access to sibling worktrees/submodules already granted by an extra root.
        common_already_writable = any(
            common.resolve().is_relative_to(Path(root).resolve()) for root in writable_roots
        )
        automatic_roots: list[str] = []
        if sys.platform == "darwin":
            automatic_roots.append(str(Path.home() / "Library" / "Caches" / "ms-playwright"))
        entries = {str(directory): "write" for directory in opened}
        for name in ("hooks", "config", "config.worktree"):
            entries[str(common / name)] = "read"
        if windows or not common_already_writable:
            for name in ("modules", "worktrees"):
                entries[str(common / name)] = "read"
        if git_dir is not None:
            for name in ("commondir", "gitdir", "config.worktree"):
                entries[str(git_dir / name)] = "read"
        # More-specific entries re-open only this linked worktree's metadata.
        access_paths = [
            (candidate, access)
            for path, access in entries.items()
            for candidate in (Path(path), Path(path).resolve())
        ]
        for root in [*writable_roots, *automatic_roots]:
            covered = False
            for path in (Path(root), Path(root).resolve()):
                matches = [
                    (len(directory.parts), access)
                    for directory, access in access_paths
                    if path.is_relative_to(directory)
                ]
                if matches:
                    access = max(matches)[1]
                    if access == "read":
                        return decline("a writable root would reopen a protected Git path")
                    covered = True
            if not covered:
                entries[root] = "write"
        filesystem = ",".join(
            f"{json.dumps(path, ensure_ascii=False)}={json.dumps(access)}"
            for path, access in entries.items()
        )
        overrides = [
            f'permissions.{PROFILE_ID}.extends=":workspace"',
            f"permissions.{PROFILE_ID}.filesystem={{{filesystem}}}",
        ]
        if sandbox.get("network_access") is True:
            overrides.append(f"permissions.{PROFILE_ID}.network.enabled=true")
        overrides.append(f"default_permissions={json.dumps(PROFILE_ID)}")
        return CodexWorkspaceProfile(
            PROFILE_ID,
            tuple(overrides),
            tuple(str(Path(root).resolve()) for root in writable_roots),
            sandbox.get("network_access") is True,
        )
    except Exception:
        _logger.warning("Failed to build Codex workspace permission profile", exc_info=True)
        return None


def codex_workspace_roots(args: Sequence[str], cwd: Path) -> tuple[Path, list[str]]:
    """Resolve the working directory, ``--add-dir`` roots and linked-worktree gitdir.

    :param args: Codex CLI args, e.g. ``["--cd", "app", "--add-dir", "../shared"]``.
    :param cwd: Directory the launch starts in.
    :returns: The effective working directory and absolute additional writable roots.
    :raises ValueError: When ``--add-dir``, ``--cd`` or ``-C`` has no directory.
    """
    canonical = canonical_codex_launch_args(args)
    effective_cwd = cwd
    roots: list[str] = []
    index = 0
    while index < len(canonical):
        arg = canonical[index]
        if arg == "--":
            break
        if arg in {"--add-dir", "--cd", "-C"}:
            if index + 1 == len(canonical) or canonical[index + 1].startswith("-"):
                raise ValueError(f"Codex requires a directory for {arg}")
            if arg == "--add-dir":
                roots.append(canonical[index + 1])
            else:
                effective_cwd = Path(absolute_codex_path(canonical[index + 1], effective_cwd))
        if arg in _CODEX_VALUE_FLAGS:
            index += 1
        index += 1
    resolved_roots = [absolute_codex_path(root, effective_cwd) for root in roots]
    git_dir = linked_worktree_git_dir(effective_cwd)
    if git_dir is not None:
        resolved_roots.append(str(git_dir))
    return effective_cwd, list(dict.fromkeys(resolved_roots))


def without_codex_add_dirs(args: Sequence[str]) -> list[str]:
    """Drop ``--add-dir`` options, which Codex refuses on a ``--remote`` TUI."""
    canonical = canonical_codex_launch_args(args)
    stripped: list[str] = []
    index = 0
    while index < len(canonical):
        arg = canonical[index]
        if arg == "--":
            stripped.extend(canonical[index:])
            break
        if arg == "--add-dir":
            index += 2
            continue
        stripped.append(arg)
        if arg in _CODEX_VALUE_FLAGS and index + 1 < len(canonical):
            index += 1
            stripped.append(canonical[index])
        index += 1
    return stripped


def without_codex_positional_prompt(args: Sequence[str]) -> list[str]:
    """Drop the ``[PROMPT]`` positional and any ``--`` tail; options keep their values."""
    canonical = canonical_codex_launch_args(args)
    kept: list[str] = []
    index = 0
    while index < len(canonical):
        arg = canonical[index]
        if arg == "--":
            break
        if arg in _CODEX_VALUE_FLAGS:
            kept.extend(canonical[index : index + 2])
            index += 2
            continue
        if arg.startswith("-") and arg != "-":
            kept.append(arg)
        index += 1
    return kept


def _merge_tables(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_tables(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def _carry_config_edits(
    base: dict[str, Any], previous: dict[str, Any], current: dict[str, Any]
) -> None:
    for key in previous.keys() | current.keys():
        if key not in current:
            base.pop(key, None)
        elif key not in previous or current[key] != previous[key]:
            if all(isinstance(table.get(key), dict) for table in (previous, current)):
                if not isinstance(base.get(key), dict):
                    base[key] = {}
                _carry_config_edits(base[key], previous[key], current[key])
            else:
                base[key] = copy.deepcopy(current[key])


def _write_private_config(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_codex_mcp_servers(
    source_home: Path,
    profile: str | None,
    *,
    codex_version: tuple[int, int, int] | None,
    minimal_config: bool = False,
) -> dict[str, Any]:
    """Read current user MCPs; minimal mode omits ambient, not explicit profile MCPs."""

    def read(path: Path, *, optional: bool = False) -> dict[str, Any]:
        try:
            return tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
        except FileNotFoundError:
            if optional:
                return {}
            raise ValueError(f"Missing Codex MCP config: {path}") from None
        except OSError as error:
            reason = error.strerror or type(error).__name__
            raise ValueError(f"Cannot read Codex MCP config: {path} ({reason})") from None
        except ParseError as error:
            # Parser messages can contain config values; expose the location and kind only.
            raise ValueError(
                f"Invalid Codex MCP config: {path} "
                f"({type(error).__name__} at line {error.line}, column {error.col})"
            ) from None
        except (TOMLKitError, UnicodeError) as error:
            raise ValueError(
                f"Invalid Codex MCP config: {path} ({type(error).__name__})"
            ) from None

    def inventory(config: dict[str, Any], path: Path) -> dict[str, Any]:
        servers = config.get("mcp_servers", {})
        if not isinstance(servers, dict) or any(
            not isinstance(server, dict) for server in servers.values()
        ):
            raise ValueError(f"Invalid mcp_servers table in Codex config: {path}")
        return copy.deepcopy(servers)

    source_path = source_home / "config.toml"
    file_profile = codex_version is None or codex_version >= (0, 134, 0)
    # Minimal mode needs the base only to find an explicitly selected legacy profile.
    source = (
        read(source_path, optional=True)
        if not minimal_config or (profile is not None and not file_profile)
        else {}
    )
    servers = {} if minimal_config else inventory(source, source_path)
    if profile is not None:
        # Reject invalid profile names before deriving a file path from them.
        codex_config_profile(["--profile", profile])
        if file_profile:
            profile_path = source_home / f"{profile}.config.toml"
            overlay = read(profile_path)
        else:
            profile_path = source_path
            profiles = source.get("profiles", {})
            overlay = profiles.get(profile) if isinstance(profiles, dict) else None
            if not isinstance(overlay, dict):
                raise ValueError(f"Codex config profile {profile!r} does not exist")
        _merge_tables(servers, inventory(overlay, profile_path))
    return servers


def _profile_base(state_path: Path, current: dict[str, Any]) -> dict[str, Any]:
    try:
        state = tomlkit.parse(state_path.read_text()).unwrap()
    except TOMLKitError as error:
        raise ValueError(f"Invalid Codex profile state: {state_path}") from error
    if not all(isinstance(state.get(key), dict) for key in ("base", "applied")):
        raise ValueError(f"Invalid Codex profile state: {state_path}")
    for key in ("base", "applied", "pending"):
        value = state.get(key)
        if isinstance(value, dict):
            value.pop("developer_instructions", None)
    base = state["base"]
    if "pending" in state:
        if not isinstance(state["pending"], dict):
            raise ValueError(f"Invalid Codex profile state: {state_path}")
        if current not in (state["applied"], state["pending"]):
            raise ValueError(
                f"Codex config changed during an incomplete profile update: {state_path}"
            )
    else:
        _carry_config_edits(base, state["applied"], current)
    return base


def validate_codex_config_profile_state(codex_home: Path) -> None:
    """Validate an existing profile journal without modifying private config."""
    state_path = codex_home / ".omnigent-config-profile.toml"
    if not state_path.exists():
        return
    config_path = codex_home / "config.toml"
    current = tomlkit.parse(config_path.read_text()).unwrap() if config_path.exists() else {}
    current.pop("developer_instructions", None)
    _profile_base(state_path, current)


def materialize_codex_config_profile(
    codex_home: Path,
    source_home: Path,
    profile: str | None,
    *,
    codex_version: tuple[int, int, int] | None,
) -> None:
    """Fold the selected user profile into the private user layer, below project/CLI.

    Codex app-server has no file-profile selector. Preserve the base separately
    so switching/removing a profile on restart is reversible. Private TUI edits
    survive profile removal; selected profiles still take precedence over them.
    Journal atomic file replacements so interrupted updates can be retried.
    Never modify the source home.
    """
    state_path = codex_home / ".omnigent-config-profile.toml"
    if profile is None and not state_path.exists():
        return
    if codex_home.resolve() == source_home.resolve():
        raise ValueError("Codex profiles require a private CODEX_HOME")
    config_path = codex_home / "config.toml"
    current = (
        tomlkit.parse(config_path.read_text()) if config_path.exists() else tomlkit.document()
    )
    current_config = current.unwrap()
    current_instructions = current_config.pop("developer_instructions", _MISSING)
    base = _profile_base(state_path, current_config) if state_path.exists() else current_config
    merged = copy.deepcopy(base)
    if profile is not None:
        if codex_config_profile(["--profile", profile]) != profile:
            raise ValueError("Invalid Codex config profile name")
        if codex_version is None or codex_version >= (0, 134, 0):
            overlay = tomlkit.parse((source_home / f"{profile}.config.toml").read_text()).unwrap()
        else:
            overlay = base.get("profiles", {}).get(profile)
            if not isinstance(overlay, dict):
                raise ValueError(f"Codex config profile {profile!r} does not exist")
        overlay = copy.deepcopy(overlay)
        profile_instructions = overlay.pop("developer_instructions", _MISSING)
        _resolve_profile_paths(overlay, source_home)
        _merge_tables(merged, overlay)
        if overlay.get("sandbox_mode") is not None and overlay.get("default_permissions") is None:
            merged.pop("default_permissions", None)
    else:
        profile_instructions = _MISSING
    rendered_config = copy.deepcopy(merged)
    effective_instructions = (
        profile_instructions if profile_instructions is not _MISSING else current_instructions
    )
    if effective_instructions is not _MISSING:
        rendered_config["developer_instructions"] = effective_instructions
    rendered = tomlkit.dumps(rendered_config)
    pending_state = tomlkit.dumps({"base": base, "applied": current_config, "pending": merged})
    final_state = tomlkit.dumps({"base": base, "applied": merged})
    _write_private_config(state_path, pending_state)
    _write_private_config(config_path, rendered)
    _write_private_config(state_path, final_state)


def redact_codex_launch_args(args: Sequence[str]) -> list[str]:
    """
    Return the resolved Codex argv with secret-bearing values masked.

    Flag names, subcommands, paths and thread ids stay intact so a log row
    shows exactly which launch was attempted. Masked: the value of every
    ``-c``/``--config`` override outside :data:`_LOGGABLE_CONFIG_KEYS`, every
    ``NAME=value`` environment assignment (an ``env`` wrapper's config args),
    and the userinfo and query of any URL, whether it is a bare argument or
    attached to an option by ``=`` (e.g. ``--remote=ws://user:pw@host?sig=x``).
    Redaction never raises: a malformed URL is masked whole rather than
    aborting the launch it is meant to record.

    :param args: Resolved argv after the host's config args and Omnigent's
        remote args are merged, e.g. ``["OPENAI_API_KEY=sk-x", "codex", "-c",
        'model="gpt-5.4"', "resume", "--remote", "ws://127.0.0.1:1", "t1"]``.
    :returns: The argv with masked values, e.g. ``["OPENAI_API_KEY=***",
        "codex", "-c", 'model="gpt-5.4"', "resume", "--remote",
        "ws://127.0.0.1:1", "t1"]``.
    """
    redacted: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-c", "--config"} and index + 1 < len(args):
            redacted.extend((arg, _redact_config_override(args[index + 1])))
            index += 2
            continue
        if arg.startswith(("-c=", "--config=")):
            flag, _, override = arg.partition("=")
            redacted.append(f"{flag}={_redact_config_override(override)}")
        elif arg.startswith("-c") and len(arg) > 2 and not arg.startswith("--"):
            redacted.append(f"-c{_redact_config_override(arg[2:])}")
        elif _URL_SCHEME.match(arg):
            redacted.append(_redact_url(arg))
        elif (attached := _ATTACHED_OPTION.match(arg)) and _URL_SCHEME.match(attached.group(2)):
            redacted.append(f"{attached.group(1)}={_redact_url(attached.group(2))}")
        elif not arg.startswith("-") and (match := _ENV_ASSIGNMENT.match(arg)):
            redacted.append(f"{match.group(1)}=***")
        else:
            redacted.append(arg)
        index += 1
    return redacted


def _redact_config_override(override: str) -> str:
    key, separator, _ = override.partition("=")
    if not separator or key.strip() in _LOGGABLE_CONFIG_KEYS:
        return override
    return f"{key}=***"


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        # urlsplit rejects some inputs (e.g. an unterminated IPv6 literal like
        # ``ws://[broken``). Mask the whole value so logging never aborts the
        # launch it records.
        return "***"
    if not parts.scheme:
        return "***"
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))
