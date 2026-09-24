"""Global instructions transport inside the session-init snapshot.

The payload builder writes the text into the snapshot; an envelope from
an older server that lacks the field parses as ``None``, and unknown
extra fields are ignored rather than rejected.
"""

from __future__ import annotations

from typing import Any, cast

from omnigent.entities import Conversation
from omnigent.runner.session_init_protocol import (
    build_runner_session_init_payload,
    parse_runner_session_init_envelope,
)


def _conversation() -> Conversation:
    return Conversation(
        id="conv_global_init",
        created_at=10,
        updated_at=11,
        root_conversation_id="conv_global_init",
        agent_id="agent_global_init",
        runner_id="runner_global_init",
    )


def test_builder_carries_global_instructions() -> None:
    payload = build_runner_session_init_payload(
        _conversation(),
        server_version="0.6.0.dev0",
        global_instructions="prefer rg",
    )
    envelope = parse_runner_session_init_envelope(payload)
    assert envelope is not None
    assert envelope.snapshot.global_instructions == "prefer rg"


def test_envelope_without_field_parses_as_none() -> None:
    payload = build_runner_session_init_payload(_conversation(), server_version="0.6.0.dev0")
    session_init = cast(dict[str, Any], payload["session_init"])
    snapshot = cast(dict[str, Any], session_init["snapshot"])
    del snapshot["global_instructions"]

    envelope = parse_runner_session_init_envelope(payload)
    assert envelope is not None
    assert envelope.snapshot.global_instructions is None


def test_envelope_with_unknown_field_still_parses() -> None:
    payload = build_runner_session_init_payload(
        _conversation(),
        server_version="0.6.0.dev0",
        global_instructions="G",
    )
    session_init = cast(dict[str, Any], payload["session_init"])
    session_init["future_envelope_field"] = "ignored"
    snapshot = cast(dict[str, Any], session_init["snapshot"])
    snapshot["future_snapshot_field"] = 7

    envelope = parse_runner_session_init_envelope(payload)
    assert envelope is not None
    assert envelope.snapshot.global_instructions == "G"
