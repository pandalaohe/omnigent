"""Runner-owned Pi loader principals and opaque, call-bound transport receipts."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from omnigent.permission_tiers.host import HostPermissionState
from omnigent.server.permission_tiers import BoundToolCall
from omnigent.util.atomic_json import atomic_write_json


@dataclass(frozen=True)
class PiScopePrincipal:
    root_session_id: str
    runner_epoch: str
    launch_epoch: str
    scope_id: str
    origin: Literal["parent", "child"]
    installation_id: str
    canonical_cwd: str
    registry_generation: str

    def registry_binding(self) -> dict[str, str]:
        return {
            "scope_id": self.scope_id,
            "runner_epoch": self.runner_epoch,
            "launch_epoch": self.launch_epoch,
            "origin": self.origin,
            "canonical_cwd": self.canonical_cwd,
            "registry_generation": self.registry_generation,
        }


@dataclass(frozen=True)
class PiScopeAdmission:
    principal: PiScopePrincipal
    registry_dir: Path
    credential: str = field(repr=False)


@dataclass
class PiScopeRecord:
    admission: PiScopeAdmission
    expected: dict[str, object]
    status: Literal["pending", "ready", "revoked"] = "pending"


@dataclass(frozen=True)
class PiOriginReceipt:
    principal: PiScopePrincipal
    binding: BoundToolCall
    local_execution: bool
    token: str = field(repr=False)


class PiScopeRegistry:
    """One process incarnation; bootstrap methods are never model-facing controls."""

    def __init__(self, runner_epoch: str) -> None:
        self.runner_epoch = runner_epoch
        self._launches: dict[str, str] = {}
        self._scopes: dict[str, PiScopeRecord] = {}
        self._credentials: dict[str, str] = {}
        self._receipts: dict[str, PiOriginReceipt] = {}
        self._scope_counts: dict[str, int] = {}
        self._receipt_counts: dict[str, int] = {}

    @staticmethod
    def _digest(value: object) -> str:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError("Pi scope credential or receipt unavailable")
        return hashlib.sha256(value.encode()).hexdigest()

    def enabled(self, root_session_id: str) -> bool:
        return root_session_id in self._launches

    def begin_launch(self, root_session_id: str) -> None:
        self.invalidate(root_session_id)
        old_scopes = {
            scope_id
            for scope_id, record in self._scopes.items()
            if record.admission.principal.root_session_id == root_session_id
        }
        self._scopes = {key: value for key, value in self._scopes.items() if key not in old_scopes}
        self._credentials = {
            key: value for key, value in self._credentials.items() if value not in old_scopes
        }
        self._receipts = {
            key: value
            for key, value in self._receipts.items()
            if value.principal.root_session_id != root_session_id
        }
        self._launches[root_session_id] = uuid4().hex
        self._scope_counts[root_session_id] = 0
        self._receipt_counts[root_session_id] = 0

    def invalidate(self, root_session_id: str) -> None:
        for record in self._scopes.values():
            if record.admission.principal.root_session_id == root_session_id:
                record.status = "revoked"
        # Keep the v2 requirement latched during a blocked/rebinding launch.
        if root_session_id in self._launches:
            self._launches[root_session_id] = uuid4().hex

    def open_scope(
        self,
        state: HostPermissionState,
        bridge_dir: Path,
        expected: dict[str, object],
        *,
        origin: Literal["parent", "child"],
        installation_id: str,
        registry_generation: str,
    ) -> PiScopeAdmission:
        launch_epoch = self._launches.get(state.session_id)
        if launch_epoch is None or state.status != "ready":
            raise ValueError("Pi scope launch is not ready")
        if not installation_id or not registry_generation:
            raise ValueError("Pi scope installation identity is incomplete")
        if self._scope_counts.get(state.session_id, 0) >= 4096:
            raise ValueError("Pi loader scope limit reached; relaunch required")
        principal = PiScopePrincipal(
            state.session_id,
            self.runner_epoch,
            launch_epoch,
            uuid4().hex,
            origin,
            installation_id,
            str(Path(state.workspace).resolve()),
            registry_generation,
        )
        directory = bridge_dir.resolve() / "pi-scopes" / principal.scope_id
        expectation = {
            **expected,
            **principal.registry_binding(),
            "scope_protocol_version": 2,
            "installation_id": installation_id,
            "session_id": state.session_id,
        }
        directory.mkdir(mode=0o700, parents=True)
        atomic_write_json(directory / "permission_expected.json", expectation)
        admission = PiScopeAdmission(principal, directory, secrets.token_urlsafe(32))
        self._scopes[principal.scope_id] = PiScopeRecord(admission, expectation)
        self._credentials[self._digest(admission.credential)] = principal.scope_id
        self._scope_counts[state.session_id] = self._scope_counts.get(state.session_id, 0) + 1
        return admission

    def record(self, principal: PiScopePrincipal) -> PiScopeRecord:
        record = self._scopes.get(principal.scope_id)
        if (
            record is None
            or record.admission.principal != principal
            or principal.runner_epoch != self.runner_epoch
            or self._launches.get(principal.root_session_id) != principal.launch_epoch
        ):
            raise ValueError("Pi scope installation is stale")
        return record

    def authenticate(
        self, root_session_id: str, credential: object, *, settlement: bool = False
    ) -> PiScopeRecord:
        scope_id = self._credentials.get(self._digest(credential))
        record = self._scopes.get(scope_id) if scope_id is not None else None
        if record is None:
            raise ValueError("Pi scope credential unavailable")
        principal = record.admission.principal
        if principal.root_session_id != root_session_id:
            raise ValueError("Pi scope belongs to another root")
        self.record(principal)
        if record.status != "ready" and not (settlement and record.status == "revoked"):
            raise ValueError("Pi scope is not ready")
        return record

    def pending(self, root_session_id: str, credential: object) -> PiScopeRecord:
        scope_id = self._credentials.get(self._digest(credential))
        record = self._scopes.get(scope_id) if scope_id is not None else None
        if record is None or record.admission.principal.root_session_id != root_session_id:
            raise ValueError("Pi scope credential unavailable")
        self.record(record.admission.principal)
        if record.status == "revoked":
            raise ValueError("Pi scope is revoked")
        return record

    def mint(
        self,
        record: PiScopeRecord,
        state: HostPermissionState,
        *,
        call_id: str,
        name: str,
        arguments: dict[str, object],
        local_execution: bool,
    ) -> PiOriginReceipt:
        principal = record.admission.principal
        self.record(principal)
        if record.status != "ready" or state.status != "ready":
            raise ValueError("Pi scope is not ready")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("Pi tool call identity is missing")
        if type(local_execution) is not bool:
            raise ValueError("Pi execution classification must come from its adapter")
        if self._receipt_counts.get(state.session_id, 0) >= 32768:
            raise ValueError("Pi transport receipt limit reached; relaunch required")
        canonical_id = (
            "pi:" + hashlib.sha256((principal.scope_id + "\0" + call_id).encode()).hexdigest()
        )
        binding = BoundToolCall.bind(
            session_id=state.session_id,
            host_id=state.host_id,
            call_id=canonical_id,
            name=name,
            arguments=arguments,
            tier_revision=state.revision,
        )
        receipt = PiOriginReceipt(principal, binding, local_execution, secrets.token_urlsafe(32))
        self._receipts[self._digest(receipt.token)] = receipt
        self._receipt_counts[state.session_id] = self._receipt_counts.get(state.session_id, 0) + 1
        return receipt

    def resolve(
        self, root_session_id: str, token: object, *, settlement: bool = False
    ) -> PiOriginReceipt:
        receipt = self._receipts.get(self._digest(token))
        if receipt is None or receipt.principal.root_session_id != root_session_id:
            raise ValueError("Pi origin receipt unavailable")
        record = self.record(receipt.principal)
        if record.status != "ready" and not (settlement and record.status == "revoked"):
            raise ValueError("Pi scope is not ready")
        return receipt

    def revoke(self, principal: PiScopePrincipal) -> tuple[str, ...]:
        record = self.record(principal)
        record.status = "revoked"
        return tuple(
            receipt.binding.call_id
            for receipt in self._receipts.values()
            if receipt.principal == principal
        )
