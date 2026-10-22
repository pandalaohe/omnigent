"""Real native Codex can write Git state while hooks and config stay protected."""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer, client_for_transport
from omnigent.inner.codex_executor import CodexExecutor, _codex_cli_version
from omnigent.inner.executor import ExecutorConfig, TurnComplete
from omnigent.sdk_permission_modes import CODEX_SDK_TURN_POLICIES
from tests.e2e._codex_loopback_resume import clean_codex_env, sse_text_response, wait_for_turn_end
from tests.e2e._harness_probes import cli_unavailable_reason

_COMMANDS = (
    "git commit --allow-empty -m probe",
    "git fetch origin",
    "echo x > .git/hooks/probe",
    "git config omniprobe.flag 1",
)


def _tool_response(index: int, command: str) -> bytes:
    item = {
        "id": f"fc-probe-{index}",
        "type": "function_call",
        "status": "completed",
        "call_id": f"call-probe-{index}",
        "name": "exec_command",
        "arguments": json.dumps(
            {"cmd": command, "yield_time_ms": 10000, "max_output_tokens": 2000}
        ),
    }
    events = [
        ("response.created", {"response": {"id": f"resp-probe-{index}"}}),
        (
            "response.output_item.added",
            {"output_index": 0, "item": {**item, "status": "in_progress", "arguments": ""}},
        ),
        (
            "response.function_call_arguments.delta",
            {"item_id": item["id"], "output_index": 0, "delta": item["arguments"]},
        ),
        (
            "response.function_call_arguments.done",
            {"item_id": item["id"], "output_index": 0, "arguments": item["arguments"]},
        ),
        ("response.output_item.done", {"output_index": 0, "item": item}),
        (
            "response.completed",
            {
                "response": {
                    "id": f"resp-probe-{index}",
                    "object": "response",
                    "status": "completed",
                    "output": [item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
            },
        ),
    ]
    return "".join(
        f"event: {event}\ndata: {json.dumps({'type': event, **payload})}\n\n"
        for event, payload in events
    ).encode()


class _ScriptedProvider(http.server.ThreadingHTTPServer):
    def __init__(self, commands: list[str | None]) -> None:
        self.commands = commands
        self.requests: list[dict[str, Any]] = []
        super().__init__(("127.0.0.1", 0), _ScriptedHandler)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class _ScriptedHandler(http.server.BaseHTTPRequestHandler):
    server: _ScriptedProvider

    def do_GET(self) -> None:
        self._send("application/json", b'{"models":[]}')

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        index = len(self.server.requests)
        self.server.requests.append(body)
        response = (
            _tool_response(index, command)
            if index < len(self.server.commands)
            and (command := self.server.commands[index]) is not None
            else sse_text_response("Git probes finished")
        )
        self._send("text/event-stream", response)

    def _send(self, content_type: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _probe_exit_codes(provider: _ScriptedProvider, indices: list[int]) -> list[int]:
    exits = []
    for index in indices:
        request = provider.requests[index + 1]
        output = next(
            item["output"]
            for item in request["input"]
            if item.get("type") == "function_call_output"
            and item.get("call_id") == f"call-probe-{index}"
        )
        assert isinstance(output, str), output
        match = re.search(r"(?:Process exited with code|Exit code:)\s*(-?\d+)", output)
        assert match is not None, output
        exits.append(int(match[1]))
    return exits


def _loopback_overrides(provider: _ScriptedProvider) -> list[str]:
    return [
        'model_provider="loopback"',
        f'model_providers.loopback={{name="Loopback",base_url={json.dumps(provider.base_url)},wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}}',
        "analytics.enabled=false",
        "feedback.enabled=false",
        "features.plugins=false",
        'otel.metrics_exporter="none"',
        "check_for_update_on_startup=false",
    ]


@pytest.fixture
def sibling_repo() -> Iterator[Path]:
    # Codex also grants OS temp directories; put the denial probe outside them.
    scratch_root = Path(__file__).resolve().parents[2] / "tmp"
    scratch_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-sibling-", dir=scratch_root) as directory:
        yield Path(directory)


def _git_probe_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: bool, sibling_repo: Path
) -> tuple[Path, Path, list[str]]:
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global.gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    remote = tmp_path / "remote.git"
    remote.mkdir()
    for directory, args in (
        (remote, ["init", "--bare"]),
        (workspace, ["init", "-b", "main"]),
        (workspace, ["config", "user.name", "Probe"]),
        (workspace, ["config", "user.email", "probe@example.test"]),
        (workspace, ["config", "commit.gpgsign", "false"]),
        (workspace, ["commit", "--allow-empty", "-m", "initial"]),
        (workspace, ["remote", "add", "origin", str(remote)]),
        (workspace, ["push", "origin", "main"]),
    ):
        subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True)
    common = workspace / ".git"
    commands = list(_COMMANDS)
    if linked:
        linked_workspace = tmp_path / "linked-workspace"
        subprocess.run(
            ["git", "-C", str(workspace), "worktree", "add", str(linked_workspace)],
            check=True,
            capture_output=True,
        )
        workspace = linked_workspace
        commands[2] = f"echo x > {common / 'hooks' / 'probe'}"
        commands[3] = f"git config --file {common / 'config'} omniprobe.flag 1"
        commands.append(f"echo x > {common / 'worktrees' / 'linked-workspace' / 'commondir'}")
    subprocess.run(["git", "-C", str(sibling_repo), "init"], check=True, capture_output=True)
    commands.append(f"echo x > {shlex.quote(str(sibling_repo / '.git' / 'probe'))}")
    return workspace, common, commands


@pytest.mark.parametrize("linked,broad_root", [(False, False), (True, False), (True, True)])
async def test_native_codex_git_write_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked: bool,
    broad_root: bool,
    sibling_repo: Path,
) -> None:
    reason = cli_unavailable_reason("codex")
    if reason is not None:
        pytest.skip(reason)
    codex_path = shutil.which("codex")
    assert codex_path is not None
    workspace, common, commands = _git_probe_workspace(tmp_path, monkeypatch, linked, sibling_repo)
    successful_probes = 2
    if broad_root:
        (tmp_path / "source-codex-home" / "config.toml").write_text(
            f"[sandbox_workspace_write]\nwritable_roots = [{json.dumps(str(common.parent))}]\n"
        )
        sibling = tmp_path / "sibling-worktree"
        subprocess.run(
            ["git", "-C", str(workspace), "worktree", "add", str(sibling)],
            check=True,
            capture_output=True,
        )
        commands[2:2] = [
            f"git -C {shlex.quote(str(sibling))} commit --allow-empty -m sibling",
            f"git -C {shlex.quote(str(sibling))} fetch origin",
        ]
        successful_probes += 2
        for name in ("gitdir", "config.worktree"):
            target = common / "worktrees" / "linked-workspace" / name
            commands.append(f"echo x > {shlex.quote(str(target))}")
    child_tmp = tmp_path / "child-tmp"
    child_tmp.mkdir()
    child_home = tmp_path / "child-home"
    child_home.mkdir()
    provider = _ScriptedProvider(["git commit --allow-empty -m control", None, *commands, None])
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        listen_url = f"ws://127.0.0.1:{probe.getsockname()[1]}"
    app_server = CodexNativeAppServer(
        codex_path=codex_path,
        socket_path=tmp_path / "bridge" / "app-server.sock",
        listen_url=listen_url,
        codex_home=tmp_path / "codex-home",
        env={**clean_codex_env(child_home), "TMPDIR": str(child_tmp)},
        config_overrides=_loopback_overrides(provider),
        cwd=workspace,
        bridge_dir=tmp_path / "bridge",
        pinned_model="mock-model",
        model_catalog_rows=[{"id": "mock-model"}],
        trust_project=True,
        reconcile_process_registry=False,
        permission_launch_args=("--approve-for-me",),
    )
    client = client_for_transport(listen_url)
    try:
        await app_server.start()
        if app_server.workspace_profile is None and (
            app_server.codex_cli_version is None or app_server.codex_cli_version < (0, 162, 0)
        ):
            pytest.skip("native Git profile requires Codex >= 0.162.0")
        assert app_server.workspace_profile is not None, app_server.codex_cli_version
        await client.connect()
        started = await client.request(
            "thread/start",
            {
                "cwd": str(workspace),
                "sandbox": "workspace-write",
                "approvalPolicy": "never",
            },
        )
        thread_id = started["result"]["thread"]["id"]
        for control in (True, False):
            if not control:
                await client.request(
                    "thread/settings/update",
                    {"threadId": thread_id, "permissions": "omnigent-workspace"},
                )
            await client.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": "Run the Git probes."}],
                },
            )
            terminal = await wait_for_turn_end(client)
            assert terminal["method"] == "turn/completed", terminal
            assert terminal["params"]["turn"]["status"] == "completed", terminal
        assert len(provider.requests) == len(commands) + 3
        exits = _probe_exit_codes(provider, [0, *range(2, len(commands) + 2)])
        assert exits[0] != 0, exits
        assert exits[1 : successful_probes + 1] == [0] * successful_probes, exits
        assert all(code != 0 for code in exits[successful_probes + 1 :]), exits
        assert not (common / "hooks" / "probe").exists()
        assert not (sibling_repo / ".git" / "probe").exists()
        configured = subprocess.run(
            ["git", "-C", str(workspace), "config", "--get", "omniprobe.flag"],
            capture_output=True,
            check=False,
        )
        assert configured.returncode == 1, configured.returncode
    finally:
        with contextlib.suppress(Exception):
            await client.close()
        await app_server.close()
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)


@pytest.mark.parametrize("linked", [False, True])
async def test_sdk_codex_git_write_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: bool, sibling_repo: Path
) -> None:
    reason = cli_unavailable_reason("codex")
    if reason is not None:
        pytest.skip(reason)
    codex_path = shutil.which("codex")
    assert codex_path is not None
    version = await _codex_cli_version(codex_path)
    if version is None or version < (0, 139, 0):
        pytest.skip("SDK Git profile requires Codex >= 0.139.0")
    workspace, common, commands = _git_probe_workspace(tmp_path, monkeypatch, linked, sibling_repo)
    child_home = tmp_path / "child-home"
    child_home.mkdir()
    child_tmp = tmp_path / "child-tmp"
    child_tmp.mkdir()
    provider = _ScriptedProvider(["git commit --allow-empty -m control", None, *commands, None])
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    executor = CodexExecutor(
        codex_path=codex_path, cwd=str(workspace), model="mock-model", skills_filter="none"
    )
    executor._env = {**clean_codex_env(child_home), "TMPDIR": str(child_tmp)}
    executor._codex_config_overrides.extend(_loopback_overrides(provider))
    # Denied probes cannot escalate to an interactive approval.
    monkeypatch.setitem(
        CODEX_SDK_TURN_POLICIES, "ask-for-approval", ("never", "user", "workspaceWrite")
    )
    try:
        for mode in ("read-only", "ask-for-approval"):
            async with asyncio.timeout(90):
                events = [
                    event
                    async for event in executor.run_turn(
                        [{"role": "user", "content": "Run the Git probes."}],
                        [],
                        "",
                        ExecutorConfig(approval_mode=mode),
                    )
                ]
            assert isinstance(events[-1], TurnComplete), events
        assert len(provider.requests) == len(commands) + 3
        session = executor._live_app_session()
        assert session is not None and session._workspace_profile is not None, version
        exits = _probe_exit_codes(provider, [0, *range(2, len(commands) + 2)])
        assert exits[0] != 0, exits
        assert exits[1:3] == [0, 0], exits
        assert all(code != 0 for code in exits[3:]), exits
        assert not (common / "hooks" / "probe").exists()
        assert not (sibling_repo / ".git" / "probe").exists()
        configured = subprocess.run(
            ["git", "-C", str(workspace), "config", "--get", "omniprobe.flag"],
            capture_output=True,
            check=False,
        )
        assert configured.returncode == 1
    finally:
        await executor.close()
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)
