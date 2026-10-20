"""Helpers for seeding a managed Pi agent dir (``PI_CODING_AGENT_DIR``).

When the pi harness runs in gateway mode it relocates Pi's agent root to a
per-session temp directory (for ``models.json``). That hides the user's global
``~/.pi/agent/settings.json`` and installed package trees. This module copies
the global settings metadata into the managed dir and symlinks install
directories so Pi's native loader still sees extensions and ``pi install``
packages — without mutating the user's home directory.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias, cast

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


# Install / checkout trees Pi places under the agent dir. Symlinked into the
# managed dir so ``packages`` entries in settings keep resolving.
_PI_AGENT_RESOURCE_DIRS: tuple[str, ...] = ("npm", "git")


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


def _symlink_agent_resource_dirs(
    managed_dir: Path,
    global_agent_dir: Path,
) -> None:
    """Symlink package install trees from *global_agent_dir* into *managed_dir*."""
    for name in _PI_AGENT_RESOURCE_DIRS:
        source = global_agent_dir / name
        if not source.is_dir():
            continue
        target = managed_dir / name
        if target.exists():
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


def prepare_managed_pi_agent_dir(
    managed_dir: Path,
    *,
    overlay: Mapping[str, object] | None = None,
    global_agent_dir: Path | None = None,
) -> None:
    """
    Seed a managed ``PI_CODING_AGENT_DIR`` with the user's global Pi settings.

    Copies (merges) ``settings.json`` from the user's global agent dir into
    *managed_dir*, applies *overlay* (e.g. Omnigent retry policy), and
    symlinks install trees (``npm/``, ``git/``) so ``packages`` entries keep
    working. The user's ``~/.pi/agent`` is never modified.

    Project-scoped ``.pi/settings.json`` at the session cwd is still read by
    Pi at runtime and overrides these global settings per Pi's normal rules.

    :param managed_dir: Per-session agent root (already contains ``models.json``).
    :param overlay: Settings merged on top of the copied global file.
    :param global_agent_dir: Override for tests; defaults to
        :data:`DEFAULT_PI_AGENT_DIR`.
    """
    agent_root = global_agent_dir if global_agent_dir is not None else DEFAULT_PI_AGENT_DIR
    settings = _read_settings_file(agent_root / "settings.json")
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

    _symlink_agent_resource_dirs(managed_dir, agent_root)
