"""
Unit tests for the ``sys_session_send`` tool schema.

These cover the ``file_ids`` field added to the OBJECT form of the
``args`` parameter (U1). The schema is validated with ``jsonschema``
against the ``args`` sub-schema so the back-compat contract (plain
string form, object form without ``file_ids``) is exercised directly.
"""

from __future__ import annotations

import jsonschema
import pytest

from omnigent.spec.types import AgentSpec
from omnigent.tools.builtins.spawn import _build_sys_session_send_schema


def _args_schema() -> dict:
    """:returns: The ``anyOf`` schema for the ``args`` parameter."""
    schema = _build_sys_session_send_schema({})
    return schema["function"]["parameters"]["properties"]["args"]


def _schema_with_subagent() -> dict:
    """:returns: The schema for a tool with one named sub-agent."""
    return _build_sys_session_send_schema(
        {"researcher": AgentSpec(spec_version=1, name="researcher", description="d")}
    )


def _object_branch() -> dict:
    """:returns: The object branch of the ``args`` ``anyOf``."""
    for branch in _args_schema()["anyOf"]:
        if branch.get("type") == "object":
            return branch
    raise AssertionError("no object branch in args anyOf")


def _validate(args: object) -> None:
    """Validate ``args`` against the ``args`` parameter schema."""
    jsonschema.validate(instance=args, schema=_args_schema())


# ── Schema structure ──────────────────────────────────


def test_file_ids_present_and_optional() -> None:
    branch = _object_branch()
    assert branch["properties"]["file_ids"] == {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "minItems": 1,
        "description": branch["properties"]["file_ids"]["description"],
    }
    assert "file_ids" not in branch["required"]
    assert branch["required"] == ["input"]
    assert branch["additionalProperties"] is False


def test_description_mentions_file_ids() -> None:
    schema = _schema_with_subagent()
    assert "file_ids" in schema["function"]["description"]


def test_file_ids_description_mentions_fresh_named_spawn_only() -> None:
    schema = _schema_with_subagent()
    description = schema["function"]["description"]
    file_ids_description = _object_branch()["properties"]["file_ids"]["description"]

    assert "first named" in description
    assert "session_id" in description
    assert "continuing an existing named session" in description
    assert "first named" in file_ids_description
    assert "session_id" in file_ids_description
    assert "continuing an existing named session" in file_ids_description


def test_parallel_description_requires_distinct_titles() -> None:
    schema = _schema_with_subagent()
    description = schema["function"]["description"]
    title_description = schema["function"]["parameters"]["properties"]["title"]["description"]

    assert "distinct task-based title" in description
    assert "auto-assigned" in title_description
    assert "structured name" in title_description


# ── Happy paths ───────────────────────────────────────


def test_object_args_with_file_ids_validate() -> None:
    _validate({"input": "go", "file_ids": ["file_abc"]})


def test_object_args_without_file_ids_validate() -> None:
    _validate({"input": "go"})


def test_plain_string_args_validate() -> None:
    _validate("just a message")


# ── Errors ────────────────────────────────────────────


def test_file_ids_string_rejected() -> None:
    with pytest.raises(jsonschema.ValidationError):
        _validate({"input": "go", "file_ids": "file_abc"})


def test_empty_file_ids_rejected() -> None:
    with pytest.raises(jsonschema.ValidationError):
        _validate({"input": "go", "file_ids": []})


def test_duplicate_file_ids_allowed() -> None:
    # uniqueItems was removed so non-OpenAI models don't reject the schema;
    # duplicate file ids are deduplicated by the server handler instead.
    _validate({"input": "go", "file_ids": ["file_abc", "file_abc"]})


def test_empty_file_id_rejected() -> None:
    with pytest.raises(jsonschema.ValidationError):
        _validate({"input": "go", "file_ids": [""]})


def test_file_ids_non_string_item_rejected() -> None:
    with pytest.raises(jsonschema.ValidationError):
        _validate({"input": "go", "file_ids": [123]})


# ── Peer options (T4) ───────────────────────────────────


def _top_properties(*, peer_enabled: bool = False) -> dict:
    """:returns: Top-level parameter properties of the send schema."""
    schema = _build_sys_session_send_schema({}, peer_enabled=peer_enabled)
    return schema["function"]["parameters"]["properties"]


def test_peer_options_present_with_bounds() -> None:
    """``correlation_id`` / ``wait_seconds`` / ``wait_for_reply_seconds`` ranges."""
    props = _top_properties()
    assert props["correlation_id"]["maxLength"] == 64
    assert (props["wait_seconds"]["minimum"], props["wait_seconds"]["maximum"]) == (0, 3600)
    assert props["wait_seconds"]["default"] == 0
    assert (
        props["wait_for_reply_seconds"]["minimum"],
        props["wait_for_reply_seconds"]["maximum"],
    ) == (
        0,
        600,
    )
    assert props["wait_for_reply_seconds"]["default"] == 0


def test_session_id_description_advertises_peer_sends() -> None:
    """The by-id text names peer messaging and the ``sys_session_list`` source."""
    desc = _top_properties()["session_id"]["description"]
    assert "peer messaging is enabled" in desc
    assert "sys_session_list" in desc
    assert "not from its user" in desc


def test_send_description_mentions_peer_mode() -> None:
    """The named-mode description points at peer sends for the by-id mode."""
    desc = _schema_with_subagent()["function"]["description"]
    assert "peer messaging is enabled" in desc


def test_by_id_description_mentions_peer_when_flagged() -> None:
    """The empty-spec variant advertises peers only with ``peer_enabled``."""
    plain = _build_sys_session_send_schema({})["function"]["description"]
    assert "peer messaging is enabled" not in plain
    peer = _build_sys_session_send_schema({}, peer_enabled=True)["function"]["description"]
    assert "peer messaging is enabled" in peer
