"""End-to-end acceptance: a project's code location reaches the agent.

A real server, a real host daemon, and a real runner are driven end to end:
two repositories are registered on a project, each is bound to a folder on the
host, the host's live folder facts report a credential-free remote, the
``agent-code-note`` preview matches the startup text, and a session launched on
that host actually carries the block naming the code and related folders.
"""

from __future__ import annotations

import json
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    get_mock_requests,
    lookup_agent_id,
    poll_session_until_terminal,
    send_user_message_to_session,
    upload_agent,
)
from tests.e2e.helpers import final_assistant_text
from tests.e2e.test_host_e2e import (
    _spawn_host_daemon,
    _wait_for_host_online,
    _write_smoke_agent_yaml,
)

DEMO_REMOTE = "https://git.example.test/demo.git"
NOTES_REMOTE = "https://git.example.test/notes.git"
DEMO_REMOTE_WITH_CREDENTIAL = "https://someone:EXAMPLE@git.example.test/demo.git"


def _git(folder: Path, *args: str) -> None:
    """Run one git command in *folder*, raising on failure."""
    subprocess.run(
        ["git", *args],
        cwd=folder,
        check=True,
        capture_output=True,
    )


def _init_repo(folder: Path, remote_url: str) -> None:
    """Create a committed git repo on branch ``main`` with one remote."""
    folder.mkdir()
    _git(folder, "init", "-b", "main")
    (folder / "README.md").write_text("hello\n")
    _git(folder, "-c", "user.name=t", "-c", "user.email=t@example.test", "add", "-A")
    _git(folder, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-m", "init")
    _git(folder, "remote", "add", "origin", remote_url)


def _iter_strings(node: Any, path: str = "root") -> Iterator[tuple[str, str]]:
    """Yield ``(json_path, value)`` for every string nested in *node*."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_strings(value, f"{path}[{index}]")


def test_project_code_location_e2e(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A project's folders reach the agent's startup text through a real host."""
    marker = "PROJECT_CODE_LOCATION_OK"
    configure_mock_llm(mock_llm_server_url, [{"text": marker}])

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    host_proc = daemon.proc
    host_id = daemon.host_id

    try:
        _wait_for_host_online(http_client, host_id, timeout=30.0)

        # 1. Two folders: a code repo with a credentialled remote, and a
        #    related repo whose remote carries no credential.
        demo_folder = tmp_path / "demo"
        notes_folder = tmp_path / "notes"
        _init_repo(demo_folder, DEMO_REMOTE_WITH_CREDENTIAL)
        _init_repo(notes_folder, NOTES_REMOTE)

        # 2. Project, repositories, and one binding per repository on the host.
        created = http_client.post(
            "/v1/projects",
            json={"name": "code-location-e2e"},
        )
        created.raise_for_status()
        project_id = created.json()["id"]

        for name, remote, role, branch in (
            ("demo", DEMO_REMOTE, "code", "main"),
            ("notes", NOTES_REMOTE, "related", "main"),
        ):
            repo_resp = http_client.put(
                f"/v1/projects/{project_id}/repositories/{name}",
                json={"remote_url": remote, "default_branch": branch, "role": role},
            )
            repo_resp.raise_for_status()

        demo_binding_resp = http_client.put(
            f"/v1/projects/{project_id}/hosts/{host_id}/bindings/demo",
            json={
                "workspace": str(demo_folder),
                "repository_name": "demo",
                "is_primary": True,
                "enabled": True,
            },
        )
        demo_binding_resp.raise_for_status()
        demo_binding = demo_binding_resp.json()
        notes_binding_resp = http_client.put(
            f"/v1/projects/{project_id}/hosts/{host_id}/bindings/notes",
            json={
                "workspace": str(notes_folder),
                "repository_name": "notes",
                "enabled": True,
            },
        )
        notes_binding_resp.raise_for_status()
        notes_binding = notes_binding_resp.json()

        assert demo_binding["checked"] is True, demo_binding
        assert notes_binding["checked"] is True, notes_binding
        assert demo_binding["is_primary"] is True, demo_binding
        assert notes_binding["is_primary"] is False, notes_binding

        # The stored (canonical) folder is what the preview and the agent see.
        demo_ws = demo_binding["workspace"]
        notes_ws = notes_binding["workspace"]

        # 3. Live folder facts: the credential is gone from the remote.
        facts_resp = http_client.get(
            f"/v1/hosts/{host_id}/folder-facts",
            params={"path": demo_ws},
        )
        facts_resp.raise_for_status()
        facts = facts_resp.json()
        assert facts["is_repo"] is True, facts
        assert facts["branch"] == "main", facts
        assert facts["dirty"] is False, facts
        assert facts["remotes"] == [{"name": "origin", "url": DEMO_REMOTE}], facts

        # 4. The preview equals the exact two-line block, built by hand.
        demo_line = (
            f"- demo (the code you change): {json.dumps(demo_ws, ensure_ascii=False)}"
            f" — git {json.dumps(DEMO_REMOTE, ensure_ascii=False)}"
            f", default branch {json.dumps('main', ensure_ascii=False)}"
        )
        notes_line = (
            f"- notes (related code): {json.dumps(notes_ws, ensure_ascii=False)}"
            f" — git {json.dumps(NOTES_REMOTE, ensure_ascii=False)}"
        )
        expected_block = "\n".join(["This project's code on this host:", demo_line, notes_line])

        note_resp = http_client.get(f"/v1/projects/{project_id}/hosts/{host_id}/agent-code-note")
        note_resp.raise_for_status()
        note = note_resp.json()
        assert note["delivered"] is True, note
        assert note["text"] == expected_block, (
            f"agent-code-note text mismatch:\n{note['text']!r}\n!=\n{expected_block!r}"
        )

        # 5. A session in the project on the host, launched the golden-path way.
        agent_id = lookup_agent_id(
            http_client,
            upload_agent(http_client, _write_smoke_agent_yaml(tmp_path)),
        )
        # host_type is named explicitly so the project's single-host default
        # does not auto-launch the runner; the golden-path launch below binds
        # the host and workspace (and sets host_id for the code note).
        session_resp = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "project_id": project_id, "host_type": "external"},
        )
        session_resp.raise_for_status()
        session_id = session_resp.json()["id"]

        launch_resp = http_client.post(
            f"/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": demo_ws},
            timeout=60.0,
        )
        assert launch_resp.status_code == 200, (
            f"Launch failed: {launch_resp.status_code} {launch_resp.text}"
        )
        runner_id = launch_resp.json()["runner_id"]

        deadline = time.monotonic() + 30.0
        runner_online = False
        while time.monotonic() < deadline:
            status_resp = http_client.get(f"/v1/runners/{runner_id}/status")
            if status_resp.status_code == 200 and status_resp.json().get("online") is True:
                runner_online = True
                break
            time.sleep(0.5)
        assert runner_online, f"Runner {runner_id} never came online after launch"

        http_client.patch(
            f"/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
        ).raise_for_status()

        response_id = send_user_message_to_session(
            http_client,
            session_id=session_id,
            content=(
                f"Reply with exactly the literal string {marker} "
                "and nothing else. Do not call tools."
            ),
        )
        body = poll_session_until_terminal(
            http_client,
            session_id=session_id,
            response_id=response_id,
            timeout=180,
        )
        assert body["status"] == "completed", f"Session failed: {body.get('error')}"
        assert marker in final_assistant_text(body)

        # 6. The runner carried the block to the model in its request.
        carrier_path: str | None = None
        for request in get_mock_requests(mock_llm_server_url):
            for path, value in _iter_strings(request):
                if (
                    "This project's code on this host:" in value
                    and demo_line in value
                    and notes_line in value
                ):
                    carrier_path = path
                    break
            if carrier_path is not None:
                break

        assert carrier_path is not None, (
            "No captured LLM request carried the project code block "
            f"(looked for {demo_line!r} and {notes_line!r} in every string "
            "field of every request body)."
        )
        print(f"project code block carried in request field: {carrier_path}")

    finally:
        host_proc.send_signal(signal.SIGTERM)
        try:
            host_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            host_proc.kill()
            host_proc.wait()
