"""Shared trusted host state for Pi extension scenarios."""

from pathlib import Path

from omnigent.permission_tiers.host import (
    HOST_PERMISSION_STATE_FILE,
    HostPermissionState,
    write_host_permission_state,
)


def ready_permission_state(bridge_dir: Path, session_id: str) -> None:
    """Model the runner's ready ask-tier cache without bypassing policy evaluation."""
    write_host_permission_state(
        bridge_dir / HOST_PERMISSION_STATE_FILE,
        HostPermissionState(session_id, "test-host", str(bridge_dir), "ask", 1, "ready"),
    )
