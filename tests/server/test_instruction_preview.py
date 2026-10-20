"""Preview text matches the real composer for each launch family."""

import json

import pytest

from omnigent.runtime.prompt import (
    build_instructions,
    build_instructions_nullable,
    child_session_question_instruction,
    native_startup_instructions,
    session_instruction_parts,
)
from omnigent.server.instruction_preview import preview_instructions
from omnigent.spec import AgentSpec, ExecutorSpec
from omnigent.stores.global_instructions_store import GlobalInstructionRevision


def _revision(scope: str, text: str, *, agent_key: str = "", host_id: str = ""):
    return GlobalInstructionRevision(
        id=f"{scope}-{agent_key}-{host_id}",
        text=text,
        created_at=1,
        created_by=None,
        scope=scope,
        agent_key=agent_key,
        host_id=host_id,
    )


@pytest.mark.parametrize("harness", ["codex-native", "claude-sdk", "opencode-native"])
@pytest.mark.parametrize("quota", ["", "Quota evidence"])
def test_preview_matches_composer_with_untrimmed_inner_layers(harness: str, quota: str) -> None:
    spec = AgentSpec(spec_version=1, instructions=" Author ", spawn=True)
    layers = [
        _revision("shared", "  Shared  "),
        _revision("agent", "  Agent  ", agent_key="codex"),
        _revision("host", "  Host  ", host_id="host_a"),
        _revision("mother", "  Mother  "),
    ]
    preview = preview_instructions(
        layers,
        host_id="host_a",
        agent_key="codex",
        spec=spec,
        name="Test",
        harness=harness,
        has_parent=False,
        spawn=True,
        peer=False,
        quota_line=quota,
    )
    route = "  Shared  \n\n  Agent  \n\n  Host  \n\n  Mother  "
    if quota:
        route += "\n\n" + quota
    if harness == "codex-native":
        expected = native_startup_instructions(spec, global_instructions=route)
    elif harness == "opencode-native":
        expected = build_instructions_nullable(spec, None, [], framework_instructions=[route])
    else:
        expected = build_instructions(spec, None, [], framework_instructions=[route])
    assert preview["text"] == expected
    assert preview["total_chars"] == len(expected)
    assert (
        "\n\n".join(p["text"] for p in preview["parts"] if p["sent"] and p["text"] is not None)
        == expected
    )
    assert [p["scope"] for p in preview["parts"] if p.get("scope") and p["sent"]] == [
        "shared",
        "agent",
        "host",
        "mother",
    ]
    assert all(
        p["sent"] and p["text"] is None
        for p in preview["parts"]
        if p["kind"] in {"worktree", "project_code", "per_request", "skills_hint", "routing_note"}
    )


def test_independent_replaced_layers_are_visible_but_not_sent() -> None:
    spec = AgentSpec(spec_version=1)
    preview = preview_instructions(
        [
            _revision("shared", "Shared"),
            _revision("independent", " Own ", agent_key="codex", host_id="host_a"),
        ],
        host_id="host_a",
        agent_key="codex",
        spec=spec,
        name="Test",
        harness="codex-native",
        has_parent=False,
        spawn=False,
        peer=False,
    )
    assert "Shared" not in preview["text"]
    assert next(p for p in preview["parts"] if p.get("scope") == "shared")["sent"] is False
    assert next(p for p in preview["parts"] if p.get("scope") == "independent")["sent"]


def test_role_blocks_follow_route_and_are_exclusive() -> None:
    spec = AgentSpec(spec_version=1, spawn=True)
    layers = [
        _revision("shared", "Route"),
        _revision("mother", "Mother"),
        _revision("child", "Child"),
    ]
    mother = preview_instructions(
        layers,
        host_id=None,
        agent_key=None,
        spec=spec,
        name="Test",
        harness="codex-native",
        has_parent=False,
        spawn=True,
        peer=False,
    )
    child = preview_instructions(
        layers,
        host_id=None,
        agent_key=None,
        spec=spec,
        name="Test",
        harness="codex-native",
        has_parent=True,
        spawn=True,
        peer=False,
    )
    assert "Route\n\nMother" in mother["text"]
    assert "Child" not in mother["text"]
    assert "Route\n\nChild" in child["text"]
    assert "Mother" not in child["text"]
    assert child["parts"][-1]["sent"] is True
    assert child["parts"][-1]["kind"] == "child_question"


@pytest.mark.parametrize("harness", ["codex-native", "claude-sdk", "opencode-native"])
def test_child_preview_matches_runner_fold_with_peer_and_padding(harness: str) -> None:
    spec = AgentSpec(spec_version=1, instructions=" Author ", spawn=True)
    layers = [_revision("shared", "  Shared  "), _revision("child", " Child ")]
    preview = preview_instructions(
        layers,
        host_id=None,
        agent_key=None,
        spec=spec,
        name="Child",
        harness=harness,
        has_parent=True,
        spawn=True,
        peer=True,
    )
    folded = "\n\n".join(
        part.text
        for part in session_instruction_parts(
            "  Shared  \n\n Child ",
            workspace=None,
            worktree=None,
            peer_messaging_enabled=True,
            has_parent=True,
        )
    )
    if harness == "codex-native":
        expected = "\n\n".join(
            (
                native_startup_instructions(spec, global_instructions=folded),
                child_session_question_instruction(True),
            )
        )
    elif harness == "opencode-native":
        expected = build_instructions_nullable(spec, None, [], framework_instructions=[folded])
    else:
        expected = build_instructions(spec, None, [], framework_instructions=[folded])
    assert preview["text"] == expected
    assert (
        "\n\n".join(p["text"] for p in preview["parts"] if p["sent"] and p["text"] is not None)
        == expected
    )


def test_preview_never_serializes_executor_connection_settings() -> None:
    marker = "synthetic-connection-setting"
    spec = AgentSpec(
        spec_version=1,
        instructions="Safe author text",
        executor=ExecutorSpec(config={"connection_marker": marker}),
    )
    preview = preview_instructions(
        [],
        host_id=None,
        agent_key=None,
        spec=spec,
        name="Test",
        harness="claude-sdk",
        has_parent=False,
        spawn=False,
        peer=False,
    )
    assert marker not in json.dumps(preview)


@pytest.mark.parametrize("product", ["codex", "claude", "opencode"])
def test_native_aliases_use_the_same_composer_and_child_instructions(product: str) -> None:
    spec = AgentSpec(spec_version=1, instructions=" Author ", spawn=True)
    arguments = {
        "host_id": None,
        "agent_key": product,
        "spec": spec,
        "name": "Child",
        "has_parent": True,
        "spawn": True,
        "peer": True,
    }
    layers = [_revision("shared", " Shared ")]
    canonical = preview_instructions(layers, harness=f"{product}-native", **arguments)
    alias = preview_instructions(layers, harness=f"native-{product}", **arguments)
    assert alias["text"] == canonical["text"]
    assert alias["parts"] == canonical["parts"]
