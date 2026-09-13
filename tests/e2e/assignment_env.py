"""Hermetic harness for project-assignment cross-component tests.

Boots a real server subprocess with the ``project_assignments`` feature
flag plus real host daemons, backed by real git repositories with local
bare remotes and the mock LLM server. Imported as
``tests.e2e.assignment_env`` from both ``tests/e2e`` and
``tests/integration``. Callers own teardown: every spawner has a matching
stop, meant for ``try/finally`` or fixture teardown.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import (
    apply_runner_env,
    apply_server_env,
    compat_runner_cwd,
    compat_server_cwd,
    runner_executable,
    server_executable,
)
from tests.e2e.conftest import (
    configure_mock_llm,
    find_free_port,
    get_mock_requests,
    lookup_agent_id,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
    set_fallback_mock_llm,
)
from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]

_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "assign-test",
    "GIT_AUTHOR_EMAIL": "assign-test@example.com",
    "GIT_COMMITTER_NAME": "assign-test",
    "GIT_COMMITTER_EMAIL": "assign-test@example.com",
}

_MANIFEST_PATH = ".agents/project/manifest.json"

_POLICY_FALLBACK = '{"action": "allow", "reason": ""}'


def tail_text(path: Path, limit: int = 4000) -> str:
    """Return the last ``limit`` chars of ``path``, or a missing marker."""
    try:
        return path.read_text()[-limit:]
    except OSError:
        return f"<no log at {path}>"


@dataclass
class AssignmentServer:
    """A booted server subprocess and the files that identify it."""

    url: str
    port: int
    proc: subprocess.Popen[bytes]
    log_path: Path
    db_path: Path

    def stop(self) -> None:
        """Terminate the server, killing it when it ignores SIGTERM."""
        if self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


@dataclass
class AssignmentHost:
    """A spawned host daemon under a fixed HOME and host id."""

    host_id: str
    home: Path
    proc: subprocess.Popen[bytes]
    log_path: Path

    def stop(self) -> None:
        """Terminate the daemon, killing it when it ignores SIGTERM."""
        if self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


def boot_server(
    root: Path,
    *,
    mock_llm_url: str,
    db_path: Path,
    port: int | None = None,
) -> AssignmentServer:
    """Boot a server with ``project_assignments`` on, pointed at the mock LLM.

    :param root: Scratch dir for logs, artifacts and the server config.
    :param mock_llm_url: Mock LLM base URL (no ``/v1`` suffix).
    :param db_path: Sqlite file; pass the same one to restart on one database.
    :param port: Port to listen on, or ``None`` for a free one.
    :returns: The running server handle.
    :raises RuntimeError: When the server never passes ``/health``.
    """
    port = port if port is not None else find_free_port()
    url = f"http://127.0.0.1:{port}"
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / f"server-{port}.log"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir = root / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    server_cfg = root / f"server-{port}.yaml"
    server_cfg.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": "_policy_llm_",
                    "connection": {"base_url": f"{mock_llm_url}/v1", "api_key": "mock-key"},
                }
            }
        )
    )
    env = {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_url}/v1",
        "OMNIGENT_FEATURES": "project_assignments",
    }
    apply_server_env(env, _REPO_ROOT)
    log_handle = open(log_path, "w")  # noqa: SIM115 - handle lives for Popen's lifetime
    proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
            "--config",
            str(server_cfg),
        ],
        env=env,
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server died:\n{tail_text(log_path)}")
            try:
                if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                    set_fallback_mock_llm(mock_llm_url, "_policy_llm_", _POLICY_FALLBACK)
                    return AssignmentServer(
                        url=url, port=port, proc=proc, log_path=log_path, db_path=db_path
                    )
            except httpx.ConnectError:
                pass
            time.sleep(POLL_INTERVAL_S)
    except BaseException:
        proc.kill()
        proc.wait(timeout=10)
        log_handle.close()
        raise
    proc.kill()
    proc.wait(timeout=10)
    log_handle.close()
    raise RuntimeError(f"server never healthy:\n{tail_text(log_path)}")


def spawn_host(
    *,
    server_url: str,
    mock_llm_url: str,
    home: Path,
    host_id: str | None = None,
    name: str | None = None,
) -> AssignmentHost:
    """Spawn a host daemon under ``home`` with a stable host id for restarts.

    :param server_url: Server base URL to register with.
    :param mock_llm_url: Mock LLM base URL (no ``/v1`` suffix).
    :param home: Fixed HOME dir; reuse it with ``host_id`` to restart one host.
    :param host_id: Bare 32-char hex, or ``None`` for a fresh one.
    :param name: Host display name, or ``None`` for a derived one.
    :returns: The spawned host handle.
    """
    home.mkdir(parents=True, exist_ok=True)
    (home / ".omnigent").mkdir(parents=True, exist_ok=True)
    host_id = host_id if host_id is not None else uuid.uuid4().hex
    (home / ".omnigent" / "config.yaml").write_text(
        yaml.safe_dump({"host": {"host_id": host_id, "name": name or f"assign-{host_id[:12]}"}})
    )
    log_path = home / "host-daemon.log"
    env = {
        **os.environ,
        "HOME": str(home),
        "OPENAI_BASE_URL": f"{mock_llm_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        PROCESS_LOG_FILE_ENV_VAR: str(log_path),
    }
    # The daemon lifecycle lock lives under data_dir(), which prefers the
    # shared OMNIGENT_DATA_DIR tests/conftest.py sets over $HOME. Pin it per
    # host or a second daemon for the same server exits as already-claimed.
    env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")
    log_handle = open(log_path, "w")  # noqa: SIM115 - handle lives for Popen's lifetime
    proc = subprocess.Popen(
        [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", server_url],
        env=apply_runner_env(env),
        cwd=compat_runner_cwd(),
        stdout=subprocess.DEVNULL,
        stderr=log_handle,
    )
    return AssignmentHost(host_id=host_id, home=home, proc=proc, log_path=log_path)


def _host_rows(client: httpx.Client) -> list[dict[str, Any]]:
    """Return the server's host list rows, or ``[]`` when unreachable."""
    try:
        resp = client.get("/v1/hosts")
    except httpx.HTTPError:
        return []
    if resp.status_code != 200:
        return []
    hosts = resp.json().get("hosts", [])
    return [host for host in hosts if isinstance(host, dict)]


def wait_host_online(client: httpx.Client, host_id: str, timeout: float = 120.0) -> None:
    """Poll until ``host_id`` reads online.

    :param client: HTTP client on the server.
    :param host_id: Host to wait for.
    :param timeout: Max seconds to wait.
    :raises AssertionError: When the host never reads online.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for host in _host_rows(client):
            if host.get("host_id") == host_id and host.get("status") == "online":
                return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"host {host_id!r} never came online within {timeout}s")


def wait_host_offline(client: httpx.Client, host_id: str, timeout: float = 120.0) -> None:
    """Poll until ``host_id`` no longer reads online.

    :param client: HTTP client on the server.
    :param host_id: Host to wait for.
    :param timeout: Max seconds to wait.
    :raises AssertionError: When the host still reads online past the timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(
            not (host.get("host_id") == host_id and host.get("status") == "online")
            for host in _host_rows(client)
        ):
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"host {host_id!r} stayed online past {timeout}s")


def wait_runner_online(client: httpx.Client, runner_id: str, timeout: float = 120.0) -> None:
    """Poll the runner status route until ``runner_id`` reads online.

    :param client: HTTP client on the server.
    :param runner_id: Runner to wait for.
    :param timeout: Max seconds to wait.
    :raises AssertionError: When the runner never reads online.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get(f"/v1/runners/{runner_id}/status")
        except httpx.HTTPError:
            time.sleep(POLL_INTERVAL_S)
            continue
        if resp.status_code == 200 and resp.json().get("online") is True:
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"runner {runner_id!r} never came online within {timeout}s")


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git argv in ``cwd`` with a canned test identity."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={**os.environ, **_GIT_IDENTITY_ENV},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def git_ok(cwd: Path, *args: str) -> str:
    """Run one git argv, raising with stderr when it fails."""
    result = git(cwd, *args)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


def make_remote_and_source(root: Path, name: str) -> tuple[Path, Path, str]:
    """Create a bare remote plus a source checkout carrying the manifest.

    :param root: Scratch dir holding both repositories.
    :param name: Base name for the ``<name>-remote`` / ``<name>`` dirs.
    :returns: ``(remote, source, head)`` with the pinned input commit.
    """
    remote = (root / f"{name}-remote").resolve()
    remote.mkdir(parents=True, exist_ok=True)
    git_ok(remote, "init", "-q", "-b", "main", "--bare")
    source = (root / name).resolve()
    source.mkdir(parents=True, exist_ok=True)
    git_ok(source, "init", "-q", "-b", "main")
    # Pre-enable the untracked cache: otherwise the runner's background
    # registry probe runs git's --test-untracked-cache, which drops a
    # transient mtime-test-* dir in the checkout and races status checks.
    git_ok(source, "config", "core.untrackedCache", "true")
    manifest = source / _MANIFEST_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"version": 1}))
    (source / "README.md").write_text("hi\n")
    git_ok(source, "add", ".")
    git_ok(source, "commit", "-q", "-m", "init")
    git_ok(source, "remote", "add", "origin", str(remote))
    return remote, source, git_ok(source, "rev-parse", "HEAD")


def commit_all(source: Path, message: str) -> str:
    """Commit everything in ``source`` and return the new HEAD."""
    git_ok(source, "add", "-A")
    git_ok(source, "commit", "-q", "-m", message)
    return git_ok(source, "rev-parse", "HEAD")


def ls_remote_exact(remote: Path, ref: str) -> str | None:
    """Return the commit ``ref`` points at on ``remote``, exact-match only."""
    out = git(remote, "ls-remote", str(remote), ref).stdout.strip()
    for line in out.splitlines():
        sha, _, refname = line.partition("\t")
        if refname == ref:
            return sha
    return None


def assignment_id_for(sender_session_id: str, idempotency_key: str) -> str:
    """Precompute the deterministic dispatch id for a sender session and key."""
    return hashlib.sha256(
        f"assignment-dispatch:{sender_session_id}:{idempotency_key}".encode()
    ).hexdigest()[:32]


def _ledger_tool_names(mock_llm_url: str, model: str) -> tuple[int, list[str]]:
    """Return ``(request_count, tool_names)`` the mock saw for ``model``."""
    seen = get_mock_requests(mock_llm_url, key=model)
    names: list[str] = []
    for request in seen:
        for tool in request.get("tools", []):
            name = tool.get("name")
            if not isinstance(name, str):
                function = tool.get("function")
                name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str):
                names.append(name)
    return len(seen), names


def ensure_tool_advertised(
    client: httpx.Client,
    mock_llm_url: str,
    session_id: str,
    model: str,
    tool_name: str,
    *,
    timeout: float = 180.0,
    server: AssignmentServer,
    hosts: Sequence[AssignmentHost] = (),
) -> None:
    """Warm one session until its LLM requests advertise ``tool_name``.

    The runner builds and caches a session's tool surface on first use from
    a flag stored at session-init; a first turn can build the surface before
    the flag lands, and a scripted tool call then fails with "tool not
    found". A benign warm-up turn plus this ledger gate sequences past that
    window: it passes only when the runner actually advertised the tool, and
    fails loudly when the product never does. Call before configuring the
    scripted tool-call queue (this owns the model queue while it runs).

    :param client: HTTP client on the server.
    :param mock_llm_url: Mock LLM base URL (no ``/v1`` suffix).
    :param session_id: Session to warm up.
    :param model: Mock model key of the session's agent.
    :param tool_name: Tool the ledger must show, e.g. ``sys_assignment_dispatch``.
    :param timeout: Max seconds to wait.
    :param server: Server handle whose log tail joins the failure message.
    :param hosts: Host handles whose log tails join the failure message.
    :raises AssertionError: With the advertised names plus log tails.
    """
    configure_mock_llm(mock_llm_url, [{"text": "ready"}], key=model)
    deadline = time.monotonic() + timeout
    count, names = 0, []
    while time.monotonic() < deadline:
        token = f"warmup-{uuid.uuid4().hex[:8]}"
        remaining = max(1.0, deadline - time.monotonic())
        response_id = send_user_message_to_session(
            client, session_id=session_id, content=f"{token} reply with the word ready"
        )
        turn = poll_session_until_terminal(
            client, session_id=session_id, response_id=response_id, timeout=remaining
        )
        assert turn["status"] == "completed", f"warm-up turn failed: {turn.get('error')}"
        count, names = _ledger_tool_names(mock_llm_url, model)
        if tool_name in names:
            return
    lines = [
        f"session {session_id} never advertised {tool_name!r} within {timeout}s",
        f"mock saw {count} request(s) for model {model}; advertised tools: {sorted(set(names))}",
        f"server log tail:\n{tail_text(server.log_path)}",
    ]
    lines.extend(f"host {host.host_id} log tail:\n{tail_text(host.log_path)}" for host in hosts)
    raise AssertionError("\n".join(lines))


def poll_assignment(
    client: httpx.Client,
    assignment_id: str,
    *,
    want: str,
    timeout: float,
    server: AssignmentServer,
    hosts: Sequence[AssignmentHost] = (),
    wait_reason_contains: str | None = None,
) -> dict[str, Any]:
    """Poll one assignment row until ``want``, with log tails on failure.

    :param client: HTTP client on the server.
    :param assignment_id: Row to poll.
    :param want: State to wait for, e.g. ``"running"``.
    :param timeout: Max seconds to wait.
    :param server: Server handle whose log tail joins the failure message.
    :param hosts: Host handles whose log tails join the failure message.
    :param wait_reason_contains: Additionally require this substring in
        ``wait_reason`` before returning.
    :returns: The row that reached ``want``.
    :raises AssertionError: With the last row plus log tails past the timeout.
    """
    deadline = time.monotonic() + timeout
    row: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            resp = client.get(f"/v1/assignments/{assignment_id}")
        except httpx.HTTPError:
            time.sleep(POLL_INTERVAL_S)
            continue
        if resp.status_code == 200 and isinstance(resp.json(), dict):
            row = resp.json()
            if row.get("state") == want and (
                wait_reason_contains is None
                or wait_reason_contains in str(row.get("wait_reason") or "")
            ):
                return row
        time.sleep(POLL_INTERVAL_S)
    lines = [
        f"assignment {assignment_id} never reached {want!r} within {timeout}s",
        f"last row: {row}",
        f"server log tail:\n{tail_text(server.log_path)}",
    ]
    lines.extend(f"host {host.host_id} log tail:\n{tail_text(host.log_path)}" for host in hosts)
    raise AssertionError("\n".join(lines))


def wait_session_idle(client: httpx.Client, session_id: str, timeout: float = 120.0) -> None:
    """Poll a session snapshot until its status leaves running/waiting.

    :param client: HTTP client on the server.
    :param session_id: Session to poll.
    :param timeout: Max seconds to wait.
    :raises AssertionError: With the last snapshot past the timeout.
    """
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        last = resp.json()
        if last.get("status") not in ("running", "waiting"):
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"session {session_id} never idled within {timeout}s: {last}")


def create_project(client: httpx.Client, name: str) -> str:
    """Create a project and return its id."""
    resp = client.post("/v1/projects", json={"name": name})
    resp.raise_for_status()
    return str(resp.json()["id"])


def set_collaboration(
    client: httpx.Client, project_id: str, *, enabled: bool, expected_revision: int
) -> dict[str, Any]:
    """Flip the collaboration switch under its optimistic-concurrency guard."""
    resp = client.patch(
        f"/v1/projects/{project_id}/collaboration",
        json={"enabled": enabled, "expected_revision": expected_revision},
    )
    resp.raise_for_status()
    return resp.json()


def register_repository(
    client: httpx.Client, project_id: str, name: str, remote_url: str
) -> dict[str, Any]:
    """Register ``name`` on the project with a local bare remote."""
    resp = client.put(
        f"/v1/projects/{project_id}/repositories/{name}",
        json={"remote_url": remote_url, "default_branch": "main"},
    )
    resp.raise_for_status()
    return resp.json()


def put_primary_binding(
    client: httpx.Client,
    project_id: str,
    host_id: str,
    workspace: str,
    repository_name: str,
    *,
    name: str = "primary",
) -> dict[str, Any]:
    """Store an enabled primary binding; the host stats the path live."""
    resp = client.put(
        f"/v1/projects/{project_id}/hosts/{host_id}/bindings/{name}",
        json={
            "workspace": workspace,
            "repository_name": repository_name,
            "is_primary": True,
            "enabled": True,
        },
    )
    resp.raise_for_status()
    return resp.json()


def register_assignment_agent(
    client: httpx.Client, *, name: str, model: str, mock_llm_url: str
) -> tuple[str, str]:
    """Register an openai-agents mock agent; return its name and durable id."""
    agent_name = register_inline_agent(
        client,
        name=name,
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a terse test assistant. Follow instructions exactly.",
        mock_llm_base_url=f"{mock_llm_url}/v1",
    )
    return agent_name, lookup_agent_id(client, agent_name)


def create_session(client: httpx.Client, agent_id: str) -> str:
    """Create a session for ``agent_id`` and return its id."""
    from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN

    resp = client.post(
        "/v1/sessions",
        json={"agent_id": agent_id},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


def launch_session_on_host(
    client: httpx.Client, host_id: str, session_id: str, workspace: str, timeout: float = 120.0
) -> str:
    """Bind ``session_id`` to a runner launched on ``host_id`` at ``workspace``.

    :param client: HTTP client on the server.
    :param host_id: Host to launch on.
    :param session_id: Session to bind.
    :param workspace: Absolute path on the host.
    :param timeout: Max seconds for the runner to read online.
    :returns: The bound runner id.
    """
    resp = client.post(
        f"/v1/hosts/{host_id}/runners",
        json={"session_id": session_id, "workspace": workspace},
        timeout=90.0,
    )
    resp.raise_for_status()
    runner_id = str(resp.json()["runner_id"])
    wait_runner_online(client, runner_id, timeout=timeout)
    return runner_id


def _flatten_turn_item(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data")
    if not isinstance(data, dict):
        return item
    return {
        "id": item.get("id"),
        "response_id": item.get("response_id"),
        "type": item.get("type"),
        "status": item.get("status"),
        **data,
    }


def send_turn(
    client: httpx.Client, session_id: str, content: str, timeout: float = 240.0
) -> dict[str, Any]:
    """Send one user message and poll the turn it started until terminal.

    Only output positioned after the newly posted user item counts; the
    snapshot retains earlier turns, so any historical output would
    otherwise report the previous turn's result as this turn's.
    """
    response_id = send_user_message_to_session(client, session_id=session_id, content=content)
    deadline = time.monotonic() + timeout
    last_body: dict[str, Any] = {}
    seen_running = False
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        last_body = resp.json()
        flat = [
            _flatten_turn_item(item)
            for item in last_body.get("items", [])
            if isinstance(item, dict)
        ]
        posted = next(
            (
                index
                for index, item in enumerate(flat)
                if item.get("type") == "message"
                and item.get("role") == "user"
                and item.get("response_id") == response_id
            ),
            None,
        )
        if posted is None:
            time.sleep(POLL_INTERVAL_S)
            continue
        output = [
            item
            for item in flat[posted + 1 :]
            if not (item.get("type") == "message" and item.get("role") == "user")
        ]
        has_new_output = any(item.get("type") != "resource_event" for item in output)
        status = last_body.get("status")
        if status in ("running", "waiting"):
            seen_running = True
        if (status == "idle" and has_new_output) or (
            status == "failed" and (seen_running or has_new_output)
        ):
            return {
                "id": response_id,
                "status": "completed" if status == "idle" else "failed",
                "output": output,
                "error": last_body.get("last_task_error") or last_body.get("error"),
            }
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Session {session_id} turn {response_id} produced no output within {timeout}s; "
        f"last snapshot={last_body}"
    )
