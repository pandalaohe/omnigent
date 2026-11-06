from __future__ import annotations

import pytest

from omnigent.harnesses.claude_compaction import (
    claude_auto_compact_token_limit,
    read_claude_auto_compact_token_limit,
)


@pytest.mark.parametrize(
    ("window", "settings", "env", "expected"),
    [
        (1_000_000, {}, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "550000"}, 517_000),
        (200_000, {"autoCompactWindow": 550_000}, {}, 167_000),
        (1_000_000, {}, {}, None),
        (1_000_000, {"autoCompactWindow": 400_000}, {}, 367_000),
        (
            1_000_000,
            {"autoCompactWindow": 400_000},
            {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "550000"},
            517_000,
        ),
        (1_000_000, {"autoCompactEnabled": False}, {}, None),
        (1_000_000, {}, {"DISABLE_AUTO_COMPACT": "1"}, None),
        (1_000_000, {}, {"DISABLE_COMPACT": "true"}, None),
        (
            1_000_000,
            {"autoCompactWindow": 1_000_000},
            {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"},
            490_000,
        ),
        (
            1_000_000,
            {"autoCompactWindow": 1_000_000},
            {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "nan"},
            967_000,
        ),
        (
            1_000_000,
            {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "550000"}},
            {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"},
            517_000,
        ),
        (
            1_000_000,
            {
                "autoCompactWindow": 550_000,
                "modelSettings": {"claude-opus-4-8": {"autoCompactWindow": 400_000}},
            },
            {},
            367_000,
        ),
    ],
)
def test_claude_compact_point_includes_both_reserves(window, settings, env, expected):
    assert (
        claude_auto_compact_token_limit(
            window, settings=settings, env=env, model="claude-opus-4-8[1m]"
        )
        == expected
    )


def test_claude_compact_point_reads_project_model_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "user-config")
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    (tmp_path / "user-config").mkdir()
    (tmp_path / "user-config/settings.json").write_text('{"autoCompactWindow":550000}')
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.local.json").write_text(
        '{"modelSettings":{"claude-opus-4-8":{"autoCompactWindow":400000}}}'
    )
    assert (
        read_claude_auto_compact_token_limit(1_000_000, cwd=tmp_path, model="claude-opus-4-8")
        == 367_000
    )
    (tmp_path / "user-config/.claude.json").write_text('{"autoCompactEnabled":false}')
    assert (
        read_claude_auto_compact_token_limit(1_000_000, cwd=tmp_path, model="claude-opus-4-8")
        is None
    )
    (tmp_path / "user-config/.claude.json").write_text("{}")
    (tmp_path / "user-config/settings.json").write_text(
        '{"modelSettings":{"claude-opus-4-8":{"autoCompactWindow":300000}}}'
    )
    (tmp_path / ".claude/settings.local.json").write_text('{"autoCompactWindow":450000}')
    assert (
        read_claude_auto_compact_token_limit(1_000_000, cwd=tmp_path, model="claude-opus-4-8")
        == 417_000
    )
