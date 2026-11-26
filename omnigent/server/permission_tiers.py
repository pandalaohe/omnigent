"""Server-owned permission state and exact, single-call approval bindings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.permission_tiers import (
    AGY_PERMISSION_TIER_LABEL_KEY,
    DEFAULT_PERMISSION_TIER,
    NON_FULL_TIERS,
    PI_PERMISSION_TIER_LABEL_KEY,
    PermissionTier,
    normalize_permission_tier,
)
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.session_open_rate import CREATE_ORIGIN_AGENT, CREATE_ORIGIN_HEADER

if TYPE_CHECKING:
    from omnigent.server.host_registry import HostRegistry
    from omnigent.spec.types import AgentSpec

PERMISSION_STATE_LABEL_KEY = "omnigent.permission_tier.state"
PERMISSION_MIGRATION_LABEL_KEY = "omnigent.permission_tier.migrated_from"
PERMISSION_CAPABILITY_LABEL_KEY = "omnigent.permission_tier.capability"
PERMISSION_CAPABILITY_VALUE = "permission_tiers_v1"
PERMISSION_RUNTIME_ID_LABEL_KEY = "omnigent.permission_tier.runtime_id"
PERMISSION_PROTOCOL_HEADER = "X-Omnigent-Permission-Tiers"
PERMISSION_PROTOCOL_VERSION = "1"
# Sent only on a server-to-runner inspection after catalog and selection checks.
REGISTRY_TOOL_INSPECTION_HEADER = "X-Omnigent-Registry-Tool-Inspection"
TIER_HARNESSES = frozenset({"pi", "pi-native", "antigravity-native"})
ACP_PERMISSION_TIER_LABEL_KEY = "omnigent.acp.permission_tier"
PERMISSION_LABEL_KEYS = frozenset(
    {
        PI_PERMISSION_TIER_LABEL_KEY,
        AGY_PERMISSION_TIER_LABEL_KEY,
        ACP_PERMISSION_TIER_LABEL_KEY,
        PERMISSION_STATE_LABEL_KEY,
        PERMISSION_MIGRATION_LABEL_KEY,
        PERMISSION_CAPABILITY_LABEL_KEY,
        PERMISSION_RUNTIME_ID_LABEL_KEY,
    }
)


def is_acp_permission_harness(harness: str | None) -> bool:
    from omnigent.acp_cli_harnesses import is_acp_harness

    return is_acp_harness(harness)


def configured_acp_default_tier(
    registry: HostRegistry | None, host_id: str | None, harness: str | None
) -> str | None:
    if registry is None or not host_id or not is_acp_permission_harness(harness):
        return None
    connection = registry.get(host_id)
    if connection is None:
        return None
    tiers = getattr(connection.hello, "acp_permission_tiers", None) or {}
    tier = tiers.get(harness)
    return tier if tier in NON_FULL_TIERS else None


def supports_delegated_acp_tier(
    registry: HostRegistry | None, host_id: str | None, harness: str | None, spec: AgentSpec | None
) -> bool:
    """Default to managed tiers only for ACP launches with a known tool schema."""
    from omnigent.acp_cli_harnesses import is_antigravity_acp_command
    from omnigent.host.frames import CAP_AGY_ACP_PERMISSIONS_PREFIX

    config = spec.executor.config if spec is not None else {}
    if "acp_agent" in config:
        embedded = config["acp_agent"]
        command = embedded.get("command") if isinstance(embedded, dict) else None
        return isinstance(command, str) and is_antigravity_acp_command(command)
    if registry is None or not host_id or harness is None:
        return False
    connection = registry.get(host_id)
    return connection is not None and (
        CAP_AGY_ACP_PERMISSIONS_PREFIX + harness in connection.hello.capabilities
    )


def permission_tier_active(harness: str | None, labels: Mapping[str, str]) -> bool:
    """ACP opts into managed tiers; existing managed harnesses always require them."""
    return harness in TIER_HARNESSES or (
        is_acp_permission_harness(harness)
        and (ACP_PERMISSION_TIER_LABEL_KEY in labels or PERMISSION_STATE_LABEL_KEY in labels)
    )


def is_permission_label_key(key: str) -> bool:
    """Reserve DB-collation lookalikes as well as exact authority labels."""
    from omnigent.stores.conversation_store import _may_collate_to

    return any(_may_collate_to(key, owned, prefix=False) for owned in PERMISSION_LABEL_KEYS)


def strip_creation_permission_labels(labels: dict[str, str]) -> None:
    """Discard copied authority; only permission_tier selects a new session's tier."""
    for key in list(labels):
        if is_permission_label_key(key):
            del labels[key]


def permission_execution_host_id(*, host_id: str | None, runner_id: str | None) -> str:
    """Hostless CLI sessions bind authority to their server-assigned runner."""
    if host_id:
        return host_id
    if runner_id:
        return f"runner:{runner_id}"
    raise ValueError("Permission check requires a verified execution host")


def tier_label_key(harness: str) -> str:
    if harness == "antigravity-native":
        return AGY_PERMISSION_TIER_LABEL_KEY
    if harness in {"pi", "pi-native"}:
        return PI_PERMISSION_TIER_LABEL_KEY
    if is_acp_permission_harness(harness):
        return ACP_PERMISSION_TIER_LABEL_KEY
    raise ValueError(f"Harness does not support permission tiers: {harness}")


@dataclass(frozen=True)
class ConfirmedPermissionState:
    """The tier has one label; its separate state fences incomplete transactions."""

    tier: PermissionTier
    revision: int
    status: Literal["ready", "switching", "blocked"]
    transaction_id: str | None = None

    def state_label(self) -> str:
        return json.dumps(
            {
                "revision": self.revision,
                "status": self.status,
                "transaction_id": self.transaction_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def labels(self, harness: str) -> dict[str, str]:
        return {
            tier_label_key(harness): self.tier,
            PERMISSION_STATE_LABEL_KEY: self.state_label(),
        }


def read_permission_state(labels: Mapping[str, str], *, harness: str) -> ConfirmedPermissionState:
    """Require a canonical tier and a confirmed state; missing state is not full."""
    tier = normalize_permission_tier(
        labels.get(tier_label_key(harness)), harness=harness, legacy=False
    )
    raw = labels.get(PERMISSION_STATE_LABEL_KEY)
    if raw is None:
        raise ValueError("Permission state requires controlled recovery")
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError("Permission state must be an object")
    revision, status = state.get("revision"), state.get("status")
    transaction_id = state.get("transaction_id")
    if type(revision) is not int or revision < 1:
        raise ValueError("Permission revision must be a positive integer")
    if not isinstance(status, str) or status not in {"ready", "switching", "blocked"}:
        raise ValueError("Unknown permission runtime state")
    if transaction_id is not None and (not isinstance(transaction_id, str) or not transaction_id):
        raise ValueError("Invalid permission transaction id")
    if status == "switching" and transaction_id is None:
        raise ValueError("Switching permission state requires a transaction id")
    if (
        status == "ready"
        and labels.get(PERMISSION_CAPABILITY_LABEL_KEY) != PERMISSION_CAPABILITY_VALUE
    ):
        raise ValueError("Ready permission state requires confirmed runner capability")
    return ConfirmedPermissionState(tier, revision, status, transaction_id)


def require_permission_protocol(
    request: object, *, harness: str | None, allow_query: bool = False, required: bool = False
) -> None:
    """Refuse an old client before a tiered session is exposed or changed."""
    if harness not in TIER_HARNESSES and not required:
        return
    from fastapi import HTTPException

    headers = getattr(request, "headers", {})
    query = getattr(request, "query_params", {})
    if headers.get(PERMISSION_PROTOCOL_HEADER) != PERMISSION_PROTOCOL_VERSION and not (
        allow_query and query.get("permission_tiers") == PERMISSION_PROTOCOL_VERSION
    ):
        raise HTTPException(
            status_code=426,
            detail={
                "code": "permission_tiers_refresh_required",
                "message": "Refresh this client to use permission tiers.",
            },
            headers={"Upgrade": "permission-tiers-v1"},
        )


def creation_permission_tier(
    *,
    harness: str,
    explicit: object = None,
    default: object = None,
    inherited: PermissionTier | None = None,
    ceiling: PermissionTier | None = None,
) -> PermissionTier:
    """Honor explicit selections and saved defaults without copying full authority."""
    tier_label_key(harness)
    if explicit is not None:
        try:
            selected = normalize_permission_tier(explicit, harness=harness, legacy=True)
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        # Auto denies some edits outright that accept-edits would run without a card.
        if (
            ceiling not in (None, "full", selected)
            and selected != "plan"
            and not (selected == "ask" and ceiling in ("accept-edits", "auto"))
        ):
            raise OmnigentError(
                "A child session cannot exceed its creator's permission tier",
                code=ErrorCode.FORBIDDEN,
            )
        return selected
    if inherited is not None:
        return DEFAULT_PERMISSION_TIER if inherited == "full" else inherited
    if default is None:
        return DEFAULT_PERMISSION_TIER
    return normalize_permission_tier(default, harness=harness, legacy=True)


def legacy_permission_tier(
    *, harness: str, labels: Mapping[str, str], launch_args: Sequence[str]
) -> PermissionTier:
    """Normalize stored values without deriving full from a malformed label."""
    key = tier_label_key(harness)
    if key in labels:
        return normalize_permission_tier(labels[key], harness=harness, legacy=True)
    if harness == "antigravity-native" and "--dangerously-skip-permissions" in launch_args:
        return "full"
    return "ask"


def agent_originated_create(headers: Mapping[str, str]) -> bool:
    """Recognize the runner's create paths without treating a UI fork as delegated."""
    return (
        headers.get(CREATE_ORIGIN_HEADER, "").strip().lower() == CREATE_ORIGIN_AGENT
        or RUNNER_TUNNEL_TOKEN_HEADER in headers
        or ORCHESTRATION_SOURCE_HEADER in headers
    )


def source_permission_tier(
    harness: str | None,
    labels: Mapping[str, str],
    launch_args: Sequence[str],
    *,
    require_ready: bool = False,
) -> PermissionTier | None:
    """Read the source's tier in its own harness, including legacy sessions."""
    if harness is None or not permission_tier_active(harness, labels):
        return None
    try:
        if require_ready and PERMISSION_STATE_LABEL_KEY in labels:
            state = read_permission_state(labels, harness=harness)
            if state.status != "ready":
                raise ValueError("Source runtime permission state is not ready")
            return state.tier
        return legacy_permission_tier(harness=harness, labels=labels, launch_args=launch_args)
    except ValueError as exc:
        raise OmnigentError(
            f"Source permission tier requires recovery: {exc}", code=ErrorCode.INVALID_INPUT
        ) from exc


@dataclass(frozen=True)
class BoundToolCall:
    session_id: str
    host_id: str
    call_id: str
    name: str
    arguments_digest: str
    tier_revision: int

    @classmethod
    def bind(
        cls,
        *,
        session_id: str,
        host_id: str,
        call_id: str,
        name: str,
        arguments: Mapping[str, object],
        tier_revision: int,
    ) -> BoundToolCall:
        if not all(
            isinstance(value, str) and value for value in (session_id, host_id, call_id, name)
        ):
            raise ValueError("Tool authorization requires session, host, call id and name")
        if type(tier_revision) is not int or tier_revision < 1:
            raise ValueError("Tool authorization requires a confirmed permission revision")
        if not isinstance(arguments, Mapping) or any(
            not isinstance(key, str) for key in arguments
        ):
            raise ValueError("Tool arguments must be a JSON object")
        encoded = json.dumps(
            dict(arguments), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return cls(
            session_id, host_id, call_id, name, hashlib.sha256(encoded).hexdigest(), tier_revision
        )

    def approval_id(self) -> str:
        encoded = json.dumps(
            [
                self.session_id,
                self.host_id,
                self.call_id,
                self.name,
                self.arguments_digest,
                self.tier_revision,
            ],
            separators=(",", ":"),
        ).encode()
        return "elicit_permission_tier_" + hashlib.sha256(encoded).hexdigest()
