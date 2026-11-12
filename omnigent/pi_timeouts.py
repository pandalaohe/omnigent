"""Backstops for Pi control and cold-start confirmation, in seconds."""

CONTROL_TIMEOUT_S = 60.0
BOOTSTRAP_CONTROL_TIMEOUT_S = 55.0
COLD_START_TIMEOUT_S = 180.0
# Each observer leaves the inner owner time to report its specific failure.
BOOTSTRAP_RESPONSE_TIMEOUT_S = 195.0
INITIALIZATION_TIMEOUT_S = 210.0
INITIALIZATION_WAIT_TIMEOUT_S = 225.0
PERMISSION_CONFIRMATION_TIMEOUT_S = 240.0


def startup_dependency_failure(text: str) -> str | None:
    """Return a fixed diagnostic, never dependency output or a private path."""
    for code in ("ERR_MODULE_NOT_FOUND", "MODULE_NOT_FOUND"):
        if code in text:
            return code
    if "pi-subagents is missing ./native-child-lifecycle" in text:
        return "native_child_lifecycle"
    return None
