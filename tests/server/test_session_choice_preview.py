"""Exact draft previews share validation and never grant admission or mutate policy."""

import time
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from omnigent.server.session_choice import SLOT_LABEL, state_for
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import create_test_agent
from tests.server.test_agent_quota import usage_report
from tests.server.test_user_preferences import _preferences_app

OWNER = "owner@example.test"
HOST = "1" * 32
URL = "/v1/me/session-choice"


@pytest.fixture
async def preview_env(db_uri, tmp_path):
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(HOST, "sample-host", OWNER)
    app = _preferences_app(
        db_uri, tmp_path, host_store=hosts, permission_store=SqlAlchemyPermissionStore(db_uri)
    )
    catalogs = HostModelCatalogCacheStore(db_uri)
    app.state.host_model_catalog_cache_store = catalogs
    catalogs.upsert(
        HOST,
        "codex-native",
        [{"id": "model-x", "isDefault": True}, {"id": "model-y"}],
        int(time.time()),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Test-User": OWNER},
    ) as client:
        agent = await create_test_agent(
            client,
            name="preview-agent",
            include_llm=False,
            executor={"type": "omnigent", "config": {"harness": "codex-native"}},
        )
        saved = (await client.get(URL)).json()
        draft = {
            "schemaVersion": 2,
            "enabled": True,
            "workTypes": [{"id": "work", "label": "Work"}],
            "limitRules": [{"id": "base", "agent": agent["id"], "maxRunning": 3}],
        }
        body = {
            "mode": "candidate",
            "host_id": HOST,
            "candidate": {"agent_id": agent["id"], "model": "model-x"},
            "settings": draft,
        }
        yield SimpleNamespace(
            app=app,
            client=client,
            agent=agent["id"],
            parent=agent["_session_id"],
            body=body,
            saved=saved,
            drafts=draft,
            catalogs=catalogs,
            hosts=hosts,
        )


@pytest.mark.asyncio
async def test_legacy_notice_keeps_quota_warning_across_provider_rename(preview_env, monkeypatch):
    from omnigent.models.model_reference import ALIASES_NAMESPACE
    from tests.server.test_session_choice import reading

    env = preview_env
    prefs = env.app.state.user_preferences_store
    prefs.patch_namespace(OWNER, ALIASES_NAMESPACE, {"pairs": [["old", "new"]]})
    link = {"agent": env.agent, "model": "old/model-x"}
    settings = {
        "enabled": True,
        "workTypes": [{"id": "work", "label": "Work"}],
        "presets": [{"id": "one", "name": "One", "links": [link]}],
        "hosts": {HOST: {"work": [[{"links": [link]}]]}},
        "limits": {
            "codex:old/model-x": {
                "reserve": {"mode": "total", "window": "7d", "pct": 15, "stopPct": 5}
            }
        },
    }
    prefs.replace_session_choice(OWNER, settings, env.saved["revision"])
    quota = reading("codex:new/model-x", used=96)
    quota.windows[0].resets_at = time.time() + 86400
    monkeypatch.setattr(
        env.app.state.agent_quota_service,
        "pool_readings",
        lambda *args, **kwargs: {"codex:new/model-x": quota},
    )
    query = {"host_id": HOST, "agent_id": env.agent, "model": "new/model-x"}
    response = await env.client.get(URL + "/notice", params=query)
    assert response.status_code == 200, response.text
    assert response.json()["notice"] and response.json()["pool"] == "codex:new/model-x"
    assert response.json()["remaining_pct"] == 4
    assert (
        await env.client.get(URL + "/notice", params={**query, "model": "unconfigured"})
    ).json() == {"notice": None}


@pytest.mark.asyncio
async def test_candidate_outside_tiers_explains_draft_and_preserves_saved_state(preview_env):
    env = preview_env
    state = state_for(env.app)
    state.cooldowns[(OWNER, "unrelated")] = time.time() + 60
    state.counters[(OWNER, HOST, "work", 0, 0)] = 7
    before = deepcopy((state.counters, state.cooldowns))
    response = await env.client.post(URL + "/preview", json=env.body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mode"] == "candidate" and result["admission"] is False
    assert result["candidate"] == {"agent_id": env.agent, "host_id": HOST, "model": "model-x"}
    assert result["configuredInTiers"] is False and result["policyActive"] is False
    cap = result["effectiveLimits"]["maxRunning"]
    assert (cap["value"], cap["sourceRuleId"], cap["runningCount"], cap["reservedCount"]) == (
        3,
        "base",
        0,
        0,
    )
    assert "runningSessionIds" not in cap
    assert result["savedRevision"] == env.saved["revision"] and result["draftFingerprint"]
    assert "outcome" not in result
    assert (await env.client.get(URL)).json() == env.saved
    assert (state.counters, state.cooldowns) == before
    default = deepcopy(env.body)
    default["candidate"].pop("model")
    result = (await env.client.post(URL + "/preview", json=default)).json()
    assert result["candidate"]["model"] == "model-x" and result["requestedModel"] is None
    env.hosts.set_offline(HOST)
    offline = await env.client.post(URL + "/preview", json=env.body)
    assert offline.status_code == 200, offline.text
    assert offline.json()["effectiveLimits"]["maxRunning"]["value"] == 3
    assert offline.json()["check"] == {"state": "blocked", "reason": "host_offline"}


@pytest.mark.asyncio
async def test_scoped_count_and_rule_order_are_explained_without_pool_count_substitution(
    preview_env,
):
    env = preview_env
    rows = [
        env.app.state.conversation_store.create_conversation(
            agent_id=env.agent,
            parent_conversation_id=env.parent,
            kind="sub_agent",
            host_id=host,
            workspace="/opt/work/project",
            model_override=model,
            labels={SLOT_LABEL: f"admitted@{host}@shared@{time.time()}"},
        )
        for host, model in [(HOST, "model-x"), (HOST, "model-y"), ("2" * 32, "model-x")]
    ]
    rules = [
        {"id": "host", "agent": env.agent, "host": HOST, "maxRunning": 2},
        {"id": "model", "agent": env.agent, "model": "model-x", "maxRunning": 4},
    ]
    env.body.update(include_running=True)
    env.body["settings"]["limitRules"] = rules
    first = (await env.client.post(URL + "/preview", json=env.body)).json()
    cap = first["effectiveLimits"]["maxRunning"]
    assert cap["sourceRuleId"] == "host" and cap["runningCount"] == cap["reservedCount"] == 2
    assert set(cap["runningSessionIds"]) == {rows[0].id, rows[1].id}
    assert first["check"] == {"state": "blocked", "reason": "busy"}
    env.body["settings"]["limitRules"] = list(reversed(rules))
    second = (await env.client.post(URL + "/preview", json=env.body)).json()
    cap = second["effectiveLimits"]["maxRunning"]
    assert cap["value"] == 4 and cap["sourceRuleId"] == "model"
    assert set(cap["runningSessionIds"]) == {rows[0].id, rows[2].id}
    assert first["draftFingerprint"] != second["draftFingerprint"]


@pytest.mark.asyncio
async def test_candidate_errors_and_owner_boundaries(preview_env):
    env = preview_env
    for change, status in [
        ({"pin": None}, 422),
        ({"conversion": False}, 422),
        ({"exclude": []}, 422),
        ({"settings": None}, 422),
        ({"candidate": None}, 422),
        ({"caller_id": "missing"}, 404),
        ({"host_id": "2" * 32}, 404),
        ({"candidate": {"agent_id": "missing", "model": "model-x"}}, 404),
        ({"candidate": {"agent_id": env.agent, "model": "not-in-catalog"}}, 200),
    ]:
        response = await env.client.post(URL + "/preview", json={**env.body, **change})
        assert response.status_code == status, (change, response.text)
    env.hosts.upsert_on_connect("2" * 32, "other-host", "other@example.test")
    response = await env.client.post(URL + "/preview", json={**env.body, "host_id": "2" * 32})
    assert response.status_code == 404
    missing_default = deepcopy(env.body)
    missing_default["candidate"].pop("model")
    env.catalogs.upsert(HOST, "codex-native", [{"id": "model-x"}], int(time.time()))
    response = await env.client.post(URL + "/preview", json=missing_default)
    assert (
        response.status_code == 409 and response.json()["detail"]["code"] == "candidate_unresolved"
    )
    assert (await env.client.get(URL)).json() == env.saved

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app),
        base_url="http://test",
        headers={"X-Test-User": "other@example.test"},
    ) as other:
        foreign = await create_test_agent(
            other,
            name="private-agent",
            include_llm=False,
            executor={"type": "omnigent", "config": {"harness": "codex-native"}},
        )
    response = await env.client.post(
        URL + "/preview",
        json={**env.body, "candidate": {"agent_id": foreign["id"], "model": "model-x"}},
    )
    assert response.status_code == 404
    response = await env.client.post(
        URL + "/preview", json={**env.body, "caller_id": foreign["_session_id"]}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_notice_lines_floor_and_daily_window_metadata_share_exact_scope(preview_env):
    env = preview_env
    draft = deepcopy(env.body["settings"])
    draft["presets"] = [
        {
            "id": "sample",
            "name": "Sample",
            "links": [{"agent": env.agent, "model": "model-x", "meter": "shared"}],
        }
    ]
    draft["limitRules"] = [
        {
            "id": "base",
            "agent": env.agent,
            "reserve": {"mode": "total", "window": "weekly", "pct": 10, "stopPct": 5},
        }
    ]
    draft["limits"] = {"shared": {"reserve": {"mode": "total", "window": "weekly", "pct": 10}}}
    now = time.time()
    env.app.state.agent_quota_service.report_account(
        OWNER,
        "shared",
        usage_report(
            account_product="codex",
            observed_at=now,
            windows=[
                {
                    "id": "weekly",
                    "kind": "weekly",
                    "percent_used": 96,
                    "window_minutes": 10080,
                    "resets_at": now + 86400,
                }
            ],
        ),
        now=now,
    )
    saved = await env.client.put(
        URL, json={"settings": draft, "expected_revision": env.saved["revision"]}
    )
    assert saved.status_code == 200, saved.text
    query = {"host_id": HOST, "agent_id": env.agent, "model": "model-x"}
    notice = (await env.client.get(URL + "/notice", params=query)).json()
    assert (
        notice["notice"] and notice["effectiveLimits"]["reserve.stopPct"]["sourceRuleId"] == "base"
    )
    assert ": stop " in notice["notice"]
    lines = (await env.client.get("/v1/me/usage/lines", params=query)).json()
    assert lines["limitsVaryByCandidate"] is True
    assert lines["scoped"]["lines"][-1]["state"] == "stop"
    assert (
        await env.client.post(URL + "/floor", json={"tile": "shared", "off": True, "keep": True})
    ).status_code == 200
    notice = (await env.client.get(URL + "/notice", params=query)).json()
    assert notice["notice"] is None and notice["lines"][-1]["released"]
    # Known short windows reject daily mode; unknown/offline metadata is not fabricated.
    daily = deepcopy(draft)
    daily["limitRules"][0]["reserve"].update(mode="daily", window="short")
    env.app.state.agent_quota_service.report_account(
        OWNER,
        "shared",
        usage_report(
            account_product="codex",
            observed_at=now + 1,
            windows=[
                {
                    "id": "short",
                    "kind": "session",
                    "percent_used": 10,
                    "window_minutes": 60,
                    "resets_at": now + 3600,
                }
            ],
        ),
        now=now + 1,
    )
    # Explicit link metadata still counts when the model catalog is unavailable.
    env.app.state.host_model_catalog_cache_store = None
    response = await env.client.put(
        URL, json={"settings": daily, "expected_revision": saved.json()["revision"]}
    )
    assert (
        response.status_code == 422
        and response.json()["detail"]["code"] == "daily_window_unavailable"
    ), response.text
    assert (await env.client.get(URL)).json()["revision"] == saved.json()["revision"]
    unknown = deepcopy(daily)
    unknown["presets"][0]["links"][0]["meter"] = "not-reported"
    response = await env.client.put(
        URL, json={"settings": unknown, "expected_revision": saved.json()["revision"]}
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_conversion_reports_unproved_shared_ownership_and_never_clears_legacy(preview_env):
    env = preview_env
    legacy = {
        "enabled": False,
        "workTypes": [{"id": "work", "label": "Work"}],
        "limits": {"shared": {"maxRunning": 2}, "missing": {"maxRunning": 1}},
        "presets": [
            {
                "id": "sample",
                "name": "Sample",
                "links": [
                    {"agent": env.agent, "model": "model-x", "meter": "shared"},
                    {"agent": env.agent, "model": "model-y", "meter": "shared"},
                ],
            }
        ],
    }
    saved = (
        await env.client.put(
            URL, json={"settings": legacy, "expected_revision": env.saved["revision"]}
        )
    ).json()
    body = {"host_id": HOST, "conversion": True}
    response = await env.client.post(URL + "/preview", json=body)
    assert response.status_code == 200, response.text
    plan = response.json()["legacyConversion"]
    assert plan["sourceRevision"] == saved["revision"]
    assert not plan["convertible"] and not plan["replacedPaths"] and not plan["newRules"]
    assert {tuple(row["path"]): row["reason"] for row in plan["unmapped"]} == {
        ("limits", "shared"): "shared_pool",
        ("limits", "missing"): "pool_mapping_unavailable",
    }
    forged = deepcopy(saved["settings"])
    forged.update(
        schemaVersion=2, limitRules=[{"id": "converted", "agent": env.agent, "maxRunning": 2}]
    )
    del forged["limits"]["shared"]
    response = await env.client.put(
        URL,
        json={
            "settings": forged,
            "expected_revision": saved["revision"],
            "conversion_proof": {
                "source_revision": saved["revision"],
                "mapping_fingerprint": plan["mappingFingerprint"],
                "replaced_paths": [["limits", "shared"]],
                "new_rule_ids": ["converted"],
            },
        },
    )
    assert response.status_code == 409, response.text
    assert (await env.client.get(URL)).json()["settings"] == saved["settings"]
    assert (await env.client.get(URL)).json()["revision"] == saved["revision"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["model-x", None])
async def test_conversion_keeps_pool_when_selector_can_reach_another_meter(preview_env, model):
    env = preview_env
    pool = "codex:model-x"
    legacy = {
        "workTypes": [{"id": "work", "label": "Work"}],
        "presets": [
            {"id": "exact", "name": "Exact", "links": [{"agent": env.agent, "model": "model-x"}]}
        ],
        "hosts": {
            HOST: {
                "work": [[{"links": [{"agent": env.agent, "model": model, "meter": "shared"}]}]]
            }
        },
        "limits": {pool: {"reserve": {"mode": "total", "window": "weekly", "stopPct": 5}}},
    }
    saved = await env.client.put(
        URL, json={"settings": legacy, "expected_revision": env.saved["revision"]}
    )
    assert saved.status_code == 200, saved.text
    # The host catalog resolves the omitted model to model-x; there are no running sessions.
    assert env.catalogs.list([HOST])[0]["models"][0]["isDefault"] is True
    result = await env.client.post(URL + "/preview", json={"host_id": HOST, "conversion": True})
    assert result.status_code == 200, result.text
    plan = result.json()["legacyConversion"]
    assert not plan["convertible"] and not plan["newRules"] and not plan["replacedPaths"]
    assert plan["unmapped"] == [
        {"path": ["limits", pool], "reason": "selector_pool_scope_unproven"}
    ]
    current = (await env.client.get(URL)).json()
    assert current["settings"] == saved.json()["settings"]
    assert current["revision"] == saved.json()["revision"]


@pytest.mark.asyncio
async def test_conversion_keeps_ignored_generated_pool_cap_at_selection_boundary(preview_env):
    env = preview_env
    pool = "codex:model-x"
    link = {"agent": env.agent, "model": "model-x"}
    legacy = {
        "enabled": True,
        "workTypes": [{"id": "work", "label": "Work"}],
        "presets": [{"id": "exact", "name": "Exact", "links": [link]}],
        "hosts": {HOST: {"work": [[{"links": [link]}]]}},
        "limits": {pool: {"maxRunning": 2}},
    }
    saved = await env.client.put(
        URL, json={"settings": legacy, "expected_revision": env.saved["revision"]}
    )
    assert saved.status_code == 200, saved.text
    for _ in range(2):
        env.app.state.conversation_store.create_conversation(
            agent_id=env.agent,
            parent_conversation_id=env.parent,
            kind="sub_agent",
            host_id=HOST,
            workspace="/opt/work/project",
            model_override="model-x",
            labels={SLOT_LABEL: f"admitted@{HOST}@{pool}@{time.time()}"},
        )
    before = await env.client.post(URL + "/preview", json={"host_id": HOST})
    assert before.status_code == 200 and before.json()["outcome"] == "pick", before.text
    result = await env.client.post(URL + "/preview", json={"host_id": HOST, "conversion": True})
    assert result.status_code == 200, result.text
    plan = result.json()["legacyConversion"]
    assert not plan["convertible"]
    assert plan["unmapped"] == [
        {"path": ["limits", pool], "reason": "generated_pool_cap_not_equivalent"}
    ]
    after = await env.client.post(URL + "/preview", json={"host_id": HOST})
    assert after.status_code == 200 and after.json() == before.json()
    current = (await env.client.get(URL)).json()
    assert current["settings"] == saved.json()["settings"]
    assert current["revision"] == saved.json()["revision"]


@pytest.mark.asyncio
async def test_selection_preview_keeps_get_contract_and_adds_effective_source(preview_env):
    env = preview_env
    draft = deepcopy(env.body["settings"])
    draft["hosts"] = {HOST: {"work": [[{"links": [{"agent": env.agent, "model": "model-x"}]}]]}}
    saved = await env.client.put(
        URL, json={"settings": draft, "expected_revision": env.saved["revision"]}
    )
    assert saved.status_code == 200, saved.text
    post = await env.client.post(URL + "/preview", json={"host_id": HOST})
    get = await env.client.get(URL + "/preview", params={"host_id": HOST})
    assert post.status_code == get.status_code == 200
    assert post.json() == get.json()
    assert get.json()["mode"] == "selection" and get.json()["outcome"] == "pick"
    assert get.json()["effectiveLimits"]["maxRunning"]["sourceRuleId"] == "base"
    assert state_for(env.app).counters == {} and state_for(env.app).cooldowns == {}
    session = env.app.state.conversation_store.get_conversation(env.parent)
    from dataclasses import replace

    summary = env.app.state.agent_quota_service.session_choice_line(
        replace(session, host_id=HOST, model_override="model-x")
    )
    assert '"sourceRuleId":"base"' in summary and '"value":3' in summary


@pytest.mark.asyncio
async def test_usage_lines_keeps_one_saved_snapshot_during_concurrent_save(
    preview_env, monkeypatch
):
    env = preview_env
    prefs = env.app.state.user_preferences_store
    draft = deepcopy(env.body["settings"])
    draft["limits"] = {"shared": {"maxRunning": 2}}
    old = prefs.replace_session_choice(OWNER, draft, env.saved["revision"])
    original = prefs.get
    changed_once = []

    def read_then_save(owner):
        snapshot = original(owner)
        if not changed_once:
            changed_once.append(True)
            changed = deepcopy(draft)
            changed["limits"] = {"new-pool": {"maxRunning": 4}}
            changed["limitRules"][0].update(id="new", maxRunning=7)
            prefs.replace_session_choice(owner, changed, old["revision"])
        return snapshot

    monkeypatch.setattr(prefs, "get", read_then_save)
    response = await env.client.get(
        "/v1/me/usage/lines",
        params={"host_id": HOST, "agent_id": env.agent, "model": "model-x"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert "shared" in result["tiles"] and "new-pool" not in result["tiles"]
    assert result["savedRevision"] == result["scoped"]["savedRevision"] == old["revision"]
    cap = result["scoped"]["effectiveLimits"]["maxRunning"]
    assert (cap["value"], cap["sourceRuleId"]) == (3, "base")
    assert prefs.session_choice(OWNER)["revision"] != old["revision"]


@pytest.mark.asyncio
async def test_summary_uses_side_chat_routing_host_and_reports_unknown(preview_env):
    from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY

    env = preview_env
    prefs = env.app.state.user_preferences_store
    draft = deepcopy(env.body["settings"])
    draft["hosts"] = {HOST: {"work": [[{"links": [{"agent": env.agent, "model": "model-x"}]}]]}}
    draft["limitRules"] = [{"id": "host", "agent": env.agent, "host": HOST, "maxRunning": 2}]
    prefs.replace_session_choice(OWNER, draft, env.saved["revision"])
    store = env.app.state.conversation_store
    source = store.create_conversation(
        agent_id=env.agent, host_id=HOST, workspace="/opt/work/project"
    )
    env.app.state.permission_store.grant(OWNER, source.id, 4)
    side = store.create_conversation(
        agent_id=env.agent,
        model_override="model-x",
        labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source.id},
    )
    for row in (source, side):
        store.set_runner_id(row.id, "f" * 32)
    side = store.get_conversation(side.id)
    unknown = store.create_conversation(agent_id=env.agent, model_override="model-x")
    for row in (side, unknown):
        env.app.state.permission_store.grant(OWNER, row.id, 4)
    service = env.app.state.agent_quota_service
    summary = service.session_choice_line(side)
    assert '"host":"' + HOST + '"' in summary
    assert '"sourceRuleId":"host"' in summary and '"value":2' in summary
    unresolved = service.session_choice_line(unknown)
    assert "host identity unresolved" in unresolved.lower()
    assert '"sourceRuleId":"host"' not in unresolved


@pytest.mark.asyncio
async def test_conversion_proves_exact_pool_and_undo_preserves_later_rules(preview_env):
    env = preview_env
    pool = "codex:model-x"
    legacy = {
        "enabled": False,
        "workTypes": [{"id": "work", "label": "Work"}],
        "limits": {
            pool: {"reserve": {"mode": "total", "window": "weekly", "pct": 10, "stopPct": 5}},
            "missing": {"maxRunning": 1},
        },
        "presets": [
            {
                "id": "exact",
                "name": "Exact",
                "links": [
                    {"agent": env.agent, "model": "model-x"},
                    {"agent": env.agent, "model": "model-y", "meter": "shared"},
                ],
            }
        ],
    }
    saved = (
        await env.client.put(
            URL, json={"settings": legacy, "expected_revision": env.saved["revision"]}
        )
    ).json()
    store = env.app.state.conversation_store
    running = store.create_conversation(
        agent_id=env.agent,
        parent_conversation_id=env.parent,
        kind="sub_agent",
        host_id=HOST,
        workspace="/opt/work/project",
        model_override="model-x",
        labels={SLOT_LABEL: f"admitted@{HOST}@{pool}@{time.time()}"},
    )
    response = await env.client.post(URL + "/preview", json={"host_id": HOST, "conversion": True})
    assert response.status_code == 200, response.text
    plan = response.json()["legacyConversion"]
    assert plan["convertible"] is True
    assert plan["replacedPaths"] == [["limits", pool]]
    assert plan["newRules"][0]["agent"] == env.agent
    assert plan["newRules"][0]["model"] == "model-x"
    assert plan["newRules"][0]["reserve"] == {
        "mode": "total",
        "window": "weekly",
        "pct": 10,
        "stopPct": 5,
    }
    assert plan["newRules"][0].get("maxRunning") is None
    from omnigent.server.session_choice import LimitScope, parse_session_choice
    from omnigent.server.session_choice_runtime import running_pools, session_products

    settings = parse_session_choice(saved["settings"])
    counts = running_pools(
        env.app, OWNER, settings, session_products(env.app, settings), time.time()
    )
    assert counts.scoped(LimitScope(agent=env.agent, model="model-x")) == (running.id,)
    assert counts.get(pool) == (running.id,)
    proof = {
        "source_revision": plan["sourceRevision"],
        "mapping_fingerprint": plan["mappingFingerprint"],
        "replaced_paths": plan["replacedPaths"],
        "new_rule_ids": plan["newRuleIds"],
    }
    request = {
        "settings": plan["settings"],
        "expected_revision": saved["revision"],
        "conversion_proof": proof,
    }
    converted = await env.client.put(URL, json=request)
    assert converted.status_code == 200, converted.text
    assert converted.json()["settings"]["limits"] == {
        "missing": {"reserveExempt": [], "maxRunning": 1}
    }
    repeat = await env.client.put(URL, json=request)
    assert repeat.status_code == 409
    current = converted.json()
    later = deepcopy(current["settings"])
    later["limitRules"].append(
        {"id": "independent", "agent": env.agent, "model": "model-y", "maxRunning": 4}
    )
    latest = await env.client.put(
        URL, json={"settings": later, "expected_revision": current["revision"]}
    )
    assert latest.status_code == 200, latest.text
    undo = {**saved["settings"], "schemaVersion": 2, "limitRules": [later["limitRules"][-1]]}
    stale = await env.client.put(
        URL, json={"settings": undo, "expected_revision": current["revision"]}
    )
    assert stale.status_code == 409
    restored = await env.client.put(
        URL, json={"settings": undo, "expected_revision": latest.json()["revision"]}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["settings"]["schemaVersion"] == 2
    assert restored.json()["settings"]["limits"] == saved["settings"]["limits"]
    assert restored.json()["settings"]["limitRules"] == [later["limitRules"][-1]]

    next_plan = (
        await env.client.post(URL + "/preview", json={"host_id": HOST, "conversion": True})
    ).json()["legacyConversion"]
    assert next_plan["convertible"]
    await create_test_agent(
        env.client,
        name="another-native-agent",
        include_llm=False,
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
    )
    stale_mapping = await env.client.put(
        URL,
        json={
            "settings": next_plan["settings"],
            "expected_revision": restored.json()["revision"],
            "conversion_proof": {
                "source_revision": next_plan["sourceRevision"],
                "mapping_fingerprint": next_plan["mappingFingerprint"],
                "replaced_paths": next_plan["replacedPaths"],
                "new_rule_ids": next_plan["newRuleIds"],
            },
        },
    )
    assert stale_mapping.status_code == 409, stale_mapping.text
    assert (await env.client.get(URL)).json()["revision"] == restored.json()["revision"]
