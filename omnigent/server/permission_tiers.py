"""Server-owned permission state and exact, single-call approval bindings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from omnigent.permission_tiers import (
    AGY_PERMISSION_TIER_LABEL_KEY,
    DEFAULT_PERMISSION_TIER,
    PI_PERMISSION_TIER_LABEL_KEY,
    PermissionTier,
    normalize_permission_tier,
)

PERMISSION_STATE_LABEL_KEY = "omnigent.permission_tier.state"
PERMISSION_MIGRATION_LABEL_KEY = "omnigent.permission_tier.migrated_from"
PERMISSION_CAPABILITY_LABEL_KEY = "omnigent.permission_tier.capability"
PERMISSION_CAPABILITY_VALUE = "permission_tiers_v1"
PERMISSION_RUNTIME_ID_LABEL_KEY = "omnigent.permission_tier.runtime_id"
PERMISSION_PROTOCOL_HEADER = "X-Omnigent-Permission-Tiers"
PERMISSION_PROTOCOL_VERSION = "1"
TIER_HARNESSES = frozenset({"pi", "pi-native", "antigravity-native"})
PERMISSION_LABEL_KEYS = frozenset(
    {
        PI_PERMISSION_TIER_LABEL_KEY,
        AGY_PERMISSION_TIER_LABEL_KEY,
        PERMISSION_STATE_LABEL_KEY,
        PERMISSION_MIGRATION_LABEL_KEY,
        PERMISSION_CAPABILITY_LABEL_KEY,
        PERMISSION_RUNTIME_ID_LABEL_KEY,
    }
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


def tier_label_key(harness: str) -> str:
    if harness == "antigravity-native":
        return AGY_PERMISSION_TIER_LABEL_KEY
    if harness in {"pi", "pi-native"}:
        return PI_PERMISSION_TIER_LABEL_KEY
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
    request: object, *, harness: str | None, allow_query: bool = False
) -> None:
    """Refuse an old client before a tiered session is exposed or changed."""
    if harness not in TIER_HARNESSES:
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
) -> PermissionTier:
    """Only this create request's explicit selection can mint a new full session."""
    tier_label_key(harness)
    if explicit is not None:
        try:
            return normalize_permission_tier(explicit, harness=harness, legacy=True)
        except ValueError as exc:
            from omnigent.errors import ErrorCode, OmnigentError

            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
    if inherited is not None:
        return DEFAULT_PERMISSION_TIER if inherited == "full" else inherited
    if default is None:
        return DEFAULT_PERMISSION_TIER
    tier = normalize_permission_tier(default, harness=harness, legacy=True)
    return DEFAULT_PERMISSION_TIER if tier == "full" else tier


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
