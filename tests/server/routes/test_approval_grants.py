"""Unit tests for the one-shot deferred-approval grant registry."""

from __future__ import annotations

from omnigent.server.routes._sessions.approval_grants import ApprovalGrants
from omnigent.server.schemas import ElicitationResult


def test_approval_grants_consume_once_expire_and_stay_session_scoped() -> None:
    """A grant is consumed once, expires with its TTL, and never crosses sessions."""
    now = {"value": 100.0}
    grants = ApprovalGrants(clock=lambda: now["value"])
    verdict = ElicitationResult(action="accept", content={"remember": True})
    key = ("Bash", "ls", "/repo")

    grants.put("conv_a", key, verdict, 10.0)
    assert grants.consume("conv_a", key) is verdict
    assert grants.consume("conv_a", key) is None

    grants.put("conv_a", key, verdict, 10.0)
    assert grants.consume("conv_b", key) is None
    now["value"] += 11.0
    assert grants.consume("conv_a", key) is None

    grants.put("conv_a", key, verdict, 10.0)
    now["value"] += 11.0
    assert grants.consume("conv_a", key) is None
