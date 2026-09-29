"""Unit tests for the one-shot deferred-approval grant registry."""

from __future__ import annotations

from omnigent.server.routes._sessions.approval_grants import (
    ApprovalGrants,
    claude_grant_key,
)
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


def test_claude_grant_key_binds_non_bash_tools_to_cwd() -> None:
    """The same non-Bash input under two cwds must not share a grant key."""
    tool_input = {"file_path": "/repo/app/main.py", "content": "print('hi')\n"}
    assert claude_grant_key("Write", tool_input, "/repo/app") != claude_grant_key(
        "Write", tool_input, "/repo/other"
    )
