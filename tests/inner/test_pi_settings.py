"""Tests for managed Pi agent dir seeding (extensions / packages)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from omnigent.inner.pi_settings import prepare_managed_pi_agent_dir


def test_default_thinking_and_per_model_compaction_survive_settings_merge(tmp_path, monkeypatch):
    from omnigent.inner import pi_settings

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


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.parametrize("managed", [False, True])
def test_inline_guard_follows_actual_sdk_extensions(tmp_path, monkeypatch, trusted, managed):
    """Exercise Pi's loader, including built-ins and an untrusted project."""
    import subprocess

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
    user = agent / "extensions" / "user.js"
    project_extension = project / ".pi" / "extensions" / "project.js"
    package = agent / "local-package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"pi": {"extensions": ["index.js"]}}))
    package_extension = package / "index.js"
    relative_extension = agent / "relative.js"
    (agent / "settings.json").write_text(
        json.dumps(
            {
                "packages": ["./local-package"],
                "extensions": ["./relative.js"],
            }
        )
    )
    for extension in (user, project_extension, package_extension, relative_extension):
        extension.write_text("module.exports = pi => pi.on('tool_call', () => {});")
    active_agent = tmp_path / "managed" if managed else agent
    if managed:
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
export async function main(args, options) {{
  const settingsManager = SettingsManager.create(args[0], args[1],
    {{projectTrusted: {str(trusted).lower()}}});
  const loader = new DefaultResourceLoader({{cwd: args[0], agentDir: args[1],
    settingsManager, extensionFactories: [...builtInExtensions, ...options.extensionFactories]}});
  await loader.reload();
  console.log(JSON.stringify(loader.getExtensions().extensions.map(entry => entry.path)));
}}
"""
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", agent.as_uri())
    launcher, args = ordered_pi_launch(
        str(sdk / "cli.js"),
        [str(project), str(active_agent), "--extension", str(guard)],
        extension=guard,
    )
    result = subprocess.run(
        [node, launcher, *args],
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "PI_CODING_AGENT_DIR": str(active_agent)},
    )
    paths = json.loads(result.stdout)
    assert paths[-1] == "<inline:omnigent-permission>"
    assert str(user) in paths
    assert str(package_extension) in paths
    assert str(relative_extension) in paths
    assert any(path.startswith("builtin:") for path in paths)
    assert (str(project_extension) in paths) is trusted


def test_inline_launch_refuses_unsupported_sdk(tmp_path):
    from omnigent.inner.pi_settings import ordered_pi_launch

    with pytest.raises(ValueError, match="does not expose"):
        ordered_pi_launch("/opt/bin/standalone-pi", [], extension=tmp_path / "guard.js")
