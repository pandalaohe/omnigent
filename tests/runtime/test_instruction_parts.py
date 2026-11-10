"""Labelled parts preserve the startup and per-turn delivery channels exactly."""

import pytest

from omnigent.runtime.prompt import (
    CHILD_QUIET_INSTRUCTION,
    EMBEDDED_BROWSER_PRIORITY_INSTRUCTION,
    PEER_SESSION_GRANT,
    SUBAGENT_WAKE_NOTICE_INSTRUCTION,
    InstructionPart,
    build_instructions,
    build_instructions_nullable,
    child_session_question_instruction,
    instruction_parts,
    native_startup_instruction_parts,
    native_startup_instructions,
    session_instruction_parts,
)
from omnigent.spec import AgentSpec, BuiltinToolConfig, ToolsConfig


def test_native_parts_follow_received_order_without_stripping_user_text():
    spec = AgentSpec(spec_version=1, instructions="  Author\n", spawn=True)
    session = session_instruction_parts(
        "  Route\n", workspace=None, worktree=None, peer_messaging_enabled=True, has_parent=True
    )
    parts = native_startup_instruction_parts(spec, session)
    assert [part.kind for part in parts] == [
        "author",
        "framework",
        "framework",
        "user_layers",
        "peer_grant",
        "child_rule",
    ]
    expected = "\n\n".join(
        [
            "  Author\n",
            SUBAGENT_WAKE_NOTICE_INSTRUCTION,
            EMBEDDED_BROWSER_PRIORITY_INSTRUCTION,
            "  Route\n",
            PEER_SESSION_GRANT,
            CHILD_QUIET_INSTRUCTION,
        ]
    )
    assert "\n\n".join(part.text for part in parts) == expected
    assert (
        native_startup_instructions(
            spec, global_instructions="\n\n".join(part.text for part in session)
        )
        == expected
    )


def test_per_turn_parts_strip_framework_but_preserve_author_and_request():
    spec = AgentSpec(spec_version=1, instructions=" Author ")
    parts = instruction_parts(spec, " Request ", [], framework_instructions=[" Route ", " \n"])
    assert [part.kind for part in parts] == ["author", "per_request", "framework", "framework"]
    expected = " Author \n\n Request \n\n" + EMBEDDED_BROWSER_PRIORITY_INSTRUCTION + "\n\nRoute"
    assert "\n\n".join(part.text for part in parts) == expected
    assert (
        build_instructions(spec, " Request ", [], framework_instructions=[" Route ", " \n"])
        == expected
    )


def test_regular_and_nullable_fallbacks_stay_distinct():
    spec = AgentSpec(spec_version=1)
    assert (
        build_instructions(spec, None, [])
        == "You are a helpful assistant.\n\n" + EMBEDDED_BROWSER_PRIORITY_INSTRUCTION
    )
    assert build_instructions_nullable(spec, None, []) == EMBEDDED_BROWSER_PRIORITY_INSTRUCTION


def test_explicit_host_side_placeholders_keep_their_position():
    spec = AgentSpec(spec_version=1)
    sections = [InstructionPart("worktree", None), InstructionPart("user_layers", "Route")]
    native = native_startup_instruction_parts(spec, sections)
    sdk = instruction_parts(spec, None, [], framework_instructions=sections)
    assert native[-2:] == sections
    assert sdk[-2:] == sections
    assert native_startup_instructions(spec, global_instructions="Route") == "\n\n".join(
        part.text for part in native if part.text is not None
    )


@pytest.mark.parametrize("has_parent", [False, True])
@pytest.mark.parametrize("grant", ["spawn", "agent", "web_fetch", "none"])
def test_wake_notice_tracks_effective_dispatch(has_parent, grant):
    tools = ToolsConfig(
        agents=["worker"] if grant == "agent" else [],
        builtins=[BuiltinToolConfig(name="web_fetch")] if grant == "web_fetch" else [],
    )
    spec = AgentSpec(spec_version=1, spawn=grant == "spawn", tools=tools)
    expected = grant in {"agent", "web_fetch"} or (grant == "spawn" and not has_parent)
    for text in (
        native_startup_instructions(spec, has_parent=has_parent),
        build_instructions(spec, None, [], has_parent=has_parent),
        build_instructions_nullable(spec, None, [], has_parent=has_parent),
    ):
        assert text is not None
        assert (SUBAGENT_WAKE_NOTICE_INSTRUCTION in text) == expected


@pytest.mark.parametrize("peer", [False, True])
@pytest.mark.parametrize("has_parent", [False, True])
def test_child_question_is_last_and_emitted_once(peer, has_parent):
    spec = AgentSpec(spec_version=1, spawn=True, instructions=" Author ")
    session = session_instruction_parts(
        " Global ",
        workspace=None,
        worktree=None,
        peer_messaging_enabled=peer,
        has_parent=has_parent,
    )
    parts = native_startup_instruction_parts(
        spec, session, has_parent=has_parent, peer_messaging_enabled=peer
    )
    question = child_session_question_instruction(peer)
    text = "\n\n".join(part.text for part in parts if part.text is not None)
    assert text.count(question) == int(has_parent)
    if has_parent:
        assert parts[-1] == InstructionPart("child_question", question)
