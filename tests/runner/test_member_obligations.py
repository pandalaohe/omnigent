"""Role routing obligations and visible notices for joint-agent leads (SCC06 F1a-2).

A user message that names ``@role`` / ``[role]`` members becomes a set of
dispatch obligations for the lead's turn: the note prepended to the lead's
input asks for a ``sys_session_send(agent=role)``, a successful child create
meets the obligation, and one unmet follow-up is sent at turn end. Still unmet
(or the dispatch/child run failed) turns into a visible chat notice
``"<role> did not run: <reason>"`` — never silence. Sessions without member
labels behave exactly as before.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from omnigent.member_snapshot import encode_member_entry, member_label_key, parse_role_mentions
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _BlockingHarnessClient,
    _FakeProcessManager,
    _ordered_user_texts,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

PARENT = "conv_joint_lead"
WORKER_CHILD = "conv_member_child"
LEAD_ROLE = "orchestrator"
WORKER_ROLE = "executor"


# --------------------------------------------------------------------------
# parse_role_mentions — the pure mention parser
# --------------------------------------------------------------------------


def test_parse_role_mentions_both_forms_and_segments() -> None:
    """``@role`` and ``[role]`` match, and each segment runs to the next mention."""
    text = "please @executor build the parser, then [reviewer] check it"
    assert parse_role_mentions(text, ["executor", "reviewer"]) == [
        ("executor", "build the parser, then"),
        ("reviewer", "check it"),
    ]


def test_parse_role_mentions_exact_names_only() -> None:
    """A longer token, an email, or unknown text never matches a role."""
    roles = ["executor"]
    assert parse_role_mentions("@executor-two do it", roles) == []
    assert parse_role_mentions("mail me at a@executor.example", roles) == []
    assert parse_role_mentions("[executors] please", roles) == []


def test_parse_role_mentions_ignores_attached_preamble() -> None:
    """The web's ``[Attached: …]`` preamble never parses as a mention."""
    assert parse_role_mentions("[Attached: /tmp/notes.txt] please run", ["executor"]) == []
    assert parse_role_mentions("[Attached: /tmp/x] [executor] go", ["executor"]) == [
        ("executor", "go")
    ]


def test_parse_role_mentions_ignores_role_text_inside_attachment_marker() -> None:
    """An ``@`` inside an ``[Attached: …]`` path is not a mention (review probe)."""
    assert (
        parse_role_mentions(
            "[Attached: /tmp/@executor.txt] summarize this file",
            ["executor"],
        )
        == []
    )
    assert parse_role_mentions("@executor read [Attached: /tmp/@executor.txt]", ["executor"]) == [
        ("executor", "read")
    ]


def test_parse_role_mentions_repeats_and_empty_cases() -> None:
    """Repeated mentions yield repeated pairs; no roles / no text match nothing."""
    assert parse_role_mentions("[executor] one @executor two", ["executor"]) == [
        ("executor", "one"),
        ("executor", "two"),
    ]
    assert parse_role_mentions("@executor go", []) == []
    assert parse_role_mentions("nothing to see", ["executor"]) == []


# --------------------------------------------------------------------------
# Harness for the runner-level tests
# --------------------------------------------------------------------------


def _member_labels(
    *,
    worker_unavailable: str | None = None,
    roles: tuple[str, ...] = (LEAD_ROLE, WORKER_ROLE),
) -> dict[str, str]:
    """Member snapshot labels: a lead plus one worker (and optional extras)."""
    entries: dict[str, dict[str, Any]] = {
        LEAD_ROLE: {
            "host": None,
            "harness": "claude-sdk",
            "model": "model-lead",
            "effort": None,
            "lead": True,
        },
        WORKER_ROLE: {
            "host": None,
            "harness": "claude-sdk",
            "model": "model-worker",
            "effort": "high",
            "lead": False,
        },
    }
    if worker_unavailable is not None:
        entries[WORKER_ROLE]["unavailable"] = worker_unavailable
    extra = {"researcher": {"harness": "claude-sdk", "model": None, "effort": None, "lead": False}}
    for role in roles:
        if role not in entries:
            entries[role] = dict(extra.get(role, {"lead": False}))
    return {member_label_key(role): encode_member_entry(entry) for role, entry in entries.items()}


class _MemberServerClient(NullServerClient):
    """Server client stub that records every posted event body."""

    def __init__(self) -> None:
        """Start with an empty post log."""
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> Any:
        """Record the posted JSON payload, then answer like the null base."""
        self.posts.append((url, kwargs.get("json") or {}))
        return await super().post(url, **kwargs)


def _build_app(
    server_client: _MemberServerClient,
    *,
    harness: str | None = None,
    harness_client: _ScriptedHarnessClient | None = None,
) -> tuple[Any, _FakeProcessManager, _ScriptedHarnessClient]:
    """Wire a runner app with a scripted harness and the member server stub."""
    spec = (
        AgentSpec(spec_version=1, name="t")
        if harness is None
        else AgentSpec(
            spec_version=1,
            name="t",
            executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
        )
    )
    if harness_client is None:
        harness_client = _ScriptedHarnessClient(
            [
                _sse({"type": "response.created", "response": {"id": "resp_1"}}),
                _sse({"type": "response.output_text.delta", "delta": "done"}),
                _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
            ]
        )
    process_manager = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=process_manager,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    return app, process_manager, harness_client


async def _seed_session(
    client: httpx.AsyncClient,
    *,
    labels: dict[str, str] | None = None,
    session_id: str = PARENT,
) -> None:
    """Register the session and its member snapshot, so turns resolve the stub spec."""
    if labels is not None:
        runner_app.set_session_member_entries(session_id, labels)
    await client.post(
        "/v1/sessions",
        json={"session_id": session_id, "agent_id": "ag_joint_lead"},
    )


async def _post_message(
    client: httpx.AsyncClient,
    text: str,
    session_id: str = PARENT,
    *,
    agent_id: str = "ag_joint_lead",
) -> httpx.Response:
    """POST one user message into the runner's session-events endpoint."""
    return await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "role": "user",
            "agent_id": agent_id,
            "content": [{"type": "input_text", "text": text}],
        },
    )


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Poll *predicate* until true, failing the test after *timeout* seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)


def _events_of_type(server: _MemberServerClient, event_type: str) -> list[dict[str, Any]]:
    """Every recorded POST body of *event_type* (in order)."""
    return [
        payload
        for url, payload in server.posts
        if url.endswith("/events") and payload.get("type") == event_type
    ]


def _notices(server: _MemberServerClient) -> list[dict[str, Any]]:
    """Every recorded visible notice item (``external_conversation_item``)."""
    return _events_of_type(server, "external_conversation_item")


@pytest.fixture(autouse=True)
def _clean_member_obligations() -> Iterator[None]:
    """Keep the module-level obligation, member, and history state per-test."""
    saved = {
        session: dict(obligations)
        for session, obligations in runner_app._member_obligations.items()
    }
    saved_members = {
        session: dict(entries) for session, entries in runner_app._session_member_entries.items()
    }
    saved_histories = {
        session: list(items) for session, items in runner_app._session_histories_ref.items()
    }
    runner_app._member_obligations.clear()
    runner_app._session_member_entries.clear()
    runner_app._session_histories_ref.clear()
    try:
        yield
    finally:
        runner_app._member_obligations.clear()
        runner_app._member_obligations.update(saved)
        runner_app._session_member_entries.clear()
        runner_app._session_member_entries.update(saved_members)
        runner_app._session_histories_ref.clear()
        runner_app._session_histories_ref.update(saved_histories)


@pytest.fixture
def _clean_subagent_registry() -> Iterator[None]:
    """Snapshot/restore the sub-agent work registry and inbox queues."""
    saved_work = dict(runner_app._subagent_work_by_child)
    saved_parent = {key: set(value) for key, value in runner_app._subagent_work_by_parent.items()}
    saved_inboxes = dict(runner_app._session_inboxes_ref)
    runner_app._subagent_work_by_child.clear()
    runner_app._subagent_work_by_parent.clear()
    runner_app._session_inboxes_ref.clear()
    try:
        yield
    finally:
        runner_app._subagent_work_by_child.clear()
        runner_app._subagent_work_by_child.update(saved_work)
        runner_app._subagent_work_by_parent.clear()
        runner_app._subagent_work_by_parent.update(saved_parent)
        runner_app._session_inboxes_ref.clear()
        runner_app._session_inboxes_ref.update(saved_inboxes)


# --------------------------------------------------------------------------
# Note prepending and obligation recording
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_note_prepended_for_named_non_lead_role() -> None:
    """Naming a member prepends the dispatch note and opens its obligation."""
    server = _MemberServerClient()
    app, _pm, harness = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client, labels=_member_labels())
        assert (
            await _post_message(client, "@executor build the parser please")
        ).status_code == 202
        await _wait_until(lambda: bool(harness.posted_bodies))

    texts = _ordered_user_texts(harness.posted_bodies[0])
    assert len(texts) == 2, texts
    note, user_text = texts
    assert note.startswith("[System:")
    assert "'executor'" in note
    assert "'build the parser please'" in note
    assert "sys_session_send" in note
    assert user_text == "@executor build the parser please"
    obligations = runner_app.list_member_obligations(PARENT)
    assert list(obligations) == ["executor"]
    assert obligations["executor"].child_session_id is None


@pytest.mark.asyncio
async def test_naming_the_lead_adds_no_note() -> None:
    """The lead's own name is not an obligation — its turn is that part already."""
    server = _MemberServerClient()
    app, _pm, harness = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client, labels=_member_labels())
        await _post_message(client, f"@{LEAD_ROLE} do it yourself")
        await _wait_until(lambda: bool(harness.posted_bodies))

    assert _ordered_user_texts(harness.posted_bodies[0]) == [f"@{LEAD_ROLE} do it yourself"]
    assert runner_app.list_member_obligations(PARENT) == {}


@pytest.mark.asyncio
async def test_message_without_mentions_is_untouched() -> None:
    """Unnamed work stays with the lead: no note, no obligation."""
    server = _MemberServerClient()
    app, _pm, harness = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client, labels=_member_labels())
        await _post_message(client, "just do the thing")
        await _wait_until(lambda: bool(harness.posted_bodies))

    assert _ordered_user_texts(harness.posted_bodies[0]) == ["just do the thing"]
    assert runner_app.list_member_obligations(PARENT) == {}


@pytest.mark.asyncio
async def test_session_without_members_is_untouched() -> None:
    """A session with fewer than two member labels behaves exactly as today."""
    server = _MemberServerClient()
    app, _pm, harness = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(
            client,
            labels={member_label_key(LEAD_ROLE): encode_member_entry({"lead": True})},
        )
        await _post_message(client, "@executor build it")
        await _wait_until(lambda: bool(harness.posted_bodies))

    assert _ordered_user_texts(harness.posted_bodies[0]) == ["@executor build it"]
    assert runner_app.list_member_obligations(PARENT) == {}


@pytest.mark.asyncio
async def test_unavailable_named_role_gets_immediate_notice_not_note() -> None:
    """An unavailable member is noticed right away; no note, no obligation."""
    server = _MemberServerClient()
    app, _pm, harness = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client, labels=_member_labels(worker_unavailable="host_offline"))
        response = await _post_message(client, "[executor] build it")
        assert response.status_code == 202
        await _wait_until(lambda: bool(harness.posted_bodies))

    assert _ordered_user_texts(harness.posted_bodies[0]) == ["[executor] build it"]
    assert runner_app.list_member_obligations(PARENT) == {}
    assert _notices(server) == [
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "error",
                "item_data": {
                    "source": "execution",
                    "code": "member_did_not_run",
                    "message": "executor did not run: host_offline",
                    "level": "info",
                },
            },
        }
    ]


# --------------------------------------------------------------------------
# Met on child create; failure recorded from a failed dispatch
# --------------------------------------------------------------------------


async def _run_named_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    create_status: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Drive one named ``sys_session_send`` against a mock server.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param create_status: Status the child-create POST answers.
    :returns: ``(tool_output, create_bodies)``.
    """
    from types import SimpleNamespace

    from omnigent.onboarding import harness_install
    from omnigent.runner.tool_dispatch import execute_tool

    monkeypatch.setattr(harness_install, "missing_harness_cli", lambda _harness: None)
    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_joint_lead")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)

    agent_spec = SimpleNamespace(
        sub_agents=[
            SimpleNamespace(
                name="executor",
                executor=SimpleNamespace(type="omnigent", config={"harness": "claude-sdk"}),
            )
        ]
    )
    create_bodies: list[dict[str, Any]] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/sessions/{PARENT}/labels":
            return httpx.Response(200, json={"labels": _member_labels()})
        if request.method == "GET" and request.url.path == f"/v1/sessions/{PARENT}":
            return httpx.Response(
                200,
                json={
                    "id": PARENT,
                    "agent_id": "ag_joint_lead",
                    "harness": "claude-sdk",
                    "model_override": None,
                    "llm_model": None,
                },
            )
        if request.method == "GET" and request.url.path == (
            f"/v1/sessions/{PARENT}/child_sessions"
        ):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_bodies.append(json.loads(request.content))
            if create_status >= 400:
                return httpx.Response(create_status, json={"error": "boom"})
            return httpx.Response(201, json={"id": WORKER_CHILD})
        if request.method == "POST" and request.url.path == f"/v1/sessions/{WORKER_CHILD}/events":
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://server"
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps({"agent": WORKER_ROLE, "title": "task", "args": "do it"}),
                server_client=server_client,
                conversation_id=PARENT,
                agent_spec=agent_spec,
                session_inbox=session_inbox,
            )
        finally:
            runner_app.unregister_subagent_work(WORKER_CHILD)
            runner_app._session_inboxes_ref.pop(PARENT, None)
    return output, create_bodies


@pytest.mark.asyncio
async def test_child_create_meets_the_obligation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful named dispatch records the child id on the obligation."""
    runner_app.record_member_obligation(PARENT, WORKER_ROLE)

    output, create_bodies = await _run_named_dispatch(monkeypatch, create_status=201)

    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert len(create_bodies) == 1
    obligation = runner_app.list_member_obligations(PARENT)[WORKER_ROLE]
    assert obligation.child_session_id == WORKER_CHILD
    assert obligation.failure_reason is None


@pytest.mark.asyncio
async def test_failed_child_create_records_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dispatch that errors records why, for the turn-end notice to quote."""
    runner_app.record_member_obligation(PARENT, WORKER_ROLE)

    output, create_bodies = await _run_named_dispatch(monkeypatch, create_status=500)

    assert output.startswith("Error:")
    assert create_bodies
    obligation = runner_app.list_member_obligations(PARENT)[WORKER_ROLE]
    assert obligation.child_session_id is None
    assert obligation.failure_reason is not None
    assert "failed to create child session" in obligation.failure_reason


# --------------------------------------------------------------------------
# Turn end: one follow-up, then a visible notice
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmet_role_gets_exactly_one_follow_up_then_a_notice() -> None:
    """Unmet at turn end → one follow-up; still unmet → one notice, dropped."""
    server = _MemberServerClient()
    app, _pm, _harness = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client, labels=_member_labels())
        await _post_message(client, "[executor] build the parser")
        await _wait_until(lambda: bool(_events_of_type(server, "message")))

        follow_ups = _events_of_type(server, "message")
        assert len(follow_ups) == 1
        follow_up_text = follow_ups[0]["data"]["content"][0]["text"]
        assert "executor" in follow_up_text
        assert "sys_session_send" in follow_up_text
        assert runner_app.list_member_obligations(PARENT)["executor"].followed_up is True

        # The follow-up turn ends still unmet → one visible notice, obligation gone.
        await _post_message(client, "carry on")
        await _wait_until(lambda: bool(_notices(server)))

    notices = _notices(server)
    assert len(notices) == 1
    item_data = notices[0]["data"]["item_data"]
    assert item_data["message"] == "executor did not run: not dispatched after the follow-up"
    assert item_data["level"] == "info"
    assert runner_app.list_member_obligations(PARENT) == {}
    # Never a second follow-up.
    assert len(_events_of_type(server, "message")) == 1


@pytest.mark.asyncio
async def test_failed_dispatch_notices_at_turn_end_without_a_follow_up() -> None:
    """A recorded dispatch failure goes straight to the notice, not a reminder."""
    gate = asyncio.Event()
    harness = _BlockingHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ],
        gate,
    )
    server = _MemberServerClient()
    app, _pm, _harness = _build_app(server, harness_client=harness)

    async with _runner_client(app) as client:
        await _seed_session(client, labels=_member_labels())
        await _post_message(client, "@executor build the parser")
        await asyncio.wait_for(harness.post_seen.wait(), timeout=5.0)
        runner_app.mark_member_obligation_failed(
            PARENT, "executor", "failed to create child session: 500 boom"
        )
        gate.set()
        await _wait_until(lambda: bool(_notices(server)))

    assert _events_of_type(server, "message") == []
    assert _notices(server)[0]["data"]["item_data"]["message"] == (
        "executor did not run: failed to create child session: 500 boom"
    )
    assert runner_app.list_member_obligations(PARENT) == {}


@pytest.mark.asyncio
async def test_child_failure_posts_the_notice_and_closes(
    _clean_subagent_registry: None,
) -> None:
    """A met obligation closes on the child's result; a failure becomes a notice."""
    server = _MemberServerClient()
    app, _pm, _harness = _build_app(server)

    async with _runner_client(app) as client:
        runner_app.record_member_obligation(PARENT, WORKER_ROLE)
        runner_app.mark_member_obligation_met(PARENT, WORKER_ROLE, WORKER_CHILD)
        runner_app._session_inboxes_ref[PARENT] = asyncio.Queue()
        runner_app.register_subagent_work(
            parent_session_id=PARENT,
            child_session_id=WORKER_CHILD,
            agent=WORKER_ROLE,
            title="task",
        )

        response = await client.post(
            f"/v1/sessions/{WORKER_CHILD}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "failed", "output": "Error: sub-agent blew up"},
            },
        )
        assert response.status_code in (200, 204)
        await _wait_until(lambda: bool(_notices(server)))

    assert runner_app.list_member_obligations(PARENT) == {}
    assert _notices(server)[0]["data"]["item_data"]["message"] == (
        "executor did not run: Error: sub-agent blew up"
    )


@pytest.mark.asyncio
async def test_child_completion_closes_without_a_notice(
    _clean_subagent_registry: None,
) -> None:
    """A completed child closes the obligation silently."""
    server = _MemberServerClient()
    app, _pm, _harness = _build_app(server)

    async with _runner_client(app) as client:
        runner_app.record_member_obligation(PARENT, WORKER_ROLE)
        runner_app.mark_member_obligation_met(PARENT, WORKER_ROLE, WORKER_CHILD)
        runner_app._session_inboxes_ref[PARENT] = asyncio.Queue()
        runner_app.register_subagent_work(
            parent_session_id=PARENT,
            child_session_id=WORKER_CHILD,
            agent=WORKER_ROLE,
            title="task",
        )

        await client.post(
            f"/v1/sessions/{WORKER_CHILD}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "completed", "output": "all done"},
            },
        )
        await _wait_until(lambda: runner_app.list_member_obligations(PARENT) == {})
        await asyncio.sleep(0.05)

    assert _notices(server) == []


# --------------------------------------------------------------------------
# Native lead: the idle edge is the turn end
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_lead_idle_edge_sends_the_follow_up() -> None:
    """A native lead's note rides the terminal input; its idle edge follows up."""
    server = _MemberServerClient()
    app, _pm, harness = _build_app(server, harness="codex-native")

    async with _runner_client(app) as client:
        await _seed_session(client, labels=_member_labels())
        await _post_message(client, "@executor build the parser")
        await _wait_until(lambda: bool(harness.posted_bodies))
        # The proxy stream end proves only that the prompt was typed — no
        # follow-up may fire there for a native lead.
        await asyncio.sleep(0.05)
        assert _events_of_type(server, "message") == []

        assert (
            await client.post(
                f"/v1/sessions/{PARENT}/events",
                json={"type": "external_session_status", "data": {"status": "idle"}},
            )
        ).status_code in (200, 204)
        await _wait_until(lambda: bool(_events_of_type(server, "message")))

    texts = _ordered_user_texts(harness.posted_bodies[0])
    assert texts[0].startswith("[System:")
    assert "'executor'" in texts[0]
    follow_ups = _events_of_type(server, "message")
    assert len(follow_ups) == 1
    assert "executor" in follow_ups[0]["data"]["content"][0]["text"]
