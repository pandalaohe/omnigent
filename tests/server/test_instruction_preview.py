"""Preview text matches the real composer for each launch family."""

import json

import pytest

from omnigent.runtime.prompt import (
    build_instructions,
    build_instructions_nullable,
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


def test_codex_parent_column_does_not_leak_into_claude_preview():
    preview = preview_instructions(
        [_revision("parent", "Codex only", agent_key="codex")],
        host_id="host_a",
        agent_key="claude",
        spec=None,
        name="Claude",
        harness="claude-native",
        has_parent=False,
        spawn=True,
        peer=False,
        quota_line="Quota",
    )
    assert preview["text"] == ""
    assert preview["parts"] == []


@pytest.mark.parametrize("role", ["parent", "child"])
@pytest.mark.parametrize("independent", [" \n", " Role cell "])
@pytest.mark.parametrize("route_independent", [False, True])
def test_preview_role_sources_keep_original_keys(role, independent, route_independent):
    layers = [
        _revision("shared", "Route"),
        _revision(role, " Shared role "),
        _revision(role, " Column ", agent_key="codex"),
        _revision(role, " Host ", host_id="host_a"),
        _revision(role, independent, agent_key="codex", host_id="host_a"),
        _revision(role, "Other agent", agent_key="claude"),
        _revision(role, "Other host", host_id="host_b"),
    ]
    if route_independent:
        layers.append(_revision("independent", "Own route", agent_key="codex", host_id="host_a"))
    preview = preview_instructions(
        layers,
        host_id="host_a",
        agent_key="codex",
        spec=None,
        name="Test",
        harness="codex-native",
        has_parent=role == "child",
        spawn=True,
        peer=False,
        quota_line="Quota",
    )
    selected = [layers[4]] if independent.strip() else layers[1:4]
    role_parts = [p for p in preview["parts"] if p.get("scope") == role and p["sent"]]
    assert [(p["agent_key"], p["host_id"], p["revision_id"], p["text"]) for p in role_parts] == [
        (r.agent_key, r.host_id, r.id, r.text) for r in selected
    ]
    expected = "\n\n".join(
        ["Own route" if route_independent else "Route"] + [r.text for r in selected]
    )
    if role == "parent":
        expected += "\n\nQuota"
    assert preview["text"] == expected
    assert preview["total_chars"] == len(expected)


@pytest.mark.parametrize("harness", ["codex-native", "claude-sdk", "opencode-native"])
@pytest.mark.parametrize("quota", ["", "Quota evidence"])
def test_preview_matches_composer_with_untrimmed_inner_layers(harness: str, quota: str) -> None:
    spec = AgentSpec(spec_version=1, instructions=" Author ", spawn=True)
    layers = [
        _revision("shared", "  Shared  "),
        _revision("agent", "  Agent  ", agent_key="codex"),
        _revision("host", "  Host  ", host_id="host_a"),
        _revision("parent", "  Parent  "),
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
    route = "  Shared  \n\n  Agent  \n\n  Host  \n\n  Parent  "
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
        "parent",
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
        _revision("parent", "Parent"),
        _revision("child", "Child"),
    ]
    parent = preview_instructions(
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
    assert "Route\n\nParent" in parent["text"]
    assert "Child" not in parent["text"]
    assert "Route\n\nChild" in child["text"]
    assert "Parent" not in child["text"]
    assert child["parts"][-2]["sent"] is True
    assert child["parts"][-2]["kind"] == "child_question"


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
        expected = native_startup_instructions(
            spec, global_instructions=folded, has_parent=True, peer_messaging_enabled=True
        )
    elif harness == "opencode-native":
        expected = build_instructions_nullable(
            spec, None, [], framework_instructions=[folded], has_parent=True
        )
    else:
        expected = build_instructions(
            spec, None, [], framework_instructions=[folded], has_parent=True
        )
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


@pytest.mark.parametrize(
    "harness",
    [
        "claude-native",
        "codex-native",
        "pi-native",
        "antigravity-native",
        "hermes-native",
        "qwen-native",
    ],
)
@pytest.mark.parametrize("has_parent", [False, True])
@pytest.mark.parametrize("peer", [False, True])
def test_native_preview_matches_delivered_startup(harness, has_parent, peer):
    spec = AgentSpec(spec_version=1, instructions=" Author ", spawn=True)
    layers = [_revision("shared", " Global ")]
    preview = preview_instructions(
        layers,
        host_id=None,
        agent_key=None,
        spec=spec,
        name="agent",
        harness=harness,
        has_parent=has_parent,
        spawn=True,
        peer=peer,
    )
    session = session_instruction_parts(
        " Global ",
        workspace=None,
        worktree=None,
        peer_messaging_enabled=peer,
        has_parent=has_parent,
    )
    from omnigent.runtime.prompt import native_startup_instruction_parts

    expected = "\n\n".join(
        part.text
        for part in native_startup_instruction_parts(
            spec,
            session,
            has_parent=has_parent
            and harness in {"claude-native", "codex-native", "pi-native", "antigravity-native"},
            peer_messaging_enabled=peer,
        )
        if part.text is not None
    )
    assert preview["text"] == expected
    assert preview["total_chars"] == len(expected)
