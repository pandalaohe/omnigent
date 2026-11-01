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
@pytest.mark.parametrize("route", ["own", "full", "ordered", "ordered-windows"])
def test_inline_guard_follows_actual_sdk_extensions(tmp_path, monkeypatch, trusted, route):
    """Use Pi's consumer for context, MCP, resources, trust and guard ordering."""

    from omnigent.harnesses import pi_cli
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
    executable = sdk / "cli.js"
    if route == "ordered-windows":
        executable = tmp_path / "pi.CMD"
        executable.write_text('@node "%dp0%\\sdk\\cli.js" %*\n')
        monkeypatch.setattr(pi_cli, "IS_WINDOWS", True)
        monkeypatch.setattr(pi_cli, "resolve_cli_binary", lambda _: node)
    launcher, args = ordered_pi_launch(
        str(executable),
        [str(project), str(active_agent), "--extension", str(guard)],
        extension=guard,
    )
    if not route.startswith("ordered"):
        launcher, args = str(sdk / "probe.js"), [str(project), str(active_agent)]
    home = tmp_path / "home"
    home.mkdir()

    def load_resources():
        result = subprocess.run(
            [*pi_cli.pi_cli_argv(launcher), *args]
            if route == "ordered-windows"
            else [node, launcher, *args],
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
    if route.startswith("ordered"):
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


def test_prepare_managed_pi_agent_dir_links_only_supported_resources(tmp_path: Path) -> None:
    """Supported resources are linked; instructions and session state stay isolated."""
    global_agent = tmp_path / "global-agent"
    for directory in (
        "npm",
        "git",
        "skills/review",
        "extensions",
        "prompts",
        "themes",
        "sessions",
    ):
        (global_agent / directory).mkdir(parents=True)
    for name in (
        "mcp.json",
        "mcp-auth.json",
        "keybindings.json",
        "auth.json",
        "models.json",
        "AGENTS.md",
        "SYSTEM.md",
        "APPEND_SYSTEM.md",
    ):
        (global_agent / name).write_text("{}", encoding="utf-8")
    managed = tmp_path / "managed"
    managed.mkdir()

    prepare_managed_pi_agent_dir(managed, global_agent_dir=global_agent)

    for name in (
        "npm",
        "git",
        "mcp.json",
        "mcp-auth.json",
        "skills",
        "extensions",
        "prompts",
        "themes",
        "keybindings.json",
    ):
        if name in {"mcp.json", "mcp-auth.json", "keybindings.json"}:
            assert not (managed / name).is_symlink()
            assert (managed / name).read_bytes() == (global_agent / name).read_bytes()
        else:
            assert (managed / name).readlink() == global_agent / name
    for name in (
        "sessions",
        "auth.json",
        "models.json",
    ):
        assert not (managed / name).exists(), name
    for name in ("AGENTS.md", "SYSTEM.md", "APPEND_SYSTEM.md"):
        assert (managed / name).read_text() == "{}"
        assert not (managed / name).is_symlink()
    assert not (managed / "settings.json").is_symlink()


def test_prepare_managed_pi_agent_dir_preserves_unowned_entries_on_reuse(tmp_path: Path) -> None:
    """Without a seeding receipt, existing links and directories remain caller-owned."""
    previous_root = tmp_path / "previous-agent"
    previous_root.mkdir()
    (previous_root / "mcp.json").write_text("{}", encoding="utf-8")
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    (global_agent / "mcp.json").write_text("{}", encoding="utf-8")
    (global_agent / "mcp-auth.json").write_text("{}", encoding="utf-8")

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "mcp.json").symlink_to(previous_root / "mcp.json")
    (managed / "mcp-auth.json").symlink_to(tmp_path / "removed-auth.json")
    (managed / "git").symlink_to(global_agent / "git", target_is_directory=True)
    (managed / "npm").mkdir()

    prepare_managed_pi_agent_dir(managed, global_agent_dir=global_agent)

    assert (managed / "mcp.json").readlink() == previous_root / "mcp.json"
    assert (managed / "mcp-auth.json").readlink() == tmp_path / "removed-auth.json"
    assert (managed / "git").readlink() == global_agent / "git"
    assert not (managed / "npm").is_symlink() and (managed / "npm").is_dir()


@pytest.mark.parametrize("parent_relative_root", [False, True])
def test_relative_settings_keep_their_original_root(
    tmp_path: Path, parent_relative_root: bool
) -> None:
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    settings = {
        "extensions": [
            "./custom/demo.js",
            "!custom/disabled.js",
            "-extensions/off.js",
            "-builtin:mcp",
            "-" + str(global_agent / "extensions/disabled.js"),
        ],
        "skills": ["../shared-skills", "/absolute/skills", "~/skills"],
        "packages": [
            "npm:@example/package",
            "./local-package",
            {"source": "../package", "skills": []},
        ],
    }
    settings_path = global_agent / "settings.json"
    settings_path.write_text(json.dumps(settings))
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "settings.json").symlink_to(settings_path)

    (tmp_path / "workspace").mkdir()
    root = tmp_path / "workspace/../global-agent" if parent_relative_root else global_agent
    prepare_managed_pi_agent_dir(managed, global_agent_dir=root)

    written = json.loads((managed / "settings.json").read_text())
    assert written["extensions"] == [
        str(global_agent / "custom/demo.js"),
        "!custom/disabled.js",
        "!" + str(global_agent / "custom/disabled.js"),
        "!" + str(managed / "custom/disabled.js"),
        "-extensions/off.js",
        "-" + str(global_agent / "extensions/off.js"),
        "-" + str(managed / "extensions/off.js"),
        "-builtin:mcp",
        "-" + str(global_agent / "extensions/disabled.js"),
        "-" + str(managed / "extensions/disabled.js"),
    ]
    assert written["skills"] == [
        str((global_agent / "../shared-skills").resolve()),
        "/absolute/skills",
        str(Path.home() / "skills"),
    ]
    assert written["packages"] == [
        "npm:@example/package",
        str(global_agent / "local-package"),
        {"source": str((global_agent / "../package").resolve()), "skills": []},
    ]
    assert json.loads(settings_path.read_text()) == settings
    assert not (managed / "settings.json").is_symlink()


@pytest.mark.parametrize("content", [b"{invalid", b"[]", b"\xff"])
def test_invalid_settings_do_not_hide_resources(tmp_path: Path, content: bytes) -> None:
    global_agent = tmp_path / "global-agent"
    global_agent.mkdir()
    (global_agent / "settings.json").write_bytes(content)
    (global_agent / "mcp.json").write_text("{malformed; Pi validates this}")
    managed = tmp_path / "managed"

    prepare_managed_pi_agent_dir(
        managed, global_agent_dir=global_agent, overlay={"retry": {"enabled": True}}
    )

    assert json.loads((managed / "settings.json").read_text()) == {"retry": {"enabled": True}}
    assert not (managed / "mcp.json").is_symlink()
    assert (managed / "mcp.json").read_text() == "{malformed; Pi validates this}"


def test_source_is_never_rewritten_in_place(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")
    prepare_managed_pi_agent_dir(
        tmp_path, global_agent_dir=tmp_path, overlay={"retry": {"enabled": True}}
    )
    assert settings_path.read_text() == "{}"
