"""Shared Pi CLI argv for RPC startup and version discovery."""

from __future__ import annotations

from omnigent._platform import IS_WINDOWS, resolve_cli_binary
from omnigent._windows_launcher import windows_launch_spec


def pi_cli_launch(pi_path: str) -> tuple[list[str], dict[str, str]]:
    """Return ``(argv, extra_env)`` bypassing npm/pnpm Windows shell shims in one parse."""
    return windows_launch_spec(
        pi_path,
        strict_batch=True,
        missing_target_prefix="Pi npm entry point does not exist",
        missing_node_error="Pi launcher requires Node.js",
        unknown_batch_error=(
            "Pi batch launcher is not an npm shim; configure a native executable instead"
        ),
        node_resolver=resolve_cli_binary,
        is_windows=IS_WINDOWS,
    )


def pi_cli_argv(pi_path: str) -> list[str]:
    """Launch JS through Node and bypass npm/pnpm Windows shell shims."""
    return pi_cli_launch(pi_path)[0]
