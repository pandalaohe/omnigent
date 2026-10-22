"""Runner-owned permission snapshot and one-call execution leases."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from omnigent.permission_tiers import PermissionTier, TierDecision, Verdict, load_rules
from omnigent.permission_tiers.host import (
    HOST_PERMISSION_STATE_FILE,
    HostPermissionState,
    evaluate_host_tool,
    read_host_permission_state,
    trusted_permission_control_paths,
    write_host_permission_state,
)
from omnigent.runner.session_init_protocol import RunnerSessionInitEnvelope
from omnigent.server.permission_tiers import (
    PERMISSION_CAPABILITY_VALUE,
    PERMISSION_RUNTIME_ID_LABEL_KEY,
    PERMISSION_STATE_LABEL_KEY,
    TIER_HARNESSES,
    BoundToolCall,
    permission_execution_host_id,
    read_permission_state,
)

PERMISSION_RUNTIME_ID = uuid4().hex


class PermissionTierEpochChanged(ValueError):
    """A new runner process must invalidate approvals bound to the old one."""


@dataclass
class PermissionLease:
    binding: BoundToolCall
    operation_id: str | None = None
    steps: set[str] = field(default_factory=set)


class PermissionTierRuntime:
    """Mutable host state; only authenticated server controls can update it."""

    def __init__(self) -> None:
        self._states: dict[str, HostPermissionState] = {}
        self._paths: dict[str, Path] = {}
        self._harnesses: dict[str, str] = {}
        self._leases: dict[str, dict[str, PermissionLease]] = {}
        self._inspected: dict[str, dict[str, tuple[BoundToolCall, dict[str, object], bool]]] = {}
        self._finished: dict[str, set[str]] = {}

    @staticmethod
    def state_path(session_id: str, harness: str, labels: Mapping[str, str]) -> Path:
        if harness == "antigravity-native":
            from omnigent.harnesses.antigravity_native.bridge import (
                ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
                bridge_dir_for_bridge_id,
            )

            bridge_id = labels.get(ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY) or session_id
            return bridge_dir_for_bridge_id(bridge_id) / HOST_PERMISSION_STATE_FILE
        from omnigent.harnesses.pi_native.bridge import bridge_dir_for_session_id

        return bridge_dir_for_session_id(session_id) / HOST_PERMISSION_STATE_FILE

    def fence_start(self, envelope: RunnerSessionInitEnvelope | None, harness: str) -> None:
        """Place a deny-safe cache before any tiered harness can launch."""
        if harness not in TIER_HARNESSES:
            return
        if (
            envelope is None
            or not envelope.snapshot.workspace
            or not (envelope.snapshot.host_id or envelope.snapshot.runner_id)
        ):
            raise ValueError("Permission-tier startup requires a trusted host snapshot")
        if self._leases.get(envelope.session_id):
            raise ValueError("Permission-tier reinitialization requires idle tool execution")
        snapshot = envelope.snapshot
        host_id = permission_execution_host_id(
            host_id=snapshot.host_id, runner_id=snapshot.runner_id
        )
        workspace = snapshot.worktree or snapshot.workspace
        if not host_id or not workspace:
            raise ValueError("Incomplete permission host snapshot")
        if PERMISSION_STATE_LABEL_KEY not in snapshot.labels:
            raise ValueError("Permission-tier session requires controlled migration")
        confirmed = read_permission_state(snapshot.labels, harness=harness)
        if confirmed.status == "switching":
            raise ValueError("Permission-tier session is switching")
        if (
            confirmed.status == "ready"
            and snapshot.labels.get(PERMISSION_RUNTIME_ID_LABEL_KEY) != PERMISSION_RUNTIME_ID
        ):
            raise PermissionTierEpochChanged("Permission runtime changed")
        path = self.state_path(envelope.session_id, harness, snapshot.labels)
        fenced = HostPermissionState(
            session_id=envelope.session_id,
            host_id=host_id,
            workspace=workspace,
            tier=confirmed.tier,
            revision=confirmed.revision,
            status="blocked",
        )
        write_host_permission_state(path, fenced)
        self._states[envelope.session_id] = fenced
        self._paths[envelope.session_id] = path
        self._harnesses[envelope.session_id] = harness
        self._inspected.pop(envelope.session_id, None)
        self._leases.pop(envelope.session_id, None)

    def install(
        self, envelope: RunnerSessionInitEnvelope | None, harness: str
    ) -> HostPermissionState | None:
        """Validate server state and local rules before confirming the launch."""
        if harness not in TIER_HARNESSES:
            return None
        if envelope is None:
            raise ValueError("A permission-tier session requires a current server envelope")
        snapshot = envelope.snapshot
        host_id = permission_execution_host_id(
            host_id=snapshot.host_id, runner_id=snapshot.runner_id
        )
        workspace = snapshot.worktree or snapshot.workspace
        if not host_id or not workspace:
            raise ValueError("Permission-tier session is missing host or workspace")
        if PERMISSION_STATE_LABEL_KEY not in snapshot.labels:
            raise ValueError("Permission-tier session requires controlled migration")
        confirmed = read_permission_state(snapshot.labels, harness=harness)
        if confirmed.status != "ready" and confirmed.status != "blocked":
            raise ValueError("Permission-tier session is switching")
        if confirmed.tier != "full":
            load_rules()
        state = HostPermissionState(
            session_id=envelope.session_id,
            host_id=host_id,
            workspace=workspace,
            tier=confirmed.tier,
            revision=confirmed.revision,
            status="ready" if confirmed.status == "blocked" else confirmed.status,
        )
        path = self.state_path(envelope.session_id, harness, snapshot.labels)
        self._verify_installation(path, harness, confirmed.tier)
        write_host_permission_state(path, state)
        if read_host_permission_state(path) != state:
            raise ValueError("Permission state readback failed")
        previous = self._states.get(envelope.session_id)
        if previous is not None and (
            previous.session_id,
            previous.host_id,
            previous.workspace,
            previous.tier,
            previous.revision,
        ) != (
            state.session_id,
            state.host_id,
            state.workspace,
            state.tier,
            state.revision,
        ):
            self._leases.pop(envelope.session_id, None)
            self._inspected.pop(envelope.session_id, None)
        self._states[envelope.session_id] = state
        self._paths[envelope.session_id] = path
        self._harnesses[envelope.session_id] = harness
        return state

    @staticmethod
    def _verify_installation(path: Path, harness: str, tier: PermissionTier) -> None:
        """Do not acknowledge an unarmed native guard or missing Pi extension."""
        bridge_dir = path.parent
        if harness == "antigravity-native":
            from omnigent.harnesses.antigravity_native.bridge import agy_gemini_dir

            if (bridge_dir / "permission_tier").read_text(encoding="utf-8") != tier + "\n":
                raise ValueError("Antigravity tier file does not match")
            hooks_path = agy_gemini_dir(bridge_dir) / "config" / "hooks.json"
            hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
            guard = hooks.get("omnigent-guard") if isinstance(hooks, dict) else None
            if not isinstance(guard, dict) or guard.get("enabled") is not True:
                raise ValueError("Antigravity permission guard is not installed")
            steps = guard.get("PreToolUse")
            if not isinstance(steps, list) or not steps:
                raise ValueError("Antigravity permission guard is incomplete")
            return
        if harness == "pi-native":
            from omnigent.harnesses.pi_native.bridge import (
                extension_path,
                verify_pi_permission_installation,
            )

            extension = extension_path(bridge_dir)
            if not extension.is_file() or extension.is_symlink():
                raise ValueError("Pi permission extension is not installed")
            if not verify_pi_permission_installation(bridge_dir, tier):
                raise ValueError("Pi permission extension did not acknowledge its tools")
            return
        # The ordinary Pi executor installs a generated extension in its own
        # process-local directory. Its executor must expose a confirmation.
        from omnigent.harnesses.pi_native.bridge import verify_pi_permission_installation

        if not verify_pi_permission_installation(path.parent, tier):
            raise ValueError("Pi permission extension is not installed")

    def state(self, session_id: str) -> HostPermissionState | None:
        state = self._states.get(session_id)
        path = self._paths.get(session_id)
        if state is None or path is None:
            return None
        try:
            return state if read_host_permission_state(path) == state else None
        except (OSError, ValueError, TypeError):
            return None

    def path(self, session_id: str) -> Path:
        return self._paths[session_id]

    def _control_paths(self, session_id: str, state: HostPermissionState) -> tuple[str, ...]:
        """Bind policy data and this launch's extension paths on the host."""
        harness = self._harnesses.get(session_id)
        state_path = self._paths[session_id]
        package_root = Path(__file__).resolve().parents[1]
        installed: list[Path] = [Path(__file__).resolve()]
        if harness == "antigravity-native":
            installed.extend(
                (
                    package_root / "harnesses" / "antigravity_native" / "guard.py",
                    package_root / "harnesses" / "antigravity_native" / "bridge.py",
                    Path.home() / ".gemini" / "antigravity-cli" / "settings.json",
                )
            )
        elif harness in ("pi", "pi-native"):
            installed.extend(
                (
                    package_root / "harnesses" / "pi_native" / "bridge.py",
                    package_root / "resources" / "pi_native" / "omnigent_pi_native_extension.js",
                    package_root / "inner" / "pi_executor.py",
                    package_root / "inner" / "pi_harness.py",
                    Path.home() / ".pi" / "agent" / "settings.json",
                    Path.home() / ".pi" / "agent" / "extensions",
                    Path.home() / ".pi" / "agent" / "npm",
                    Path.home() / ".pi" / "agent" / "git",
                    Path(state.workspace) / ".pi" / "settings.json",
                )
            )
            if harness == "pi":
                from omnigent.harnesses.pi_native.bridge import PERMISSION_EXPECTED_FILE

                expected_path = state_path.parent / PERMISSION_EXPECTED_FILE
                if expected_path.is_symlink():
                    raise ValueError("Pi permission installation is symlinked")
                expected = json.loads(expected_path.read_text(encoding="utf-8"))
                if not isinstance(expected, dict):
                    raise ValueError("Pi permission installation is malformed")
                raw_extension = expected.get("extension_path")
                digest = expected.get("extension_sha256")
                if not isinstance(raw_extension, str) or not isinstance(digest, str):
                    raise ValueError("Pi permission extension is missing")
                extension = Path(raw_extension)
                if (
                    expected.get("version") != 1
                    or expected.get("session_id") != session_id
                    or expected.get("harness") != "pi"
                    or not extension.is_absolute()
                    or ".." in extension.parts
                    or extension.name != "omnigent_tools.js"
                    or not extension.parent.name.startswith("omnigent_pi_")
                    or extension.parent.is_symlink()
                    or extension.is_symlink()
                    or not extension.is_file()
                    or len(digest) != 64
                    or hashlib.sha256(extension.read_bytes()).hexdigest() != digest
                ):
                    raise ValueError("Pi permission extension is unverified")
                installed.append(extension.parent)
        else:
            raise ValueError("Unknown permission harness")
        return trusted_permission_control_paths(state_path, installed_paths=tuple(installed))

    def inspect(
        self,
        session_id: str,
        *,
        call_id: str,
        name: str,
        arguments: Mapping[str, object],
        expected_revision: int,
        local_execution: bool = True,
    ) -> tuple[BoundToolCall, TierDecision]:
        state = self.state(session_id)
        if state is None or state.status != "ready" or state.revision != expected_revision:
            raise ValueError("Permission tier is unavailable or stale")
        binding = BoundToolCall.bind(
            session_id=session_id,
            host_id=state.host_id,
            call_id=call_id,
            name=name,
            arguments=arguments,
            tier_revision=expected_revision,
        )
        if call_id in self._finished.get(session_id, set()):
            raise ValueError("Tool call already finished")
        if call_id in self._leases.get(session_id, {}):
            raise ValueError("Tool call is already executing")
        saved_arguments = json.loads(
            json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"), allow_nan=False)
        )
        control_paths = self._control_paths(session_id, state)
        decision = evaluate_host_tool(
            state,
            name if local_execution else "__omnigent_remote_tool__",
            arguments,
            control_paths=control_paths,
        )
        inspected = self._inspected.setdefault(session_id, {})
        prior = inspected.get(call_id)
        if prior is not None and prior[0] != binding:
            raise ValueError("Tool call binding changed")
        if decision.action != Verdict.DENY:
            inspected[call_id] = (binding, saved_arguments, local_execution)
        else:
            inspected.pop(call_id, None)
        return binding, decision

    def authorize(
        self,
        session_id: str,
        *,
        call_id: object,
        name: object,
        arguments_digest: object,
        expected_revision: object,
    ) -> bool:
        state = self.state(session_id)
        if (
            state is None
            or state.status != "ready"
            or type(expected_revision) is not int
            or state.revision != expected_revision
            or not isinstance(call_id, str)
            or not call_id
            or not isinstance(name, str)
            or not name
        ):
            return False
        if not isinstance(arguments_digest, str) or len(arguments_digest) != 64:
            return False
        try:
            int(arguments_digest, 16)
            binding = BoundToolCall(
                session_id=session_id,
                host_id=state.host_id,
                call_id=call_id,
                name=name,
                arguments_digest=arguments_digest,
                tier_revision=expected_revision,
            )
        except (TypeError, ValueError):
            return False
        leases = self._leases.setdefault(session_id, {})
        if call_id in self._finished.get(session_id, set()):
            return False
        inspected = self._inspected.get(session_id, {}).get(call_id)
        if inspected is None or inspected[0] != binding:
            return False
        try:
            control_paths = self._control_paths(session_id, state)
        except (OSError, ValueError, TypeError, KeyError):
            return False
        refreshed = evaluate_host_tool(
            state,
            name if inspected[2] else "__omnigent_remote_tool__",
            inspected[1],
            control_paths=control_paths,
        )
        if refreshed.action == Verdict.DENY:
            self._inspected.get(session_id, {}).pop(call_id, None)
            return False
        prior = leases.get(call_id)
        if prior is not None and prior.binding != binding:
            return False
        leases.setdefault(call_id, PermissionLease(binding))
        return True

    def verify_execute(
        self,
        session_id: str,
        *,
        call_id: object,
        revision: object,
        arguments_digest: object,
        name: str,
        arguments: Mapping[str, object],
        operation_id: object,
        step: object,
    ) -> bool:
        state = self.state(session_id)
        if (
            state is None
            or state.status != "ready"
            or not isinstance(call_id, str)
            or type(revision) is not int
            or not isinstance(arguments_digest, str)
            or not isinstance(operation_id, str)
            or not isinstance(step, str)
            or state.revision != revision
            or not operation_id
            or not step
        ):
            return False
        lease = self._leases.get(session_id, {}).get(call_id)
        if lease is None or lease.binding.arguments_digest != arguments_digest:
            return False
        try:
            actual = BoundToolCall.bind(
                session_id=session_id,
                host_id=state.host_id,
                call_id=call_id,
                name=name,
                arguments=arguments,
                tier_revision=revision,
            )
        except (TypeError, ValueError):
            return False
        if actual != lease.binding:
            return False
        if lease.operation_id is not None and lease.operation_id != operation_id:
            return False
        if step in lease.steps:
            return False
        lease.operation_id = operation_id
        lease.steps.add(step)
        return True

    def matches_authorized_call(
        self,
        session_id: str,
        *,
        call_id: object,
        revision: object,
        arguments_digest: object,
        name: object,
        arguments: object,
    ) -> bool:
        """Check a live lease without consuming its execution step."""
        state = self.state(session_id)
        if (
            state is None
            or state.status != "ready"
            or not isinstance(call_id, str)
            or type(revision) is not int
            or state.revision != revision
            or not isinstance(arguments_digest, str)
            or not isinstance(name, str)
            or not isinstance(arguments, Mapping)
        ):
            return False
        lease = self._leases.get(session_id, {}).get(call_id)
        if lease is None or lease.binding.arguments_digest != arguments_digest:
            return False
        try:
            actual = BoundToolCall.bind(
                session_id=session_id,
                host_id=state.host_id,
                call_id=call_id,
                name=name,
                arguments=arguments,
                tier_revision=revision,
            )
        except (TypeError, ValueError):
            return False
        return actual == lease.binding

    def finish(self, session_id: str, *, call_id: object, revision: object) -> bool:
        if not isinstance(call_id, str) or type(revision) is not int:
            return False
        lease = self._leases.get(session_id, {}).get(call_id)
        if lease is None or lease.binding.tier_revision != revision:
            return False
        del self._leases[session_id][call_id]
        self._inspected.get(session_id, {}).pop(call_id, None)
        self._finished.setdefault(session_id, set()).add(call_id)
        return True

    def idle(self, session_id: str) -> bool:
        return not self._leases.get(session_id)

    def prepare(
        self, session_id: str, *, expected_revision: int, transaction_id: str
    ) -> HostPermissionState:
        state = self.state(session_id)
        if (
            state is None
            or state.status != "ready"
            or state.revision != expected_revision
            or not transaction_id
            or not self.idle(session_id)
        ):
            raise ValueError("Permission state is busy or stale")
        switching = HostPermissionState(
            session_id=state.session_id,
            host_id=state.host_id,
            workspace=state.workspace,
            tier=state.tier,
            revision=state.revision,
            status="switching",
            transaction_id=transaction_id,
        )
        write_host_permission_state(self._paths[session_id], switching)
        self._states[session_id] = switching
        self._inspected.pop(session_id, None)
        return switching

    def settle(
        self,
        session_id: str,
        *,
        transaction_id: str,
        tier: PermissionTier,
        revision: int,
        status: str = "ready",
    ) -> HostPermissionState:
        state = self.state(session_id)
        if state is None or state.status != "switching" or state.transaction_id != transaction_id:
            raise ValueError("Permission transaction is stale")
        if status not in {"ready", "blocked"}:
            raise ValueError("Invalid permission settle status")
        settled = HostPermissionState(
            session_id=state.session_id,
            host_id=state.host_id,
            workspace=state.workspace,
            tier=tier,
            revision=revision,
            status=status,
            transaction_id=None if status == "ready" else transaction_id,
        )
        write_host_permission_state(self._paths[session_id], settled)
        self._states[session_id] = settled
        self._inspected.pop(session_id, None)
        return settled

    def force_block(self, session_id: str, transaction_id: str) -> None:
        state = self._states.get(session_id)
        path = self._paths.get(session_id)
        if state is None or path is None:
            return
        blocked = HostPermissionState(
            session_id=state.session_id,
            host_id=state.host_id,
            workspace=state.workspace,
            tier=state.tier,
            revision=state.revision,
            status="blocked",
            transaction_id=transaction_id,
        )
        write_host_permission_state(path, blocked)
        self._states[session_id] = blocked
        self._inspected.pop(session_id, None)


PERMISSION_TIERS_CAPABILITY = PERMISSION_CAPABILITY_VALUE


def decision_payload(decision: TierDecision) -> dict[str, object]:
    return {"verdict": decision.action.value, "reason": decision.reason}
