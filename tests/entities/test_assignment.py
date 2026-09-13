"""Tests for the assignment entities.

Covers the §2.3 transition table as data (every legal pair accepted, a
representative illegal set rejected), the exactly-one-execution-root
invariant, and the inputs/outputs JSON round trip.
"""

from __future__ import annotations

import pytest

from omnigent.entities.assignment import (
    LEGAL_TRANSITIONS,
    NON_TERMINAL_STATES,
    TERMINAL_STATES,
    Assignment,
    AssignmentInputEntry,
    AssignmentOutputEntry,
    AssignmentState,
    inputs_from_json,
    inputs_to_json,
    is_legal_transition,
    outputs_from_json,
    outputs_to_json,
)


def _input(root: bool = True) -> AssignmentInputEntry:
    """One minimal input entry."""
    return AssignmentInputEntry(
        repository_name="root",
        repository_revision=3,
        remote_url="git@github.com:example/repo.git",
        input_commit="a" * 40,
        input_ref="refs/omnigent/assignments/a/input/root",
        context_manifest_path=".agents/project/manifest.json",
        manifest_digest="d" * 64,
        artifact_paths=["dist/"],
        is_execution_root=root,
    )


def _assignment(**overrides) -> Assignment:
    """A minimal valid assignment."""
    kwargs = {
        "id": "a" * 32,
        "project_id": "b" * 32,
        "source_session_id": "c" * 32,
        "target_agent_id": "d" * 32,
        "task": "Do the thing",
        "inputs": [_input()],
        "idempotency_key": "key-1",
        "request_digest": "e" * 64,
    }
    kwargs.update(overrides)
    return Assignment(**kwargs)  # type: ignore[arg-type]


# ── transition table ────────────────────────────────────────────────────


# The §2.3 design table as a literal: 26 legal (from, to) pairs,
# creation ``None -> preparing`` included. Kept as data, not derived
# from the implementation, so an omitted edge fails the count below.
_LEGAL_PAIRS: list[tuple[str | None, str]] = [
    (None, "preparing"),
    ("preparing", "waiting"),
    ("preparing", "failed"),
    ("preparing", "cancelled"),
    ("waiting", "starting"),
    ("waiting", "expired"),
    ("waiting", "cancelled"),
    ("starting", "running"),
    ("starting", "waiting"),
    ("starting", "expired"),
    ("starting", "failed"),
    ("starting", "interrupted"),
    ("starting", "stopping"),
    ("running", "publishing"),
    ("running", "failed"),
    ("running", "interrupted"),
    ("running", "stopping"),
    ("publishing", "succeeded"),
    ("publishing", "failed"),
    ("publishing", "interrupted"),
    ("publishing", "stopping"),
    ("stopping", "cancelled"),
    ("stopping", "interrupted"),
    ("interrupted", "waiting"),
    ("interrupted", "expired"),
    ("interrupted", "cancelled"),
]


def test_design_table_pairs_are_legal() -> None:
    """Every §2.3 pair is legal — all 26, creation included."""
    assert len(_LEGAL_PAIRS) == 26
    for from_state, to_state in _LEGAL_PAIRS:
        assert is_legal_transition(from_state, to_state), f"{from_state} -> {to_state}"


def test_every_other_pair_is_illegal() -> None:
    """Every pair outside the §2.3 table is rejected."""
    legal = set(_LEGAL_PAIRS)
    from_states: list[str | None] = [None, *(s.value for s in AssignmentState)]
    to_states = [s.value for s in AssignmentState]
    illegal_count = 0
    for from_state in from_states:
        for to_state in to_states:
            if (from_state, to_state) in legal:
                continue
            assert not is_legal_transition(from_state, to_state), f"{from_state} -> {to_state}"
            illegal_count += 1
    assert illegal_count == len(from_states) * len(to_states) - 26


def test_creation_is_only_none_to_preparing() -> None:
    """``None`` creates exactly one state."""
    assert LEGAL_TRANSITIONS[None] == frozenset({"preparing"})


def test_terminal_states_have_no_outgoing_transitions() -> None:
    """Terminal states map to the empty set and reject every target."""
    assert {"succeeded", "failed", "cancelled", "expired"} == TERMINAL_STATES
    all_states = [s.value for s in AssignmentState]
    for terminal in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[terminal] == frozenset()
        for target in all_states:
            assert not is_legal_transition(terminal, target), f"{terminal} -> {target}"


def test_non_terminal_states_cover_the_rest() -> None:
    """Terminal + non-terminal partition the state set."""
    assert {
        "preparing",
        "waiting",
        "starting",
        "running",
        "publishing",
        "stopping",
        "interrupted",
    } == NON_TERMINAL_STATES
    assert {s.value for s in AssignmentState} == TERMINAL_STATES | NON_TERMINAL_STATES
    assert not (TERMINAL_STATES & NON_TERMINAL_STATES)


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        ("preparing", "starting"),  # must publish first
        ("preparing", "running"),
        ("waiting", "running"),  # must start first
        ("waiting", "succeeded"),
        ("starting", "succeeded"),  # must run, then publish
        ("running", "waiting"),  # no return to the queue
        ("running", "succeeded"),  # must publish outputs first
        ("publishing", "waiting"),
        ("publishing", "running"),  # no rewind once publishing
        ("stopping", "running"),  # stop is one-way
        ("stopping", "waiting"),
        ("stopping", "succeeded"),  # stop lands cancelled or interrupted
        ("interrupted", "running"),  # explicit retry goes via waiting
        ("interrupted", "starting"),
        ("interrupted", "succeeded"),
        ("waiting", "preparing"),  # no rewind to preparing
        ("succeeded", "waiting"),  # terminal, covered in bulk too
        ("preparing", "preparing"),  # no self-loop
        ("waiting", "waiting"),
        ("running", "running"),
    ],
)
def test_representative_illegal_transitions_rejected(from_state: str, to_state: str) -> None:
    """Pairs absent from the §2.3 table are rejected."""
    assert not is_legal_transition(from_state, to_state)


def test_unknown_states_rejected() -> None:
    """States outside the enum never transition."""
    assert not is_legal_transition("waiting", "nope")
    assert not is_legal_transition("nope", "waiting")
    assert not is_legal_transition("nope", "nope")


# ── execution-root invariant ────────────────────────────────────────────


def test_single_execution_root_valid() -> None:
    """One root among several entries is valid."""
    assignment = _assignment(inputs=[_input(root=True), _input(root=False), _input(root=False)])
    assert sum(e.is_execution_root for e in assignment.inputs) == 1


def test_no_execution_root_rejected() -> None:
    """Zero roots is rejected."""
    with pytest.raises(ValueError, match="exactly one"):
        _assignment(inputs=[_input(root=False)])


def test_empty_inputs_rejected() -> None:
    """An empty snapshot holds no root, so it is rejected."""
    with pytest.raises(ValueError, match="exactly one"):
        _assignment(inputs=[])


def test_two_execution_roots_rejected() -> None:
    """Two roots are rejected."""
    with pytest.raises(ValueError, match="exactly one"):
        _assignment(inputs=[_input(root=True), _input(root=True)])


# ── JSON round trip ─────────────────────────────────────────────────────


def test_inputs_json_round_trip() -> None:
    """Input entries survive the JSON encoding, in order."""
    entries = [
        _input(root=True),
        AssignmentInputEntry(
            repository_name="docs",
            repository_revision=7,
            remote_url="git@github.com:example/docs.git",
            input_commit="b" * 40,
            input_ref="refs/omnigent/assignments/a/input/docs",
            context_manifest_path=".agents/project/manifest.json",
            manifest_digest="e" * 64,
            artifact_paths=[],
            is_execution_root=False,
        ),
    ]
    assert inputs_from_json(inputs_to_json(entries)) == entries


def test_inputs_from_json_none_is_empty() -> None:
    """A NULL blob decodes to no entries."""
    assert inputs_from_json(None) == []


def test_outputs_json_round_trip() -> None:
    """Output entries survive the JSON encoding, in order."""
    entries = [
        AssignmentOutputEntry(
            repository_name="root",
            commit="c" * 40,
            ref="refs/omnigent/assignments/a/output/att/root",
            artifact_paths=["dist/bundle.js"],
        ),
        AssignmentOutputEntry(
            repository_name="docs", commit="d" * 40, ref="r2", artifact_paths=[]
        ),
    ]
    assert outputs_from_json(outputs_to_json(entries)) == entries


def test_outputs_from_json_none_is_empty() -> None:
    """A NULL blob decodes to no entries."""
    assert outputs_from_json(None) == []
