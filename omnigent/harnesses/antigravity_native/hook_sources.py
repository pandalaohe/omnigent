"""Fail-closed startup discovery of third-party Antigravity PreToolUse hooks."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

_ALIASES = (".agents", ".agent", "_agents", "_agent")
_REMEDY = 'Choose full for a new session, or set that hook "enabled": false in its source file.'


def _invalid(path: Path, reason: str) -> ValueError:
    return ValueError(f"Antigravity restricted startup refused: {reason}: {path}. {_REMEDY}")


def _stat(path: Path):
    try:
        return path.stat()
    except FileNotFoundError:
        if path.is_symlink():
            raise _invalid(path, "broken source link") from None
        return None
    except OSError:
        raise _invalid(path, "unreadable hook source") from None


def _read(path: Path, *, required: bool = False) -> dict[str, Any] | None:
    if _stat(path) is None:
        if required:
            raise _invalid(path, "missing declared source")
        return None

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, ValueError):
        raise _invalid(path, "unreadable or invalid JSON hook source") from None
    if not isinstance(value, dict):
        raise _invalid(path, "expected a JSON object")
    return value


def _children(path: Path, *, required: bool = False) -> list[Path]:
    info = _stat(path)
    if info is None and not required:
        return []
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise _invalid(path, "unavailable plugin directory")
    try:
        return sorted(path.iterdir())
    except OSError:
        raise _invalid(path, "unreadable plugin directory") from None


def _hooks(value: object, path: Path, owned: dict[str, object] | None = None) -> None:
    if not isinstance(value, dict):
        raise _invalid(path, "invalid hooks object")
    for name, spec in value.items():
        if not isinstance(spec, dict) or type(spec.get("enabled", True)) is not bool:
            raise _invalid(path, f"invalid hook {name!r}")
        if owned is not None and name == "omnigent-guard" and spec == owned:
            continue
        if spec.get("enabled", True) is False:
            continue
        groups = spec.get("PreToolUse", [])
        if not isinstance(groups, list):
            raise _invalid(path, f"invalid PreToolUse hook {name!r}")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise _invalid(path, f"invalid PreToolUse hook {name!r}")
            if group["hooks"]:
                raise _invalid(path, f"enabled third-party PreToolUse hook {name!r}")


def check_agy_hook_sources(workspace: Path, tier: str, bridge_dir: Path) -> None:
    """Inspect original and effective sources; never change the user's hook files."""
    if tier == "full":
        return
    if tier not in ("ask", "plan", "accept-edits", "auto"):
        raise ValueError("Unknown Antigravity permission tier")
    from omnigent.harnesses.antigravity_native.bridge import _agy_guard_hook, agy_gemini_dir

    try:
        workspace = workspace.resolve(strict=True)
    except OSError:
        raise _invalid(workspace, "unavailable workspace") from None
    project_dirs = [workspace]
    for candidate in (workspace, *workspace.parents):
        if _stat(candidate / ".git") is not None:
            project_dirs = [workspace, *workspace.parents][
                : (workspace, *workspace.parents).index(candidate) + 1
            ]
            break
    project_root = project_dirs[-1]
    gemini_dirs = (Path.home() / ".gemini", agy_gemini_dir(bridge_dir))
    for gemini in gemini_dirs:
        _stat(gemini)
    owned_path = agy_gemini_dir(bridge_dir) / "config" / "hooks.json"
    preferences_by_gemini: dict[Path, dict[str, list[bool]]] = {}
    for gemini in gemini_dirs:
        plugin_preferences: dict[str, list[bool]] = {}
        preferences_by_gemini[gemini] = plugin_preferences
        for path in (
            gemini / "config.json",
            gemini / "config" / "config.json",
            gemini / "antigravity-cli" / "config.json",
        ):
            config = _read(path)
            if config is None:
                continue
            preferences = config.get("plugins", {})
            if not isinstance(preferences, dict):
                raise _invalid(path, "invalid plugin preferences")
            for name, preference in preferences.items():
                if not isinstance(preference, dict) or type(preference.get("enabled")) is not bool:
                    raise _invalid(path, f"invalid plugin preference {name!r}")
                plugin_preferences.setdefault(name, []).append(preference["enabled"])
        for path in (gemini / "settings.json", gemini / "antigravity-cli" / "settings.json"):
            settings = _read(path)
            if settings is not None:
                _hooks(settings.get("hooks", {}), path)

    def plugin(
        path: Path, plugin_preferences: dict[str, list[bool]], *, declared: bool = False
    ) -> None:
        manifests = [
            _read(path / relative)
            for relative in (
                "plugin.json",
                ".antigravity-plugin/plugin.json",
                ".gemini-plugin/plugin.json",
            )
        ]
        declarations = [manifest for manifest in manifests if manifest is not None]
        if not declarations and not declared:
            return
        preferences = plugin_preferences.get(path.name, [])
        # Only this config root's preferences can disable its discovered plugin.
        if preferences and not any(preferences):
            return
        if (
            not preferences
            and declarations
            and all(item.get("disabled") is True for item in declarations)
        ):
            return
        hooks = _read(path / "hooks.json")
        if hooks is not None:
            _hooks(hooks, path / "hooks.json")

    def configured_paths(raw: object, source: Path) -> list[Path]:
        if not isinstance(raw, str) or not raw:
            raise _invalid(source, "invalid declared plugin path")
        path = Path(raw).expanduser()
        if path.is_absolute():
            return [path]
        # Vendor docs describe both declaring-directory and repository fallbacks.
        # Inspect all existing candidates so ambiguous relative paths cannot hide hooks.
        candidates = {source.parent.parent / path, source.parent / path, project_root / path}
        existing = [candidate for candidate in sorted(candidates) if _stat(candidate) is not None]
        if not existing:
            raise _invalid(source, "missing declared plugin path")
        return existing

    def manifest(
        path: Path,
        plugin_preferences: dict[str, list[bool]],
        active: frozenset[Path] = frozenset(),
    ) -> None:
        try:
            canonical = path.resolve(strict=True)
        except OSError:
            raise _invalid(path, "unavailable plugin manifest") from None
        if canonical in active or len(active) >= 64:
            raise _invalid(path, "cyclic or excessive plugin manifest inheritance")
        config = _read(path, required=True)
        assert config is not None
        for kind in ("inherits", "entries"):
            entries = config.get(kind, [])
            if not isinstance(entries, list):
                raise _invalid(path, f"invalid plugin {kind}")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise _invalid(path, f"invalid plugin {kind}")
                # Vendor pattern matching is not specified precisely enough to
                # exempt a source. Check every candidate, including filtered ones.
                for key in ("include_only", "exclude"):
                    if key in entry and (
                        not isinstance(entry[key], list)
                        or any(not isinstance(pattern, str) for pattern in entry[key])
                    ):
                        raise _invalid(path, f"invalid {key} patterns")
                for target in configured_paths(entry.get("path"), path):
                    if kind == "inherits":
                        manifest(target, plugin_preferences, active | {canonical})
                    else:
                        for child in _children(target, required=True):
                            info = _stat(child)
                            if info is not None and stat.S_ISDIR(info.st_mode):
                                plugin(child, plugin_preferences, declared=True)

    roots = [(gemini / "config", preferences_by_gemini[gemini]) for gemini in gemini_dirs]
    roots.extend(
        (directory / alias, preferences_by_gemini[gemini_dirs[1]])
        for directory in project_dirs
        for alias in _ALIASES
    )
    for root, plugin_preferences in roots:
        _stat(root)
        hooks_path = root / "hooks.json"
        hooks = _read(hooks_path)
        if hooks is not None:
            _hooks(
                hooks,
                hooks_path,
                _agy_guard_hook(bridge_dir) if hooks_path == owned_path else None,
            )
        settings = _read(root / "settings.json")
        if settings is not None:
            _hooks(settings.get("hooks", {}), root / "settings.json")
        for child in _children(root / "plugins"):
            info = _stat(child)
            if info is not None and stat.S_ISDIR(info.st_mode):
                plugin(child, plugin_preferences)
        plugin_config = root / "plugins.json"
        if _stat(plugin_config) is not None:
            manifest(plugin_config, plugin_preferences)
        imports_path = root / "import_manifest.json"
        imports = _read(imports_path)
        if imports is not None:
            entries = imports.get("imports", [])
            if not isinstance(entries, list):
                raise _invalid(imports_path, "invalid imported plugins")
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("name"), str)
                    or Path(entry["name"]).name != entry["name"]
                    or entry["name"] in ("", ".", "..")
                ):
                    raise _invalid(imports_path, "invalid imported plugin name")
                imported = root / "plugins" / entry["name"]
                if _stat(imported) is None:
                    raise _invalid(imports_path, "missing imported plugin")
                plugin(imported, plugin_preferences, declared=True)
