"""Tests for ``omnigent.chat._merge_host_skills``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.chat import _merge_host_skills


def _write_skill(skills_dir: Path, name: str) -> None:
    """Write a minimal ``<skills_dir>/<name>/SKILL.md`` with valid frontmatter."""
    d = skills_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} desc\n---\nbody\n")


def _spec(harness_kind: str) -> SimpleNamespace:
    """Build the spec shape ``_merge_host_skills`` reads."""
    return SimpleNamespace(
        skills=[],
        skills_filter="all",
        executor=SimpleNamespace(harness_kind=harness_kind),
    )


def _write_off_config(config_home: Path) -> None:
    """Write a config turning the Claude portable-skills switch off."""
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text("skills:\n  claude_portable_skills: false\n")


def test_merge_host_skills_claude_switch_off_drops_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Claude-family spec respects the host switch; codex keeps its walk."""
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    agent = tmp_path / "agent"
    _write_skill(agent / ".agents" / "skills", "foo")
    _write_skill(agent / ".claude" / "skills", "bar")
    _write_off_config(tmp_path / "config")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))

    claude_names = {s.name for s in _merge_host_skills(_spec("claude-sdk"), agent)}
    codex_names = {s.name for s in _merge_host_skills(_spec("codex"), agent)}

    assert claude_names == {"bar"}
    assert codex_names == {"foo", "bar"}


def test_merge_host_skills_claude_switch_on_keeps_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no switch set, the Claude-family generic walk keeps ``.agents``."""
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    agent = tmp_path / "agent"
    _write_skill(agent / ".agents" / "skills", "foo")
    _write_skill(agent / ".claude" / "skills", "bar")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "empty-config"))

    names = {s.name for s in _merge_host_skills(_spec("claude-sdk"), agent)}

    assert names == {"foo", "bar"}
