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
async def test_admitted_drain_survives_state_restart_and_preview_is_read_only(drain_choice_env):
    import httpx
    from fastapi import FastAPI

    from omnigent.server.routes.session_choice import create_session_choice_router
    from omnigent.server.session_choice import FloorOff, state_for

    env = drain_choice_env
    now = time.time()
    state_for(env.app).exhausted[(RESERVED_USER_LOCAL, "pool-0")] = now + 60
    before = env.prefs.session_choice_snapshot(RESERVED_USER_LOCAL)
    child = await create(env)
    assert child.status_code == 201, child.text
    assert child.json()["session_choice"]["pool"] == "pool-1"
    assert "Drain lock selected" in child.json()["session_choice"]["reason"]
    env.app.state.session_choice_states.clear()
    env.prefs.set_choice_floor_off(RESERVED_USER_LOCAL, "pool-1", FloorOff("manual", {}))

    app = FastAPI()
    app.state = env.app.state
    app.include_router(create_session_choice_router(env.prefs, None), prefix="/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = "/v1/me/session-choice"
        settings = await client.get(url)
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
        preview = await client.post(f"{url}/preview", json=body)
        assert preview.status_code == 200, preview.text
        assert preview.json()["pool"] == "pool-1"
        one_off = await client.post(f"{url}/preview", json={**body, "exclude": ["pool-1"]})
        assert one_off.status_code == 200, one_off.text
        assert one_off.json()["pool"] == "pool-0"
        assert (await client.get(url)).json()["runtime"] == runtime
    assert state_for(env.app).cooldowns == {}
    line = env.app.state.agent_quota_service.session_choice_line(env.parent)
    assert "Draining: test-model (work) until it closes." in line
    assert "Use up: pool-1." in line
    next_child = await create(env)
    assert next_child.status_code == 201, next_child.text
    assert next_child.json()["session_choice"]["pool"] == "pool-1"


@pytest.mark.asyncio
async def test_drain_writes_only_after_admission_and_db_failure_preserves_create(
    drain_choice_env, monkeypatch
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

    with monkeypatch.context() as patch:
        patch.setattr(env.prefs, "apply_drain_lock_delta", write_failure)
        created = await create(env)
    assert created.status_code == 201, created.text
    assert env.store.get_conversation(created.json()["id"]) is not None
    assert env.prefs.session_choice_snapshot(RESERVED_USER_LOCAL).locks == {}


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

    def read_then_save(user):
        snapshot = original(user)
        if not saved:
            saved.append(env.prefs.replace_session_choice(user, changed, snapshot.revision))
            saved.append(
                env.prefs.replace_session_choice(user, snapshot.document, saved[0]["revision"])
            )
        return snapshot

    monkeypatch.setattr(env.prefs, "session_choice_snapshot", read_then_save)
    child = await create(env)
    assert child.status_code == 201, child.text
    assert child.json()["session_choice"]["pool"] == "pool-1"
    assert len(saved) == 2
    after = original(owner)
    assert after.revision == before.revision
    assert after.locks == {}


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


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["krill", "ccs-krill"])
@pytest.mark.parametrize("limit", ["busy", "reserve", "stop"])
async def test_codex_default_route_is_admitted_against_its_actual_tile(
    choice_env, monkeypatch, prefix, limit
):
    from tests.server.test_agent_quota import usage_report

    env = choice_env
    model = f"{prefix}/gpt-small"
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "codex-native",
    )
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_llm_model",
        lambda *a, **kw: model,
    )
    link = {"agent": env.agents[0]["id"], "meter": "codex-pro"}
    env.settings["presets"][0]["links"] = [link]
    env.settings["hosts"][HOST]["work"] = [[{"links": [link]}]]
    env.settings["limits"] = {
        "krill": {
            "maxRunning": 1,
            "reserve": {"mode": "total", "window": "7d", "pct": 15, "stopPct": 5},
        }
    }
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    if limit == "busy":
        first = await create(env)
        assert first.status_code == 201, first.text
    else:
        now = time.time()
        env.app.state.agent_quota_service.report_account(
            RESERVED_USER_LOCAL,
            "krill",
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
    assert (choice["outcome"], choice["pool"], choice["reason"]) == ("hold", "krill", limit)
    assert child["model_override"] == choice["model"] == model
    assert choice_record(env.store.get_conversation(child["id"]))["pool"] == "krill"


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
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "codex-native",
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    catalogs.upsert(HOST, "codex-native", [{"id": "gpt-example"}], time.time())
    env.app.state.host_model_catalog_cache_store = catalogs
    env.settings["limits"] = {
        "codex-pro": {
            "maxRunning": 1,
            "reserve": {"mode": "total", "window": "7d", "pct": 15, "stopPct": 5},
        },
        "krill": {"maxRunning": 1},
    }
    for index, pool in enumerate(("codex-pro", "krill")):
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
    official, krill, held = children
    assert held["session_choice"]["outcome"] == "hold"
    queue = HostLoadQueue(env.store)
    monkeypatch.setattr(queue, "_start", AsyncMock())
    monkeypatch.setattr(env.app.state.host_load_inputs, "deliver_session", AsyncMock())
    assert not await queue.release(env.app, held["id"], HOST)
    env.store.set_session_live_status(official["id"], "idle")
    assert await queue.release(env.app, held["id"], HOST)
    queue._start.assert_awaited_once()
    now = time.time()
    env.app.state.agent_quota_service.report_account(
        RESERVED_USER_LOCAL,
        "codex-pro",
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
    for child in (held, krill):
        env.store.set_runner_id(child["id"], "a" * 32)
        env.store.set_session_live_status(child["id"], "running")
    env.store.touch_runner_liveness(["a" * 32], int(now))
    krill_session = replace(env.store.get_conversation(krill["id"]), reported_model="gpt-example")
    assert tile_of_session(krill_session, settings, readings, {}) is None
    assert stopped_line(env.app, krill_session) is None
    assert stopped_line(env.app, env.store.get_conversation(held["id"])).state == "stop"
    assert stop_targets(
        env.app, RESERVED_USER_LOCAL, settings, readings, {}, {"codex-pro"}, now
    ) == {"codex-pro": (held["id"],)}


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
    for index, pool in enumerate(("codex-pro", "krill")):
        link = {"agent": env.agents[index]["id"], "model": "gpt-6.1-sol", "meter": pool}
        env.settings["presets"][index]["links"] = [link]
        env.settings["hosts"][HOST]["work"][index][0]["links"] = [link]
    env.settings["limits"] = {
        "codex-pro": {
            "maxRunning": 1,
            "reserve": {"mode": "total", "window": "7d", "pct": 15, "stopPct": 5},
        },
        "krill": {"maxRunning": 1},
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
        "codex-pro",
    )
    record = choice_record(env.store.get_conversation(child["id"]))
    assert (record["pool"], record["model"]) == ("codex-pro", "gpt-6.1-sol")
    named = await create(
        env, agent_id=env.agents[1]["id"], user_named=True, model_override="gpt-6.1-sol"
    )
    assert named.status_code == 201, named.text
    assert named.json()["model_override"] == named.json()["session_choice"]["model"]
    assert named.json()["session_choice"]["pool"] == "krill"

    settings = parse_session_choice(env.settings)
    conv = env.store.get_conversation(child["id"])
    now = time.time()
    env.app.state.agent_quota_service.report_account(
        RESERVED_USER_LOCAL,
        "codex-pro",
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
    assert tile_of_session(conv, settings, readings, {}) == "codex-pro"
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
    official = "gpt-example"
    custom = "provider-example/missing-model"
    for index, model in enumerate((custom, official)):
        env.settings["hosts"][HOST]["work"][index][0]["links"][0]["model"] = model
    env.prefs.replace_session_choice(
        RESERVED_USER_LOCAL,
        env.settings,
        env.prefs.session_choice(RESERVED_USER_LOCAL)["revision"],
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    catalogs.upsert(HOST, harness, [{"id": official}], 1)
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
    rows = [{"id": official}, *([{"id": custom}] if available else [])]
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

        catalogs.upsert(HOST, harness, [{"id": official}], 1)
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
        assert legacy.models[env.agents[0]["id"]] == frozenset({official})
        lookup.assert_not_awaited()


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
async def test_provider_quota_failure_blocks_pool_until_reset(
    quota_runner, monkeypatch, event_type
):
    env = quota_runner
    clock = [time.time()]
    monkeypatch.setattr("omnigent.server.session_choice_runtime.time.time", lambda: clock[0])
    first = (await create(env)).json()
    message = "Individual quota reached. Resets in 132h0m26s"
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
    reset = clock[0] + 132 * 3600 + 26
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
@pytest.mark.parametrize("pool,route", [("codex-pro", "official"), ("krill", "krill")])
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
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._resolve_harness_impl",
        lambda *a, **kw: "codex-native",
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    env.app.state.host_model_catalog_cache_store = catalogs
    link = {"agent": agent, "model": model}
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
        assert (choice["outcome"], choice["reason"], choice["model"]) == ("hold", "stop", model)
        assert choice["effectiveLimits"]["reserve.stopPct"]["sourceRuleId"] == "launch"
        assert not await release_choice(env.app, held, HOST)
    else:
        env.store.set_session_live_status(first["id"], "idle")
        assert await release_choice(env.app, held, HOST)
        released = choice_record(env.store.get_conversation(second["id"]))
        assert released is not None
        assert released["model"] == model
        assert released["effectiveLimits"]["maxRunning"]["sourceRuleId"] == "launch"
        assert released["effectiveLimits"]["maxRunning"]["runningCount"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("pool", ["codex-pro", "krill"])
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
    save_rules(env, [{"id": "pinned", "agent": agent, "model": "official/gpt-example"}, rule])
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
    "route,pool_id", [(None, "pool-0"), ("official", "codex-pro"), ("krill", "krill")]
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
