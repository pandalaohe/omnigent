"""Authenticate scoped Pi relay callers before decoding their event payloads."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from omnigent.permission_tiers.pi_private import public_pi_value
from omnigent.permission_tiers.pi_tools import read_pi_tool_registry
from omnigent.runner.pi_bootstrap import PiBootstrapBroker
from omnigent.runner.pi_scopes import PiScopeRecord
from omnigent.server.permission_tiers import BoundToolCall

if TYPE_CHECKING:
    from omnigent.runner.permission_tiers import PermissionTierRuntime


@dataclass(frozen=True)
class PiMcpExecution:
    binding: BoundToolCall
    operation_id: str
    retained_operation_id: str
    origin_receipt: str = field(repr=False)

    def proxy_params(self) -> dict[str, str]:
        return {
            "_omnigent_permission_call_id": self.binding.call_id,
            "_omnigent_pi_origin_receipt": self.origin_receipt,
        }


_mcp_execution: ContextVar[PiMcpExecution | None] = ContextVar("pi_mcp_execution", default=None)


def current_pi_mcp_execution(
    session_id: str, name: str, arguments: dict[str, object]
) -> PiMcpExecution | None:
    execution = _mcp_execution.get()
    if execution is not None:
        actual = BoundToolCall.bind(
            session_id=session_id,
            host_id=execution.binding.host_id,
            call_id=execution.binding.call_id,
            name=name,
            arguments=arguments,
            tier_revision=execution.binding.tier_revision,
        )
        if actual != execution.binding:
            raise ValueError("Pi MCP dispatch does not match its scoped call")
    return execution


class PiScopeRelay:
    def __init__(self, runtime: PermissionTierRuntime, session_id: str) -> None:
        self.runtime = runtime
        self.session_id = session_id
        self.bootstrap = PiBootstrapBroker(runtime, session_id)

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
            "/pi/rpc",
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
        state = self.runtime.state(self.session_id)
        if state is None:
            raise ValueError("Pi scope state unavailable")
        registry = read_pi_tool_registry(
            record.admission.registry_dir, state, expected_binding=record.expected
        )
        receipt = self.runtime.pi_origin_receipt(
            self.session_id,
            record.admission.credential,
            call_id=call_id,
            name=name,
            arguments=arguments,
            local_execution=name not in registry.bridge_tools,
        )
        return {
            **payload,
            "pi_origin_receipt": receipt.token,
            "event": {**event, "data": {**data, "call_id": receipt.binding.call_id}},
        }

    async def execute_rpc(
        self,
        authorization: str | None,
        opaque_payload: object,
        tool_executor: Callable[[str, dict[str, object]], Awaitable[object]],
        tool_names: set[str],
    ) -> dict[str, object]:
        """Shared native/TCP entry; authentication precedes inner JSON decoding."""
        record = self.authenticate(authorization, "/pi/rpc")
        if (
            record.status != "ready"
            or not isinstance(opaque_payload, str)
            or len(opaque_payload) > 1024 * 1024
        ):
            raise ValueError("Pi RPC payload unavailable")
        payload = json.loads(opaque_payload)
        if not isinstance(payload, dict) or set(payload) - {
            "call_id",
            "expected_revision",
            "pi_origin_receipt",
            "arguments_digest",
            "name",
            "arguments",
            "operation_id",
            "step",
        }:
            raise ValueError("Pi RPC binding unavailable")
        self._bound_call(record, payload, settlement=False)
        receipt = self.runtime.pi_scopes.resolve(self.session_id, payload.get("pi_origin_receipt"))
        name, arguments = payload.get("name"), payload.get("arguments")
        if (
            receipt.local_execution
            or not isinstance(name, str)
            or name not in tool_names
            or not isinstance(arguments, dict)
        ):
            raise ValueError("Pi RPC tool unavailable")
        if not self.runtime.verify_execute(
            self.session_id,
            call_id=payload.get("call_id"),
            revision=payload.get("expected_revision"),
            arguments_digest=payload.get("arguments_digest"),
            name=name,
            arguments=arguments,
            operation_id=payload.get("operation_id"),
            step="rpc",
            origin_receipt=payload.get("pi_origin_receipt"),
        ):
            raise ValueError("Pi RPC execution unavailable")
        operation_id = cast(str, payload["operation_id"])
        binding = receipt.binding
        execution = PiMcpExecution(
            binding,
            operation_id,
            self.runtime.pi_operation_id(
                self.session_id,
                operation_id,
                {
                    "call_id": binding.call_id,
                    "revision": binding.tier_revision,
                    "arguments_digest": binding.arguments_digest,
                    "pi_origin_receipt": receipt.token,
                },
                name=name,
                arguments=arguments,
            ),
            receipt.token,
        )
        token = _mcp_execution.set(execution)
        try:
            # No scheduling point between the live execution fence and dispatch entry.
            result = await tool_executor(name, arguments)
        finally:
            _mcp_execution.reset(token)
        projected = public_pi_value(result)
        return {"result": projected}

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
