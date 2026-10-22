"""Helpers for seeding a managed Pi agent dir (``PI_CODING_AGENT_DIR``).

When the pi harness runs in gateway mode it relocates Pi's agent root to a
per-session temp directory (for ``models.json``). That hides the user's global
agent settings, context and resources. This module seeds those inputs while
leaving model/auth files and unrelated state owned by the managed directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias, cast
from urllib.parse import unquote, urlsplit

_logger = logging.getLogger(__name__)

_Settings: TypeAlias = dict[str, object]

# Default global Pi agent root (``PI_CODING_AGENT_DIR`` when unset).
DEFAULT_PI_AGENT_DIR = Path.home() / ".pi" / "agent"

# The SDK appends inline factories after file, package and built-in extensions.
_INLINE_LAUNCHER_JS = r"""
#!/usr/bin/env node
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const dist = __PI_SDK_DIR__;
const extensionPath = __OMNIGENT_EXTENSION__;
const resourceAgentSource = __PI_RESOURCE_AGENT_DIR__;
(async () => {
  const load = (name) => import(pathToFileURL(path.join(dist, name)).href);
  const { setupCli } = await load("cli/setup.js");
  const { main } = await load("main.js");
  const { DefaultResourceLoader } = await load("core/resource-loader.js");
  const { DefaultPackageManager } = await load("core/package-manager.js");
  const { SettingsManager } = await load("core/settings-manager.js");
  const { resolvePath } = await load("utils/paths.js");
  const resourceAgentDir = resolvePath(resourceAgentSource);
  // Managed model/auth files must not relocate the user's resource root.
  const resolve = DefaultPackageManager.prototype.resolve;
  DefaultPackageManager.prototype.resolve = function (...args) {
    if (path.resolve(this.agentDir) === resourceAgentDir) return resolve.apply(this, args);
    const settingsManager = SettingsManager.create(this.cwd, resourceAgentDir, {
      projectTrusted: this.settingsManager.isProjectTrusted(),
    });
    if (settingsManager.drainErrors().length) throw new Error("Invalid Pi resource settings");
    const resources = new DefaultPackageManager({
      cwd: this.cwd, agentDir: resourceAgentDir, settingsManager,
      builtinExtensions: this.builtinExtensions,
    });
    resources.setProgressCallback(this.progressCallback);
    return resolve.apply(resources, args);
  };
  const factory = require(extensionPath);
  if (typeof factory !== "function") throw new Error("Omnigent extension factory unavailable");
  // Validate the real loader result, including built-ins and project trust passes.
  const getExtensions = DefaultResourceLoader.prototype.getExtensions;
  DefaultResourceLoader.prototype.getExtensions = function () {
    const result = getExtensions.call(this);
    const last = result.extensions.at(-1);
    if (result.errors.length || !last || last.path !== "<inline:omnigent-permission>") {
      throw new Error("Pi cannot order all extensions before the Omnigent permission check");
    }
    return result;
  };
  process.env.OMNIGENT_PI_PERMISSION_INLINE = fs.realpathSync(extensionPath);
  setupCli();
  await main(process.argv.slice(2), {
    extensionFactories: [{ name: "omnigent-permission", factory }],
  });
})().catch(() => {
  console.error("Pi cannot prove permission extension ordering; check Pi SDK/extension errors.");
  process.exitCode = 1;
});
"""


def ordered_pi_launch(
    executable: str,
    args: list[str],
    *,
    extension: Path,
    resource_agent_dir: Path | None = None,
) -> tuple[str, list[str]]:
    """Use Pi's own CLI/resource loader with a verified final inline policy factory."""
    required = (
        "cli/setup.js",
        "main.js",
        "core/resource-loader.js",
        "core/package-manager.js",
        "core/settings-manager.js",
        "utils/paths.js",
    )
    dist = next(
        (
            parent
            for parent in Path(executable).resolve().parents
            if all((parent / name).is_file() for name in required)
        ),
        None,
    )
    if dist is None:
        raise ValueError(
            "Pi cannot prove extension ordering: this CLI installation does not expose "
            "the supported Pi SDK loader"
        )
    remaining: list[str] = []
    index = 0
    while index < len(args):
        if (
            args[index] == "--extension"
            and index + 1 < len(args)
            and args[index + 1] == str(extension)
        ):
            index += 2
            continue
        remaining.append(args[index])
        index += 1
    launcher = extension.with_name("omnigent_pi_permissions.cjs")
    source = (
        _INLINE_LAUNCHER_JS.lstrip()
        .replace("__PI_SDK_DIR__", json.dumps(str(dist)))
        .replace("__OMNIGENT_EXTENSION__", json.dumps(str(extension)))
        .replace(
            "__PI_RESOURCE_AGENT_DIR__",
            json.dumps(
                str(resource_agent_dir)
                if resource_agent_dir is not None
                else os.environ.get("PI_CODING_AGENT_DIR") or str(DEFAULT_PI_AGENT_DIR)
            ),
        )
    )
    launcher.write_text(source, encoding="utf-8")
    launcher.chmod(0o700)
    return str(launcher), remaining


_PI_RESOURCE_SETTINGS = ("extensions", "skills", "prompts", "themes")
_PI_AGENT_RESOURCE_DIRS = ("npm", "git", *_PI_RESOURCE_SETTINGS)
_PI_AGENT_RESOURCE_FILES = (
    "AGENTS.override.md",
    "AGENTS.md",
    "AGENTS.MD",
    "CLAUDE.md",
    "CLAUDE.MD",
    "SYSTEM.md",
    "APPEND_SYSTEM.md",
    "mcp.json",
)
_PI_SEED_MANIFEST = ".omnigent-resource-seeds.json"


def _normalize_pi_path(value: str) -> str:
    """Match Pi's source/local path forms before applying native path operations."""
    windows = os.name == "nt"
    if windows and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        match = re.fullmatch(r"/(?:mnt/|cygdrive/)?([a-z])(?:/(.*))?", value, re.IGNORECASE)
        if match:
            value = match[1].upper() + ":\\" + (match[2] or "").replace("/", "\\")
    if value == "~":
        return str(Path.home())
    if value.startswith("~/") or (windows and value.startswith("~\\")):
        return os.path.join(Path.home(), value[2:])
    if value.startswith("file://"):
        parsed = urlsplit(value)
        value = unquote(parsed.path)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            if not windows:
                raise ValueError("Pi agent file URLs must refer to the local host")
            value = "//" + parsed.netloc + value
        elif windows and re.match(r"^/[a-z]:", value, re.IGNORECASE):
            value = value[1:]
    return value.replace("/", "\\") if windows else value


def _resource_path(value: str, agent_root: Path) -> str:
    """Keep SDK-independent paths intact; anchor local paths at their original root."""
    if value.startswith(("npm:", "git:", "github:", "http:", "https:", "ssh:", "builtin:")):
        return value
    return os.path.join(agent_root, _normalize_pi_path(value))


def _relocate_resource_settings(settings: _Settings, agent_root: Path, managed_dir: Path) -> None:
    for key in _PI_RESOURCE_SETTINGS:
        entries = settings.get(key)
        if not isinstance(entries, list):
            continue
        relocated: list[object] = []
        for entry in entries:
            if not isinstance(entry, str):
                relocated.append(entry)
            elif entry.startswith(("!", "+", "-")):
                # Filters must cover both linked auto-discovery and original local paths.
                relocated.append(entry)
                pattern = entry[1:]
                if entry[0] in "+-" and pattern.startswith(("./", ".\\")):
                    pattern = pattern[2:]
                path = _resource_path(pattern, agent_root)
                if path != entry[1:]:
                    relocated.append(entry[0] + path)
                root_prefix = str(agent_root).rstrip("/\\") + os.sep
                if os.path.normcase(path).startswith(os.path.normcase(root_prefix)):
                    relocated.append(
                        entry[0] + os.path.join(managed_dir, path[len(root_prefix) :])
                    )
            else:
                relocated.append(_resource_path(entry.strip(), agent_root))
        settings[key] = relocated
    packages = settings.get("packages")
    if isinstance(packages, list):
        relocated_packages: list[object] = []
        for entry in packages:
            if isinstance(entry, str):
                entry = _resource_path(entry.strip(), agent_root)
            elif isinstance(entry, dict) and isinstance(entry.get("source"), str):
                entry = {**entry, "source": _resource_path(entry["source"].strip(), agent_root)}
            relocated_packages.append(entry)
        settings["packages"] = relocated_packages


def default_pi_thinking_level(cwd: str | None, model_selection: str | None = None) -> str:
    """Read Pi's startup default, including the project's settings override."""
    settings = _read_settings_file(DEFAULT_PI_AGENT_DIR / "settings.json")
    if cwd:
        settings = _deep_merge_settings(
            settings, _read_settings_file(Path(cwd) / ".pi" / "settings.json")
        )
    level = settings.get("defaultThinkingLevel", "medium")
    model_levels = settings.get("modelThinkingLevels")
    if isinstance(model_levels, dict) and model_selection in model_levels:
        level = model_levels[model_selection]
    return (
        level
        if isinstance(level, str)
        and level in ("off", "minimal", "low", "medium", "high", "xhigh", "max")
        else "medium"
    )


def _read_settings_file(path: Path) -> _Settings:
    """Load a Pi ``settings.json`` file, returning ``{}`` when absent/invalid."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning("Ignoring unreadable Pi settings at %s: %s", path, exc)
        return {}
    return cast(_Settings, raw) if isinstance(raw, dict) else {}


def _deep_merge_settings(base: Mapping[str, object], overlay: Mapping[str, object]) -> _Settings:
    """Merge *overlay* onto *base* using Pi's nested-object merge semantics."""
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_settings(existing, value)
        else:
            merged[key] = value
    return merged


def _seed_agent_resources(
    managed_dir: Path,
    global_agent_dir: Path,
) -> None:
    """Expose only Pi's resource trees, preserving existing managed entries."""
    for name in _PI_AGENT_RESOURCE_DIRS:
        source = global_agent_dir / name
        if not source.is_dir():
            continue
        target = managed_dir / name
        if target.exists() or target.is_symlink():
            continue
        try:
            target.symlink_to(source, target_is_directory=True)
        except OSError as exc:
            _logger.warning(
                "Could not symlink Pi agent resource %s -> %s: %s",
                target,
                source,
                exc,
            )

    manifest_path = managed_dir / _PI_SEED_MANIFEST
    manifest = _read_settings_file(manifest_path)
    for name in _PI_AGENT_RESOURCE_FILES:
        source = global_agent_dir / name
        target = managed_dir / name
        try:
            previous = manifest.get(name)
            owned_hashes = previous if isinstance(previous, list) else [previous]
            current_hash = None
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_file():
                    manifest.pop(name, None)
                    continue
                current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                if current_hash not in owned_hashes:
                    manifest.pop(name, None)
                    continue
            if not source.is_file():
                if current_hash is not None:
                    target.unlink()
                manifest.pop(name, None)
                continue
            content = source.read_bytes()
            source_hash = hashlib.sha256(content).hexdigest()
            # Journal both hashes before replacement so an interrupted update is retryable.
            manifest[name] = [current_hash, source_hash]
            _atomic_write(manifest_path, json.dumps(manifest).encode())
            _atomic_write(target, content, stat.S_IMODE(source.stat().st_mode))
            manifest[name] = source_hash
        except OSError as exc:
            _logger.warning("Could not copy Pi agent resource %s -> %s: %s", source, target, exc)
    try:
        _atomic_write(manifest_path, json.dumps(manifest).encode())
    except OSError as exc:
        _logger.warning("Could not write Pi resource ownership at %s: %s", manifest_path, exc)


def _atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Replace a managed file without exposing a partial copy on failure."""
    fd, temporary = tempfile.mkstemp(prefix=".omnigent-pi-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare_managed_pi_agent_dir(
    managed_dir: Path,
    *,
    overlay: Mapping[str, object] | None = None,
    global_agent_dir: Path | None = None,
) -> None:
    """
    Seed a managed ``PI_CODING_AGENT_DIR`` with the user's global Pi settings.

    Copies settings, context, system prompts and MCP configuration, and links
    resource/install trees. Relative settings paths retain their original root.
    Unchanged seeded copies follow source edits/removals on relaunch. Managed
    resource edits and model/auth files are preserved.

    Project-scoped ``.pi/settings.json`` at the session cwd is still read by
    Pi at runtime and overrides these global settings per Pi's normal rules.

    :param managed_dir: Per-session agent root (already contains ``models.json``).
    :param overlay: Settings merged on top of the copied global file.
    :param global_agent_dir: Explicit source; otherwise ``PI_CODING_AGENT_DIR``
        or :data:`DEFAULT_PI_AGENT_DIR`.
    """
    source = (
        str(global_agent_dir)
        if global_agent_dir is not None
        else (os.environ.get("PI_CODING_AGENT_DIR") or str(DEFAULT_PI_AGENT_DIR))
    )
    agent_root = Path(os.path.abspath(_normalize_pi_path(source)))
    settings = _read_settings_file(agent_root / "settings.json")
    _relocate_resource_settings(settings, agent_root, managed_dir.absolute())
    if overlay:
        settings = _deep_merge_settings(settings, overlay)

    managed_dir.mkdir(parents=True, exist_ok=True)
    settings_path = managed_dir / "settings.json"
    try:
        settings_path.write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _logger.warning("Could not write managed Pi settings at %s: %s", settings_path, exc)
        return

    _seed_agent_resources(managed_dir, agent_root)
