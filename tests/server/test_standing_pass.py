"""Standing passes at persistence, init, tool and authenticated HTTP boundaries."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.polling import poll_until_async
from tests.server.helpers import create_test_agent


@pytest.fixture
def passes(db_uri):
    from omnigent.server.standing_pass import StandingPassService

    store = SqlAlchemyConversationStore(db_uri)
    prefs = SqlAlchemyUserPreferencesStore(db_uri)
    return StandingPassService(store, prefs_store=prefs)


def test_defaults_empty_snapshot_malformed_and_fork(passes, caplog):
    from omnigent.server.standing_pass import STANDING_PASS_LABEL_KEY

    root = passes.store.create_conversation()
    assert passes.line(root) == ""
    empty = passes.store.get_conversation(root.id).labels[STANDING_PASS_LABEL_KEY]
    passes.prefs_store.patch_namespace(
        RESERVED_USER_LOCAL,
        "session_collab",
        {"standingPassDefault": ["install", "merge", "release"]},
    )
    assert passes.line(root) == ""
    assert passes.store.get_conversation(root.id).labels[STANDING_PASS_LABEL_KEY] == empty
    fresh = passes.store.create_conversation()
    assert passes.resolve(fresh)["kinds"] == ["install", "merge", "release"]
    passes.change(fresh, "revoke", ["install"], "user-item")
    assert "install" not in passes.resolve(fresh)["kinds"]
    assert f"{STANDING_PASS_LABEL_KEY}.0" in passes.store.get_conversation(fresh.id).labels
    fork = passes.store.fork_conversation(fresh.id)
    assert not any(key.startswith(STANDING_PASS_LABEL_KEY) for key in fork.labels)
    assert passes.resolve(fork)["kinds"] == ["install", "merge", "release"]
    passes.store.set_labels(root.id, {STANDING_PASS_LABEL_KEY: "not json"})
    assert passes.line(root) == ""
    assert "Invalid standing pass" in caplog.text


def test_project_removal_exceptions_and_no_later_default_changes(passes, db_uri):
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

    passes.prefs_store.patch_namespace(
        RESERVED_USER_LOCAL, "session_collab", {"standingPassDefault": ["install", "merge"]}
    )
    passes.project_store = SqlAlchemyProjectStore(db_uri)
    project = passes.project_store.create(
        "a" * 32,
        "project",
        None,
        {
            "orchestration": {
                "standing_pass": {
                    "remove": ["install"],
                    "exceptions": ["fn server release", 'custom\nline "push"'],
                }
            }
        },
    )
    root = passes.store.create_conversation(project_id=project.id)
    assert passes.resolve(root)["kinds"] == ["merge"]
    line = passes.line(root)
    assert '"fn server release"' in line
    assert '"custom line \\"push\\""' in line
    assert "\n" not in line
    passes.prefs_store.patch_namespace(
        RESERVED_USER_LOCAL, "session_collab", {"standingPassDefault": ["release"]}
    )
    assert passes.resolve(root)["kinds"] == ["merge"]


def test_cas_retries_preserve_concurrent_revokes(passes, monkeypatch):
    from omnigent.server.standing_pass import StandingPassConflict

    root = passes.store.create_conversation()
    passes.resolve(root)
    passes.change(root, "grant", ["install", "merge"], "approval:test")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda kind: passes.change(root, "revoke", [kind], kind), ["install", "merge"]
            )
        )
    assert len(outcomes) == 2
    record = passes.resolve(root)
    assert record["kinds"] == []
    assert [event["op"] for event in record["events"]].count("revoke") == 2
    assert "Revoked: install, merge" in passes.line(root)
    attempts = []

    def conflict(*args):
        attempts.append(args)
        return False

    monkeypatch.setattr(passes.store, "mutate_label_group", conflict)
    with pytest.raises(StandingPassConflict, match="all 5"):
        passes.change(root, "revoke", ["release"], "retry")
    assert len(attempts) == 5


async def test_empty_instructions_still_inject_mother_and_child(passes, monkeypatch):
    from omnigent.server.instruction_layers import session_instructions_text
    from omnigent.server.standing_pass import STANDING_PASS_LABEL_KEY

    passes.prefs_store.patch_namespace(
        RESERVED_USER_LOCAL,
        "session_collab",
        {"standingPassDefault": ["install", "merge", "release"]},
    )
    root = passes.store.create_conversation()
    child = passes.store.create_conversation(parent_conversation_id=root.id, kind="sub_agent")
    monkeypatch.setattr(
        "omnigent.server.instruction_layers.get_global_instructions_store", lambda: None
    )
    mother_text = await session_instructions_text(root, standing_pass_resolver=passes.line)
    child_text = await session_instructions_text(child, standing_pass_resolver=passes.line)
    assert mother_text == child_text
    assert mother_text.startswith("Standing pass (this mother session, since ")
    assert '"install", "merge", "release"' in mother_text
    assert STANDING_PASS_LABEL_KEY not in passes.store.get_conversation(child.id).labels


async def test_change_notice_only_running_descendants(passes):
    root = passes.store.create_conversation()
    children = [passes.store.create_conversation(parent_conversation_id=root.id) for _ in range(3)]
    for child, state in zip(children, ["running", "idle", "running"], strict=True):
        passes.store.set_session_live_status(child.id, state)
    passes.store.update_conversation(children[2].id, archived=True)
    record = passes.change(root, "revoke", ["install"], "user-message")
    post = AsyncMock()
    outcomes = await passes.steer(root.id, record, post)
    assert outcomes == {
        children[0].id: "steered",
        children[1].id: "skipped_not_running",
        children[2].id: "skipped_not_running",
    }
    post.assert_awaited_once()
    assert "[System: standing pass changed: revoke" in post.await_args.args[1]


async def test_http_child_guard_reserved_labels_and_tool_transport(app, client, db_uri):
    from omnigent.runner.tool_dispatch import _NATIVE_RELAY_BUILTIN_TOOLS, execute_tool
    from omnigent.spec import AgentSpec
    from omnigent.tools.manager import ToolManager

    agent = await create_test_agent(client)
    root_id = agent["_session_id"]
    created = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "parent_session_id": root_id}
    )
    assert created.status_code == 201, created.text
    child_id = created.json()["id"]
    for op in ["revoke", "grant"]:
        response = await client.post(
            f"/v1/sessions/{child_id}/standing-pass", json={"op": op, "kinds": ["install"]}
        )
        assert response.status_code == 403
    for method, path, payload in [
        ("patch", f"/v1/sessions/{root_id}", {"labels": {"omnigent.standing_pass": "{}"}}),
        (
            "post",
            "/v1/sessions",
            {"agent_id": agent["id"], "labels": {"omnigent.standing_pass": "{}"}},
        ),
    ]:
        response = await getattr(client, method)(path, json=payload)
        assert response.status_code == 400, response.text
    manager = ToolManager(AgentSpec(spec_version=1, spawn=False))
    try:
        advertised = {schema["function"]["name"] for schema in manager.get_tool_schemas()}
        assert "sys_standing_pass" in advertised & _NATIVE_RELAY_BUILTIN_TOOLS
    finally:
        manager.shutdown()
    result = json.loads(
        await execute_tool(
            tool_name="sys_standing_pass",
            arguments='{"op":"read"}',
            conversation_id=child_id,
            server_client=client,
        )
    )
    assert result["root_id"] == root_id
    assert result["record"]["kinds"] == []


@pytest.mark.parametrize("action,expected", [("accept", ["install"]), ("decline", [])])
async def test_grant_accept_deny_binds_source_to_real_card(client, action, expected):
    from omnigent.server._elicitation_registry import _harness_elicitation_registry

    agent = await create_test_agent(client)
    root_id = agent["_session_id"]
    task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{root_id}/standing-pass",
            json={"op": "grant", "kinds": ["install"], "source": "forged-user-source"},
        )
    )
    await poll_until_async(lambda: bool(_harness_elicitation_registry), timeout_s=5)
    card_id = next(iter(_harness_elicitation_registry))
    response = await client.post(
        f"/v1/sessions/{root_id}/events",
        json={"type": "approval", "data": {"elicitation_id": card_id, "action": action}},
    )
    assert response.status_code == 202, response.text
    result = await asyncio.wait_for(task, 5)
    assert result.status_code == (200 if action == "accept" else 403), result.text
    record = (await client.get(f"/v1/sessions/{root_id}/standing-pass")).json()["record"]
    assert record["kinds"] == expected
    if action == "accept":
        assert record["events"][-1]["source"] == f"approval:{card_id}"


async def test_first_child_message_gets_live_pass(app, client, db_uri, monkeypatch):
    from omnigent.server.routes.sessions import routes_events

    received = []

    def receive(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"queued": True})

    runner = httpx.AsyncClient(base_url="http://runner", transport=httpx.MockTransport(receive))
    monkeypatch.setattr(routes_events, "_get_runner_client", AsyncMock(return_value=runner))
    monkeypatch.setattr(routes_events, "_ensure_runner_relay_ready", AsyncMock())
    agent = await create_test_agent(client)
    root_id = agent["_session_id"]
    service = app.state.standing_pass_service
    root = service.store.get_conversation(root_id)
    service.change(root, "grant", ["install"], "approval:fixture")
    created = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "parent_session_id": root_id}
    )
    child_id = created.json()["id"]
    response = await client.post(
        f"/v1/sessions/{child_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "Do the task"}]},
        },
    )
    await runner.aclose()
    assert response.status_code == 202, response.text
    page = service.store.list_items(child_id, type="message")
    assert page.data
    content = page.data[0].data.content
    assert content[0]["text"].startswith("Standing pass (this mother session")
    assert content[1]["text"] == "Do the task"
    assert received


def test_successor_carries_predecessor_after_child_move(passes, monkeypatch):
    from omnigent.errors import OmnigentError
    from omnigent.stores.conversation_store import SUCCEEDED_BY_LABEL_KEY, SUCCEEDS_LABEL_KEY

    old = passes.store.create_conversation()
    passes.change(old, "grant", ["install", "merge"], "approval:test")
    passes.change(old, "revoke", ["install"], "user-item")
    successor = passes.store.create_conversation()
    passes.store.set_labels(successor.id, {SUCCEEDS_LABEL_KEY: old.id})
    passes.store.set_labels(old.id, {SUCCEEDED_BY_LABEL_KEY: successor.id})
    monkeypatch.setattr(
        passes.store,
        "get_succession",
        lambda old_id, new_id: SimpleNamespace(old_id=old_id, new_id=new_id),
    )
    moved_child = passes.store.create_conversation(parent_conversation_id=successor.id)
    assert passes.resolve(moved_child)["kinds"] == ["merge"]
    events = passes.resolve(successor)["events"]
    assert events[-1]["op"] == "carry"
    assert events[-1]["source"] == old.id
    assert events[-2]["op"] == "revoke"
    with pytest.raises(OmnigentError, match="read-only"):
        passes.change(old, "grant", ["install"], "approval:late")


async def test_policy_requires_exact_grant_and_skips_reads_and_revokes(passes):
    from omnigent.policies.types import EvaluationContext
    from omnigent.runtime.policies.builder import any_policies_apply, build_policy_engine
    from omnigent.spec import AgentSpec
    from omnigent.spec.types import Phase, PolicyAction

    root = passes.store.create_conversation()
    spec = AgentSpec(spec_version=1)
    assert any_policies_apply(
        spec=spec,
        conversation_id=root.id,
        default_policies=None,
        policy_store=None,
        phase=Phase.TOOL_CALL,
        tool_name="sys_standing_pass",
    )
    engine = build_policy_engine(
        spec=spec, conversation_id=root.id, conversation_store=passes.store
    )
    for op in ["read", "revoke", "grant"]:
        result = await engine.evaluate(
            EvaluationContext(
                phase=Phase.TOOL_CALL,
                tool_name="sys_standing_pass",
                content={
                    "name": "sys_standing_pass",
                    "arguments": {"op": op, "kinds": ["custom-kind"]},
                },
            )
        )
        assert result.action == (PolicyAction.ASK if op == "grant" else PolicyAction.ALLOW)
        if op == "grant":
            assert "custom-kind" in result.reason


async def test_policy_approval_crosses_runner_hop_without_second_card(app, client, db_uri):
    from omnigent.runner.tool_dispatch import execute_tool
    from omnigent.server._elicitation_registry import _harness_elicitation_registry
    from omnigent.server.routes._sessions.orchestration import (
        _evaluate_independent_tool_call_policy,
    )
    from omnigent.server.schemas import SessionEventInput
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    agent = await create_test_agent(client)
    service = app.state.standing_pass_service
    conv = service.store.get_conversation(agent["_session_id"])
    args = {"op": "grant", "kinds": ["install"]}
    task = asyncio.create_task(
        _evaluate_independent_tool_call_policy(
            conv.id,
            conv,
            SessionEventInput(
                type="function_call",
                data={"name": "sys_standing_pass", "arguments": json.dumps(args)},
            ),
            service.store,
            SqlAlchemyAgentStore(db_uri),
            None,
            blocking=True,
        )
    )
    await poll_until_async(lambda: bool(_harness_elicitation_registry), timeout_s=5)
    card_id = next(iter(_harness_elicitation_registry))
    response = await client.post(
        f"/v1/sessions/{conv.id}/events",
        json={"type": "approval", "data": {"elicitation_id": card_id, "action": "accept"}},
    )
    assert response.status_code == 202
    assert await asyncio.wait_for(task, 5) is None
    result = await asyncio.wait_for(
        execute_tool(
            tool_name="sys_standing_pass",
            arguments=json.dumps(args),
            conversation_id=conv.id,
            server_client=client,
        ),
        2,
    )
    record = json.loads(result)["record"]
    assert record["kinds"] == ["install"]
    assert record["events"][-1]["source"] == f"approval:{card_id}"
    assert not _harness_elicitation_registry


async def test_initial_items_seed_gets_pass_and_later_messages_do_not(app, client):
    agent = await create_test_agent(client)
    service = app.state.standing_pass_service
    root = service.store.get_conversation(agent["_session_id"])
    service.change(root, "grant", ["install"], "approval:fixture")
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": root.id,
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Initial task"}],
                    },
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    child = service.store.get_conversation(response.json()["id"])
    content = service.store.list_items(child.id, type="message").data[0].data.content
    assert content[0]["text"].startswith("Standing pass (this mother session")
    assert content[1]["text"] == "Initial task"
    from omnigent.server.schemas import SessionEventInput

    later = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "Next task"}]},
    )
    assert service.first_message(child, later) == later


def test_succession_started_before_cas_blocks_late_mutation(passes, monkeypatch):
    from omnigent.errors import OmnigentError
    from omnigent.stores.conversation_store import SUCCEEDED_BY_LABEL_KEY

    root = passes.store.create_conversation()
    passes.resolve(root)
    original = passes.store.mutate_label_group

    def after_succession(*args):
        passes.store.set_labels(root.id, {SUCCEEDED_BY_LABEL_KEY: "a" * 32})
        return original(*args)

    monkeypatch.setattr(passes.store, "mutate_label_group", after_succession)
    with pytest.raises(OmnigentError, match="read-only"):
        passes.change(root, "grant", ["install"], "approval:late")
    assert passes.resolve(root)["kinds"] == []


@pytest.mark.parametrize("harness", ["pi", "pi-native", "codex-native", "claude-native"])
async def test_prepared_successor_never_resolves_defaults_before_receipt(
    app, client, db_uri, harness, monkeypatch
):
    from omnigent.server.routes.sessions import routes_succession
    from omnigent.stores.conversation_store import ROTATE_REQUESTED_LABEL_KEY
    from tests.server.test_pi_successor import _source

    service = app.state.standing_pass_service
    service.prefs_store = SqlAlchemyUserPreferencesStore(db_uri)
    service.prefs_store.patch_namespace(
        RESERVED_USER_LOCAL, "session_collab", {"standingPassDefault": ["install"]}
    )
    source_id = (
        _source(service.store, harness=harness)
        if harness in {"pi", "pi-native"}
        else service.store.create_conversation(
            agent_id="a" * 32, runner_id="runner-test", harness_override=harness
        ).id
    )
    source = service.store.get_conversation(source_id)
    service.store.set_labels(source.id, {ROTATE_REQUESTED_LABEL_KEY: "0"})
    service.resolve(source)
    service.change(source, "revoke", ["install"], "user-item")
    created = await client.post(f"/v1/sessions/{source.id}/successor")
    assert created.status_code == 200, created.text
    successor = service.store.get_conversation(created.json()["id"])
    assert service.store.get_succession(source.id, successor.id) is None
    record = service.resolve(successor)
    assert record["kinds"] == []
    assert record["events"][-1]["op"] == "revoke"
    assert "omnigent.standing_pass" not in successor.labels
    # The source can still revoke or grant while terminal transfer is pending.
    service.change(source, "grant", ["merge"], "approval:during-transfer")

    async def park(receipt_id, **_kwargs):
        return service.store.get_succession_by_id(receipt_id)

    monkeypatch.setattr(routes_succession, "advance_succession", park)
    # Requested handover defaults allow_empty to False. Prepared successors
    # must nevertheless get their receipt even without any live children.
    finalised = await client.post(
        f"/v1/sessions/{source.id}/succession", json={"target_session_id": successor.id}
    )
    assert finalised.status_code == 200, finalised.text
    assert finalised.json()["status"] != "noop"
    final = service.resolve(successor)
    assert final["kinds"] == ["merge"]
    assert final["events"][-1]["op"] == "carry"
    assert final["events"][-1]["source"] == source.id


async def test_failed_clear_candidate_stays_archived_and_new_attempt_is_stable(app, client):
    service = app.state.standing_pass_service
    source = service.store.create_conversation(
        agent_id="a" * 32, runner_id="runner-test", harness_override="claude-native"
    )
    first = await client.post(f"/v1/sessions/{source.id}/successor")
    assert first.status_code == 200, first.text
    failed_id = first.json()["id"]
    service.store.update_conversation(failed_id, archived=True)
    service.store.set_runner_id(failed_id, None)
    second = await client.post(f"/v1/sessions/{source.id}/successor")
    assert second.status_code == 200, second.text
    new_id = second.json()["id"]
    assert new_id != failed_id
    assert service.store.get_conversation(failed_id).archived is True
    assert service.store.get_conversation(new_id).archived is False
    retry = await client.post(f"/v1/sessions/{source.id}/successor")
    assert retry.json()["id"] == new_id


async def test_mcp_accept_replay_cannot_restore_revoked_grant(app, client, db_uri):
    from omnigent.errors import OmnigentError
    from omnigent.server.routes._sessions.common import (
        _pending_policy_ask_writes,
        _PendingPolicyAskWrites,
    )
    from omnigent.server.routes._sessions.helpers import _apply_pending_policy_ask_writes
    from omnigent.server.standing_pass import consume_grant_approval
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    agent = await create_test_agent(client)
    service = app.state.standing_pass_service
    root = service.store.get_conversation(agent["_session_id"])
    eid = "elicit_standing_pass_replay"
    args = {"op": "grant", "kinds": ["install"]}
    _pending_policy_ask_writes[eid] = _PendingPolicyAskWrites(
        state_updates=None,
        set_labels=None,
        from_mcp=True,
        reviewed_arguments=args,
        mcp_call_identity=(root.id, "sys_standing_pass", None, None),
    )

    async def accept():
        await _apply_pending_policy_ask_writes(
            root.id,
            root,
            service.store,
            SqlAlchemyAgentStore(db_uri),
            {"elicitation_id": eid, "action": "accept"},
        )

    try:
        await accept()
        granted = await asyncio.wait_for(
            client.post(f"/v1/sessions/{root.id}/standing-pass", json=args), 2
        )
        assert granted.status_code == 200, granted.text
        service.change(root, "revoke", ["install"], "user-revoke")
        await accept()
        assert consume_grant_approval(root.id, ["install"]) is None
        with pytest.raises(OmnigentError, match="already been used"):
            service.change(root, "grant", ["install"], f"approval:{eid}")
        assert service.resolve(root)["kinds"] == []
    finally:
        _pending_policy_ask_writes.pop(eid, None)
