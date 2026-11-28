"""Trusted local permission cache shared by managed native tool adapters."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from omnigent._platform import windows_os_env_shell_path
from omnigent.permission_tiers import (
    NativeCallBinding,
    NativeSupport,
    PermissionTier,
    TierDecision,
    ToolIntent,
    ToolKind,
    TrustedHostContext,
    Verdict,
    _under,
    adapt_tool_call,
    decide_tier,
    load_rules,
    normalize_permission_tier,
    resolve_local_path,
)

HOST_PERMISSION_STATE_FILE = "permission_state.json"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostPermissionState:
    session_id: str
    host_id: str
    workspace: str
    tier: PermissionTier
    revision: int
    status: Literal["ready", "switching", "blocked"]
    transaction_id: str | None = None
    cwd: str | None = None

    def __post_init__(self) -> None:
        for value in (self.session_id, self.host_id, self.workspace):
            if not isinstance(value, str) or not value:
                raise ValueError("Incomplete trusted permission snapshot")
        if self.cwd is not None and (not isinstance(self.cwd, str) or not self.cwd):
            raise ValueError("Invalid permission working directory")
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
    state: HostPermissionState,
    *,
    control_paths: tuple[str, ...],
    readable_control_subtrees: tuple[str, ...] = (),
) -> TrustedHostContext:
    """Resolve paths on this executor's host, using its own environment."""
    platform = "windows" if sys.platform == "win32" else sys.platform
    if platform not in ("linux", "darwin", "windows"):
        raise ValueError("Unsupported permission host platform")
    environment = (
        dict(os.environ)
        if platform == "windows"
        else {
            name: value
            for name in ("SystemRoot", "SYSTEMROOT", "WINDIR", "ProgramFiles", "ProgramFiles(x86)")
            if (value := os.environ.get(name)) is not None
        }
    )
    # The current drive's cwd is known; other drive-relative paths stay denied.
    cwd = state.cwd or state.workspace
    drive_cwds = {}
    if platform == "windows":
        import ntpath

        drive_cwds[ntpath.splitdrive(cwd)[0].upper()] = cwd
    return TrustedHostContext(
        host_id=state.host_id,
        platform=platform,
        cwd=cwd,
        workspace=state.workspace,
        home=str(Path.home()),
        shell=os.environ.get("COMSPEC", "cmd")
        if platform == "windows"
        else os.environ.get("SHELL", "/bin/sh"),
        control_paths=control_paths,
        resolver=resolve_local_path,
        drive_cwds=drive_cwds,
        environment=environment,
        readable_control_subtrees=readable_control_subtrees,
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


def _log_relaxed_auto(intent: ToolIntent, decision: TierDecision) -> TierDecision:
    if decision.action == Verdict.ALLOW and decision.reason.startswith("relaxed-auto:"):
        logger.info(
            "permission_tier tier=allow reason=%s tool=%s host=%s",
            decision.reason,
            intent.name,
            intent.context.host_id,
        )
    return decision


def apply_auto_floor(
    tier: PermissionTier,
    intent: ToolIntent,
    decision: TierDecision,
    *,
    floor: Literal["narrow"] | None,
) -> TierDecision:
    """Apply the single narrow auto-tier floor after decide_tier returns ALLOW."""
    if tier != "auto" or decision.action != Verdict.ALLOW:
        return decision
    if floor is None:
        return _log_relaxed_auto(intent, decision)
    windows = intent.context.platform == "windows"
    if intent.kind == ToolKind.EDIT and intent.target is not None:
        known = [
            bound
            for bound in (intent.target, intent.source)
            if bound is not None and bound.kind != "unknown"
        ]
        if known:
            from omnigent.server.child_approvals import _critical_target

            if any(_critical_target(bound.path) for bound in known):
                return TierDecision(Verdict.ASK, "Writing to a critical path requires approval")
            if intent.workspace_path is not None and any(
                not _under(bound.path, intent.workspace_path.path, windows) for bound in known
            ):
                decision = TierDecision(Verdict.ALLOW, "relaxed-auto:outside-workspace-edit")
    if intent.kind == ToolKind.EXEC:
        if intent.hidden_script:
            return TierDecision(Verdict.ASK, "Command requires user approval")
        from omnigent.server.child_approvals import auto_floor_decision

        if intent.command:
            asks, relaxed_floor = auto_floor_decision(
                intent.command,
                platform=intent.context.platform,
                opaque=intent.command_opaque,
            )
            if asks:
                return TierDecision(Verdict.ASK, "Command requires user approval")
            if relaxed_floor:
                decision = TierDecision(Verdict.ALLOW, f"relaxed-auto:{relaxed_floor}")
    return _log_relaxed_auto(intent, decision)


def evaluate_host_tool(
    state: HostPermissionState,
    name: str,
    arguments: Mapping[str, object],
    *,
    control_paths: tuple[str, ...],
    extension_tools: Mapping[str, str] | None = None,
    native_tools: Mapping[str, str] | None = None,
    native_binding: NativeCallBinding | None = None,
    native_support: NativeSupport | None = None,
    local_execution: bool = True,
    agy_delegation_status: Literal["proven", "missing", "failed"] = "missing",
    registry_tool_name: str | None = None,
    readable_control_file: tuple[str, str] | None = None,
    readable_control_subtrees: tuple[str, ...] = (),
    auto_approval_floor: bool | Literal["narrow"] = False,
    acp_bridge_tool_name: str | None = None,
) -> TierDecision:
    """Adapt with local filesystem facts immediately before requesting approval."""
    try:
        if state.status != "ready":
            return TierDecision(Verdict.DENY, "Permission state is blocked or switching")
        if state.tier == "full":
            return TierDecision(Verdict.ALLOW)
        context = local_host_context(state, control_paths=control_paths)
        if context.platform == "windows" and local_execution:
            if name == "sys_os_shell":
                context = replace(context, shell=windows_os_env_shell_path())
            elif name == "bash":
                context = replace(context, shell="bash")
            elif name == "powershell":
                context = replace(context, shell="pwsh")
        if extension_tools is not None:
            context = replace(context, extension_tools=extension_tools)
        context = replace(
            context,
            native_tools=native_tools or {},
            native_binding=native_binding,
            native_support=native_support,
            local_execution=local_execution,
            agy_delegation_status=agy_delegation_status,
            registry_tool_name=registry_tool_name,
            acp_bridge_tool_name=acp_bridge_tool_name,
            readable_control_file=readable_control_file,
            readable_control_subtrees=readable_control_subtrees,
        )
        intent = adapt_tool_call(name, arguments, context)
        decision = decide_tier(state.tier, intent, load_rules())
        floor_mode: Literal["narrow"] | None = "narrow" if auto_approval_floor else None
        return apply_auto_floor(state.tier, intent, decision, floor=floor_mode)
    except Exception as exc:  # noqa: BLE001 - failures never become execution permission
        return TierDecision(Verdict.DENY, f"Permission check unavailable: {type(exc).__name__}")
