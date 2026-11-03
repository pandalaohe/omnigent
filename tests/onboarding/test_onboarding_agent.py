"""Tests for the built-in onboarding agent spec and skills."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from omnigent.spec.parser import parse


def onboarding_agent_dir() -> Path:
    """
    Return the path to the built-in onboarding agent directory.

    :returns: Absolute path to ``omnigent/onboarding/agent/``.
    """
    return Path(__file__).parent.parent.parent / "omnigent" / "onboarding" / "agent"


def test_onboarding_agent_non_python_files_are_packaged() -> None:
    """The onboarding agent's runtime data must be included in the wheel."""
    agent_dir = onboarding_agent_dir()
    pyproject_path = Path(__file__).parents[2] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    package_data = pyproject["tool"]["setuptools"]["package-data"]
    patterns = package_data.get("omnigent.onboarding.agent", [])
    packaged = {
        path.relative_to(agent_dir).as_posix()
        for pattern in patterns
        for path in agent_dir.glob(pattern)
        if path.is_file()
    }
    expected = {
        "AGENTS.md",
        "config.yaml",
        "skills/build-omnigent/SKILL.md",
        "skills/detect-framework/SKILL.md",
        "skills/omnigent-knowledge/SKILL.md",
    }
    assert expected <= packaged


def test_onboarding_agent_parses_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The onboarding agent config.yaml must parse without errors."""
    monkeypatch.setenv("AP_ONBOARDING_MODEL", "anthropic/claude-sonnet-4-20250514")
    monkeypatch.setenv("AP_ONBOARDING_API_KEY", "sk-test")
    spec = parse(onboarding_agent_dir())
    assert spec.name == "onboarding-buddy"


def test_onboarding_agent_has_expected_skills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The onboarding agent must have all three designed skills."""
    monkeypatch.setenv("AP_ONBOARDING_MODEL", "test/model")
    monkeypatch.setenv("AP_ONBOARDING_API_KEY", "test")
    spec = parse(onboarding_agent_dir())
    skill_names = sorted(s.name for s in spec.skills)
    # These are the three skills defined in the design doc, in sorted order.
    assert skill_names == [
        "build-omnigent",
        "detect-framework",
        "omnigent-knowledge",
    ], f"Expected exactly the three designed skills, got {skill_names}."


def test_onboarding_agent_has_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The onboarding agent must load its AGENTS.md instructions."""
    monkeypatch.setenv("AP_ONBOARDING_MODEL", "test/model")
    monkeypatch.setenv("AP_ONBOARDING_API_KEY", "test")
    spec = parse(onboarding_agent_dir())
    # Instructions should mention the onboarding purpose.
    assert "onboarding" in spec.instructions.lower(), (
        "AGENTS.md should mention 'onboarding' in its instructions."
    )


def test_onboarding_agent_skills_have_descriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every skill must have a non-empty description."""
    monkeypatch.setenv("AP_ONBOARDING_MODEL", "test/model")
    monkeypatch.setenv("AP_ONBOARDING_API_KEY", "test")
    spec = parse(onboarding_agent_dir())
    for skill in spec.skills:
        assert skill.description, f"Skill {skill.name!r} has empty description."


def test_onboarding_agent_skills_have_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every skill must have non-trivial content (> 100 chars)."""
    monkeypatch.setenv("AP_ONBOARDING_MODEL", "test/model")
    monkeypatch.setenv("AP_ONBOARDING_API_KEY", "test")
    spec = parse(onboarding_agent_dir())
    for skill in spec.skills:
        # 100 chars is the minimum for a meaningful skill body;
        # shorter means the skill is likely a stub.
        assert len(skill.content) > 100, (
            f"Skill {skill.name!r} content is too short "
            f"({len(skill.content)} chars). Skills should have "
            f"detailed instructions."
        )
