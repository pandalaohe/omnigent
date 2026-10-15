"""Attempt identity and outcomes for non-interrupting SDK peer delivery."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

SDK_PEER_CAPABILITY = "sdk_peer_receipts_v1"
CLAUDE_SDK_PEER_CAPABILITY = "claude_sdk_hook_receipts_v1"
SDK_PEER_RECEIPT_EVENT = "sdk_peer_delivery_receipt"
SDK_PEER_STREAM_EVENT = "sdk.peer.receipt"

PeerOutcome = Literal["accepted", "refused", "unknown"]
AttemptPhase = Literal["wait", "accepted", "refused", "unknown"]
_ID = re.compile(r"[0-9a-f]{32}\Z")
_PREFIXES: dict[AttemptPhase, str] = {
    "wait": "sdk_wait:",
    "accepted": "sdk_accept:",
    "refused": "sdk_refuse:",
    "unknown": "sdk_unknown:",
}


@dataclass(frozen=True)
class SDKPeerInput:
    """Server-owned identity accompanying one peer envelope."""

    peer_id: str
    attempt_id: str
    text: str

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.peer_id) or not _ID.fullmatch(self.attempt_id):
            raise ValueError("SDK peer identity must contain two 32-character hexadecimal IDs")

    def metadata(self) -> dict[str, str]:
        """Control metadata travels separately from model-visible text."""
        return {"peer_id": self.peer_id, "attempt_id": self.attempt_id}

    @classmethod
    def from_metadata(cls, value: object, text: str) -> SDKPeerInput:
        """Validate metadata at each process boundary."""
        if not isinstance(value, dict) or set(value) != {"peer_id", "attempt_id"}:
            raise ValueError("Invalid SDK peer delivery metadata")
        peer_id, attempt_id = value["peer_id"], value["attempt_id"]
        if not isinstance(peer_id, str) or not isinstance(attempt_id, str):
            raise ValueError("Invalid SDK peer delivery identity")
        return cls(peer_id, attempt_id, text)


@dataclass(frozen=True)
class SDKPeerOutcome:
    """An SDK-origin result; queueing alone is never accepted."""

    peer_id: str
    attempt_id: str
    outcome: PeerOutcome
    proof_source: str

    @classmethod
    def for_input(
        cls, delivery: SDKPeerInput, outcome: PeerOutcome, proof_source: str
    ) -> SDKPeerOutcome:
        return cls(delivery.peer_id, delivery.attempt_id, outcome, proof_source)


ReportPeerOutcome = Callable[[SDKPeerOutcome], Awaitable[None]]


def sdk_attempt_reason(attempt_id: str, phase: AttemptPhase, *, released: bool = False) -> str:
    """Encode an attempt in the existing bounded reason column."""
    if not _ID.fullmatch(attempt_id):
        raise ValueError("Invalid SDK peer attempt ID")
    return ("released:" if released else "") + _PREFIXES[phase] + attempt_id


def sdk_attempt(reason: str | None) -> tuple[str, AttemptPhase] | None:
    """Decode only complete SDK attempt reasons, excluding arbitrary user text."""
    if reason is None:
        return None
    raw = reason.removeprefix("released:")
    for phase, prefix in _PREFIXES.items():
        if raw.startswith(prefix) and _ID.fullmatch(raw[len(prefix) :]):
            return raw[len(prefix) :], phase
    return None
