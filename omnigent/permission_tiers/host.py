"""Trusted local permission cache shared by managed native tool adapters."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from omnigent.permission_tiers import (
    PermissionTier,
    TierDecision,
    TrustedHostContext,
    Verdict,
    adapt_tool_call,
    decide_tier,
    load_rules,
    normalize_permission_tier,
    resolve_local_path,
)

HOST_PERMISSION_STATE_FILE = "permission_state.json"


@dataclass(frozen=True)
class HostPermissionState:
    session_id: str
    host_id: str
    workspace: str
    tier: PermissionTier
    revision: int
    status: Literal["ready", "switching", "blocked"]
    transaction_id: str | None = None

    def __post_init__(self) -> None:
        for value in (self.session_id, self.host_id, self.workspace):
            if not isinstance(value, str) or not value:
                raise ValueError("Incomplete trusted permission snapshot")
        normalize_permission_tier(self.tier, harness="pi", legacy=False)
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Invalid permission revision")
        if self.status not in ("ready", "switching", "blocked"):
            raise ValueError("Invalid permission status")
        if self.transaction_id is not None and (
            not isinstance(self.transaction_id, str) or not self.transaction_id
        ):
            raise ValueError("Invalid permission transaction")
        if self.status == "switching" and self.transaction_id is None:
            raise ValueError("Missing permission transaction")


def read_host_permission_state(path: Path) -> HostPermissionState:
    """A cache is never inferred from launch flags or model-authored tool fields."""
    if path.is_symlink():
        raise ValueError("Refusing symlink permission state")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Permission state must be an object")
    return HostPermissionState(**raw)


def write_host_permission_state(path: Path, state: HostPermissionState) -> None:
    """Replace the cache atomically; a failed write leaves the previous fence intact."""
    if path.is_symlink():
        raise ValueError("Refusing symlink permission state")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="permission-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(asdict(state), stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise ValueError("Refusing symlink permission state")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def local_host_context(
    state: HostPermissionState, *, control_paths: tuple[str, ...]
) -> TrustedHostContext:
    """Resolve paths on this executor's host, using its own environment."""
    platform = "windows" if sys.platform == "win32" else sys.platform
    if platform not in ("linux", "darwin", "windows"):
        raise ValueError("Unsupported permission host platform")
    environment = {
        name: value
        for name in ("SystemRoot", "SYSTEMROOT", "WINDIR", "ProgramFiles", "ProgramFiles(x86)")
        if (value := os.environ.get(name)) is not None
    }
    # The current drive's cwd is known; other drive-relative paths stay denied.
    drive_cwds = {}
    if platform == "windows":
        import ntpath

        drive_cwds[ntpath.splitdrive(state.workspace)[0].upper()] = state.workspace
    return TrustedHostContext(
        host_id=state.host_id,
        platform=platform,
        cwd=state.workspace,
        workspace=state.workspace,
        home=str(Path.home()),
        shell=os.environ.get("COMSPEC", "cmd")
        if platform == "windows"
        else os.environ.get("SHELL", "/bin/sh"),
        control_paths=control_paths,
        resolver=resolve_local_path,
        drive_cwds=drive_cwds,
        environment=environment,
    )


def trusted_permission_control_paths(
    state_path: Path,
    *,
    installed_paths: tuple[Path, ...] = (),
) -> tuple[str, ...]:
    """Protect the active bridge and installed policy from tool-side edits."""
    paths = (
        state_path.parent,
        Path(__file__).resolve().parent,
        *installed_paths,
    )
    return tuple(dict.fromkeys(str(path) for path in paths))


def evaluate_host_tool(
    state: HostPermissionState,
    name: str,
    arguments: Mapping[str, object],
    *,
    control_paths: tuple[str, ...],
    extension_tools: Mapping[str, str] | None = None,
) -> TierDecision:
    """Adapt with local filesystem facts immediately before requesting approval."""
    try:
        if state.status != "ready":
            return TierDecision(Verdict.DENY, "Permission state is blocked or switching")
        if state.tier == "full":
            return TierDecision(Verdict.ALLOW)
        context = local_host_context(state, control_paths=control_paths)
        if extension_tools is not None:
            context = replace(context, extension_tools=extension_tools)
        intent = adapt_tool_call(name, arguments, context)
        return decide_tier(state.tier, intent, load_rules())
    except Exception as exc:  # noqa: BLE001 - failures never become execution permission
        return TierDecision(Verdict.DENY, f"Permission check unavailable: {type(exc).__name__}")
