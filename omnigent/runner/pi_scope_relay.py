"""Authenticate scoped Pi relay callers before decoding their event payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnigent.runner.pi_scopes import PiScopeRecord
from omnigent.server.permission_tiers import BoundToolCall

if TYPE_CHECKING:
    from omnigent.runner.permission_tiers import PermissionTierRuntime


class PiScopeRelay:
    def __init__(self, runtime: PermissionTierRuntime, session_id: str) -> None:
        self.runtime = runtime
        self.session_id = session_id

    def legacy_allowed(self) -> bool:
        if not self.runtime.pi_scope_protocol_active(self.session_id):
            return True
        state = self.runtime.state(self.session_id)
        return state is not None and state.status == "ready" and state.tier == "full"

    def authenticate(self, authorization: str | None, path: str) -> PiScopeRecord:
        if path not in (
            "/policies/evaluate",
            "/permission-tiers/control",
            "/permission-tiers/state",
        ):
            raise ValueError("Pi scope cannot access this relay operation")
        if not authorization or not authorization.startswith("Bearer "):
            raise ValueError("Pi scope credential missing")
        return self.runtime.pi_scopes.authenticate(
            self.session_id,
            authorization.removeprefix("Bearer "),
            settlement=path != "/permission-tiers/state",
        )

    async def prepare(
        self, record: PiScopeRecord, path: str, payload: dict[str, object]
    ) -> dict[str, object]:
        """Runs on the runner loop, serialized with inspection and revocation."""
        self.runtime.pi_scopes.record(record.admission.principal)
        if path == "/permission-tiers/state":
            return {}
        if path == "/permission-tiers/control":
            # Only the server may approve a tool. A scoped caller can start or
            # settle its own existing lease, never approve its own ASK.
            if payload.get("type") not in ("permission_tier_finish", "permission_tier_execute"):
                raise ValueError("Pi scope cannot authorize its own tool calls")
            self._bound_call(
                record, payload, settlement=payload["type"] == "permission_tier_finish"
            )
            return payload
        event = payload.get("event")
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            raise ValueError("Malformed scoped Pi policy event")
        data = event["data"]
        if event.get("type") == "PHASE_TOOL_RESULT":
            self._bound_call(
                record,
                {**data, "pi_origin_receipt": payload.get("pi_origin_receipt")},
                settlement=True,
            )
            return payload
        if event.get("type") != "PHASE_TOOL_CALL":
            raise ValueError("Pi scope cannot post parent lifecycle events")
        name, arguments, call_id = data.get("name"), data.get("arguments"), data.get("call_id")
        if (
            not isinstance(name, str)
            or not isinstance(arguments, dict)
            or not isinstance(call_id, str)
        ):
            raise ValueError("Malformed scoped Pi tool call")
        receipt = self.runtime.pi_origin_receipt(
            self.session_id,
            record.admission.credential,
            call_id=call_id,
            name=name,
            arguments=arguments,
            local_execution=True,
        )
        return {
            **payload,
            "pi_origin_receipt": receipt.token,
            "event": {**event, "data": {**data, "call_id": receipt.binding.call_id}},
        }

    def _bound_call(
        self, record: PiScopeRecord, payload: dict[str, object], *, settlement: bool
    ) -> None:
        receipt = self.runtime.pi_scopes.resolve(
            self.session_id, payload.get("pi_origin_receipt"), settlement=settlement
        )
        revision = payload.get("expected_revision", payload.get("permission_tier_revision"))
        if (
            receipt.principal != record.admission.principal
            or payload.get("call_id") != receipt.binding.call_id
            or revision != receipt.binding.tier_revision
            or type(revision) is not int
        ):
            raise ValueError("Pi settlement is not bound to this scope and call")
        if not self.runtime.pi_settlement_available(self.session_id, receipt.token):
            raise ValueError("Pi settlement requires a previously authorized call")
        if "arguments" in payload:
            arguments, name = payload.get("arguments"), payload.get("name")
            if not isinstance(arguments, dict) or not isinstance(name, str):
                raise ValueError("Malformed Pi settlement")
            actual = BoundToolCall.bind(
                session_id=self.session_id,
                host_id=receipt.binding.host_id,
                call_id=receipt.binding.call_id,
                name=name,
                arguments=arguments,
                tier_revision=revision,
            )
            if actual != receipt.binding:
                raise ValueError("Pi settlement arguments changed")
