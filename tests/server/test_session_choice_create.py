"""Selection at the real create boundary and the durable startup hold."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_load_queue import HostLoadQueue, start_allowed
from omnigent.server.session_choice import CHOICE_LABEL, SLOT_LABEL, ChoiceState, holds_slot
from omnigent.server.session_choice_runtime import choice_record
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

HOST = "1" * 32
HEADERS = {"X-Omnigent-Create-Origin": "agent", "X-Omnigent-Session-Choice": "1"}


@pytest.fixture
async def choice_env(client, app, db_uri, monkeypatch):
    agents = [await create_test_agent(client, name=f"choice-{index}") for index in range(2)]
    store = app.state.conversation_store
    parent = store.create_conversation(
        agent_id=agents[0]["id"], host_id=HOST, workspace="/opt/work/project"
    )
    prefs = SqlAlchemyUserPreferencesStore(db_uri)
    app.state.user_preferences_store = prefs
    app.state.host_store = HostStore(db_uri)
    app.state.host_store.upsert_on_connect(HOST, "sample-host", RESERVED_USER_LOCAL)
    app.state.agent_quota_service.preferences = prefs
    from omnigent.server.routes import sessions

    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=None))
    links = [
        {"agent": agent["id"], "model": "test-model", "meter": f"pool-{index}"}
        for index, agent in enumerate(agents)
    ]
    settings = {
        "enabled": True,
        "workTypes": [{"id": "work", "label": "Work"}],
        "presets": [
            {"id": f"p{index}", "name": f"Choice {index}", "links": [link]}
            for index, link in enumerate(links)
        ],
        "hosts": {
            HOST: {
                "work": [
                    [{"preset": "p0", "links": [links[0]], "weight": 1}],
                    [{"preset": "p1", "links": [links[1]], "weight": 1}],
                ]
            }
        },
        "limits": {"pool-0": {"maxRunning": 1}},
    }
    prefs.replace_session_choice(
        RESERVED_USER_LOCAL, settings, prefs.session_choice(RESERVED_USER_LOCAL)["revision"]
    )
    return SimpleNamespace(
        app=app,
        client=client,
        store=store,
        prefs=prefs,
        parent=parent,
        agents=agents,
        settings=settings,
    )


async def create(env, **changes):
    return await env.client.post(
        "/v1/sessions",
        headers=HEADERS,
        json={"parent_session_id": env.parent.id, "work_type": "work", **changes},
    )


@pytest.mark.asyncio
async def test_creates_select_before_project_defaults_and_admit_in_parallel(choice_env):
    env = choice_env
    one, two = await asyncio.gather(create(env), create(env))
    assert one.status_code == two.status_code == 201, (one.text, two.text)
    choices = [one.json(), two.json()]
    assert {item["session_choice"]["pool"] for item in choices} == {"pool-0", "pool-1"}
    first = next(item for item in choices if item["session_choice"]["pool"] == "pool-0")
    conv = env.store.get_conversation(first["id"])
    assert holds_slot(conv, time.time())
    assert choice_record(conv)["pool"] == "pool-0"
    assert all(len(value) <= 256 for value in conv.labels.values())
    snapshot = await env.client.get(f"/v1/sessions/{conv.id}")
    assert snapshot.json()["session_choice"]["pool"] == "pool-0"
    env.store.set_session_live_status(conv.id, "idle")
    assert SLOT_LABEL not in env.store.get_conversation(conv.id).labels


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["named_no_reason", "choice_reason", "user_named", "legacy"])
async def test_named_precedence_and_old_runner(choice_env, mode):
    env = choice_env
    body = {"agent_id": env.agents[1]["id"]}
    if mode == "choice_reason":
        body["choice_reason"] = "The task needs this agent"
    elif mode == "user_named":
        body["user_named"] = True
    if mode == "legacy":
        response = await env.client.post(
            "/v1/sessions", json={"parent_session_id": env.parent.id, **body}
        )
    else:
        response = await create(env, **body)
    assert response.status_code == 201, response.text
    result = response.json()
    expected = env.agents[0 if mode == "named_no_reason" else 1]["id"]
    assert result["agent_id"] == expected
    assert bool(result.get("session_choice")) == (mode != "legacy")


@pytest.mark.asyncio
async def test_named_reason_checks_cap_but_user_can_override(choice_env):
    env = choice_env
    first = await create(env)
    assert first.status_code == 201, first.text
    named = {
        "agent_id": env.agents[0]["id"],
        "model_override": "test-model",
        "choice_reason": "specific need",
    }
    refused = await create(env, **named)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "agent_busy"
    allowed = await create(env, **{**named, "user_named": True})
    assert allowed.status_code == 201, allowed.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["quota", "running", "choose"])
async def test_failed_reads_and_selector_keep_projectless_create_usable(
    choice_env, monkeypatch, failure
):
    env = choice_env

    def fail(*args, **kwargs):
        raise RuntimeError("unavailable test input")

    if failure == "quota":
        monkeypatch.setattr(env.app.state.agent_quota_service, "pool_readings", fail)
    elif failure == "running":
        monkeypatch.setattr("omnigent.server.session_choice_runtime.running_pools", fail)
    else:
        monkeypatch.setattr(ChoiceState, "select", fail)
    response = await create(env)
    assert response.status_code == 201, response.text
    assert response.json()["agent_id"] == env.agents[0]["id"]
    if failure == "choose":
        assert "choice_failed" in response.json()["session_choice"]["flags"]


@pytest.mark.asyncio
async def test_all_full_holds_then_releases_only_bound_pool(choice_env, monkeypatch):
    env = choice_env
    env.settings["hosts"][HOST]["work"] = env.settings["hosts"][HOST]["work"][:1]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    first = (await create(env)).json()
    held_response = await create(env)
    assert held_response.status_code == 201, held_response.text
    held = held_response.json()
    assert held["startup"]["reason"] == "pool_full"
    row = env.store.get_conversation(held["id"])
    assert not start_allowed(row)
    assert not holds_slot(row, time.time())
    queue = HostLoadQueue(env.store)
    monkeypatch.setattr(queue, "_start", AsyncMock())
    monkeypatch.setattr(env.app.state.host_load_inputs, "deliver_session", AsyncMock())
    assert not await queue.release(env.app, row.id, HOST)
    env.store.set_session_live_status(first["id"], "idle")
    assert await queue.release(env.app, row.id, HOST)
    queue._start.assert_awaited_once()
    assert start_allowed(env.store.get_conversation(row.id))
    assert holds_slot(env.store.get_conversation(row.id), time.time())


@pytest.mark.asyncio
async def test_parallel_release_keeps_one_admitted_slot(choice_env, monkeypatch):
    env = choice_env
    first = (await create(env)).json()
    row = env.store.get_conversation(first["id"])
    env.store.set_session_live_status(row.id, "idle")
    labels = dict(row.labels)
    labels[SLOT_LABEL] = f"queued@{HOST}@pool-0"
    queued = [
        env.store.create_conversation(
            agent_id=env.agents[0]["id"],
            parent_conversation_id=env.parent.id,
            kind="sub_agent",
            host_id=host,
            workspace="/opt/work/project",
            labels={**labels, SLOT_LABEL: f"queued@{host}@pool-0"},
        )
        for host in (HOST, "2" * 32)
    ]
    queue = HostLoadQueue(env.store)
    monkeypatch.setattr(queue, "_start", AsyncMock())
    monkeypatch.setattr(env.app.state.host_load_inputs, "deliver_session", AsyncMock())
    results = await asyncio.gather(
        *(queue.release(env.app, conv.id, conv.host_id) for conv in queued)
    )
    assert sum(results) == 1
    assert (
        sum(holds_slot(env.store.get_conversation(conv.id), time.time()) for conv in queued) == 1
    )
    another = await create(env)
    assert another.json()["session_choice"]["pool"] == "pool-1"


@pytest.mark.asyncio
async def test_refusal_and_reserved_labels_leave_no_child(choice_env):
    env = choice_env
    env.settings["hosts"][HOST]["work"] = []
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    response = await create(env)
    assert (
        response.status_code == 409 and response.json()["detail"]["code"] == "session_choice_ask"
    )
    response = await create(
        env,
        agent_id=env.agents[0]["id"],
        user_named=True,
        labels={CHOICE_LABEL: json.dumps({"pool": "forged"})},
    )
    assert response.status_code == 400, response.text
