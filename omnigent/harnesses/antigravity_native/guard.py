"""Thin stdlib-only PreToolUse adapter for managed Antigravity sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path

from omnigent import permission_tiers
from omnigent.permission_tiers import (
    RelaxedRules,
    TrustedHostContext,
    Verdict,
    adapt_tool_call,
    decide_tier,
    load_rules,
    normalize_permission_tier,
)
from omnigent.permission_tiers.host import (
    HOST_PERMISSION_STATE_FILE,
    HostPermissionState,
    local_host_context,
    read_host_permission_state,
)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Expected a JSON object")
    return value


def _error(exc: Exception) -> dict[str, str]:
    return {"decision": "deny", "reason": f"Permission check unavailable: {type(exc).__name__}"}


def decide(
    payload: Mapping[str, object],
    tier: str,
    rules: RelaxedRules,
    context: TrustedHostContext,
) -> dict[str, str]:
    """A native `ask` means continue; force_ask is reserved for actual approval."""
    try:
        canonical = normalize_permission_tier(tier, harness="antigravity-native", legacy=True)
        call = _object(payload["toolCall"])
        name = call.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Missing tool name")
        intent = adapt_tool_call(name, _object(call["args"]), context)
        decision = decide_tier(canonical, intent, rules)
        if decision.action == Verdict.ALLOW:
            return {"decision": "ask"}
        reason = decision.reason
        if decision.rule_id:
            reason = f"[{decision.rule_id}] {reason}"
        return {
            "decision": "force_ask" if decision.action == Verdict.ASK else "deny",
            "reason": reason,
        }
    except Exception as exc:
        return _error(exc)


def _confirmed_relay(bridge_dir: Path, state: HostPermissionState) -> dict[str, object]:
    """Check server readiness as well as the local fence before any native tool."""
    if state.status != "ready":
        raise ValueError("Permission state is blocked or switching")
    relay = _relay_config(bridge_dir, state)
    confirmed = _relay_post(relay, "/permission-tiers/state", {})
    if (
        confirmed.get("permission_tiers_capability") is not True
        or confirmed.get("permission_tier") != state.tier
        or type(confirmed.get("permission_tier_revision")) is not int
        or confirmed.get("permission_tier_revision") != state.revision
        or confirmed.get("permission_tier_state") != "ready"
    ):
        raise ValueError("Server and host permission state differ")
    return relay


def _relay_config(bridge_dir: Path, state: HostPermissionState) -> dict[str, object]:
    relay_file = bridge_dir / "tool_relay.json"
    if relay_file.is_symlink():
        raise ValueError("Refusing symlink relay configuration")
    relay = _object(json.loads(relay_file.read_text(encoding="utf-8")))
    if relay.get("session_id") != state.session_id or relay.get("permission_tiers_v1") is not True:
        raise ValueError("Permission relay is not verified")
    return relay


def _relay_post(
    relay: Mapping[str, object], path: str, payload: dict[str, object]
) -> dict[str, object]:
    url, token = relay.get("url"), relay.get("token")
    if not isinstance(url, str) or not isinstance(token, str) or not token:
        raise ValueError("Permission relay unavailable")
    request = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    # Policy ASK is a server-held approval; state/control requests remain short.
    timeout = 86400 if path == "/policies/evaluate" else 5
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return _object(json.load(response))


def _native_call_id(payload: Mapping[str, object], state: HostPermissionState) -> str:
    conversation_id, step = payload.get("conversationId"), payload.get("stepIdx")
    if (
        not isinstance(conversation_id, str)
        or not conversation_id
        or type(step) is not int
        or step < 0
    ):
        raise ValueError("Native hook requires conversationId and stepIdx")
    identity = json.dumps([state.session_id, conversation_id, step]).encode()
    return "agy-hook:" + hashlib.sha256(identity).hexdigest()


def _lease_native_call(
    payload: Mapping[str, object], state: HostPermissionState, relay: Mapping[str, object]
) -> dict[str, object]:
    """The service evaluates independent policy before leasing the exact tiered call."""
    call = _object(payload.get("toolCall"))
    name, arguments = call.get("name"), _object(call.get("args"))
    if not isinstance(name, str) or not name:
        raise ValueError("Missing native tool name")
    call_id = _native_call_id(payload, state)
    verdict = _relay_post(
        relay,
        "/policies/evaluate",
        {
            "event": {
                "type": "PHASE_TOOL_CALL",
                "data": {
                    "name": name,
                    "arguments": arguments,
                    "call_id": call_id,
                    "expected_permission_tier_revision": state.revision,
                },
                "context": {"harness": "antigravity-native"},
            },
            "_omnigent_elicitation_id": "elicit_evaluate_" + call_id.partition(":")[2][:32],
        },
    )
    action = verdict.get("result")
    if action == "POLICY_ACTION_DENY":
        return {"decision": "deny", "reason": verdict.get("reason") or "Omnigent policy denied"}
    if action not in ("POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"):
        raise ValueError("Native policy did not confirm permission")
    # The service has completed both independent policy and mandatory tier approval.
    return {"decision": "ask" if state.tier == "full" else "allow"}


def _delegated_bridge_call(payload: Mapping[str, object], relay: Mapping[str, object]) -> bool:
    call = _object(payload.get("toolCall"))
    if call.get("name") != "call_mcp_tool":
        return False
    args = _object(call.get("args"))
    if args.get("ServerName") != "omnigent":
        return False
    tool = args.get("ToolName")
    tools = relay.get("tools")
    return (
        isinstance(tool, str)
        and isinstance(tools, list)
        and any(isinstance(entry, dict) and entry.get("name") == tool for entry in tools)
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        parser.add_argument("--tier-file")
        parser.add_argument("--finish", action="store_true")
        args, unknown = parser.parse_known_args(argv)
        if unknown or not args.tier_file:
            raise ValueError("Expected --tier-file")
        tier_file = Path(args.tier_file)
        if tier_file.is_symlink():
            raise ValueError("Refusing symlink permission tier")
        state = read_host_permission_state(tier_file.parent / HOST_PERMISSION_STATE_FILE)
        if tier_file.read_text(encoding="utf-8").strip() != state.tier:
            raise ValueError("Permission cache mismatch")
        payload = _object(json.load(sys.stdin))
        if args.finish:
            if state.tier == "full":
                print("{}")
                return 0
            relay = _relay_config(tier_file.parent, state)
            if not _delegated_bridge_call(payload, relay):
                completed = _relay_post(
                    relay,
                    "/permission-tiers/control",
                    {
                        "type": "permission_tier_finish",
                        "call_id": _native_call_id(payload, state),
                        "expected_revision": state.revision,
                    },
                )
                if completed.get("finished") is not True:
                    raise ValueError("Native completion was not confirmed")
            print("{}")
            return 0
        relay = _confirmed_relay(tier_file.parent, state)
        if _delegated_bridge_call(payload, relay):
            result = {"decision": "ask"}
        elif state.tier == "full":
            result = _lease_native_call(payload, state, relay)
        else:
            context = local_host_context(
                state,
                control_paths=(
                    str(tier_file.parent),
                    str(Path(permission_tiers.__file__).parent),
                    str(Path(__file__).resolve()),
                ),
            )
            result = decide(payload, state.tier, load_rules(), context)
            if result["decision"] != "deny":
                result = _lease_native_call(payload, state, relay)
        if read_host_permission_state(tier_file.parent / HOST_PERMISSION_STATE_FILE) != state:
            raise ValueError("Permission state changed during native policy check")
    except Exception as exc:
        result = _error(exc)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
