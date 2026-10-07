"""Tests for omnigent.tools.builtins.read_skill_file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnigent.spec.types import SkillSpec
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import ReadSkillFileTool
from omnigent.tools.builtins.read_skill_file import _is_plain_relative


@pytest.fixture()
def skill_with_resources(tmp_path: Path) -> SkillSpec:
    """
    A skill with a ``references/`` directory containing a
    file, for testing ``read_skill_file``.

    :returns: A ``SkillSpec`` pointing at a real directory
        with a reference file.
    """
    skill_dir = tmp_path / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    refs_dir = skill_dir / "references"
    refs_dir.mkdir()
    (refs_dir / "style-guide.md").write_text("# Style Guide\n\nUse snake_case.")
    return SkillSpec(
        name="code-review",
        description="Reviews code.",
        content="Review the code.",
        skill_dir=skill_dir,
    )


@pytest.fixture()
def skill_no_resources() -> SkillSpec:
    """
    A skill with no ``skill_dir`` (in-memory only).

    :returns: A ``SkillSpec`` with ``skill_dir=None``.
    """
    return SkillSpec(
        name="summarize",
        description="Summarizes text.",
        content="Summarize the input concisely.",
    )


def test_read_skill_file_returns_content(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke reads a file from the skill dir.
    """
    tool = ReadSkillFileTool([skill_with_resources])
    result = tool.invoke(
        json.dumps(
            {
                "skill_name": "code-review",
                "path": "references/style-guide.md",
            }
        ),
        tool_ctx,
    )
    assert "# Style Guide" in result
    assert "snake_case" in result


def test_read_skill_file_unknown_skill(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke returns error for unknown skill.
    """
    tool = ReadSkillFileTool([skill_with_resources])
    result = tool.invoke(
        json.dumps(
            {
                "skill_name": "nonexistent",
                "path": "references/style-guide.md",
            }
        ),
        tool_ctx,
    )
    assert "not found" in result
    assert "code-review" in result


def test_read_skill_file_traversal_blocked(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke rejects path traversal attempts.
    """
    tool = ReadSkillFileTool([skill_with_resources])
    result = tool.invoke(
        json.dumps(
            {
                "skill_name": "code-review",
                "path": "../../etc/passwd",
            }
        ),
        tool_ctx,
    )
    assert "traversal not allowed" in result


def test_read_skill_file_absolute_path_blocked(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke rejects absolute paths.
    """
    tool = ReadSkillFileTool([skill_with_resources])
    result = tool.invoke(
        json.dumps(
            {
                "skill_name": "code-review",
                "path": "/etc/passwd",
            }
        ),
        tool_ctx,
    )
    assert "path must be relative" in result


def test_read_skill_file_not_found(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke returns error for missing files.
    """
    tool = ReadSkillFileTool([skill_with_resources])
    result = tool.invoke(
        json.dumps(
            {
                "skill_name": "code-review",
                "path": "references/nonexistent.md",
            }
        ),
        tool_ctx,
    )
    assert "file not found" in result


def test_read_skill_file_no_skill_dir(
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke returns error when skill has no
    directory on disk.
    """
    tool = ReadSkillFileTool([skill_no_resources])
    result = tool.invoke(
        json.dumps(
            {
                "skill_name": "summarize",
                "path": "references/foo.md",
            }
        ),
        tool_ctx,
    )
    assert "no directory on disk" in result


def test_read_skill_file_missing_arguments(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke returns error when required
    arguments are missing.
    """
    tool = ReadSkillFileTool([skill_with_resources])

    result_no_name = tool.invoke(
        json.dumps({"path": "references/style-guide.md"}),
        tool_ctx,
    )
    assert "missing required 'skill_name'" in result_no_name

    result_no_path = tool.invoke(
        json.dumps({"skill_name": "code-review"}),
        tool_ctx,
    )
    assert "missing required 'path'" in result_no_path


@pytest.mark.parametrize("arguments", ["not-json", "[]"])
def test_read_skill_file_rejects_invalid_arguments(
    arguments: str,
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    Malformed or non-object arguments return an error string.
    """
    tool = ReadSkillFileTool([skill_with_resources])
    result = tool.invoke(arguments, tool_ctx)

    assert result.startswith("Error:")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"skill_name": 123, "path": "references/style-guide.md"},
            "Error: 'skill_name' must be a string",
        ),
        (
            {"skill_name": "code-review", "path": 123},
            "Error: 'path' must be a string",
        ),
    ],
)
def test_read_skill_file_rejects_non_string_arguments(
    payload: dict[str, object],
    expected: str,
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    Resource lookup fields must be strings before path handling.
    """
    tool = ReadSkillFileTool([skill_with_resources])
    result = tool.invoke(json.dumps(payload), tool_ctx)

    assert result == expected


def _skill(name: str, skill_dir: Path) -> SkillSpec:
    """Build a minimal on-disk skill for link tests."""
    return SkillSpec(
        name=name,
        description=f"{name} skill.",
        content=f"Use {name}.",
        skill_dir=skill_dir,
    )


def _write_trusted_roots_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    roots: list[Path],
) -> None:
    """Point ``OMNIGENT_CONFIG_HOME`` at a config trusting *roots*."""
    config_home = tmp_path / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        yaml.safe_dump({"skills": {"trusted_link_roots": [str(root) for root in roots]}})
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))


def test_read_skill_file_follows_link_into_trusted_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool_ctx: ToolContext,
) -> None:
    """
    A skill's symlink into a host-trusted root is read.
    """
    lib = tmp_path / "lib"
    (lib / "x").mkdir(parents=True)
    (lib / "x" / "SKILL.md").write_text("# Linked body\n")
    skill_dir = tmp_path / "skills" / "s"
    skill_dir.mkdir(parents=True)
    (skill_dir / "BODY.md").symlink_to(lib / "x" / "SKILL.md")
    _write_trusted_roots_config(monkeypatch, tmp_path, [lib])
    tool = ReadSkillFileTool([_skill("s", skill_dir)])

    result = tool.invoke(
        json.dumps({"skill_name": "s", "path": "BODY.md"}),
        tool_ctx,
    )

    assert result == "# Linked body\n"


def test_read_skill_file_follows_linked_directory_into_trusted_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool_ctx: ToolContext,
) -> None:
    """
    A file under a symlinked directory inside a trusted root is read.
    """
    lib_refs = tmp_path / "lib" / "x" / "references"
    lib_refs.mkdir(parents=True)
    (lib_refs / "a.md").write_text("nested content\n")
    skill_dir = tmp_path / "skills" / "s"
    skill_dir.mkdir(parents=True)
    (skill_dir / "references").symlink_to(lib_refs)
    _write_trusted_roots_config(monkeypatch, tmp_path, [tmp_path / "lib"])
    tool = ReadSkillFileTool([_skill("s", skill_dir)])

    result = tool.invoke(
        json.dumps({"skill_name": "s", "path": "references/a.md"}),
        tool_ctx,
    )

    assert result == "nested content\n"


def test_read_skill_file_link_outside_skill_refused_without_trusted_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool_ctx: ToolContext,
) -> None:
    """
    Without a trusted-root setting, an out-of-skill link is refused.
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "SKILL.md").write_text("library body\n")
    skill_dir = tmp_path / "skills" / "s"
    skill_dir.mkdir(parents=True)
    (skill_dir / "BODY.md").symlink_to(lib / "SKILL.md")
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    tool = ReadSkillFileTool([_skill("s", skill_dir)])

    result = tool.invoke(
        json.dumps({"skill_name": "s", "path": "BODY.md"}),
        tool_ctx,
    )

    assert result == "Error: path traversal not allowed"


def test_read_skill_file_untrusted_link_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool_ctx: ToolContext,
) -> None:
    """
    A link that resolves outside every trusted root is refused.
    """
    lib_refs = tmp_path / "lib" / "x" / "references"
    lib_refs.mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("secret\n")
    (lib_refs / "evil").symlink_to(secret)
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "a.md").write_text("key\n")
    skill_dir = tmp_path / "skills" / "s"
    skill_dir.mkdir(parents=True)
    (skill_dir / "references").symlink_to(lib_refs)
    (skill_dir / "keys").symlink_to(keys)
    _write_trusted_roots_config(monkeypatch, tmp_path, [tmp_path / "lib"])
    tool = ReadSkillFileTool([_skill("s", skill_dir)])

    result_evil = tool.invoke(
        json.dumps({"skill_name": "s", "path": "references/evil"}),
        tool_ctx,
    )
    result_keys = tool.invoke(
        json.dumps({"skill_name": "s", "path": "keys/a.md"}),
        tool_ctx,
    )

    assert result_evil == "Error: path traversal not allowed"
    assert result_keys == "Error: path traversal not allowed"


def test_read_skill_file_path_text_cannot_steer_into_trusted_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool_ctx: ToolContext,
) -> None:
    """
    Only a link the skill contains may reach a trusted root; a path
    spelled with ``..`` or an absolute path never may.
    """
    lib = tmp_path / "lib"
    (lib / "y").mkdir(parents=True)
    (lib / "y" / "SKILL.md").write_text("linked body\n")
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "s"
    skill_dir.mkdir(parents=True)
    other_dir = skills_root / "other"
    other_dir.mkdir(parents=True)
    (other_dir / "SKILL.md").symlink_to(lib / "y" / "SKILL.md")
    _write_trusted_roots_config(monkeypatch, tmp_path, [lib])
    tool = ReadSkillFileTool([_skill("s", skill_dir)])

    sibling = tool.invoke(
        json.dumps({"skill_name": "s", "path": "../other/SKILL.md"}),
        tool_ctx,
    )
    relative = tool.invoke(
        json.dumps({"skill_name": "s", "path": "../../lib/y/SKILL.md"}),
        tool_ctx,
    )
    absolute = tool.invoke(
        json.dumps({"skill_name": "s", "path": str(lib / "y" / "SKILL.md")}),
        tool_ctx,
    )

    assert sibling == "Error: path traversal not allowed"
    assert relative == "Error: path traversal not allowed"
    assert absolute == "Error: path must be relative"


@pytest.mark.parametrize(
    "rel_path",
    [
        "../x",
        "a/../b",
        "/x",
        "\\x",
        "\\\\srv\\share\\x",
        "C:x.md",
        "C:\\x.md",
        "a\\..\\b",
    ],
)
def test_is_plain_relative_rejects_anchors_and_parent_parts(rel_path: str) -> None:
    """
    Anchored, drive-rooted, UNC, and ``..`` paths are not plain.
    """
    assert _is_plain_relative(rel_path) is False


@pytest.mark.parametrize("rel_path", ["BODY.md", "references/a.md", "./BODY.md"])
def test_is_plain_relative_accepts_plain_paths(rel_path: str) -> None:
    """
    Unanchored paths without ``..`` are plain.
    """
    assert _is_plain_relative(rel_path) is True
