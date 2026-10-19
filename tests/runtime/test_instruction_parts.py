"""Labelled parts preserve the startup and per-turn delivery channels exactly."""

from omnigent.runtime.prompt import (
    CHILD_QUIET_INSTRUCTION,
    EMBEDDED_BROWSER_PRIORITY_INSTRUCTION,
    PEER_SESSION_GRANT,
    SUBAGENT_WAKE_NOTICE_INSTRUCTION,
    InstructionPart,
    build_instructions,
    build_instructions_nullable,
    instruction_parts,
    native_startup_instruction_parts,
    native_startup_instructions,
    session_instruction_parts,
)
from omnigent.spec import AgentSpec


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
