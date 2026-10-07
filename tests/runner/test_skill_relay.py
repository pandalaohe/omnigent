"""Skill tools reach native harnesses through the relay.

A native session ignores ``request.tools`` and sees only
``build_native_relay_tool_schemas``. ``load_skill`` is what discovers
host-scope skills — ``.agents/skills``, ``.claude/skills`` and their
home-directory equivalents — so if it is absent from the relay, a skill dropped
where the docs say every agent picks it up is invisible to every native
harness, silently.

The per-family sources do not cover it: codex walks its bundle plus
``~/.codex/skills``, cursor walks ``~/.cursor/skills``, and the agy provider
skips the generic walk entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.tool_dispatch import (
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _SKILL_TOOLS,
    _execute_skill_tool,
    _granted_tool_names,
    build_native_relay_tool_schemas,
    session_skill_registry,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec


def test_skill_tools_are_in_the_native_relay_union() -> None:
    """Without this membership the relay filters skill schemas out."""
    assert _SKILL_TOOLS <= _NATIVE_RELAY_BUILTIN_TOOLS


def test_load_skill_reaches_a_bare_spec() -> None:
    """A spec declaring no skills of its own still gets ``load_skill``.

    That is the point rather than an oversight: ``ToolManager`` registers
    ``load_skill`` unconditionally *because* its discovery covers host-scope
    directories, which an agent does not declare. Gating it on bundled skills
    would leave exactly the documented case — drop a folder in
    ``~/.agents/skills`` and every agent picks it up — still broken.
    """
    schemas = build_native_relay_tool_schemas(AgentSpec(spec_version=1))

    names = {schema["name"] for schema in schemas}
    assert "load_skill" in names


def test_relayed_skill_schemas_are_the_flat_shape() -> None:
    """The bridges consume ``{name, description, parameters}`` directly.

    A schema that arrives nested or without parameters is one the harness
    cannot register, which fails at spawn rather than at call time.

    Pinned on ``load_skill`` alone: ``ToolManager`` registers that one
    unconditionally, while ``read_skill_file`` is meant to appear only once a
    skill actually has resources to read.
    """
    schemas = build_native_relay_tool_schemas(AgentSpec(spec_version=1))
    relayed = {s["name"]: s for s in schemas if s["name"] in _SKILL_TOOLS}

    assert "load_skill" in relayed
    for schema in relayed.values():
        assert schema["description"]
        parameters = schema["parameters"]
        assert isinstance(parameters, dict)
        assert parameters["type"] == "object"


def test_relayed_load_skill_discovers_a_host_scope_skill(tmp_path: Path) -> None:
    """The relayed tool reaches the directories the docs advertise.

    Membership in the union is only half the claim; the other half is that
    ``load_skill`` run from a native session's workspace actually finds a skill
    dropped in ``.claude/skills``, which is what makes relaying it worth doing.
    """
    from omnigent.runner.tool_dispatch import _execute_skill_tool

    skill_dir = tmp_path / ".claude" / "skills" / "host-only"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: host-only\ndescription: a host-scope skill\n---\n\nthe skill body\n"
    )

    loaded = _execute_skill_tool(
        "load_skill",
        {"name": "host-only"},
        agent_spec=None,
        runner_workspace=tmp_path,
    )

    assert "the skill body" in loaded, loaded[:300]


def _agent_spec(harness: str) -> AgentSpec:
    """A skill-less spec carrying *harness* as its executor kind."""
    return AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )


def _write_skill(skills_dir: Path, name: str, body: str) -> None:
    """Write ``<skills_dir>/<name>/SKILL.md`` with *body* as its content."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} desc\n---\n{body}\n"
    )


def _seed_split_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin a home where Claude and Codex mounts carry different skills.

    ``api-design`` exists only under ``~/.agents/skills`` (Codex's mount);
    ``plan`` exists in both mounts with distinct bodies.
    """
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_skill(home / ".agents" / "skills", "api-design", "agents api body")
    _write_skill(home / ".claude" / "skills", "plan", "claude plan body")
    _write_skill(home / ".agents" / "skills", "plan", "agents plan body")


def test_execute_skill_tool_claude_registry_ignores_agents_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claude-native session's registry never lists Codex-only skills."""
    _seed_split_home(tmp_path, monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = _agent_spec("claude-native")

    plan = _execute_skill_tool(
        "load_skill", {"name": "plan"}, agent_spec=spec, runner_workspace=workspace
    )
    api = _execute_skill_tool(
        "load_skill", {"name": "api-design"}, agent_spec=spec, runner_workspace=workspace
    )

    assert "claude plan body" in plan
    assert "skill 'api-design' not found" in api


def test_execute_skill_tool_codex_registry_reads_agents_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A codex-native session's registry lists ``~/.agents/skills`` and not Claude's."""
    _seed_split_home(tmp_path, monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = _agent_spec("codex-native")

    plan = _execute_skill_tool(
        "load_skill", {"name": "plan"}, agent_spec=spec, runner_workspace=workspace
    )
    api = _execute_skill_tool(
        "load_skill", {"name": "api-design"}, agent_spec=spec, runner_workspace=workspace
    )

    assert "agents plan body" in plan
    assert "agents api body" in api


def test_execute_skill_tool_effective_harness_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session's harness override selects the overridden family's registry."""
    _seed_split_home(tmp_path, monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = _agent_spec("claude-native")

    plan = _execute_skill_tool(
        "load_skill",
        {"name": "plan"},
        agent_spec=spec,
        runner_workspace=workspace,
        effective_harness="codex-native",
    )
    api = _execute_skill_tool(
        "load_skill",
        {"name": "api-design"},
        agent_spec=spec,
        runner_workspace=workspace,
        effective_harness="codex-native",
    )

    assert "agents plan body" in plan
    assert "agents api body" in api


def test_relayed_load_skill_description_uses_the_given_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advertised description lists exactly the session registry's names."""
    _seed_split_home(tmp_path, monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = _agent_spec("claude-native")
    registry = session_skill_registry(spec, "claude-native", workspace, None)

    schemas = build_native_relay_tool_schemas(spec, skill_registry=registry)
    load_schema = next(schema for schema in schemas if schema["name"] == "load_skill")
    description = load_schema["description"]

    assert "plan" in description
    assert "api-design" not in description


def test_session_registry_injects_build_omnigent_into_the_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill-less spec's registry and its advertised description carry build-omnigent."""
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = _agent_spec("claude-native")

    registry = session_skill_registry(spec, "claude-native", workspace, None)
    schemas = build_native_relay_tool_schemas(spec, skill_registry=registry)
    load_schema = next(schema for schema in schemas if schema["name"] == "load_skill")

    assert "build-omnigent" in [s.name for s in registry]
    assert "build-omnigent" in load_schema["description"]


def test_grant_pairs_read_skill_file_with_load_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec with no resource skills still grants read_skill_file with load_skill."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    spec = AgentSpec(spec_version=1, skills_filter="none")

    granted = _granted_tool_names(spec, "claude-native")

    assert {"load_skill", "read_skill_file"} <= granted


def test_execute_skill_tool_loads_a_bundle_host_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host skill under the bundle workdir loads for a claude-native session."""
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bundle = tmp_path / "bundle"
    _write_skill(bundle / ".claude" / "skills", "bundle-host", "bundle host body")
    spec = _agent_spec("claude-native")

    loaded = _execute_skill_tool(
        "load_skill",
        {"name": "bundle-host"},
        agent_spec=spec,
        runner_workspace=workspace,
        bundle_workdir=bundle,
    )

    assert "bundle host body" in loaded
