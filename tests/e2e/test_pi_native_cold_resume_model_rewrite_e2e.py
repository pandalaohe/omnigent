"""Pi models, user resources and prompt settings survive a real terminal restart."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.pi_native.bridge import bridge_dir_for_session_id
from tests._helpers.host import isolated_host
from tests._helpers.pi_native import seed_pi_own_login_catalog
from tests._helpers.polling import poll_until
from tests._helpers.session import bundle_files, post_session_bundle

_INSTRUCTIONS = "OMNI_PI_AUTHORED_ROLE: act as the chief of staff."
_USER_ADDITION = "OMNI_PI_USER_ADDITION: keep replies concise."
_OBSERVER = """const fs = require('fs');
module.exports = function(pi) {
  pi.on('session_start', (_event, ctx) => {
    fs.appendFileSync(require('path').join(process.cwd(), 'prompts.jsonl'),
      JSON.stringify({pid: process.pid, prompt: ctx.getSystemPrompt(),
        agentDir: process.env.PI_CODING_AGENT_DIR,
        commands: pi.getCommands()}) + '\\n');
  });
};
"""

_SELECTION = "openai-codex/gpt-5.6-sol"
_MANAGED_SELECTION = "omnigent/gpt-4o-mini"
_CONTEXT_MARKERS = ["PI_GLOBAL_CONTEXT", "PI_ANCESTOR_CONTEXT", "PI_WORKSPACE_CONTEXT"]
_BASE_MARKER = "PI_DISCOVERED_BASE"
_APPEND_MARKER = "PI_DISCOVERED_APPEND"
_GLOBAL_APPEND_MARKER = "PI_GLOBAL_APPEND"
_NODE_OPTIONS_PRELOAD = """import fs from 'node:fs';
import path from 'node:path';
if (process.env.OMNIGENT_PI_NATIVE_BRIDGE_DIR) {
  fs.appendFileSync(path.join(process.env.OMNIGENT_PI_NATIVE_BRIDGE_DIR, 'node-options.jsonl'),
    JSON.stringify({pid: process.pid}) + '\\n');
}
"""


def _pane_command(metadata: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "tmux",
            "-S",
            metadata["tmux_socket"],
            "display-message",
            "-p",
            "-t",
            metadata["tmux_target"],
            "#{pane_start_command}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )


def _tmux_type(metadata: dict, text: str) -> None:
    subprocess.run(
        [
            "tmux",
            "-S",
            metadata["tmux_socket"],
            "send-keys",
            "-t",
            metadata["tmux_target"],
            "-l",
            text,
        ],
        check=True,
        timeout=10,
    )
    subprocess.run(
        [
            "tmux",
            "-S",
            metadata["tmux_socket"],
            "send-keys",
            "-t",
            metadata["tmux_target"],
            "Enter",
        ],
        check=True,
        timeout=10,
    )


def _scope_snapshots(bridge: Path) -> list[dict]:
    root = bridge / "pi-scopes"
    if not root.is_dir():
        return []
    snapshots = []
    for directory in root.iterdir():
        expected = directory / "permission_expected.json"
        registry = directory / "permission_tools.json"
        if expected.is_file() and registry.is_file():
            snapshots.append(json.loads(expected.read_text()))
    return snapshots


def _await_scope_count(bridge: Path, count: int) -> list[dict]:
    deadline = time.monotonic() + 30
    snapshots = []
    while time.monotonic() < deadline:
        snapshots = _scope_snapshots(bridge)
        if len(snapshots) == count:
            assert all(
                snapshot.get("harness") == "pi-native"
                and snapshot.get("origin") == "parent"
                and snapshot.get("scope_protocol_version") == 2
                for snapshot in snapshots
            )
            return snapshots
        time.sleep(0.2)
    raise AssertionError(f"Expected {count} native root scopes, got {snapshots}")


def _await_permission_ready(bridge: Path) -> None:
    deadline = time.monotonic() + 30
    state = {}
    while time.monotonic() < deadline:
        path = bridge / "permission_state.json"
        if path.is_file():
            state = json.loads(path.read_text())
            if state.get("status") == "ready":
                return
        time.sleep(0.1)
    raise AssertionError(f"Initial Pi permission launch did not finish: {state}")


def _node_option_pids(bridge: Path) -> list[int]:
    path = bridge / "node-options.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line)["pid"] for line in path.read_text().splitlines() if line]


def _wait_for_catalog(client: httpx.Client, session_id: str, selection: str) -> dict:
    deadline = time.monotonic() + 150
    snapshot = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/sessions/{session_id}", timeout=10)
        response.raise_for_status()
        snapshot = response.json()
        if snapshot.get("external_session_id") and any(
            option["id"] == selection for option in snapshot.get("model_options") or []
        ):
            return snapshot
        time.sleep(0.5)
    raise AssertionError(f"Pi never published its session ID and catalog: {snapshot}")


@pytest.mark.parametrize(
    ("context_files", "prompt_mode", "terminal_args", "provider_managed"),
    [
        (None, None, [], False),
        (False, "replace", ["--append-system-prompt", _USER_ADDITION], False),
        (None, None, ["--no-approve"], False),
        (False, "replace", ["--append-system-prompt", _USER_ADDITION], True),
    ],
    ids=["default", "replace-no-context", "untrusted-project", "managed-relative-root"],
)
def test_cold_resume_preserves_model_and_prompt_settings(
    live_server: str,
    http_client: httpx.Client,
    mock_llm_server_url: str,
    tmp_path: Path,
    context_files: bool | None,
    prompt_mode: str | None,
    terminal_args: list[str],
    provider_managed: bool,
) -> None:
    home = tmp_path / "home"
    workspace = home / "workspace"
    workspace.mkdir(parents=True)
    agent = home / ("custom-pi-agent" if provider_managed else ".pi/agent")
    selection = _MANAGED_SELECTION if provider_managed else _SELECTION
    seed_pi_own_login_catalog(agent, "openai-codex", ["gpt-5.6-sol", "gpt-5.5"])
    (agent / "auth.json").write_text(
        json.dumps(
            {
                "openai-codex": {"type": "oauth", "access": "test-only"},
            }
        )
    )
    extensions = agent / "extensions"
    extensions.mkdir()
    observer = extensions / "prompt-observer.js"
    observer.write_text(_OBSERVER)
    resources = agent / "resource-settings"
    resources.mkdir()
    for name in ("initial", "resumed"):
        (resources / f"{name}.js").write_text(
            "module.exports = function(pi) { "
            f"pi.registerCommand('relative-extension-{name}', "
            "{description: 'Relative extension resource', handler: async () => {}}); };"
        )
    for name, directory in (("discovered", agent), ("relative", resources)):
        skill = directory / "skills" / f"{name}-skill" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            f"---\nname: {name}-skill\ndescription: {name} user skill\n---\n"
            "Keep replies concise.\n"
        )
        prompt = directory / "prompts" / f"{name}-prompt.md"
        prompt.parent.mkdir(parents=True)
        prompt.write_text(f"---\ndescription: {name} user prompt\n---\nKeep replies concise.\n")
    (agent / "AGENTS.md").write_text(_CONTEXT_MARKERS[0])
    (agent / "APPEND_SYSTEM.md").write_text(_GLOBAL_APPEND_MARKER)
    user_settings = {
        "extensions": ["./resource-settings/initial.js"],
        "skills": ["./resource-settings/skills"],
        "prompts": ["./resource-settings/prompts"],
    }
    (agent / "settings.json").write_text(json.dumps(user_settings))
    config = {
        "providers": {
            "openrouter": {
                "kind": "gateway",
                "default": "pi",
                "openai": {
                    "base_url": f"{mock_llm_server_url}/v1",
                    "api_key": "test-only",
                    "models": {"pi": "gpt-4o-mini"},
                },
            }
        }
    }
    preload = tmp_path / "node options" / "native-carrier-preload.mjs"
    preload.parent.mkdir()
    preload.write_text(_NODE_OPTIONS_PRELOAD)
    with isolated_host(
        tmp_path,
        live_server,
        config=config,
        env={
            "OMNIGENT_PI_PATH": os.environ.get("OMNIGENT_PI_PATH"),
            "PI_CODING_AGENT_DIR": "../custom-pi-agent" if provider_managed else None,
            "OMNIGENT_RUNNER_ENV_PASSTHROUGH": (
                "PI_CODING_AGENT_DIR,NODE_OPTIONS" if provider_managed else "NODE_OPTIONS"
            ),
            "NODE_OPTIONS": f"--import={preload.as_uri()}",
        },
    ) as host:
        host.wait_online(http_client)
        (host.home / "AGENTS.md").write_text(_CONTEXT_MARKERS[1])
        (workspace / "AGENTS.md").write_text(_CONTEXT_MARKERS[2])
        project_config = workspace / ".pi"
        project_config.mkdir()
        base_prompt = project_config / "SYSTEM.md"
        base_prompt.write_text(_BASE_MARKER)
        (project_config / "APPEND_SYSTEM.md").write_text(_APPEND_MARKER)
        settings = ""
        if context_files is not None:
            settings += f"  context_files: {str(context_files).lower()}\n"
        if prompt_mode is not None:
            settings += f"  system_prompt_mode: {prompt_mode}\n"
        bundle = bundle_files(
            {
                "pi-native-ui.yaml": f"""
name: pi-native-ui
instructions: role.md
executor:
  harness: pi-native
  model: {selection}
{settings}spawn: true
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
""".encode(),
                "role.md": _INSTRUCTIONS.encode(),
            }
        )
        create = post_session_bundle(
            http_client.post,
            "/v1/sessions",
            bundle,
            metadata={
                "host_id": host.host_id,
                "workspace": str(workspace),
                "terminal_launch_args": terminal_args,
                "labels": {"omnigent.ui": "terminal", "omnigent.wrapper": "pi-native-ui"},
            },
            timeout=60,
        )
        create.raise_for_status()
        session_id = create.json()["session_id"]
        terminal_url = f"/v1/sessions/{session_id}/resources/terminals"
        try:
            _wait_for_catalog(http_client, session_id, selection)
            terminal = http_client.get(f"{terminal_url}/terminal_pi_main", timeout=30)
            terminal.raise_for_status()
            prompts = workspace / "prompts.jsonl"
            poll_until(lambda: prompts.exists(), timeout_s=30, interval_s=0.2)
            first_prompt = json.loads(prompts.read_text().splitlines()[0])
            bridge = host.home / ".omnigent/pi-native" / bridge_dir_for_session_id(session_id).name
            if provider_managed:
                assert Path(first_prompt["agentDir"]).resolve() == (bridge / "pi-agent").resolve()
                assert (bridge / "pi-agent/models.json").is_file()
            assert first_prompt["prompt"].count(_INSTRUCTIONS) == 1
            assert first_prompt["prompt"].count(_USER_ADDITION) == (
                _USER_ADDITION in terminal_args
            )
            project_trusted = "--no-approve" not in terminal_args
            assert (_BASE_MARKER in first_prompt["prompt"]) is (
                prompt_mode != "replace" and project_trusted
            )
            assert first_prompt["prompt"].count(_APPEND_MARKER) == (
                prompt_mode != "replace" and project_trusted
            )
            assert first_prompt["prompt"].count(_GLOBAL_APPEND_MARKER) == (
                prompt_mode != "replace" and not project_trusted
            )
            for marker in _CONTEXT_MARKERS:
                assert (marker in first_prompt["prompt"]) is (context_files is not False)
            initial_scopes = _await_scope_count(bridge, 1)
            _await_permission_ready(bridge)
            assert _node_option_pids(bridge) == [first_prompt["pid"]]
            old_pane = terminal.json()["metadata"]
            _tmux_type(old_pane, "/reload")
            poll_until(
                lambda: len(prompts.read_text().splitlines()) >= 2,
                timeout_s=30,
                interval_s=0.2,
            )
            reloaded_prompt = json.loads(prompts.read_text().splitlines()[-1])
            assert reloaded_prompt["pid"] == first_prompt["pid"]
            # /reload keeps the Node incarnation but revokes and replaces the
            # scoped client; registry files retain that revoked installation's history.
            reloaded_scopes = _await_scope_count(bridge, 2)
            assert len({scope["launch_epoch"] for scope in reloaded_scopes}) == 1
            assert {scope["scope_id"] for scope in initial_scopes} < {
                scope["scope_id"] for scope in reloaded_scopes
            }
            http_client.delete(f"{terminal_url}/terminal_pi_main", timeout=30).raise_for_status()
            deadline = time.monotonic() + 30
            while _pane_command(old_pane).returncode == 0:
                assert time.monotonic() < deadline, "initial Pi terminal did not stop"
                time.sleep(0.5)

            base_prompt.unlink()
            (project_config / "APPEND_SYSTEM.md").unlink()
            # Hiding a saved model must preserve its launch identity.
            user_settings["enabledModels"] = [
                "omnigent/not-enabled" if provider_managed else "openai-codex/gpt-5.5"
            ]
            user_settings["extensions"] = ["./resource-settings/resumed.js"]
            (agent / "settings.json").write_text(json.dumps(user_settings))

            # A stale catalog must be replaced by the new process's publication.
            http_client.post(
                f"/v1/sessions/{session_id}/events",
                timeout=30,
                json={
                    "type": "external_model_options",
                    "data": {"models": [{"id": "test/stale"}]},
                },
            ).raise_for_status()

            ensured = http_client.post(
                terminal_url,
                timeout=60,
                json={
                    "terminal": "pi",
                    "session_key": "main",
                    "ensure_native_terminal": True,
                },
            )
            ensured.raise_for_status()
            terminal = http_client.get(f"{terminal_url}/terminal_pi_main", timeout=30)
            terminal.raise_for_status()
            command = _pane_command(terminal.json()["metadata"])
            assert command.returncode == 0, command.stderr
            model_arg = "gpt-4o-mini" if provider_managed else selection
            assert f"--model {model_arg}" in command.stdout
            if provider_managed:
                assert "--provider omnigent" in command.stdout
            assert f"omnigent/{selection}" not in command.stdout
            assert (bridge / "pi-agent/models.json").exists() is provider_managed
            options = _wait_for_catalog(http_client, session_id, selection)["model_options"]
            ids = {option["id"] for option in options}
            assert ids == (
                {selection} if provider_managed else {_SELECTION, "openai-codex/gpt-5.5"}
            )
            poll_until(
                lambda: len(prompts.read_text().splitlines()) >= 3, timeout_s=30, interval_s=0.2
            )
            resumed_prompt = json.loads(prompts.read_text().splitlines()[-1])
            assert resumed_prompt["pid"] != first_prompt["pid"]
            resumed_scopes = _await_scope_count(bridge, 3)
            assert len({scope["launch_epoch"] for scope in resumed_scopes}) == 2
            assert len({scope["scope_id"] for scope in resumed_scopes}) == 3
            assert {scope["scope_id"] for scope in reloaded_scopes} < {
                scope["scope_id"] for scope in resumed_scopes
            }
            assert _node_option_pids(bridge) == [first_prompt["pid"], resumed_prompt["pid"]]
            if provider_managed:
                assert resumed_prompt["agentDir"] == first_prompt["agentDir"]
            for snapshot, phase in ((first_prompt, "initial"), (resumed_prompt, "resumed")):
                commands = [(entry["name"], entry["source"]) for entry in snapshot["commands"]]
                for resource in (
                    ("skill:discovered-skill", "skill"),
                    ("skill:relative-skill", "skill"),
                    ("discovered-prompt", "prompt"),
                    ("relative-prompt", "prompt"),
                    (f"relative-extension-{phase}", "extension"),
                ):
                    assert commands.count(resource) == 1, snapshot
            assert ("relative-extension-initial", "extension") not in commands
            assert resumed_prompt["prompt"].count(_INSTRUCTIONS) == 1
            assert resumed_prompt["prompt"].count(_USER_ADDITION) == (
                _USER_ADDITION in terminal_args
            )
            assert ("coding assistant" in resumed_prompt["prompt"].lower()) is (
                prompt_mode != "replace"
            )
            assert _BASE_MARKER not in resumed_prompt["prompt"]
            assert _APPEND_MARKER not in resumed_prompt["prompt"]
            assert resumed_prompt["prompt"].count(_GLOBAL_APPEND_MARKER) == (
                prompt_mode != "replace"
            )
            for marker in _CONTEXT_MARKERS:
                assert (marker in resumed_prompt["prompt"]) is (context_files is not False)
        finally:
            with contextlib.suppress(httpx.HTTPError):
                http_client.delete(f"/v1/sessions/{session_id}", timeout=10)
