"""Unified Windows CLI discovery, shim unwrapping, and path-length helpers.

Centralizes Windows-specific launch mechanics so every harness (Codex, Claude,
Pi, Antigravity/ACP) resolves off-PATH installs consistently:

- Three-layer Windows ``PATH`` merging (Process → Machine registry → User
  registry) plus known package-manager bin dirs (npm, pnpm, Volta, WinGet,
  Node, ``~/.local/bin``, Bun, Scoop), deduplicated case-insensitively.
- ``PATHEXT`` candidate probing for off-PATH directories (mirroring
  ``shutil.which``).
- Complete-template ``.cmd`` / ``.bat`` shim unwrapping (npm, pnpm, WinGet,
  Scoop) to the real native ``.exe`` or ``[node.exe, *node_args, script.js]``,
  preserving ``NODE_PATH`` semantics while never bypassing custom wrappers.
- Claude Code Windows shim resolution to the package's native ``claude.exe``.
- Exclusively allocated short runtime temp directories (via ``tempfile.mkdtemp``
  plus Win32 ``GetShortPathNameW`` when available) and unprivileged NTFS
  directory junctions (``_winapi.CreateJunction``) to keep Antigravity under
  Windows' 260-character ``MAX_PATH`` and avoid symlink elevation errors.

Keep this module stdlib-only and import-cheap: :mod:`omnigent._platform`
delegates to it early in startup.
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import logging
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

_logger = logging.getLogger(__name__)

#: Default executable extensions probed when ``PATHEXT`` is unset or empty.
WINDOWS_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"

#: Launcher shim extensions that ``CreateProcess`` should not invoke directly
#: when the underlying native executable or JS entrypoint can be launched.
WINDOWS_SHIM_SUFFIXES = frozenset({".cmd", ".bat", ".ps1"})

_MAX_SHIM_BYTES = 8192
_WINDOWS_ENV_VAR_RE = re.compile(r"%([^%]+)%")

# Standard npm cmd-shim header lines (exact sequence, case-insensitive).
_NPM_HEADER_LINES: tuple[str, ...] = (
    "@echo off",
    "goto start",
    ":find_dp0",
    "set dp0=%~dp0",
    "exit /b",
    ":start",
    "setlocal",
    "call :find_dp0",
)

# Node command invocation inside recognized npm / pnpm shims, capturing any
# safe node flags (e.g. ``--no-deprecation``) and the relative JS entrypoint.
_NODE_ARGS_RE = r"(?P<node_args>(?:--[A-Za-z0-9_=-]+\s+)*)"
_REL_JS_RE = r'"(?:%dp0%|%~dp0)[\\/]*(?P<script>[^"\r\n%]+\.(?:js|cjs|mjs))"\s+%\*$'

_NPM_NODE_EXEC_RE = re.compile(
    r"^(?:endLocal\s*&\s*goto\s+#_undefined_#\s+2>NUL\s*\|\|\s*title\s+%COMSPEC%\s*&\s*)?"
    r"(?:set\s+PATHEXT=%PATHEXT:[^\"&|<>%\r\n]*%\s*&\s*)?"
    r'@?"%_prog%"\s+' + _NODE_ARGS_RE + _REL_JS_RE,
    re.IGNORECASE,
)

_DIRECT_NODE_EXEC_RE = re.compile(
    r"^@?node(?:\.exe)?\s+" + _NODE_ARGS_RE + _REL_JS_RE,
    re.IGNORECASE,
)

_SIBLING_NODE_EXEC_RE = re.compile(
    r'^@?"(?:%dp0%|%~dp0)[\\/]*node\.exe"\s+' + _NODE_ARGS_RE + _REL_JS_RE,
    re.IGNORECASE,
)

# Native executable invocation inside npm / pnpm / WinGet / Scoop .cmd shims:
#   "%dp0%\node_modules\pkg\bin\tool.exe" %*
#   "%~dp0\..\Packages\Vendor.Pkg\tool.exe" %*
#   "C:\Path\To\tool.exe" %*
_NATIVE_SHIM_EXEC_RE = re.compile(
    r'^@?"(?:(?P<rel_base>%dp0%|%~dp0)[\\/]*(?P<rel>[^"\r\n%]+\.(?:exe|com))'
    r'|(?P<abs>(?:[A-Za-z]:[\\/]|/)[^"\r\n%]+\.(?:exe|com)))"\s+%\*$',
    re.IGNORECASE,
)

_PATHEXT_SET_RE = re.compile(
    r'^@?set\s+"?pathext=%pathext:[^"&|<>%\r\n]*%"?$',
    re.IGNORECASE,
)
_NODE_PATH_SIMPLE_RE = re.compile(
    r'^@?set\s+"?node_path=(?P<val>[^"&|<>\r\n]+?)"?$',
    re.IGNORECASE,
)
_NODE_PATH_IF_NOT_DEF_RE = re.compile(r"^@?if\s+not\s+defined\s+node_path\s*\($", re.IGNORECASE)
_NODE_PATH_APPEND_RE = re.compile(
    r'^@?set\s+"?node_path=%node_path%;(?P<val>[^"&|<>\r\n]+?)"?$',
    re.IGNORECASE,
)
_IF_EXIST_SIBLING_NODE_RE = re.compile(
    r'^@?if\s+exist\s+"(?:%dp0%|%~dp0)[\\/]*node\.exe"\s*\($', re.IGNORECASE
)
_ELSE_BLOCK_RE = re.compile(r"^\)\s*else\s*\($", re.IGNORECASE)

# Known Claude Code native executable locations relative to a recognized shim
# directory (T3 Code ClaudeExecutable pattern, extended with pnpm global layout).
_CLAUDE_NATIVE_PACKAGE_ENTRIES: tuple[tuple[str, ...], ...] = (
    ("node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe"),
    ("global", "5", "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe"),
    ("..", "Packages", "Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe", "claude.exe"),
    ("claude.exe",),
)


@dataclass(frozen=True)
class UnwrappedWindowsShim:
    """Target extracted from a recognized Windows ``.cmd`` / ``.bat`` launcher shim."""

    kind: Literal["native", "node"]
    target: Path
    node_args: tuple[str, ...] = ()
    node_path_append: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)


def _is_windows(is_windows: bool | None = None) -> bool:
    if is_windows is not None:
        return is_windows
    from omnigent import _platform

    return _platform.IS_WINDOWS


def _env_get_ci(env: Mapping[str, str], name: str) -> str | None:
    """Read *name* from *env* case-insensitively (preferring exact match)."""
    if name in env:
        return env[name]
    target = name.upper()
    for key, value in env.items():
        if key.upper() == target:
            return value
    return None


def merge_shim_env(
    base_env: Mapping[str, str] | None,
    shim_env: Mapping[str, str],
    *,
    deny_exact: Sequence[str] = (),
) -> dict[str, str] | None:
    """Merge shim-derived env vars (such as ``NODE_PATH``) into *base_env*.

    Returns *base_env* as a ``dict`` only when *shim_env* adds or updates a key;
    when *shim_env* is empty, returns ``None`` if *base_env* was ``None`` so
    POSIX callers preserve ``env=None`` semantics.
    """
    if not shim_env:
        return None if base_env is None else dict(base_env)
    denied = {name.upper() for name in deny_exact}
    merged = dict(os.environ if base_env is None else base_env)
    for key, value in shim_env.items():
        upper = key.upper()
        if upper in denied:
            continue
        existing_key = next((k for k in merged if k.upper() == upper), None)
        if upper == "NODE_PATH" and existing_key is not None and merged[existing_key]:
            # Append shim NODE_PATH cleanly without clobbering caller entries.
            current = merged[existing_key]
            if value.lower() not in current.lower().split(";"):
                merged[existing_key] = f"{current};{value}"
        elif existing_key is None:
            merged[key] = value
    return merged


def _expand_windows_env_vars(value: str, env: Mapping[str, str]) -> str:
    """Expand Windows ``%VAR%`` references against *env* case-insensitively."""
    if "%" not in value:
        return value
    lookup = {key.upper(): val for key, val in env.items()}

    def _replace(match: re.Match[str]) -> str:
        var = match.group(1).upper()
        return lookup.get(var, match.group(0))

    return _WINDOWS_ENV_VAR_RE.sub(_replace, value)


def _read_registry_path(scope: Literal["machine", "user"]) -> str | None:
    """Read Windows ``Path`` from the Machine (HKLM) or User (HKCU) registry."""
    try:
        winreg: Any = importlib.import_module("winreg")
    except ImportError:
        return None
    if scope == "machine":
        root = winreg.HKEY_LOCAL_MACHINE
        subkey = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    else:
        root = winreg.HKEY_CURRENT_USER
        subkey = r"Environment"
    try:
        with winreg.OpenKey(root, subkey) as key:
            value, _reg_type = winreg.QueryValueEx(key, "Path")
    except OSError:
        return None
    return value if isinstance(value, str) and value.strip() else None


def _split_path_entries(raw: str, *, windows_only_delimiter: bool = False) -> list[str]:
    """Split a PATH string without breaking Windows drive letters (``C:\\...``)."""
    if not raw:
        return []
    if windows_only_delimiter or ";" in raw or os.name == "nt":
        parts = raw.split(";")
    elif len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        # Single drive-rooted Windows path on a POSIX test host.
        parts = [raw]
    else:
        parts = raw.split(os.pathsep)
    entries: list[str] = []
    for part in parts:
        cleaned = part.strip().strip('"').strip()
        if cleaned:
            entries.append(cleaned)
    return entries


def _normalize_windows_path_key(entry: str) -> str:
    """Canonical lowercase key for case-insensitive Windows PATH deduplication."""
    cleaned = entry.strip().strip('"').strip()
    if len(cleaned) > 3 and cleaned.endswith(("\\", "/")):
        cleaned = cleaned.rstrip("\\/")
    elif (
        len(cleaned) > 1
        and cleaned.endswith("/")
        and not (len(cleaned) == 3 and cleaned[1] == ":")
    ):
        cleaned = cleaned.rstrip("/")
    return cleaned.lower()


def _join_windows_dir(base: Path | str, *parts: str) -> Path:
    """Join directory segments preserving Windows backslashes in synthetic strings."""
    raw = str(base)
    if os.name != "nt" and "\\" in raw:
        return Path(ntpath.join(raw, *parts))
    return Path(base, *parts)


def windows_registry_path_dirs(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Return deduplicated Machine + User Windows registry ``PATH`` directories."""
    source = os.environ if env is None else env
    dirs: list[Path] = []
    seen: set[str] = set()
    for scope in ("machine", "user"):
        raw = _read_registry_path(scope)
        if not raw:
            continue
        expanded = _expand_windows_env_vars(raw, source)
        for entry in _split_path_entries(expanded, windows_only_delimiter=True):
            key = _normalize_windows_path_key(entry)
            if key and key not in seen:
                seen.add(key)
                dirs.append(Path(entry))
    return tuple(dirs)


def windows_known_cli_dirs(
    env: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> tuple[Path, ...]:
    """Return well-known Windows CLI install directories in discovery priority order.

    Keeps ``%APPDATA%\\npm`` and ``%PROGRAMFILES%\\nodejs`` first so existing
    discovery precedence is preserved, followed by pnpm, Volta, per-user Node,
    WinGet Links, ``.local\\bin``, Bun, and Scoop.
    """
    source = os.environ if env is None else env
    resolved_home = Path.home() if home is None else home

    appdata_raw = _env_get_ci(source, "APPDATA")
    appdata: Path | str = (
        appdata_raw if appdata_raw else _join_windows_dir(resolved_home, "AppData", "Roaming")
    )

    local_appdata_raw = _env_get_ci(source, "LOCALAPPDATA")
    local_appdata: Path | str = (
        local_appdata_raw
        if local_appdata_raw
        else _join_windows_dir(resolved_home, "AppData", "Local")
    )

    user_profile_raw = _env_get_ci(source, "USERPROFILE")
    user_profile: Path | str = user_profile_raw if user_profile_raw else resolved_home

    candidates: list[Path] = [_join_windows_dir(appdata, "npm")]
    if program_files := _env_get_ci(source, "PROGRAMFILES"):
        candidates.append(_join_windows_dir(program_files, "nodejs"))
    if pnpm_home := _env_get_ci(source, "PNPM_HOME"):
        candidates.append(Path(pnpm_home))
    candidates.append(_join_windows_dir(local_appdata, "pnpm"))
    if volta_home := _env_get_ci(source, "VOLTA_HOME"):
        candidates.append(_join_windows_dir(volta_home, "bin"))
    candidates.extend(
        [
            _join_windows_dir(local_appdata, "Volta", "bin"),
            _join_windows_dir(local_appdata, "Programs", "nodejs"),
            _join_windows_dir(local_appdata, "Microsoft", "WinGet", "Links"),
            _join_windows_dir(user_profile, ".local", "bin"),
        ]
    )
    if bun_install := _env_get_ci(source, "BUN_INSTALL"):
        candidates.append(_join_windows_dir(bun_install, "bin"))
    candidates.append(_join_windows_dir(user_profile, ".bun", "bin"))
    if scoop := _env_get_ci(source, "SCOOP"):
        candidates.append(_join_windows_dir(scoop, "shims"))
    candidates.append(_join_windows_dir(user_profile, "scoop", "shims"))

    deduped: list[Path] = []
    seen: set[str] = set()
    for directory in candidates:
        key = _normalize_windows_path_key(str(directory))
        if key and key not in seen:
            seen.add(key)
            deduped.append(directory)
    return tuple(deduped)


def merged_windows_path(
    env: Mapping[str, str] | None = None,
    *,
    include_fallback_dirs: bool = True,
    home: Path | None = None,
    is_windows: bool | None = None,
) -> str:
    """Merge Process, Machine registry, User registry, and known Windows CLI dirs.

    Process entries come first so explicit session/test overrides win, followed
    by Machine and User registry ``PATH``, then known CLI directories. Entries
    are deduplicated case-insensitively. When ``OMNIGENT_CLI_FALLBACK_DIRS`` is
    set in *env*, registry and default global dirs are skipped (keeping
    hermetic test environments isolated).

    No-op on POSIX: returns the existing ``PATH`` value unchanged.
    """
    source = os.environ if env is None else env
    process_path = _env_get_ci(source, "PATH") or ""
    if not _is_windows(is_windows):
        return process_path

    from omnigent._platform import CLI_FALLBACK_DIRS_ENV_VAR

    merged: list[str] = []
    seen: set[str] = set()

    def _add(entry: str) -> None:
        key = _normalize_windows_path_key(entry)
        if key and key not in seen:
            seen.add(key)
            merged.append(entry)

    for entry in _split_path_entries(process_path):
        _add(entry)

    override = _env_get_ci(source, CLI_FALLBACK_DIRS_ENV_VAR)
    if override is not None:
        if include_fallback_dirs:
            for entry in _split_path_entries(override):
                _add(entry)
        return ";".join(merged)

    for directory in windows_registry_path_dirs(source):
        _add(str(directory))
    if include_fallback_dirs:
        for directory in windows_known_cli_dirs(source, home=home):
            _add(str(directory))
    return ";".join(merged)


def windows_pathext_candidate_names(
    name: str,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Return Windows ``PATHEXT`` spellings for *name*, matching ``shutil.which`` order.

    Places ``PATHEXT`` matches ahead of the bare name unless *name* already
    carries a ``PATHEXT`` suffix, so npm's bare POSIX ``codex`` shim never
    shadows ``codex.cmd`` or ``codex.exe``.
    """
    source = os.environ if env is None else env
    pathext = (_env_get_ci(source, "PATHEXT") or "").strip() or WINDOWS_DEFAULT_PATHEXT
    extensions: list[str] = []
    for raw_ext in pathext.split(";"):
        ext = raw_ext.strip()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        extensions.append(ext)
    if not extensions:
        extensions = list(WINDOWS_DEFAULT_PATHEXT.split(";"))
    names = [name + ext for ext in extensions]
    suffix = os.path.splitext(name)[1].upper()
    if any(suffix == ext.upper() for ext in extensions):
        names.insert(0, name)
    else:
        names.append(name)
    return names


def probe_windows_binary_in_dir(
    directory: Path,
    name: str,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Probe *directory* for *name* using Windows ``PATHEXT`` precedence.

    Scans *directory* once so matching is case-insensitive while preserving the
    actual on-disk filename spelling across Windows, macOS APFS, and Linux.
    Returns ``None`` cleanly when *directory* does not exist or is unreadable.
    """
    source = os.environ if env is None else env
    candidates = windows_pathext_candidate_names(name, env=source)
    pathext_set = {
        ext.strip().lower()
        for ext in ((_env_get_ci(source, "PATHEXT") or WINDOWS_DEFAULT_PATHEXT).split(";"))
        if ext.strip()
    }
    try:
        entries = {entry.name.lower(): entry.name for entry in os.scandir(directory)}
    except OSError:
        return None
    for candidate_name in candidates:
        actual_name = entries.get(candidate_name.lower())
        if actual_name is None:
            continue
        candidate = directory / actual_name
        if not os.path.isfile(candidate):
            continue
        suffix = os.path.splitext(actual_name)[1].lower()
        if suffix in pathext_set or os.name == "nt" or os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _resolve_shim_relative_path(shim_dir: Path, raw_relative: str) -> Path:
    normalized = raw_relative.replace("\\", "/").lstrip("/")
    return (shim_dir / normalized).resolve(strict=False)


def _expand_shim_dp0(value: str, shim_dir: Path) -> str:
    prefix = str(shim_dir)
    if not prefix.endswith(("\\", "/")):
        prefix += os.sep
    return re.sub(r"(?:%dp0%|%~dp0)[\\/]*", lambda _m: prefix, value, flags=re.IGNORECASE)


def _parse_node_args(raw: str | None) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return ()
    return tuple(raw.strip().split())


def _parse_npm_cmd_shim(lines: list[str], shim_dir: Path) -> UnwrappedWindowsShim | None:
    """Match npm's exact ``cmd-shim`` layout (native exe or Node JS)."""
    if len(lines) < len(_NPM_HEADER_LINES) + 1:
        return None
    if tuple(line.lower() for line in lines[: len(_NPM_HEADER_LINES)]) != _NPM_HEADER_LINES:
        return None
    rest = lines[len(_NPM_HEADER_LINES) :]

    # Native executable npm shim: header followed immediately by "<target>.exe" %*
    if len(rest) == 1:
        native = _NATIVE_SHIM_EXEC_RE.match(rest[0])
        if native is None:
            return None
        if native.group("rel") is not None:
            target = _resolve_shim_relative_path(shim_dir, native.group("rel"))
        else:
            raw_abs = native.group("abs")
            target = Path(raw_abs.replace("\\", "/") if os.name != "nt" else raw_abs)
        return UnwrappedWindowsShim(kind="native", target=target)

    # Node JS npm shim: IF EXIST "%dp0%\node.exe" (...) ELSE (...) followed by exec line.
    idx = 0
    if not _IF_EXIST_SIBLING_NODE_RE.match(rest[idx]):
        return None
    idx += 1
    if idx >= len(rest) or rest[idx].lower() not in {
        'set "_prog=%dp0%\\node.exe"',
        'set "_prog=%dp0%/node.exe"',
        "set _prog=%dp0%\\node.exe",
    }:
        return None
    idx += 1
    if idx >= len(rest) or not _ELSE_BLOCK_RE.match(rest[idx]):
        return None
    idx += 1
    if idx >= len(rest) or rest[idx].lower() not in {'set "_prog=node"', "set _prog=node"}:
        return None
    idx += 1
    if idx < len(rest) and _PATHEXT_SET_RE.match(rest[idx]):
        idx += 1
    if idx >= len(rest) or rest[idx] != ")":
        return None
    idx += 1
    if idx != len(rest) - 1:
        return None
    match = _NPM_NODE_EXEC_RE.match(rest[idx])
    if match is None:
        return None
    target = _resolve_shim_relative_path(shim_dir, match.group("script"))
    return UnwrappedWindowsShim(
        kind="node",
        target=target,
        node_args=_parse_node_args(match.group("node_args")),
    )


def _parse_pnpm_cmd_shim(lines: list[str], shim_dir: Path) -> UnwrappedWindowsShim | None:
    """Match ``@pnpm/cmd-shim`` Node/native templates (including conditional blocks)."""
    idx = 0
    if idx < len(lines) and lines[idx].lower() in {"@echo off", "echo off"}:
        idx += 1
    if idx < len(lines) and lines[idx].lower() in {"@setlocal", "setlocal"}:
        idx += 1

    extra_env: dict[str, str] = {}
    node_path_append: str | None = None
    seen_pathext = False
    seen_node_path = False

    # Optional PATHEXT and NODE_PATH header statements may appear in either order
    # (at most one of each; no arbitrary commands permitted).
    while idx < len(lines):
        if not seen_pathext and _PATHEXT_SET_RE.match(lines[idx]):
            seen_pathext = True
            idx += 1
            continue
        if not seen_node_path and _NODE_PATH_IF_NOT_DEF_RE.match(lines[idx]):
            if idx + 4 >= len(lines):
                return None
            m_if = _NODE_PATH_SIMPLE_RE.match(lines[idx + 1])
            m_else = _ELSE_BLOCK_RE.match(lines[idx + 2])
            m_append = _NODE_PATH_APPEND_RE.match(lines[idx + 3])
            if m_if is None or m_else is None or m_append is None or lines[idx + 4] != ")":
                return None
            if m_if.group("val").strip() != m_append.group("val").strip():
                return None
            expanded = _expand_shim_dp0(m_if.group("val").strip(), shim_dir)
            extra_env["NODE_PATH"] = expanded
            node_path_append = expanded
            seen_node_path = True
            idx += 5
            continue
        if not seen_node_path and (m_simple := _NODE_PATH_SIMPLE_RE.match(lines[idx])):
            expanded = _expand_shim_dp0(m_simple.group("val").strip(), shim_dir)
            extra_env["NODE_PATH"] = expanded
            node_path_append = expanded
            seen_node_path = True
            idx += 1
            continue
        break

    rest = lines[idx:]
    if not rest:
        return None

    # Standard @pnpm/cmd-shim conditional block:
    #   @IF EXIST "%~dp0\node.exe" (
    #     "%~dp0\node.exe" [flags] "<script>" %*
    #   ) ELSE (
    #     @SET PATHEXT=%PATHEXT:;.JS;=;%
    #     node [flags] "<script>" %*
    #   )
    if _IF_EXIST_SIBLING_NODE_RE.match(rest[0]):
        if len(rest) not in (5, 6):
            return None
        sibling_match = _SIBLING_NODE_EXEC_RE.match(rest[1])
        if sibling_match is None or not _ELSE_BLOCK_RE.match(rest[2]):
            return None
        else_idx = 3
        if len(rest) == 6:
            if not _PATHEXT_SET_RE.match(rest[else_idx]):
                return None
            else_idx += 1
        direct_match = _DIRECT_NODE_EXEC_RE.match(rest[else_idx])
        if direct_match is None or rest[else_idx + 1] != ")":
            return None
        if sibling_match.group("script").lower() != direct_match.group(
            "script"
        ).lower() or _parse_node_args(sibling_match.group("node_args")) != _parse_node_args(
            direct_match.group("node_args")
        ):
            return None
        target = _resolve_shim_relative_path(shim_dir, direct_match.group("script"))
        return UnwrappedWindowsShim(
            kind="node",
            target=target,
            node_args=_parse_node_args(direct_match.group("node_args")),
            node_path_append=node_path_append,
            extra_env=extra_env,
        )

    # Compact single-command shim (e.g. `@SETLOCAL` + `node "%~dp0\...\cli.js" %*`,
    # `@node "%dp0%\cli.js" %*`, single-line `@"%_prog%" "%dp0%\...\cli.js" %*`,
    # or WinGet/Scoop `"%~dp0\...\tool.exe" %*`).
    if len(rest) == 1:
        direct_match = _DIRECT_NODE_EXEC_RE.match(rest[0])
        if direct_match is None and idx == 0:
            direct_match = _NPM_NODE_EXEC_RE.match(rest[0])
        if direct_match is not None:
            target = _resolve_shim_relative_path(shim_dir, direct_match.group("script"))
            return UnwrappedWindowsShim(
                kind="node",
                target=target,
                node_args=_parse_node_args(direct_match.group("node_args")),
                node_path_append=node_path_append,
                extra_env=extra_env,
            )
        native_match = _NATIVE_SHIM_EXEC_RE.match(rest[0])
        if native_match is not None:
            if native_match.group("rel") is not None:
                target = _resolve_shim_relative_path(shim_dir, native_match.group("rel"))
            else:
                raw_abs = native_match.group("abs")
                target = Path(raw_abs.replace("\\", "/") if os.name != "nt" else raw_abs)
            return UnwrappedWindowsShim(kind="native", target=target, extra_env=extra_env)

    return None


def unwrap_windows_cmd_shim(shim_path: Path) -> UnwrappedWindowsShim | None:
    """Parse a complete, recognized npm / pnpm / WinGet / Scoop ``.cmd`` or ``.bat`` shim.

    Returns ``None`` for non-batch files, unreadable/oversized files, or custom
    wrapper scripts that execute extra commands, set arbitrary env vars, or exit
    early.
    """
    if shim_path.suffix.lower() not in {".cmd", ".bat"}:
        return None
    try:
        if not shim_path.is_file() or shim_path.stat().st_size > _MAX_SHIM_BYTES:
            return None
        text = shim_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    return _parse_npm_cmd_shim(lines, shim_path.parent) or _parse_pnpm_cmd_shim(
        lines, shim_path.parent
    )


def _resolve_binary_in_windows_env(name: str, env: Mapping[str, str]) -> str | None:
    """Resolve *name* strictly against *env*'s ``PATH`` and fallback configuration."""
    if os.path.isabs(name) or "/" in name or "\\" in name:
        cmd_path = Path(name)
        probed = probe_windows_binary_in_dir(cmd_path.parent, cmd_path.name, env=env)
        if probed:
            return probed
        return str(cmd_path) if cmd_path.is_file() else None

    env_path = _env_get_ci(env, "PATH")
    if env_path:
        for entry in _split_path_entries(env_path):
            probed = probe_windows_binary_in_dir(Path(entry), name, env=env)
            if probed:
                return probed

    from omnigent._platform import CLI_FALLBACK_DIRS_ENV_VAR

    fallback_override = _env_get_ci(env, CLI_FALLBACK_DIRS_ENV_VAR)
    if fallback_override is not None:
        for entry in _split_path_entries(fallback_override):
            probed = probe_windows_binary_in_dir(Path(entry), name, env=env)
            if probed:
                return probed
        return None

    for directory in windows_registry_path_dirs(env=env):
        probed = probe_windows_binary_in_dir(directory, name, env=env)
        if probed:
            return probed
    for directory in windows_known_cli_dirs(env=env):
        probed = probe_windows_binary_in_dir(directory, name, env=env)
        if probed:
            return probed
    return None


def _resolve_node_for_shim(
    shim_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
    node_resolver: Callable[[str], str | None] | None = None,
) -> str | None:
    sibling = shim_dir / "node.exe"
    if sibling.is_file():
        return str(sibling)
    if node_resolver is not None:
        return node_resolver("node")
    if env is not None:
        return _resolve_binary_in_windows_env("node", env)
    from omnigent._platform import resolve_cli_binary

    return resolve_cli_binary("node")


def windows_launch_spec(
    binary_path: str,
    *,
    strict_batch: bool = False,
    missing_target_prefix: str = "Shim entry point does not exist",
    missing_node_error: str = "Node.js runtime not found for Windows CLI shim",
    unknown_batch_error: str = (
        "Batch launcher is not a recognized npm/pnpm shim; configure a native executable instead"
    ),
    env: Mapping[str, str] | None = None,
    node_resolver: Callable[[str], str | None] | None = None,
    is_windows: bool | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Return ``(argv_prefix, extra_env)`` to execute *binary_path* without ``cmd.exe``.

    On POSIX this is a strict no-op returning ``([binary_path], {})``.
    On Windows it unwraps recognized npm / pnpm / WinGet / Scoop ``.cmd`` and
    ``.bat`` shims into either ``[real.exe]`` or
    ``[node.exe, *node_args, script.js]``, plus any ``NODE_PATH`` declared by a
    pnpm shim. Custom batch wrappers are kept intact unless *strict_batch* is
    ``True``, in which case ``OSError`` is raised.
    """
    if not _is_windows(is_windows):
        return [binary_path], {}

    path = Path(binary_path)
    suffix = path.suffix.lower()
    if suffix in {".cmd", ".bat"}:
        unwrapped = unwrap_windows_cmd_shim(path)
        if unwrapped is None:
            if strict_batch:
                raise OSError(unknown_batch_error)
            return [binary_path], {}
        if unwrapped.kind == "native":
            if not unwrapped.target.is_file():
                if strict_batch:
                    raise FileNotFoundError(f"{missing_target_prefix}: {unwrapped.target}")
                return [binary_path], {}
            return [str(unwrapped.target)], dict(unwrapped.extra_env)
        script = unwrapped.target
        if not script.is_file():
            if strict_batch:
                raise FileNotFoundError(f"{missing_target_prefix}: {script}")
            return [binary_path], {}
        node = _resolve_node_for_shim(path.parent, env=env, node_resolver=node_resolver)
        if not node:
            if strict_batch:
                raise FileNotFoundError(missing_node_error)
            return [binary_path], {}
        return [node, *unwrapped.node_args, str(script)], dict(unwrapped.extra_env)

    if suffix in {".js", ".cjs", ".mjs"}:
        script = path.resolve(strict=False)
        if not script.is_file():
            if strict_batch:
                raise FileNotFoundError(f"{missing_target_prefix}: {script}")
            return [binary_path], {}
        node = _resolve_node_for_shim(path.parent, env=env, node_resolver=node_resolver)
        if not node:
            if strict_batch:
                raise FileNotFoundError(missing_node_error)
            return [binary_path], {}
        return [node, str(script)], {}

    if suffix == ".py":
        return [sys.executable, "-P", str(path)], {}

    return [binary_path], {}


def windows_cli_argv(
    binary_path: str,
    *,
    strict_batch: bool = False,
    missing_target_prefix: str = "Shim entry point does not exist",
    missing_node_error: str = "Node.js runtime not found for Windows CLI shim",
    unknown_batch_error: str = (
        "Batch launcher is not a recognized npm/pnpm shim; configure a native executable instead"
    ),
    node_resolver: Callable[[str], str | None] | None = None,
    is_windows: bool | None = None,
) -> list[str]:
    """Return the direct argv prefix for *binary_path* (unwrapping Windows shims)."""
    argv, _ = windows_launch_spec(
        binary_path,
        strict_batch=strict_batch,
        missing_target_prefix=missing_target_prefix,
        missing_node_error=missing_node_error,
        unknown_batch_error=unknown_batch_error,
        node_resolver=node_resolver,
        is_windows=is_windows,
    )
    return argv


def _is_recognized_ps1_shim(path: Path) -> bool:
    """Match a complete npm/pnpm generated ``.ps1`` shim, rejecting custom scripts."""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_SHIM_BYTES:
            return False
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return False
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        return False
    if lines[0].lower() != "$basedir=split-path $myinvocation.mycommand.definition -parent":
        return False
    if lines[-1].lower() != "exit $ret":
        return False
    # Reject any early exit statement or unrecognized top-level commands outside
    # the standard npm / @pnpm/cmd-shim PowerShell wrapper template.
    has_ret_init = False
    has_exec_branch = False
    for line in lines[1:-1]:
        lower = line.lower()
        if re.search(r"\bexit\b", lower):
            return False
        if lower == "$ret=0":
            has_ret_init = True
        if "& " in line and "$args" in lower:
            has_exec_branch = True
        if not (
            lower.startswith(
                (
                    "$exe=",
                    "$ret=",
                    "$env:node_path=",
                    "$env_node_path=",
                    "$pathsep=",
                    "$basedir=",
                    "if ",
                    "} else {",
                    "}",
                    "{",
                    "& ",
                    "$input | & ",
                )
            )
        ):
            return False
    return has_ret_init and has_exec_branch


def resolve_claude_windows_executable(
    resolved_path: str,
    *,
    is_windows: bool | None = None,
) -> str:
    """Unwrap a recognized Windows Claude Code shim to its native ``claude.exe``.

    Python's ``claude-agent-sdk`` spawns ``[options.cli_path, ...]`` directly
    via ``anyio.open_process`` without a shell; handing it a native
    ``claude.exe`` avoids ``cmd.exe`` argument mangling and job-object escapes.
    Custom wrapper scripts that do not match a recognized package-manager shim
    are preserved intact. No-op on POSIX or when *resolved_path* is already a
    native binary.
    """
    if not _is_windows(is_windows):
        return resolved_path
    path = Path(resolved_path)
    suffix = path.suffix.lower()
    if suffix not in WINDOWS_SHIM_SUFFIXES:
        return resolved_path

    if suffix in {".cmd", ".bat"}:
        unwrapped = unwrap_windows_cmd_shim(path)
        if unwrapped is None:
            # Do not bypass a custom user batch wrapper.
            return resolved_path
        if unwrapped.kind == "native" and unwrapped.target.is_file():
            return str(unwrapped.target)
        if unwrapped.kind == "node":
            for candidate in (
                unwrapped.target.parent / "bin" / "claude.exe",
                unwrapped.target.parent / "claude.exe",
            ):
                if candidate.is_file():
                    return str(candidate.resolve(strict=False))
        for segments in _CLAUDE_NATIVE_PACKAGE_ENTRIES:
            candidate = path.parent.joinpath(*segments)
            if candidate.is_file():
                return str(candidate.resolve(strict=False))
        _logger.warning(
            "Claude launcher shim %r resolved without a native claude.exe beside it; "
            "keeping shim path.",
            resolved_path,
        )
        return resolved_path

    # .ps1 shim: only probe known package entries when the file is a generated npm/pnpm shim.
    if not _is_recognized_ps1_shim(path):
        return resolved_path
    for segments in _CLAUDE_NATIVE_PACKAGE_ENTRIES:
        candidate = path.parent.joinpath(*segments)
        if candidate.is_file():
            return str(candidate.resolve(strict=False))
    _logger.warning(
        "Claude launcher shim %r resolved without a native claude.exe beside it; "
        "keeping shim path.",
        resolved_path,
    )
    return resolved_path


def _split_windows_command_line(command: str) -> list[str]:
    """Split *command* using Win32 ``CommandLineToArgvW`` quoting rules.

    Preserves Windows backslashes and handles mid-token quotes such as
    ``--label="a b"`` without splitting on the inner space.
    """
    args: list[str] = []
    i = 0
    length = len(command)
    while i < length:
        while i < length and command[i] in " \t\r\n":
            i += 1
        if i >= length:
            break
        if command[i] == "'":
            # Retain compatibility with legacy shlex.join-serialized Windows
            # commands such as "'C:\\Program Files\\agy.exe' acp".
            closing = command.find("'", i + 1)
            if closing != -1 and (closing + 1 == length or command[closing + 1] in " \t\r\n"):
                args.append(command[i + 1 : closing])
                i = closing + 1
                continue
        token: list[str] = []
        in_quotes = False
        while i < length:
            ch = command[i]
            if not in_quotes and ch in " \t\r\n":
                break
            if ch == "\\":
                slash_count = 0
                while i < length and command[i] == "\\":
                    slash_count += 1
                    i += 1
                if i < length and command[i] == '"':
                    token.append("\\" * (slash_count // 2))
                    if slash_count % 2 == 1:
                        token.append('"')
                        i += 1
                    else:
                        if in_quotes and i + 1 < length and command[i + 1] == '"':
                            token.append('"')
                            i += 2
                        else:
                            in_quotes = not in_quotes
                            i += 1
                else:
                    token.append("\\" * slash_count)
                continue
            if ch == '"':
                if in_quotes and i + 1 < length and command[i + 1] == '"':
                    token.append('"')
                    i += 2
                else:
                    in_quotes = not in_quotes
                    i += 1
                continue
            token.append(ch)
            i += 1
        if in_quotes:
            raise ValueError("No closing quotation")
        args.append("".join(token))
    return args


def join_command_argv(argv: Sequence[str], *, is_windows: bool | None = None) -> str:
    """Serialize *argv* for the active platform (Win32 quoting on Windows, POSIX shlex)."""
    if not _is_windows(is_windows):
        return shlex.join(argv)
    return subprocess.list2cmdline(argv)


def split_command_argv(command: str, *, is_windows: bool | None = None) -> list[str]:
    """Split a configured command string into argv without stripping Windows backslashes."""
    if not _is_windows(is_windows):
        return shlex.split(command)
    return _split_windows_command_line(command)


def resolve_windows_command_argv(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    is_windows: bool | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Resolve and unwrap ``argv[0]`` on Windows via the unified launcher base.

    When *env* is provided, both the top-level binary and any Node runtime for
    unwrapped JS shims resolve strictly against *env* (honoring *env*'s
    ``PATH`` and ``OMNIGENT_CLI_FALLBACK_DIRS`` without leaking parent lookup).
    On POSIX this returns ``(list(argv), {})`` untouched.
    """
    items = list(argv)
    if not items or not _is_windows(is_windows):
        return items, {}
    if env is not None and which is None:
        resolved = _resolve_binary_in_windows_env(items[0], env) or items[0]
        prefix, extra_env = windows_launch_spec(resolved, env=env, is_windows=True)
        return [*prefix, *items[1:]], extra_env
    from omnigent._platform import resolve_cli_binary

    resolved = resolve_cli_binary(items[0], which=which) or items[0]
    prefix, extra_env = windows_launch_spec(resolved, env=env, is_windows=True)
    return [*prefix, *items[1:]], extra_env


def _get_short_path_name_win32(path: str) -> str | None:
    """Call Win32 ``GetShortPathNameW``; returns ``None`` off Windows or on error."""
    if os.name != "nt":
        return None
    windll: Any = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    kernel32 = windll.kernel32
    required = int(kernel32.GetShortPathNameW(path, None, 0))
    if required <= 0:
        return None
    buffer = ctypes.create_unicode_buffer(required)
    written = int(kernel32.GetShortPathNameW(path, buffer, required))
    if written <= 0 or written >= required:
        return None
    return buffer.value


def windows_short_path(path: Path, *, is_windows: bool | None = None) -> str:
    """Return Windows' 8.3 short path for *path* when available, else ``str(path)``."""
    raw = str(path)
    if not _is_windows(is_windows):
        return raw
    with contextlib.suppress(OSError, AttributeError, ValueError):
        short = _get_short_path_name_win32(raw)
        if short:
            return short
    return raw


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or is_directory_junction(path)


def windows_short_temp_dir(
    prefix: str,
    *,
    env: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
    is_windows: bool | None = None,
) -> tuple[Path, str]:
    """Exclusively allocate a short temp directory to keep Antigravity under ``MAX_PATH``.

    Tries candidate roots shortest-first (explicit *base_dir*, else real
    non-link directories from ``LOCALAPPDATA``, ``TEMP``, ``TMP``, and
    :func:`tempfile.gettempdir`), allocating an exclusively owned leaf with
    :func:`tempfile.mkdtemp` so an existing directory is never adopted and an
    unwritable shorter root cleanly falls back to the next eligible root.
    """
    candidates: list[Path] = []
    fallback_candidates: list[Path] = []
    if base_dir is not None:
        candidates.append(base_dir)
    else:
        source = os.environ if env is None else env
        seen: set[str] = set()
        for raw in (
            _env_get_ci(source, "LOCALAPPDATA"),
            _env_get_ci(source, "TEMP"),
            _env_get_ci(source, "TMP"),
        ):
            if not raw or not raw.strip():
                continue
            candidate = Path(raw.strip())
            key = _normalize_windows_path_key(str(candidate))
            if key in seen or not candidate.is_dir() or _is_link_or_junction(candidate):
                continue
            seen.add(key)
            candidates.append(candidate)
        candidates.sort(key=lambda item: len(str(item)))
        # Keep tempfile.gettempdir() as a fallback after env roots so a broken
        # default temp lookup never blocks a usable LOCALAPPDATA root.
        try:
            default_tmp = tempfile.gettempdir()
        except OSError:
            default_tmp = None
        if default_tmp:
            candidate = Path(default_tmp)
            key = _normalize_windows_path_key(str(candidate))
            if key not in seen and candidate.is_dir() and not _is_link_or_junction(candidate):
                fallback_candidates.append(candidate)

    last_error: OSError | None = None
    safe_prefix = prefix.rstrip("-") + "-"
    for root in (*candidates, *fallback_candidates):
        try:
            created = Path(tempfile.mkdtemp(prefix=safe_prefix, dir=str(root)))
        except OSError as exc:
            last_error = exc
            continue
        return created, windows_short_path(created, is_windows=is_windows)
    raise OSError(f"Could not allocate short temp directory for {prefix!r}") from last_error


def _create_windows_junction(target: Path, link: Path) -> bool:
    """Create an NTFS directory junction via ``_winapi.CreateJunction`` when available."""
    try:
        import _winapi  # type: ignore[import-not-found]
    except ImportError:
        return False
    create_junction = getattr(_winapi, "CreateJunction", None)
    if create_junction is None:
        return False
    existed_before = link.exists() or _is_link_or_junction(link)
    try:
        create_junction(str(target), str(link))
    except OSError:
        # CPython's CreateJunction creates the destination directory before
        # setting the reparse point; remove only an empty dir this call created.
        if not existed_before and link.is_dir() and not _is_link_or_junction(link):
            with contextlib.suppress(OSError):
                link.rmdir()
        raise
    return True


def is_directory_junction(path: Path) -> bool:
    """Return whether *path* is a Windows NTFS directory junction."""
    checker = getattr(path, "is_junction", None)
    if callable(checker):
        try:
            return bool(checker())
        except OSError:
            return False
    return False


def remove_existing_link(link: Path) -> None:
    """Remove an existing symlink, NTFS junction, or regular file without touching targets."""
    if is_directory_junction(link):
        link.rmdir()
        return
    if link.is_symlink() or link.is_file():
        link.unlink()


def create_directory_link(
    target: Path,
    link: Path,
    *,
    is_windows: bool | None = None,
) -> None:
    """Link *link* to directory *target*, using an NTFS junction on Windows.

    NTFS junctions work without Administrator privileges or Developer Mode,
    avoiding ``WinError 1314`` from ``os.symlink`` on standard Windows hosts.
    """
    resolved = target.resolve()
    if _is_windows(is_windows) and _create_windows_junction(resolved, link):
        return
    link.symlink_to(resolved, target_is_directory=True)


def link_or_copy_path(
    source: Path,
    dest: Path,
    *,
    is_windows: bool | None = None,
) -> None:
    """Link *dest* to *source* (junction for Windows dirs, copy fallback for Windows files)."""
    if source.is_dir():
        create_directory_link(source, dest, is_windows=is_windows)
        return
    if _is_windows(is_windows):
        try:
            dest.symlink_to(source.resolve(), target_is_directory=False)
        except OSError:
            shutil.copy2(source, dest)
        return
    dest.symlink_to(source.resolve(), target_is_directory=False)
