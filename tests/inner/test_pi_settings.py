"""Tests for managed Pi agent dir seeding (extensions / packages)."""

from __future__ import annotations

import json
import ntpath
import os
import shutil
import subprocess
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from omnigent.inner.pi_settings import prepare_managed_pi_agent_dir


def test_default_thinking_and_per_model_compaction_survive_settings_merge(tmp_path, monkeypatch):
    from omnigent.inner import pi_settings

    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "settings.json").write_text(
        json.dumps(
            {
                "defaultThinkingLevel": "high",
                "modelThinkingLevels": {"databricks-completions/kimi-k2": "xhigh"},
                "compaction": {
                    "modelOverrides": {
                        "databricks-completions/kimi-k2": {"reserveTokens": 32000},
                        "databricks-completions/deepseek-v4": {"reserveTokens": 64000},
                    }
                },
            }
        )
    )
    project_dir = tmp_path / "project" / ".pi"
    project_dir.mkdir(parents=True)
    (project_dir / "settings.json").write_text('{"defaultThinkingLevel": "low"}')
    monkeypatch.setattr(pi_settings, "DEFAULT_PI_AGENT_DIR", global_dir)
    managed = tmp_path / "managed"
    prepare_managed_pi_agent_dir(managed, overlay={"retry": {"maxRetries": 2}})
    overrides = json.loads((managed / "settings.json").read_text())["compaction"]["modelOverrides"]
    assert overrides["databricks-completions/kimi-k2"]["reserveTokens"] == 32000
    assert overrides["databricks-completions/deepseek-v4"]["reserveTokens"] == 64000
    assert pi_settings.default_pi_thinking_level(str(tmp_path / "project")) == "low"
    assert (
        pi_settings.default_pi_thinking_level(
            str(tmp_path / "project"), "databricks-completions/kimi-k2"
        )
        == "xhigh"
    )


def test_prepare_managed_pi_agent_dir_copies_settings_and_symlinks_npm(
    tmp_path: Path,
) -> None:
    """Gateway managed dir gets global settings metadata and npm install tree."""
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    (global_agent / "settings.json").write_text(
        json.dumps(
            {
                "extensions": ["/tmp/my-ext.ts"],
                "packages": ["npm:@foo/bar"],
            }
        ),
        encoding="utf-8",
    )
    npm_dir = global_agent / "npm" / "foo"
    npm_dir.mkdir(parents=True)

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "models.json").write_text("{}", encoding="utf-8")

    prepare_managed_pi_agent_dir(
        managed,
        overlay={"retry": {"maxRetries": 5}},
        global_agent_dir=global_agent,
    )

    written = json.loads((managed / "settings.json").read_text(encoding="utf-8"))
    assert written["extensions"] == ["/tmp/my-ext.ts"]
    assert written["packages"] == ["npm:@foo/bar"]
    assert written["retry"] == {"maxRetries": 5}
    assert (managed / "npm").is_symlink()
    assert (managed / "npm").resolve() == (global_agent / "npm").resolve()


def test_prepare_managed_pi_agent_dir_empty_global_writes_overlay_only(
    tmp_path: Path,
) -> None:
    """Missing global settings still writes the Omnigent overlay."""
    global_agent = tmp_path / "empty-global"
    global_agent.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()

    prepare_managed_pi_agent_dir(
        managed,
        overlay={"retry": {"enabled": True}},
        global_agent_dir=global_agent,
    )

    written = json.loads((managed / "settings.json").read_text(encoding="utf-8"))
    assert written == {"retry": {"enabled": True}}


def test_prepare_managed_pi_agent_dir_deep_merges_nested_overlay(tmp_path: Path) -> None:
    """Nested settings (e.g. compaction) merge like Pi project overrides."""
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    (global_agent / "settings.json").write_text(
        json.dumps({"compaction": {"enabled": True, "reserveTokens": 16384}}),
        encoding="utf-8",
    )
    managed = tmp_path / "managed"
    managed.mkdir()

    prepare_managed_pi_agent_dir(
        managed,
        overlay={"compaction": {"reserveTokens": 8192}},
        global_agent_dir=global_agent,
    )

    written = json.loads((managed / "settings.json").read_text(encoding="utf-8"))
    assert written["compaction"] == {
        "enabled": True,
        "reserveTokens": 8192,
    }


@pytest.mark.parametrize(
    "context_name", ["AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"]
)
def test_managed_context_candidates_and_existing_entries(tmp_path, context_name):
    source = tmp_path / "source"
    source.mkdir()
    context = tmp_path / "context.md"
    context.write_text("global context")
    (source / context_name).symlink_to(context)
    (source / "mcp.json").write_text('{"mcpServers":{}}')
    (source / "mcp.json").chmod(0o600)
    (source / "skills").mkdir()
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (source / "prompts").symlink_to(prompts, target_is_directory=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "SYSTEM.md").write_text("managed system")
    (source / "SYSTEM.md").write_text("global system")
    (managed / "skills").symlink_to(tmp_path / "missing", target_is_directory=True)
    (managed / "models.json").write_text("{}")
    (managed / "auth.json").write_text("{}")
    # The seeder must not read or link unrelated model/auth/session state.
    for name in ("auth.json", "models.json", "sessions"):
        (source / name).symlink_to(tmp_path / "missing")

    prepare_managed_pi_agent_dir(managed, global_agent_dir=source)

    assert (managed / context_name).read_text() == "global context"
    assert (managed / "SYSTEM.md").read_text() == "managed system"
    assert (managed / "skills").readlink() == tmp_path / "missing"
    assert (managed / "prompts").resolve() == prompts
    assert not (managed / "mcp.json").is_symlink()
    assert (managed / "mcp.json").stat().st_mode & 0o777 == 0o600
    (managed / "mcp.json").write_text("{}")
    assert (source / "mcp.json").read_text() == '{"mcpServers":{}}'
    assert not (managed / "auth.json").is_symlink()
    assert not (managed / "models.json").is_symlink()
    assert (managed / "auth.json").read_text() == "{}"
    assert (managed / "models.json").read_text() == "{}"
    assert not (managed / "sessions").is_symlink()


@pytest.mark.parametrize("source_form", ["path", "tilde", "uri"])
def test_managed_source_honors_pi_agent_dir(tmp_path, monkeypatch, source_form):
    source = tmp_path / "agent source"
    source.mkdir()
    (source / "AGENTS.md").write_text("override context")
    monkeypatch.setenv("HOME", str(tmp_path))
    value = {"path": str(source), "tilde": "~/agent source", "uri": source.as_uri()}[source_form]
    monkeypatch.setenv("PI_CODING_AGENT_DIR", value)
    managed = tmp_path / "managed"

    prepare_managed_pi_agent_dir(managed)

    assert (managed / "AGENTS.md").read_text() == "override context"


def test_reseeding_refreshes_owned_files_and_preserves_managed_edits(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "AGENTS.md").write_text("fallback first")
    (source / "AGENTS.override.md").write_text("override first")
    (source / "SYSTEM.md").write_text("system first")
    (source / "APPEND_SYSTEM.md").write_text("append first")
    (source / "mcp.json").write_text('{"mcpServers":{}}')
    managed = tmp_path / "managed"
    prepare_managed_pi_agent_dir(managed, global_agent_dir=source)

    (source / "AGENTS.md").write_text("fallback second")
    (source / "AGENTS.override.md").write_text("override second")
    (source / "SYSTEM.md").write_text("system second")
    (source / "APPEND_SYSTEM.md").write_text("append second")
    (source / "mcp.json").write_text('{"mcpServers":{},"autoEnableCodemode":false}')
    prepare_managed_pi_agent_dir(managed, global_agent_dir=source)

    assert (managed / "AGENTS.md").read_text() == "fallback second"
    assert (managed / "AGENTS.override.md").read_text() == "override second"
    assert (managed / "SYSTEM.md").read_text() == "system second"
    assert (managed / "APPEND_SYSTEM.md").read_text() == "append second"
    assert json.loads((managed / "mcp.json").read_text())["autoEnableCodemode"] is False

    (source / "AGENTS.override.md").unlink()
    prepare_managed_pi_agent_dir(managed, global_agent_dir=source)
    assert not (managed / "AGENTS.override.md").exists()
    assert (managed / "AGENTS.md").read_text() == "fallback second"

    (managed / "AGENTS.override.md").write_text("managed override")
    (managed / "SYSTEM.md").write_text("managed system")
    (managed / "mcp.json").write_text('{"mcpServers":{},"autoEnableCodemode":true}')
    (managed / "APPEND_SYSTEM.md").unlink()
    append = tmp_path / "managed-append.md"
    append.write_text("managed append")
    (managed / "APPEND_SYSTEM.md").symlink_to(append)
    (source / "AGENTS.override.md").write_text("source override")
    (source / "SYSTEM.md").unlink()
    (source / "AGENTS.md").unlink()
    (source / "CLAUDE.MD").write_text("new fallback")
    prepare_managed_pi_agent_dir(managed, global_agent_dir=source)

    assert (managed / "AGENTS.override.md").read_text() == "managed override"
    assert (managed / "SYSTEM.md").read_text() == "managed system"
    assert (managed / "APPEND_SYSTEM.md").is_symlink()
    assert (managed / "APPEND_SYSTEM.md").read_text() == "managed append"
    assert json.loads((managed / "mcp.json").read_text())["autoEnableCodemode"] is True
    assert json.loads((source / "mcp.json").read_text())["autoEnableCodemode"] is False
    assert not (managed / "AGENTS.md").exists()
    assert (managed / "CLAUDE.MD").read_text() == "new fallback"


@pytest.mark.parametrize("failure", ["resource-replacement", "manifest-finalization"])
def test_reseeding_recovers_from_interrupted_replacement(tmp_path, monkeypatch, failure):
    from omnigent.inner import pi_settings

    source = tmp_path / "source"
    source.mkdir()
    (source / "mcp.json").write_text("{}")
    managed = tmp_path / "managed"
    prepare_managed_pi_agent_dir(managed, global_agent_dir=source)
    (source / "mcp.json").write_text('{"mcpServers":{}}')
    replace = os.replace
    resource_replaced = False

    def interrupt(temporary, target):
        nonlocal resource_replaced
        if Path(target) == managed / "mcp.json":
            if failure == "resource-replacement":
                raise OSError("fixture replacement interruption")
            resource_replaced = True
        elif resource_replaced and Path(target).name == pi_settings._PI_SEED_MANIFEST:
            raise OSError("fixture manifest interruption")
        return replace(temporary, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt)
        prepare_managed_pi_agent_dir(managed, global_agent_dir=source)

    expected = "{}" if failure == "resource-replacement" else '{"mcpServers":{}}'
    assert (managed / "mcp.json").read_text() == expected
    (source / "mcp.json").write_text('{"mcpServers":{},"autoEnableCodemode":false}')
    prepare_managed_pi_agent_dir(managed, global_agent_dir=source)
    assert json.loads((managed / "mcp.json").read_text())["autoEnableCodemode"] is False


@pytest.mark.parametrize(
    "value,source_root,expected",
    [
        (
            "file://server/share/pi%20agent/extensions/skip.js",
            r"\\server\share\pi agent",
            r"\\server\share\pi agent\extensions\skip.js",
        ),
        (
            "file:///D:/pi-agent/extensions/skip.js",
            r"D:\pi-agent",
            r"D:\pi-agent\extensions\skip.js",
        ),
        ("/d/pi-agent/extensions/skip.js", r"D:\pi-agent", r"D:\pi-agent\extensions\skip.js"),
        ("/mnt/d/pi-agent/extensions/skip.js", r"D:\pi-agent", r"D:\pi-agent\extensions\skip.js"),
        (
            "/cygdrive/d/pi-agent/extensions/skip.js",
            r"D:\pi-agent",
            r"D:\pi-agent\extensions\skip.js",
        ),
    ],
)
def test_windows_resource_paths_and_filters(value, source_root, expected, monkeypatch):
    from omnigent.inner import pi_settings

    # Inject path semantics only; never access a Windows filesystem from this host.
    monkeypatch.setattr(pi_settings, "os", SimpleNamespace(name="nt", path=ntpath, sep="\\"))
    root = PureWindowsPath(source_root)
    assert pi_settings._normalize_pi_path(value) == expected
    assert pi_settings._resource_path(value, root) == expected
    settings = {"extensions": [value, "-" + value]}
    pi_settings._relocate_resource_settings(settings, root, PureWindowsPath(r"C:\managed"))
    assert settings["extensions"][0] == expected
    assert r"-C:\managed\extensions\skip.js" in settings["extensions"]


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.parametrize("route", ["own", "full", "ordered"])
def test_inline_guard_follows_actual_sdk_extensions(tmp_path, monkeypatch, trusted, route):
    """Use Pi's consumer for context, MCP, resources, trust and guard ordering."""

    from omnigent.inner.pi_settings import ordered_pi_launch

    pi = shutil.which("pi")
    node = shutil.which("node")
    if pi is None or node is None:
        pytest.skip("installed Pi SDK and Node required")
    dist = next(
        parent
        for parent in Path(pi).resolve().parents
        if (parent / "core" / "resource-loader.js").is_file()
    )
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    (agent / "extensions").mkdir(parents=True)
    (project / ".pi" / "extensions").mkdir(parents=True)
    (project / ".git").mkdir()  # Stop trusted skill discovery at the fixture's boundary.
    user = agent / "extensions" / "user.js"
    project_extension = project / ".pi" / "extensions" / "project.js"
    package = agent / "local-package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"pi": {"extensions": ["index.js"]}}))
    package_extension = package / "index.js"
    relative_extension = agent / "relative.js"
    (agent / "AGENTS.override.md").write_text("GLOBAL_OVERRIDE_CONTEXT")
    (agent / "AGENTS.md").write_text("SHADOWED_GLOBAL_CONTEXT")
    (agent / "SYSTEM.md").write_text("GLOBAL_SYSTEM")
    (agent / "APPEND_SYSTEM.md").write_text("GLOBAL_APPEND")
    (project / ".pi" / "SYSTEM.md").write_text("PROJECT_SYSTEM")
    (project / ".pi" / "APPEND_SYSTEM.md").write_text("PROJECT_APPEND")
    (agent / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fixture": {"command": "never-started", "enabled": False},
                }
            }
        )
    )
    (project / ".pi" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "project": {"command": "never-started", "enabled": False},
                }
            }
        )
    )
    for name in ("skills", "prompts", "themes", "extra"):
        (agent / name).mkdir()
    (agent / "skills" / "fixture.md").write_text(
        "---\nname: fixture-skill\ndescription: Fixture skill\n---\nfixture"
    )
    (agent / "prompts" / "fixture.md").write_text("---\ndescription: Fixture prompt\n---\nfixture")
    (agent / "themes" / "fixture.json").write_text("{}")
    extra_extension = agent / "extra" / "allowed.js"
    blocked_extension = agent / "extra" / "blocked.js"
    disabled_extension = agent / "extensions" / "disabled.js"
    absolute_disabled_extension = agent / "extensions" / "absolute-disabled.js"
    (agent / "settings.json").write_text(
        json.dumps(
            {
                "packages": [{"source": "./local-package", "extensions": ["index.js"]}],
                "extensions": [
                    "./relative.js",
                    "./extra",
                    "!extra/allowed.js",
                    "+extra/allowed.js",
                    "!extra/blocked.js",
                    "-extensions/disabled.js",
                    "-" + str(absolute_disabled_extension),
                ],
            }
        )
    )
    for extension in (
        user,
        project_extension,
        package_extension,
        relative_extension,
        extra_extension,
        blocked_extension,
        disabled_extension,
        absolute_disabled_extension,
    ):
        extension.write_text("module.exports = pi => pi.on('tool_call', () => {});")
    active_agent = agent if route == "own" else tmp_path / "managed"
    if route != "own":
        prepare_managed_pi_agent_dir(active_agent, global_agent_dir=agent)
    guard = tmp_path / "guard.js"
    guard.write_text("module.exports = pi => pi.on('tool_call', () => {});")
    # Replace only the CLI front end; keep the real SDK loader and built-ins.
    sdk = tmp_path / "sdk"
    (sdk / "cli").mkdir(parents=True)
    (sdk / "core").mkdir()
    (sdk / "utils").mkdir()
    (sdk / "package.json").write_text('{"type":"module"}')
    (sdk / "cli.js").write_text("")
    (sdk / "cli" / "setup.js").write_text("export function setupCli() {}")
    resource_loader = (dist / "core" / "resource-loader.js").as_uri()
    settings_manager = (dist / "core" / "settings-manager.js").as_uri()
    builtins = (dist / "extensions" / "index.js").as_uri()
    (sdk / "core" / "resource-loader.js").write_text(
        f"export {{DefaultResourceLoader}} from {json.dumps(resource_loader)};"
    )
    for name, symbol in (
        ("package-manager", "DefaultPackageManager"),
        ("settings-manager", "SettingsManager"),
    ):
        (sdk / "core" / f"{name}.js").write_text(
            f"export {{{symbol}}} from {json.dumps((dist / 'core' / f'{name}.js').as_uri())};"
        )
    (sdk / "utils" / "paths.js").write_text(
        f"export {{resolvePath}} from {json.dumps((dist / 'utils' / 'paths.js').as_uri())};"
    )
    (sdk / "main.js").write_text(
        f"""
import {{DefaultResourceLoader}} from {json.dumps(resource_loader)};
import {{SettingsManager}} from {json.dumps(settings_manager)};
import {{builtInExtensions}} from {json.dumps(builtins)};
import {{loadMcpConfig}} from {json.dumps((dist / "extensions" / "mcp" / "config.js").as_uri())};
export async function main(args, options) {{
  const settingsManager = SettingsManager.create(args[0], args[1],
    {{projectTrusted: {str(trusted).lower()}}});
  const loader = new DefaultResourceLoader({{cwd: args[0], agentDir: args[1],
    settingsManager,
    extensionFactories: [...builtInExtensions, ...(options.extensionFactories ?? [])]}});
  await loader.reload();
  console.log(JSON.stringify({{
    paths: loader.getExtensions().extensions.map(entry => entry.path),
    context: loader.getAgentsFiles().agentsFiles.map(entry => entry.content),
    system: loader.getSystemPrompt(), append: loader.getAppendSystemPrompt(),
    skills: loader.getSkills().skills.map(entry => entry.name),
    prompts: loader.getPrompts().prompts.map(entry => entry.name),
    themes: (await loader.packageManager.resolve()).themes.map(entry => entry.path),
    mcp: loadMcpConfig({{cwd: args[0], agentDir: args[1], projectTrusted: {str(trusted).lower()}}})
      .servers.map(entry => entry.name),
  }}));
}}
"""
    )
    (sdk / "probe.js").write_text(
        "import {main} from './main.js'; await main(process.argv.slice(2), {});"
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", agent.as_uri())
    launcher, args = ordered_pi_launch(
        str(sdk / "cli.js"),
        [str(project), str(active_agent), "--extension", str(guard)],
        extension=guard,
    )
    if route != "ordered":
        launcher, args = str(sdk / "probe.js"), [str(project), str(active_agent)]
    home = tmp_path / "home"
    home.mkdir()

    def load_resources():
        result = subprocess.run(
            [node, launcher, *args],
            text=True,
            capture_output=True,
            check=True,
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(home),
                "PI_CODING_AGENT_DIR": str(active_agent),
            },
            cwd=project,
            timeout=30,
        )
        return json.loads(result.stdout)

    loaded = load_resources()
    paths = [
        str(Path(path).resolve()) if Path(path).is_absolute() else path for path in loaded["paths"]
    ]
    if route == "ordered":
        assert paths[-1] == "<inline:omnigent-permission>"
    assert str(user) in paths
    assert str(package_extension) in paths
    assert str(relative_extension) in paths
    assert str(extra_extension) in paths
    assert str(blocked_extension) not in paths
    assert str(disabled_extension) not in paths
    assert str(absolute_disabled_extension) not in paths
    assert any(path.startswith("builtin:") for path in paths)
    assert (str(project_extension) in paths) is trusted
    assert "GLOBAL_OVERRIDE_CONTEXT" in loaded["context"]
    assert "SHADOWED_GLOBAL_CONTEXT" not in loaded["context"]
    assert loaded["system"] == ("PROJECT_SYSTEM" if trusted else "GLOBAL_SYSTEM")
    assert loaded["append"] == ["PROJECT_APPEND" if trusted else "GLOBAL_APPEND"]
    assert "fixture-skill" in loaded["skills"]
    assert "fixture" in loaded["prompts"]
    assert any(Path(path).name == "fixture.json" for path in loaded["themes"])
    assert "fixture" in loaded["mcp"]
    assert ("project" in loaded["mcp"]) is trusted
    if route != "own":
        (agent / "AGENTS.override.md").unlink()
        (agent / "AGENTS.md").write_text("GLOBAL_REFRESHED_CONTEXT")
        prepare_managed_pi_agent_dir(active_agent, global_agent_dir=agent)
        refreshed = load_resources()
        assert "GLOBAL_REFRESHED_CONTEXT" in refreshed["context"]
        assert "GLOBAL_OVERRIDE_CONTEXT" not in refreshed["context"]


def test_inline_launch_refuses_unsupported_sdk(tmp_path):
    from omnigent.inner.pi_settings import ordered_pi_launch

    with pytest.raises(ValueError, match="does not expose"):
        ordered_pi_launch("/opt/bin/standalone-pi", [], extension=tmp_path / "guard.js")
