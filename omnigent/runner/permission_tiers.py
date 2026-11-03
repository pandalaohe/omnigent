"""Runner-owned permission snapshot and one-call execution leases."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

from omnigent.permission_tiers import (
    NativeCallBinding,
    NativeInputSnapshot,
    NativeSupport,
    NativeSupportStatus,
    PermissionTier,
    TierDecision,
    Verdict,
    load_rules,
)
from omnigent.permission_tiers.host import (
    HOST_PERMISSION_STATE_FILE,
    HostPermissionState,
    evaluate_host_tool,
    read_host_permission_state,
    trusted_permission_control_paths,
    write_host_permission_state,
)
from omnigent.runner.pi_scopes import (
    PiOriginReceipt,
    PiScopeAdmission,
    PiScopePrincipal,
    PiScopeRecord,
    PiScopeRegistry,
)
from omnigent.runner.session_init_protocol import (
    RunnerSessionInitEnvelope,
    RunnerSessionInitSnapshot,
)
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

    def __init__(self, *, runner_workspace: Path | None = None) -> None:
        # Shared-runner children may omit their own launch directory.
        self._runner_workspace = str(runner_workspace.resolve()) if runner_workspace else None
        self._states: dict[str, HostPermissionState] = {}
        self._paths: dict[str, Path] = {}
        self._harnesses: dict[str, str] = {}
        self._leases: dict[str, dict[str, PermissionLease]] = {}
        self._inspected: dict[
            str, dict[str, tuple[BoundToolCall, dict[str, object], bool, TierDecision]]
        ] = {}
        self._finished: dict[str, set[str]] = {}
        self._pi_control_paths: dict[str, set[str]] = {}
        self.pi_scopes = PiScopeRegistry(PERMISSION_RUNTIME_ID)

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

    def _workspace(self, snapshot: RunnerSessionInitSnapshot) -> str | None:
        if snapshot.worktree and not snapshot.workspace:
            raise ValueError("Permission-tier worktree requires an explicit launch workspace")
        return (snapshot.worktree or snapshot.workspace or "").strip() or self._runner_workspace

    def fence_start(self, envelope: RunnerSessionInitEnvelope | None, harness: str) -> None:
        """Place a deny-safe cache before any tiered harness can launch."""
        if harness not in TIER_HARNESSES:
            if envelope is not None:
                self.pi_scopes.invalidate(envelope.session_id)
                self._harnesses.pop(envelope.session_id, None)
            return
        if envelope is None or not (envelope.snapshot.host_id or envelope.snapshot.runner_id):
            raise ValueError("Permission-tier startup requires a trusted host snapshot")
        if self._leases.get(envelope.session_id):
            raise ValueError("Permission-tier reinitialization requires idle tool execution")
        snapshot = envelope.snapshot
        host_id = permission_execution_host_id(
            host_id=snapshot.host_id, runner_id=snapshot.runner_id
        )
        workspace = self._workspace(snapshot)
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
        self._pi_control_paths.pop(envelope.session_id, None)
        self.pi_scopes.invalidate(envelope.session_id)

    def install(
        self, envelope: RunnerSessionInitEnvelope | None, harness: str
    ) -> HostPermissionState | None:
        """Validate server state and local rules before confirming the launch."""
        if harness not in TIER_HARNESSES:
            if envelope is not None:
                self.pi_scopes.invalidate(envelope.session_id)
                self._harnesses.pop(envelope.session_id, None)
            return None
        if envelope is None:
            raise ValueError("A permission-tier session requires a current server envelope")
        snapshot = envelope.snapshot
        host_id = permission_execution_host_id(
            host_id=snapshot.host_id, runner_id=snapshot.runner_id
        )
        workspace = self._workspace(snapshot)
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
        confirm_scope = (
            harness in ("pi", "pi-native")
            and confirmed.tier != "full"
            and self.pi_scope_protocol_active(envelope.session_id)
        )
        if confirm_scope:
            parent = self.pi_scopes.parent_scope(envelope.session_id)
            if parent.native_classifier is None or not self.acknowledge_pi_scope(
                envelope.session_id, parent.admission.credential
            ):
                raise ValueError("Pi scope startup registration is incomplete")
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
            self.pi_scopes.invalidate(envelope.session_id)
        self._states[envelope.session_id] = state
        self._paths[envelope.session_id] = path
        self._harnesses[envelope.session_id] = harness
        if confirm_scope:
            self.pi_scopes.confirm_launch(envelope.session_id)
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

    def observation_harness(self, session_id: str) -> str | None:
        """Read the already installed state owner; never load an agent/config."""
        return self._harnesses.get(session_id)

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

    def pi_scope_protocol_active(self, session_id: str) -> bool:
        return self._harnesses.get(session_id) in ("pi", "pi-native") and self.pi_scopes.enabled(
            session_id
        )

    def pi_execution_kind(
        self, session_id: str, receipt: object, *, legacy_local_execution: bool
    ) -> bool:
        """Adapters derive v2 execution placement from the minted receipt."""
        if not self.pi_scope_protocol_active(session_id):
            return legacy_local_execution
        return self.pi_scopes.resolve(session_id, receipt).local_execution

    def begin_pi_scope_launch(
        self, session_id: str, *, native_guard: bool = False
    ) -> PiScopeAdmission:
        """Called by the trusted bootstrap, never by a tool/event payload."""
        state = self.state(session_id)
        if (
            state is None
            or state.status not in ("ready", "blocked")
            or self._harnesses.get(session_id) not in ("pi", "pi-native")
        ):
            raise ValueError("Pi scope launch unavailable")
        if self._leases.get(session_id):
            raise ValueError("Pi scope rebind requires idle execution")
        self.pi_scopes.begin_launch(session_id, preparing=state.status == "blocked")
        self._inspected.pop(session_id, None)
        self._finished.pop(session_id, None)
        return self._open_pi_scope(session_id, origin="parent", native_guard=native_guard)

    def open_pi_child_scope(self, session_id: str) -> PiScopeAdmission:
        """Allocate an independent loader installation from the host bootstrap."""
        return self._open_pi_scope(session_id, origin="child")

    def _open_pi_scope(
        self,
        session_id: str,
        *,
        origin: Literal["parent", "child"],
        native_guard: bool = False,
    ) -> PiScopeAdmission:
        state = self.state(session_id)
        if state is None or self._harnesses.get(session_id) not in ("pi", "pi-native"):
            raise ValueError("Pi scope launch unavailable")
        from omnigent.permission_tiers.pi_tools import read_pi_tool_expectation

        expected = read_pi_tool_expectation(self.path(session_id).parent)
        if (
            expected.get("session_id") != session_id
            or expected.get("harness") != self._harnesses[session_id]
        ):
            raise ValueError("Pi enforcement belongs to another launch")
        if native_guard:
            guard = (
                Path(__file__).resolve().parent.parent
                / "resources/pi_native/omnigent_pi_child_extension.js"
            )
            expected = {
                **expected,
                "extension_path": str(guard),
                "extension_sha256": hashlib.sha256(guard.read_bytes()).hexdigest(),
            }
        return self.pi_scopes.open_scope(
            state,
            self.path(session_id).parent,
            expected,
            origin=origin,
            installation_id=uuid4().hex,
            registry_generation=uuid4().hex,
        )

    def acknowledge_pi_scope(self, session_id: str, credential: object) -> bool:
        """Retry incomplete publication; an existing expectation alone is not readiness."""
        from omnigent.permission_tiers.pi_tools import read_pi_tool_registry

        record = self.pi_scopes.pending(session_id, credential)
        state = self.state(session_id)
        if state is None or not (
            state.status == "ready"
            or (state.status == "blocked" and self.pi_scopes.preparing(session_id))
        ):
            return False
        registry = read_pi_tool_registry(
            record.admission.registry_dir, state, expected_binding=record.expected
        )
        if (
            registry.reason
            or registry.invalid_tools
            or not registry.registration_digest
            or (
                record.registration_digest is not None
                and record.registration_digest != registry.registration_digest
            )
        ):
            if record.status == "ready":
                self.revoke_pi_scope(record.admission.principal)
            return False
        record.registration_digest = registry.registration_digest
        record.status = "ready"
        return True

    def pi_origin_receipt(
        self,
        session_id: str,
        credential: object,
        *,
        call_id: str,
        name: str,
        arguments: dict[str, object],
        local_execution: bool,
    ) -> PiOriginReceipt:
        """Mint only after transport authentication; classification belongs to the adapter."""
        record = self.pi_scopes.authenticate(session_id, credential)
        state = self.state(session_id)
        if state is None:
            raise ValueError("Permission state unavailable")
        return self.pi_scopes.mint(
            record,
            state,
            call_id=call_id,
            name=name,
            arguments=arguments,
            local_execution=local_execution,
        )

    def install_pi_native_classifier(
        self,
        principal: PiScopePrincipal,
        classifier: Callable[[str, NativeCallBinding, Mapping[str, object]], NativeSupport],
    ) -> None:
        """Trusted bootstrap API; a tool or event cannot install a native attestor."""
        record = self.pi_scopes.record(principal)
        if record.status == "revoked" or not callable(classifier):
            raise ValueError("Native bootstrap is unavailable")
        if record.native_classifier is not None and record.native_classifier is not classifier:
            self.revoke_pi_scope(principal)
            raise ValueError("Native bootstrap changed; relaunch required")
        record.native_classifier = classifier

    def _native_support(
        self,
        session_id: str,
        token: object,
        arguments: Mapping[str, object],
    ) -> tuple[NativeCallBinding, NativeSupport] | None:
        if token is None:
            return None
        receipt = self.pi_scopes.resolve(session_id, token)
        record = self.pi_scopes.record(receipt.principal)
        support = self.pi_scopes.classify_native(
            record, receipt.binding.call_id, receipt.native_binding, arguments
        )
        try:
            if support != receipt.native_support:
                raise ValueError("Native support changed")
            if support.status != NativeSupportStatus.FAILED:
                self._verify_native_inputs(session_id, support)
        except (OSError, ValueError, TypeError, RuntimeError):
            support = NativeSupport(
                receipt.native_binding,
                NativeSupportStatus.FAILED,
                reason="Native launch inputs or bootstrap changed; re-prove the call",
            )
        return receipt.native_binding, support

    def _verify_native_inputs(self, session_id: str, support: NativeSupport) -> None:
        from omnigent.permission_tiers.pi_tools import _read_file

        state = self.state(session_id)
        if state is None or not isinstance(support.inputs, tuple) or not support.inputs:
            raise ValueError("Native input proof unavailable")
        workspace = Path(state.workspace).resolve(strict=True)
        roots = [Path.home() / ".pi", Path.home() / ".agents", self.path(session_id).parent]
        for parent in (workspace, *workspace.parents):
            roots.extend((parent / ".pi", parent / ".agents"))
        protected = self._pi_control_paths.setdefault(session_id, set())
        for item in support.inputs:
            if not isinstance(item, NativeInputSnapshot):
                raise ValueError("Malformed native input proof")
            path = Path(item.path)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("Native input path is not canonical")
            if path.is_relative_to(workspace) and not any(
                path.is_relative_to(root) for root in roots
            ):
                raise ValueError("Native input is model-writable")
            if item.sha256 is None:
                if item.resolved_path is not None:
                    raise ValueError("Malformed absent input proof")
                try:
                    path.lstat()
                except FileNotFoundError:
                    protected.add(str(path))
                    continue
                raise ValueError("Native input appeared")
            resolved = Path(item.resolved_path) if isinstance(item.resolved_path, str) else None
            if resolved is None or not resolved.is_absolute():
                raise ValueError("Native input target is missing")
            for candidate in (path, resolved):
                if candidate.is_relative_to(workspace) and not any(
                    candidate.is_relative_to(root) for root in roots
                ):
                    raise ValueError("Native input is model-writable")
            contents = _read_file(path, resolved_target=resolved, require_nofollow=True)
            if hashlib.sha256(contents).hexdigest() != item.sha256:
                raise ValueError("Native input changed")
            protected.update((str(path), str(resolved)))

    def revoke_pi_scope(self, principal: PiScopePrincipal) -> None:
        """Seal before acknowledgement; a delayed approval cannot recreate a lease."""
        for call_id in self.pi_scopes.revoke(principal):
            root = principal.root_session_id
            self._inspected.get(root, {}).pop(call_id, None)
            lease = self._leases.get(root, {}).get(call_id)
            if lease is not None and lease.operation_id is None:
                self._leases[root].pop(call_id)
                self._finished.setdefault(root, set()).add(call_id)

    def pi_settlement_available(self, session_id: str, token: object) -> bool:
        try:
            receipt = self.pi_scopes.resolve(session_id, token, settlement=True)
        except ValueError:
            return False
        call_id = receipt.binding.call_id
        lease = self._leases.get(session_id, {}).get(call_id)
        return (lease is not None and lease.binding == receipt.binding) or (
            call_id in self._finished.get(session_id, set())
        )

    def pi_operation_id(
        self,
        session_id: str,
        operation_id: str,
        permission: object,
        *,
        name: object,
        arguments: object,
    ) -> str:
        """A retained MCP result belongs to one scoped call, including cache hits."""
        metadata = permission if isinstance(permission, Mapping) else {}
        token = metadata.get("pi_origin_receipt")
        state = self.state(session_id)
        if token is None and (
            not self.pi_scope_protocol_active(session_id)
            or (state is not None and state.tier == "full")
        ):
            return operation_id
        receipt = self.pi_scopes.resolve(session_id, token, settlement=True)
        if (
            not isinstance(arguments, Mapping)
            or not isinstance(name, str)
            or metadata.get("call_id") != receipt.binding.call_id
            or metadata.get("revision") != receipt.binding.tier_revision
            or type(metadata.get("revision")) is not int
            or metadata.get("arguments_digest") != receipt.binding.arguments_digest
        ):
            raise ValueError("Pi operation binding does not match")
        actual = BoundToolCall.bind(
            session_id=session_id,
            host_id=receipt.binding.host_id,
            call_id=receipt.binding.call_id,
            name=name,
            arguments=arguments,
            tier_revision=receipt.binding.tier_revision,
        )
        if actual != receipt.binding:
            raise ValueError("Pi operation arguments changed")
        return (
            "pi:"
            + hashlib.sha256(
                (receipt.principal.scope_id + "\0" + actual.call_id + "\0" + operation_id).encode()
            ).hexdigest()
        )

    def _pi_receipt(
        self,
        session_id: str,
        token: object,
        binding: BoundToolCall,
        *,
        local_execution: bool | None = None,
        settlement: bool = False,
    ) -> PiScopeRecord | None:
        state = self.state(session_id)
        required = self.pi_scope_protocol_active(session_id) and (
            state is None or state.tier != "full"
        )
        if token is None and not required:
            return None
        receipt = self.pi_scopes.resolve(session_id, token, settlement=settlement)
        if receipt.binding != binding or (
            local_execution is not None and receipt.local_execution != local_execution
        ):
            raise ValueError("Pi origin receipt does not match this call")
        return self.pi_scopes.record(receipt.principal)

    def _control_paths(self, session_id: str, state: HostPermissionState) -> tuple[str, ...]:
        """Bind policy data and this launch's extension paths on the host."""
        harness = self._harnesses.get(session_id)
        state_path = self._paths[session_id]
        package_root = Path(__file__).resolve().parents[1]
        installed: list[Path] = [
            Path(__file__).resolve(),
            package_root / "runner" / "pi_scopes.py",
            package_root / "runner" / "pi_scope_relay.py",
        ]
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
                    package_root / "resources" / "pi_native" / "permission_tool_registry.cjs",
                    package_root / "resources" / "pi_native" / "native_support.cjs",
                    package_root / "resources" / "pi_native" / "omnigent_pi_child_extension.js",
                    package_root / "resources" / "pi_native" / "omnigent_pi_bootstrap.mjs",
                    package_root / "inner" / "pi_executor.py",
                    package_root / "inner" / "pi_harness.py",
                    Path.home() / ".pi",
                    Path.home() / ".agents",
                    Path(state.workspace) / ".pi",
                    Path(state.workspace) / ".agents",
                )
            )
            from omnigent.permission_tiers.pi_tools import read_pi_tool_registry

            protected = self._pi_control_paths.setdefault(session_id, set())
            protected.update(read_pi_tool_registry(state_path.parent, state).control_paths)
            installed.extend(Path(path) for path in sorted(protected))
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

    def _evaluate(
        self,
        session_id: str,
        state: HostPermissionState,
        name: str,
        arguments: Mapping[str, object],
        local_execution: bool,
        pi_scope: PiScopeRecord | None = None,
        native: tuple[NativeCallBinding, NativeSupport] | None = None,
    ) -> TierDecision:
        if pi_scope:
            state = replace(state, workspace=pi_scope.admission.principal.canonical_cwd)
        control_paths = self._control_paths(session_id, state)
        extensions = {}
        native_tools = {}
        if (
            state.tier != "full"
            and (local_execution or pi_scope is not None)
            and self._harnesses.get(session_id) in ("pi", "pi-native")
        ):
            from omnigent.permission_tiers.pi_tools import read_pi_tool_registry

            bridge = (
                pi_scope.admission.registry_dir if pi_scope else self._paths[session_id].parent
            )
            registry = read_pi_tool_registry(
                bridge, state, expected_binding=pi_scope.expected if pi_scope else None
            )
            if pi_scope:
                native_tools = registry.native_tools
                protected = self._pi_control_paths.setdefault(session_id, set())
                protected.update(registry.control_paths)
                control_paths = tuple(dict.fromkeys((*control_paths, *protected)))
                if (
                    registry.reason
                    or registry.invalid_tools
                    or registry.registration_digest != pi_scope.registration_digest
                ):
                    self.revoke_pi_scope(pi_scope.admission.principal)
                    return TierDecision(
                        Verdict.DENY, "Pi scope registry changed; relaunch required"
                    )
                if name not in registry.tool_names:
                    return TierDecision(Verdict.DENY, "Tool is not registered in this Pi scope")
                if name in registry.unsupported_tools:
                    return TierDecision(Verdict.DENY, "Pi inline tool source is not attested")
            if name in registry.invalid_tools:
                return TierDecision(Verdict.DENY, registry.extensions[name])
            if registry.reason:
                # Older launches have no registry protocol; never grant them extension trust.
                try:
                    required = (
                        json.loads((bridge / "permission_expected.json").read_text()).get(
                            "tool_registry_version"
                        )
                        == 1
                    )
                except (OSError, ValueError, TypeError, AttributeError):
                    required = True
                if required:
                    return TierDecision(Verdict.DENY, registry.reason)
            extensions = registry.extensions
        return evaluate_host_tool(
            state,
            name,
            arguments,
            control_paths=control_paths,
            extension_tools=extensions,
            native_tools=native_tools,
            native_binding=native[0] if native is not None else None,
            native_support=native[1] if native is not None else None,
            local_execution=local_execution,
        )

    def inspect(
        self,
        session_id: str,
        *,
        call_id: str,
        name: str,
        arguments: Mapping[str, object],
        expected_revision: int,
        local_execution: bool = True,
        origin_receipt: object = None,
        execution_operation_id: object = None,
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
        pi_scope = self._pi_receipt(
            session_id, origin_receipt, binding, local_execution=local_execution
        )
        if call_id in self._finished.get(session_id, set()):
            raise ValueError("Tool call already finished")
        if execution_operation_id is not None:
            lease = self._leases.get(session_id, {}).get(call_id)
            if (
                pi_scope is None
                or not isinstance(execution_operation_id, str)
                or lease is None
                or lease.operation_id != execution_operation_id
                or "rpc" not in lease.steps
                or not self.matches_authorized_call(
                    session_id,
                    call_id=call_id,
                    revision=expected_revision,
                    arguments_digest=binding.arguments_digest,
                    name=name,
                    arguments=arguments,
                    origin_receipt=origin_receipt,
                )
            ):
                raise ValueError("Pi MCP execution continuation is unavailable")
            return binding, TierDecision(Verdict.ALLOW)
        if call_id in self._leases.get(session_id, {}):
            raise ValueError("Tool call is already executing")
        saved_arguments = json.loads(
            json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"), allow_nan=False)
        )
        native = self._native_support(session_id, origin_receipt, arguments)
        decision = self._evaluate(
            session_id, state, name, arguments, local_execution, pi_scope, native
        )
        inspected = self._inspected.setdefault(session_id, {})
        prior = inspected.get(call_id)
        if prior is not None and prior[0] != binding:
            raise ValueError("Tool call binding changed")
        if decision.action != Verdict.DENY:
            inspected[call_id] = (binding, saved_arguments, local_execution, decision)
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
        origin_receipt: object = None,
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
            pi_scope = self._pi_receipt(
                session_id, origin_receipt, binding, local_execution=inspected[2]
            )
            refreshed = self._evaluate(
                session_id,
                state,
                name,
                inspected[1],
                inspected[2],
                pi_scope,
                self._native_support(session_id, origin_receipt, inspected[1]),
            )
        except (OSError, ValueError, TypeError, KeyError):
            return False
        if refreshed.action == Verdict.DENY or refreshed != inspected[3]:
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
        origin_receipt: object = None,
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
        try:
            inspected = self._inspected.get(session_id, {}).get(call_id)
            if inspected is None or inspected[0] != actual:
                return False
            self._pi_receipt(session_id, origin_receipt, actual, local_execution=inspected[2])
            native = self._native_support(session_id, origin_receipt, arguments)
            if (
                state.tier != "full"
                and native is not None
                and native[1].status == NativeSupportStatus.FAILED
            ):
                return False
        except ValueError:
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
        origin_receipt: object = None,
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
        try:
            inspected = self._inspected.get(session_id, {}).get(call_id)
            if inspected is None or inspected[0] != actual:
                return False
            self._pi_receipt(session_id, origin_receipt, actual, local_execution=inspected[2])
            native = self._native_support(session_id, origin_receipt, arguments)
            if (
                state.tier != "full"
                and native is not None
                and native[1].status == NativeSupportStatus.FAILED
            ):
                return False
        except ValueError:
            return False
        return actual == lease.binding

    def finish(
        self,
        session_id: str,
        *,
        call_id: object,
        revision: object,
        origin_receipt: object = None,
    ) -> bool:
        if not isinstance(call_id, str) or type(revision) is not int:
            return False
        lease = self._leases.get(session_id, {}).get(call_id)
        state = self.state(session_id)
        if origin_receipt is not None or (
            self.pi_scope_protocol_active(session_id) and (state is None or state.tier != "full")
        ):
            try:
                receipt = self.pi_scopes.resolve(session_id, origin_receipt, settlement=True)
                if receipt.binding.call_id != call_id or receipt.binding.tier_revision != revision:
                    return False
                if call_id in self._finished.get(session_id, set()):
                    return True
                if lease is None or lease.binding != receipt.binding:
                    return False
            except ValueError:
                return False
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
        if state.tier == "full" and tier != "full" and self.pi_scope_protocol_active(session_id):
            self.pi_scopes.invalidate(session_id)
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
