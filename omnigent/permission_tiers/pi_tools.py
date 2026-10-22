"""Read Pi-owned registration facts from this launch's protected local bridge."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from omnigent.permission_tiers.host import HostPermissionState


@dataclass(frozen=True)
class PiToolRegistry:
    extensions: dict[str, str] = field(default_factory=dict)
    control_paths: tuple[str, ...] = ()
    reason: str = ""
    invalid_tools: frozenset[str] = frozenset()


def _object(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("symlinked registry")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("malformed registry")
    return value


def read_pi_tool_registry(bridge: Path, state: HostPermissionState) -> PiToolRegistry:
    """Never consume provenance sent in tool arguments or policy event data."""
    try:
        expected = _object(bridge / "permission_expected.json")
        registry = _object(bridge / "permission_tools.json")
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
        if guard.is_symlink() or hashlib.sha256(guard.read_bytes()).hexdigest() != expected.get(
            "extension_sha256"
        ):
            raise ValueError("changed bridge")
        tools = registry.get("tools")
        if not isinstance(tools, list):
            raise ValueError("missing tool registrations")
        extensions: dict[str, str] = {}
        controls: list[str] = []
        names: set[str] = set()
        invalid: set[str] = set()
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise ValueError("malformed tool registration")
            name = tool["name"]
            if name in names:
                raise ValueError("duplicate tool registration")
            names.add(name)
            source = tool.get("sourceInfo")
            if isinstance(source, dict):
                path = source.get("path")
                if source.get("source") == "builtin" and path == f"builtin:{name}":
                    continue
                if path in (str(guard), "<inline:omnigent-permission>"):
                    continue
            extensions[name] = "Pi extension source is not a verified user installation"
            if not isinstance(source, dict) or source.get("scope") != "user":
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
                if hashlib.sha256(real_entry.read_bytes()).hexdigest() != tool.get(
                    "source_sha256"
                ):
                    raise ValueError("source changed since Pi loaded it")
                extensions[name] = ""
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                invalid.add(name)
                extensions[name] = (
                    "Pi extension implementation or configuration changed; restart to verify it"
                )
        return PiToolRegistry(
            extensions, tuple(dict.fromkeys(controls)), invalid_tools=frozenset(invalid)
        )
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return PiToolRegistry(
            reason="Pi extension registry is missing, stale or unverifiable; restart to verify it"
        )
