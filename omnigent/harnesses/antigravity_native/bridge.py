"""Bridge state for native Antigravity TUI sessions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from omnigent.harnesses.antigravity_native.steps import PendingInteraction
from omnigent.native import native_bridge_common
from omnigent.permission_tiers import PERMISSION_TIERS, PermissionTier, normalize_permission_tier

if TYPE_CHECKING:
    from omnigent.inner.terminal import TerminalInstance

_logger = logging.getLogger(__name__)

ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY = "omnigent.antigravity_native.bridge_id"
ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR = "HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR"
ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR = "HARNESS_ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID"
AGY_PERMISSION_TIER_LABEL_KEY = "omnigent.antigravity_native.permission_tier"
AGY_PERMISSION_TIERS = PERMISSION_TIERS
AGY_DEFAULT_PERMISSION_TIER = "auto"

_STATE_FILE = "state.json"
# Advertises the runner-owned tmux pane (socket + target) hosting agy, so the
# executor can deliver a FIRST web turn into the idle agy TUI via send-keys (see
# the tmux/TUI section at the end of this module). Written by the runner after
# the agy terminal launches; read by the executor's first-turn bootstrap.
_TMUX_FILE = "tmux.json"
_PLAN_INPUT_RECEIPTS_DIR = "plan-input-receipts"
_FIRST_PLAN_INPUT_CASCADE = "first-cascade"
_MAX_PLAN_INPUT_RECEIPTS = 128
_BRIDGE_ROOT = Path.home() / ".omnigent" / "antigravity-native"

# Prefix of the launcher-minted placeholder conversation id (see
# ``antigravity_native._mint_agy_conversation_id``). agy mints its own real
# UUID, so the placeholder only seeds bridge state until the forwarder discovers
# and overwrites it; consumers (e.g. the executor) must treat a placeholder id as
# "not ready yet" rather than resolving an RPC against it.
AGY_PLACEHOLDER_CONVERSATION_PREFIX = "agy_conv_"


def is_placeholder_conversation_id(conversation_id: str) -> bool:
    """Return whether *conversation_id* is the launcher placeholder, not agy's real id.

    :param conversation_id: A bridge-state conversation id.
    :returns: ``True`` when it is an ``agy_conv_*`` placeholder (the forwarder has
        not yet discovered agy's real UUID); ``False`` for a real agy id.
    """
    return conversation_id.startswith(AGY_PLACEHOLDER_CONVERSATION_PREFIX)


# Canonical root of agy's per-user app-data tree (``~/.gemini/antigravity-cli``).
# Single-sourced here and imported by the forwarder (transcript / brain
# discovery) so a change to agy's data-dir layout is a one-line edit. (The
# onboarding/auth layer keeps its own ``~/.gemini`` OAuth root in ``gemini_auth``
# — a broader, distinct concern that must not depend on this harness module.)
AGY_APP_DATA_DIR = Path.home() / ".gemini" / "antigravity-cli"

# agy persists onboarding completion in this HOME-global cache file. On a first
# run where it is absent, agy launches an interactive TUI onboarding wizard
# (login method, then color-scheme / telemetry steps) that has no pre-emptive
# flag and no PermissionRequest-style hook. On a host-spawned (web-driven) or
# otherwise headless launch there is no TTY to answer it, so agy hangs and the
# web UI shows nothing. Seeding ``onboardingComplete`` here suppresses the
# wizard, mirroring how ``claude_native_bridge.ensure_claude_workspace_trusted``
# pre-accepts Claude's ``hasCompletedOnboarding`` gate. The agy OAuth token is a
# separate per-host secret (seeded outside this code path); this file carries no
# credential — only the three onboarding-state booleans.
_AGY_ONBOARDING_MARKER = AGY_APP_DATA_DIR / "cache" / "onboarding.json"
# The exact keys agy itself writes on a completed consumer (subscription)
# onboarding — captured ground-truth from a real onboarded profile. Enterprise
# onboarding is a distinct flow Omnigent does not drive, so it stays ``False``.
_AGY_ONBOARDING_COMPLETE_STATE: dict[str, object] = {
    "consumerOnboardingComplete": True,
    "enterpriseOnboardingComplete": False,
    "onboardingComplete": True,
}


def ensure_agy_onboarding_complete() -> None:
    """Pre-accept agy's first-run onboarding wizard so a headless launch never blocks.

    Idempotently seeds ``onboardingComplete`` (and the sibling consumer/enterprise
    flags) into agy's ``~/.gemini/antigravity-cli/cache/onboarding.json`` so the
    interactive TUI onboarding wizard does not stall a host-spawned or headless
    ``agy`` launch that has no TTY to answer it.

    .. deprecated:: 0.9.0
        No longer called by either launch path; slated for removal in 0.10.0.
        Both launches now scope agy to a per-session ``--gemini_dir``, where
        :func:`seed_isolated_agy_home` writes this same marker — so seeding the
        real ``~/.gemini`` copy only wrote the user's tree for a file agy never
        reads. Use :func:`seed_isolated_agy_home` instead.

    Any unrecognised keys already in the file are preserved (the three known keys
    are merged over them), and the write is skipped entirely when all three
    already hold their exact boolean values — so a returning user's agy state is
    never churned. Unlike
    ``~/.claude.json`` (which holds the Claude OAuth account block and is treated
    fail-loud), this file is a regenerable, non-secret agy cache: an existing
    file that is unreadable or not a JSON object is treated as absent and
    overwritten with the known-complete state rather than raising.

    :returns: None.
    :raises OSError: If the marker directory cannot be created or the file cannot
        be written (e.g. an unwritable home directory). Surfaced rather than
        swallowed: a missing marker means agy will hang on the wizard, so a write
        failure is a real, launch-blocking fault worth failing loudly on.
    """
    marker = _AGY_ONBOARDING_MARKER
    existing: dict[str, object] = {}
    if marker.is_file():
        try:
            loaded = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Regenerable non-secret cache: an unreadable (OSError), non-UTF-8
            # (UnicodeDecodeError) or malformed-JSON (json.JSONDecodeError) marker
            # would make agy re-run onboarding anyway, so overwrite from an empty
            # base. ValueError is the common supertype of both decode failures.
            loaded = None
        if isinstance(loaded, dict):
            existing = loaded
    # Type-strict identity (``is``, not ``==``) so a stored numeric ``1``/``0``
    # (which ``==`` True/False in Python) is NOT accepted as the boolean state but
    # is normalised on write — matching this module's ``read_bridge_state``, which
    # likewise rejects bool/int conflation.
    if all(existing.get(key) is value for key, value in _AGY_ONBOARDING_COMPLETE_STATE.items()):
        return
    merged: dict[str, object] = {**existing, **_AGY_ONBOARDING_COMPLETE_STATE}
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{marker.name}.", dir=str(marker.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, sort_keys=True)
            handle.write("\n")
            # fsync before the atomic replace so a crash/power-loss cannot leave a
            # present-but-empty marker that would send agy back into the wizard
            # (matches ``claude_native_bridge._atomic_write_user_json``).
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, marker)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    _logger.info("Seeded agy onboarding-complete marker at %s", marker)


def bridge_root() -> Path:
    """
    Return the configured Antigravity-native bridge root.

    Tests may monkeypatch :data:`_BRIDGE_ROOT` to isolate bridge files.

    :returns: Absolute root for Antigravity-native bridge directories, e.g.
        ``Path("~/.omnigent/antigravity-native")``.
    """
    return _BRIDGE_ROOT


@dataclass(frozen=True)
class AntigravityNativeBridgeState:
    """
    Runtime state shared by the native Antigravity wrapper and harness.

    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param conversation_id: agy's real conversation id, e.g.
        ``"68caaeac-2eaf-4e2c-9b95-721b022f4903"``. Discovered by the forwarder
        via by-pid ownership of this session's agy process (agy mints its own
        UUID and ignores any ``ANTIGRAVITY_CONVERSATION_ID`` the launcher sets).
        The launcher seeds this with its minted ``agy_conv_*`` placeholder, which
        the forwarder overwrites once it discovers the real id.
    :param active_turn_id: Current agy turn id, if one is running,
        e.g. ``"turn_abc123"``.

    .. note::
        There is intentionally no durable read cursor. The retired transcript
        forwarder persisted a ``forwarded_steps`` set (+ a ``forwarded_step_index``
        mirror) so a tail (re)start did not re-mirror the whole JSONL. The RPC read
        driver that superseded it (:mod:`omnigent.harnesses.antigravity_native.reader`) keeps
        an *in-memory* seen-set only and is recreated per session by the runner, so
        no on-disk cursor is needed. Legacy ``forwarded_step_index`` /
        ``forwarded_steps`` keys in an on-disk ``state.json`` are tolerated and
        ignored on read.

    .. note::
        There is intentionally no ``agy_pid`` field. agy's pid is not stable to
        capture at launch — the CLI terminal uses ``tmux_start_on_attach=True``
        (agy does not exist until the human TTY attaches), and even the
        runner-owned web terminal (``tmux_start_on_attach=False``, agy started
        immediately) re-execs/supervises, so a captured pid can go stale. The
        executor instead discovers agy's connect-RPC port at injection time by
        enumerating agy processes and validating each against
        :attr:`conversation_id` via ``GetConversationMetadata`` (see
        :func:`omnigent.harnesses.antigravity_native.rpc.resolve_language_server_port`).
        A legacy ``agy_pid`` key in an on-disk ``state.json`` is tolerated and
        ignored on read.
    """

    session_id: str
    conversation_id: str
    active_turn_id: str | None = None


def bridge_dir_for_bridge_id(bridge_id: str) -> Path:
    """
    Return the bridge directory for a native Antigravity bridge id.

    :param bridge_id: Opaque bridge id, e.g. ``"bridge_abc123"``.
    :returns: Absolute bridge directory under
        ``~/.omnigent/antigravity-native``.
    """
    digest = hashlib.sha256(bridge_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def build_antigravity_native_spawn_env(
    conversation_id: str,
    *,
    bridge_id: str | None = None,
) -> dict[str, str]:
    """
    Build spawn env for the ``antigravity-native`` harness process.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param bridge_id: Opaque bridge id from
        :data:`ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY`, e.g.
        ``"bridge_abc123"``. ``None`` uses *conversation_id*.
    :returns: Environment variables needed by the Antigravity-native
        harness executor.
    """
    resolved_bridge_id = bridge_id or conversation_id
    return {
        ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_bridge_id(resolved_bridge_id)),
        ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR: conversation_id,
    }


def prepare_bridge_dir(bridge_id: str) -> Path:
    """
    Create the bridge directory for *bridge_id*.

    :param bridge_id: Opaque bridge id, e.g. ``"bridge_abc123"``.
    :returns: Prepared absolute bridge directory.
    """
    bridge_dir = bridge_dir_for_bridge_id(bridge_id)
    with native_bridge_common.bridge_dir_preparation_lock(bridge_dir):
        bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(bridge_dir, 0o700)
        # Owner-pid marker for the periodic dead-owner prune; refreshed every
        # turn so it always names the current runner. See native_bridge_common.
        native_bridge_common.write_owner_pid_marker(bridge_dir)
    return bridge_dir


def prune_orphaned_bridge_dirs() -> int:
    """
    Remove antigravity-native bridge dirs whose owner process is provably dead
    and whose newest file is at least 70 days old.

    Delegates to the shared sweep against this harness's bridge root; the
    global maintenance calls it (via ``native_bridge_common.reap_orphaned_native_bridge_dirs``)
    at startup to reclaim dirs leaked by a prior runner that died without
    running the explicit delete path.

    :returns: The number of orphaned bridge dirs removed.
    """
    return native_bridge_common.prune_orphaned_dirs(
        bridge_root(),
        should_prune=native_bridge_common.orphan_retention_expired,
    )


# ── Omnigent MCP relay wiring (sys_* tools) ──────────────────────────────────
#
# agy is otherwise tool-isolated from Omnigent: unlike claude/codex/cursor it
# wires no MCP relay, so the wrapped agy cannot reach any ``sys_*`` tool (spawn
# sub-agent sessions, drive Omnigent terminals, list agents/models, ``sys_os_*``).
# This section wires the SAME relay cursor #742 uses — the shared
# ``omnigent.harnesses.claude_native.bridge serve-mcp`` stdio server — into agy.
#
# THE CONFIG-SCOPING FOOTGUN. agy has no ``--mcp-config`` flag and ignores every
# ``ANTIGRAVITY_*`` env knob; it loads MCP servers from a single HOME-GLOBAL file,
# ``$HOME/.gemini/config/mcp_config.json`` — the *same* file the user's
# interactive agy reads (verified empirically against agy 1.0.12). Writing that
# file directly would (a) clobber/pollute the user's interactive agy config and
# (b) be incorrect under concurrency: the relay command is bridge-dir-specific, so
# two antigravity-native sessions (or a session + the user's interactive agy)
# would fight over the one global file.
#
# CHOSEN DESIGN — per-session ISOLATED GEMINI DIR. The runner keeps agy's real
# ``HOME`` intact (needed for platform auth such as macOS keyring-backed tokens)
# and launches agy with its hidden ``--gemini_dir=<bridge_dir>/agy-home/.gemini``
# flag. That isolated Gemini dir is seeded with onboarding/migration markers and
# a bridge-scoped ``config/mcp_config.json`` written by :func:`write_mcp_config`.
# This (1) NEVER touches the user's real ``~/.gemini/config/mcp_config.json`` and
# (2) gives each session its own config so concurrent sessions never clobber one
# another. Only account and installation markers are copied; OAuth token files
# stay in the real tree, and HOME is deliberately not relocated.
#
# WHY NOT RELOCATE HOME (#1477). An earlier isolation design pointed ``HOME`` at
# the per-session tree. That broke auth on macOS: agy stores its OAuth token in
# the OS keyring (verified against agy 1.0.12 — the binary's auth path is
# ``keyring`` / "load token from keyring", NOT a ``~/.gemini`` file), and the
# keyring item is bound to the real login HOME, so any relocated HOME stalled
# every turn at the interactive OAuth prompt. The alternative of dropping HOME
# isolation entirely on macOS (#1493) restored auth but reintroduced the
# HOME-global ``mcp_config.json`` footgun (#1194) there — concurrent sessions
# would clobber one another's relay config. ``--gemini_dir`` resolves both at
# once: real HOME ⇒ keyring auth works on every platform; isolated gemini dir ⇒
# per-session MCP config. Verified live (agy 1.0.12): ``agy --gemini_dir=<dir>``
# materializes its ``antigravity-cli`` + ``config`` state under ``<dir>`` while
# authenticating from the real-HOME keyring.
_MCP_CONFIG_DIR = "config"
_MCP_CONFIG_FILE = "mcp_config.json"
_BRIDGE_CONFIG_FILE = "bridge.json"
_MCP_SERVER_NAME = "omnigent"
# agy only exposes relay tools named in this per-server allowlist, so it must name
# the whole native relay surface; a test keeps the two equal.
_AGY_ENABLED_TOOLS = [
    "ask_user_async",
    "browser_click",
    "browser_navigate",
    "browser_screenshot",
    "browser_snapshot",
    "browser_type",
    "hindsight_recall",
    "hindsight_reflect",
    "hindsight_retain",
    "list_comments",
    "load_skill",
    "open_in_panel",
    "read_skill_file",
    "sys_add_policy",
    "sys_advise_models",
    "sys_agent_download",
    "sys_agent_get",
    "sys_agent_list",
    "sys_call_async",
    "sys_cancel_async",
    "sys_cancel_task",
    "sys_child_cap",
    "sys_flow_cancel",
    "sys_flow_list",
    "sys_flow_start",
    "sys_instruction_layer_update",
    "sys_instruction_layers_read",
    "sys_list_models",
    "sys_os_edit",
    "sys_os_read",
    "sys_os_shell",
    "sys_os_write",
    "sys_policy_registry",
    "sys_read_inbox",
    "sys_scheduled_task_create",
    "sys_scheduled_task_delete",
    "sys_scheduled_task_list",
    "sys_scheduled_task_update",
    "sys_session_archive",
    "sys_session_close",
    "sys_session_create",
    "sys_session_get_history",
    "sys_session_get_info",
    "sys_session_handover",
    "sys_session_list",
    "sys_session_open",
    "sys_session_rename",
    "sys_session_send",
    "sys_session_share",
    "sys_session_unarchive",
    "sys_terminal_close",
    "sys_terminal_launch",
    "sys_terminal_list",
    "sys_terminal_read",
    "sys_terminal_send",
    "sys_timer_cancel",
    "sys_timer_set",
    "update_comment",
]

# Only account and installation markers are copied, never OAuth token files.
# macOS/Windows use the OS keyring through the real HOME; Linux file-stored tokens
# are not copied, so those sessions sign in on their own.
_AGY_SEED_FILES = (
    # Without the account identity a fresh Gemini dir can prompt for account
    # selection, which reads as "not signed in" on a headless launch (#1477).
    Path("google_accounts.json"),
    Path("installation_id"),
    Path("antigravity-cli") / "installation_id",
)


# agy resolves imported plugins (skills + hooks) relative to its Gemini dir, so a
# bridge-owned ``--gemini_dir`` starts with none of the user's plugins unless they
# are seeded. ``plugins/`` is symlinked rather than copied: the payload is a git
# checkout per plugin, and a link keeps a mid-session ``/plugin install`` or update
# visible instead of pinning a stale copy.
_AGY_PLUGINS_DIR = "plugins"
_AGY_IMPORT_MANIFEST = "import_manifest.json"


_AGY_HOOKS_FILE = "hooks.json"


# agy's non-plugin skill trees, relative to the Gemini dir — the "Global" and
# "Shared" sources its ``/skills`` panel names. They are linked for the same
# reason plugins are: :func:`~omnigent.spec.skill_sources._agy_skill_dirs`
# enumerates them from the user's REAL home, so a session that could not resolve
# them under ``--gemini_dir`` would offer a skill agy then fails to expand.
#
# The panel's other two sources need no seeding: the workspace tree
# (``<ws>/.agents/skills``) is not under the Gemini dir at all, and agy recreates
# ``antigravity-cli/builtin/skills`` in whatever Gemini dir it is launched with
# (live-verified — isolated dirs carry the same builtins as the real home).
_AGY_SKILL_DIRS = (
    Path("antigravity-cli") / "skills",
    Path("skills"),
)


# agy reads global instructions and rules from its Gemini dir, so a session under
# ``--gemini_dir`` would otherwise miss them.
_AGY_INSTRUCTION_PATHS = (
    Path("AGENTS.md"),
    Path("GEMINI.md"),
    Path("config") / "rules",
    Path("antigravity-cli") / "rules",
)


def agy_home_dir(bridge_dir: Path) -> Path:
    """Return the parent directory for this session's isolated agy state.

    The actual agy config/state root is :func:`agy_gemini_dir`, passed to agy via
    ``--gemini_dir``. Keeping this parent below *bridge_dir* makes it naturally
    per-session and easy to tear down with the bridge, while preserving the real
    ``HOME`` for auth providers that depend on platform state such as macOS
    Keychain.

    :param bridge_dir: Native Antigravity bridge directory.
    :returns: Absolute parent path for this session's isolated agy state.
    """
    return bridge_dir / "agy-home"


def agy_gemini_dir(bridge_dir: Path) -> Path:
    """Return the per-session ``--gemini_dir`` path for an agy launch.

    :param bridge_dir: Native Antigravity bridge directory.
    :returns: Absolute ``.gemini`` directory that should receive agy's session
        config/state and the Omnigent MCP config.
    """
    return agy_home_dir(bridge_dir) / ".gemini"


def resolve_agy_permission_tier(
    labels: Mapping[str, object] | None, launch_args: Sequence[str]
) -> PermissionTier:
    """Normalize a server label; only an absent legacy label can use bypass args."""
    tier = labels.get(AGY_PERMISSION_TIER_LABEL_KEY) if labels else None
    if labels is not None and AGY_PERMISSION_TIER_LABEL_KEY in labels:
        return normalize_permission_tier(tier, harness="antigravity-native", legacy=True)
    return "full" if "--dangerously-skip-permissions" in launch_args else "ask"


def agy_tier_launch_args(tier: str) -> list[str]:
    """Return agy's launch flags for a permission tier."""
    if tier not in AGY_PERMISSION_TIERS:
        raise ValueError(f"Unknown agy permission tier: {tier!r}")
    if tier == "full":
        return ["--dangerously-skip-permissions"]
    if tier == "plan":
        return ["--mode", "plan"]
    if tier in ("accept-edits", "auto"):
        return ["--mode", "accept-edits"]
    return []


def _read_agy_permission_json(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError(f"Refusing symlink permission file: {path}")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object in permission file: {path}")
    return loaded


def _write_agy_permission_file(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing symlink permission file: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise ValueError(f"Refusing symlink permission file: {path}")
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def reassert_agy_permission_tier(bridge_dir: Path, tier: str) -> None:
    """Repair a drifted tier file from the confirmed tier."""
    if tier not in AGY_PERMISSION_TIERS:
        raise ValueError(f"Unknown agy permission tier: {tier!r}")
    path = bridge_dir / "permission_tier"
    if path.is_symlink():
        raise ValueError(f"Refusing symlink permission file: {path}")
    desired = tier + "\n"
    try:
        if path.read_text(encoding="utf-8") == desired:
            return
    except (FileNotFoundError, UnicodeDecodeError):
        pass
    _write_agy_permission_file(path, desired)


def apply_agy_permission_tier(bridge_dir: Path, tier: str) -> None:
    """Merge owned settings and the guard hook, then persist the tier."""
    if tier not in AGY_PERMISSION_TIERS:
        raise ValueError(f"Unknown agy permission tier: {tier!r}")
    gemini = agy_gemini_dir(bridge_dir)
    settings_path = gemini / "antigravity-cli" / "settings.json"
    hooks_path = gemini / "config" / "hooks.json"
    snapshot_path = bridge_dir / "agy_user_settings.json"
    settings = _read_agy_permission_json(settings_path)
    hooks = _read_agy_permission_json(hooks_path)
    snapshot = _read_agy_permission_json(snapshot_path)
    if (bridge_dir / "permission_tier").is_symlink():
        raise ValueError("Refusing symlink permission_tier")
    if not snapshot_path.exists():
        snapshot = {
            "allowNonWorkspaceAccess": {
                "present": "allowNonWorkspaceAccess" in settings,
                "value": settings.get("allowNonWorkspaceAccess"),
            }
        }
        _write_agy_permission_file(snapshot_path, json.dumps(snapshot, indent=2) + "\n")
    original = snapshot.get("allowNonWorkspaceAccess")
    if not isinstance(original, dict) or not isinstance(original.get("present"), bool):
        raise ValueError(f"Invalid user settings snapshot: {snapshot_path}")
    auto = tier == "auto"
    if tier == "full":
        settings.pop("toolPermission", None)
    else:
        settings["toolPermission"] = "always-proceed" if auto else "request-review"
    if auto:
        settings["allowNonWorkspaceAccess"] = True
    elif original["present"]:
        settings["allowNonWorkspaceAccess"] = original["value"]
    else:
        settings.pop("allowNonWorkspaceAccess", None)
    settings.pop("agentMode", None)
    permissions = settings.get("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError(f"Expected permissions object in {settings_path}")
    allow = permissions.get("allow", [])
    if not isinstance(allow, list):
        raise ValueError(f"Expected permissions.allow list in {settings_path}")
    if "mcp(omnigent/*)" not in allow:
        allow.append("mcp(omnigent/*)")
    permissions["allow"] = allow
    settings["permissions"] = permissions
    hooks["omnigent-guard"] = _agy_guard_hook(bridge_dir)
    _write_agy_permission_file(
        settings_path, json.dumps(settings, indent=2, sort_keys=True) + "\n"
    )
    _write_agy_permission_file(hooks_path, json.dumps(hooks, indent=2, sort_keys=True) + "\n")
    reassert_agy_permission_tier(bridge_dir, tier)


def _agy_guard_hook(bridge_dir: Path) -> dict[str, object]:
    command = shlex.join(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("guard.py").resolve()),
            "--tier-file",
            str((bridge_dir / "permission_tier").absolute()),
        ]
    )
    return {
        "enabled": True,
        "PreToolUse": [
            {
                "matcher": ".*",
                "hooks": [{"type": "command", "command": command, "timeout": 86400}],
            }
        ],
        "PostToolUse": [
            {
                "matcher": ".*",
                "hooks": [{"type": "command", "command": command + " --finish", "timeout": 10}],
            }
        ],
    }


def build_mcp_config(
    bridge_dir: Path,
    *,
    python_executable: str | None = None,
) -> dict[str, object]:
    """Build agy's ``mcp_config.json`` payload for the Omnigent relay server.

    Mirrors cursor #742's :func:`omnigent.harnesses.cursor_native.bridge.build_mcp_config`
    but emits agy's per-server MCP schema (lowercase ``command`` / ``args`` /
    ``env`` keys plus ``enabledTools``, agy's tool-exposure allowlist — verified
    against agy 1.0.12's config struct) under the top-level ``mcpServers`` key.
    The server command is the SAME shared relay claude/codex/cursor use:
    ``<python> -I -m omnigent.harnesses.claude_native.bridge serve-mcp --bridge-dir <dir>``.

    **HOME pinning.** agy spawns this relay as a child. The relay validates its
    ``--bridge-dir`` against ``bridge_root()`` (``$HOME/.omnigent/antigravity-native``),
    which must resolve to the RUNNER's real home where the bridge dir actually
    lives. Pinning ``HOME`` in the relay env keeps that invariant true even when a
    future agy launch path customizes process environment. ``-I`` does not clear
    ``HOME``; it only ignores ``PYTHON*`` vars and user site-packages.

    :param bridge_dir: Native Antigravity bridge directory whose relay this
        config points at.
    :param python_executable: Python interpreter for the relay command;
        defaults to the current interpreter (``sys.executable``).
    :returns: The ``mcp_config.json`` payload as a dict.
    """
    python = python_executable or sys.executable
    return {
        "mcpServers": {
            _MCP_SERVER_NAME: {
                "command": python,
                "args": [
                    "-I",
                    "-m",
                    "omnigent.harnesses.claude_native.bridge",
                    "serve-mcp",
                    "--bridge-dir",
                    str(bridge_dir),
                ],
                "enabledTools": list(_AGY_ENABLED_TOOLS),
                "env": {
                    "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
                    # Pin the relay to the RUNNER's real home so its bridge-root
                    # validation matches where the bridge dir lives. See docstring.
                    "HOME": str(Path.home()),
                },
            }
        }
    }


def write_mcp_bridge_config(bridge_dir: Path) -> None:
    """Write the token config the shared Omnigent MCP relay requires.

    The relay's ``serve-mcp`` reads ``<bridge_dir>/bridge.json`` for a token that
    authorizes its localhost control endpoint. Idempotent: an existing token is
    left untouched so a resume/clear does not rotate a live relay's token.
    Mirrors :func:`omnigent.harnesses.cursor_native.bridge.write_mcp_bridge_config`.

    :param bridge_dir: Native Antigravity bridge directory.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = bridge_dir / _BRIDGE_CONFIG_FILE
    if config_path.exists():
        return
    payload = {"token": secrets.token_urlsafe(32)}
    tmp = bridge_dir / (_BRIDGE_CONFIG_FILE + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, config_path)


def write_mcp_config(
    bridge_dir: Path,
    *,
    python_executable: str | None = None,
) -> Path:
    """Write the per-session agy MCP config + relay token.

    Writes (1) ``bridge.json`` (the relay token) into *bridge_dir* and (2) the
    ``mcp_config.json`` for the Omnigent relay into the per-session isolated agy
    Gemini dir at ``<bridge_dir>/agy-home/.gemini/config/mcp_config.json`` — the
    path agy loads when launched with ``--gemini_dir``. Because the Gemini dir is
    per-session and not the user's real ``~/.gemini``, this never clobbers the
    user's interactive agy config and two concurrent sessions never share one
    config. Mirrors cursor #742's :func:`omnigent.harnesses.cursor_native.bridge.write_mcp_config`,
    adapted to agy's hidden ``--gemini_dir`` flag.

    :param bridge_dir: Native Antigravity bridge directory (holds ``bridge.json``
        and the isolated agy Gemini dir).
    :param python_executable: Python interpreter for the relay command;
        defaults to the current interpreter.
    :returns: Absolute path to the written ``mcp_config.json``.
    """
    write_mcp_bridge_config(bridge_dir)
    config_dir = agy_gemini_dir(bridge_dir) / _MCP_CONFIG_DIR
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = config_dir / _MCP_CONFIG_FILE
    payload = build_mcp_config(bridge_dir, python_executable=python_executable)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def seed_isolated_agy_home(
    bridge_dir: Path,
    *,
    trusted_workspace: Path | str | None = None,
    startup_instructions: str | None = None,
    permission_tier: str | None = None,
) -> dict[str, str]:
    """Seed the per-session isolated agy Gemini dir and return env overrides.

    Copies account and installation markers, the user's ``settings.json``
    (backend config such as the GCP project/location block), the user's global
    lifecycle hooks (``config/hooks.json``), and onboarding/migration state
    (NEVER moving or modifying the real files) into
    ``<bridge_dir>/agy-home/.gemini``. Links the user's global instruction files
    and rule directories, skill trees, and imported plugins into that directory.
    OAuth token files are never copied.
    The runner keeps agy's real ``HOME`` intact and passes this directory through
    ``--gemini_dir``; on macOS that is required because agy uses keyring-backed
    auth that is not portable to a relocated ``HOME``. The relay's
    ``mcp_config.json`` is written separately by :func:`write_mcp_config` so it
    lands in this same isolated tree rather than the user's real ``~/.gemini``
    (the footgun this whole design avoids).

    :param bridge_dir: Native Antigravity bridge directory.
    :param trusted_workspace: Optional workspace path to pre-trust inside the
        isolated agy settings file. This suppresses agy's unhookable first-run
        "Do you trust this project?" TUI gate for host-spawned sessions, mirroring
        ``ensure_claude_workspace_trusted`` while keeping trust scoped to this
        bridge-owned ``--gemini_dir``.
    :param startup_instructions: Session instructions appended to a private
        AGENTS.md copy, replacing any link without modifying its target.
    :returns: Env overrides to layer onto the agy launch environment. Currently
        empty because ``HOME`` must stay real for platform auth.
    """
    from omnigent.harnesses.antigravity_native.hook_sources import check_agy_hook_sources

    if permission_tier is not None:
        if trusted_workspace is None:
            raise ValueError("Antigravity permission startup requires an explicit workspace")
        check_agy_hook_sources(Path(trusted_workspace), permission_tier, bridge_dir)
    real_home = Path.home()
    iso_gemini = agy_gemini_dir(bridge_dir)
    (iso_gemini / "antigravity-cli" / "cache").mkdir(mode=0o700, parents=True, exist_ok=True)
    (iso_gemini / _MCP_CONFIG_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)

    # Missing account/installation markers are harmless; agy regenerates them.
    for rel in _AGY_SEED_FILES:
        src = real_home / ".gemini" / rel
        if not src.is_file():
            continue
        dst = iso_gemini / rel
        dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            dst.write_bytes(src.read_bytes())
            os.chmod(dst, 0o600)

    _seed_isolated_agy_settings(real_home, iso_gemini)
    _seed_isolated_agy_onboarding_marker(real_home, iso_gemini)

    # Seed the migration marker so agy does not churn a from-scratch migration on
    # first launch under the fresh Gemini dir (cosmetic; agy creates it itself otherwise).
    with contextlib.suppress(OSError):
        (iso_gemini / _MCP_CONFIG_DIR / ".migrated").touch()

    _seed_isolated_agy_plugins(real_home, iso_gemini)
    _seed_isolated_agy_skills(real_home, iso_gemini)
    for rel in _AGY_INSTRUCTION_PATHS:
        if rel == Path("AGENTS.md") and startup_instructions:
            source = real_home / ".gemini" / rel
            authored = source.read_text(encoding="utf-8") if source.is_file() else ""
            text = f"{authored}\n\n{startup_instructions}" if authored else startup_instructions
            fd, tmp_name = tempfile.mkstemp(prefix="AGENTS.md.", dir=iso_gemini)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(text)
                # Replace the directory entry; never follow the user's symlink.
                os.replace(tmp_name, iso_gemini / rel)
            finally:
                Path(tmp_name).unlink(missing_ok=True)
            continue
        if rel == Path("AGENTS.md"):
            (iso_gemini / rel).unlink(missing_ok=True)
        _link_into_isolated_gemini_dir(real_home / ".gemini" / rel, iso_gemini / rel)
    _seed_isolated_agy_hooks(real_home, iso_gemini)

    if trusted_workspace is not None:
        _seed_isolated_agy_workspace_trust(iso_gemini, Path(trusted_workspace))

    if permission_tier is not None:
        assert trusted_workspace is not None
        check_agy_hook_sources(Path(trusted_workspace), permission_tier, bridge_dir)

    return {}


def _seed_isolated_agy_settings(real_home: Path, iso_gemini: Path) -> None:
    """Seed the user's real ``settings.json`` as the base of the isolated one.

    The real settings carry backend config agy needs at turn time — notably the
    ``gcp`` block (project + location) for GCP/enterprise logins, without which
    every turn fails server-side with ``invalid location: ""``. Copied only when
    the isolated file is absent so a re-seed never clobbers per-session edits
    (the trust / survey seeders then merge their keys on top). Best-effort: a
    failed copy only means agy runs without the user's settings.

    :param real_home: The user's real home directory.
    :param iso_gemini: The bridge-owned ``--gemini_dir`` being seeded.
    """
    real_settings = real_home / ".gemini" / "antigravity-cli" / "settings.json"
    iso_settings = iso_gemini / "antigravity-cli" / "settings.json"
    if real_settings.is_file() and not iso_settings.exists():
        with contextlib.suppress(OSError):
            iso_settings.write_bytes(real_settings.read_bytes())
            os.chmod(iso_settings, 0o600)


def _seed_isolated_agy_onboarding_marker(real_home: Path, iso_gemini: Path) -> None:
    """Seed the onboarding-complete marker so the first-run wizard never blocks.

    The user's real marker is preferred: it carries auth-flow state the
    synthetic default lacks (``enterpriseOnboardingComplete``,
    ``previousAuthMethod``), without which an enterprise/GCP login re-enters
    the first-run wizard and blocks TUI injection. The marker is intentionally
    re-written on every seed (unlike the guarded settings copy): it is a
    regenerable, non-secret wizard gate that agy does not meaningfully edit
    per-session, so the freshest real-home state always wins.

    The synthetic fallback (no real marker, e.g. a cleared agy cache) marks
    BOTH onboarding flows complete: the marker's only job here is suppressing
    an unhookable wizard on a headless launch, and ``enterpriseOnboardingComplete:
    false`` verifiably re-triggers the wizard for enterprise/GCP accounts.

    :param real_home: The user's real home directory.
    :param iso_gemini: The bridge-owned ``--gemini_dir`` being seeded.
    """
    onboarding = iso_gemini / "antigravity-cli" / "cache" / "onboarding.json"
    real_onboarding = real_home / ".gemini" / "antigravity-cli" / "cache" / "onboarding.json"
    with contextlib.suppress(OSError):
        if real_onboarding.is_file():
            onboarding.write_bytes(real_onboarding.read_bytes())
        else:
            onboarding.write_text(
                json.dumps(
                    {
                        "consumerOnboardingComplete": True,
                        "enterpriseOnboardingComplete": True,
                        "onboardingComplete": True,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )


def _seed_isolated_agy_plugins(real_home: Path, iso_gemini: Path) -> None:
    """Expose the user's imported agy plugins under the isolated Gemini dir.

    Links ``config/plugins`` to the real tree and copies ``import_manifest.json``
    (agy needs both: the manifest declares which plugins are imported, the
    directory holds their skills and hooks). Without this an omnigent-spawned
    session lists zero plugin skills while a direct ``agy`` run lists them all.

    Best-effort: a user with no plugins, or a platform that refuses symlinks,
    simply gets a session without them rather than a failed launch.

    :param real_home: The user's real home directory.
    :param iso_gemini: The bridge-owned ``--gemini_dir`` being seeded.
    """
    real_config = real_home / ".gemini" / _MCP_CONFIG_DIR
    iso_config = iso_gemini / _MCP_CONFIG_DIR

    _link_into_isolated_gemini_dir(real_config / _AGY_PLUGINS_DIR, iso_config / _AGY_PLUGINS_DIR)

    real_manifest = real_config / _AGY_IMPORT_MANIFEST
    if real_manifest.is_file():
        with contextlib.suppress(OSError):
            (iso_config / _AGY_IMPORT_MANIFEST).write_bytes(real_manifest.read_bytes())


def _seed_isolated_agy_skills(real_home: Path, iso_gemini: Path) -> None:
    """Expose the user's Global and Shared agy skills under the isolated Gemini dir.

    Links the trees in :data:`_AGY_SKILL_DIRS` back to the real home so the
    ``/skills`` menu — which enumerates them from that same real home — cannot
    offer a skill this session's agy would fail to expand.

    Best-effort, like the plugin seed: a user with neither tree, or a platform
    that refuses symlinks, gets a session without them rather than a failed launch.

    :param real_home: The user's real home directory.
    :param iso_gemini: The bridge-owned ``--gemini_dir`` being seeded.
    """
    for rel in _AGY_SKILL_DIRS:
        _link_into_isolated_gemini_dir(real_home / ".gemini" / rel, iso_gemini / rel)


def _seed_isolated_agy_hooks(real_home: Path, iso_gemini: Path) -> None:
    """Refresh session hooks while preserving each command's original working directory."""
    real_hooks = real_home / ".gemini" / _MCP_CONFIG_DIR / _AGY_HOOKS_FILE
    iso_hooks = iso_gemini / _MCP_CONFIG_DIR / _AGY_HOOKS_FILE

    def preserve_cwd(value: dict[str, object]) -> dict[str, object]:
        command = value.get("command")
        if isinstance(command, str) and command and value.get("type", "command") == "command":
            # agy runs hook commands from the directory containing hooks.json.
            value["command"] = (
                f"cd {shlex.quote(str(real_hooks.parent))} && exec sh -c {shlex.quote(command)}"
            )
        return value

    try:
        try:
            content = real_hooks.read_text(encoding="utf-8")
        except FileNotFoundError:
            iso_hooks.unlink(missing_ok=True)
            return
        hooks = json.loads(content, object_hook=preserve_cwd)
        iso_hooks.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="hooks.json.", dir=iso_hooks.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(hooks, stream)
                stream.write("\n")
            os.replace(tmp_name, iso_hooks)
        finally:
            Path(tmp_name).unlink(missing_ok=True)
    except (OSError, ValueError):
        _logger.warning("Could not refresh Antigravity hooks at %s", iso_hooks, exc_info=True)


def _link_into_isolated_gemini_dir(real: Path, link: Path) -> None:
    """Symlink *link* in the isolated Gemini dir to a real file or directory *real*.

    Linking rather than copying keeps a tree the user edits mid-session (a
    ``/plugin install``, a newly authored skill) visible instead of pinning a
    stale snapshot. A missing source seeds nothing.

    :param real: Source file or directory in the user's real ``~/.gemini``.
    :param link: Path to create under the bridge-owned ``--gemini_dir``.
    :returns: None.
    """
    if not (real.is_dir() or real.is_file()):
        return
    with contextlib.suppress(OSError):
        # Re-seeding an existing bridge dir must not fail on the prior link;
        # drop a stale or broken one so the target is always current.
        if link.is_symlink() or link.is_file():
            link.unlink()
        if not link.exists():
            link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            link.symlink_to(real.resolve(), target_is_directory=real.is_dir())


# agy's periodic engagement survey ("How's the CLI experience so far?") is gated by
# this ``settings.json`` key. Its modal footer line ``esc to cancel`` is identical
# to :data:`_AGY_ACTIVE_MARKER` (the running-turn signal the TUI inject path keys
# on), so a web turn typed into the pane while the survey is up is misread as a
# mid-turn steer and silently lost (#1494). Disabling the survey removes the
# trigger. Verified live: toggling agy's ``/config`` "Show Feedback Survey" off
# writes exactly ``"showFeedbackSurvey": false`` here (``disableFeedback`` is an
# unrelated internal proto field, NOT a settings key — it would be ignored).
_AGY_FEEDBACK_SURVEY_SETTING = "showFeedbackSurvey"
_AGY_MODEL_PROVIDER_SETTING = "modelProvider"


def ensure_agy_feedback_survey_disabled(home: Path) -> None:
    """
    Disable agy's feedback survey in the launch HOME's ``settings.json``.

    agy periodically renders an engagement survey whose modal footer
    (``esc to cancel``) collides with :data:`_AGY_ACTIVE_MARKER`; with it up, a
    web turn injected into the TUI is mistaken for a running turn and lost
    (#1494). The survey is governed by ``showFeedbackSurvey`` in
    ``<home>/.gemini/antigravity-cli/settings.json`` — set it ``false`` before
    launch so the survey never appears (deterministic prevention; text-matching
    the survey itself would be brittle to agy wording changes).

    Merge-only + idempotent: existing keys (``model``, ``trustedWorkspaces``,
    ``enableTelemetry``, …) are preserved. When ``GEMINI_API_KEY`` is present,
    ``modelProvider`` is set to ``"gemini"``; when the key disappears that
    bridge-owned value is removed so OAuth can take over on resume. A malformed
    / non-object file is left UNTOUCHED rather than clobbered. Best-effort — a
    read/write failure is logged and the launch proceeds.

    :param home: The HOME agy launches under (the per-session isolated home, or
        the real home when the harness runs agy under it). Settings live at
        ``<home>/.gemini/antigravity-cli/settings.json``.
    :returns: None.
    """
    # resolve() follows a symlinked settings.json (e.g. a dotfiles-managed file) to
    # its real target, so the atomic os.replace below rewrites the linked file rather
    # than silently replacing the symlink with a regular one. strict=False is a no-op
    # for a normal or not-yet-existing path (it only resolves parent symlinks).
    settings_path = (home / ".gemini" / "antigravity-cli" / "settings.json").resolve(strict=False)
    data: dict[str, object] = {}
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = ""  # no settings yet -> create a fresh one carrying just this key
    except OSError:
        # An EXISTING but unreadable file (perms, stale NFS, …) must NOT be
        # recreated from scratch — that would drop the user's real settings.
        _logger.warning(
            "could not read agy settings.json at %s; leaving it untouched",
            settings_path,
            exc_info=True,
        )
        return
    except UnicodeDecodeError:
        # read_text raises this (a ValueError, not an OSError) on non-UTF-8 bytes;
        # treat it like malformed JSON and never clobber the file (mirrors the
        # ``(OSError, ValueError)`` read guard in ensure_agy_onboarding_complete).
        _logger.warning(
            "agy settings.json at %s is not valid UTF-8; leaving it untouched",
            settings_path,
        )
        return
    if raw.strip():
        try:
            loaded = json.loads(raw)
        except ValueError:
            # json.JSONDecodeError subclasses ValueError; catch the supertype to
            # match the decode-family guard in ensure_agy_onboarding_complete.
            _logger.warning(
                "agy settings.json at %s is not valid JSON; leaving it untouched "
                "(the feedback survey may still appear)",
                settings_path,
            )
            return
        if not isinstance(loaded, dict):
            _logger.warning(
                "agy settings.json at %s is not a JSON object; leaving it untouched",
                settings_path,
            )
            return
        data = loaded
    desired: dict[str, object] = {_AGY_FEEDBACK_SURVEY_SETTING: False}
    if (os.environ.get("GEMINI_API_KEY") or "").strip():
        desired[_AGY_MODEL_PROVIDER_SETTING] = "gemini"
    provider_removed = False
    current_provider = data.get(_AGY_MODEL_PROVIDER_SETTING)
    if _AGY_MODEL_PROVIDER_SETTING not in desired and current_provider == "gemini":
        del data[_AGY_MODEL_PROVIDER_SETTING]
        provider_removed = True
    if not provider_removed and all(data.get(key) == value for key, value in desired.items()):
        return  # already disabled — avoid a needless rewrite
    data.update(desired)
    # The write is atomic (mkstemp + os.replace) so a concurrent reader/writer never
    # sees a torn file. Both launch paths pass the per-session isolated agy dir, so
    # the only writer that can race here is the session's own agy; the idempotent
    # short-circuit above makes every launch after the first disable read-only, and
    # a clobbered value fails safe (lost trust is re-prompted, lost model defaults).
    # A cross-process lock is intentionally not taken (agy would not honor it).
    try:
        settings_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="settings.json.", dir=str(settings_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())  # crash-safe, mirrors ensure_agy_onboarding_complete
            os.replace(tmp_name, settings_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    except OSError:
        _logger.warning(
            "could not write agy settings.json at %s to disable the feedback survey",
            settings_path,
            exc_info=True,
        )


def _seed_isolated_agy_workspace_trust(iso_gemini: Path, workspace: Path) -> None:
    """Add *workspace* to isolated agy ``trustedWorkspaces`` settings."""
    settings_path = iso_gemini / "antigravity-cli" / "settings.json"
    data: dict[str, object] = {}
    if settings_path.is_file():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
    workspace_key = str(workspace.resolve())
    existing = data.get("trustedWorkspaces")
    trusted = list(existing) if isinstance(existing, list) else []
    if workspace_key in trusted:
        return
    trusted.append(workspace_key)
    data["trustedWorkspaces"] = trusted
    settings_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, settings_path)


def write_bridge_state(bridge_dir: Path, state: AntigravityNativeBridgeState) -> None:
    """
    Persist shared native Antigravity state atomically.

    :param bridge_dir: Native Antigravity bridge directory.
    :param state: State payload to persist.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = bridge_dir / _STATE_FILE
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_STATE_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "session_id": state.session_id,
                    "conversation_id": state.conversation_id,
                    "active_turn_id": state.active_turn_id,
                },
                handle,
                sort_keys=True,
            )
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def clear_bridge_state(bridge_dir: Path) -> None:
    """
    Remove stale native Antigravity runtime state for a bridge directory.

    New agy launches reuse the same bridge directory for a conversation id,
    but the old ``state.json`` still names the *previous* agy run's discovered
    conversation id. Clear it before the new launch so the forwarder
    rediscovers the new run's real conversation id instead of binding to the
    stale one. The advertised tmux pane (``tmux.json``) is cleared for the same
    reason: a new launch opens a new pane (and, after a host restart, a new
    socket), so the executor must not bootstrap the first turn against the prior
    run's pane. The new launch re-advertises its pane via
    :func:`write_tmux_target`.

    :param bridge_dir: Native Antigravity bridge directory.
    :returns: None.
    """
    receipts_dir = bridge_dir / _PLAN_INPUT_RECEIPTS_DIR
    state = read_bridge_state(bridge_dir)
    first_cascade = _first_plan_input_cascade(bridge_dir)
    cascade_id = first_cascade or (
        state.conversation_id
        if state is not None and not is_placeholder_conversation_id(state.conversation_id)
        else None
    )
    for receipt in _plan_input_receipts(bridge_dir):
        record = _read_plan_input_receipt(receipt)
        if record is None or record[2] is not None:
            continue
        if cascade_id is None:
            receipt.unlink(missing_ok=True)
        else:
            _write_agy_permission_file(
                receipt,
                json.dumps({"digest": record[0], "plan": record[1], "cascade": cascade_id}) + "\n",
            )
    if receipts_dir.is_dir() and not receipts_dir.is_symlink():
        (receipts_dir / _FIRST_PLAN_INPUT_CASCADE).unlink(missing_ok=True)
    for runtime_file in (_STATE_FILE, _TMUX_FILE):
        with contextlib.suppress(FileNotFoundError):
            (bridge_dir / runtime_file).unlink()


def read_bridge_state(bridge_dir: Path) -> AntigravityNativeBridgeState | None:
    """
    Read shared native Antigravity bridge state.

    Legacy ``agy_pid`` / ``forwarded_step_index`` / ``forwarded_steps`` keys
    written by an older build are tolerated and ignored: ``agy_pid`` because agy
    does not exist at launch (the executor always discovers the connect-RPC port
    at injection time), and the two cursor keys because the durable read cursor
    was retired with the transcript forwarder (the RPC reader keeps an in-memory
    seen-set only).

    :param bridge_dir: Native Antigravity bridge directory.
    :returns: Parsed state, or ``None`` when no state exists or the file
        is missing, corrupt, or has invalid field types.
    """
    path = bridge_dir / _STATE_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    session_id = raw.get("session_id")
    conversation_id = raw.get("conversation_id")
    active_turn_id = raw.get("active_turn_id")
    # Validate required string fields are non-empty strings.
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    parsed_active_turn_id = (
        active_turn_id if isinstance(active_turn_id, str) and active_turn_id else None
    )
    return AntigravityNativeBridgeState(
        session_id=session_id,
        conversation_id=conversation_id,
        active_turn_id=parsed_active_turn_id,
    )


def update_conversation_id(
    bridge_dir: Path,
    conversation_id: str,
    active_turn_id: str | None = None,
) -> bool:
    """
    Update the agy conversation id in bridge state.

    Used when a native agy action creates a fresh conversation while the
    Omnigent session stays the same (e.g. the runner cold-start replacing the
    ``agy_conv_*`` placeholder with agy's real cascade id).

    When there is no existing state to update (a missing or invalid state file),
    the write is SKIPPED and a WARNING is logged naming the dropped id — silently
    dropping it would leave the reader bound to the ``agy_conv_*`` placeholder
    forever, surfaced only as a generic "conversation not ready". The caller
    decides how to react (it stays best-effort; this never raises).

    :param bridge_dir: Native Antigravity bridge directory.
    :param conversation_id: New agy conversation id, e.g.
        ``"agy_conv_abc123"``.
    :param active_turn_id: Active turn id for the new conversation, e.g.
        ``"turn_abc123"``, or ``None`` when no turn is running yet.
    :returns: ``True`` when the new id was written, ``False`` when there was no
        existing state to update (the id was dropped; a WARNING was logged).
    """
    state = read_bridge_state(bridge_dir)
    if state is None:
        _logger.warning(
            "Antigravity bridge: no existing state at %s to update; dropping "
            "conversation_id=%s (the reader will stay on the placeholder id)",
            bridge_dir,
            conversation_id,
        )
        return False
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id=state.session_id,
            conversation_id=conversation_id,
            active_turn_id=active_turn_id,
        ),
    )
    return True


# ── Web-turn TUI delivery (tmux send-keys) ───────────────────────────────────
#
# The executor types EVERY web/mobile turn into the agy TUI over the runner-owned
# tmux pane — never agy's connect-RPC ``SendAgentMessage``, which agy records as a
# ``SYSTEM_MESSAGE`` the transcript forwarder would not mirror (so the user's
# message would never be committed). Typing creates a real ``USER_INPUT`` step
# that the forwarder mirrors in order (validated against agy 1.0.10: a
# paste-buffer + Enter is recorded as ``USER_INPUT`` whether agy is idle or
# mid-turn, and on a fresh session it also creates the conversation + brain dir
# the forwarder then discovers). This mirrors the cursor/claude native send-keys
# delivery; no shared tmux helper exists, so the small primitives are duplicated
# per the established per-harness convention.

# Per-command tmux budget. These are local IPC calls to the runner's own tmux
# server, but that server can stall past 5s under parallel worker boots on a
# large worktree while still healthy — 10s matches every other native bridge.
_TMUX_SEND_TIMEOUT_S = 10.0
# Per-readiness-gate wait (tmux.json advertised, then the agy input box mounted).
_TMUX_READY_TIMEOUT_S = 30.0
# Poll cadence for the readiness / paste-commit / submit-verify loops.
_TMUX_POLL_INTERVAL_S = 0.2
# Five intervals let an RPC-driven prompt close before a selection is sent.
_INTERACTION_DISMISSAL_POLLS = 6
# How long to wait for the pasted draft to render in the input box before Enter.
_PASTE_COMMIT_TIMEOUT_S = 5.0
# Settle pause after the draft renders, before the submit Enter.
_PASTE_SETTLE_S = 0.2
_IDLE_CLEAR_BATCH_SIZE = 300
_IDLE_CLEAR_MAX_BATCHES = 4
_IDLE_CLEAR_TIMEOUT_S = 5.0
_BASELINE_CAPTURE_TIMEOUT_S = 5.0
# How long one submit Enter is given to start a turn before it is re-sent.
_SUBMIT_VERIFY_TIMEOUT_S = 5.0
# Re-send budget for a submit Enter that the TUI folded into the paste burst.
_MAX_SUBMIT_ATTEMPTS = 3
# Named tmux buffer used to stream the paste (avoids the ~16KB send-keys argv cap).
_PASTE_BUFFER = "omnigent-agy-paste"
# agy TUI footer when idle (input box mounted, ready for a turn).
_AGY_IDLE_MARKER = "? for shortcuts"
# agy TUI footer while a turn is running. Used as a readiness hint and to detect
# a mid-turn steer; submission itself is verified by draft disappearance, NOT by
# this marker (footer text can change across agy builds or be truncated).
_AGY_ACTIVE_MARKER = "esc to cancel"
_AGY_MODE_HINT_RE = re.compile(r"^[A-Z][a-z]*(?:-[a-z]+)* mode: .+ \(shift\+tab to cycle\)$")
# agy collapses a paste carrying many line breaks into a single placeholder row
# (``[Pasted text #1 +17 lines]``) instead of echoing the text into the composer.
# The threshold is line-count based — ~13+ line breaks; total length does not
# matter, so a long single-line message still renders verbatim (measured against
# agy 1.1.8). The pasted text is therefore NEVER visible for a multi-line prompt,
# which is exactly the shape a sub-agent task prompt has. Treated as draft content
# so both the render gate and the submit verification key off the placeholder
# appearing and then leaving the composer.
_AGY_PASTE_PLACEHOLDER_RE = re.compile(r"\[Pasted text #\d+[^\]]*\]")
_AGY_COLLAPSED_LINES_RE = re.compile(r"↑ \d+ more lines?")
# agy's notice when a turn is submitted before its startup account-eligibility
# check settles. The composer footer mounts ~3s after launch but eligibility is
# not settled for ~7-9s, and a turn landing in that window is CONSUMED (the draft
# leaves the composer, so the submit verifies) and answered with this notice
# instead of a cascade. A programmatic first turn — a sub-agent dispatch — lands
# there every time, so the turn must be re-delivered or it is silently lost.
# Matched case-insensitively against the pane.
_AGY_VERIFYING_MARKER = "verifying your account eligibility"
# How long after a submit to watch for a running turn before reading the pane for
# the verification notice. Both signals render effectively immediately (measured
# against agy 1.1.8), so this only bounds the fail-open path.
_VERIFY_PROBE_TIMEOUT_S = 2.0
# Total budget for re-delivering a turn agy rejected while verifying the account.
_VERIFY_RETRY_TIMEOUT_S = 90.0
# Pause between re-delivery attempts. agy asks the user to "try again shortly".
_VERIFY_RETRY_INTERVAL_S = 3.0
# Patterns redacted from a pane tail before it is surfaced in a delivery-failure
# error. The agy header shows the signed-in account email, and the transcript
# region can echo tokens/keys agy printed; redaction is best-effort and biased
# toward over-redaction so a secret never leaks into a user-facing error.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
_SECRET_RES = (
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}"),  # OpenAI-style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"\bya29\.[A-Za-z0-9._-]{10,}"),  # Google OAuth access tokens
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),  # Google API keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key ids
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}"),  # JWTs
    re.compile(r"(?i)\b(?:bearer|token|api[_-]?key|secret|password)\b\s*[:=]?\s*\S{6,}"),
)
# TUI horizontal separator around agy's bottom composer.
_AGY_SEPARATOR_CHAR = "─"
# Box-drawing glyphs agy may use to frame/decorate the composer rule. A rule is
# detected when its non-space chars are all box-drawing AND it carries a run of
# the horizontal rule char, so a future agy that frames the composer with
# corner/join glyphs (e.g. ``╭────╮``) is still detected, while ordinary text
# (which carries non-box chars) never matches.
_AGY_BOX_GLYPHS = frozenset("─━╌╍┄┅┈┉╭╮╰╯┌┐└┘├┤┬┴┼│║╔╗╚╝═╠╣╦╩╬╴╵╶╷")


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
) -> None:
    """
    Advertise the tmux socket + target for the running agy terminal.

    The runner calls this after launching the agy terminal so the executor can
    shell out to ``tmux send-keys`` against the same private socket to bootstrap
    the first web turn (see :func:`inject_user_message_via_tui`). Written
    atomically next to ``state.json``.

    :param bridge_dir: Native Antigravity bridge directory.
    :param socket_path: Absolute path to the terminal's private tmux socket.
    :param tmux_target: tmux pane target string, e.g. ``"main"``.
    :param pid: Optional agy/pane pid, recorded for diagnostics only.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "socket_path": str(socket_path),
        "tmux_target": tmux_target,
        "updated_at": time.time(),
    }
    if pid is not None:
        payload["pid"] = pid
    path = bridge_dir / _TMUX_FILE
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_TMUX_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_tmux_info(bridge_dir: Path) -> dict[str, str] | None:
    """
    Return the advertised ``{socket_path, tmux_target}`` for the agy terminal.

    :param bridge_dir: Native Antigravity bridge directory.
    :returns: A dict with non-empty ``socket_path`` and ``tmux_target`` string
        values, or ``None`` when ``tmux.json`` is missing, unreadable, malformed,
        or has invalid field types.
    """
    try:
        raw = (bridge_dir / _TMUX_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    socket_path = data.get("socket_path")
    tmux_target = data.get("tmux_target")
    if (
        isinstance(socket_path, str)
        and socket_path
        and isinstance(tmux_target, str)
        and tmux_target
    ):
        return {"socket_path": socket_path, "tmux_target": tmux_target}
    return None


def _wait_for_tmux_info(bridge_dir: Path, *, timeout_s: float) -> dict[str, str]:
    """
    Block until the agy terminal's tmux target is advertised, or raise.

    :param bridge_dir: Native Antigravity bridge directory.
    :param timeout_s: Maximum seconds to wait for ``tmux.json``.
    :returns: The advertised ``{socket_path, tmux_target}``.
    :raises RuntimeError: When the target is not advertised within *timeout_s*.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        info = read_tmux_info(bridge_dir)
        if info is not None:
            return info
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"antigravity-native tmux target was not advertised within {timeout_s:.0f}s "
                "(is the agy terminal running on this host?)"
            )
        time.sleep(_TMUX_POLL_INTERVAL_S)


def _run_tmux(socket_path: str, *args: str) -> None:
    """
    Invoke ``tmux -S <socket> <args...>`` and raise on failure.

    :param socket_path: tmux server socket path.
    :param args: tmux subcommand and arguments, e.g.
        ``("send-keys", "-t", "main", "Enter")``.
    :returns: None.
    :raises RuntimeError: On a non-zero exit or a timeout.
    """
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"tmux command timed out after {_TMUX_SEND_TIMEOUT_S:.0f}s") from exc
    except OSError as exc:
        raise RuntimeError(f"tmux could not be executed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "<no output>"
        raise RuntimeError(f"tmux command failed (rc={proc.returncode}): {detail}")


def _capture_pane(socket_path: str, tmux_target: str) -> str:
    """
    Capture the visible pane contents; ``""`` on any failure (treat as not-ready).

    :param socket_path: tmux server socket path.
    :param tmux_target: tmux pane target.
    :returns: The pane text, or ``""`` when the capture fails.
    """
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, "capture-pane", "-p", "-t", tmux_target],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _session_alive(socket_path: str, tmux_target: str) -> bool:
    """
    Return whether the tmux session/pane still exists (the agy TUI is running).

    :param socket_path: tmux server socket path.
    :param tmux_target: tmux pane target.
    :returns: ``True`` when ``has-session`` reports the target exists.
    """
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, "has-session", "-t", tmux_target],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _paste_payload_bytes(text: str) -> bytes:
    r"""
    Encode text for ``tmux load-buffer``.

    Line breaks become CR (0x0D) so the agy TUI keeps a multi-line message as a
    single input under bracketed paste rather than submitting on each newline;
    tabs are kept; other control bytes are dropped (a stray ESC would close the
    bracketed paste early).

    :param text: Raw message text.
    :returns: The encoded paste payload.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    body = bytearray()
    for ch in normalized:
        if ch == "\n":
            body.append(0x0D)
            continue
        if ch == "\t":
            body.append(0x09)
            continue
        if ord(ch) < 0x20:
            continue
        body.extend(ch.encode("utf-8"))
    return bytes(body)


def _submit_needle(content: str, *, from_tail: bool = False) -> str:
    """
    A stable single-line substring used to confirm the paste rendered in the pane.

    :param content: Message content.
    :param from_tail: Use the last qualifying line for agy's collapsed composer.
    :returns: Up to 24 chars of the first (or last) line with at least 4 non-space
        chars, or ``""`` when no such line exists.
    """
    lines = content.splitlines()
    for line in reversed(lines) if from_tail else lines:
        stripped = line.strip()
        if len(stripped) >= 4:
            return stripped[:24]
    stripped = content.strip()
    return stripped[:24] if len(stripped) >= 4 else ""


def _redact_pane_secrets(text: str) -> str:
    """Redact emails and common secret shapes from a pane tail.

    Best-effort and biased toward over-redaction: the tail is embedded in a
    user-facing delivery error, and agy may have printed the signed-in account
    email or tokens/keys in the transcript region.
    """
    redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    for pattern in _SECRET_RES:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def _format_pane_debug_tail(pane: str) -> str:
    """
    Return a short, redacted pane tail for delivery failure diagnostics.

    The agy pane header can include the authenticated user's email address and
    the transcript region can echo secrets agy printed. Keep this diagnostic safe
    to surface in an executor error by redacting emails + common secret shapes
    (see :func:`_redact_pane_secrets`) and limiting the output to a small tail.
    """
    lines = [line.rstrip() for line in pane.splitlines() if line.strip()]
    tail = "\n".join(lines[-12:])
    return _redact_pane_secrets(tail) or "<empty pane>"


def _agy_input_region(pane: str) -> str:
    """
    Return agy's live bottom composer region, excluding transcript history.

    agy renders the editable composer between horizontal separator lines. After
    a successful submit, the submitted prompt remains in transcript history
    above a fresh empty composer, so checking the full pane would falsely think
    the draft is still present. The last separator pair scopes matching to the
    currently editable input box, mirroring Kiro's input-region guard.
    """
    lines = pane.splitlines()
    separator_indexes = [index for index, line in enumerate(lines) if _agy_separator_line(line)]
    if len(separator_indexes) >= 2:
        start = separator_indexes[-2] + 1
        end = separator_indexes[-1]
        return "\n".join(lines[start:end])
    return "\n".join(lines[-8:])


def _agy_separator_line(line: str) -> bool:
    """Return whether *line* is one of agy's composer separator rules.

    Accepts a pure ``─`` rule (agy 1.0.14) and a box-decorated rule (corner/join
    glyphs) so a future agy that frames the composer is still detected, while an
    ordinary text/draft line (which carries non-box chars) never matches. A short
    dash run (< 8 rule chars) is not treated as a separator.
    """
    stripped = line.strip()
    if stripped.count(_AGY_SEPARATOR_CHAR) < 8:
        return False
    return all(char in _AGY_BOX_GLYPHS for char in stripped if not char.isspace())


def _agy_turn_running(pane: str) -> bool:
    """Match activity in the footer, excluding draft and transcript text."""
    lines = pane.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if _agy_separator_line(lines[index]):
            return any(_AGY_ACTIVE_MARKER in line for line in lines[index + 1 :])
    return _AGY_ACTIVE_MARKER in pane


def _draft_in_input_region(
    pane: str, needle: str, baseline_region: str, *, tail_needle: str = ""
) -> bool:
    """
    Return whether the pasted draft is still visible in agy's composer.

    The baseline region prevents matching the empty prompt or stale text that was
    already present before this paste. Candidate lines are normalized by removing
    agy's leading ``>`` prompt glyph.

    When *needle* is empty (a message too short for a stable needle, e.g.
    ``"ok"``), any draft-like content in a composer that differs from the
    pre-paste baseline counts as present — so short messages are still render- and
    submit-verified by composer state instead of being submitted blind.
    """
    region = _agy_input_region(pane)
    if region == baseline_region:
        return False
    candidates = _agy_draft_candidate_lines(region)
    # A single-chunk paste can hide all text behind a placeholder.
    if any(_AGY_PASTE_PLACEHOLDER_RE.search(line) for line in candidates):
        return True
    normalized_needle = needle.strip() if needle else ""
    if not normalized_needle:
        return bool(candidates)
    # A split paste in agy 1.3.4 hides the first lines but leaves the tail visible.
    if (
        tail_needle.strip()
        and any(_AGY_COLLAPSED_LINES_RE.fullmatch(line) for line in candidates)
        and any(tail_needle.strip() in line for line in candidates)
    ):
        return True
    return any(
        line == normalized_needle
        or line.startswith(normalized_needle)
        or normalized_needle in line
        for line in candidates
    )


def _agy_draft_candidate_lines(region: str) -> list[str]:
    """Return composer lines that can represent editable draft text.

    The mode-cycle hint is chrome despite its ``>`` prompt glyph.
    Other lines carrying agy's ``>`` prompt glyph are editable draft content and are
    kept verbatim (after the glyph is stripped) even when the user's own text
    contains a word like ``Generating`` or a footer phrase — otherwise a
    legitimate message would be filtered out and misread as "never rendered".
    Status/footer rows (idle/active/"Generating …") carry no ``>`` prompt, so the
    chrome filters apply only to those non-draft lines.
    """
    candidates: list[str] = []
    for raw_line in region.splitlines():
        line = raw_line.strip()
        if not line or line == ">":
            continue
        if line.startswith(">"):
            draft = line[1:].strip()
            if _AGY_MODE_HINT_RE.fullmatch(draft):
                continue
            if draft:
                candidates.append(draft)
            continue
        if _AGY_IDLE_MARKER in line or _AGY_ACTIVE_MARKER in line:
            continue
        if "Generating" in line:
            continue
        candidates.append(line)
    return candidates


def _wait_for_agy_prompt_ready(socket_path: str, tmux_target: str, *, timeout_s: float) -> None:
    """
    Best-effort wait until agy's input box is mounted (its footer is rendered).

    Polls ``capture-pane`` for EITHER footer marker: :data:`_AGY_IDLE_MARKER`
    (ready for a new turn) or :data:`_AGY_ACTIVE_MARKER` (a turn is running). Both
    mean the input box is mounted and can take a paste — agy accepts a mid-turn
    paste and queues it as the next turn — so a mid-turn steer must NOT wait for
    the idle footer that will not appear until the active turn ends (that would
    burn the whole budget). Falls through after *timeout_s* rather than raising:
    the submit step is the real guard, and a changed footer string in a future
    agy build must not hard-block delivery.

    :param socket_path: tmux server socket path.
    :param tmux_target: tmux pane target.
    :param timeout_s: Maximum seconds to wait for a footer to render.
    :returns: None.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pane = _capture_pane(socket_path, tmux_target)
        if _AGY_IDLE_MARKER in pane or _AGY_ACTIVE_MARKER in pane:
            return
        time.sleep(_TMUX_POLL_INTERVAL_S)


def _submit_and_verify(
    socket_path: str,
    tmux_target: str,
    *,
    needle: str = "",
    tail_needle: str = "",
    baseline_region: str = "",
    draft_seen: bool = False,
) -> None:
    """
    Press Enter to submit the pasted draft, verifying by composer state.

    This mirrors the stronger Claude/Kiro native bridge contract. Footer text is
    only a readiness hint: it can change across agy builds, be truncated in narrow
    panes, or redraw independently from the editable draft. The reliable local
    submit signal is that the draft which was visible before Enter is no longer
    present in the live bottom composer region (for an empty needle this keys off
    the composer returning to the pre-paste baseline). If the same draft remains
    visible, Enter was likely folded into the paste burst and is re-sent within a
    bounded retry budget. ``inject_user_message_via_tui`` fails earlier when a
    text draft never renders into an idle composer.

    **Mid-turn steer.** When agy already shows :data:`_AGY_ACTIVE_MARKER`, a turn
    is running: the draft-disappearance signal is unreliable (agy queues the steer
    as the next ``USER_INPUT`` and the composer may not clear promptly) and a
    second Enter could queue a spurious empty turn. So a single best-effort Enter
    is sent and the function returns without re-sending or hard-failing — the
    forwarder is the system-of-record for whether the queued steer registered.

    :param socket_path: tmux server socket path.
    :param tmux_target: tmux pane target.
    :returns: None.
    :raises RuntimeError: When the agy TUI exits mid-submit, or the visible
        draft remains in the composer after :data:`_MAX_SUBMIT_ATTEMPTS` attempts.
    """
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "the agy terminal exited before the message could be submitted; restart the session"
        )
    # Mid-turn steer: a turn is already running, so verifying via draft
    # disappearance is unreliable and a re-sent Enter could queue an empty turn.
    # Deliver one best-effort Enter and return (see the docstring).
    if _agy_turn_running(_capture_pane(socket_path, tmux_target)):
        _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
        return
    if not draft_seen:
        _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
        return
    last_pane = _capture_pane(socket_path, tmux_target)
    for _ in range(_MAX_SUBMIT_ATTEMPTS):
        # Re-check liveness each attempt: if the TUI exited between the paste and
        # now, every capture returns "" and the draft never clears — so fail
        # fast with the real cause instead of spinning the full budget and then
        # blaming paste-coalescing.
        if not _session_alive(socket_path, tmux_target):
            raise RuntimeError(
                "the agy terminal exited before the message could be submitted; "
                "restart the session"
            )
        _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
        deadline = time.monotonic() + _SUBMIT_VERIFY_TIMEOUT_S
        while time.monotonic() < deadline:
            if not _session_alive(socket_path, tmux_target):
                raise RuntimeError(
                    "the agy terminal exited before the message could be submitted; "
                    "restart the session"
                )
            pane = _capture_pane(socket_path, tmux_target)
            last_pane = pane or last_pane
            if pane and not _draft_in_input_region(
                pane, needle, baseline_region, tail_needle=tail_needle
            ):
                return
            time.sleep(_TMUX_POLL_INTERVAL_S)
    draft_visible = _draft_in_input_region(
        last_pane, needle, baseline_region, tail_needle=tail_needle
    )
    raise RuntimeError(
        "agy did not accept the submitted message; the draft is still visible "
        "in the input box; "
        f"draft_visible={draft_visible}; pane_tail:\n{_format_pane_debug_tail(last_pane)}"
    )


def _account_verification_rejected(socket_path: str, tmux_target: str) -> bool:
    """
    Whether agy answered the submit with its account-verification notice.

    A verified submit only proves the draft left the composer, which is also true
    when agy swallows the turn during its startup eligibility check (see
    :data:`_AGY_VERIFYING_MARKER`). Distinguishes the two by watching for a
    running turn first, then requiring both the notice and the idle footer before
    retrying. The idle check prevents a stale notice from duplicating an accepted
    turn when agy's running footer is renamed or truncated.

    Fails open (returns ``False``) unless rejection is explicit within
    :data:`_VERIFY_PROBE_TIMEOUT_S`.

    :param socket_path: tmux server socket path.
    :param tmux_target: tmux pane target.
    :returns: ``True`` when the turn was rejected and should be re-delivered.
    """
    deadline = time.monotonic() + _VERIFY_PROBE_TIMEOUT_S
    pane = ""
    while True:
        pane = _capture_pane(socket_path, tmux_target) or pane
        if _agy_turn_running(pane):
            return False
        if time.monotonic() >= deadline:
            return _AGY_VERIFYING_MARKER in pane.lower() and _AGY_IDLE_MARKER in pane
        time.sleep(_TMUX_POLL_INTERVAL_S)


def _clear_idle_composer(socket_path: str, tmux_target: str, pane: str) -> str:
    """Clear idle drafts in bounded batches; allow unchanged one-line hints for paste.
    A composer whose draft cannot be cleared raises.
    """
    if not pane:
        raise RuntimeError("agy idle composer capture-pane failed; captured_pane_empty=True")
    for _ in range(_IDLE_CLEAR_MAX_BATCHES):
        before_region = _agy_input_region(pane)
        if _agy_turn_running(pane) or not _agy_draft_candidate_lines(before_region):
            return pane
        _run_tmux(
            socket_path,
            "send-keys",
            "-t",
            tmux_target,
            "-N",
            str(_IDLE_CLEAR_BATCH_SIZE),
            "C-w",
        )
        deadline = time.monotonic() + _IDLE_CLEAR_TIMEOUT_S
        captured_pane_seen = False
        while True:
            captured_pane = _capture_pane(socket_path, tmux_target)
            if captured_pane:
                pane = captured_pane
                captured_pane_seen = True
            if _agy_turn_running(pane) or not _agy_draft_candidate_lines(_agy_input_region(pane)):
                return pane
            if time.monotonic() >= deadline:
                break
            time.sleep(_TMUX_POLL_INTERVAL_S)
        if not captured_pane_seen:
            raise RuntimeError("agy idle composer capture-pane failed; captured_pane_empty=True")
        if _agy_input_region(pane) == before_region:
            candidates = _agy_draft_candidate_lines(before_region)
            # C-a/C-k already empties a one-line editable draft; only a surviving
            # single-line hint without draft markers can be treated as chrome.
            if (
                len(candidates) == 1
                and not _AGY_PASTE_PLACEHOLDER_RE.search(candidates[0])
                and not _AGY_COLLAPSED_LINES_RE.fullmatch(candidates[0])
            ):
                return pane
            break
    raise RuntimeError(
        "agy idle composer did not clear; draft_candidates_empty=False; "
        f"composer_tail:\n{_format_pane_debug_tail(_agy_input_region(pane))}"
    )


def _paste_and_submit(
    bridge_dir: Path,
    socket_path: str,
    tmux_target: str,
    *,
    content: str,
) -> None:
    """
    Clear the composer, paste *content* through a tmux buffer, and submit it.

    One delivery attempt, factored out of :func:`inject_user_message_via_tui` so
    a turn agy rejected while verifying the account can be re-delivered verbatim.
    Assumes the readiness gates already passed.

    :param bridge_dir: Native Antigravity bridge directory (holds the temp paste
        file).
    :param socket_path: tmux server socket path.
    :param tmux_target: tmux pane target.
    :param content: User text to deliver. Must be non-empty.
    :returns: None.
    :raises RuntimeError: When the TUI has exited, a tmux command fails, the
        pasted draft never renders, or the submit never starts a turn.
    """
    # C-a/C-k and Escape leave multi-line drafts; C-w batches clear an idle composer.
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-a")
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-k")
    capture_deadline = time.monotonic() + _BASELINE_CAPTURE_TIMEOUT_S
    while True:
        baseline_pane = _capture_pane(socket_path, tmux_target)
        if baseline_pane:
            break
        if time.monotonic() >= capture_deadline:
            raise RuntimeError(
                "agy baseline capture-pane failed before paste; captured_pane_empty=True"
            )
        time.sleep(_TMUX_POLL_INTERVAL_S)
    baseline_pane = _clear_idle_composer(socket_path, tmux_target, baseline_pane)
    baseline_region = _agy_input_region(baseline_pane)
    # ``delete=False`` + name captured BEFORE the write, so a write failure still
    # leaves a path the finally can unlink (no leaked temp file in the bridge dir).
    paste_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=bridge_dir, prefix="paste_", suffix=".bin", delete=False
        ) as paste_file:
            paste_path = paste_file.name
            # Trailing newline absorbs any trailing backslash so it can't escape Enter.
            paste_file.write(_paste_payload_bytes(content + "\n"))
        _run_tmux(socket_path, "load-buffer", "-b", _PASTE_BUFFER, paste_path)
        _run_tmux(
            socket_path,
            "paste-buffer",
            "-p",  # bracketed-paste markers — the TUI keeps newlines as data
            "-d",  # drop the buffer after pasting (no stale copies server-side)
            "-b",
            _PASTE_BUFFER,
            "-t",
            tmux_target,
        )
    finally:
        if paste_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(paste_path)
    # Wait until the paste is visibly committed to the input box before Enter, so
    # the submit is not folded into the paste burst (see _submit_and_verify). This
    # runs for EVERY message — including short ones with no stable needle — so a
    # paste that never lands in the composer is caught rather than submitted blind
    # (``_draft_in_input_region`` keys off composer change when the needle is "").
    needle = _submit_needle(content)
    tail_needle = _submit_needle(content, from_tail=True)
    # A mid-turn steer (agy already running a turn) is delivered best-effort: agy
    # queues it as the next turn and may not surface the draft in the composer the
    # same way an idle turn does, so a non-render is NOT a swallowed message here.
    # The render hard-fail below is for the idle/sequential case, where a paste
    # that never lands in the composer (e.g. eaten by an unhooked modal such as
    # the first-run trust gate) must fail loudly instead of vanishing.
    mid_turn = _agy_turn_running(_capture_pane(socket_path, tmux_target))
    draft_seen = False
    last_commit_pane = ""
    commit_deadline = time.monotonic() + _PASTE_COMMIT_TIMEOUT_S
    while time.monotonic() < commit_deadline:
        pane = _capture_pane(socket_path, tmux_target)
        last_commit_pane = pane or last_commit_pane
        if _draft_in_input_region(pane, needle, baseline_region, tail_needle=tail_needle):
            draft_seen = True
            break
        time.sleep(_TMUX_POLL_INTERVAL_S)
    if not draft_seen and not mid_turn:
        pane = _capture_pane(socket_path, tmux_target)
        with contextlib.suppress(RuntimeError):
            _clear_idle_composer(socket_path, tmux_target, pane)
        raise RuntimeError(
            "agy did not render the pasted message in its input box before submit; "
            f"pane_tail:\n{_format_pane_debug_tail(last_commit_pane)}"
        )
    time.sleep(_PASTE_SETTLE_S)
    _submit_and_verify(
        socket_path,
        tmux_target,
        needle=needle,
        tail_needle=tail_needle,
        baseline_region=baseline_region,
        draft_seen=draft_seen,
    )


def _plan_input_digest(text: str) -> str:
    """Identify a delivered turn without persisting its contents."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _plan_input_receipts(bridge_dir: Path) -> list[Path]:
    receipts_dir = bridge_dir / _PLAN_INPUT_RECEIPTS_DIR
    if receipts_dir.is_symlink() or not receipts_dir.is_dir():
        return []
    return sorted(receipts_dir.glob("receipt_*"))


def _read_plan_input_receipt(path: Path) -> tuple[str, bool, str | None] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    digest, plan, cascade = record.get("digest"), record.get("plan"), record.get("cascade")
    if not isinstance(digest, str) or len(digest) != 64 or not isinstance(plan, bool):
        return None
    if cascade is not None and not isinstance(cascade, str):
        return None
    return digest, plan, cascade


def _first_plan_input_cascade(bridge_dir: Path) -> str | None:
    receipts_dir = bridge_dir / _PLAN_INPUT_RECEIPTS_DIR
    if receipts_dir.is_symlink() or not receipts_dir.is_dir():
        return None
    path = receipts_dir / _FIRST_PLAN_INPUT_CASCADE
    if path.is_symlink():
        return None
    try:
        return path.read_text(encoding="ascii").strip() or None
    except (OSError, UnicodeError):
        return None


def _record_plan_input_receipt(bridge_dir: Path, content: str) -> Path:
    """Record each delivered turn's digest, tier, and known agy cascade."""
    tier_path = bridge_dir / "permission_tier"
    if tier_path.is_symlink():
        raise ValueError("Refusing symlink permission_tier")
    try:
        tier = tier_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        tier = ""
    state = read_bridge_state(bridge_dir)
    cascade_id = (
        state.conversation_id
        if state is not None and not is_placeholder_conversation_id(state.conversation_id)
        else None
    )

    receipts_dir = bridge_dir / _PLAN_INPUT_RECEIPTS_DIR
    if receipts_dir.is_symlink():
        raise ValueError("Refusing symlink plan-input-receipts")
    receipts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"receipt_{time.time_ns()}_", dir=receipts_dir)
    receipt = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            json.dump(
                {
                    "digest": _plan_input_digest(content),
                    "plan": tier == "plan",
                    "cascade": cascade_id,
                },
                handle,
            )
            handle.write("\n")
    except BaseException:
        receipt.unlink(missing_ok=True)
        raise
    for stale in _plan_input_receipts(bridge_dir)[:-_MAX_PLAN_INPUT_RECEIPTS]:
        stale.unlink(missing_ok=True)
    return receipt


def rebind_unmapped_plan_input_receipts(
    bridge_dir: Path, old_cascade_id: str, new_cascade_id: str
) -> None:
    """Carry pending deliveries from the headless bootstrap to the first TUI cascade."""
    for receipt in _plan_input_receipts(bridge_dir):
        record = _read_plan_input_receipt(receipt)
        if record is None or record[2] != old_cascade_id:
            continue
        try:
            _write_agy_permission_file(
                receipt,
                json.dumps({"digest": record[0], "plan": record[1], "cascade": new_cascade_id})
                + "\n",
            )
        except OSError:
            _logger.warning("Could not rebind pending plan-input receipt", exc_info=True)


def reconcile_plan_input_echo(
    bridge_dir: Path,
    text: str,
    *,
    cascade_id: str | None = None,
    step_key: tuple[object, ...] | None = None,
) -> str:
    """Bind one delivered turn to a USER_INPUT identity for repeatable mirroring."""
    if cascade_id is None or step_key is None:
        return text
    receipts_dir = bridge_dir / _PLAN_INPUT_RECEIPTS_DIR
    if receipts_dir.is_symlink() or not receipts_dir.is_dir():
        return text
    identity = hashlib.sha256(
        json.dumps((cascade_id, step_key), separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    mapped = receipts_dir / f"mapped_{identity}"
    exact = _plan_input_digest(text)
    prefixed = (
        _plan_input_digest(text.removeprefix("/plan ")) if text.startswith("/plan ") else None
    )

    bound = _read_plan_input_receipt(mapped)
    if bound is not None:
        digest, plan, _ = bound
        if digest == exact:
            return text
        if plan and digest == prefixed:
            return text.removeprefix("/plan ")
        return text

    first_cascade = _first_plan_input_cascade(bridge_dir)
    for receipt in _plan_input_receipts(bridge_dir):
        record = _read_plan_input_receipt(receipt)
        if record is None:
            continue
        digest, plan, delivered_cascade = record
        if delivered_cascade not in (None, cascade_id):
            continue
        if delivered_cascade is None and first_cascade not in (None, cascade_id):
            continue
        if digest == exact:
            result = text
        elif plan and digest == prefixed:
            result = text.removeprefix("/plan ")
        else:
            continue
        if delivered_cascade is None and first_cascade is None:
            _write_agy_permission_file(receipts_dir / _FIRST_PLAN_INPUT_CASCADE, cascade_id + "\n")
        os.replace(receipt, mapped)
        return result
    return text


def inject_user_message_via_tui(
    bridge_dir: Path,
    *,
    content: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """
    Deliver a user message into the agy TUI via a tmux bracketed paste + Enter.

    The executor's delivery path for EVERY web/mobile turn (sequential and
    mid-turn steer): a turn delivered over agy's connect-RPC ``SendAgentMessage``
    is recorded as a ``SYSTEM_MESSAGE`` the forwarder would not mirror, whereas
    typing into the TUI creates a real ``USER_INPUT`` step the forwarder mirrors
    in order. On a fresh session this also creates agy's conversation (agy mints
    its id only after processing a turn), which the forwarder then discovers.

    Steps: wait for the advertised tmux target and the agy input box (idle OR a
    running turn — agy accepts a mid-turn paste), then deliver via
    :func:`_paste_and_submit` (clear the draft, stream the content through a named
    tmux buffer, wait for the draft to render, submit and verify it left the live
    composer).

    **Account-verification re-delivery.** A verified submit does not prove a turn
    started: agy also consumes a turn submitted before its startup eligibility
    check settles and answers with a notice instead (see
    :data:`_AGY_VERIFYING_MARKER`). Delivery therefore re-sends the message while
    that notice is agy's answer, bounded by :data:`_VERIFY_RETRY_TIMEOUT_S`, so a
    programmatic first turn is not silently lost.

    :param bridge_dir: Native Antigravity bridge directory holding ``tmux.json``.
    :param content: User text from the web UI. Must be non-empty.
    :param timeout_s: Total readiness budget, shared across the two gates
        (``tmux.json`` advertised, then the input box mounted) so the worst case
        is one ``timeout_s``, not two stacked. Does NOT cover the
        account-verification re-delivery budget.
    :returns: None.
    :raises RuntimeError: When the tmux target is never advertised, the agy TUI
        has exited, a tmux command fails, the submit never starts a turn, or agy
        keeps rejecting the message while verifying the account.
    """
    if not content:
        raise RuntimeError("antigravity-native TUI injection requires non-empty content")
    # One shared deadline across both readiness gates: the prompt-ready gate gets
    # only the budget the tmux-target gate did not consume, capping total
    # readiness latency at timeout_s (the gates were previously each given a full
    # timeout_s, so a stuck launch cost 2x before delivery even began).
    deadline = time.monotonic() + timeout_s
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    # Fast-fail if the TUI already exited: otherwise the readiness gate polls a
    # dead pane for the full timeout and the web message is silently lost. A clear
    # error lets the executor surface an ExecutorError so the UI can say "restart".
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "the agy terminal is no longer running (the TUI exited); restart the session"
        )
    _wait_for_agy_prompt_ready(
        socket_path, tmux_target, timeout_s=max(0.0, deadline - time.monotonic())
    )
    # Deliver, then re-deliver for as long as agy keeps answering with its
    # account-verification notice (see ``_account_verification_rejected``). The
    # happy path costs one extra pane capture: a started turn is detected on the
    # first poll and returns immediately.
    retry_deadline = time.monotonic() + _VERIFY_RETRY_TIMEOUT_S
    while True:
        try:
            receipt = _record_plan_input_receipt(bridge_dir, content)
        except OSError:
            _logger.warning("Could not record TUI delivery receipt", exc_info=True)
            receipt = None
        try:
            _paste_and_submit(bridge_dir, socket_path, tmux_target, content=content)
            rejected = _account_verification_rejected(socket_path, tmux_target)
        except BaseException:
            if receipt is not None:
                try:
                    receipt.unlink(missing_ok=True)
                except OSError:
                    _logger.warning("Could not remove TUI delivery receipt", exc_info=True)
            raise
        if not rejected:
            return
        if receipt is not None:
            try:
                receipt.unlink(missing_ok=True)
            except OSError:
                _logger.warning("Could not remove rejected TUI delivery receipt", exc_info=True)
        if time.monotonic() >= retry_deadline:
            raise RuntimeError(
                "agy is still verifying the Antigravity account and rejected the message "
                f"for {_VERIFY_RETRY_TIMEOUT_S:.0f}s; the turn was not started"
            )
        _logger.info(
            "agy rejected the turn while verifying the account; re-delivering in %.0fs",
            _VERIFY_RETRY_INTERVAL_S,
        )
        time.sleep(_VERIFY_RETRY_INTERVAL_S)


def send_interaction_keys_via_tui(
    bridge_dir: Path,
    *keys: str,
) -> None:
    """
    Send raw key arguments to the agy TUI pane.

    The companion to :func:`inject_user_message_via_tui` for the OTHER thing the
    attended agy TUI maintains in parallel with the RPC step state: its
    **in-process permission / question prompt**. When agy runs attended (the
    web/mobile path types every turn into the TUI), an approval surfaces in the
    Omnigent web UI AND as agy's own numbered TUI prompt ("Do you want to
    proceed?", 1.Yes … 4.No). Delivering the verdict over
    :func:`omnigent.harnesses.antigravity_native.rpc.handle_user_interaction` flips the
    backend trajectory step to DONE and the command runs, but the **TUI prompt
    for that interaction can stay open** — and the next typed turn then lands in
    that stale prompt's filter/amend buffer instead of starting a fresh turn
    (live-verified; see ``docs/claude/antigravity-rpc-spike-notes.md`` §"attended
    TUI"). Older agy builds can need a TUI selection after RPC delivery. The
    interaction injector guards that selection with
    :func:`dismiss_interaction_prompt_via_tui`. This raw primitive also serves
    permission-tier controls, mirroring cursor-native's
    :func:`omnigent.harnesses.cursor_native.bridge.send_cursor_pane_keys`.

    Each argument is a tmux ``send-keys`` key argument — a bare ``"1"`` / ``"4"``
    selects a numbered option, ``"Enter"`` confirms, ``"Escape"`` cancels — sent
    in order as ONE ``send-keys`` invocation so the digit and its Enter cannot be
    reordered or split across the pane's input handling. Keys are interpreted by
    tmux (no ``-l``), so named keys like ``Enter`` / ``Escape`` work.

    :param bridge_dir: Native Antigravity bridge directory holding ``tmux.json``.
    :param keys: Ordered tmux key arguments, e.g. ``("1", "Enter")`` to approve.
    :returns: None.
    :raises RuntimeError: When the tmux target is not advertised, the agy TUI has
        exited, or the ``send-keys`` invocation fails.
    """
    if not keys:
        raise RuntimeError("antigravity-native TUI selection requires at least one key")
    info = read_tmux_info(bridge_dir)
    if info is None:
        raise RuntimeError(
            "antigravity-native tmux target not advertised (is the agy terminal running?)"
        )
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "the agy terminal is no longer running (the TUI exited); restart the session"
        )
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, *keys)


def _compact_prompt_text(text: str) -> str:
    """Compare question/option labels through terminal wrapping."""
    return re.sub(r"\s+", "", text)


def _wrapped_command_text(text: str) -> str:
    """Join display line breaks while preserving all horizontal whitespace."""
    return re.sub(r"\r?\n", "", text)


def _prompt_options(region: str) -> dict[str, str]:
    """Extract numbered choice labels, including lines wrapped below a choice."""
    choices = list(re.finditer(r"(?m)^\s*>?\s*([1-9]\d*)\.\s+", region))
    navigation = region.find("↑/↓ Navigate")
    options: dict[str, str] = {}
    for index, match in enumerate(choices):
        end = choices[index + 1].start() if index + 1 < len(choices) else navigation
        options[match.group(1)] = region[match.end() : end if end >= 0 else len(region)].strip()
    return options


def _live_interaction_prompt(
    pane: str, expected: PendingInteraction, keys: tuple[str, ...]
) -> bool:
    """Recognize the answered interaction in the live pane tail, not scrollback."""
    lines = [line.rstrip("\r") for line in pane.splitlines() if line.strip()]
    if not lines or not lines[-1].strip().startswith(_AGY_ACTIVE_MARKER):
        return False
    separators = [index for index, line in enumerate(lines) if _agy_separator_line(line)]
    region = "\n".join(lines[separators[-1] + 1 :] if separators else lines[-16:])
    if "↑/↓ Navigate" not in region or not keys:
        return False
    options = _prompt_options(region)

    spec = expected["spec"]
    if expected["kind"] == "permission":
        if keys not in (("1", "Enter"), ("4", "Enter")) or keys[0] not in options:
            return False
        resource = spec.get("resource")
        target = resource.get("target") if isinstance(resource, dict) else None
        command = re.search(
            r"Requesting permission for:\s*(.*?)\s*Run this command\?",
            region,
            re.DOTALL,
        )
        label = options[keys[0]].lstrip()
        return bool(
            isinstance(target, str)
            and target
            and "\n" not in target
            and "\r" not in target
            and command
            and _wrapped_command_text(command.group(1)) == target
            and (
                (keys[0] == "1" and re.match(r"(?i)yes\b", label))
                or (keys[0] == "4" and re.match(r"(?i)no\b", label))
            )
        )

    questions = spec.get("questions")
    heading = re.search(
        r"(?m)^Question ([1-9]\d*)/([1-9]\d*):\s*(.*?)(?=^\s*>?\s*[1-9]\d*\.\s)",
        region,
        re.DOTALL | re.MULTILINE,
    )
    if not isinstance(questions, list) or heading is None:
        return False
    question_index, total = int(heading.group(1)) - 1, int(heading.group(2))
    if total != len(questions) or question_index >= len(questions):
        return False
    question = questions[question_index]
    if not isinstance(question, dict):
        return False
    text = question.get("question")
    expected_options = question.get("options")
    if not isinstance(text, str) or not text or not isinstance(expected_options, list):
        return False
    if _compact_prompt_text(heading.group(3)) != _compact_prompt_text(text):
        return False
    option_ids: set[str] = set()
    for option in expected_options:
        if not isinstance(option, dict):
            return False
        option_id, option_text = option.get("id"), option.get("text")
        if not isinstance(option_id, str) or not isinstance(option_text, str):
            return False
        if option_id not in options or _compact_prompt_text(
            options[option_id]
        ) != _compact_prompt_text(option_text):
            return False
        option_ids.add(option_id)
    if not option_ids:
        return False
    if keys == ("Escape",):
        return True
    return len(keys) >= 2 and keys[-1] == "Enter" and all(key in option_ids for key in keys[:-1])


def dismiss_interaction_prompt_via_tui(
    bridge_dir: Path, *keys: str, expected: PendingInteraction
) -> bool:
    """Dismiss a still-open agy interaction after RPC delivery.

    Recent agy builds close the TUI prompt when RPC answers it. Wait briefly and
    recheck the live pane before sending keys, matching its text to the answered
    interaction. Older builds with a persistent prompt still receive the original
    selection. A failed capture is inconclusive and never authorizes a key send.
    """
    if not keys:
        raise RuntimeError("antigravity-native TUI selection requires at least one key")
    info = read_tmux_info(bridge_dir)
    if info is None:
        raise RuntimeError("antigravity-native tmux target not advertised")
    for attempt in range(_INTERACTION_DISMISSAL_POLLS):
        if attempt:
            time.sleep(_TMUX_POLL_INTERVAL_S)
        pane = _capture_pane(info["socket_path"], info["tmux_target"])
        if not pane:
            _logger.warning("agy interaction pane capture failed; skipping TUI dismissal")
            return False
        if not _live_interaction_prompt(pane, expected, keys):
            return False
    send_interaction_keys_via_tui(bridge_dir, *keys)
    return True


class AgyPermissionRollbackError(RuntimeError):
    """Live permission state could not be restored; the runner must cold-resume."""


# Verification budget for one live tier-switch step. The runner passes it as
# ``timeout_s`` to ``switch_agy_permission_tier`` and the server derives its
# forward timeout from it, so both sides budget from one number.
AGY_SWITCH_STEP_TIMEOUT_S = 3.0


def _agy_control_pane(bridge_dir: Path) -> str:
    info = read_tmux_info(bridge_dir)
    if info is None:
        raise RuntimeError("antigravity-native tmux target not advertised")
    return _capture_pane(info["socket_path"], info["tmux_target"])


def _require_agy_idle(bridge_dir: Path) -> str:
    pane = _agy_control_pane(bridge_dir)
    mode = parse_agy_agent_mode(pane)
    if mode is None and _AGY_ACTIVE_MARKER in pane:
        raise RuntimeError("Antigravity is busy; wait for the active turn to finish")
    if mode is None:
        raise RuntimeError(f"Antigravity input is not idle; pane: {_format_pane_debug_tail(pane)}")
    return pane


def parse_agy_config_row(pane: str, row: str) -> str | None:
    match = re.search(r"^\s*>\s+" + re.escape(row) + r"\s+(\S+)\s*$", pane, re.MULTILINE)
    return match.group(1) if match else None


def parse_agy_agent_mode(pane: str) -> str | None:
    line = next((line for line in reversed(pane.splitlines()) if line.strip()), "")
    if _AGY_IDLE_MARKER not in line:
        return None
    footer = line.split(_AGY_IDLE_MARKER, 1)[1].strip()
    if footer.startswith("accept-edits · "):
        return "accept-edits"
    if footer.startswith("plan · "):
        return "plan"
    return "default" if footer else None


_AGY_EFFORT_SUFFIX_RE = re.compile(r"-(low|medium|high|xhigh|max)$")


def split_agy_effort_suffix(slug: str) -> tuple[str, str | None]:
    """Split an agy model slug into its base id and trailing effort level.

    agy exposes one slug per (model, effort) pair — ``gemini-3-flash-low``,
    ``claude-sonnet-5-5-xhigh`` — so callers that need the bare model id or the
    effort separately share this split instead of re-typing the ladder.

    :param slug: An agy model slug, optionally carrying an effort suffix.
    :returns: ``(base, effort)``; *effort* is ``None`` when the slug has none.
    """
    match = _AGY_EFFORT_SUFFIX_RE.search(slug)
    if match is None:
        return slug, None
    return slug[: match.start()], match.group(1)


def run_agy_slash_command(bridge_dir: Path, command: str, *, expect: str, timeout_s: float) -> str:
    """Submit one idle slash command and verify its new pane confirmation."""
    if not command.startswith("/") or "\n" in command or "\r" in command:
        raise ValueError("Expected one slash command")
    baseline = _require_agy_idle(bridge_dir)
    info = read_tmux_info(bridge_dir)
    assert info is not None
    _paste_and_submit(bridge_dir, info["socket_path"], info["tmux_target"], content=command)
    deadline = time.monotonic() + timeout_s
    while True:
        pane = _agy_control_pane(bridge_dir)
        matches = [line.strip() for line in pane.splitlines() if expect in line]
        old = [line.strip() for line in baseline.splitlines() if expect in line]
        if matches and (matches != old or pane.count(expect) > baseline.count(expect)):
            return matches[-1]
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Antigravity did not confirm {command!r}; expected {expect!r}; "
                f"pane: {_format_pane_debug_tail(pane)}"
            )
        time.sleep(_TMUX_POLL_INTERVAL_S)


def _set_agy_config_row(bridge_dir: Path, row: str, target: str, *, timeout_s: float) -> None:
    row_filter = {"Tool Permission": "tool perm", "Non-Workspace Access": "non-work"}[row]

    def open_row() -> str:
        run_agy_slash_command(bridge_dir, "/config", expect="Settings", timeout_s=timeout_s)
        info = read_tmux_info(bridge_dir)
        assert info is not None
        _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "-l", row_filter)
        deadline = time.monotonic() + timeout_s
        while True:
            pane = _agy_control_pane(bridge_dir)
            value = parse_agy_config_row(pane, row)
            if value is not None:
                return value
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Antigravity config row {row!r} missing; "
                    f"pane: {_format_pane_debug_tail(pane)}"
                )
            time.sleep(_TMUX_POLL_INTERVAL_S)

    def close_row() -> None:
        # The first Escape clears the search; the second leaves settings.
        send_interaction_keys_via_tui(bridge_dir, "Escape")
        time.sleep(_TMUX_POLL_INTERVAL_S)
        send_interaction_keys_via_tui(bridge_dir, "Escape")
        deadline = time.monotonic() + timeout_s
        while parse_agy_agent_mode(_agy_control_pane(bridge_dir)) is None:
            if time.monotonic() >= deadline:
                raise RuntimeError("Antigravity config did not close")
            time.sleep(_TMUX_POLL_INTERVAL_S)

    try:
        value = open_row()
        for _ in range(4):
            if value == target:
                break
            send_interaction_keys_via_tui(bridge_dir, "Right")
            deadline = time.monotonic() + timeout_s
            while True:
                observed = parse_agy_config_row(_agy_control_pane(bridge_dir), row)
                if observed is not None and observed != value:
                    value = observed
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Antigravity config {row!r}: expected {target!r}; "
                        f"observed {observed!r} after Right from {value!r}"
                    )
                time.sleep(_TMUX_POLL_INTERVAL_S)
        if value != target:
            raise RuntimeError(
                f"Antigravity config {row!r}: expected {target!r}, observed {value!r}"
            )
    finally:
        close_row()
    try:
        confirmed = open_row()
        if confirmed != target:
            raise RuntimeError(
                f"Antigravity config {row!r}: expected {target!r}, reopened {confirmed!r}"
            )
    finally:
        close_row()


def _set_agy_agent_mode(bridge_dir: Path, target: str, *, timeout_s: float) -> None:
    value = parse_agy_agent_mode(_require_agy_idle(bridge_dir))
    for _ in range(3):
        if value == target:
            return
        send_interaction_keys_via_tui(bridge_dir, "BTab")
        deadline = time.monotonic() + timeout_s
        while True:
            observed = parse_agy_agent_mode(_agy_control_pane(bridge_dir))
            if observed is not None and observed != value:
                value = observed
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Antigravity agent mode: expected {target!r}; "
                    f"observed {observed!r} after Shift+Tab from {value!r}"
                )
            time.sleep(_TMUX_POLL_INTERVAL_S)
    if value != target:
        raise RuntimeError(f"Antigravity agent mode: expected {target!r}, observed {value!r}")


def switch_agy_permission_tier(
    bridge_dir: Path, *, tier: str, previous: str, timeout_s: float
) -> str:
    """Switch live controls in guard-safe order and verify reverse undo on failure."""
    if tier not in AGY_PERMISSION_TIERS or previous not in AGY_PERMISSION_TIERS:
        raise ValueError("Unknown antigravity permission tier")
    if "full" in (tier, previous):
        raise ValueError("Skip permissions is create-only")
    _require_agy_idle(bridge_dir)
    snapshot_path = bridge_dir / "agy_user_settings.json"
    # A launch always snapshots the user's /config values before any tier file
    # is written, so a live switch without one means state was lost; guessing
    # here could undo a user's own setting on rollback.
    if not snapshot_path.exists():
        raise ValueError("missing user settings snapshot")
    snapshot = _read_agy_permission_json(snapshot_path)
    user_access = snapshot.get("allowNonWorkspaceAccess", {})
    if not isinstance(user_access, dict) or not isinstance(user_access.get("present"), bool):
        raise ValueError("Invalid Antigravity user settings snapshot")
    user_on = user_access["present"] and user_access.get("value") is True
    auto, was_auto = tier == "auto", previous == "auto"
    mode = "accept-edits" if auto else "default" if tier == "ask" else tier
    previous_mode = "accept-edits" if was_auto else "default" if previous == "ask" else previous
    steps: list[tuple[str, str, str]]
    if auto and not was_auto:
        steps = [
            ("tier", tier, previous),
            ("Tool Permission", "always-proceed", "request-review"),
            ("Non-Workspace Access", "on", "on" if user_on else "off"),
            ("mode", mode, previous_mode),
        ]
    elif was_auto and not auto:
        steps = [
            ("Tool Permission", "request-review", "always-proceed"),
            ("Non-Workspace Access", "on" if user_on else "off", "on"),
            ("mode", mode, previous_mode),
            ("tier", tier, previous),
        ]
    elif auto:
        steps = [("tier", tier, previous)]
    else:
        steps = [("mode", mode, previous_mode)]

    def apply(component: str, value: str) -> None:
        if component == "tier":
            reassert_agy_permission_tier(bridge_dir, value)
        elif component == "mode":
            _set_agy_agent_mode(bridge_dir, value, timeout_s=timeout_s)
        else:
            _set_agy_config_row(bridge_dir, component, value, timeout_s=timeout_s)

    attempted: list[tuple[str, str, str]] = []
    try:
        for step in steps:
            # A driver's failed verification can follow a successful key press.
            attempted.append(step)
            apply(step[0], step[1])
        apply_agy_permission_tier(bridge_dir, tier)
    except (OSError, RuntimeError, ValueError) as exc:
        try:
            for component, _, old in reversed(attempted):
                apply(component, old)
            apply_agy_permission_tier(bridge_dir, previous)
        except (OSError, RuntimeError, ValueError) as undo:
            detail = str(undo)
            try:
                from dataclasses import replace

                from omnigent.permission_tiers.host import (
                    HOST_PERMISSION_STATE_FILE,
                    read_host_permission_state,
                    write_host_permission_state,
                )

                state_path = bridge_dir / HOST_PERMISSION_STATE_FILE
                state = read_host_permission_state(state_path)
                write_host_permission_state(state_path, replace(state, status="blocked"))
            except (OSError, ValueError, TypeError) as safety:
                detail += f"; permission cache remains unavailable: {type(safety).__name__}"
            try:
                pane = _agy_control_pane(bridge_dir)
            except (RuntimeError, OSError, ValueError) as capture:
                pane = f"unavailable: {capture}"
            raise AgyPermissionRollbackError(
                f"Antigravity rollback unverified: {detail}; pane: {_format_pane_debug_tail(pane)}"
            ) from exc
        raise RuntimeError(
            f"Antigravity permission switch failed; restored {previous}: {exc}"
        ) from exc
    return tier


def native_input_ready(session_id: str, instance: TerminalInstance) -> bool:
    """Provider ``input_ready_probe``: agy's input footer is rendered (idle or mid-turn).

    :param session_id: Omnigent conversation id (unused; the pane is enough).
    :param instance: The live terminal; the watcher already captured its pane.
    """
    del session_id
    pane = instance.last_pane_text() or ""
    return _AGY_IDLE_MARKER in pane or _AGY_ACTIVE_MARKER in pane
