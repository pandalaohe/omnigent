"""Remember verdicts for Codex command approvals are bound to the displayed request."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.errors import OmnigentError
from omnigent.runtime import pending_elicitations
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
)
from omnigent.server.routes._codex_elicitation import _codex_command_approval_params
from omnigent.server.routes._sessions.orchestration import (
    _harness_elicitation_request_fingerprint,
    _resolve_elicitation,
)
from omnigent.server.schemas import ElicitationRequestEvent, ElicitationResult

_METHOD = "item/commandExecution/requestApproval"


def _command_params(command: str, rule: list[str]) -> dict[str, Any]:
    return {
        "command": command,
        "availableDecisions": [{"acceptWithExecpolicyAmendment": {"execpolicy_amendment": rule}}],
    }


def _displayed_fingerprint(params: dict[str, Any]) -> str:
    value = _codex_command_approval_params(1, _METHOD, params).model_dump()[
        "approval_request_fingerprint"
    ]
    assert isinstance(value, str)
    return value


@pytest.mark.parametrize("remember", [False, True], ids=["once", "remember"])
async def test_reused_elicitation_id_requires_current_command_fingerprint(
    remember: bool,
) -> None:
    session_id = "codex-approval-reuse-test"
    elicitation_id = "reused-codex-id"
    displayed = _displayed_fingerprint(_command_params("pytest -q", ["python", "-m", "pytest"]))
    current = _codex_command_approval_params(
        1, _METHOD, _command_params("sh -c new-command", ["sh", "-c"])
    )
    pending_elicitations.record_publish(
        session_id,
        ElicitationRequestEvent(
            type="response.elicitation_request",
            elicitation_id=elicitation_id,
            params=current,
        ).model_dump(),
    )
    future: asyncio.Future[ElicitationResult] = asyncio.get_running_loop().create_future()
    _harness_elicitation_registry[elicitation_id] = future
    _harness_elicitation_owners[elicitation_id] = session_id
    _harness_parked_elicitations[elicitation_id] = _ParkedHarnessElicitation(
        session_id=session_id,
        tool_name=None,
        tool_input=None,
        resolved_elsewhere=asyncio.Event(),
        request_fingerprint=_harness_elicitation_request_fingerprint(current),
    )
    try:
        with pytest.raises(OmnigentError, match="changed"):
            await _resolve_elicitation(
                session_id,
                {
                    "elicitation_id": elicitation_id,
                    "action": "accept",
                    "content": {
                        **({"remember": True} if remember else {}),
                        "request_fingerprint": displayed,
                    },
                },
                None,
            )
        assert not future.done()
        await _resolve_elicitation(
            session_id,
            {
                "elicitation_id": elicitation_id,
                "action": "accept",
                "content": {
                    **({"remember": True} if remember else {}),
                    "request_fingerprint": current.approval_request_fingerprint,
                },
            },
            None,
        )
        assert future.result().action == "accept"
    finally:
        _harness_elicitation_registry.pop(elicitation_id, None)
        _harness_elicitation_owners.pop(elicitation_id, None)
        _harness_parked_elicitations.pop(elicitation_id, None)
        _harness_pre_resolved_elicitations.pop(elicitation_id, None)
        pending_elicitations.resolve(session_id, elicitation_id)


@pytest.mark.parametrize("conflicting_owner", [False, True])
async def test_codex_verdict_during_reconnect_grace(conflicting_owner: bool) -> None:
    session_id = "codex-approval-reconnect"
    elicitation_id = "reconnecting-approval"
    command = _codex_command_approval_params(1, _METHOD, _command_params("pytest -q", ["pytest"]))
    fingerprint = _harness_elicitation_request_fingerprint(command)
    pending_elicitations.record_publish(
        session_id,
        ElicitationRequestEvent(
            type="response.elicitation_request", elicitation_id=elicitation_id, params=command
        ).model_dump(),
    )
    _harness_parked_elicitations[elicitation_id] = _ParkedHarnessElicitation(
        session_id=session_id,
        tool_name=None,
        tool_input=None,
        resolved_elsewhere=asyncio.Event(),
        request_fingerprint=fingerprint,
    )
    if conflicting_owner:
        _harness_elicitation_owners[elicitation_id] = "other-session"
    try:
        verdict = {
            "elicitation_id": elicitation_id,
            "action": "accept",
            "content": {"request_fingerprint": command.approval_request_fingerprint},
        }
        if conflicting_owner:
            with pytest.raises(OmnigentError, match="changed"):
                await _resolve_elicitation(session_id, verdict, None)
            assert elicitation_id not in _harness_pre_resolved_elicitations
        else:
            await _resolve_elicitation(session_id, verdict, None)
            receipt = _harness_pre_resolved_elicitations[elicitation_id]
            assert receipt.session_id == session_id
            assert receipt.request_fingerprint == fingerprint
            assert receipt.result is not None and receipt.result.action == "accept"
    finally:
        _harness_elicitation_owners.pop(elicitation_id, None)
        _harness_parked_elicitations.pop(elicitation_id, None)
        _harness_pre_resolved_elicitations.pop(elicitation_id, None)
        pending_elicitations.resolve(session_id, elicitation_id)
