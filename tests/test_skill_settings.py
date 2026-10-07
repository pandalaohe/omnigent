"""Tests for omnigent.skill_settings."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from omnigent.skill_settings import HostSkillSettings, host_skill_settings
from omnigent.spec.types import SkillSpec
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import ReadSkillFileTool


@pytest.fixture()
def tool_ctx() -> ToolContext:
    """
    Dummy :class:`ToolContext` for the tool-refusal assertion.

    :returns: A :class:`ToolContext` with placeholder IDs.
    """
    return ToolContext(task_id="task_test", agent_id="agent_test")


def _write_config(config_home: Path, data: object) -> None:
    """Write *data* as YAML to ``<config_home>/config.yaml``."""
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(yaml.safe_dump(data))


def _write_bytes_config(config_home: Path, content: bytes) -> None:
    """Write raw *content* to ``<config_home>/config.yaml``."""
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_bytes(content)


def _linked_skill(tmp_path: Path) -> SkillSpec:
    """Build a skill whose ``BODY.md`` links outside the skill dir."""
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "SKILL.md").write_text("library body\n")
    skill_dir = tmp_path / "skills" / "s"
    skill_dir.mkdir(parents=True)
    (skill_dir / "BODY.md").symlink_to(lib / "SKILL.md")
    return SkillSpec(
        name="s",
        description="Linked skill.",
        content="Use s.",
        skill_dir=skill_dir,
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return warnings emitted by the skill-settings logger."""
    return [
        record
        for record in caplog.records
        if record.name == "omnigent.skill_settings" and record.levelno == logging.WARNING
    ]


def test_host_skill_settings_reads_valid_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A valid list is expanded, resolved, and returned in order.
    """
    home = tmp_path / "home"
    home.mkdir()
    home_lib = home / "lib"
    home_lib.mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    config_home = tmp_path / "config"
    _write_config(config_home, {"skills": {"trusted_link_roots": ["~/lib", str(lib)]}})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    settings = host_skill_settings()

    assert settings == HostSkillSettings(trusted_link_roots=(home_lib.resolve(), lib.resolve()))


@pytest.mark.parametrize(
    "config",
    [
        {"skills": {"trusted_link_roots": "/opt/lib"}},
        # A scalar "/" would trust the filesystem root if strings were iterated.
        {"skills": {"trusted_link_roots": "/"}},
        {"skills": {"trusted_link_roots": [1]}},
        {"skills": {"trusted_link_roots": [""]}},
        {"skills": {"trusted_link_roots": ["rel/lib"]}},
        {"skills": {"trusted_link_roots": ["~nosuchuser_skl001/lib"]}},
        {"skills": {"trusted_link_roots": ["~bad\x00user/lib"]}},
        {"skills": {"trusted_link_roots": ["/a\x00b"]}},
        {"skills": []},
    ],
    ids=[
        "roots-string",
        "roots-root-string",
        "int-entry",
        "empty-entry",
        "relative-entry",
        "unknown-user-entry",
        "nul-user-entry",
        "nul-entry",
        "skills-list",
    ],
)
def test_host_skill_settings_invalid_value_returns_empty(
    config: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Every invalid value fails closed to ``()`` with one warning.
    """
    config_home = tmp_path / "config"
    _write_config(config_home, config)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    with caplog.at_level(logging.WARNING, logger="omnigent.skill_settings"):
        settings = host_skill_settings()

    assert settings == HostSkillSettings(trusted_link_roots=())
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "skills.trusted_link_roots" in warnings[0].getMessage()


@pytest.mark.parametrize(
    "bad_config",
    [b"skills: [unclosed", b"\xff\xfe skills:", b"x: " + b"[" * 1200 + b"]" * 1200],
    ids=["invalid-yaml", "invalid-utf8", "nested-yaml"],
)
def test_host_skill_settings_bad_config_returns_empty_and_tool_still_refuses(
    bad_config: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    tool_ctx: ToolContext,
) -> None:
    """
    An unparseable config yields defaults and leaves the tool's
    default refusal intact.
    """
    config_home = tmp_path / "config"
    _write_bytes_config(config_home, bad_config)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    skill = _linked_skill(tmp_path)

    with caplog.at_level(logging.WARNING, logger="omnigent.skill_settings"):
        settings = host_skill_settings()
        result = ReadSkillFileTool([skill]).invoke(
            json.dumps({"skill_name": "s", "path": "BODY.md"}),
            tool_ctx,
        )

    assert settings == HostSkillSettings(trusted_link_roots=())
    assert result == "Error: path traversal not allowed"
    assert _warnings(caplog)


def test_host_skill_settings_relative_config_home_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A workspace-relative config location must not grant the setting.
    """
    project = tmp_path / "project"
    lib = tmp_path / "lib"
    lib.mkdir()
    _write_config(project / "config", {"skills": {"trusted_link_roots": [str(lib)]}})
    monkeypatch.chdir(project)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", "config")

    with caplog.at_level(logging.WARNING, logger="omnigent.skill_settings"):
        settings = host_skill_settings()

    assert settings == HostSkillSettings(trusted_link_roots=())
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "not absolute" in warnings[0].getMessage()
