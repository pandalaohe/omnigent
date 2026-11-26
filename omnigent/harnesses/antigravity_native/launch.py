"""Launch-config helpers for native Antigravity (agy) TUI sessions.

This module assembles the ``agy`` command-line arguments and environment
overrides needed to start or resume a native agy session.  It contains only
argument builders and setup credential resolution.

Key design points:

* **Authentication** — an Omnigent Gemini provider selects the API key and
  endpoint together. Otherwise agy inherits the user's existing login.

* **Per-session identity is discovered, not assigned** — agy mints its own UUID
  conversation and ignores the ``ANTIGRAVITY_CONVERSATION_ID`` env var
  (verified empirically; see ``docs/claude/antigravity-spike-findings.md``). A
  fresh session therefore sets nothing for identity; the forwarder discovers
  agy's real id from the newest ``brain/<uuid>`` dir and persists it. A resume
  passes ``--conversation <id>`` with that discovered id on the command line.

* **Workspace = the agy process cwd** — agy runs its tools in its own working
  directory (verified empirically: a tmux-launched agy with ``cwd`` set to a
  project dir ran ``run_command`` with that ``Cwd``, never the default
  ``scratch`` dir). The launcher pins the terminal cwd to the session working
  directory, so no ``--add-dir`` flag is required.

* **No usable env knobs** — agy also ignores ``ANTIGRAVITY_SIDECAR_WEB_PORT``
  (it binds its own ephemeral ports) and ``ANTIGRAVITY_EXECUTABLE_DATA_DIR``
  (its conversation store stays under the default ``~/.gemini/antigravity-cli``)
  for the host process; both are sidecar-plugin-scoped no-ops. So
  :func:`build_agy_launch` emits no env overrides for a fresh session.

* **Auth is inherited** — current agy releases accept either their persisted
  Google OAuth login or ``GEMINI_API_KEY``. The isolated settings prepared by
  the bridge select the Gemini provider when the key is present.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from omnigent._platform import CLI_FALLBACK_DIRS_ENV_VAR, IS_WINDOWS, resolve_cli_binary
from omnigent._windows_launcher import (
    merge_shim_env,
    probe_windows_binary_in_dir,
    unwrap_windows_cmd_shim,
    windows_launch_spec,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.antigravity_native.bridge import split_agy_effort_suffix
from omnigent.inner._subprocess_lifecycle import terminate_direct_subprocess
from omnigent.onboarding.gemini_auth import gemini_auth_has_credential
from omnigent.util.reasoning_effort import EFFORT_ALIASES, clamp_antigravity_effort

_logger = logging.getLogger(__name__)
AGY_MODEL_PROBE_TIMEOUT_S = 90.0
# Allow native setup, cold-start and notice delivery around the probe.
AGY_MODEL_LAUNCH_TIMEOUT_S = AGY_MODEL_PROBE_TIMEOUT_S + 90.0
# The binding PATCH waits for the runner's session initialization, which can
# include the same model probe and cold start before it returns.
AGY_RUNNER_INIT_TIMEOUT_S = AGY_MODEL_LAUNCH_TIMEOUT_S + 60.0
# The CLI's post-bind ensure request must also outlive the runner-side launch.
AGY_TERMINAL_ENSURE_TIMEOUT_S = AGY_MODEL_LAUNCH_TIMEOUT_S + 10.0
# Keep the CLI's binding wait outside the server-side initialization budget.
AGY_CLI_BIND_TIMEOUT_S = AGY_RUNNER_INIT_TIMEOUT_S + 60.0
_agy_model_probes: dict[
    tuple[asyncio.AbstractEventLoop, str, str, str | None], asyncio.Task[bool | None]
] = {}

# Tier settings and the PreToolUse guard are installed by the bridge.
# This flag bypasses agy's native permission prompts for the full tier.
_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
# Omnigent permission mode that maps to agy's all-or-nothing bypass.
_BYPASS_PERMISSION_MODE = "bypassPermissions"
# Fallback binary path when ``agy`` is not on PATH.
_AGY_FALLBACK_PATH = Path.home() / ".local" / "bin" / "agy"
# Install instructions surfaced in the RuntimeError when agy is missing.
_AGY_INSTALL_HINT = (
    "Install agy via the Antigravity installer script:\n"
    "  curl -fsSL https://antigravity.google/cli/install.sh | bash\n"
    "Then restart your shell so ~/.local/bin is on PATH."
)
# Value-taking options reported by ``agy --help`` plus the two hidden options
# Omnigent owns. Their following token stays opaque even when it resembles one
# of the model/effort flags extracted below.
_AGY_OPTIONS_WITH_VALUE = frozenset(
    {
        "--add-dir",
        "--agent",
        "--conversation",
        "--csrf_token",
        "--effort",
        "--gemini_dir",
        "--input-format",
        "--json-schema",
        "--log-file",
        "--mode",
        "--model",
        "--output-format",
        "--print-timeout",
        "--project",
        "--settings",
    }
)


def agy_binary_path() -> str:
    """Return the absolute path to the ``agy`` CLI executable.

    Searches PATH first via :func:`shutil.which`, then falls back to
    ``~/.local/bin/agy`` (the installer's default location). On Windows it
    resolves through the unified launcher base (merged registry PATH, known
    package-manager dirs, ``PATHEXT``, and native ``.cmd`` shim unwrapping).

    :returns: Absolute path string for the agy executable, e.g.
        ``"/home/user/.local/bin/agy"``.
    :raises RuntimeError: When agy is not found on PATH and
        ``~/.local/bin/agy`` does not exist.  The error message includes
        the curl installer command.
    """
    if IS_WINDOWS:
        resolved = resolve_cli_binary("agy", which=shutil.which)
        if resolved is None and os.environ.get(CLI_FALLBACK_DIRS_ENV_VAR) is None:
            resolved = probe_windows_binary_in_dir(
                _AGY_FALLBACK_PATH.parent, _AGY_FALLBACK_PATH.name
            )
        if resolved is not None:
            unwrapped = unwrap_windows_cmd_shim(Path(resolved))
            if unwrapped is not None and unwrapped.kind == "native" and unwrapped.target.is_file():
                return str(unwrapped.target)
            return resolved
        raise RuntimeError(
            f"agy CLI not found on PATH and not at {_AGY_FALLBACK_PATH}.\n{_AGY_INSTALL_HINT}"
        )
    on_path = shutil.which("agy")
    if on_path is not None:
        return on_path
    if _AGY_FALLBACK_PATH.is_file():
        return str(_AGY_FALLBACK_PATH)
    raise RuntimeError(
        f"agy CLI not found on PATH and not at {_AGY_FALLBACK_PATH}.\n{_AGY_INSTALL_HINT}"
    )


@dataclass(frozen=True)
class NativeAntigravityLaunch:
    """How a native Antigravity (agy) session should be launched.

    :param auth_mode: "api-key" for a resolved Gemini credential, otherwise
        "subscription" to let agy use its existing login.
    :param model: Optional model label to pass to agy via ``--model``, e.g.
        ``"gemini-2.5-pro"``.  ``None`` lets agy use its own default.
    :param extra_env: Additional environment variables to inject into the
        agy process, beyond those set by :func:`build_agy_launch`.  Empty
        by default.
    """

    auth_mode: str
    model: str | None
    extra_env: dict[str, str] = field(default_factory=dict, repr=False)


def resolve_native_antigravity_launch(
    *,
    model: str | None = None,
) -> NativeAntigravityLaunch:
    """Resolve setup credentials and a model override for native agy.

    Configured Gemini providers take precedence over legacy keys and ambient
    credentials. With no API key, agy retains its existing OAuth/ADC behavior.
    """
    from omnigent.harnesses.antigravity_native.credentials import resolve_antigravity_credentials

    try:
        credentials = resolve_antigravity_credentials()
    except OmnigentError as exc:
        if exc.code != ErrorCode.INVALID_INPUT:
            raise
        raise OmnigentError(
            exc.message + " Run omni setup on the host to repair the Gemini configuration.",
            code=ErrorCode.HARNESS_NOT_CONFIGURED,
        ) from None
    if credentials is not None:
        return NativeAntigravityLaunch(
            auth_mode="api-key",
            model=model if model is not None else credentials.model,
            extra_env=credentials.environment(),
        )
    if not gemini_auth_has_credential():
        _logger.warning(
            "No agy credential found (checked GEMINI_API_KEY, ~/.gemini/oauth_creds.json "
            "and ~/.gemini/antigravity-cli/antigravity-oauth-token, plus "
            "`agy models` on macOS for a Keychain-stored login); "
            "agy will prompt for login on first run."
        )
    return NativeAntigravityLaunch(auth_mode="subscription", model=model)


def should_skip_permissions(
    *,
    permission_mode: str | None,
    headless: bool,
) -> bool:
    """Resolve the CLI create request's legacy bypass choice.

    The CLI sends this choice as the full tier before binding a runner.

    :param permission_mode: The session's effective Omnigent permission mode,
        e.g. ``"bypassPermissions"`` / ``"default"`` / ``"acceptEdits"``.
        ``None`` is treated as a non-bypass mode.
    :param headless: ``True`` when no interactive client will attach (sandbox /
        autonomous / server-spawned / detached launch).
    :returns: ``True`` when the request selects the ``full`` tier (the legacy
        bypass flag on direct CLI launches).
    """
    if permission_mode == _BYPASS_PERMISSION_MODE:
        return True
    return headless


async def _probe_agy_subscription_model(
    command: str, model: str, effort: str | None
) -> bool | None:
    """Return acceptance, explicit rejection, or an inconclusive probe."""
    cmd_argv, shim_env = windows_launch_spec(command, is_windows=IS_WINDOWS)
    args = [
        *cmd_argv,
        f"--gemini_dir={Path.home() / '.gemini'}",
        "--model",
        model,
    ]
    if effort is not None:
        args.extend(["--effort", effort])
    probe_env = merge_shim_env(None, shim_env)
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            "-p",
            "/model",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=probe_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, NotImplementedError):
        return None
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=AGY_MODEL_PROBE_TIMEOUT_S
        )
    except (OSError, TimeoutError):
        return None
    finally:
        await terminate_direct_subprocess(process, terminate_timeout=0.5, kill_timeout=0.5)
    if process.returncode == 0:
        return True
    if b"invalid model selection" in stderr.lower():
        return False
    # An older CLI, sign-in, or transport failure is not a verdict about the ID.
    return None


@dataclass(frozen=True)
class AgyLaunchCommand:
    """Command and environment for the native agy terminal."""

    argv: list[str] = field(repr=False)
    env: dict[str, str] = field(repr=False)
    subscription_model: str | None = None
    effort: str | None = None

    async def check_subscription_model(self) -> str | None:
        """Reject an unavailable model or return a caller-visible warning."""
        model = self.subscription_model
        if model is None:
            return None
        command = self.argv[0]
        key = (asyncio.get_running_loop(), command, model, self.effort)

        async def probe() -> bool | None:
            try:
                return await _probe_agy_subscription_model(command, model, self.effort)
            finally:
                _agy_model_probes.pop(key, None)

        task = _agy_model_probes.get(key)
        if task is None:
            task = asyncio.create_task(probe())
            _agy_model_probes[key] = task
        accepted = await asyncio.shield(task)
        if accepted is False:
            raise OmnigentError(
                f"Antigravity does not offer model {model!r} for this login. "
                "Choose a model from the host's `agy models` list.",
                code=ErrorCode.HARNESS_NOT_CONFIGURED,
            )
        if accepted is None:
            return (
                f"Could not verify Antigravity model {model!r} before launch. "
                "Antigravity may use its default model."
            )
        return None


def _is_xhigh_effort(effort: str | None) -> bool:
    """Return whether *effort* is ``xhigh`` or one of its higher aliases."""
    return effort is not None and EFFORT_ALIASES.get(effort, effort) == "xhigh"


def _extract_model_and_effort_from_extra_args(
    extra_args: tuple[str, ...],
) -> tuple[str | None, str | None, tuple[str, ...], bool, bool]:
    """Strip ``--model`` and ``--effort`` (split or ``=`` forms) from *extra_args*.

    Stops parsing flags at ``--`` so positional arguments are preserved verbatim.
    When repeated, the last occurrence wins, matching CLI flag semantics.
    """
    extracted_model: str | None = None
    extracted_effort: str | None = None
    remaining: list[str] = []
    skip_permissions_seen = False
    csrf_token_seen = False
    xhigh_seen = False
    idx = 0
    while idx < len(extra_args):
        arg = extra_args[idx]
        if arg == "--":
            remaining.extend(extra_args[idx:])
            break
        if arg == "--model":
            if idx + 1 < len(extra_args) and extra_args[idx + 1] != "--":
                val = extra_args[idx + 1]
                extracted_model = val if val else None
                if val and _is_xhigh_effort(split_agy_effort_suffix(val)[1]):
                    xhigh_seen = True
                idx += 2
                continue
            idx += 1
            continue
        if arg.startswith("--model="):
            val = arg.partition("=")[2]
            extracted_model = val if val else None
            if val and _is_xhigh_effort(split_agy_effort_suffix(val)[1]):
                xhigh_seen = True
            idx += 1
            continue
        if arg == "--effort":
            if idx + 1 < len(extra_args) and extra_args[idx + 1] != "--":
                val = extra_args[idx + 1]
                extracted_effort = val if val else None
                if _is_xhigh_effort(val):
                    xhigh_seen = True
                idx += 2
                continue
            idx += 1
            continue
        if arg.startswith("--effort="):
            val = arg.partition("=")[2]
            extracted_effort = val if val else None
            if _is_xhigh_effort(val):
                xhigh_seen = True
            idx += 1
            continue
        if arg == _SKIP_PERMISSIONS_FLAG:
            if not skip_permissions_seen:
                remaining.append(arg)
                skip_permissions_seen = True
            idx += 1
            continue
        if arg == "--csrf_token" or arg.startswith("--csrf_token="):
            csrf_token_seen = True
        remaining.append(arg)
        if (
            arg in _AGY_OPTIONS_WITH_VALUE
            and idx + 1 < len(extra_args)
            and extra_args[idx + 1] != "--"
        ):
            remaining.append(extra_args[idx + 1])
            idx += 2
            continue
        idx += 1
    return extracted_model, extracted_effort, tuple(remaining), csrf_token_seen, xhigh_seen


def build_agy_launch(
    *,
    conversation_id: str | None,
    model: str | None,
    resume: bool,
    effort: str | None = None,
    extra_args: tuple[str, ...] = (),
    log_dir: Path | None = None,
    command: str | None = None,
    catalog_models: Iterable[Mapping[str, object]] | None = None,
) -> AgyLaunchCommand:
    """Build the argv and environment overrides for an agy launch.

    Each launch receives a fresh CSRF token for its local RPC server unless
    the caller supplies one in ``extra_args``.

    Two launch modes are supported:

    * **Fresh session** (``resume=False``) — nothing is set for identity. agy
      mints its own UUID conversation and ignores any
      ``ANTIGRAVITY_CONVERSATION_ID`` we set (verified empirically), so
      *conversation_id* is not used and the forwarder discovers agy's real id
      afterward. The ``--conversation`` flag is NOT added to argv.

    * **Resume session** (``resume=True``) — ``--conversation <id>`` is
      appended to argv so agy resumes the existing conversation. Here
      *conversation_id* MUST be agy's real (discovered) UUID, persisted from a
      prior launch; agy cannot resume the launcher's minted placeholder.

    Tier-aware callers install settings and hooks before launch and supply their
    mode flags in extra_args. Legacy bypass flags are deduplicated.

    Both launch paths resolve setup credentials here and pass the resulting
    environment to the child. The terminal spec supplies the workspace cwd.

    :param conversation_id: agy's real conversation id to resume, e.g.
        ``"68caaeac-..."``. Required (non-``None``) when ``resume=True``;
        ignored when ``resume=False``.
    :param model: Optional model label, e.g. ``"gemini-2.5-pro"``.
        ``None`` omits ``--model`` so agy uses its default.
    :param effort: Optional agy reasoning effort passed through as --effort.
    :param resume: ``True`` to resume an existing conversation; ``False``
        to start a fresh one.
    :param extra_args: Additional raw CLI args appended after all generated
        flags, e.g. ``("--print-timeout", "30")``.
    :param log_dir: Private directory for this launch's RPC discovery log.
    :param command: Override the resolved agy executable for both probe and launch.
    :param catalog_models: Optional agy model-catalog snapshot used to require
        explicit per-model ``xhigh`` support when a row is present.
    :returns: The launch command. Call ``check_subscription_model`` before
        starting the terminal.
    :raises ValueError: When ``resume=True`` but *conversation_id* is ``None``
        or empty (agy needs a real id to resume).
    :raises OmnigentError: When credentials need repair.
    :raises RuntimeError: When the agy executable cannot be found.
    """
    (
        extra_model,
        extra_effort,
        remaining_extra_args,
        csrf_token_seen,
        extra_has_xhigh,
    ) = _extract_model_and_effort_from_extra_args(extra_args)
    model_suffix = split_agy_effort_suffix(model)[1] if model is not None else None
    rewrite_model_and_effort = (
        _is_xhigh_effort(effort) or _is_xhigh_effort(model_suffix) or extra_has_xhigh
    )
    launch = resolve_native_antigravity_launch(
        model=extra_model if rewrite_model_and_effort and extra_model is not None else model
    )
    if (
        not rewrite_model_and_effort
        and launch.model is not None
        and _is_xhigh_effort(split_agy_effort_suffix(launch.model)[1])
    ):
        rewrite_model_and_effort = True
        if extra_model is not None:
            launch = NativeAntigravityLaunch(
                auth_mode=launch.auth_mode,
                model=extra_model,
                extra_env=launch.extra_env,
            )
    argv: list[str] = [command if command is not None else agy_binary_path()]
    if not csrf_token_seen:
        argv.append(f"--csrf_token={secrets.token_urlsafe(32)}")
    if log_dir is not None:
        argv.append(f"--log-file={log_dir / f'agy-{secrets.token_hex(16)}.log'}")
    if resume:
        if not conversation_id:
            raise ValueError("Resuming an agy conversation requires a conversation id.")
        argv.extend(["--conversation", conversation_id])
    model_arg = launch.model
    final_effort = extra_effort if extra_effort is not None else effort
    if not rewrite_model_and_effort:
        if model_arg is not None:
            model_arg = split_agy_effort_suffix(model_arg)[0] if effort else model_arg
            argv.extend(["--model", model_arg])
        if effort is not None:
            argv.extend(["--effort", effort])
        skip_permissions_seen = False
        for idx, arg in enumerate(extra_args):
            if arg == "--":
                argv.extend(extra_args[idx:])
                break
            if arg == _SKIP_PERMISSIONS_FLAG:
                if skip_permissions_seen:
                    continue
                skip_permissions_seen = True
            argv.append(arg)
        model_arg = extra_model if extra_model is not None else model_arg
    else:
        if model_arg is not None:
            base_model, suffix_effort = split_agy_effort_suffix(model_arg)
            if final_effort:
                final_effort = clamp_antigravity_effort(
                    final_effort, base_model, catalog_models=catalog_models
                )
                model_arg = base_model
            elif suffix_effort is not None:
                clamped_suffix = clamp_antigravity_effort(
                    suffix_effort, base_model, catalog_models=catalog_models
                )
                if clamped_suffix != suffix_effort:
                    model_arg = f"{base_model}-{clamped_suffix}"
            argv.extend(["--model", model_arg])
        elif final_effort:
            final_effort = clamp_antigravity_effort(
                final_effort, None, catalog_models=catalog_models
            )
        if final_effort is not None:
            argv.extend(["--effort", final_effort])
        argv.extend(remaining_extra_args)

    subscription_model = model_arg if launch.auth_mode == "subscription" else None
    return AgyLaunchCommand(argv, launch.extra_env, subscription_model, final_effort)
