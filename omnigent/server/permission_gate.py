"""Mandatory tier checks outside the user-editable policy engine."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from omnigent.db.workspace_cache import WorkspaceScopedSet
from omnigent.permission_tiers import TierDecision, Verdict
from omnigent.server.permission_tiers import (
    PERMISSION_CAPABILITY_VALUE,
    TIER_HARNESSES,
    BoundToolCall,
    permission_execution_host_id,
    read_permission_state,
)

if TYPE_CHECKING:
    from fastapi import Request

    from omnigent.entities import Conversation
    from omnigent.runner.routing import RunnerRouter
    from omnigent.stores.conversation_store import ConversationStore


@dataclass(frozen=True)
class InspectedPermissionCall:
    binding: BoundToolCall | None
    decision: TierDecision

    def execution_binding(self) -> dict[str, object] | None:
        if self.binding is None:
            return None
        return {
            "call_id": self.binding.call_id,
            "revision": self.binding.tier_revision,
            "arguments_digest": self.binding.arguments_digest,
        }


def _denied(reason: str) -> InspectedPermissionCall:
    return InspectedPermissionCall(None, TierDecision(Verdict.DENY, reason))


async def inspect_permission_call(
    conversation: Conversation,
    *,
    harness: str | None,
    name: str,
    arguments: object,
    call_id: object,
    runner_router: RunnerRouter | None,
    local_execution: bool = True,
) -> InspectedPermissionCall:
    """Only the authenticated executing runner may supply filesystem facts."""
    if harness is None and any(
        key in conversation.labels
        for key in ("omnigent.pi.permission_tier", "omnigent.antigravity_native.permission_tier")
    ):
        return _denied("Permission harness unavailable")
    if harness is None or harness not in TIER_HARNESSES:
        return InspectedPermissionCall(None, TierDecision(Verdict.ALLOW))
    try:
        state = read_permission_state(conversation.labels, harness=harness)
        if state.status != "ready":
            return _denied(
                "Permission state is blocked or switching; controlled recovery required"
            )
        if state.tier == "full":
            return InspectedPermissionCall(None, TierDecision(Verdict.ALLOW))
        if not isinstance(arguments, dict) or not isinstance(call_id, str):
            return _denied("Permission check requires object arguments and a stable call id")
        binding = BoundToolCall.bind(
            session_id=conversation.id,
            host_id=permission_execution_host_id(
                host_id=conversation.host_id, runner_id=conversation.runner_id
            ),
            call_id=call_id,
            name=name,
            arguments=arguments,
            tier_revision=state.revision,
        )
        from omnigent.server.routes._sessions.helpers import _get_runner_client

        client = await _get_runner_client(
            conversation.id, runner_router, conversation=conversation
        )
        if client is None:
            return _denied("Permission checker unavailable on the execution host")
        response = await client.post(
            f"/v1/sessions/{conversation.id}/events",
            json={
                "type": "permission_tier_inspect",
                "call_id": binding.call_id,
                "name": name,
                "arguments": arguments,
                "expected_revision": state.revision,
                "local_execution": local_execution,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, dict) or any(
            raw.get(key) != expected
            for key, expected in {
                "permission_tiers_capability": PERMISSION_CAPABILITY_VALUE,
                "session_id": binding.session_id,
                "host_id": binding.host_id,
                "call_id": binding.call_id,
                "name": binding.name,
                "arguments_digest": binding.arguments_digest,
                "tier_revision": binding.tier_revision,
            }.items()
        ):
            return _denied("Execution-host permission confirmation does not match this call")
        if type(raw.get("tier_revision")) is not int:
            return _denied("Invalid execution-host permission revision")
        verdict = Verdict(raw.get("verdict"))
        reason, rule_id = raw.get("reason", ""), raw.get("rule_id")
        if not isinstance(reason, str) or (rule_id is not None and not isinstance(rule_id, str)):
            return _denied("Malformed execution-host permission verdict")
        return InspectedPermissionCall(binding, TierDecision(verdict, reason, rule_id))
    except Exception as exc:  # noqa: BLE001 - unavailable facts cannot become permission
        return _denied(f"Permission check unavailable: {type(exc).__name__}")


_active_approvals: WorkspaceScopedSet[str] = WorkspaceScopedSet()
_consumed_approvals: WorkspaceScopedSet[str] = WorkspaceScopedSet()


async def finish_permission_call(
    conversation: Conversation,
    *,
    call_id: object,
    revision: object,
    runner_router: RunnerRouter | None,
) -> bool:
    """Release only the executing host's matching completed-call lease."""
    if not isinstance(call_id, str) or not call_id or type(revision) is not int:
        return False
    try:
        from omnigent.server.routes._sessions.helpers import _get_runner_client

        client = await _get_runner_client(
            conversation.id, runner_router, conversation=conversation
        )
        if client is None:
            return False
        response = await client.post(
            f"/v1/sessions/{conversation.id}/events",
            json={
                "type": "permission_tier_finish",
                "call_id": call_id,
                "expected_revision": revision,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        body = response.json()
        return isinstance(body, dict) and body.get("finished") is True
    except Exception:  # noqa: BLE001 - retain the fence when completion is unconfirmed
        return False


async def authorize_permission_call(
    inspection: InspectedPermissionCall,
    *,
    request: Request | None,
    harness: str | None,
    arguments: dict[str, object],
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    can_approve: bool = True,
) -> TierDecision:
    """Ask for this exact call once, then revalidate state before handing off."""
    decision, binding = inspection.decision, inspection.binding
    if decision.action == Verdict.DENY or binding is None:
        return decision
    try:
        candidate = BoundToolCall.bind(
            session_id=binding.session_id,
            host_id=binding.host_id,
            call_id=binding.call_id,
            name=binding.name,
            arguments=arguments,
            tier_revision=binding.tier_revision,
        )
        if candidate != binding:
            return TierDecision(Verdict.DENY, "Tool arguments changed after permission inspection")
    except (TypeError, ValueError):
        return TierDecision(Verdict.DENY, "Malformed tool authorization")
    if decision.action == Verdict.ASK:
        if not can_approve:
            return TierDecision(
                Verdict.DENY, "Session edit permission is required to approve tools"
            )
        approval_id = binding.approval_id()
        if approval_id in _active_approvals or approval_id in _consumed_approvals:
            return TierDecision(Verdict.DENY, "This tool approval is already pending or consumed")
        from omnigent.server.routes._sessions.orchestration import (
            _publish_and_wait_for_harness_elicitation,
        )
        from omnigent.server.schemas import ElicitationRequestParams

        _active_approvals.add(approval_id)
        try:
            result = await _publish_and_wait_for_harness_elicitation(
                request,
                session_id=binding.session_id,
                elicitation_id=approval_id,
                params=ElicitationRequestParams(
                    mode="form",
                    message=decision.reason or "Approve this tool call once?",
                    requestedSchema=None,
                    url=None,
                    phase="permission_tier",
                    policy_name="permission_tier_once",
                    tool_name=binding.name,
                    content_preview=json.dumps(arguments, ensure_ascii=False)[:4096],
                    permission_tier_revision=binding.tier_revision,
                ),
                timeout_s=86400.0,
                conversation_store=conversation_store,
            )
            if result is None or result.action != "accept":
                return TierDecision(Verdict.DENY, "This tool call was not approved")
            _consumed_approvals.add(approval_id)
        finally:
            _active_approvals.discard(approval_id)
    try:
        current = await asyncio.to_thread(conversation_store.get_conversation, binding.session_id)
        if (
            current is None
            or permission_execution_host_id(host_id=current.host_id, runner_id=current.runner_id)
            != binding.host_id
            or harness is None
            or harness not in TIER_HARNESSES
        ):
            raise ValueError("Session host changed")
        state = read_permission_state(current.labels, harness=harness)
        if state.status != "ready" or state.revision != binding.tier_revision:
            raise ValueError("Permission revision changed")
        from omnigent.server.routes._sessions.helpers import _get_runner_client

        client = await _get_runner_client(binding.session_id, runner_router, conversation=current)
        if client is None:
            raise ValueError("Execution host unavailable")
        response = await client.post(
            f"/v1/sessions/{binding.session_id}/events",
            json={
                "type": "permission_tier_authorize",
                "call_id": binding.call_id,
                "name": binding.name,
                "arguments_digest": binding.arguments_digest,
                "expected_revision": binding.tier_revision,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, dict) or raw.get("authorized") is not True:
            raise ValueError("Execution host did not authorize this call")
        if any(
            raw.get(key) != expected
            for key, expected in {
                "call_id": binding.call_id,
                "tier_revision": binding.tier_revision,
                "session_id": binding.session_id,
                "host_id": binding.host_id,
                "permission_tiers_capability": PERMISSION_CAPABILITY_VALUE,
            }.items()
        ):
            raise ValueError("Execution host authorization mismatch")
        return TierDecision(Verdict.ALLOW)
    except Exception as exc:  # noqa: BLE001 - stale approval cannot execute
        return TierDecision(
            Verdict.DENY, f"Permission authorization unavailable: {type(exc).__name__}"
        )
