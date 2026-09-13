"""Offline-host project-assignment tests with two host daemons.

A waiting row pins its destination host: stopping host B parks the dispatch
in ``waiting`` with a reason naming B, and the same row reaches ``running``
once B returns — with no second dispatch. Restart recovery moves the same
row across a server restart on one database file.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.assignment_env import (
    AssignmentHost,
    AssignmentServer,
    assignment_id_for,
    boot_server,
    create_project,
    create_session,
    ensure_tool_advertised,
    launch_session_on_host,
    make_remote_and_source,
    poll_assignment,
    put_primary_binding,
    register_assignment_agent,
    register_repository,
    send_turn,
    set_collaboration,
    spawn_host,
    wait_host_offline,
    wait_host_online,
)
from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

_TIMEOUT_S = 240.0


@dataclass
class _TwoHostEnv:
    """Server plus sender host A, destination host B, and a bound sender."""

    root: Path
    server: AssignmentServer
    client: httpx.Client
    mock_url: str
    host_a: AssignmentHost
    host_b: AssignmentHost
    host_b_name: str
    project_id: str
    source: Path
    head: str
    sender_model: str
    sender_session: str
    receiver_agent_id: str
    receiver_model: str


def _new_client(url: str) -> httpx.Client:
    """Return a first-party client on ``url``."""
    return httpx.Client(base_url=url, timeout=300, headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN})


@pytest.fixture
def two_host_env(tmp_path: Path, mock_llm_server_url: str) -> Iterator[_TwoHostEnv]:
    """Server + hosts A/B + project + repo + bindings on one source checkout."""
    root = tmp_path / "assign"
    suffix = uuid.uuid4().hex[:12]
    stack = contextlib.ExitStack()
    try:
        server = boot_server(root, mock_llm_url=mock_llm_server_url, db_path=root / "assign.db")
        stack.callback(server.stop)
        client = _new_client(server.url)
        stack.callback(client.close)
        host_a = spawn_host(
            server_url=server.url, mock_llm_url=mock_llm_server_url, home=root / "home-a"
        )
        stack.callback(host_a.stop)
        host_b_id = uuid.uuid4().hex
        host_b_name = f"assign-b-{suffix}"
        host_b = spawn_host(
            server_url=server.url,
            mock_llm_url=mock_llm_server_url,
            home=root / "home-b",
            host_id=host_b_id,
            name=host_b_name,
        )
        stack.callback(host_b.stop)
        wait_host_online(client, host_a.host_id)
        wait_host_online(client, host_b.host_id)
        reset_mock_llm(mock_llm_server_url)
        remote, source, head = make_remote_and_source(root / "git", "root")
        project_id = create_project(client, f"assign-offline-{suffix}")
        set_collaboration(client, project_id, enabled=True, expected_revision=0)
        register_repository(client, project_id, "root", str(remote))
        put_primary_binding(client, project_id, host_a.host_id, str(source), "root")
        put_primary_binding(client, project_id, host_b.host_id, str(source), "root")
        sender_model = f"mock-assign-offline-sender-{suffix}"
        receiver_model = f"mock-assign-offline-receiver-{suffix}"
        _, sender_agent_id = register_assignment_agent(
            client,
            name=f"assign-offline-sender-{suffix}",
            model=sender_model,
            mock_llm_url=mock_llm_server_url,
        )
        _, receiver_agent_id = register_assignment_agent(
            client,
            name=f"assign-offline-receiver-{suffix}",
            model=receiver_model,
            mock_llm_url=mock_llm_server_url,
        )
        sender_session = create_session(client, sender_agent_id)
        client.patch(
            f"/v1/sessions/{sender_session}", json={"project_id": project_id}
        ).raise_for_status()
        launch_session_on_host(client, host_a.host_id, sender_session, str(source))
        env = _TwoHostEnv(
            root=root,
            server=server,
            client=client,
            mock_url=mock_llm_server_url,
            host_a=host_a,
            host_b=host_b,
            host_b_name=host_b_name,
            project_id=project_id,
            source=source,
            head=head,
            sender_model=sender_model,
            sender_session=sender_session,
            receiver_agent_id=receiver_agent_id,
            receiver_model=receiver_model,
        )
    except BaseException:
        stack.close()
        raise
    try:
        yield env
    finally:
        # Tests may respawn host B / reboot the server into new handles on
        # ``env``; stop those plus everything the stack registered.
        env.client.close()
        env.host_a.stop()
        env.host_b.stop()
        env.server.stop()
        stack.close()


def _dispatch_to_host_b(env: _TwoHostEnv, task_token: str) -> str:
    """Script one sender dispatch at host B and return the assignment id."""
    idempotency_key = f"key-{task_token}"
    assignment_id = assignment_id_for(env.sender_session, idempotency_key)
    ensure_tool_advertised(
        env.client,
        env.mock_url,
        env.sender_session,
        env.sender_model,
        "sys_assignment_dispatch",
        server=env.server,
        hosts=[env.host_a, env.host_b],
    )
    configure_mock_llm(
        env.mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"dispatch-{task_token}",
                        "name": "sys_assignment_dispatch",
                        "arguments": json.dumps(
                            {
                                "target_agent_id": env.receiver_agent_id,
                                "task": f"{task_token} implement the change",
                                "repositories": [{"repository_name": "root", "commit": env.head}],
                                "idempotency_key": idempotency_key,
                                "host_id": env.host_b.host_id,
                            }
                        ),
                    }
                ]
            },
            {"text": "dispatched"},
        ],
        key=env.sender_model,
    )
    configure_mock_llm(env.mock_url, [{"text": "standing by"}], key=env.receiver_model)
    turn = send_turn(env.client, env.sender_session, f"{task_token} hand off the work")
    assert turn["status"] == "completed", f"sender turn failed: {turn.get('error')}"
    return assignment_id


def test_offline_then_online(two_host_env: _TwoHostEnv) -> None:
    """A dispatch at a stopped host waits, then runs when it returns."""
    env = two_host_env
    env.host_b.stop()
    wait_host_offline(env.client, env.host_b.host_id)
    task_token = f"task-{uuid.uuid4().hex[:12]}"
    assignment_id = _dispatch_to_host_b(env, task_token)
    row = poll_assignment(
        env.client,
        assignment_id,
        want="waiting",
        timeout=_TIMEOUT_S,
        server=env.server,
        hosts=[env.host_a, env.host_b],
        wait_reason_contains=env.host_b.host_id,
    )
    assert env.host_b.host_id in str(row.get("wait_reason") or "")
    env.host_b = spawn_host(
        server_url=env.server.url,
        mock_llm_url=env.mock_url,
        home=env.root / "home-b",
        host_id=env.host_b.host_id,
        name=env.host_b_name,
    )
    wait_host_online(env.client, env.host_b.host_id)
    row = poll_assignment(
        env.client,
        assignment_id,
        want="running",
        timeout=_TIMEOUT_S,
        server=env.server,
        hosts=[env.host_a, env.host_b],
    )
    assert row["resolved_host_id"] == env.host_b.host_id


def test_restart_recovery(two_host_env: _TwoHostEnv) -> None:
    """A waiting row survives a server restart and is placed with no new dispatch.

    Reaching ``running`` after the restart exercises the real-process path;
    host B's reconnection also schedules the row, so this cannot isolate the
    startup due-work pass. That pass is proven separately by
    ``tests/server/test_assignment_coordinator.py::test_startup_due_pass_runs_without_trigger``.
    """
    env = two_host_env
    env.host_b.stop()
    wait_host_offline(env.client, env.host_b.host_id)
    task_token = f"task-{uuid.uuid4().hex[:12]}"
    assignment_id = _dispatch_to_host_b(env, task_token)
    poll_assignment(
        env.client,
        assignment_id,
        want="waiting",
        timeout=_TIMEOUT_S,
        server=env.server,
        hosts=[env.host_a, env.host_b],
        wait_reason_contains=env.host_b.host_id,
    )
    port = env.server.port
    db_path = env.server.db_path
    env.server.stop()
    env.host_b = spawn_host(
        server_url=env.server.url,
        mock_llm_url=env.mock_url,
        home=env.root / "home-b",
        host_id=env.host_b.host_id,
        name=env.host_b_name,
    )
    env.server = boot_server(env.root, mock_llm_url=env.mock_url, db_path=db_path, port=port)
    env.client.close()
    env.client = _new_client(env.server.url)
    wait_host_online(env.client, env.host_a.host_id)
    wait_host_online(env.client, env.host_b.host_id)
    row = poll_assignment(
        env.client,
        assignment_id,
        want="running",
        timeout=_TIMEOUT_S,
        server=env.server,
        hosts=[env.host_a, env.host_b],
    )
    assert row["resolved_host_id"] == env.host_b.host_id
