"""Timeout contract for native Codex startup and control forwarding."""

# A loaded host can take over 10 seconds to start its session app-server.
# Match the native terminal's startup allowance; an early exit fails at once.
APP_SERVER_READY_TIMEOUT_SECONDS = 180.0

# Thread discovery starts after terminal creation, including cold-resume repair.
SETTINGS_BRIDGE_READY_TIMEOUT_SECONDS = 60.0
SETTINGS_LAUNCH_TIMEOUT_SECONDS = 245.0

# Preserve 65 seconds for settings RPCs, confirmation mirrors, and cleanup after
# the bounded launch and bridge-discovery phases finish.
SETTINGS_FORWARD_TIMEOUT_SECONDS = SETTINGS_LAUNCH_TIMEOUT_SECONDS + 65.0

# Cover interrupt, backend close, process retirement and final reporting, with
# a reply margin at each enclosing HTTP boundary.
HELPER_STOP_TIMEOUT_SECONDS = 35.0
HELPER_STOP_FORWARD_TIMEOUT_SECONDS = HELPER_STOP_TIMEOUT_SECONDS + 5.0
HELPER_STOP_EVENT_TIMEOUT_SECONDS = HELPER_STOP_FORWARD_TIMEOUT_SECONDS + 5.0
