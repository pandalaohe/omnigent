"""Runner-side cross-host member dispatch (SCC06 F2b, Step A).

A member whose snapshot ``host`` differs from the lead session's host runs on
that host: the runner re-checks the target host online / harness readiness at
dispatch, resolves the lead's branch worktree on that host through the server,
and creates the child there with ``host_id`` + ``workspace`` — no local CLI
probe, no local model normalization, and no inherited runner. Same-host
members keep the existing local path.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.member_snapshot import encode_member_entry, member_label_key

_LEAD_HOST = "host_lead"
_MEMBER_HOST = "host_member"
_CHILD_ID = "conv_child_remote"
_MEMBER_MODEL = "databricks-claude-haiku-4-5"
_PARENT_MODEL = "databricks-claude-sonnet-4-6"
_WORKSPACE = "/host-b/repo/.worktrees/feature-x"


def _spec_with_worker(harness: str) -> SimpleNamespace:
    """Build a parent-spec stub declaring one ``worker`` sub-agent."""
    executor = SimpleNamespace(type="omnigent", config={"harness": harness})
    return SimpleNamespace(sub_agents=[SimpleNamespace(name="worker", executor=executor)])


def _member_labels(**entry: object) -> dict[str, str]:
    """One member snapshot label for ``worker`` with sensible overrides."""
    payload: dict[str, object] = {
        "host": _MEMBER_HOST,
        "harness": "claude-sdk",
        "model": _MEMBER_MODEL,
        "effort": "high",
        "lead": False,
    }
    payload.update(entry)
    return {member_label_key("worker"): encode_member_entry(payload)}


async def _dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    labels: dict[str, str],
    lead_host: str | None = _LEAD_HOST,
    host_status: str = "online",
    configured_harnesses: dict[str, object] | None = None,
    worktree_status: int = 200,
    worktree_body: dict[str, Any] | None = None,
    child_runner_id: str | None = "runner_member",
    existing_child: dict[str, Any] | None = None,
    keep_work: bool = False,
    events_status: int = 202,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """
    Drive one named ``sys_session_send`` against a remote-member mock server.

    :param existing_child: When set, the child-sessions lookup returns it, so
        the send continues that child instead of creating one.
    :param keep_work: Leave the registered work entry in the registry (the
        caller cleans up) so it can be handed to the launch reaper.
    :param events_status: Status the child message POST answers with.
    :returns: ``(tool_output, create_bodies, calls)`` where *calls* counts the
        host / worktree lookups and captures the final work entry.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    create_bodies: list[dict[str, Any]] = []
    calls: dict[str, Any] = {"host": 0, "worktree": 0, "work_status": None}
    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_member_cross_host/labels":
            return httpx.Response(200, json={"labels": labels})
        if (
            request.method == "GET"
            and path == "/v1/sessions/conv_member_cross_host/child_sessions"
        ):
            return httpx.Response(
                200, json={"data": [existing_child] if existing_child is not None else []}
            )
        if request.method == "GET" and path == "/v1/sessions/conv_member_cross_host":
            return httpx.Response(
                200,
                json={
                    "id": "conv_member_cross_host",
                    "agent_id": "ag_parent",
                    "host_id": lead_host,
                },
            )
        if request.method == "PATCH" and path == f"/v1/sessions/{_CHILD_ID}":
            return httpx.Response(200, json={"id": _CHILD_ID})
        if request.method == "GET" and path == f"/v1/hosts/{_MEMBER_HOST}":
            calls["host"] += 1
            return httpx.Response(
                200,
                json={
                    "host_id": _MEMBER_HOST,
                    "status": host_status,
                    "configured_harnesses": configured_harnesses
                    if configured_harnesses is not None
                    else {"claude-sdk": True},
                },
            )
        if request.method == "GET" and path == (
            "/v1/sessions/conv_member_cross_host/member-worktree"
        ):
            calls["worktree"] += 1
            if worktree_status != 200:
                return httpx.Response(
                    worktree_status,
                    json={
                        "error": {
                            "code": "invalid_input",
                            "message": "branch 'feature-x' is not checked out in a worktree",
                        }
                    },
                )
            body = worktree_body if worktree_body is not None else {"workspace": _WORKSPACE}
            return httpx.Response(200, json=body)
        if request.method == "POST" and path == "/v1/sessions":
            create_bodies.append(json.loads(request.content))
            payload: dict[str, Any] = {"id": _CHILD_ID}
            if child_runner_id is not None:
                payload["runner_id"] = child_runner_id
            return httpx.Response(201, json=payload)
        if request.method == "POST" and path == f"/v1/sessions/{_CHILD_ID}/events":
            return httpx.Response(events_status, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {"agent": "worker", "title": "task", "args": {"input": "go"}}
                ),
                server_client=server_client,
                conversation_id="conv_member_cross_host",
                agent_spec=_spec_with_worker("claude-sdk"),
                session_inbox=session_inbox,
            )
        finally:
            work = runner_app.get_subagent_work(_CHILD_ID)
            calls["work_status"] = work.status if work is not None else None
            if keep_work:
                calls["work_entry"] = work
            else:
                runner_app.unregister_subagent_work(_CHILD_ID)
            runner_app._session_inboxes_ref.pop("conv_member_cross_host", None)
    return output, create_bodies, calls


@pytest.mark.asyncio
async def test_offline_member_host_is_refused_before_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offline member host is a tool error naming role, host, and reason."""
    output, bodies, calls = await _dispatch(
        monkeypatch, labels=_member_labels(), host_status="offline"
    )

    assert output.startswith("Error:")
    assert "'worker'" in output
    assert _MEMBER_HOST in output
    assert "offline" in output
    assert bodies == []
    # The worktree lookup is never reached for a refused host.
    assert calls == {"host": 1, "worktree": 0, "work_status": None}


@pytest.mark.asyncio
async def test_harness_not_ready_on_member_host_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harness the host reports not-ready refuses the dispatch, no child."""
    output, bodies, _calls = await _dispatch(
        monkeypatch,
        labels=_member_labels(),
        configured_harnesses={"claude-sdk": "needs-auth"},
    )

    assert output.startswith("Error:")
    assert "claude-sdk" in output
    assert "needs-auth" in output
    assert bodies == []


@pytest.mark.asyncio
async def test_missing_member_worktree_is_refused_with_the_server_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No matching worktree on the host → tool error carrying the server reason."""
    output, bodies, calls = await _dispatch(
        monkeypatch, labels=_member_labels(), worktree_status=400
    )

    assert output.startswith("Error:")
    assert "'worker'" in output and _MEMBER_HOST in output
    assert "not checked out in a worktree" in output
    assert bodies == []
    assert calls == {"host": 1, "worktree": 1, "work_status": None}


@pytest.mark.asyncio
async def test_remote_member_creates_the_child_on_its_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: host_id + resolved workspace, frozen model as is, started work."""
    output, bodies, calls = await _dispatch(monkeypatch, labels=_member_labels())

    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert len(bodies) == 1
    body = bodies[0]
    assert body["host_id"] == _MEMBER_HOST
    assert body["workspace"] == _WORKSPACE
    # The snapshot model is already the target host's spelling: no local
    # normalization, no inherited parent model.
    assert body["model_override"] == _MEMBER_MODEL
    assert body["reasoning_effort"] == "high"
    assert "harness_override" not in body
    assert calls == {"host": 1, "worktree": 1, "work_status": "running"}, (
        "a bound remote child must be marked started so the launch reaper does not fail it"
    )


@pytest.mark.asyncio
async def test_remote_member_skips_the_local_cli_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local missing-CLI probe never runs for a remote member."""
    from omnigent.onboarding import harness_install

    def _fail_probe(_harness: str) -> None:
        raise AssertionError("local CLI probe must not run for a remote member")

    monkeypatch.setattr(harness_install, "missing_harness_cli", _fail_probe)
    output, bodies, _calls = await _dispatch(monkeypatch, labels=_member_labels())

    assert json.loads(output)["status"] == "launching", output
    assert bodies and bodies[0]["host_id"] == _MEMBER_HOST


@pytest.mark.asyncio
async def test_same_host_member_keeps_the_local_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member on the lead's own host stays local: no host_id / workspace."""
    from omnigent.onboarding import harness_install

    probed: list[str] = []
    monkeypatch.setattr(
        harness_install, "missing_harness_cli", lambda harness: probed.append(harness) or None
    )
    output, bodies, calls = await _dispatch(
        monkeypatch,
        labels=_member_labels(host=_LEAD_HOST),
        configured_harnesses=None,
    )

    assert json.loads(output)["status"] == "launching", output
    assert len(bodies) == 1
    assert "host_id" not in bodies[0]
    assert "workspace" not in bodies[0]
    assert probed == ["claude-sdk"], "the local CLI probe still gates a same-host member"
    # A local child stays "launching" until its own running edge arrives.
    assert calls == {"host": 0, "worktree": 0, "work_status": "launching"}


@pytest.mark.asyncio
async def test_snapshot_member_without_host_stays_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member with no snapshot host is not remote even when the lead has one."""
    output, bodies, calls = await _dispatch(
        monkeypatch, labels=_member_labels(host=None), configured_harnesses=None
    )

    assert json.loads(output)["status"] == "launching", output
    assert "host_id" not in bodies[0]
    assert calls == {"host": 0, "worktree": 0, "work_status": "launching"}


def _existing_child(*, title: str = "worker:task") -> dict[str, Any]:
    """A child-session summary the named ``(agent, title)`` lookup matches."""
    return {"id": _CHILD_ID, "title": title, "labels": {}, "busy": False}


@pytest.mark.asyncio
async def test_continuation_to_an_existing_remote_child_is_marked_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continued remote child must not trip the 180 s launch reaper.

    Its next turn's running edge stays on its own host's runner, so the
    continuation marks the work started from the child's live session there.
    """
    from omnigent.runner import app as runner_app

    try:
        output, bodies, calls = await _dispatch(
            monkeypatch,
            labels=_member_labels(),
            existing_child=_existing_child(),
            keep_work=True,
        )

        payload = json.loads(output)
        assert payload["status"] == "launching", output
        assert bodies == [], "a continuation must not create a child"
        entry = calls["work_entry"]
        assert entry is not None and entry.status == "running"
        assert entry.remote is True
        assert (
            runner_app.reap_stalled_subagent_launches(now=entry.created_at + 181, timeout_s=180)
            == []
        )
        assert runner_app.get_subagent_work(_CHILD_ID) is not None
    finally:
        runner_app.unregister_subagent_work(_CHILD_ID)
        runner_app._session_inboxes_ref.pop("conv_member_cross_host", None)


@pytest.mark.asyncio
async def test_continuation_is_marked_started_only_after_its_message_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation whose message POST fails never becomes started work.

    Before the POST the server row still carries the previous turn's terminal,
    so marking the dispatch started early would let the reconciliation backstop
    read that terminal as this dispatch's result.
    """
    from omnigent.runner import app as runner_app

    started: list[str] = []
    monkeypatch.setattr(
        runner_app, "mark_subagent_work_started", lambda cid: started.append(cid) or None
    )

    output, bodies, calls = await _dispatch(
        monkeypatch,
        labels=_member_labels(),
        existing_child=_existing_child(),
        events_status=500,
    )

    assert output.startswith("Error:")
    assert bodies == [], "a continuation must not create a child"
    assert started == [], "a failed POST must not mark the remote work started"
    assert calls["work_status"] is None, "the failed dispatch leaves no work entry"


@pytest.mark.asyncio
async def test_by_id_continuation_of_a_remote_child_is_marked_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A by-session-id send to a remote child gets the same start proof."""
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    child_id = "conv_child_remote_by_id"
    calls = {"create": 0, "events": 0, "patch": 0}

    async def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_member_cross_host/labels":
            return httpx.Response(200, json={"labels": {}})
        if request.method == "GET" and path == "/v1/sessions/conv_member_cross_host":
            return httpx.Response(
                200, json={"id": "conv_member_cross_host", "host_id": _LEAD_HOST, "labels": {}}
            )
        if request.method == "GET" and path == f"/v1/sessions/{child_id}":
            return httpx.Response(
                200,
                json={
                    "id": child_id,
                    "title": "worker:task",
                    "parent_session_id": "conv_member_cross_host",
                    "host_id": _MEMBER_HOST,
                    "labels": {},
                    "busy": False,
                },
            )
        if request.method == "PATCH" and path == f"/v1/sessions/{child_id}":
            calls["patch"] += 1
            return httpx.Response(200, json={"id": child_id})
        if request.method == "POST" and path == f"/v1/sessions/{child_id}/events":
            calls["events"] += 1
            return httpx.Response(202, json={"queued": True})
        if request.method == "POST" and path == "/v1/sessions":
            calls["create"] += 1
            return httpx.Response(201, json={"id": "conv_other"})
        return httpx.Response(404, json={"error": str(request.url)})

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_handler), base_url="http://server"
        ) as server_client:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps({"session_id": child_id, "args": "go"}),
                server_client=server_client,
                conversation_id="conv_member_cross_host",
                agent_spec=_spec_with_worker("claude-sdk"),
                session_inbox=asyncio.Queue(),
            )

        assert json.loads(output)["status"] == "launching", output
        assert calls["create"] == 0
        assert calls["patch"] == 1 and calls["events"] == 1
        entry = runner_app.get_subagent_work(child_id)
        assert entry is not None and entry.status == "running"
        assert entry.remote is True
        assert (
            runner_app.reap_stalled_subagent_launches(now=entry.created_at + 181, timeout_s=180)
            == []
        )
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop("conv_member_cross_host", None)
