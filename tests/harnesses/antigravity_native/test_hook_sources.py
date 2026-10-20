"""Documented hook sources are a restricted-startup boundary, not executed fixtures."""

import json
from pathlib import Path

import pytest

from omnigent.harnesses.antigravity_native.bridge import (
    _agy_guard_hook,
    agy_gemini_dir,
    seed_isolated_agy_home,
)
from omnigent.harnesses.antigravity_native.hook_sources import check_agy_hook_sources


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


@pytest.fixture
def sources(tmp_path, monkeypatch):
    home, project, bridge = (tmp_path / name for name in ("home", "project", "bridge"))
    project.mkdir()
    (project / ".git").write_text("gitdir: /opt/work/repository/.git/worktrees/demo")
    workspace = project / "subdir"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home / ".gemini", project, workspace, bridge


def pre_hook(*, enabled=True):
    return {
        "argument-rewriter": {
            "enabled": enabled,
            "PreToolUse": [{"matcher": ".*", "hooks": [{"command": "true"}]}],
        }
    }


@pytest.mark.parametrize("tier", ["ask", "plan", "accept-edits", "auto"])
@pytest.mark.parametrize(
    "source",
    [
        "global",
        "inline",
        "effective-inline",
        "effective-hooks",
        ".agents",
        ".agent",
        "_agents",
        "_agent",
        "cwd",
        "automatic",
        "antigravity-manifest",
        "gemini-manifest",
        "declared",
        "inherited",
        "imported",
    ],
)
def test_restricted_start_checks_every_documented_source(sources, tier, source):
    gemini, project, workspace, bridge = sources
    root = gemini / "config"
    path = root / "hooks.json"
    if source in ("inline", "effective-inline"):
        base = gemini if source == "inline" else agy_gemini_dir(bridge)
        path = write_json(base / "antigravity-cli/settings.json", {"hooks": pre_hook()})
    else:
        if source == "effective-hooks":
            path = agy_gemini_dir(bridge) / "config/hooks.json"
        elif source in (".agents", ".agent", "_agents", "_agent", "cwd"):
            path = (
                (workspace if source == "cwd" else project)
                / (".agents" if source == "cwd" else source)
                / "hooks.json"
            )
        elif source in ("automatic", "antigravity-manifest", "gemini-manifest", "imported"):
            plugin = root / "plugins/demo"
            if source != "imported":
                manifest = {
                    "automatic": "plugin.json",
                    "antigravity-manifest": ".antigravity-plugin/plugin.json",
                    "gemini-manifest": ".gemini-plugin/plugin.json",
                }[source]
                write_json(plugin / manifest, {"name": "demo"})
            else:
                write_json(
                    root / "import_manifest.json",
                    {"imports": [{"name": "demo", "components": ["hooks"]}]},
                )
            path = plugin / "hooks.json"
        elif source in ("declared", "inherited"):
            path = project / "shared-plugins/demo/hooks.json"
            config = {"entries": [{"path": "shared-plugins", "include_only": ["demo"]}]}
            if source == "inherited":
                write_json(project / "shared/plugins.json", config)
                config = {"inherits": [{"path": "shared/plugins.json"}]}
            write_json(project / ".agents/plugins.json", config)
        write_json(path, pre_hook())
    before = path.read_bytes()
    with pytest.raises(ValueError, match="argument-rewriter") as error:
        seed_isolated_agy_home(bridge, trusted_workspace=workspace, permission_tier=tier)
    assert str(path) in str(error.value)
    assert "full" in str(error.value) and '"enabled": false' in str(error.value)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "event", ["PostToolUse", "SessionStart", "PreInvocation", "PostInvocation", "Stop"]
)
def test_other_events_and_disabled_pre_tool_hooks_do_not_block(sources, event):
    gemini, project, workspace, bridge = sources
    hooks = {**pre_hook(enabled=False), "other-event": {event: [{"command": "true"}]}}
    write_json(project / ".agents/hooks.json", hooks)
    write_json(gemini / "antigravity-cli/settings.json", {"hooks": hooks})
    seed_isolated_agy_home(bridge, trusted_workspace=workspace, permission_tier="ask")


@pytest.mark.parametrize(
    "fault",
    [
        "invalid-json",
        "wrong-shape",
        "duplicate-key",
        "unreadable",
        "broken-alias",
        "cycle",
        "missing-declaration",
        "unreadable-plugin-dir",
    ],
)
def test_source_discovery_errors_refuse_without_contents(sources, monkeypatch, fault):
    gemini, project, workspace, bridge = sources
    source = project / ".agents/hooks.json"
    write_json(source, {})
    if fault == "invalid-json":
        source.write_text("private-sentinel: {")
    elif fault == "wrong-shape":
        source.write_text("[]")
    elif fault == "duplicate-key":
        source.write_text('{"x":{}, "x":{"enabled":false}}')
    elif fault == "unreadable":
        read = Path.read_text

        def fail(path, *args, **kwargs):
            if path == source:
                raise PermissionError("private-sentinel")
            return read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail)
    elif fault == "broken-alias":
        source = project / "_agents"
        source.symlink_to(project / "absent")
    elif fault in ("cycle", "missing-declaration"):
        source = project / ".agents/plugins.json"
        write_json(
            source,
            {
                "inherits": [
                    {"path": str(source if fault == "cycle" else project / "missing.json")}
                ]
            },
        )
    else:
        source = gemini / "config/plugins"
        source.mkdir(parents=True)
        iterdir = Path.iterdir

        def fail_dir(path):
            if path == source:
                raise PermissionError("private-sentinel")
            return iterdir(path)

        monkeypatch.setattr(Path, "iterdir", fail_dir)
    with pytest.raises(ValueError) as error:
        check_agy_hook_sources(workspace, "auto", bridge)
    expected_source = project / "missing.json" if fault == "missing-declaration" else source
    assert str(expected_source) in str(error.value)
    assert "private-sentinel" not in str(error.value)


def test_full_skips_discovery_and_owned_guard_is_exact(sources):
    gemini, _project, workspace, bridge = sources
    original = write_json(gemini / "config/hooks.json", pre_hook())
    check_agy_hook_sources(workspace, "full", bridge)
    original.write_text("not json")
    check_agy_hook_sources(workspace, "full", bridge)
    original.unlink()
    path = write_json(
        agy_gemini_dir(bridge) / "config/hooks.json", {"omnigent-guard": _agy_guard_hook(bridge)}
    )
    check_agy_hook_sources(workspace, "auto", bridge)
    write_json(path, {"omnigent-guard": pre_hook()["argument-rewriter"]})
    with pytest.raises(ValueError, match="omnigent-guard"):
        check_agy_hook_sources(workspace, "auto", bridge)


def test_parent_above_repository_and_disabled_plugins_are_not_loaded(sources):
    gemini, project, workspace, bridge = sources
    write_json(project.parent / ".agents/hooks.json", pre_hook())
    plugin = gemini / "config/plugins/demo"
    write_json(plugin / "plugin.json", {"disabled": True})
    write_json(plugin / "hooks.json", pre_hook())
    check_agy_hook_sources(workspace, "auto", bridge)
    write_json(gemini / "antigravity-cli/config.json", {"plugins": {"demo": {"enabled": True}}})
    with pytest.raises(ValueError, match="argument-rewriter"):
        check_agy_hook_sources(workspace, "auto", bridge)


def test_original_plugin_disable_cannot_hide_enabled_effective_plugin(sources):
    gemini, _project, workspace, bridge = sources
    plugin = gemini / "config/plugins/demo"
    write_json(plugin / "plugin.json", {})
    write_json(plugin / "hooks.json", pre_hook())
    write_json(gemini / "antigravity-cli/config.json", {"plugins": {"demo": {"enabled": False}}})
    # The real root is disabled, but seeding exposes the plugin without its config.json.
    with pytest.raises(ValueError, match="argument-rewriter"):
        seed_isolated_agy_home(bridge, trusted_workspace=workspace, permission_tier="auto")
    effective = agy_gemini_dir(bridge)
    write_json(
        effective / "antigravity-cli/config.json", {"plugins": {"demo": {"enabled": False}}}
    )
    check_agy_hook_sources(workspace, "auto", bridge)


@pytest.mark.parametrize("filters", [{"include_only": ["other*"]}, {"exclude": ["demo"]}])
def test_unproven_plugin_filters_never_exempt_pre_tool_hooks(sources, filters):
    _gemini, project, workspace, bridge = sources
    write_json(project / "shared/demo/hooks.json", pre_hook())
    write_json(project / ".agents/plugins.json", {"entries": [{"path": "shared", **filters}]})
    with pytest.raises(ValueError, match="argument-rewriter"):
        check_agy_hook_sources(workspace, "auto", bridge)
