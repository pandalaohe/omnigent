"""Cross-component project-assignment tests on one host with real git refs.

Scenario 1 drives the happy path end to end: a sender session dispatches
via ``sys_assignment_dispatch``, the coordinator prepares a worktree on the
same host and starts the receiving session, and the receiver completes via
``sys_assignment_complete``. Scenario 12 disables the collaboration switch
mid-flight: in-flight work still finishes while a new dispatch is refused.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.assignment_env import (
    AssignmentHost,
    AssignmentServer,
    assignment_id_for,
    boot_server,
    commit_all,
    create_project,
    create_session,
    ensure_tool_advertised,
    git_ok,
    launch_session_on_host,
    ls_remote_exact,
    make_remote_and_source,
    poll_assignment,
    put_primary_binding,
    register_assignment_agent,
    register_repository,
    send_turn,
    set_collaboration,
    spawn_host,
    wait_host_online,
    wait_session_idle,
)
from tests.e2e.conftest import configure_mock_llm, reset_mock_llm, send_user_message_to_session
from tests.e2e.helpers import POLL_INTERVAL_S

_TIMEOUT_S = 240.0


@dataclass
class _OneHostEnv:
    """Booted server, one host, project, repo, binding and two agents."""

    server: AssignmentServer
    host: AssignmentHost
    client: httpx.Client
    mock_url: str
    project_id: str
    remote: Path
    source: Path
    head: str
    binding_workspace: str
    sender_model: str
    sender_session: str
    sender_runner_id: str
    receiver_name: str
    receiver_agent_id: str
    receiver_model: str


def _dispatch_arguments(
    receiver_agent_id: str, task: str, head: str, idempotency_key: str, host_id: str
) -> dict[str, Any]:
    """Build the ``sys_assignment_dispatch`` arguments for one handoff."""
    return {
        "target_agent_id": receiver_agent_id,
        "task": task,
        "repositories": [{"repository_name": "root", "commit": head}],
        "idempotency_key": idempotency_key,
        "host_id": host_id,
    }


def _find_receiver_session(
    client: httpx.Client, receiver_name: str, task_token: str, timeout: float = 120.0
) -> str:
    """Poll the session list until the coordinator's session for ``task`` shows."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(
            "/v1/sessions",
            params={"agent_name": receiver_name, "limit": 100, "visibility": "all"},
        )
        resp.raise_for_status()
        for session in resp.json().get("data", []):
            if task_token in str(session.get("title") or ""):
                return str(session["id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"no receiving session for task {task_token!r} within {timeout}s")


def _finish_receiver_side(
    env: _OneHostEnv,
    *,
    assignment_id: str,
    task_token: str,
    new_file: str,
) -> tuple[str, str, dict[str, Any]]:
    """Commit ``new_file`` in the worktree and complete via the receiver's turn.

    The turn itself may observe ``runner_disconnected``: once ``/finish``
    lands, the coordinator's terminal release stops the receiver's dedicated
    runner, which can win the race against the turn's final LLM call. The
    row reaching ``succeeded`` proves the tool ran, so assert on the row.

    :returns: ``(receiver_session_id, output_commit, succeeded_row)``.
    """
    receiver_session = _find_receiver_session(env.client, env.receiver_name, task_token)
    wait_session_idle(env.client, receiver_session)
    worktree = Path(env.binding_workspace) / ".omnigent" / "worktrees" / assignment_id / "root"
    (worktree / new_file).write_text(f"{task_token}\n")
    output_commit = commit_all(worktree, f"receiver work {task_token}")
    configure_mock_llm(
        env.mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"complete-{task_token}",
                        "name": "sys_assignment_complete",
                        "arguments": json.dumps(
                            {
                                "assignment_id": assignment_id,
                                "outputs": [{"repository_name": "root", "commit": output_commit}],
                                "summary": f"done {task_token}",
                            }
                        ),
                    }
                ]
            },
            {"text": "finished"},
        ],
        key=env.receiver_model,
    )
    send_user_message_to_session(
        env.client,
        session_id=receiver_session,
        content=f"{task_token}-finish please complete the assignment",
    )
    row = poll_assignment(
        env.client,
        assignment_id,
        want="succeeded",
        timeout=_TIMEOUT_S,
        server=env.server,
        hosts=[env.host],
    )
    return receiver_session, output_commit, row


@pytest.fixture
def one_host_env(tmp_path: Path, mock_llm_server_url: str) -> Iterator[_OneHostEnv]:
    """Server + one host + project + repo + binding + sender/receiver agents."""
    root = tmp_path / "assign"
    suffix = uuid.uuid4().hex[:12]
    stack = contextlib.ExitStack()
    try:
        server = boot_server(root, mock_llm_url=mock_llm_server_url, db_path=root / "assign.db")
        stack.callback(server.stop)
        client = httpx.Client(
            base_url=server.url, timeout=300, headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN}
        )
        stack.callback(client.close)
        host = spawn_host(
            server_url=server.url, mock_llm_url=mock_llm_server_url, home=root / "home-a"
        )
        stack.callback(host.stop)
        wait_host_online(client, host.host_id)
        reset_mock_llm(mock_llm_server_url)
        remote, source, head = make_remote_and_source(root / "git", "root")
        project_id = create_project(client, f"assign-{suffix}")
        set_collaboration(client, project_id, enabled=True, expected_revision=0)
        register_repository(client, project_id, "root", str(remote))
        binding = put_primary_binding(client, project_id, host.host_id, str(source), "root")
        sender_model = f"mock-assign-sender-{suffix}"
        receiver_model = f"mock-assign-receiver-{suffix}"
        _, sender_agent_id = register_assignment_agent(
            client,
            name=f"assign-sender-{suffix}",
            model=sender_model,
            mock_llm_url=mock_llm_server_url,
        )
        receiver_name, receiver_agent_id = register_assignment_agent(
            client,
            name=f"assign-receiver-{suffix}",
            model=receiver_model,
            mock_llm_url=mock_llm_server_url,
        )
        sender_session = create_session(client, sender_agent_id)
        client.patch(
            f"/v1/sessions/{sender_session}", json={"project_id": project_id}
        ).raise_for_status()
        sender_runner_id = launch_session_on_host(
            client, host.host_id, sender_session, str(source)
        )
        env = _OneHostEnv(
            server=server,
            host=host,
            client=client,
            mock_url=mock_llm_server_url,
            project_id=project_id,
            remote=remote,
            source=source,
            head=head,
            binding_workspace=binding["workspace"],
            sender_model=sender_model,
            sender_session=sender_session,
            sender_runner_id=sender_runner_id,
            receiver_name=receiver_name,
            receiver_agent_id=receiver_agent_id,
            receiver_model=receiver_model,
        )
    except BaseException:
        stack.close()
        raise
    try:
        yield env
    finally:
        stack.close()


def test_assignment_happy_path(one_host_env: _OneHostEnv) -> None:
    """Dispatch → prepare → running → complete → succeeded with real refs."""
    env = one_host_env
    sender_token = f"dispatch-{uuid.uuid4().hex[:12]}"
    task_token = f"task-{uuid.uuid4().hex[:12]}"
    idempotency_key = f"key-{uuid.uuid4().hex[:12]}"
    assignment_id = assignment_id_for(env.sender_session, idempotency_key)
    ensure_tool_advertised(
        env.client,
        env.mock_url,
        env.sender_session,
        env.sender_model,
        "sys_assignment_dispatch",
        server=env.server,
        hosts=[env.host],
    )
    # Same-id rebind after the surface exists: the dispatch turn below
    # proves the sender keeps its tools across the product's rebind path.
    env.client.patch(
        f"/v1/sessions/{env.sender_session}", json={"runner_id": env.sender_runner_id}
    ).raise_for_status()
    configure_mock_llm(
        env.mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "dispatch-1",
                        "name": "sys_assignment_dispatch",
                        "arguments": json.dumps(
                            _dispatch_arguments(
                                env.receiver_agent_id,
                                f"{task_token} implement the change",
                                env.head,
                                idempotency_key,
                                env.host.host_id,
                            )
                        ),
                    }
                ]
            },
            {"text": "dispatched"},
        ],
        key=env.sender_model,
    )
    configure_mock_llm(env.mock_url, [{"text": "standing by"}], key=env.receiver_model)
    status_before = git_ok(env.source, "status", "--porcelain")
    turn = send_turn(env.client, env.sender_session, f"{sender_token} hand off the work")
    assert turn["status"] == "completed", f"sender turn failed: {turn.get('error')}"
    row = poll_assignment(
        env.client,
        assignment_id,
        want="running",
        timeout=_TIMEOUT_S,
        server=env.server,
        hosts=[env.host],
    )
    assert row["task"].startswith(task_token)
    worktree = Path(env.binding_workspace) / ".omnigent" / "worktrees" / assignment_id / "root"
    assert worktree.is_dir(), f"worktree missing at {worktree}"
    assert Path(git_ok(worktree, "rev-parse", "--show-toplevel")).resolve() == worktree.resolve()
    assert git_ok(worktree, "rev-parse", "HEAD") == env.head
    assert git_ok(env.source, "status", "--porcelain") == status_before
    _, output_commit, row = _finish_receiver_side(
        env, assignment_id=assignment_id, task_token=task_token, new_file="done.txt"
    )
    assert row["outputs"] is not None and row["outputs"][0]["commit"] == output_commit
    assert ls_remote_exact(env.remote, row["outputs"][0]["ref"]) == output_commit


def test_assignment_switch_off_mid_flight(one_host_env: _OneHostEnv) -> None:
    """In-flight work finishes after the switch goes off; new dispatches refuse."""
    env = one_host_env
    task_token = f"task-{uuid.uuid4().hex[:12]}"
    idempotency_key = f"key-{uuid.uuid4().hex[:12]}"
    assignment_id = assignment_id_for(env.sender_session, idempotency_key)
    ensure_tool_advertised(
        env.client,
        env.mock_url,
        env.sender_session,
        env.sender_model,
        "sys_assignment_dispatch",
        server=env.server,
        hosts=[env.host],
    )
    configure_mock_llm(
        env.mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "dispatch-1",
                        "name": "sys_assignment_dispatch",
                        "arguments": json.dumps(
                            _dispatch_arguments(
                                env.receiver_agent_id,
                                f"{task_token} implement the change",
                                env.head,
                                idempotency_key,
                                env.host.host_id,
                            )
                        ),
                    }
                ]
            },
            {"text": "dispatched"},
        ],
        key=env.sender_model,
    )
    configure_mock_llm(env.mock_url, [{"text": "standing by"}], key=env.receiver_model)
    turn = send_turn(env.client, env.sender_session, f"{task_token}-send hand off the work")
    assert turn["status"] == "completed", f"sender turn failed: {turn.get('error')}"
    poll_assignment(
        env.client,
        assignment_id,
        want="running",
        timeout=_TIMEOUT_S,
        server=env.server,
        hosts=[env.host],
    )
    revision_resp = env.client.get(f"/v1/projects/{env.project_id}/collaboration")
    revision_resp.raise_for_status()
    revision = revision_resp.json()["revision"]
    set_collaboration(env.client, env.project_id, enabled=False, expected_revision=revision)
    _, output_commit, row = _finish_receiver_side(
        env, assignment_id=assignment_id, task_token=task_token, new_file="done.txt"
    )
    assert row["outputs"] is not None and row["outputs"][0]["commit"] == output_commit
    retry_key = f"key-{uuid.uuid4().hex[:12]}"
    retry_id = assignment_id_for(env.sender_session, retry_key)
    configure_mock_llm(
        env.mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "dispatch-2",
                        "name": "sys_assignment_dispatch",
                        "arguments": json.dumps(
                            _dispatch_arguments(
                                env.receiver_agent_id,
                                f"{task_token} again",
                                env.head,
                                retry_key,
                                env.host.host_id,
                            )
                        ),
                    }
                ]
            },
            {"text": "dispatched again"},
        ],
        key=env.sender_model,
    )
    retry_turn = send_turn(env.client, env.sender_session, f"{task_token}-retry hand off again")
    assert retry_turn["status"] == "completed", (
        f"sender retry turn failed: {retry_turn.get('error')}"
    )
    assert "collaboration disabled" in json.dumps(retry_turn)
    assert env.client.get(f"/v1/assignments/{retry_id}").status_code == 404
