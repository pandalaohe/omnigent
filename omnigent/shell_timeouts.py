"""Execution and dispatch budgets shared by sys_os_shell callers."""

from collections.abc import Mapping

DEFAULT_SHELL_TIMEOUT_S = 2100
MAX_SHELL_DISPATCH_TIMEOUT_S = 7200
_SHELL_RESULT_GRACE_S = 30


def shell_dispatch_timeout(
    tool_name: str, arguments: Mapping[str, object], fallback: float
) -> float:
    """Wait for the shell's execution budget plus time to deliver its result."""
    if tool_name != "sys_os_shell":
        return fallback
    timeout = arguments.get("timeout")
    if timeout is None:
        timeout = DEFAULT_SHELL_TIMEOUT_S
    if type(timeout) is not int or timeout < 1:
        return fallback
    return max(fallback, min(timeout, MAX_SHELL_DISPATCH_TIMEOUT_S) + _SHELL_RESULT_GRACE_S)
