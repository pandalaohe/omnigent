"""Read Pi-owned registration facts from this launch's protected local bridge."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from omnigent._platform import IS_WINDOWS
from omnigent.permission_tiers.host import HostPermissionState


@dataclass(frozen=True)
class PiToolRegistry:
    extensions: dict[str, str] = field(default_factory=dict)
    control_paths: tuple[str, ...] = ()
    reason: str = ""
    invalid_tools: frozenset[str] = frozenset()
    tool_names: frozenset[str] = frozenset()
    registration_digest: str | None = None
    native_tools: dict[str, str] = field(default_factory=dict)
    bridge_tools: frozenset[str] = frozenset()
    unsupported_tools: frozenset[str] = frozenset()


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
    # Windows path and handle stats can report different ctimes for the same file.
    return identity if IS_WINDOWS else (*identity, info.st_ctime_ns)


def _read_file(
    path: Path,
    *,
    resolved_target: Path | None = None,
    require_nofollow: bool = False,
) -> bytes:
    """Read one stable file identity; an admitted entry alias must retain its target."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if require_nofollow and not nofollow and not IS_WINDOWS:
        raise ValueError("No-follow file validation is unavailable")
    before = path.lstat()
    if resolved_target is None and not stat.S_ISREG(before.st_mode):
        raise ValueError("Registration file is not regular")
    resolved = path.resolve(strict=True)
    if resolved_target is not None and resolved != resolved_target:
        raise ValueError("Registration target changed")
    target_before = resolved.lstat()
    if not stat.S_ISREG(target_before.st_mode):
        raise ValueError("Registration target is not regular")
    fd = os.open(
        resolved if resolved_target is not None else path,
        os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0),
    )
    try:
        opened = os.fstat(fd)
        if _file_identity(opened) != _file_identity(target_before):
            raise ValueError("Registration changed before open")
        chunks = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        if (
            _file_identity(os.fstat(fd)) != _file_identity(opened)
            or _file_identity(path.lstat()) != _file_identity(before)
            or _file_identity(resolved.lstat()) != _file_identity(opened)
            or path.resolve(strict=True) != resolved
        ):
            raise ValueError("Registration changed during read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _object(path: Path, *, require_nofollow: bool = False) -> dict:
    value = json.loads(_read_file(path, require_nofollow=require_nofollow))
    if not isinstance(value, dict):
        raise ValueError("malformed registry")
    return value


def pi_permission_control_path(
    bridge: Path, name: str, expected: Mapping[str, object] | None = None
) -> Path:
    """Keep RPC receipts beside their unique, process-owned extension file."""
    if expected is None:
        expected = _object(bridge / "permission_expected.json")
    if expected.get("harness") == "pi":
        extension = expected.get("extension_path")
        if not isinstance(extension, str) or not extension:
            raise ValueError("missing RPC extension path")
        return Path(f"{extension}.{name}")
    return bridge / name


def read_pi_tool_expectation(bridge: Path) -> dict[str, object]:
    """Read the launcher's protected enforcement identity before allocating a scope."""
    expected = _object(bridge / "permission_expected.json", require_nofollow=True)
    if expected.get("version") != 1 or expected.get("harness") not in ("pi", "pi-native"):
        raise ValueError("Invalid Pi permission expectation")
    guard = Path(expected["extension_path"])
    if (
        not guard.is_absolute()
        or not isinstance(expected.get("installation_id"), str)
        or not expected["installation_id"]
    ):
        raise ValueError("Incomplete Pi permission expectation")
    if hashlib.sha256(_read_file(guard, require_nofollow=True)).hexdigest() != expected.get(
        "extension_sha256"
    ):
        raise ValueError("Changed Pi enforcement source")
    return expected


def read_pi_tool_registry(
    bridge: Path,
    state: HostPermissionState,
    *,
    expected_binding: Mapping[str, object] | None = None,
) -> PiToolRegistry:
    """Never consume provenance sent in tool arguments or policy event data."""
    try:
        scoped = expected_binding is not None
        expected = _object(bridge / "permission_expected.json", require_nofollow=scoped)
        registry = _object(
            bridge / "permission_tools.json"
            if scoped
            else pi_permission_control_path(bridge, "permission_tools.json", expected),
            require_nofollow=scoped,
        )
        loaded: dict[str, dict] = {}
        if expected_binding is not None:
            if expected != dict(expected_binding) or expected.get("scope_protocol_version") != 2:
                raise ValueError("changed scope expectation")
            for key, value in expected_binding.items():
                if (
                    key not in ("extension_path", "tool_registry_version")
                    and registry.get(key) != value
                ):
                    raise ValueError("stale scope registry")
            rows = registry.get("loaded_extensions")
            if not isinstance(rows, list):
                raise ValueError("missing actual SDK loaded identities")
            for row in rows:
                if not isinstance(row, dict) or not all(
                    isinstance(row.get(key), str) and row[key]
                    for key in ("path", "resolved_path", "source_sha256")
                ):
                    raise ValueError("malformed loaded extension")
                prior = loaded.get(row["path"])
                if prior is not None and prior != row:
                    raise ValueError("ambiguous loaded extension")
                loaded[row["path"]] = row
            if not any(
                row["resolved_path"] == expected.get("extension_path")
                and row["source_sha256"] == expected.get("extension_sha256")
                for row in loaded.values()
            ):
                raise ValueError("missing loaded enforcement identity")
        for key in ("version", "installation_id", "session_id", "harness", "extension_sha256"):
            if registry.get(key) != expected.get(key):
                raise ValueError("stale registry")
        if (
            expected.get("version") != 1
            or expected.get("session_id") != state.session_id
            or expected.get("harness") not in ("pi", "pi-native")
            or not expected.get("installation_id")
        ):
            raise ValueError("unbound registry")
        guard = Path(expected["extension_path"])
        if hashlib.sha256(_read_file(guard, require_nofollow=scoped)).hexdigest() != expected.get(
            "extension_sha256"
        ):
            raise ValueError("changed bridge")
        tools = registry.get("tools")
        if not isinstance(tools, list):
            raise ValueError("missing tool registrations")
        extensions: dict[str, str] = {}
        controls: list[str] = []
        bridge_path = expected.get("bridge_extension_path")
        bridge_sha256 = expected.get("bridge_extension_sha256")
        if scoped and (bridge_path is not None or bridge_sha256 is not None):
            if (
                not isinstance(bridge_path, str)
                or not Path(bridge_path).is_absolute()
                or not isinstance(bridge_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", bridge_sha256)
                or hashlib.sha256(_read_file(Path(bridge_path), require_nofollow=True)).hexdigest()
                != bridge_sha256
            ):
                raise ValueError("changed scoped bridge identity")
            controls.append(bridge_path)
        names: set[str] = set()
        invalid: set[str] = set()
        native_tools: dict[str, str] = {}
        bridge_tools: set[str] = set()
        unsupported: set[str] = set()
        native_packages: dict[str, str] = {}
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise ValueError("malformed tool registration")
            name = tool["name"]
            if scoped and not name:
                raise ValueError("empty scoped tool name")
            if name in names:
                raise ValueError("duplicate tool registration")
            names.add(name)
            source = tool.get("sourceInfo")
            native_marker = (
                isinstance(source, dict)
                and isinstance(source.get("source"), str)
                and re.fullmatch(r"npm:pi-subagents(?:@[^/]+)?", source["source"])
            )
            if scoped and native_marker:
                native_tools[name] = "Native helper package source is not verified"
            if isinstance(source, dict):
                path = source.get("path")
                if source.get("source") == "builtin" and (
                    path == f"builtin:{name}"
                    or (name, path) == ("tool_search", "builtin:tool-search")
                ):
                    continue
                if path in (str(guard), "<inline:omnigent-permission>"):
                    bridge_tools.add(name)
                    continue
                if scoped and isinstance(bridge_path, str) and path == bridge_path:
                    identity = loaded.get(bridge_path)
                    if (
                        identity is None
                        or identity["resolved_path"] != bridge_path
                        or identity["source_sha256"] != bridge_sha256
                    ):
                        raise ValueError("unbound scoped bridge source")
                    bridge_tools.add(name)
                    continue
                if (
                    scoped
                    and source.get("source") == "inline"
                    and source.get("scope") == "temporary"
                    and source.get("origin") == "top-level"
                    and isinstance(path, str)
                    and path.startswith("<inline:")
                    and path.endswith(">")
                ):
                    # Record actual inline tools without granting them file-backed provenance.
                    unsupported.add(name)
                    continue
            if scoped and (
                not isinstance(source, dict)
                or not all(
                    isinstance(source.get(key), str) and source[key]
                    for key in ("path", "source", "scope", "origin", "baseDir")
                )
                or source.get("origin") not in ("package", "top-level")
                or source.get("source") in ("sdk", "builtin", "inline", "temporary")
                or not all(
                    isinstance(tool.get(key), str) and tool[key]
                    for key in (
                        "real_path",
                        "real_base_dir",
                        "implementation_root",
                        "source_sha256",
                    )
                )
            ):
                raise ValueError("incomplete scoped registration proof")
            extensions[name] = "Pi extension source is not a verified user installation"
            if not isinstance(source, dict) or (not scoped and source.get("scope") != "user"):
                continue
            if not all(
                isinstance(tool.get(key), str) and tool[key]
                for key in ("real_path", "real_base_dir", "implementation_root", "source_sha256")
            ):
                continue
            try:
                if source.get("origin") not in ("package", "top-level") or source.get(
                    "source"
                ) in (None, "", "sdk", "builtin", "inline", "temporary"):
                    continue
                if expected_binding is not None:
                    identity = loaded.get(source["path"])
                    if identity is None or identity["source_sha256"] != tool["source_sha256"]:
                        raise ValueError("unbound loaded tool source")
                    entry = Path(identity["resolved_path"])
                else:
                    entry = Path(source["path"])
                base = Path(source["baseDir"])
                root = Path(tool["implementation_root"])
                if not all(path.is_absolute() for path in (entry, base, root)):
                    raise ValueError("relative source")
                real_entry, real_base, real_root = (
                    path.resolve(strict=True) for path in (entry, base, root)
                )
                if (
                    str(real_entry) != tool.get("real_path")
                    or str(real_base) != tool.get("real_base_dir")
                    or real_root != root
                    or not real_entry.is_relative_to(real_root)
                    or not real_entry.is_file()
                    or not real_root.is_dir()
                    or not real_base.is_dir()
                    or any(
                        path in (Path(path.anchor), Path.home(), Path(state.workspace))
                        for path in (real_root, real_base)
                    )
                ):
                    raise ValueError("unprotected source location")
                controls.extend(str(path) for path in (entry, base, real_root, real_base))
                if hashlib.sha256(
                    _read_file(entry, resolved_target=real_entry, require_nofollow=scoped)
                ).hexdigest() != tool.get("source_sha256"):
                    raise ValueError("source changed since Pi loaded it")
                if (
                    base.resolve(strict=True) != real_base
                    or root.resolve(strict=True) != real_root
                ):
                    raise ValueError("source configuration target changed")
                if source.get("scope") == "user":
                    extensions[name] = ""
                if not scoped:
                    continue
                manifest_path = real_root / "package.json"
                try:
                    manifest_raw = _read_file(manifest_path, require_nofollow=scoped)
                    manifest = json.loads(manifest_raw)
                except (OSError, ValueError, TypeError):
                    if native_marker:
                        raise
                    manifest = {}
                    manifest_raw = b""
                if native_marker or (
                    isinstance(manifest, dict) and manifest.get("name") == "pi-subagents"
                ):
                    native_tools[name] = "Native helper package compatibility is not verified"
                    if (
                        not isinstance(manifest, dict)
                        or manifest.get("name") != "pi-subagents"
                        or not isinstance(manifest.get("version"), str)
                        or not re.fullmatch(r"0\.80\.\d+", manifest["version"])
                        or source.get("scope") != "user"
                    ):
                        raise ValueError("Unreviewed native helper package")
                    native_packages[str(manifest_path)] = hashlib.sha256(manifest_raw).hexdigest()
                    controls.append(str(manifest_path))
                    exports = manifest.get("exports")
                    missing = next(
                        (
                            key
                            for key in (
                                "./preflight",
                                "./required-child-extensions",
                                "./native-child-lifecycle",
                            )
                            if not isinstance(exports, dict) or not exports.get(key)
                        ),
                        None,
                    )
                    if missing:
                        native_tools[name] = (
                            f"pi-subagents is missing {missing}; subagent tools are off"
                        )
                        continue
                    native_tools[name] = ""
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                invalid.add(name)
                extensions[name] = (
                    "Pi extension implementation or configuration changed; restart to verify it"
                )
        return PiToolRegistry(
            extensions,
            tuple(dict.fromkeys(controls)),
            invalid_tools=frozenset(invalid),
            tool_names=frozenset(names),
            registration_digest=(
                hashlib.sha256(
                    json.dumps(
                        {"registry": registry, "native_packages": native_packages},
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
                if scoped
                else None
            ),
            native_tools=native_tools,
            bridge_tools=frozenset(bridge_tools),
            unsupported_tools=frozenset(unsupported),
        )
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return PiToolRegistry(
            reason="Pi extension registry is missing, stale or unverifiable; restart to verify it"
        )
