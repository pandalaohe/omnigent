"""Terminal correlation preserves source identity and diagnoses missing receipts."""

import pytest

from omnigent.entities.subagent_terminal import TerminalEnvelope, TurnIdentity


@pytest.mark.parametrize("generation", [None, -1, True, "7"])
def test_unusable_generation_is_not_coerced(generation: object) -> None:
    identity = TurnIdentity.from_data(
        {"response_id": "response-a", "input_generation": generation}
    )
    assert identity.turn_id == "response-a"
    assert identity.input_generation is None
    assert identity.identity_diagnostic == "terminal_identity_unavailable: input_id"


def test_recovered_labels_preserve_the_admitted_identity() -> None:
    identity = TurnIdentity.from_data(
        {"turn_id": "turn-a", "input_stable_id": "input-a", "input_generation": 7}
    )
    terminal = TerminalEnvelope.from_labels(
        "child",
        parent_id="parent",
        status="failed",
        labels=identity.identity_labels(),
        error={"code": "capacity", "message": "same provider failure"},
    )
    assert terminal.model_dump() == {
        "child_id": "child",
        "parent_id": "parent",
        "event_kind": "turn_terminal",
        "turn_id": "turn-a",
        "input_id": "input-a",
        "input_generation": 7,
        "status": "failed",
        "error": {"code": "capacity", "message": "same provider failure"},
        "recovered": True,
    }
    assert terminal.identity_diagnostic is None


@pytest.mark.parametrize("turn_id", [None, "", "codex_", "codex_native", "x" * 257])
def test_missing_source_identity_is_not_replaced_by_terminal_delivery(turn_id: object) -> None:
    terminal = TerminalEnvelope.from_data(
        "child", parent_id="parent", status="failed", data={"turn_id": turn_id}
    )
    assert terminal.turn_id is None
    assert terminal.identity_key is None
    assert terminal.identity_diagnostic == "terminal_identity_unavailable: turn_id, input_id"
    dispatch = TerminalEnvelope.from_data(
        "child", parent_id="parent", status="failed", data={"event_kind": "dispatch_failed"}
    )
    assert dispatch.event_kind == "dispatch_failed"
    assert dispatch.turn_id is None
    assert dispatch.identity_diagnostic is None
