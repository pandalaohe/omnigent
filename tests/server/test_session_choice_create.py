"""Selection at the real create boundary and the durable startup hold."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_load_queue import HostLoadQueue, start_allowed
from omnigent.server.routes.session_choice import create_session_choice_router
from omnigent.server.session_choice import CHOICE_LABEL, SLOT_LABEL, ChoiceState, holds_slot
from omnigent.server.session_choice_runtime import choice_record
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

HOST = "1" * 32
HEADERS = {"X-Omnigent-Create-Origin": "agent", "X-Omnigent-Session-Choice": "1"}
CHOICE_URL = "/v1/me/session-choice"


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


@pytest.fixture
async def choice_settings_client(choice_env):
    app = FastAPI()
    app.state = choice_env.app.state
    app.include_router(create_session_choice_router(choice_env.prefs, None), prefix="/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def save_choice(client, settings, revision):
    return await client.put(CHOICE_URL, json={"settings": settings, "expected_revision": revision})


@pytest.fixture
def drain_choice_env(choice_env):
    env = choice_env
    head, alternate = (preset["links"][0] for preset in env.settings["presets"])
    env.settings["hosts"][HOST]["work"] = [
        [{"links": [head, {**alternate, "fallbackMode": "drain"}]}]
    ]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    return env


@pytest.mark.asyncio
async def test_admitted_drain_survives_state_restart_and_preview_is_read_only(
    drain_choice_env, choice_settings_client
):
    from omnigent.server.session_choice import FloorOff, state_for

    env = drain_choice_env
    now = time.time()
    state_for(env.app).exhausted[(RESERVED_USER_LOCAL, "pool-0")] = now + 60
    before = env.prefs.session_choice_snapshot(RESERVED_USER_LOCAL)
    child = await create(env)
    assert child.status_code == 201, child.text
    assert child.json()["session_choice"]["pool"] == "pool-1"
    assert "Started draining test-model" in child.json()["session_choice"]["reason"]
    env.app.state.session_choice_states.clear()
    env.prefs.set_choice_floor_off(RESERVED_USER_LOCAL, "pool-1", FloorOff("manual", {}))

    client = choice_settings_client
    settings = await client.get(CHOICE_URL)
    assert settings.status_code == 200, settings.text
    envelope = settings.json()
    runtime = envelope["runtime"]
    assert runtime["settingsRevision"] == envelope["revision"] == before.revision
    [lock] = runtime["drainLocks"]
    assert set(lock) == {
        "hostId",
        "workType",
        "tierIndex",
        "rowPosition",
        "savedLink",
        "rowIdentity",
        "settingsRevision",
        "since",
    }
    assert lock["savedLink"]["fallbackMode"] == "drain"
    body = {"host_id": HOST, "work_type": "work"}
    preview = await client.post(f"{CHOICE_URL}/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["pool"] == "pool-1"
    one_off = await client.post(f"{CHOICE_URL}/preview", json={**body, "exclude": ["pool-1"]})
    assert one_off.status_code == 200, one_off.text
    assert one_off.json()["pool"] == "pool-0"
    assert (await client.get(CHOICE_URL)).json()["runtime"] == runtime
    assert state_for(env.app).cooldowns == {}
    line = env.app.state.agent_quota_service.session_choice_line(env.parent)
    assert "Draining: test-model (work) until it closes." in line
    assert "Use up: pool-1." in line
    next_child = await create(env)
    assert next_child.status_code == 201, next_child.text
    assert next_child.json()["session_choice"]["pool"] == "pool-1"
    assert "Continuing to drain test-model" in next_child.json()["session_choice"]["reason"]


@pytest.mark.asyncio
async def test_drain_writes_only_after_admission_and_db_failure_preserves_create(
    drain_choice_env, choice_settings_client, monkeypatch
):
    from omnigent.server.session_choice import state_for
    from omnigent.stores.conversation_store import NameAlreadyExistsError

    env = drain_choice_env
    state = state_for(env.app)
    now = time.time()
    state.exhausted[(RESERVED_USER_LOCAL, "pool-0")] = now + 60
    state.exhausted[(RESERVED_USER_LOCAL, "pool-1")] = now + 60
    held = await create(env)
    assert held.status_code == 201, held.text
    assert held.json()["session_choice"]["outcome"] == "hold"
    assert env.prefs.session_choice_snapshot(RESERVED_USER_LOCAL).locks == {}
    del state.exhausted[(RESERVED_USER_LOCAL, "pool-1")]
    named = await create(
        env,
        agent_id=env.agents[1]["id"],
        model_override="test-model",
        user_named=True,
    )
    assert named.status_code == 201, named.text
    assert env.prefs.session_choice_snapshot(RESERVED_USER_LOCAL).locks == {}

    def conflict(*args, **kwargs):
        raise NameAlreadyExistsError("session title already exists")

    with monkeypatch.context() as patch:
        patch.setattr(env.store, "create_conversation", conflict)
        failed = await create(env)
    assert failed.status_code == 409, failed.text
    assert env.prefs.session_choice_snapshot(RESERVED_USER_LOCAL).locks == {}

    def write_failure(*args, **kwargs):
        raise OSError("test lock write failed")

    preview = await choice_settings_client.post(
        f"{CHOICE_URL}/preview", json={"host_id": HOST, "work_type": "work"}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["pool"] == "pool-1"
    with monkeypatch.context() as patch:
        patch.setattr(env.prefs, "apply_drain_lock_delta", write_failure)
        created = await create(env)
    assert created.status_code == 201, created.text
    assert env.store.get_conversation(created.json()["id"]) is not None
    assert env.prefs.session_choice_snapshot(RESERVED_USER_LOCAL).locks == {}
    assert preview.json()["reason"] == "First eligible tier, weighted choice"
    assert created.json()["session_choice"]["reason"] == preview.json()["reason"]


@pytest.mark.asyncio
async def test_stale_create_cannot_restore_drain_after_settings_saved(
    drain_choice_env, monkeypatch
):
    from omnigent.server.session_choice import state_for

    env = drain_choice_env
    owner = RESERVED_USER_LOCAL
    state_for(env.app).exhausted[(owner, "pool-0")] = time.time() + 60
    original = env.prefs.session_choice_snapshot
    before = original(owner)
    changed = json.loads(json.dumps(before.document))
    changed["hosts"][HOST]["work"][0][0]["links"][1].pop("fallbackMode")
    saved = []
    writes = []
    apply_delta = env.prefs.apply_drain_lock_delta

    def read_then_save(user):
        snapshot = original(user)
        if not saved:
            saved.append(env.prefs.replace_session_choice(user, changed, snapshot.revision))
            saved.append(
                env.prefs.replace_session_choice(user, snapshot.document, saved[0]["revision"])
            )
        return snapshot

    def observe_delta(user, snapshot, **delta):
        result = apply_delta(user, snapshot, **delta)
        writes.append((snapshot.generation, delta["admitted"], result))
        return result

    monkeypatch.setattr(env.prefs, "session_choice_snapshot", read_then_save)
    monkeypatch.setattr(env.prefs, "apply_drain_lock_delta", observe_delta)
    child = await create(env)
    assert child.status_code == 201, child.text
    assert child.json()["session_choice"]["pool"] == "pool-1"
    assert len(saved) == 2
    after = original(owner)
    assert after.revision == before.revision
    assert after.locks == {}
    [(generation, proposal, applied)] = writes
    assert generation == before.generation < after.generation
    assert proposal.pool == "pool-1" and applied is False
    assert child.json()["session_choice"]["reason"] == "First eligible tier, weighted choice"


@pytest.mark.asyncio
async def test_settings_put_waits_for_the_admitted_create(
    drain_choice_env, choice_settings_client, monkeypatch
):
    from omnigent.server import session_choice_runtime as runtime

    env = drain_choice_env
    owner = RESERVED_USER_LOCAL
    state = runtime.state_for(env.app)
    state.exhausted[(owner, "pool-0")] = time.time() + 60
    before = (await choice_settings_client.get(CHOICE_URL)).json()
    changed = deepcopy(before["settings"])
    changed["hosts"][HOST]["work"][0][0]["links"][1]["fallbackMode"] = "return"
    choosing = asyncio.Event()
    resume_create = asyncio.Event()
    put_waiting = asyncio.Event()
    authorize = runtime.authorize_agents
    owner_locks = env.app.state.child_cap_locks
    hold = owner_locks.hold
    put_task = None

    async def pause_create(app, request):
        choosing.set()
        await resume_create.wait()
        return await authorize(app, request)

    @asynccontextmanager
    async def observe_hold(key):
        is_put = asyncio.current_task() is put_task
        if is_put and key == f"owner:{owner}":
            put_waiting.set()
        async with hold(key):
            if is_put:
                [created_lock] = env.prefs.session_choice_snapshot(owner).locks.values()
                assert created_lock.pool == "pool-1"
            yield

    monkeypatch.setattr(runtime, "authorize_agents", pause_create)
    monkeypatch.setattr(owner_locks, "hold", observe_hold)
    create_task = asyncio.create_task(create(env))
    try:
        await asyncio.wait_for(choosing.wait(), timeout=5)
        put_task = asyncio.create_task(
            save_choice(choice_settings_client, changed, before["revision"])
        )
        await asyncio.wait_for(put_waiting.wait(), timeout=5)
        assert not put_task.done()
        assert (await choice_settings_client.get(CHOICE_URL)).json() == before
    finally:
        resume_create.set()
        await asyncio.gather(create_task, *([put_task] if put_task is not None else []))

    saved = put_task.result()
    assert saved.status_code == 200, saved.text
    current = (await choice_settings_client.get(CHOICE_URL)).json()
    assert current["revision"] == saved.json()["revision"] != before["revision"]
    assert current["runtime"]["drainLocks"] == []
    state.exhausted.clear()
    following = await create(env)
    assert following.status_code == 201, following.text
    assert following.json()["session_choice"]["pool"] == "pool-0"
    child = create_task.result()
    assert child.status_code == 201, child.text
    assert "Started draining test-model" in child.json()["session_choice"]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", ["restart", "redelivery", "next-pick"])
async def test_failed_drain_close_is_retried_after_billing_recovery(
    drain_choice_env, choice_settings_client, monkeypatch, retry
):
    from omnigent.server import session_choice_runtime as runtime

    env = drain_choice_env
    owner = RESERVED_USER_LOCAL
    now = time.time()
    runtime.state_for(env.app).exhausted[(owner, "pool-0")] = now + 60
    child = await create(env)
    assert child.status_code == 201, child.text
    session = env.store.get_conversation(child.json()["id"])
    refusal = {"from_runner": True, "response_id": "refused", "input_generation": 1}
    await runtime.record_provider_recovery(env.app, session, started=True, **refusal)
    message = "HTTP 403 balance billing is disabled for this key"
    close = env.prefs.close_choice_drain_locks
    failures = 0

    def fail_close(*args):
        nonlocal failures
        if retry == "restart" and failures:
            return close(*args)
        failures += 1
        raise OSError("test closure write failed")

    with monkeypatch.context() as patch:
        patch.setattr(env.prefs, "close_choice_drain_locks", fail_close)
        await runtime.record_quota_exhaustion(env.app, session, message, actor=None, **refusal)
        locks = (await choice_settings_client.get(CHOICE_URL)).json()["runtime"]["drainLocks"]
        assert bool(locks) == (retry != "restart")
        monkeypatch.setattr(runtime.time, "time", lambda: now + 180)
        recovery = {"from_runner": True, "response_id": "recovered", "input_generation": 2}
        await runtime.record_provider_recovery(env.app, session, started=True, **recovery)
        await runtime.record_provider_recovery(env.app, session, **recovery)
        assert not runtime.state_for(env.app).refusals
        if retry != "restart":
            unavailable = await create(env)
            assert unavailable.status_code == 503, unavailable.text

    if retry == "redelivery":
        await runtime.record_quota_exhaustion(env.app, session, message, actor=None, **refusal)
    if retry != "next-pick":
        env.app.state.session_choice_states.clear()
    recovered = await create(env)
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["session_choice"]["pool"] == "pool-0"
    assert (await choice_settings_client.get(CHOICE_URL)).json()["runtime"]["drainLocks"] == []


@pytest.mark.asyncio
async def test_absolute_reset_receipts_start_and_end_a_drain(
    drain_choice_env, choice_settings_client, monkeypatch
):
    from omnigent.server import session_choice_runtime as runtime

    env = drain_choice_env
    owner = RESERVED_USER_LOCAL
    now = time.time()
    head = await create(env)
    assert head.status_code == 201, head.text
    head_session = env.store.get_conversation(head.json()["id"])
    resets = {"head quota reported": now + 300, "fallback quota reported": now + 900}
    # Supply the absolute timestamps at the parser boundary used by runner feedback.
    monkeypatch.setattr(runtime, "quota_reset_at", lambda message, now: resets[message])

    async def record(session, message, receipt):
        await runtime.record_quota_exhaustion(
            env.app, session, message, actor=None, from_runner=True, response_id=receipt
        )

    await record(head_session, "head quota reported", "head-reset")
    state = runtime.state_for(env.app)
    assert state.exhausted[(owner, "pool-0")] == now + 300
    fallback = await create(env)
    assert fallback.status_code == 201, fallback.text
    assert fallback.json()["session_choice"]["pool"] == "pool-1"
    saved = await choice_settings_client.get(CHOICE_URL)
    [lock] = saved.json()["runtime"]["drainLocks"]
    assert lock["savedLink"]["meter"] == "pool-1"
    fallback_session = env.store.get_conversation(fallback.json()["id"])
    monkeypatch.setattr(runtime.time, "time", lambda: now + 400)
    await record(fallback_session, "fallback quota reported", "fallback-reset")
    assert state.exhausted[(owner, "pool-1")] == now + 900
    assert (await choice_settings_client.get(CHOICE_URL)).json()["runtime"]["drainLocks"] == []

    monkeypatch.setattr(runtime.time, "time", lambda: now + 1000)
    await record(fallback_session, "fallback quota reported", "fallback-reset")
    recovered = await create(env)
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["session_choice"]["pool"] == "pool-0"
    assert (await choice_settings_client.get(CHOICE_URL)).json()["runtime"]["drainLocks"] == []
    assert state.exhausted == {}
    assert "Started draining test-model" in fallback.json()["session_choice"]["reason"]


@pytest.mark.asyncio
async def test_quota_receipt_clears_drain_saved_while_waiting_for_owner_lock(
    drain_choice_env, choice_settings_client, monkeypatch
):
    from omnigent.server import session_choice_runtime as runtime

    env = drain_choice_env
    owner = RESERVED_USER_LOCAL
    now = time.time()
    reset_at = now + 300
    monkeypatch.setattr(runtime, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(runtime, "quota_reset_at", lambda message, now: reset_at)
    runtime.state_for(env.app).exhausted[(owner, "pool-0")] = now + 60

    fallback = await create(env)
    assert fallback.status_code == 201, fallback.text
    assert fallback.json()["session_choice"]["pool"] == "pool-1"
    session = env.store.get_conversation(fallback.json()["id"])
    env.prefs.close_choice_drain_locks(owner, lambda lock: lock.pool == "pool-1")

    receipt_waiting = asyncio.Event()
    resume_receipt = asyncio.Event()
    owner_locks = env.app.state.child_cap_locks
    hold = owner_locks.hold

    @asynccontextmanager
    async def pause_receipt(key):
        if key == f"owner:{owner}" and asyncio.current_task() is receipt_task:
            receipt_waiting.set()
            await resume_receipt.wait()
        async with hold(key):
            yield

    monkeypatch.setattr(owner_locks, "hold", pause_receipt)
    receipt_task = asyncio.create_task(
        runtime.record_quota_exhaustion(
            env.app,
            session,
            "fallback quota reported",
            actor=None,
            from_runner=True,
            response_id="fallback-reset",
        )
    )
    try:
        await asyncio.wait_for(receipt_waiting.wait(), timeout=5)
        now += 1
        fallback = await create(env)
        assert fallback.status_code == 201, fallback.text
        [lock] = (await choice_settings_client.get(CHOICE_URL)).json()["runtime"]["drainLocks"]
        assert lock["savedLink"]["meter"] == "pool-1"
        assert lock["since"] == now
    finally:
        resume_receipt.set()
        await asyncio.wait_for(receipt_task, timeout=5)

    locks_after_receipt = (await choice_settings_client.get(CHOICE_URL)).json()["runtime"][
        "drainLocks"
    ]
    now = reset_at + 1
    recovered = await create(env)
    assert locks_after_receipt == [], "quota receipt left a drain lock"
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["session_choice"]["pool"] == "pool-0"


@pytest.mark.asyncio
async def test_exclude_with_no_matching_drain_survives_preference_write_failure(
    choice_env, monkeypatch
):
    env = choice_env

    def fail_preference_write(*args, **kwargs):
        raise OSError("test preference write failed")

    monkeypatch.setattr(env.prefs, "_upsert_row", fail_preference_write)
    excluded = await create(env, exclude=["pool-0"])
    following = await create(env)

    assert excluded.status_code == 201, f"exclude create failed: {excluded.text}"
    assert excluded.json()["session_choice"]["pool"] == "pool-1"
    assert following.status_code == 201, following.text


@pytest.mark.asyncio
async def test_changing_head_mode_keeps_weighted_create_order(choice_env, choice_settings_client):
    env = choice_env
    client = choice_settings_client
    before = (await client.get(CHOICE_URL)).json()
    settings = before["settings"]
    tiers = settings["hosts"][HOST]["work"]
    first_row, second_row = (tier[0] for tier in tiers)
    first_row["weight"] = 2
    settings["hosts"][HOST]["work"] = [[first_row, second_row]]
    settings["limits"] = {}
    saved = await save_choice(client, settings, before["revision"])
    assert saved.status_code == 200, saved.text
    first = await create(env)
    assert first.status_code == 201, first.text
    assert first.json()["session_choice"]["pool"] == "pool-0"

    first_row["links"][0]["fallbackMode"] = "drain"
    changed = await save_choice(client, settings, saved.json()["revision"])
    assert changed.status_code == 200, changed.text
    following = await create(env)
    assert following.status_code == 201, following.text
    assert following.json()["session_choice"]["pool"] == "pool-1"
    assert (await client.get(CHOICE_URL)).json()["runtime"]["drainLocks"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("edit", "expected_pool"),
    [
        ("return", "pool-0"),
        ("move", "pool-1"),
        ("replace-preset", "pool-2"),
        ("flip-back", "pool-0"),
    ],
)
async def test_weighted_winner_and_saved_row_edits_reach_next_create(
    drain_choice_env, choice_settings_client, edit, expected_pool
):
    from omnigent.server.session_choice import state_for

    env = drain_choice_env
    client = choice_settings_client
    before = (await client.get(CHOICE_URL)).json()
    settings = before["settings"]
    tier = settings["hosts"][HOST]["work"][0]
    row = tier[0]
    head, fallback = row["links"]
    other = {**fallback, "meter": "pool-2"}
    row["links"].append(other)
    row.update(preset="p0", weight=2, adjusted=False)
    settings["presets"][0]["links"] = deepcopy(row["links"])
    tier.append({"links": [head, {**fallback, "meter": "pool-3"}], "weight": 1})
    saved = await save_choice(client, settings, before["revision"])
    assert saved.status_code == 200, saved.text
    state = state_for(env.app)
    state.exhausted[(RESERVED_USER_LOCAL, "pool-0")] = time.time() + 60
    chosen = await create(env)
    assert chosen.status_code == 201, chosen.text
    assert chosen.json()["session_choice"]["pool"] == "pool-1"
    current = (await client.get(CHOICE_URL)).json()
    [winner] = current["runtime"]["drainLocks"]
    assert winner["rowPosition"] == 0 and winner["savedLink"]["meter"] == "pool-1"
    assert current["settings"]["hosts"][HOST]["work"][0][0]["adjusted"] is False

    if edit == "move":
        row["links"] = [head, other, fallback]
    elif edit == "replace-preset":
        preset = settings["presets"][0]
        preset["links"] = [other]
        row["links"] = deepcopy(preset["links"])
    else:
        row["links"][1] = {**fallback, "fallbackMode": "return"}
    changed = await save_choice(client, settings, current["revision"])
    assert changed.status_code == 200, changed.text
    current = (await client.get(CHOICE_URL)).json()
    after_edit = current["runtime"]["drainLocks"]
    if edit == "move":
        assert after_edit == [{**winner, "settingsRevision": current["revision"]}]
    else:
        assert after_edit == []
    if edit in {"return", "flip-back"}:
        assert "fallbackMode" not in current["settings"]["hosts"][HOST]["work"][0][0]["links"][1]
    if edit == "flip-back":
        row["links"][1] = fallback
        restored = await save_choice(client, settings, current["revision"])
        assert restored.status_code == 200, restored.text
        assert restored.json()["revision"] == saved.json()["revision"]

    state.exhausted.clear()
    following = await create(env)
    assert following.status_code == 201, following.text
    assert following.json()["session_choice"]["pool"] == expected_pool
    assert (await client.get(CHOICE_URL)).json()["runtime"]["drainLocks"] == after_edit
    assert "Started draining test-model" in chosen.json()["session_choice"]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["billing-restart", "billing-recovery", "reset-mark", "router"])
async def test_feedback_ends_drain_before_another_pick(drain_choice_env, monkeypatch, signal):
    from omnigent.server.session_choice import state_for
    from omnigent.server.session_choice_runtime import (
        observe_external_admission,
        record_provider_recovery,
        record_quota_exhaustion,
    )

    env = drain_choice_env
    owner = RESERVED_USER_LOCAL
    if signal == "router":
        env.settings["externalAdmission"] = [
            {
                "host": HOST,
                "agent": env.agents[1]["id"],
                "admittedByRouter": True,
                "refusalPrefix": "router-refused",
                "exhaustionCode": "all_closed",
                "routeTiles": ["pool-1"],
            }
        ]
        env.prefs.replace_session_choice(
            owner, env.settings, env.prefs.session_choice(owner)["revision"]
        )
    now = time.time()
    state_for(env.app).exhausted[(owner, "pool-0")] = now + 60
    child = await create(env)
    assert child.status_code == 201, child.text
    session = env.store.get_conversation(child.json()["id"])
    assert env.prefs.session_choice_snapshot(owner).locks

    if signal == "router":
        await observe_external_admission(
            env.app,
            session,
            "[router-refused:all_closed clears_at=-]",
            actor=None,
            from_runner=True,
            response_id="rejected",
            status="failed",
        )
        assert state_for(env.app).external_admission.exclusions
    elif signal == "reset-mark":
        await record_quota_exhaustion(
            env.app,
            session,
            "Individual quota reached. Resets in 20m",
            actor=None,
            from_runner=True,
            response_id="rejected",
        )
        assert state_for(env.app).exhausted[(owner, "pool-1")] > now
    else:
        attempt = {"from_runner": True, "response_id": "rejected", "input_generation": 1}
        await record_provider_recovery(env.app, session, started=True, **attempt)
        await record_quota_exhaustion(
            env.app,
            session,
            "HTTP 403 balance billing is disabled for this key",
            actor=None,
            **attempt,
        )
        assert state_for(env.app).refusals
    assert not env.prefs.session_choice_snapshot(owner).locks
    if signal == "billing-recovery":
        monkeypatch.setattr("omnigent.server.session_choice_runtime.time.time", lambda: now + 61)
        recovery = {"from_runner": True, "response_id": "recovered", "input_generation": 2}
        await record_provider_recovery(env.app, session, started=True, **recovery)
        await record_provider_recovery(env.app, session, **recovery)
        assert not state_for(env.app).refusals
    else:
        env.app.state.session_choice_states.clear()
    recovered = await create(env)
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["session_choice"]["pool"] == "pool-0"


async def test_codex_route_pool_aliases_share_billing_refusals_and_running_counts(
    choice_env, monkeypatch
):
    from dataclasses import replace

    from omnigent.models.model_reference import provider_groups
    from omnigent.server.session_choice import (
        ChoiceRequest,
        Link,
        parse_session_choice,
        resolved_link,
        state_for,
    )
    from omnigent.server.session_choice_runtime import (
        record_provider_recovery,
        record_quota_exhaustion,
    )

    env = choice_env
    owner, agent = RESERVED_USER_LOCAL, env.agents[0]["id"]
    pool, old_pool = "codex:new/account", "codex:old/account"
    pairs = [("old", "new"), ("other", "new")]
    env.prefs.patch_namespace(
        owner, "model_provider_aliases", {"pairs": [list(pair) for pair in pairs]}
    )
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "codex-native",
    )
    first_model, second_model = "route-a/gpt-example", "route-b/gpt-example"
    links = [{"agent": agent, "model": model} for model in (first_model, second_model)]
    env.settings["codexRoutePools"] = {"route-a": old_pool, "route-b": pool}
    env.settings["presets"][0]["links"] = links
    env.settings["hosts"][HOST]["work"] = [[{"links": links}]]
    env.settings["limits"] = {pool: {"maxRunning": 1}}
    env.prefs.replace_session_choice(
        owner, env.settings, env.prefs.session_choice(owner)["revision"]
    )

    admitted = await create(env)
    assert admitted.status_code == 201, admitted.text
    child = admitted.json()
    assert child["session_choice"]["pool"] == pool
    busy = await create(env)
    assert busy.status_code == 201, busy.text
    assert {
        item["link"]["model"]: (item["why"], item["running"])
        for item in busy.json()["session_choice"]["skipped"]
    } == dict.fromkeys((first_model, second_model), ("busy", [child["id"]]))

    session = env.store.get_conversation(child["id"])
    assert session is not None
    legacy = {
        "pool": old_pool,
        "model": first_model,
        "poolRoute": json.dumps([HOST, agent, old_pool]),
    }
    session = replace(session, labels={**session.labels, CHOICE_LABEL: json.dumps(legacy)})
    attempt = {"from_runner": True, "response_id": "refused", "input_generation": 1}
    await record_provider_recovery(env.app, session, started=True, **attempt)
    await record_quota_exhaustion(
        env.app,
        session,
        "HTTP 403 balance billing is disabled for this key",
        actor=None,
        **attempt,
    )
    refused = await create(env)
    assert refused.status_code == 201, refused.text
    assert {
        item["link"]["model"]: item["why"] for item in refused.json()["session_choice"]["skipped"]
    } == dict.fromkeys((first_model, second_model), "stop")
    assert set(state_for(env.app).refusals) == {(owner, pool, json.dumps([HOST, agent, pool]))}

    request = ChoiceRequest(
        owner,
        HOST,
        products={agent: "codex"},
        default_models={agent: second_model},
        provider_groups=provider_groups(pairs),
    )
    metered = Link(agent=agent, meter="codex:other/account")
    assert (
        resolved_link(parse_session_choice(env.settings), metered, request).model == second_model
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["route-secondary", "route-secondary-alt"])
@pytest.mark.parametrize("limit", ["busy", "reserve", "stop", "billing"])
async def test_codex_default_route_is_admitted_against_its_actual_tile(
    choice_env, monkeypatch, prefix, limit
):
    from omnigent.server.session_choice import ProviderRefusal, provider_route, state_for
    from omnigent.server.session_choice_runtime import release_choice
    from tests.server.test_agent_quota import usage_report

    env = choice_env
    env.settings["codexRoutePools"] = {"route-primary": "pool-primary", prefix: "pool-secondary"}
    model = f"{prefix}/gpt-small"
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "codex-native",
    )
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_llm_model",
        lambda *a, **kw: model,
    )
    link = {"agent": env.agents[0]["id"], "meter": "pool-primary"}
    env.settings["presets"][0]["links"] = [link]
    env.settings["hosts"][HOST]["work"] = [[{"links": [link]}]]
    env.settings["limits"] = {
        "pool-secondary": {
            "maxRunning": 1,
            "reserve": {"mode": "total", "window": "7d", "pct": 15, "stopPct": 5},
        }
    }
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    if limit == "billing":
        route = provider_route(
            HOST,
            env.agents[0]["id"],
            model,
            route_pools=env.settings["codexRoutePools"],
            product="codex",
        )
        state_for(env.app).refusals[(RESERVED_USER_LOCAL, "pool-secondary", route)] = (
            ProviderRefusal("billing_disabled", time.time(), None, "child", "turn")
        )
    elif limit == "busy":
        first = await create(env)
        assert first.status_code == 201, first.text
    else:
        now = time.time()
        env.app.state.agent_quota_service.report_account(
            RESERVED_USER_LOCAL,
            "pool-secondary",
            usage_report(
                account_product="codex",
                observed_at=now,
                windows=[
                    {
                        "id": "7d",
                        "percent_used": 90 if limit == "reserve" else 96,
                        "window_minutes": 10080,
                        "resets_at": now + 86400,
                    }
                ],
            ),
            now=now,
        )
    response = await create(env)
    assert response.status_code == 201, response.text
    child = response.json()
    choice = child["session_choice"]
    assert (choice["outcome"], choice["pool"], choice["reason"]) == (
        "hold",
        "pool-secondary",
        "stop" if limit == "billing" else limit,
    )
    assert child["model_override"] == choice["model"] == model
    assert choice_record(env.store.get_conversation(child["id"]))["pool"] == "pool-secondary"
    if limit in {"stop", "billing"}:
        env.settings["codexRoutePools"][prefix] = "pool-replacement"
        env.prefs.replace_session_choice(
            RESERVED_USER_LOCAL,
            env.settings,
            env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
        )
        assert not await release_choice(env.app, env.store.get_conversation(child["id"]), HOST)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_source", ["explicit", "agent-default", "missing"])
async def test_codex_meter_sessions_persist_through_create_queue_and_stop_targets(
    choice_env, db_uri, monkeypatch, model_source
):
    from dataclasses import replace

    from omnigent.server.agent_quota import QuotaContext
    from omnigent.server.session_choice import parse_session_choice
    from omnigent.server.session_choice_runtime import stop_targets, stopped_line, tile_of_session
    from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
    from tests.server.test_agent_quota import usage_report

    env = choice_env
    env.settings["codexRoutePools"] = {
        "route-primary": "pool-primary",
        "route-secondary": "pool-secondary",
    }
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "codex-native",
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    catalogs.upsert(HOST, "codex-native", [{"id": "gpt-example"}], time.time())
    env.app.state.host_model_catalog_cache_store = catalogs
    env.settings["limits"] = {
        "pool-primary": {
            "maxRunning": 1,
            "reserve": {"mode": "total", "window": "7d", "pct": 15, "stopPct": 5},
        },
        "pool-secondary": {"maxRunning": 1},
    }
    for index, pool in enumerate(("pool-primary", "pool-secondary")):
        link = {"agent": env.agents[index]["id"], "model": "gpt-example", "meter": pool}
        if model_source != "explicit":
            link.pop("model")
        env.settings["presets"][index]["links"] = [link]
        env.settings["hosts"][HOST]["work"][index][0]["links"] = [link]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    children = []
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_llm_model",
        lambda *a, **kw: None if model_source == "missing" else "gpt-example",
    )
    for _ in range(3):
        response = await create(env)
        if model_source == "missing":
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["reason"] == "Choose a model for this Codex route"
            return
        assert response.status_code == 201, response.text
        child = response.json()
        assert child["model_override"] == child["session_choice"]["model"] == "gpt-example"
        assert choice_record(env.store.get_conversation(child["id"]))["model"] == "gpt-example"
        children.append(child)
    primary, secondary, held = children
    assert held["session_choice"]["outcome"] == "hold"
    queue = HostLoadQueue(env.store)
    monkeypatch.setattr(queue, "_start", AsyncMock())
    monkeypatch.setattr(env.app.state.host_load_inputs, "deliver_session", AsyncMock())
    assert not await queue.release(env.app, held["id"], HOST)
    env.store.set_session_live_status(primary["id"], "idle")
    assert await queue.release(env.app, held["id"], HOST)
    queue._start.assert_awaited_once()
    now = time.time()
    env.app.state.agent_quota_service.report_account(
        RESERVED_USER_LOCAL,
        "pool-primary",
        usage_report(
            account_product="codex",
            observed_at=now,
            windows=[
                {
                    "id": "7d",
                    "percent_used": 96,
                    "window_minutes": 10080,
                    "resets_at": now + 86400,
                }
            ],
        ),
        now=now,
    )
    settings = parse_session_choice(env.settings)
    readings = env.app.state.agent_quota_service.pool_readings(
        QuotaContext(RESERVED_USER_LOCAL, None)
    )
    for child in (held, secondary):
        env.store.set_runner_id(child["id"], "a" * 32)
        env.store.set_session_live_status(child["id"], "running")
    env.store.touch_runner_liveness(["a" * 32], int(now))
    secondary_session = replace(
        env.store.get_conversation(secondary["id"]), reported_model="gpt-example"
    )
    assert tile_of_session(secondary_session, settings, readings, {}) is None
    assert stopped_line(env.app, secondary_session) is None
    assert stopped_line(env.app, env.store.get_conversation(held["id"])).state == "stop"
    assert stop_targets(
        env.app, RESERVED_USER_LOCAL, settings, readings, {}, {"pool-primary"}, now
    ) == {"pool-primary": (held["id"],)}


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex-native", "codex"])
async def test_plain_codex_model_launches_as_written_and_keeps_its_pool(
    choice_env, db_uri, monkeypatch, harness
):
    from omnigent.server.agent_quota import QuotaContext
    from omnigent.server.session_choice import parse_session_choice
    from omnigent.server.session_choice_runtime import stopped_line, tile_of_session
    from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
    from tests.server.test_agent_quota import usage_report

    env = choice_env
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl", lambda *a, **kw: harness
    )
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_llm_model",
        lambda *a, **kw: "gpt-6.1-sol",
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    catalogs.upsert(HOST, harness, [{"id": "gpt-6.1-sol"}], time.time())
    env.app.state.host_model_catalog_cache_store = catalogs
    for index, pool in enumerate(("pool-primary", "pool-secondary")):
        link = {"agent": env.agents[index]["id"], "model": "gpt-6.1-sol", "meter": pool}
        env.settings["presets"][index]["links"] = [link]
        env.settings["hosts"][HOST]["work"][index][0]["links"] = [link]
    env.settings["limits"] = {
        "pool-primary": {
            "maxRunning": 1,
            "reserve": {"mode": "total", "window": "7d", "pct": 15, "stopPct": 5},
        },
        "pool-secondary": {"maxRunning": 1},
    }
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    response = await create(env)
    assert response.status_code == 201, response.text
    child = response.json()
    assert child["model_override"] == child["session_choice"]["model"] == "gpt-6.1-sol"
    assert (child["session_choice"]["outcome"], child["session_choice"]["pool"]) == (
        "pick",
        "pool-primary",
    )
    record = choice_record(env.store.get_conversation(child["id"]))
    assert (record["pool"], record["model"]) == ("pool-primary", "gpt-6.1-sol")
    named = await create(
        env, agent_id=env.agents[1]["id"], user_named=True, model_override="gpt-6.1-sol"
    )
    assert named.status_code == 201, named.text
    assert named.json()["model_override"] == named.json()["session_choice"]["model"]
    assert named.json()["session_choice"]["pool"] == "pool-secondary"

    settings = parse_session_choice(env.settings)
    conv = env.store.get_conversation(child["id"])
    now = time.time()
    env.app.state.agent_quota_service.report_account(
        RESERVED_USER_LOCAL,
        "pool-primary",
        usage_report(
            account_product="codex",
            observed_at=now,
            windows=[
                {
                    "id": "7d",
                    "percent_used": 96,
                    "window_minutes": 10080,
                    "resets_at": now + 86400,
                }
            ],
        ),
        now=now,
    )
    readings = env.app.state.agent_quota_service.pool_readings(
        QuotaContext(RESERVED_USER_LOCAL, None)
    )
    assert tile_of_session(conv, settings, readings, {}) == "pool-primary"
    assert stopped_line(env.app, conv).state == "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex-native", "codex", "pi-native", "pi"])
@pytest.mark.parametrize("offered", [True, False, "failed"])
async def test_catalog_miss_does_not_exclude_configured_model(
    choice_env, db_uri, monkeypatch, harness, offered
):
    from omnigent.host.frames import CAP_CACHED_MODEL_OPTIONS
    from omnigent.server.session_choice import parse_session_choice
    from omnigent.server.session_choice_runtime import context_request
    from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore

    env = choice_env
    primary = "gpt-example"
    custom = "provider-example/missing-model"
    for index, model in enumerate((custom, primary)):
        env.settings["hosts"][HOST]["work"][index][0]["links"][0]["model"] = model
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    catalogs.upsert(HOST, harness, [{"id": primary}], 1)
    env.app.state.host_model_catalog_cache_store = catalogs
    connection = SimpleNamespace(
        hello=SimpleNamespace(
            configured_harnesses={harness: True}, capabilities=[CAP_CACHED_MODEL_OPTIONS]
        )
    )
    monkeypatch.setattr(env.app.state.host_registry, "get", lambda host: connection)
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl", lambda *a, **kw: harness
    )
    available = offered is True
    rows = [{"id": primary}, *([{"id": custom}] if available else [])]
    lookup = AsyncMock(return_value={"status": "ok", "models": rows})
    if offered == "failed":
        lookup.side_effect = RuntimeError("host disconnected")
    monkeypatch.setattr("omnigent.server.routes.calling_defaults._proxy_model_options", lookup)

    response = await create(env)
    assert response.status_code == 201, response.text
    assert response.json()["agent_id"] == env.agents[0]["id"]
    assert response.json()["model_override"] == custom
    assert not response.json()["session_choice"]["skipped"]
    assert catalogs.list([HOST])[0]["models"] == rows
    lookup.assert_awaited_once()
    if offered != "failed":
        await context_request(
            env.app, RESERVED_USER_LOCAL, HOST, parse_session_choice(env.settings), env.parent
        )
        lookup.assert_awaited_once()
    if available:
        from fastapi import HTTPException

        catalogs.upsert(HOST, harness, [{"id": primary}], 1)
        lookup.reset_mock()
        with pytest.raises(HTTPException) as denied:
            await context_request(
                env.app,
                RESERVED_USER_LOCAL,
                HOST,
                parse_session_choice(env.settings),
                env.parent,
                user_id="other@example.test",
            )
        assert denied.value.status_code == 403
        lookup.assert_not_awaited()
        connection.hello.capabilities = []
        legacy = await context_request(
            env.app, RESERVED_USER_LOCAL, HOST, parse_session_choice(env.settings), env.parent
        )
        assert legacy.models[env.agents[0]["id"]] == frozenset({primary})
        lookup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness_override", [None, "acp:alternate"])
async def test_named_acp_model_uses_the_agent_catalog(choice_env, db_uri, harness_override):
    from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore

    env = choice_env
    agent = await create_test_agent(
        env.client,
        name="custom-acp",
        executor={"type": "omnigent", "config": {"harness": "acp:example"}},
        include_llm=False,
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    catalogs.upsert(HOST, "acp", [{"id": "other-agent-model"}], time.time())
    catalogs.upsert(
        HOST, "acp:example", [{"id": "spec-model" if harness_override else "selected-model"}], 1
    )
    catalogs.upsert(HOST, "acp:alternate", [{"id": "selected-model"}], 1)
    env.app.state.host_model_catalog_cache_store = catalogs
    response = await create(
        env,
        agent_id=agent["id"],
        model_override="selected-model",
        user_named=True,
        **({"harness_override": harness_override} if harness_override else {}),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["session_choice"]["model"] == body["model_override"] == "selected-model"
    assert not body.get("notes") and not body["session_choice"]["warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("naming", [{}, {"user_named": True}, {"choice_reason": "Requested"}])
async def test_acp_override_uses_target_catalog_and_declared_pool(choice_env, db_uri, naming):
    from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore

    env = choice_env
    agent = await create_test_agent(
        env.client,
        name="custom-codex",
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
        include_llm=False,
    )
    link = {"agent": agent["id"], "model": "selected-model"}
    env.settings["presets"][0]["links"] = [link]
    env.settings["hosts"][HOST]["work"][0][0]["links"] = [link]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    catalogs.upsert(HOST, "codex-native", [{"id": "spec-model"}], 1)
    catalogs.upsert(HOST, "acp:alternate", [{"id": "selected-model"}], 1)
    env.app.state.host_model_catalog_cache_store = catalogs
    response = await create(env, agent_id=agent["id"], harness_override="acp:alternate", **naming)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["session_choice"]["model"] == body["model_override"] == "selected-model"
    assert not body.get("notes") and not body["session_choice"]["warnings"]
    assert body["session_choice"]["pool"] == "codex:selected-model"


@pytest.mark.asyncio
async def test_catalog_miss_preserves_configured_provider_model_with_visible_note(
    choice_env, db_uri, monkeypatch
):
    from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore

    env = choice_env
    env.settings["hosts"][HOST]["work"][0][0]["links"][0]["model"] = "old/example-model"
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    catalogs.upsert(HOST, "codex-native", [{"id": "new/example-model"}], time.time())
    env.app.state.host_model_catalog_cache_store = catalogs
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "codex-native",
    )
    response = await create(env)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["model_override"] == "old/example-model"
    assert body["notes"][0]["saved"] == "old/example-model"
    assert body["notes"][0]["outcome"] == "missing"
    assert body["session_choice"]["warnings"] and not body["session_choice"]["skipped"]


@pytest.mark.asyncio
async def test_defaults_read_failure_reaches_existing_choice_warnings(choice_env, monkeypatch):
    from omnigent.calling_defaults import load_master
    from omnigent.server.routes import _session_create_validation

    class UnavailablePreferences:
        def get(self, owner):
            raise OSError("preferences unavailable")

    async def failing_master(owner, store, **kwargs):
        return await load_master(owner, UnavailablePreferences(), **kwargs)

    monkeypatch.setattr(_session_create_validation, "load_master", failing_master)
    response = await create(choice_env)
    assert response.status_code == 201, response.text
    warnings = response.json()["session_choice"]["warnings"]
    assert len(warnings) == 1
    assert "calling defaults unavailable (storage_unavailable)" in warnings[0]
    assert "defaults" in warnings[0]


@pytest.fixture
async def quota_runner(choice_env, monkeypatch):
    import httpx

    from omnigent.server.routes import sessions

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(204)),
        base_url="http://runner",
    ) as runner:
        monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=runner))
        yield choice_env


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type", ["status", "legacy-status", "error", "legacy-error", "batch-error"]
)
@pytest.mark.parametrize(
    "message,absolute_reset",
    [
        ("Individual quota reached. Resets in 132h0m26s", None),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "Provider: OpenAI Official; model: gpt-6.2-sol; cause: Codex 官方额度保护 "
            "(quota protection): 每周 (weekly): 98.00% ≥ 98%（重置 / reset: "
            "2026-11-22T03:19:03+00:00）...",
            "2026-11-22T03:19:03+00:00",
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "Provider: OpenAI Official; model: gpt-6.2-sol; cause: Codex 官方额度保护: "
            "每周 98.00% ≥ 98%（重置 2026-11-22T03:19:03Z）",
            "2026-11-22T03:19:03+00:00",
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "cause: Codex quota protection: weekly 98.00% ≥ 98% "
            "(reset: 2026-11-22T03:19:03Z)",
            "2026-11-22T03:19:03+00:00",
        ),
    ],
    ids=["relative", "absolute-bilingual", "absolute-chinese", "absolute-english"],
)
async def test_provider_quota_failure_blocks_pool_until_reset(
    quota_runner, monkeypatch, event_type, message, absolute_reset
):
    env = quota_runner
    clock = [time.time()]
    if absolute_reset:
        clock[0] = datetime.fromisoformat(absolute_reset).timestamp() - 3600
    monkeypatch.setattr("omnigent.server.session_choice_runtime.time.time", lambda: clock[0])
    first = (await create(env)).json()
    if event_type in {"status", "legacy-status"}:
        from omnigent.harnesses.antigravity_native.steps import error_message_text

        message = error_message_text(
            {
                "type": "CORTEX_STEP_TYPE_ERROR_MESSAGE",
                "status": "CORTEX_STEP_STATUS_DONE",
                "errorMessage": {"shouldShowUser": True, "error": {"userErrorMessage": message}},
            }
        )
        assert isinstance(message, str)
    data = (
        {"status": "failed", "output": message, "response_id": "quota-turn"}
        if event_type in {"status", "legacy-status"}
        else {
            "item_type": "error",
            "item_data": {"message": message, "source": "execution", "code": "native_turn_error"},
            "response_id": "quota-turn",
            "source_id": "quota-error",
        }
    )
    if event_type in {"legacy-status", "legacy-error"}:
        data.pop("response_id")
        data.pop("source_id", None)
    event = {
        "type": "external_session_status"
        if event_type in {"status", "legacy-status"}
        else "external_conversation_item",
        "data": data,
    }
    payload = [event] if event_type == "batch-error" else event
    response = await env.client.post(f"/v1/sessions/{first['id']}/events", json=payload)
    assert response.status_code == 202, response.text
    reset = (
        datetime.fromisoformat(absolute_reset).timestamp()
        if absolute_reset
        else clock[0] + 132 * 3600 + 26
    )
    env.store.set_session_live_status(first["id"], "idle")
    second = await create(env)
    assert second.status_code == 201, second.text
    choice = second.json()["session_choice"]
    assert choice["pool"] == "pool-1"
    assert choice["skipped"][0]["why"] == "exhausted"
    assert choice["skipped"][0]["until"] == reset

    named = await create(
        env,
        agent_id=env.agents[0]["id"],
        model_override="test-model",
        choice_reason="specific need",
    )
    assert named.status_code == 409, named.text
    assert named.json()["detail"]["code"] == "agent_exhausted"
    assert named.json()["detail"]["reset"] == reset

    clock[0] += 30
    if absolute_reset:
        from omnigent.server.session_choice import state_for

        class NoDeadlineRewrite(dict[tuple[str, str], float]):
            def __setitem__(self, key: tuple[str, str], value: float) -> None:
                raise AssertionError("replayed refusal rewrote the absolute deadline")

        state = state_for(env.app)
        assert state.exhausted[(RESERVED_USER_LOCAL, "pool-0")] == reset
        state.exhausted = NoDeadlineRewrite(state.exhausted)
    replay = await env.client.post(f"/v1/sessions/{first['id']}/events", json=payload)
    assert replay.status_code == 202, replay.text
    env.settings["hosts"][HOST]["work"] = env.settings["hosts"][HOST]["work"][:1]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    held = await create(env)
    assert held.status_code == 201, held.text
    assert held.json()["session_choice"]["outcome"] == "hold"
    assert held.json()["session_choice"]["clears_at"] == reset
    clock[0] = reset
    replay = await env.client.post(f"/v1/sessions/{first['id']}/events", json=payload)
    assert replay.status_code == 202, replay.text
    recovered = await create(env)
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["session_choice"]["outcome"] == "pick"
    assert recovered.json()["session_choice"]["pool"] == "pool-0"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["external_session_status", "external_conversation_item"])
@pytest.mark.parametrize("primary_quota", ["exhausted", "reserve"])
async def test_codex_reviewer_quota_does_not_close_secondary_drain(
    quota_runner, monkeypatch, event_type, primary_quota
):
    from omnigent.server.session_choice import state_for
    from tests.server.test_agent_quota import usage_report

    env = quota_runner
    env.settings["codexRoutePools"] = {
        "route-primary": "pool-primary",
        "route-secondary-alt": "pool-secondary",
    }
    owner = RESERVED_USER_LOCAL
    reset = datetime.fromisoformat("2026-11-22T03:19:03+00:00").timestamp()
    now = reset - 3600
    monkeypatch.setattr("omnigent.server.session_choice_runtime.time.time", lambda: now)
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "codex",
    )
    agent = env.agents[0]["id"]
    env.settings["hosts"][HOST]["work"] = [
        [
            {
                "links": [
                    {"agent": agent, "model": "gpt-small", "meter": "pool-primary"},
                    {
                        "agent": agent,
                        "model": "route-secondary-alt/gpt-small",
                        "fallbackMode": "drain",
                    },
                ]
            }
        ]
    ]
    env.settings["limits"]["pool-primary"] = {
        "reserve": {"mode": "total", "window": "7d", "pct": 4}
    }
    env.prefs.replace_session_choice(
        owner, env.settings, env.prefs.session_choice(owner)["revision"]
    )
    state = state_for(env.app)
    if primary_quota == "exhausted":
        state.exhausted[(owner, "pool-primary")] = reset
    else:
        quota = usage_report(
            account_product="codex",
            observed_at=now,
            windows=[
                {"id": "7d", "percent_used": 98, "window_minutes": 10080, "resets_at": reset}
            ],
        )
        env.app.state.agent_quota_service.report_account(owner, "pool-primary", quota, now=now)

    fallback = await create(env)
    assert fallback.status_code == 201, fallback.text
    child = fallback.json()
    assert child["session_choice"]["pool"] == "pool-secondary"
    assert child["session_choice"]["skipped"][0]["why"] == primary_quota
    drain = env.prefs.session_choice_snapshot(owner).locks
    assert [lock.pool for lock in drain.values()] == ["pool-secondary"]
    exhausted = state.exhausted.copy()

    async def report(message, response_id):
        if event_type == "external_session_status":
            data = {"status": "failed", "output": message}
        else:
            data = {
                "item_type": "error",
                "item_data": {
                    "message": message,
                    "source": "execution",
                    "code": "native_turn_error",
                },
                "source_id": response_id,
            }
        posted = await env.client.post(
            f"/v1/sessions/{child['id']}/events",
            json={"type": event_type, "data": {**data, "response_id": response_id}},
        )
        assert posted.status_code == 202, posted.text

    # Keep the nested CC Switch refusal from the runner error while omitting session metadata.
    await report(
        "Error: sub-agent turn failed: inner executor error: This content was flagged for "
        "possible cybersecurity risk; approval review failed: unexpected status 403 Forbidden: "
        "CC Switch local proxy failed while handling Codex endpoint /responses. "
        "Provider: OpenAI Official; model: codex-auto-review; cause: Codex 官方额度保护 "
        "(quota protection): 每周 (weekly): 98.00% ≥ 98%（重置 / reset: "
        "2026-11-22T03:19:03+00:00）",
        "reviewer-error",
    )
    assert (owner, "pool-secondary") not in state.exhausted
    assert state.exhausted == exhausted
    assert env.prefs.session_choice_snapshot(owner).locks == drain

    following = await create(env)
    assert following.status_code == 201, following.text
    assert following.json()["session_choice"]["pool"] == "pool-secondary"

    await report("Individual quota reached. Resets in 20m", "model-error")
    assert state.exhausted[(owner, "pool-secondary")] == now + 1200
    assert not env.prefs.session_choice_snapshot(owner).locks


@pytest.mark.asyncio
async def test_assistant_fallback_is_not_a_provider_quota_rejection(quota_runner):
    from omnigent.entities import MessageData, NewConversationItem

    env = quota_runner
    child = (await create(env)).json()
    env.store.append(
        child["id"],
        [
            NewConversationItem(
                type="message",
                response_id="echo-turn",
                data=MessageData(
                    role="assistant",
                    agent="example",
                    content=[
                        {
                            "type": "output_text",
                            "text": "Individual quota reached. Resets in 1h",
                        }
                    ],
                ),
            )
        ],
    )
    failed = await env.client.post(
        f"/v1/sessions/{child['id']}/events",
        json={
            "type": "external_session_status",
            "data": {"status": "failed", "response_id": "echo-turn"},
        },
    )
    assert failed.status_code == 202, failed.text
    env.store.set_session_live_status(child["id"], "idle")
    response = await create(env)
    assert response.status_code == 201, response.text
    assert response.json()["session_choice"]["pool"] == "pool-0"


@pytest.mark.asyncio
async def test_shared_editor_cannot_exhaust_owners_pool(auth_app, auth_client):
    from omnigent.db.db_models import workspace_scope
    from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, resolve_auth_header
    from omnigent.server.session_choice import state_for

    store = auth_app.state.conversation_store
    permissions = auth_app.state.permission_store
    session = store.create_conversation(labels={CHOICE_LABEL: json.dumps({"pool": "pool-0"})})
    owner, editor = "owner@example.test", "editor@example.test"
    for user, level in ((owner, LEVEL_OWNER), (editor, LEVEL_EDIT)):
        permissions.ensure_user(user)
        permissions.grant(user, session.id, level)
    event = {
        "type": "external_session_status",
        "data": {
            "status": "failed",
            "response_id": "quota-turn",
            "output": "Individual quota reached. Resets in 1h",
        },
    }
    for actor in (editor, owner):
        response = await auth_client.post(
            f"/v1/sessions/{session.id}/events",
            json=event,
            headers={resolve_auth_header(): actor},
        )
        assert response.status_code == 202, response.text
        readings = state_for(auth_app).quota_readings(owner, {}, time.time())
        assert ("pool-0" in readings) is (actor == owner)
    with workspace_scope(42):
        assert not state_for(auth_app).quota_readings(owner, {}, time.time())


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
@pytest.mark.parametrize(
    "overrides,model,effort",
    [
        ({}, "configured-default", "medium"),
        ({"work_type": "work"}, "first-named-link", "low"),
        ({"work_type": "work", "reasoning_effort": "high"}, "first-named-link", "high"),
        (
            {"model_override": "explicit-model", "reasoning_effort": "high"},
            "explicit-model",
            "high",
        ),
    ],
)
async def test_named_create_uses_first_link_for_requested_or_default_work_type(
    choice_env, overrides, model, effort
):
    env = choice_env
    agent = env.agents[1]["id"]
    env.settings["hosts"][HOST]["work"][0][0]["links"].append(
        {"agent": agent, "model": "first-named-link", "effort": "low", "meter": "pool-first"}
    )
    env.settings["workTypes"].append({"id": "default", "label": "Configured default"})
    env.settings["defaultWorkType"] = "default"
    env.settings["hosts"][HOST]["default"] = [
        [{"links": [{"agent": agent, "model": "configured-default", "effort": "medium"}]}]
    ]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    response = await env.client.post(
        "/v1/sessions",
        headers=HEADERS,
        json={
            "parent_session_id": env.parent.id,
            "agent_id": agent,
            "choice_reason": "The task needs this agent",
            **overrides,
        },
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["agent_id"] == agent
    assert result["model_override"] == result["session_choice"]["model"] == model
    assert result["reasoning_effort"] == result["session_choice"]["effort"] == effort
    if model == "first-named-link":
        assert result["session_choice"]["pool"] == "pool-first"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_model_less_link", [False, True])
async def test_named_create_requires_model_in_host_work_chain(choice_env, has_model_less_link):
    env = choice_env
    agent = env.agents[1]["id"]
    env.settings["hosts"][HOST]["work"].pop()
    if has_model_less_link:
        env.settings["hosts"][HOST]["work"][0][0]["links"].append({"agent": agent})
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    response = await create(env, agent_id=agent, user_named=True)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "session_choice_ask",
        "agent": agent,
        "host": HOST,
        "work_type": "work",
        "reason": "No model on the named agent's first matching link. Pass model explicitly.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [None, "second-model", "absent-model"])
async def test_automatic_create_honors_explicit_options_or_refuses_model(choice_env, model):
    env = choice_env
    env.settings["hosts"][HOST]["work"][0][0]["links"][0]["effort"] = "low"
    env.settings["hosts"][HOST]["work"][1][0]["links"][0]["model"] = "second-model"
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    response = await create(env, model_override=model, reasoning_effort="high")
    if model == "absent-model":
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "session_choice_ask"
        assert "absent-model" in detail["reason"]
        return
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["agent_id"] == env.agents[int(model is not None)]["id"]
    assert result["model_override"] == result["session_choice"]["model"] == (model or "test-model")
    assert result["reasoning_effort"] == result["session_choice"]["effort"] == "high"


@pytest.mark.asyncio
async def test_automatic_create_refuses_effort_unsupported_by_selected_harness(choice_env):
    response = await create(choice_env, reasoning_effort="none")
    assert response.status_code == 400, response.text
    assert "Effort 'none' is not supported by claude-sdk" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "named,old_provider,new_provider,accepted",
    [
        (False, "old", "new", True),
        (True, "old", "new", True),
        (False, "route-primary", "route-secondary", False),
    ],
)
async def test_explicit_create_resolves_a_registered_model_alias(
    choice_env, db_uri, monkeypatch, named, old_provider, new_provider, accepted
):
    from omnigent.models.model_reference import ALIASES_NAMESPACE
    from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore

    env = choice_env
    env.settings["codexRoutePools"] = {
        "route-primary": "pool-primary",
        "route-secondary": "pool-secondary",
    }
    env.prefs.patch_namespace(
        RESERVED_USER_LOCAL, ALIASES_NAMESPACE, {"pairs": [[old_provider, new_provider]]}
    )
    requested_model = f"{old_provider}/example"
    launch_model = f"{new_provider}/example"
    catalogs = HostModelCatalogCacheStore(db_uri)
    catalogs.upsert(HOST, "codex-native", [{"id": launch_model}], int(time.time()))
    env.app.state.host_model_catalog_cache_store = catalogs
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "codex-native",
    )
    env.settings["hosts"][HOST]["work"][0][0]["links"][0]["model"] = launch_model
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    args = {"agent_id": env.agents[0]["id"], "choice_reason": "Specific task"} if named else {}
    response = await create(env, **args, model_override=requested_model, reasoning_effort="high")
    if not accepted:
        assert response.status_code == 409, response.text
        assert requested_model in response.json()["detail"]["reason"]
        return
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["model_override"] == result["session_choice"]["model"] == launch_model
    assert result["reasoning_effort"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [False, True])
async def test_explicit_create_persists_effort_alias_for_selected_harness(
    choice_env, monkeypatch, named
):
    env = choice_env
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "openai-agents",
    )
    args = {"agent_id": env.agents[0]["id"], "choice_reason": "Specific task"} if named else {}
    response = await create(env, **args, model_override="test-model", reasoning_effort="ultra")
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["reasoning_effort"] == result["session_choice"]["effort"] == "xhigh"
    assert env.store.get_conversation(result["id"]).reasoning_effort == "xhigh"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "foreign"])
async def test_create_skips_an_unbindable_agent_before_choosing_next(choice_env, invalid):
    env = choice_env
    bad_id = "f" * 32
    if invalid == "foreign":
        env.app.state.agent_store.create_user_agent(
            bad_id, "foreign", "test:///bundle", "other@example.test"
        )
    env.settings["hosts"][HOST]["work"][0][0]["links"].insert(0, {"agent": bad_id})
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    response = await create(env)
    assert response.status_code == 201, response.text
    assert response.json()["agent_id"] == env.agents[0]["id"]
    assert response.json()["session_choice"]["skipped"][0]["why"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("default_model", [False, True])
@pytest.mark.parametrize("schema_version", [1, 2])
async def test_named_reason_checks_cap_but_user_can_override(
    choice_env, default_model, schema_version
):
    env = choice_env
    if default_model:
        model = env.agents[0]["name"]
        env.settings["presets"][0]["links"][0]["model"] = model
        env.settings["hosts"][HOST]["work"][0][0]["links"][0]["model"] = model
        env.prefs.replace_session_choice(
            RESERVED_USER_LOCAL,
            env.settings,
            env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
        )
    if schema_version == 2:
        model = env.agents[0]["name"] if default_model else "test-model"
        save_rules(
            env, [{"id": "scoped", "agent": env.agents[0]["id"], "model": model, "maxRunning": 1}]
        )
    first = await create(env)
    assert first.status_code == 201, first.text
    named = {
        "agent_id": env.agents[0]["id"],
        "model_override": "test-model",
        "choice_reason": "specific need",
    }
    if default_model:
        named.pop("model_override")
    refused = await create(env, **named)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "agent_busy"
    if schema_version == 2:
        detail = refused.json()["detail"]
        assert detail["savedRevision"] == env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"]
        assert detail["effectiveLimits"]["maxRunning"]["sourceRuleId"] == "scoped"
        assert detail["effectiveLimits"]["maxRunning"]["runningCount"] == 1
    allowed = await create(env, **{**named, "user_named": True})
    assert allowed.status_code == 201, allowed.text


@pytest.mark.asyncio
@pytest.mark.parametrize("link_cap", [None, 2])
async def test_named_chain_keeps_own_cap_or_inherits_legacy_same_pool_cap(
    choice_env, monkeypatch, link_cap
):
    env = choice_env
    agent = env.agents[0]["id"]
    model = "gpt-example"
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "codex-native",
    )
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_llm_model", lambda *a, **kw: model
    )
    env.settings["presets"][0]["links"] = [{"agent": agent, "model": model, "maxRunning": 1}]
    link = {"agent": agent, "model": model}
    if link_cap is not None:
        link["maxRunning"] = link_cap
    env.settings["hosts"][HOST]["work"][0] = [{"links": [link]}]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    first = await create(env)
    assert first.status_code == 201, first.text
    named = {"agent_id": agent, "choice_reason": "Specific task"}
    response = await create(env, **named)
    if link_cap is not None:
        assert response.status_code == 201, response.text
        assert response.json()["session_choice"]["maxRunning"] == link_cap
    else:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "agent_busy"
        assert response.json()["detail"]["running"] == [first.json()["id"]]
        user_named = await create(env, **named, user_named=True)
        assert user_named.status_code == 201, user_named.text


@pytest.mark.asyncio
async def test_host_hold_rechecks_pool_after_admission_timeout(choice_env, monkeypatch):
    env = choice_env
    now = time.time()
    clock = [now]
    monkeypatch.setattr("omnigent.server.session_choice_runtime.time.time", lambda: clock[0])
    env.app.state.system_status = SimpleNamespace(
        queue_enabled=lambda workspace: True,
        host_load=lambda *args: {
            "level": "new_session_hold",
            "load_per_core": 6,
            "lines": {"heavy": 4, "new_session": 5},
        },
    )
    held_response = await create(env)
    assert held_response.status_code == 201, held_response.text
    held_id = held_response.json()["id"]
    held = env.store.get_conversation(held_id)
    assert held.labels[SLOT_LABEL].startswith("queued@")
    assert not holds_slot(held, now)
    clock[0] += 121
    env.app.state.system_status = None
    other_response = await create(env)
    assert other_response.status_code == 201, other_response.text
    other = other_response.json()
    assert other["session_choice"]["pool"] == "pool-0"
    env.store.set_host_id(other["id"], "2" * 32, workspace="/opt/work/project")
    queue = HostLoadQueue(env.store)
    monkeypatch.setattr(queue, "_start", AsyncMock())
    monkeypatch.setattr(env.app.state.host_load_inputs, "deliver_session", AsyncMock())
    assert not await queue.release(env.app, held_id, HOST)
    queue._start.assert_not_awaited()
    env.store.set_session_live_status(other["id"], "idle")
    assert await queue.release(env.app, held_id, HOST)
    queue._start.assert_awaited_once()
    assert holds_slot(env.store.get_conversation(held_id), clock[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reserve", "state", "expected"),
    [
        ({"mode": "total", "window": "7d", "pct": 50}, "ok", True),
        ({"mode": "total", "window": "7d", "pct": 30}, "low", False),
        ({"mode": "daily", "window": "7d", "pct": 11}, "ok", True),
        (None, "tight", False),
        (None, "low", True),
    ],
)
async def test_notice_uses_reserve_else_low_pace(
    choice_env, monkeypatch, reserve, state, expected
):
    import httpx
    from fastapi import FastAPI

    from omnigent.server.routes.session_choice import create_session_choice_router
    from omnigent.usage.contract import UsageAccountView, UsageWindowView

    env = choice_env
    env.settings["limits"]["pool-0"]["reserve"] = reserve
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    reset = time.time() + 4 * 86400
    reading = UsageAccountView(
        id="pool-0",
        kind="account",
        display_name="Example",
        state=state,
        freshness="fresh",
        deciding_window="7d",
        windows=[
            UsageWindowView(
                id="7d",
                kind="weekly",
                label="7d",
                percent_used=60,
                remaining_pct=40,
                window_minutes=10080,
                resets_at=reset,
                state=state,
            )
        ],
    )
    monkeypatch.setattr(
        env.app.state.agent_quota_service, "pool_readings", lambda *a, **k: {"pool-0": reading}
    )
    app = FastAPI()
    app.state = env.app.state
    app.include_router(create_session_choice_router(env.prefs, None), prefix="/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/me/session-choice/notice",
            params={"host_id": HOST, "agent_id": env.agents[0]["id"], "model": "test-model"},
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert bool(payload["notice"]) is expected, payload
    if expected:
        assert payload["remaining_pct"] == 40
        assert payload["resets_at"] == reset


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_after_queue", [False, True])
async def test_user_named_host_hold_keeps_priority_at_release(
    choice_env, monkeypatch, stop_after_queue
):
    from omnigent.server.session_choice import state_for
    from omnigent.usage.contract import UsageAccountView, UsageWindowView

    env = choice_env
    first = await create(env)
    assert first.status_code == 201, first.text
    env.app.state.system_status = SimpleNamespace(
        queue_enabled=lambda workspace: True,
        host_load=lambda *args: {
            "level": "new_session_hold",
            "load_per_core": 6,
            "lines": {"heavy": 4, "new_session": 5},
        },
    )
    response = await create(
        env, agent_id=env.agents[0]["id"], user_named=True, model_override="test-model"
    )
    assert response.status_code == 201, response.text
    held_id = response.json()["id"]
    assert choice_record(env.store.get_conversation(held_id))["flags"] == ["user_named"]
    reset = time.time() + 86400
    exhausted = UsageAccountView(
        id="pool-0",
        kind="account",
        display_name="Pool",
        state="low",
        freshness="fresh",
        windows=[
            UsageWindowView(
                id="7d",
                kind="weekly",
                percent_used=100,
                remaining_pct=0,
                resets_at=reset,
                state="low",
            )
        ],
    )
    monkeypatch.setattr(
        env.app.state.agent_quota_service, "pool_readings", lambda *a, **k: {"pool-0": exhausted}
    )
    state_for(env.app).cooldowns[(RESERVED_USER_LOCAL, "pool-0")] = reset
    env.app.state.system_status = None
    queue = HostLoadQueue(env.store)
    monkeypatch.setattr(queue, "_start", AsyncMock())
    monkeypatch.setattr(env.app.state.host_load_inputs, "deliver_session", AsyncMock())
    if stop_after_queue:
        env.settings["limits"]["pool-0"]["reserve"] = {
            "mode": "total",
            "window": "7d",
            "stopPct": 0,
        }
        env.prefs.replace_session_choice(
            RESERVED_USER_LOCAL,
            env.settings,
            env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
        )
        assert not await queue.release(env.app, held_id, HOST)
        queue._start.assert_not_awaited()
        env.settings["limits"]["pool-0"].pop("reserve")
        env.prefs.replace_session_choice(
            RESERVED_USER_LOCAL,
            env.settings,
            env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
        )
    assert await queue.release(env.app, held_id, HOST)
    queue._start.assert_awaited_once()
    assert holds_slot(env.store.get_conversation(first.json()["id"]), time.time())
    assert holds_slot(env.store.get_conversation(held_id), time.time())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["quota", "running", "choose"])
async def test_failed_reads_preserve_unknown_quota_but_do_not_bypass_caps_or_selector(
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
    if failure == "choose":
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "session_choice_unavailable"
        return
    assert response.status_code == 201, response.text
    assert response.json()["agent_id"] == env.agents[failure == "running"]["id"]
    if failure == "running":
        assert response.json()["session_choice"]["skipped"][0]["why"] == "count_unavailable"


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
async def test_explicit_model_hold_releases_only_the_requested_model(choice_env, monkeypatch):
    env = choice_env
    env.settings["hosts"][HOST]["work"][1][0]["links"][0]["model"] = "other-model"
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    first = await create(env)
    assert first.status_code == 201, first.text
    response = await create(env, model_override="test-model", reasoning_effort="high")
    assert response.status_code == 201, response.text
    held = response.json()
    identity = {"model": "test-model", "effort": "high", "pool": "pool-0"}
    assert held["session_choice"]["outcome"] == "hold"
    assert {key: held["session_choice"][key] for key in identity} == identity
    assert held["session_choice"]["skipped"][1]["why"] == "model_mismatch"

    queue = HostLoadQueue(env.store)
    start = AsyncMock()
    monkeypatch.setattr(queue, "_start", start)
    monkeypatch.setattr(env.app.state.host_load_inputs, "deliver_session", AsyncMock())
    assert not await queue.release(env.app, held["id"], HOST)
    start.assert_not_awaited()
    env.store.set_session_live_status(first.json()["id"], "idle")
    assert await queue.release(env.app, held["id"], HOST)
    start.assert_awaited_once()
    released = env.store.get_conversation(held["id"])
    receipt = choice_record(released)
    assert receipt and receipt["outcome"] == "pick"
    assert {key: receipt[key] for key in identity} == identity
    assert (released.model_override, released.reasoning_effort) == ("test-model", "high")


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
        model_override="test-model",
        labels={CHOICE_LABEL: json.dumps({"pool": "forged"})},
    )
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", [1, 2])
@pytest.mark.parametrize(
    "used,user_named,exempt,code",
    [
        (90, True, False, None),
        (95, True, False, "agent_stopped"),
        (90, False, False, "agent_reserved"),
        (90, False, True, None),
        (95, False, True, "agent_stopped"),
        (100, False, False, "agent_stopped"),
    ],
)
async def test_named_quota_lines(choice_env, used, user_named, exempt, code, schema_version):
    from omnigent.usage.contract import UsageReport

    env = choice_env
    env.settings["limits"]["pool-0"] = {
        "reserve": {"mode": "total", "window": "7d", "pct": 15, "stopPct": 5},
        "reserveExempt": ["work"] if exempt else [],
    }
    if schema_version == 2:
        limits = env.settings["limits"].pop("pool-0")
        env.settings.update(
            schemaVersion=2,
            limitRules=[
                {
                    "id": "scoped",
                    "agent": env.agents[0]["id"],
                    "host": HOST,
                    "model": "test-model",
                    **limits,
                }
            ],
        )
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    now = time.time()
    env.app.state.agent_quota_service.report_account(
        RESERVED_USER_LOCAL,
        "pool-0",
        UsageReport.model_validate(
            {
                "observed_at": now,
                "windows": [
                    {
                        "id": "7d",
                        "percent_used": used,
                        "resets_at": now + 86400,
                    }
                ],
            }
        ),
        now=now,
    )
    response = await create(
        env,
        agent_id=env.agents[0]["id"],
        model_override="test-model",
        choice_reason="specific need",
        user_named=user_named,
    )
    assert response.status_code == (409 if code else 201), response.text
    if code:
        assert response.json()["detail"]["code"] == code


def save_rules(env, rules):
    env.settings.update(schemaVersion=2, limitRules=rules, limits={})
    env.settings["hosts"][HOST]["work"] = env.settings["hosts"][HOST]["work"][:1]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )


def occupied(env, *, host=HOST, model="test-model", agent=None):
    return env.store.create_conversation(
        agent_id=agent or env.agents[0]["id"],
        host_id=host,
        workspace="/opt/work/project",
        model_override=model,
        labels={SLOT_LABEL: f"admitted@{host}@pool-0@{time.time()}"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pool,route",
    [
        ("pool-primary", "route-primary"),
        ("pool-secondary", "route-secondary"),
        (None, "route-other"),
    ],
)
@pytest.mark.parametrize("limit", ["cap", "stop"])
async def test_scoped_codex_limits_follow_an_explicit_route_pin(
    choice_env, db_uri, monkeypatch, pool, route, limit
):
    from dataclasses import replace

    from omnigent.server.session_choice_runtime import (
        release_choice,
        session_identity,
        stopped_line,
    )
    from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
    from tests.server.test_agent_quota import usage_report

    env = choice_env
    agent = env.agents[0]["id"]
    model = f"{route}/gpt-example"
    link = {"agent": agent, "model": model}
    mapped = pool is not None
    if pool is None:
        pool = "pool-standalone"
        link["meter"] = pool
    else:
        env.settings["codexRoutePools"] = {route: pool}
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "codex-native",
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    env.app.state.host_model_catalog_cache_store = catalogs
    env.settings["presets"][0]["links"] = [link]
    env.settings["hosts"][HOST]["work"] = [[{"links": [link]}]]
    catalogs.upsert(HOST, "codex-native", [{"id": "gpt-example"}, {"id": model}], int(time.time()))
    rule = {"id": "launch", "agent": agent, "model": model, "maxRunning": 1}
    if limit == "stop":
        rule["reserve"] = {"mode": "total", "window": "7d", "stopPct": 5}
    # Plain scopes are exact IDs, not aliases for launches pinned to a route, and the
    # router lists the pinned id itself so a pinned link validates from the host list.
    save_rules(
        env, [{"id": "plain", "agent": agent, "model": "gpt-example", "maxRunning": 9}, rule]
    )
    first_response = await create(env)
    assert first_response.status_code == 201, first_response.text
    first = first_response.json()
    receipt = first["session_choice"]
    assert first["model_override"] == receipt["model"] == model
    cap = receipt["effectiveLimits"]["maxRunning"]
    assert (receipt["outcome"], cap["sourceRuleId"], cap["runningCount"]) == ("pick", "launch", 0)
    second_response = await create(env)
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()
    assert second["session_choice"]["outcome"] == "hold"
    assert second["session_choice"]["skipped"][0]["running"] == [first["id"]]
    current = env.store.get_conversation(first["id"])
    reported = replace(current, reported_model="gpt-example")
    assert session_identity(env.app, reported).model == model
    # Exercise the same report through the persisted snapshot and queue boundary.
    env.store.update_conversation(first["id"], reported_model="gpt-example")
    held = env.store.get_conversation(second["id"])
    assert await release_choice(env.app, held, HOST) is not mapped
    if limit == "stop":
        now = time.time()
        env.app.state.agent_quota_service.report_account(
            RESERVED_USER_LOCAL,
            pool,
            usage_report(
                account_product="codex",
                observed_at=now,
                windows=[
                    {
                        "id": "7d",
                        "percent_used": 96,
                        "window_minutes": 10080,
                        "resets_at": now + 86400,
                    }
                ],
            ),
            now=now,
        )
        line = stopped_line(env.app, reported)
        if mapped:
            assert line is not None and line.state == "stop"
        else:
            assert line is None
        stopped = await create(env)
        assert stopped.status_code == 201, stopped.text
        choice = stopped.json()["session_choice"]
        assert (choice["outcome"], choice["reason"], choice["model"]) == ("hold", "stop", model)
        assert choice["effectiveLimits"]["reserve.stopPct"]["sourceRuleId"] == "launch"
        assert not await release_choice(env.app, held, HOST)
    else:
        env.store.set_session_live_status(first["id"], "idle")
        if mapped:
            assert await release_choice(env.app, held, HOST)
        released = choice_record(env.store.get_conversation(second["id"]))
        assert released is not None
        assert released["model"] == model
        assert released["effectiveLimits"]["maxRunning"]["sourceRuleId"] == "launch"
        assert released["effectiveLimits"]["maxRunning"]["runningCount"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("pool", ["pool-primary", "pool-secondary"])
@pytest.mark.parametrize("limit", ["cap", "stop"])
async def test_scoped_codex_limits_follow_a_plain_launch_model(
    choice_env, db_uri, monkeypatch, pool, limit
):
    from dataclasses import replace

    from omnigent.server.session_choice import parse_session_choice
    from omnigent.server.session_choice_preview import candidate_preview
    from omnigent.server.session_choice_runtime import (
        release_choice,
        session_identity,
        stopped_line,
    )
    from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
    from tests.server.test_agent_quota import usage_report

    env = choice_env
    agent = env.agents[0]["id"]
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "codex-native",
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    catalogs.upsert(HOST, "codex-native", [{"id": "gpt-example"}], int(time.time()))
    env.app.state.host_model_catalog_cache_store = catalogs
    link = {"agent": agent, "model": "gpt-example", "meter": pool}
    env.settings["presets"][0]["links"] = [link]
    env.settings["hosts"][HOST]["work"] = [[{"links": [link]}]]
    rule = {"id": "launch", "agent": agent, "model": "gpt-example", "maxRunning": 1}
    if limit == "stop":
        rule["reserve"] = {"mode": "total", "window": "7d", "stopPct": 5}
    # A route-pinned scope is an exact ID, not an alias for a plain launch.
    save_rules(env, [{"id": "pinned", "agent": agent, "model": "route-primary/gpt-example"}, rule])
    first_response = await create(env)
    assert first_response.status_code == 201, first_response.text
    first = first_response.json()
    receipt = first["session_choice"]
    assert first["model_override"] == receipt["model"] == "gpt-example"
    cap = receipt["effectiveLimits"]["maxRunning"]
    assert (receipt["outcome"], receipt["pool"], cap["sourceRuleId"]) == (
        "pick",
        pool,
        "launch",
    )
    preview = await candidate_preview(
        env.app,
        RESERVED_USER_LOCAL,
        HOST,
        parse_session_choice(env.settings),
        agent,
        "gpt-example",
    )
    assert preview["candidate"]["model"] == "gpt-example"
    assert preview["configuredInTiers"]
    assert preview["effectiveLimits"]["maxRunning"]["sourceRuleId"] == "launch"
    assert preview["check"]["reason"] == "busy"
    second_response = await create(env)
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()
    assert second["session_choice"]["outcome"] == "hold"
    assert second["session_choice"]["skipped"][0]["running"] == [first["id"]]
    current = env.store.get_conversation(first["id"])
    reported = replace(current, reported_model="gpt-example")
    assert session_identity(env.app, reported).model == "gpt-example"
    # Exercise the same report through the persisted snapshot and queue boundary.
    env.store.update_conversation(first["id"], reported_model="gpt-example")
    held = env.store.get_conversation(second["id"])
    assert not await release_choice(env.app, held, HOST)
    if limit == "stop":
        now = time.time()
        env.app.state.agent_quota_service.report_account(
            RESERVED_USER_LOCAL,
            pool,
            usage_report(
                account_product="codex",
                observed_at=now,
                windows=[
                    {
                        "id": "7d",
                        "percent_used": 96,
                        "window_minutes": 10080,
                        "resets_at": now + 86400,
                    }
                ],
            ),
            now=now,
        )
        line = stopped_line(env.app, reported)
        assert line is not None and line.state == "stop"
        stopped = await create(env)
        assert stopped.status_code == 201, stopped.text
        choice = stopped.json()["session_choice"]
        assert (choice["outcome"], choice["reason"], choice["model"]) == (
            "hold",
            "stop",
            "gpt-example",
        )
        assert choice["effectiveLimits"]["reserve.stopPct"]["sourceRuleId"] == "launch"
        assert not await release_choice(env.app, held, HOST)
    else:
        env.store.set_session_live_status(first["id"], "idle")
        assert await release_choice(env.app, held, HOST)
        released = choice_record(env.store.get_conversation(second["id"]))
        assert released is not None
        assert released["model"] == "gpt-example"
        assert released["effectiveLimits"]["maxRunning"]["sourceRuleId"] == "launch"
        assert released["effectiveLimits"]["maxRunning"]["runningCount"] == 0


@pytest.mark.asyncio
async def test_specific_four_overrides_broad_three_and_broad_still_counts_all_hosts(choice_env):
    env = choice_env
    agent = env.agents[0]["id"]
    save_rules(
        env,
        [
            {"id": "base", "agent": agent, "maxRunning": 3},
            {"id": "model", "agent": agent, "model": "test-model", "maxRunning": 4},
        ],
    )
    for host in (HOST, "2" * 32, "3" * 32):
        occupied(env, host=host)
    fourth = await create(env)
    assert fourth.status_code == 201, fourth.text
    assert fourth.json()["session_choice"]["outcome"] == "pick"
    fifth = await create(env)
    assert fifth.status_code == 201, fifth.text
    assert fifth.json()["session_choice"]["outcome"] == "hold"
    assert len(fifth.json()["session_choice"]["skipped"][0]["running"]) == 4
    broad = await create(
        env, agent_id=agent, model_override="other-model", choice_reason="Need this model"
    )
    assert broad.status_code == 409, broad.text
    assert broad.json()["detail"]["code"] == "agent_busy"
    assert len(broad.json()["detail"]["running"]) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("model_first", [False, True])
async def test_equal_specificity_count_uses_field_source_scope(choice_env, model_first):
    env = choice_env
    agent = env.agents[0]["id"]
    host_rule = {"id": "host", "agent": agent, "host": HOST, "maxRunning": 2}
    model_rule = {"id": "model", "agent": agent, "model": "test-model", "maxRunning": 4}
    save_rules(env, [model_rule, host_rule] if model_first else [host_rule, model_rule])
    local = occupied(env)
    other_model = occupied(env, model="other-model")
    remote = occupied(env, host="2" * 32)
    response = await create(env)
    assert response.status_code == 201, response.text
    choice = response.json()["session_choice"]
    assert choice["outcome"] == ("pick" if model_first else "hold")
    if not model_first:
        assert set(choice["skipped"][0]["running"]) == {local.id, other_model.id}
        assert remote.id not in choice["skipped"][0]["running"]


@pytest.mark.asyncio
async def test_two_hosts_race_for_one_owner_scoped_slot(choice_env):
    from copy import deepcopy

    env = choice_env
    second_host = "2" * 32
    save_rules(env, [{"id": "base", "agent": env.agents[0]["id"], "maxRunning": 1}])
    env.settings["hosts"][second_host] = deepcopy(env.settings["hosts"][HOST])
    env.app.state.host_store.upsert_on_connect(second_host, "second-host", RESERVED_USER_LOCAL)
    save_rules(env, env.settings["limitRules"])
    second_parent = env.store.create_conversation(
        agent_id=env.agents[0]["id"], host_id=second_host, workspace="/opt/work/project"
    )
    results = await asyncio.gather(create(env), create(env, parent_session_id=second_parent.id))
    assert all(result.status_code == 201 for result in results), [r.text for r in results]
    assert sorted(r.json()["session_choice"]["outcome"] for r in results) == ["hold", "pick"]
    assert (
        sum(holds_slot(env.store.get_conversation(r.json()["id"]), time.time()) for r in results)
        == 1
    )


@pytest.mark.asyncio
async def test_queued_release_reloads_rule_and_excludes_its_running_identity(choice_env):
    from omnigent.server.session_choice_runtime import release_choice

    env = choice_env
    rules = [{"id": "base", "agent": env.agents[0]["id"], "maxRunning": 1}]
    save_rules(env, rules)
    occupied(env, host="2" * 32)
    held = (await create(env)).json()
    assert held["session_choice"]["outcome"] == "hold"
    current = env.store.get_conversation(held["id"])
    assert not await release_choice(env.app, current, HOST)
    rules[0]["maxRunning"] = 2
    save_rules(env, rules)
    env.store.set_runner_id(current.id, "e" * 32)
    env.store.touch_runner_liveness(["e" * 32], int(time.time()))
    env.store.set_session_live_status(current.id, "running")
    assert env.store.compare_and_set_label(
        current.id,
        SLOT_LABEL,
        current.labels[SLOT_LABEL],
        f"releasing@{HOST}@pool-0@{time.time() - 121}",
    )
    current = env.store.get_conversation(current.id)
    assert holds_slot(current, time.time())
    assert await release_choice(env.app, current, HOST)
    released = env.store.get_conversation(current.id)
    assert released.labels[SLOT_LABEL].startswith("releasing@")
    receipt = choice_record(released)
    assert receipt["savedRevision"] == env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"]
    assert receipt["savedRevision"] != held["session_choice"]["savedRevision"]
    assert receipt["outcome"] == "pick"
    cap = receipt["effectiveLimits"]["maxRunning"]
    assert (cap["value"], cap["sourceRuleId"], cap["runningCount"], cap["reservedCount"]) == (
        2,
        "base",
        1,
        1,
    )
    assert "runningSessionIds" not in cap


@pytest.mark.asyncio
async def test_release_receipt_write_failure_rolls_back_marker_and_racing_retry(choice_env):
    from sqlalchemy import event

    from omnigent.db.db_models import SqlConversationLabel
    from omnigent.server.session_choice_runtime import release_choice

    env = choice_env
    rules = [{"id": "base", "agent": env.agents[0]["id"], "maxRunning": 1}]
    save_rules(env, rules)
    occupied(env)
    held = (await create(env)).json()
    current = env.store.get_conversation(held["id"])
    assert held["session_choice"]["outcome"] == "hold"
    before = dict(current.labels)
    rules[0]["maxRunning"] = 2
    save_rules(env, rules)
    revision = env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"]

    def reject_receipt_write(connection, cursor, statement, parameters, context, executemany):
        if (
            statement.lstrip().upper().startswith("INSERT")
            and SqlConversationLabel.__tablename__ in statement
            and CHOICE_LABEL in str(parameters)
        ):
            raise RuntimeError("Injected receipt write failure")

    engine = env.store._conv_engine
    event.listen(engine, "before_cursor_execute", reject_receipt_write)
    try:
        with pytest.raises(RuntimeError, match="Injected receipt write failure"):
            await release_choice(env.app, current, HOST)
    finally:
        event.remove(engine, "before_cursor_execute", reject_receipt_write)
    assert env.store.get_conversation(current.id).labels == before
    results = await asyncio.gather(
        release_choice(env.app, current, HOST), release_choice(env.app, current, HOST)
    )
    assert sum(results) == 1
    released = env.store.get_conversation(current.id)
    assert released.labels[SLOT_LABEL].startswith("releasing@")
    receipt = choice_record(released)
    assert receipt["savedRevision"] == revision and receipt["outcome"] == "pick"
    assert receipt["effectiveLimits"]["maxRunning"]["value"] == 2


@pytest.mark.asyncio
async def test_ask_explains_each_blocked_candidate_from_locked_snapshot(choice_env, monkeypatch):
    env = choice_env
    first, second = [agent["id"] for agent in env.agents]
    env.settings.update(
        schemaVersion=2,
        limitRules=[
            {"id": "first-host", "agent": first, "host": HOST, "maxRunning": 1},
            {"id": "second-model", "agent": second, "model": "test-model", "maxRunning": 2},
        ],
        limits={"pool-1": {"maxRunning": 1}},
    )
    env.settings["workTypes"][0]["ifFull"] = "ask"
    saved = env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    occupied(env, agent=first)
    env.store.create_conversation(
        agent_id=second,
        host_id=HOST,
        workspace="/opt/work/project",
        model_override="test-model",
        labels={
            SLOT_LABEL: f"admitted@{HOST}@pool-1@{time.time()}",
            CHOICE_LABEL: json.dumps({"pool": "pool-1"}),
        },
    )
    original = env.prefs.session_choice_snapshot
    captured = []

    def read_then_save(owner):
        snapshot = original(owner)
        if not captured:
            captured.append(snapshot.revision)
            changed = json.loads(json.dumps(env.settings))
            changed["limitRules"][0].update(id="later", maxRunning=9)
            env.prefs.replace_session_choice(owner, changed, snapshot.revision)
        return snapshot

    monkeypatch.setattr(env.prefs, "session_choice_snapshot", read_then_save)
    response = await create(env)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["outcome"] == "ask" and detail["code"] == "session_choice_ask"
    assert detail["savedRevision"] == saved["revision"] != original(RESERVED_USER_LOCAL).revision
    assert "agent" not in detail and detail["pool"] is None and detail["tier"] is None
    assert "effectiveLimits" not in detail and "legacyChecks" not in detail
    skipped = {item["link"]["agent"]: item for item in detail["skipped"]}
    assert set(skipped) == {first, second}
    for agent, source, value, scope in [
        (first, "first-host", 1, {"agent": first, "host": HOST}),
        (second, "second-model", 2, {"agent": second, "model": "test-model"}),
    ]:
        row = skipped[agent]
        assert row["why"] == "busy" and row["savedRevision"] == saved["revision"]
        cap = row["effectiveLimits"]["maxRunning"]
        assert (cap["sourceRuleId"], cap["value"], cap["scope"]) == (source, value, scope)
        assert cap["runningCount"] == cap["reservedCount"] == 1
        assert "runningSessionIds" not in cap
    assert skipped[first]["legacyChecks"]["limits"] is None
    assert skipped[second]["legacyChecks"]["limits"]["maxRunning"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["automatic", "choice_reason", "user_named"])
async def test_admission_receipt_binds_locked_revision_sources_and_legacy(
    choice_env, monkeypatch, mode
):
    env = choice_env
    agent = env.agents[0]["id"]
    save_rules(env, [{"id": "base", "agent": agent, "maxRunning": 3}])
    env.settings["limits"] = {"pool-0": {"maxRunning": 2}}
    saved = env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    occupied(env)
    original = env.prefs.session_choice_snapshot
    captured = []

    def read_then_save(owner):
        snapshot = original(owner)
        if not captured:
            captured.append(snapshot.revision)
            changed = json.loads(json.dumps(env.settings))
            changed["limitRules"][0].update(id="later", maxRunning=7)
            env.prefs.replace_session_choice(owner, changed, snapshot.revision)
        return snapshot

    monkeypatch.setattr(env.prefs, "session_choice_snapshot", read_then_save)
    changes = (
        {}
        if mode == "automatic"
        else {
            "agent_id": agent,
            "model_override": "test-model",
            **(
                {"user_named": True}
                if mode == "user_named"
                else {"choice_reason": "Need this agent"}
            ),
        }
    )
    response = await create(env, **changes)
    assert response.status_code == 201, response.text
    data = response.json()
    receipt = data["session_choice"]
    assert receipt["savedRevision"] == saved["revision"]
    assert captured == [saved["revision"]]
    assert original(RESERVED_USER_LOCAL).revision != receipt["savedRevision"]
    cap = receipt["effectiveLimits"]["maxRunning"]
    assert (cap["value"], cap["sourceRuleId"], cap["runningCount"], cap["reservedCount"]) == (
        3,
        "base",
        1,
        1,
    )
    assert "runningSessionIds" not in cap
    assert receipt["legacyChecks"]["pool"] == "pool-0"
    assert receipt["legacyChecks"]["limits"]["maxRunning"] == 2
    if mode != "automatic":
        assert receipt["flags"] == [mode]
    assert choice_record(env.store.get_conversation(data["id"])) == receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["invalid", "unsupported", "running", "selector", "prefs"])
async def test_v2_fault_never_selects_unchecked_first_link(choice_env, monkeypatch, fault):
    env = choice_env
    save_rules(env, [{"id": "base", "agent": env.agents[0]["id"], "maxRunning": 1}])
    if fault in {"invalid", "unsupported"}:
        value = {
            **env.settings,
            "schemaVersion": 99 if fault == "unsupported" else 2,
            "enabled": "broken",
        }
        with env.prefs._write_session("inject_choice_fixture") as session:
            env.prefs._upsert_row(
                session,
                user_id=RESERVED_USER_LOCAL,
                key="settings.session_collab",
                value=json.dumps({"sessionChoice": value}),
            )
    else:

        def fail(*args, **kwargs):
            raise RuntimeError("Unavailable input")

        if fault == "prefs":
            monkeypatch.setattr(env.prefs, "session_choice_snapshot", fail)
        elif fault == "running":
            monkeypatch.setattr("omnigent.server.session_choice_runtime.running_pools", fail)
        else:
            monkeypatch.setattr(ChoiceState, "select", fail)
    response = await create(env)
    assert response.status_code == 409, response.text
    if fault == "running":
        assert response.json()["detail"]["skipped"][0]["why"] == "count_unavailable"
    else:
        assert response.json()["detail"]["code"] == (
            "session_choice_unavailable" if fault == "selector" else "session_choice_configuration"
        )


@pytest.mark.asyncio
async def test_default_model_snapshot_matches_rule_and_pins_actual_creation(
    choice_env, monkeypatch
):
    env = choice_env
    agent = env.agents[0]["id"]
    env.settings["hosts"][HOST]["work"][0][0]["links"][0].pop("model")
    save_rules(env, [{"id": "model", "agent": agent, "model": "test-model", "maxRunning": 1}])
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_llm_model", lambda *a, **k: "test-model"
    )
    first = await create(env)
    assert first.status_code == 201, first.text
    assert first.json()["model_override"] == "test-model"
    assert choice_record(env.store.get_conversation(first.json()["id"]))["model"] == "test-model"
    second = await create(env)
    assert second.status_code == 201, second.text
    assert second.json()["session_choice"]["outcome"] == "hold"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_agent", [False, True])
async def test_unresolved_running_model_blocks_only_possibly_matching_scope(
    choice_env, monkeypatch, same_agent
):
    env = choice_env
    save_rules(
        env,
        [{"id": "model", "agent": env.agents[0]["id"], "model": "test-model", "maxRunning": 1}],
    )
    occupied(env, model=None, agent=env.agents[0 if same_agent else 1]["id"])
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_llm_model", lambda *a, **k: None
    )
    response = await create(env)
    assert response.status_code == (409 if same_agent else 201), response.text
    if same_agent:
        assert response.json()["detail"]["skipped"][0]["why"] == "count_unavailable"
    else:
        assert response.json()["session_choice"]["outcome"] == "pick"


@pytest.mark.asyncio
async def test_running_snapshot_resolves_owner_inheritance_and_skips_other_owners(choice_env):
    from omnigent.server.session_choice import LimitScope, parse_session_choice
    from omnigent.server.session_choice_runtime import running_pools
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    env = choice_env
    permissions = SqlAlchemyPermissionStore(env.store.storage_location)
    for owner in ("owner@example.test", "other@example.test"):
        permissions.ensure_user(owner)
    permissions.grant("owner@example.test", env.parent.id, 4)
    inherited = env.store.create_conversation(
        agent_id=env.agents[0]["id"],
        parent_conversation_id=env.parent.id,
        kind="sub_agent",
        model_override="test-model",
        labels={SLOT_LABEL: f"admitted@{HOST}@pool-0@{time.time()}"},
    )
    foreign = occupied(env)
    permissions.grant("other@example.test", foreign.id, 4)
    env.app.state.permission_store = permissions
    snapshot = running_pools(
        env.app, "owner@example.test", parse_session_choice(env.settings), {}, time.time()
    )
    assert snapshot.scoped(LimitScope(agent=env.agents[0]["id"], host=HOST)) == (inherited.id,)


@pytest.mark.asyncio
async def test_empty_model_rule_adds_no_identity_requirement(choice_env, monkeypatch):
    env = choice_env
    env.settings["hosts"][HOST]["work"][0][0]["links"][0].pop("model")
    save_rules(env, [{"id": "empty", "agent": env.agents[0]["id"], "model": "test-model"}])
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_llm_model", lambda *a, **k: None
    )
    response = await create(env)
    assert response.status_code == 201, response.text
    assert response.json()["session_choice"]["outcome"] == "pick"


@pytest.mark.parametrize("patch", ["empty", "parsed_null", "copy_null"])
@pytest.mark.parametrize("user_named", [False, True])
def test_unknown_model_cancellation_is_an_identity_relevant_patch(patch, user_named):
    from omnigent.server.session_choice import (
        ChoiceRequest,
        Link,
        WorkType,
        check_candidate_limits,
        parse_session_choice,
    )

    settings = parse_session_choice(
        {
            "schemaVersion": 2,
            "limitRules": [
                {
                    "id": "base",
                    "agent": "agent-a",
                    "reserve": {"mode": "total", "window": "weekly", "stopPct": 5},
                },
                {
                    "id": "cancel",
                    "agent": "agent-a",
                    "model": "model-y",
                    **({} if patch == "empty" else {"reserve": None}),
                },
            ],
        }
    )
    if patch == "copy_null":
        settings = settings.model_copy(
            update={
                "limit_rules": (
                    settings.limit_rules[0],
                    settings.limit_rules[1].model_copy(update={"reserve": None}),
                )
            }
        )
    failure = check_candidate_limits(
        settings,
        WorkType(id="work", label="Work"),
        Link(agent="agent-a", meter="account-a"),
        ChoiceRequest(RESERVED_USER_LOCAL, HOST),
        {},
        {},
        {},
        time.time(),
        user_named=user_named,
    )
    assert (failure.why if failure else None) == (
        None if patch == "empty" else "identity_unavailable"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("related", [False, True])
async def test_owner_read_failure_only_blocks_a_possibly_matching_scope(
    choice_env, monkeypatch, related
):
    from omnigent.server.session_choice import LimitScope, parse_session_choice
    from omnigent.server.session_choice_runtime import running_pools

    env = choice_env
    agent = env.agents[0]["id"]
    save_rules(env, [{"id": "cap", "agent": agent, "maxRunning": 1}])
    row = occupied(env, agent=agent if related else env.agents[1]["id"])

    # Exercise the real owner resolver's failed branch through its store boundary.
    def broken_read(*args, **kwargs):
        raise RuntimeError("owner store unavailable")

    permissions = SimpleNamespace(list_for_sessions=broken_read, list_for_session=broken_read)
    monkeypatch.setattr(env.app.state, "permission_store", permissions)
    snapshot = running_pools(
        env.app, RESERVED_USER_LOCAL, parse_session_choice(env.settings), {}, time.time()
    )
    assert snapshot.available
    assert any(
        identity.session_id == row.id and not identity.owner_known
        for identity in snapshot.identities
    )
    assert snapshot.scoped(LimitScope(agent=agent)) == (None if related else ())
    # The selector sees local uncertainty, while a failed whole snapshot stays fail-closed.
    from omnigent.server.session_choice import ChoiceRequest, ChoiceState, RunningSnapshot

    settings = parse_session_choice(env.settings)
    context = ChoiceRequest(RESERVED_USER_LOCAL, HOST, preview=True)
    choice = ChoiceState().select(settings, "work", context, {}, snapshot, {}, now=time.time())
    assert choice.outcome == ("ask" if related else "pick")
    if related:
        assert choice.skipped[0].why == "count_unavailable"
    failed = ChoiceState().select(
        settings, "work", context, {}, RunningSnapshot(available=False), {}, now=time.time()
    )
    assert failed.outcome == "ask"
    assert failed.skipped[0].why == "count_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,pool_id",
    [(None, "pool-0"), ("route-primary", "pool-primary"), ("route-secondary", "pool-secondary")],
)
@pytest.mark.parametrize("omit_model", [False, True])
async def test_persisted_period_rule_reaches_create_choice(
    choice_env, monkeypatch, route, pool_id, omit_model
):
    from omnigent.server.session_choice import state_for
    from omnigent.usage.contract import UsageAccountView, UsageWindowView

    env = choice_env
    now = time.time()
    if route:
        env.settings["codexRoutePools"] = {route: pool_id}
        monkeypatch.setattr(
            "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
            lambda *a, **kw: "codex-native",
        )
    links = [
        {"agent": agent["id"], "model": f"{route}/{model}" if route else model, "meter": pool_id}
        for agent, model in zip(env.agents, ("model-a", "model-b"), strict=True)
    ]
    default_models = {link["agent"]: link["model"] for link in links}
    if omit_model:
        monkeypatch.setattr(
            "omnigent.server.routes._sessions.helpers._resolve_llm_model",
            lambda session, **kwargs: default_models.get(session.agent_id) if session else None,
        )
        for link in links:
            link.pop("model")
    env.settings["strategy"] = "balance"
    env.settings["limits"] = {}
    for index, link in enumerate(links):
        env.settings["presets"][index]["links"] = [link]
    env.settings["hosts"][HOST]["work"] = [
        [
            {"preset": "p0", "links": [links[0]], "weight": 1},
            {"preset": "p1", "links": [links[1]], "weight": 1},
        ],
    ]
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    reset = now + 4 * 86400
    pool = UsageAccountView(
        id=pool_id,
        kind="account",
        display_name="Pool",
        state="ok",
        pace_pct=70,
        freshness="fresh",
        read_at=now,
        deciding_window="7d",
        windows=[
            UsageWindowView(
                id="7d",
                kind="weekly",
                label="7d",
                percent_used=40,
                remaining_pct=60,
                window_minutes=10080,
                resets_at=reset,
                state="ok",
            ),
            UsageWindowView(
                id="5h",
                kind="session",
                label="5h",
                percent_used=90,
                remaining_pct=10,
                window_minutes=300,
                resets_at=reset,
                state="low",
            ),
        ],
    )
    monkeypatch.setattr(
        env.app.state.agent_quota_service,
        "pool_readings",
        lambda *args, **kwargs: {pool_id: pool},
    )
    plain = await create(env)
    assert plain.status_code == 201, plain.text
    assert plain.json()["agent_id"] == env.agents[0]["id"]

    state_for(env.app).counters.clear()
    rule = {
        "agent": env.agents[0]["id"],
        "model": default_models[env.agents[0]["id"]],
        "period": "5h",
    }
    env.prefs.patch_namespace(
        RESERVED_USER_LOCAL,
        "session_collab",
        {"quotaPeriodRules": [{"agent": rule["agent"], "period": "7d"}, rule]},
    )
    scoped = await create(env)
    assert scoped.status_code == 201, scoped.text
    assert scoped.json()["agent_id"] == env.agents[1]["id"]
    assert scoped.json()["model_override"] == (
        None if omit_model and route is None else default_models[env.agents[1]["id"]]
    )
    assert scoped.json()["session_choice"]["pool"] == pool_id


@pytest.mark.asyncio
async def test_owner_billing_report_cannot_stop_provider(quota_runner):
    from omnigent.server.session_choice import state_for

    env = quota_runner
    first = (await create(env)).json()
    response = await env.client.post(
        f"/v1/sessions/{first['id']}/events",
        json={
            "type": "external_session_status",
            "data": {
                "status": "failed",
                "output": "HTTP 403 balance billing is disabled for this key",
                "response_id": "fabricated-turn",
                "input_id": "fabricated-input",
                "input_generation": 99,
                "native_generation": 99,
            },
        },
    )
    assert response.status_code == 202, response.text
    assert not state_for(env.app).refusals
    env.store.set_session_live_status(first["id"], "idle")
    assert (await create(env)).json()["session_choice"]["pool"] == "pool-0"


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["live", "resume"])
async def test_codex_success_keeps_route_healthy_after_delayed_old_failure(
    quota_runner, tmp_path, delivery
):
    from omnigent.harnesses.codex_native import bridge
    from omnigent.harnesses.codex_native import forwarder as fwd
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.server.session_choice import state_for

    env = quota_runner
    first = (await create(env)).json()
    sid = first["id"]
    binding = "test-provider-recovery-runner"
    env.store.clear_runner_id(sid)
    assert env.store.set_runner_id(sid, token_bound_runner_id(binding))
    env.client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = binding
    state = state_for(env.app)

    async def start(turn, generation):
        assert await fwd._post_status(
            env.client,
            sid,
            "running",
            response_id=f"codex_{turn}",
            input_id=f"input-{turn}",
            native_generation=generation,
        )

    async def fail(turn, generation):
        assert await fwd._post_status(
            env.client,
            sid,
            "failed",
            response_id=f"codex_{turn}",
            input_id=f"input-{turn}",
            native_generation=generation,
            output="HTTP 403 balance billing is disabled for this key",
            native_turn_generation=generation,
        )

    # This older execution's failure has not yet reached the server.
    await start("delayed", 1)
    await start("failure", 2)
    await fail("failure", 2)
    assert state.refusals
    await start("success", 3)
    if delivery == "resume":
        assert await fwd._post_status(
            env.client,
            sid,
            "idle",
            response_id="codex_delayed",
            input_id="input-delayed",
            recovered=True,
            turn_completed=True,
            native_turn_generation=1,
        )
        assert state.refusals
        assert await fwd._post_status(
            env.client,
            sid,
            "idle",
            response_id="codex_unaccepted",
            input_id="unaccepted-input",
            recovered=True,
            turn_completed=True,
            native_turn_generation=4,
            native_generation=999,
        )
        assert state.refusals
        assert env.app.state.subagent_retry_admission.journal.get_input("unaccepted-input") is None
    bridge.write_bridge_state(
        tmp_path,
        bridge.CodexNativeBridgeState(
            session_id=sid,
            socket_path="/opt/work/app.sock",
            thread_id="thread",
            codex_home=str(tmp_path / "home"),
            active_turn_id="success",
            active_turn_revision=5,
            input_id="input-success",
            input_turn_id="success",
        ),
    )
    if delivery == "resume":
        await fwd._post_resume_turn_status(
            env.client,
            session_id=sid,
            bridge_dir=tmp_path,
            thread_id="thread",
            turns=[{"id": "success", "status": "completed", "items": []}],
            replay_from_turn_id="success",
        )
    else:
        edge = fwd._terminal_turn_status_edge(
            tmp_path, "turn/completed", {"turn": {"id": "success", "status": "completed"}}
        )
        assert edge is not None
        await fwd._post_turn_status_edge(
            env.client, sid, edge, input_id="input-success", bridge_dir=tmp_path
        )
    assert not state.refusals
    await fail("delayed", 1)
    assert not state.refusals
    assert state.provider_recoveries
    env.store.set_session_live_status(sid, "idle")
    env.client.headers.pop(RUNNER_TUNNEL_TOKEN_HEADER)
    selected = await create(env)
    assert selected.status_code == 201, selected.text
    assert selected.json()["session_choice"]["pool"] == "pool-0"


@pytest.mark.asyncio
async def test_billing_refusal_stops_usage_lines_and_selection_without_reset(quota_runner):
    from omnigent.usage.contract import UsageReportV2

    env = quota_runner
    service = env.app.state.agent_quota_service
    service.usage_enabled = True
    report = UsageReportV2.model_validate(
        {
            "schema_version": 2,
            "observed_at": time.time(),
            "status": "available",
            "windows": [],
            "balances": [{"id": "credit", "unit": "USD", "remaining": 10}],
            "source_type": "relay",
            "source": "sample-feeder",
        }
    )
    service.report_account(RESERVED_USER_LOCAL, "pool-0", report, now=time.time())
    first = (await create(env)).json()
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id

    binding = "test-billing-runner"
    env.store.clear_runner_id(first["id"])
    assert env.store.set_runner_id(first["id"], token_bound_runner_id(binding))
    headers = {RUNNER_TUNNEL_TOKEN_HEADER: binding}
    running = await env.client.post(
        f"/v1/sessions/{first['id']}/events",
        headers=headers,
        json={
            "type": "external_session_status",
            "data": {
                "status": "running",
                "response_id": "billing-failure",
                "input_id": "billing-input",
                "native_generation": 1,
            },
        },
    )
    assert running.status_code == 202, running.text
    response = await env.client.post(
        f"/v1/sessions/{first['id']}/events",
        headers=headers,
        json={
            "type": "external_session_status",
            "data": {
                "status": "failed",
                "output": "HTTP 403 balance billing is disabled for this key",
                "response_id": "billing-failure",
                "input_id": "billing-input",
                "native_generation": 1,
            },
        },
    )
    assert response.status_code == 202, response.text
    env.store.set_session_live_status(first["id"], "idle")
    lines = await env.client.get("/v1/me/usage/lines")
    assert lines.status_code == 200, lines.text
    tile = lines.json()["tiles"]["pool-0"]
    assert tile["state"] == "stop"
    assert tile["reason"] == "billing_disabled"
    assert tile["source"] == f"{first['id']}:billing-failure"
    assert tile["clears_at"] is None
    overview = await env.client.get("/v1/me/usage")
    assert overview.status_code == 200, overview.text
    account = next(row for row in overview.json()["accounts"] if row["id"] == "pool-0")
    assert account["state"] == "stopped"
    assert account["balances"][0]["remaining"] == 10
    assert account["mark"]["provider_refusal"]["source"] == tile["source"]
    assert account["source_type"] == "relay"
    assert account["source"] == "sample-feeder"
    newer = report.model_copy(update={"observed_at": time.time() + 1})
    service.report_account(RESERVED_USER_LOCAL, "pool-0", newer, now=time.time())
    assert (await env.client.get("/v1/me/usage/lines")).json()["tiles"]["pool-0"][
        "state"
    ] == "stop"
    second = await create(env)
    assert second.status_code == 201, second.text
    choice = second.json()["session_choice"]
    assert choice["pool"] == "pool-1"
    assert choice["skipped"][0]["why"] == "stop"


@pytest.mark.asyncio
async def test_multi_member_cap_refuses_fourth_launch_across_hosts_and_reports_effective_limits(
    choice_env,
):
    env = choice_env
    host_b = "2" * 32
    env.app.state.host_store.upsert_on_connect(host_b, "second-host", RESERVED_USER_LOCAL)
    parent_b = env.store.create_conversation(
        agent_id=env.agents[1]["id"], host_id=host_b, workspace="/opt/work/project"
    )
    link_a = {"agent": env.agents[0]["id"], "model": "claude-opus-5-5"}
    link_b = {"agent": env.agents[1]["id"], "model": "claude-opus-5-5"}
    settings = {
        "schemaVersion": 2,
        "enabled": True,
        "workTypes": [{"id": "work", "label": "Work", "ifFull": "ask"}],
        "presets": [
            {"id": "pa", "name": "Host A Opus", "links": [link_a]},
            {"id": "pb", "name": "Host B Opus", "links": [link_b]},
        ],
        "hosts": {
            HOST: {"work": [[{"preset": "pa", "links": [link_a], "weight": 1}]]},
            host_b: {"work": [[{"preset": "pb", "links": [link_b], "weight": 1}]]},
        },
        "limits": {},
        "limitRules": [
            {"id": "host-a-cap", "agent": env.agents[0]["id"], "maxRunning": 5},
            {
                "id": "opus-shared",
                "members": [
                    {"agent": env.agents[0]["id"], "host": HOST, "model": "claude-opus-5-5"},
                    {"agent": env.agents[1]["id"], "host": host_b, "model": "claude-opus-5-5"},
                ],
                "maxRunning": 3,
            },
        ],
    }
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )

    first = await create(env)
    assert first.status_code == 201, first.text
    first_cap = first.json()["session_choice"]["effectiveLimits"]["maxRunning"]
    assert first_cap["sourceRuleId"] == "opus-shared"
    assert first_cap["runningCount"] == 0
    assert {row["ruleId"]: row["runningCount"] for row in first_cap["bindingRules"]} == {
        "opus-shared": 0,
        "host-a-cap": 0,
    }

    second = await create(env)
    assert second.status_code == 201, second.text
    third = await env.client.post(
        "/v1/sessions",
        headers=HEADERS,
        json={"parent_session_id": parent_b.id, "work_type": "work"},
    )
    assert third.status_code == 201, third.text
    third_cap = third.json()["session_choice"]["effectiveLimits"]["maxRunning"]
    assert third_cap["sourceRuleId"] == "opus-shared"
    assert third_cap["runningCount"] == 2

    fourth_a = await create(env)
    assert fourth_a.status_code == 409, fourth_a.text
    detail_a = fourth_a.json()["detail"]
    assert detail_a["code"] == "session_choice_ask"
    skipped = detail_a["skipped"][0]
    assert skipped["why"] == "busy"
    assert skipped["ruleId"] == "opus-shared"
    assert skipped["effectiveLimits"]["maxRunning"]["sourceRuleId"] == "opus-shared"
    assert skipped["effectiveLimits"]["maxRunning"]["runningCount"] == 3
    assert {
        row["ruleId"]: (row["runningCount"], row["maxRunning"])
        for row in skipped["effectiveLimits"]["maxRunning"]["bindingRules"]
    } == {
        "opus-shared": (3, 3),
        "host-a-cap": (2, 5),
    }

    fourth_b = await env.client.post(
        "/v1/sessions",
        headers=HEADERS,
        json={
            "parent_session_id": parent_b.id,
            "work_type": "work",
            "agent_id": env.agents[1]["id"],
            "choice_reason": "need host b",
        },
    )
    assert fourth_b.status_code == 409, fourth_b.text
    detail_b = fourth_b.json()["detail"]
    assert detail_b["code"] == "agent_busy"
    assert detail_b["effectiveLimits"]["maxRunning"]["sourceRuleId"] == "opus-shared"
    assert detail_b["effectiveLimits"]["maxRunning"]["runningCount"] == 3


@pytest.mark.asyncio
async def test_link_permission_tier_create_source_precedence_and_validation(
    choice_env,
):
    from omnigent.server.permission_tiers import (
        PERMISSION_CAPABILITY_LABEL_KEY,
        PERMISSION_CAPABILITY_VALUE,
        PERMISSION_PROTOCOL_HEADER,
        PERMISSION_PROTOCOL_VERSION,
        ConfirmedPermissionState,
    )

    env = choice_env
    pi_agent = await create_test_agent(
        env.client,
        name="choice-pi",
        include_llm=False,
        executor={"type": "omnigent", "config": {"harness": "pi-native"}},
    )
    codex_agent = await create_test_agent(
        env.client,
        name="choice-codex",
        include_llm=False,
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
    )
    headers = {**HEADERS, PERMISSION_PROTOCOL_HEADER: PERMISSION_PROTOCOL_VERSION}

    def save_link_settings(work_links: dict[str, dict]):
        work_types = [{"id": key, "label": key.title()} for key in work_links]
        presets = [
            {"id": f"p-{key}", "name": key.title(), "links": [link]}
            for key, link in work_links.items()
        ]
        hosts = {
            HOST: {
                key: [[{"preset": f"p-{key}", "links": [link], "weight": 1}]]
                for key, link in work_links.items()
            }
        }
        env.prefs.replace_session_choice(
            RESERVED_USER_LOCAL,
            {
                "enabled": True,
                "workTypes": work_types,
                "presets": presets,
                "hosts": hosts,
                "limits": {},
            },
            env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
        )

    save_link_settings(
        {
            "tiered": {
                "agent": pi_agent["id"],
                "model": "test-model",
                "permissionTier": "accept-edits",
            },
            "default": {"agent": pi_agent["id"], "model": "test-model"},
            "invalid": {
                "agent": codex_agent["id"],
                "model": "test-model",
                "permissionTier": "accept-edits",
            },
            "escalated": {
                "agent": pi_agent["id"],
                "model": "test-model",
                "permissionTier": "auto",
            },
        }
    )

    # 1. Link permissionTier is used when caller omits permission_tier.
    from_link = await env.client.post(
        "/v1/sessions",
        headers=headers,
        json={"parent_session_id": env.parent.id, "work_type": "tiered"},
    )
    assert from_link.status_code == 201, from_link.text
    body_link = from_link.json()
    assert body_link["permission_tier"] == "accept-edits"
    assert body_link["permission_tier_source"] == "link"
    assert body_link["session_choice"]["permissionTier"] == "accept-edits"
    assert body_link["session_choice"]["permissionTierSource"] == "link"

    # 2. Explicit permission_tier wins over link permissionTier.
    from_explicit = await env.client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "parent_session_id": env.parent.id,
            "work_type": "tiered",
            "permission_tier": "plan",
        },
    )
    assert from_explicit.status_code == 201, from_explicit.text
    body_explicit = from_explicit.json()
    assert body_explicit["permission_tier"] == "plan"
    assert body_explicit["permission_tier_source"] == "explicit"
    assert body_explicit["session_choice"]["permissionTier"] == "plan"
    assert body_explicit["session_choice"]["permissionTierSource"] == "explicit"

    # 3. Absent link tier uses host default.
    from_default = await env.client.post(
        "/v1/sessions",
        headers=headers,
        json={"parent_session_id": env.parent.id, "work_type": "default"},
    )
    assert from_default.status_code == 201, from_default.text
    body_default = from_default.json()
    assert body_default["permission_tier"] == "auto"
    assert body_default["permission_tier_source"] == "host_default"
    assert body_default["session_choice"]["permissionTier"] == "auto"
    assert body_default["session_choice"]["permissionTierSource"] == "host_default"

    # 4. Link permissionTier is refused on harnesses that cannot take permission_tier.
    refused_harness = await env.client.post(
        "/v1/sessions",
        headers=headers,
        json={"parent_session_id": env.parent.id, "work_type": "invalid"},
    )
    assert refused_harness.status_code == 400, refused_harness.text
    assert "permission_tier" in refused_harness.text

    # 5. Link permissionTier cannot exceed creator's permission tier ceiling.
    plan_parent = env.store.create_conversation(
        agent_id=pi_agent["id"],
        host_id=HOST,
        workspace="/opt/work/project",
        labels={
            **ConfirmedPermissionState("plan", 1, "ready").labels("pi-native"),
            PERMISSION_CAPABILITY_LABEL_KEY: PERMISSION_CAPABILITY_VALUE,
        },
    )
    refused_ceiling = await env.client.post(
        "/v1/sessions",
        headers=headers,
        json={"parent_session_id": plan_parent.id, "work_type": "escalated"},
    )
    assert refused_ceiling.status_code == 403, refused_ceiling.text
    assert "exceed" in refused_ceiling.text

    # 6. Runner sys_session_create handle forwards permission_tier and permission_tier_source.
    from omnigent.runner.tool_dispatch import _CreatePlacement, _finalize_created_session

    handle = json.loads(
        await _finalize_created_session(
            body_link,
            server_client=env.client,
            conversation_id=env.parent.id,
            agent_id=body_link["agent_id"],
            title=None,
            message=None,
            publish_event=None,
            placement=_CreatePlacement(caller_host_id=HOST),
            inherited_host_id=HOST,
        )
    )
    assert handle["permission_tier"] == "accept-edits"
    assert handle["permission_tier_source"] == "link"
    assert handle["session_choice"]["permissionTierSource"] == "link"

    # 7. ACP host capability check refuses link and explicit permission_tier when host lacks
    #    delegated ACP tier support, and accepts + requires protocol header once supported.
    acp_agent = await create_test_agent(
        env.client,
        name="choice-acp",
        include_llm=False,
        executor={"type": "omnigent", "config": {"harness": "acp:ag"}},
    )
    save_link_settings(
        {
            "acp-tiered": {
                "agent": acp_agent["id"],
                "model": "test-model",
                "permissionTier": "accept-edits",
            },
            "pi-tiered": {
                "agent": pi_agent["id"],
                "model": "test-model",
                "permissionTier": "accept-edits",
            },
        }
    )
    refused_acp_link = await env.client.post(
        "/v1/sessions",
        headers=headers,
        json={"parent_session_id": env.parent.id, "work_type": "acp-tiered"},
    )
    assert refused_acp_link.status_code == 400, refused_acp_link.text
    assert "ACP" in refused_acp_link.text

    refused_acp_explicit = await env.client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "parent_session_id": env.parent.id,
            "work_type": "acp-tiered",
            "permission_tier": "plan",
        },
    )
    assert refused_acp_explicit.status_code == 400, refused_acp_explicit.text
    assert "ACP" in refused_acp_explicit.text

    from omnigent.host.frames import HostHelloFrame

    class _AcpHostRegistry:
        def __init__(self, caps: list[str]):
            self._conn = SimpleNamespace(
                hello=HostHelloFrame(
                    version="test",
                    frame_protocol_version=1,
                    name="sample-host",
                    configured_harnesses={"acp": True, "acp:ag": True, "pi-native": True},
                    capabilities=caps,
                    acp_permission_tiers={},
                )
            )

        def get(self, host_id: str):
            return self._conn if host_id == HOST else None

    env.app.state.host_registry = _AcpHostRegistry(["agy_acp_permissions:acp:ag"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://test"
    ) as legacy_client:
        acp_no_header = await legacy_client.post(
            "/v1/sessions",
            headers=HEADERS,
            json={"parent_session_id": env.parent.id, "work_type": "acp-tiered"},
        )
    assert acp_no_header.status_code == 426, acp_no_header.text

    acp_ok = await env.client.post(
        "/v1/sessions",
        headers=headers,
        json={"parent_session_id": env.parent.id, "work_type": "acp-tiered"},
    )
    assert acp_ok.status_code == 201, acp_ok.text
    assert acp_ok.json()["permission_tier"] == "accept-edits"
    assert acp_ok.json()["permission_tier_source"] == "link"

    # 8. Independent (same_host / sys_session_open / non-child) admission selects the link
    #    without applying link permissionTier, matching explicit permission_tier placement rules.
    from omnigent.server.schemas import ProjectSessionCreateRequest
    from omnigent.server.session_choice_runtime import admit_create

    independent_body = ProjectSessionCreateRequest(
        host_id=HOST, workspace="/opt/work/project", work_type="pi-tiered"
    )
    async with admit_create(
        env.app, independent_body, env.parent, RESERVED_USER_LOCAL
    ) as selection:
        assert selection is not None
        assert selection.body.agent_id == pi_agent["id"]
        assert selection.body.permission_tier is None
        assert selection.permission_tier_source == "host_default"
        assert selection.choice.permission_tier is None
        assert selection.choice.permission_tier_source == "host_default"


@pytest.fixture
async def project_choice_env(runtime_init, db_uri, tmp_path, mock_llm, monkeypatch):
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.routes import sessions
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
    from tests.server.conftest import _app_client

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    prefs = SqlAlchemyUserPreferencesStore(db_uri)
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(HOST, "sample-host", RESERVED_USER_LOCAL)
    project_store = SqlAlchemyProjectStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        project_store=project_store,
        user_preferences_store=prefs,
    )
    app.state.host_store = host_store
    from omnigent.server.routes import _session_create_validation, _workspace_validation
    from omnigent.server.routes._sessions import orchestration as orch_mod

    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=None))
    monkeypatch.setattr(
        _session_create_validation,
        "_authorize_host_for_workspace",
        AsyncMock(return_value="sample-host"),
    )
    monkeypatch.setattr(
        orch_mod,
        "_authorize_host_for_workspace",
        AsyncMock(return_value="sample-host"),
    )
    monkeypatch.setattr(
        _workspace_validation,
        "validate_workspace",
        AsyncMock(side_effect=lambda **kw: kw["workspace"]),
    )
    monkeypatch.setattr(
        orch_mod,
        "validate_workspace",
        AsyncMock(side_effect=lambda **kw: kw["workspace"]),
    )
    monkeypatch.setattr(
        _session_create_validation,
        "validate_existing_host_workspace",
        AsyncMock(side_effect=lambda **kw: kw["workspace"]),
    )
    async with _app_client(app, mock_llm, tmp_path) as client:
        agents = [await create_test_agent(client, name=f"choice-{index}") for index in range(2)]
        store = app.state.conversation_store
        parent = store.create_conversation(
            agent_id=agents[0]["id"], host_id=HOST, workspace="/opt/work/project"
        )
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
        yield SimpleNamespace(
            app=app,
            client=client,
            store=store,
            prefs=prefs,
            projects=project_store,
            parent=parent,
            agents=agents,
            settings=settings,
        )


@pytest.mark.asyncio
async def test_project_blocked_agents_enforced_at_funnel_choice_routing_and_subagent(
    project_choice_env, monkeypatch
):
    """Scenarios 1-6, 30: funnel, '*', other host, host None, choice, Smart Routing, sub-agent."""
    env = project_choice_env
    projects = env.projects
    agent_a = env.agents[0]["id"]
    agent_b = env.agents[1]["id"]
    other_host = "2" * 32
    env.app.state.host_store.upsert_on_connect(other_host, "other-host", RESERVED_USER_LOCAL)

    # Create project blocking agent_a on HOST only
    proj = projects.create(
        "a" * 32,
        "Alpha",
        None,
        config={"blocked_agents": {HOST: [agent_a]}},
    )
    proj_id = proj.id

    # Scenario 1: direct POST /v1/sessions with blocked agent on HOST → 409 naming P, A, H
    r1 = await env.client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_a,
            "host_id": HOST,
            "workspace": "/opt/work/project",
            "project_id": proj_id,
        },
    )
    assert r1.status_code == 409, r1.text
    err1 = r1.json()["error"]
    assert err1["code"] == "agent_blocked_for_project"
    assert "Alpha" in err1["message"]
    assert "sample-host" in err1["message"]

    # Scenario 3: other host unaffected
    r3 = await env.client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_a,
            "host_id": other_host,
            "workspace": "/opt/work/project",
            "project_id": proj_id,
        },
    )
    assert r3.status_code == 201, r3.text

    # Scenario 4: host None allowed when blocked only on HOST; blocked when '*' includes agent_a
    r4_ok = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent_a, "project_id": proj_id},
    )
    assert r4_ok.status_code == 201, r4_ok.text

    # Scenario 5: session choice auto skips agent_a (blocked_for_project) and picks agent_b
    r5 = await create(env, project_id=proj_id, host_id=HOST, workspace="/opt/work/project")
    assert r5.status_code == 201, r5.text
    assert r5.json()["agent_id"] == agent_b
    rec = choice_record(env.store.get_conversation(r5.json()["id"]))
    assert rec is not None
    assert any(
        s.get("agent") == agent_a and s.get("why") == "blocked_for_project"
        for s in rec.get("skipped", [])
    )

    # Scenario 6: session choice named pick with blocked agent → 409
    r6 = await create(
        env,
        project_id=proj_id,
        host_id=HOST,
        workspace="/opt/work/project",
        agent_id=agent_a,
        user_named=True,
    )
    assert r6.status_code == 409, r6.text
    assert r6.json()["error"]["code"] == "agent_blocked_for_project"

    # Scenario 30 (sub-agent): runner sub-agent of a running session is exempt after blocking
    parent_a = env.store.create_conversation(
        agent_id=agent_a, host_id=HOST, project_id=proj_id, workspace="/opt/work/project"
    )
    r30_sub = await env.client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_a,
            "host_id": HOST,
            "workspace": "/opt/work/project",
            "project_id": proj_id,
            "parent_session_id": parent_a.id,
            "sub_agent_name": "helper-worker",
        },
    )
    assert r30_sub.status_code == 201, r30_sub.text

    # Scenario 30 (Smart Routing): harness_override=auto rebinding to blocked agent → 409
    from omnigent.server.routes._sessions import orchestration as orch_mod

    with monkeypatch.context() as mp:
        mp.setattr(env.app.state, "startup_admission", None)
        mp.setattr(
            orch_mod,
            "native_coding_agent_for_agent_name",
            lambda name: "claude-native" if name == "choice-1" else None,
        )
        mp.setattr(
            orch_mod,
            "_resolve_native_smart_routing",
            AsyncMock(return_value=("choice-0", None, None, None)),
        )
        r30_sr = await env.client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_b,
                "harness_override": "auto",
                "host_id": HOST,
                "workspace": "/opt/work/project",
                "project_id": proj_id,
            },
        )
    assert r30_sr.status_code == 409, r30_sr.text
    assert r30_sr.json()["error"]["code"] == "agent_blocked_for_project"

    # Scenarios 2 & 4 ('*'): all-hosts block refuses both other_host and host=None
    projects.update(proj_id, user_id=None, config={"blocked_agents": {"*": [agent_a]}})
    r2 = await env.client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_a,
            "host_id": other_host,
            "workspace": "/opt/work/project",
            "project_id": proj_id,
        },
    )
    assert r2.status_code == 409
    r4_star = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent_a, "project_id": proj_id},
    )
    assert r4_star.status_code == 409


@pytest.mark.asyncio
async def test_j1_independent_opens_use_project_host_default_and_respect_boundaries(
    project_choice_env, monkeypatch
):
    """Scenarios 19, 20, 21, 23: J1 with/without session choice, naming, and boundaries."""
    from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
    from omnigent.server.routes.sessions import routes_core

    env = project_choice_env
    monkeypatch.setattr(
        routes_core, "require_orchestration_source", lambda *_args, **_kwargs: env.parent
    )
    projects = env.projects
    agent_a = env.agents[0]["id"]
    agent_b = env.agents[1]["id"]
    # Project P: default agent on HOST is agent_b with model "opus-default"
    proj = projects.create(
        "b" * 32,
        "J1Project",
        None,
        config={
            "calling_defaults": {
                HOST: {
                    "agent_id": agent_b,
                    "harnesses": {"claude-sdk": {"model": "opus-default"}},
                }
            },
            "orchestration": {"session_choice": {"pin": "p0"}},
        },
    )
    proj_id = proj.id
    # File parent in proj_id so opener's project is proj_id
    env.store.set_conversation_project(env.parent.id, proj_id)
    env.parent = env.store.get_conversation(env.parent.id)

    from omnigent.server.schemas import ProjectSessionCreateRequest
    from omnigent.server.session_choice_runtime import _AdmitBodyOverride, admit_create

    # Scenario 19: independent create with session choice active, no agent → J1 override
    body_19 = ProjectSessionCreateRequest(
        host_id=HOST, workspace="/opt/work/project", work_type="work"
    )
    async with admit_create(env.app, body_19, env.parent, None) as sel_19:
        assert isinstance(sel_19, _AdmitBodyOverride)
        assert "agent_id" not in sel_19.body.model_fields_set

    # Scenario 20a: reasonless agent_id=agent_a with session choice active → J1 strips it → agent_b
    body_20a = ProjectSessionCreateRequest(
        agent_id=agent_a, host_id=HOST, workspace="/opt/work/project", work_type="work"
    )
    async with admit_create(env.app, body_20a, env.parent, None) as sel_20a:
        assert isinstance(sel_20a, _AdmitBodyOverride)
        assert "agent_id" not in sel_20a.body.model_fields_set

    # Scenario 20b: user_named=True agent_id=agent_a → named, NOT overridden by J1
    body_20b = ProjectSessionCreateRequest(
        agent_id=agent_a,
        user_named=True,
        host_id=HOST,
        workspace="/opt/work/project",
        work_type="work",
    )
    async with admit_create(env.app, body_20b, env.parent, None) as sel_20b:
        assert sel_20b is not None
        assert not isinstance(sel_20b, _AdmitBodyOverride)
        assert sel_20b.body.agent_id == agent_a

    # Scenario 23a: child create in P keeps session choice pin ("p0" -> agent_a), NOT J1
    r23_child = await create(env, project_id=proj_id, host_id=HOST, workspace="/opt/work/project")
    assert r23_child.status_code == 201, r23_child.text
    assert r23_child.json()["agent_id"] == agent_a
    assert choice_record(env.store.get_conversation(r23_child.json()["id"])) is not None

    # Scenario 23b: independent create with explicit project_id=None → unfiled, J1 does NOT fire
    body_23_null = ProjectSessionCreateRequest(
        project_id=None, host_id=HOST, workspace="/opt/work/project", work_type="work"
    )
    async with admit_create(env.app, body_23_null, env.parent, None) as sel_23_null:
        assert sel_23_null is not None
        assert not isinstance(sel_23_null, _AdmitBodyOverride)
        assert sel_23_null.body.agent_id == agent_a

    # Scenario 21: project session_choice="off", independent orchestrated create without agent_id
    # gets project default agent_b, while explicit agent_id=agent_a keeps agent_a
    projects.update(
        proj_id,
        user_id=None,
        config={
            "calling_defaults": {
                HOST: {
                    "agent_id": agent_b,
                    "harnesses": {"claude-sdk": {"model": "opus-default"}},
                }
            },
            "orchestration": {"session_choice": "off"},
        },
    )

    r21_default = await env.client.post(
        "/v1/sessions/orchestrated",
        headers={**HEADERS, ORCHESTRATION_SOURCE_HEADER: env.parent.id},
        json={"host_id": HOST, "workspace": "/opt/work/project", "project_id": proj_id},
    )
    assert r21_default.status_code == 201, r21_default.text
    assert r21_default.json()["agent_id"] == agent_b
    assert choice_record(env.store.get_conversation(r21_default.json()["id"])) is None

    r21_named = await env.client.post(
        "/v1/sessions/orchestrated",
        headers={**HEADERS, ORCHESTRATION_SOURCE_HEADER: env.parent.id},
        json={
            "agent_id": agent_a,
            "host_id": HOST,
            "workspace": "/opt/work/project",
            "project_id": proj_id,
        },
    )
    assert r21_named.status_code == 201, r21_named.text
    assert r21_named.json()["agent_id"] == agent_a
