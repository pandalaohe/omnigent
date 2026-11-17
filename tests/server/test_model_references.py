"""Settings repair preserves ownership, concurrent edits, and untouched auth."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from omnigent.models.model_reference import ALIASES_NAMESPACE
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.custom_agent_bundles import project_members
from omnigent.server.custom_agents_store import CustomAgentsStore
from omnigent.server.model_references import check_model_references_sync
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.server.helpers import build_agent_bundle
from tests.server.test_user_preferences import _preferences_app

OWNER = "owner@example.test"
HOST = "1" * 32
URL = "/v1/me/model-references"


@pytest.fixture
async def references_env(db_uri, tmp_path):
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(HOST, "example-host", OWNER)
    catalogs = HostModelCatalogCacheStore(db_uri)
    catalogs.upsert(HOST, "codex", [{"id": "new/x"}, {"id": "new/y"}], 1)
    projects = SqlAlchemyProjectStore(db_uri)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    custom = CustomAgentsStore(db_uri)
    app = _preferences_app(
        db_uri,
        tmp_path,
        host_store=hosts,
        host_model_catalog_cache_store=catalogs,
        project_store=projects,
        scheduled_task_store=tasks,
        custom_agents_store=custom,
    )
    app.state.host_registry = SimpleNamespace(get=lambda host: object() if host == HOST else None)
    prefs = app.state.user_preferences_store
    prefs.patch_namespace(
        OWNER, "calling_defaults", {HOST: {"codex": {"model": "old/x", "effort": "high"}}}
    )
    agent_id = uuid4().hex
    bundle = build_agent_bundle(
        name="example-agent",
        include_llm=False,
        executor={"type": "omnigent", "model": "old/x", "config": {"harness": "codex"}},
    )
    location = bundle_location(agent_id, bundle)
    app.state.artifact_store.put(location, bundle)
    app.state.agent_store.create_user_agent(agent_id, "example-agent", location, OWNER)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Test-User": OWNER},
    ) as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            prefs=prefs,
            hosts=hosts,
            catalogs=catalogs,
            projects=projects,
            tasks=tasks,
            custom=custom,
            agent=agent_id,
        )


async def test_check_warns_per_host_and_ignores_failed_or_disconnected_catalogs(references_env):
    env = references_env
    result = (await env.client.get(URL)).json()
    assert {row["outcome"] for row in result["rows"]} == {"substituted"}
    assert {row["resolved"] for row in result["rows"]} == {"new/x"}
    env.catalogs.upsert(HOST, "codex", [{"id": "unrelated"}], 2)
    result = (await env.client.get(URL)).json()
    assert {row["outcome"] for row in result["rows"]} == {"missing"}
    env.catalogs.mark_error(HOST, "codex", "probe failed")
    assert (await env.client.get(URL)).json()["rows"] == []
    env.catalogs.upsert(HOST, "codex", [{"id": "new/x"}], 4)
    env.app.state.host_registry = SimpleNamespace(get=lambda _: None)
    assert (await env.client.get(URL)).json()["rows"] == []


async def test_provider_replace_covers_settings_stores_and_is_idempotent(references_env):
    env = references_env
    project = env.projects.create(
        uuid4().hex,
        "Project",
        OWNER,
        {
            "agent_id": env.agent,
            "model": "old/x",
            "calling_defaults": {HOST: {"harnesses": {"codex": {"model": "old/x"}}}},
            "unrelated": "keep",
        },
    )
    task = env.tasks.create(
        uuid4().hex,
        "Task",
        "run",
        "FREQ=DAILY",
        OWNER,
        env.agent,
        "UTC",
        host_id=HOST,
        model_override="old/x",
    )
    choice = {
        "schemaVersion": 2,
        "workTypes": [{"id": "work", "label": "Work"}],
        "presets": [
            {"id": "one", "name": "One", "links": [{"agent": env.agent, "model": "old/x"}]}
        ],
        "hosts": {
            HOST: {
                "work": [[{"preset": "one", "links": [{"agent": env.agent, "model": "old/x"}]}]]
            }
        },
        "limits": {"codex:old/x": {"maxRunning": 1}},
        "limitRules": [{"id": "cap", "agent": env.agent, "model": "old/x", "maxRunning": 1}],
    }
    env.prefs.replace_session_choice(OWNER, choice, env.prefs.session_choice(OWNER)["revision"])
    env.prefs.patch_namespace(
        OWNER,
        "session_collab",
        {"quotaPeriodRules": [{"agent": env.agent, "model": "old/x", "period": "7d"}]},
    )
    custom_id = "ca_" + uuid4().hex
    joint_bundle = build_agent_bundle(
        name="example-joint",
        include_llm=False,
        executor={"type": "omnigent", "model": "old/x", "config": {"harness": "codex"}},
        sub_agents=[
            {
                "name": "worker",
                "executor": {"type": "omnigent", "model": "old/y", "config": {"harness": "codex"}},
            }
        ],
    )
    location = bundle_location(custom_id, joint_bundle)
    env.app.state.artifact_store.put(location, joint_bundle)
    spec = validate_agent_bundle(joint_bundle)
    env.custom.create(
        OWNER,
        {
            "id": custom_id,
            "name": spec.name,
            "harness": "codex",
            "model": spec.executor.model,
            "members": [{**member, "host_id": HOST} for member in project_members(spec)],
            "bundle_location": location,
        },
    )
    response = await env.client.post(
        URL + "/replace", json={"mode": "provider", "from": "old", "to": "new"}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["complete"] and result["count"] == 13
    assert result["aliases"] == "added" and result["remaining"] == 0
    assert env.projects.get(project.id, user_id=OWNER).config == {
        "agent_id": env.agent,
        "model": "new/x",
        "calling_defaults": {HOST: {"harnesses": {"codex": {"model": "new/x"}}}},
        "unrelated": "keep",
    }
    assert env.tasks.get(task.id).model_override == "new/x"
    custom = env.custom.get(OWNER, custom_id)
    assert [member["model"] for member in custom["members"]] == ["new/x", "new/y"]
    assert all(member["host_id"] == HOST for member in custom["members"])
    custom_spec = validate_agent_bundle(
        env.app.state.artifact_store.get(custom["bundle_location"])
    )
    assert (custom_spec.executor.model, custom_spec.sub_agents[0].executor.model) == (
        "new/x",
        "new/y",
    )
    assert (
        env.prefs.get(OWNER)["settings"]["session_collab"]["quotaPeriodRules"][0]["model"]
        == "new/x"
    )
    settings = env.prefs.session_choice(OWNER)["settings"]
    assert "codex:new/x" in settings["limits"] and "codex:old/x" not in settings["limits"]
    assert settings["limitRules"][0]["model"] == "new/x"
    agent = env.app.state.agent_store.get(env.agent)
    assert (
        validate_agent_bundle(
            env.app.state.artifact_store.get(agent.bundle_location)
        ).executor.model
        == "new/x"
    )
    assert env.prefs.get(OWNER)["settings"]["calling_defaults"][HOST]["codex"] == {
        "model": "new/x",
        "effort": "high",
    }
    repeat = (
        await env.client.post(
            URL + "/replace", json={"mode": "provider", "from": "old", "to": "new"}
        )
    ).json()
    assert repeat["complete"] and repeat["count"] == 0 and repeat["aliases"] == "present"
    assert (await env.client.get(URL)).json()["host_config_manual"]


async def test_unoffered_target_and_invalid_pair_write_nothing(references_env):
    env = references_env
    before = deepcopy(env.prefs.get(OWNER))
    response = await env.client.post(
        URL + "/replace", json={"mode": "model", "from": "old/x", "to": "absent/z"}
    )
    assert response.status_code == 400
    assert env.prefs.get(OWNER) == before
    response = await env.client.post(
        URL + "/replace", json={"mode": "provider", "from": "old", "to": "invalid/provider"}
    )
    assert response.status_code == 400
    assert env.prefs.get(OWNER) == before


async def test_namespace_replacement_preserves_sibling_edit_and_reports_model_conflict(
    references_env, monkeypatch
):
    env = references_env
    original = env.prefs.transform_namespace

    def sibling_edit(owner, namespace, transform, **kwargs):
        if namespace == "calling_defaults":
            env.prefs.patch_namespace(
                owner, namespace, {HOST: {"codex": {"model": "old/x", "effort": "low"}}}
            )
        return original(owner, namespace, transform, **kwargs)

    monkeypatch.setattr(env.prefs, "transform_namespace", sibling_edit)
    result = (
        await env.client.post(
            URL + "/replace", json={"mode": "model", "from": "old/x", "to": "new/x"}
        )
    ).json()
    assert result["complete"]
    assert env.prefs.get(OWNER)["settings"]["calling_defaults"][HOST]["codex"] == {
        "model": "new/x",
        "effort": "low",
    }

    env.prefs.patch_namespace(OWNER, "calling_defaults", {HOST: {"codex": {"model": "old/x"}}})

    def model_edit(owner, namespace, transform, **kwargs):
        if namespace == "calling_defaults":
            env.prefs.patch_namespace(owner, namespace, {HOST: {"codex": {"model": "old/y"}}})
        return original(owner, namespace, transform, **kwargs)

    monkeypatch.setattr(env.prefs, "transform_namespace", model_edit)
    result = (
        await env.client.post(
            URL + "/replace", json={"mode": "provider", "from": "old", "to": "new"}
        )
    ).json()
    assert not result["complete"] and result["conflicts"] and result["remaining"] == 1
    assert env.prefs.get(OWNER)["settings"]["calling_defaults"][HOST]["codex"]["model"] == "old/y"


async def test_limit_collision_is_reported_and_not_merged(references_env):
    env = references_env
    choice = {
        "presets": [
            {"id": "one", "name": "One", "links": [{"agent": env.agent, "model": "old/x"}]}
        ],
        "limits": {"codex:old/x": {"maxRunning": 1}, "codex:new/x": {"maxRunning": 2}},
    }
    env.prefs.replace_session_choice(OWNER, choice, env.prefs.session_choice(OWNER)["revision"])
    before_limits = env.prefs.session_choice(OWNER)["settings"]["limits"]
    env.prefs.patch_namespace(OWNER, ALIASES_NAMESPACE, {"pairs": [["old", "new"]]})
    checked = check_model_references_sync(env.app, OWNER, user_id=OWNER)
    assert any(row["outcome"] == "collision" for row in checked["rows"])
    result = (
        await env.client.post(
            URL + "/replace", json={"mode": "provider", "from": "old", "to": "new"}
        )
    ).json()
    assert not result["complete"] and result["conflicts"]
    assert env.prefs.session_choice(OWNER)["settings"]["limits"] == before_limits


async def test_owned_references_only_and_failed_alias_write_is_incomplete(
    references_env, monkeypatch
):
    env = references_env
    env.prefs.patch_namespace(
        "other@example.test", "calling_defaults", {HOST: {"codex": {"model": "old/x"}}}
    )
    original = env.prefs.transform_namespace

    def alias_failure(owner, namespace, transform, **kwargs):
        if namespace == ALIASES_NAMESPACE:
            raise RuntimeError("unavailable")
        return original(owner, namespace, transform, **kwargs)

    monkeypatch.setattr(env.prefs, "transform_namespace", alias_failure)
    result = (
        await env.client.post(
            URL + "/replace", json={"mode": "provider", "from": "old", "to": "new"}
        )
    ).json()
    assert not result["complete"] and result["aliases"] == "failed" and result["count"] == 2
    assert (
        env.prefs.get("other@example.test")["settings"]["calling_defaults"][HOST]["codex"]["model"]
        == "old/x"
    )


async def test_alias_edit_checks_its_revision_before_writing(references_env):
    env = references_env
    snapshot = (await env.client.get(URL)).json()
    body = {"pairs": [["old", "new"]], "expected_revision": snapshot["aliases_revision"]}
    assert (await env.client.put(URL + "/aliases", json=body)).status_code == 200
    stale = await env.client.put(URL + "/aliases", json={**body, "pairs": [["old", "other"]]})
    assert stale.status_code == 409
    assert env.prefs.get(OWNER)["settings"][ALIASES_NAMESPACE] == {"pairs": [["old", "new"]]}


async def test_bare_gateway_model_tokens_are_listed_for_manual_review(references_env):
    from tests._helpers.session import bundle_files

    env = references_env
    env.prefs.patch_namespace(OWNER, ALIASES_NAMESPACE, {"pairs": [["old", "new"]]})
    bundle = bundle_files(
        {
            "config.yaml": (
                b"spec_version: 1\nname: gateway-models\n"
                b"llm:\n  model: old-model\n  fallback_models: [old-backup, unrelated]\n"
                b"executor:\n  type: omnigent\n  model: new/x\n  config: {harness: codex}\n"
            )
        }
    )
    agent_id = uuid4().hex
    location = bundle_location(agent_id, bundle)
    env.app.state.artifact_store.put(location, bundle)
    env.app.state.agent_store.create_user_agent(agent_id, "gateway-models", location, OWNER)
    response = await env.client.get(URL)
    assert response.status_code == 200, response.text
    rows = [row for row in response.json()["rows"] if row["entity_id"] == agent_id]
    assert {row["saved"] for row in rows} == {"old-model", "old-backup"}
    assert all(row["manual"] and row["kind"] == "manual_llm" for row in rows)
    replaced = await env.client.post(
        URL + "/replace", json={"mode": "provider", "from": "old", "to": "new"}
    )
    assert replaced.status_code == 200, replaced.text
    saved = env.app.state.agent_store.get(agent_id)
    assert saved.bundle_location == location
    assert env.app.state.artifact_store.get(location) == bundle


async def test_legacy_and_explicit_create_values_resolve_and_missing_is_unknown(references_env):
    from omnigent.server.routes._session_create_validation import resolve_create_calling_stores

    env = references_env
    env.prefs.patch_namespace(OWNER, ALIASES_NAMESPACE, {"pairs": [["old", "new"]]})
    project = env.projects.create(
        uuid4().hex, "Legacy", OWNER, {"agent_id": env.agent, "model": "old/x"}
    )
    for explicit, fields, source in [
        ({}, set(), "project_legacy"),
        (
            {"agent_id": env.agent, "model_override": "old/x"},
            {"agent_id", "model_override"},
            "explicit",
        ),
    ]:
        result = await resolve_create_calling_stores(
            user_id=OWNER,
            project=project,
            host_id=HOST,
            explicit=explicit,
            explicit_fields=fields,
            path_label="test",
            host_store=env.hosts,
            agent_store=env.app.state.agent_store,
            agent_cache=env.app.state.agent_cache,
            preferences_store=env.prefs,
            catalog_store=env.catalogs,
        )
        assert result.model == "new/x" and result.sources["model"] == source
        assert result.model_resolution.row_id == "new/x"
        assert result.notes[0]["saved"] == "old/x"
    env.catalogs.upsert(HOST, "codex", [{"id": "unrelated"}], 2)
    warnings = []
    missing = await resolve_create_calling_stores(
        user_id=OWNER,
        project=project,
        host_id=HOST,
        explicit={},
        explicit_fields=set(),
        path_label="test",
        host_store=env.hosts,
        agent_store=env.app.state.agent_store,
        agent_cache=env.app.state.agent_cache,
        preferences_store=env.prefs,
        catalog_store=env.catalogs,
        warnings=warnings,
    )
    assert missing.model == "old/x" and missing.notes[0]["outcome"] == "missing"
    assert any("trying it as unknown" in warning for warning in warnings)


async def test_resume_resolves_persists_and_labels_the_override(references_env, db_uri):
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.server.model_resolution import notes_from_labels, resolve_session_model
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    env = references_env
    env.prefs.patch_namespace(OWNER, ALIASES_NAMESPACE, {"pairs": [["old", "new"]]})
    conversations = env.app.state.conversation_store
    conv = conversations.create_conversation(
        agent_id=env.agent, host_id=HOST, workspace="/opt/work/example", model_override="old/x"
    )
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(OWNER)
    permissions.grant(OWNER, conv.id, LEVEL_OWNER)
    await resolve_session_model(
        conv,
        conversations=conversations,
        permissions=permissions,
        agents=env.app.state.agent_store,
        agent_cache=env.app.state.agent_cache,
        preferences=env.prefs,
        catalogs=env.catalogs,
    )
    current = conversations.get_conversation(conv.id)
    assert current.model_override == "new/x"
    assert notes_from_labels(current.labels)[0]["saved"] == "old/x"


@pytest.mark.parametrize("custom", [False, True])
async def test_auth_and_gateway_models_remain_manual_after_provider_replace(
    references_env, custom
):
    from tests._helpers.session import bundle_files

    env = references_env
    agent_id = ("ca_" if custom else "") + uuid4().hex
    bundle = bundle_files(
        {
            "config.yaml": (
                b"spec_version: 1\nname: manual-models\n"
                b"llm:\n  model: old/gateway\n  fallback_models: [old/backup]\n"
                b"executor:\n  type: omnigent\n  model: old/x\n"
                b"  config: {harness: codex}\n  auth: {type: provider, name: old}\n"
            )
        }
    )
    location = bundle_location(agent_id, bundle)
    env.app.state.artifact_store.put(location, bundle)
    if custom:
        spec = validate_agent_bundle(bundle)
        env.custom.create(
            OWNER,
            {
                "id": agent_id,
                "name": "manual-models",
                "harness": "codex",
                "model": "old/x",
                "members": project_members(spec),
                "bundle_location": location,
            },
        )
    else:
        env.app.state.agent_store.create_user_agent(agent_id, "manual-models", location, OWNER)
    response = await env.client.post(
        URL + "/replace", json={"mode": "provider", "from": "old", "to": "new"}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert not result["complete"] and result["remaining"] == 2
    location = (
        env.custom.get(OWNER, agent_id)["bundle_location"]
        if custom
        else env.app.state.agent_store.get(agent_id).bundle_location
    )
    updated_bundle = env.app.state.artifact_store.get(location)
    spec = validate_agent_bundle(updated_bundle)
    assert spec.executor.model == "new/x"
    assert spec.executor.auth.name == "old"
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(updated_bundle)) as archive:
        member = next(
            item for item in archive.getmembers() if item.name.lstrip("./") == "config.yaml"
        )
        raw = archive.extractfile(member).read()
    assert b"old/gateway" in raw and b"old/backup" in raw
    rows = (await env.client.get(URL)).json()["rows"]
    assert {row["kind"] for row in rows if row["manual"]} >= {"manual_auth", "manual_llm"}


async def test_resume_members_use_their_own_host_and_keep_unknown_models(references_env, db_uri):
    from omnigent.member_snapshot import encode_member_entry, member_label_key, parse_member_entry
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.server.model_resolution import notes_from_labels, resolve_session_model
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    env = references_env
    other_host = "2" * 32
    env.catalogs.upsert(other_host, "codex", [{"id": "a/x"}, {"id": "b/x"}], 1)
    env.prefs.patch_namespace(OWNER, ALIASES_NAMESPACE, {"pairs": [["old", "new"]]})
    key = member_label_key("worker")
    conv = env.app.state.conversation_store.create_conversation(
        agent_id=env.agent,
        host_id=HOST,
        workspace="/opt/work/example",
        model_override="old/x",
        labels={
            key: encode_member_entry(
                {
                    "host": other_host,
                    "harness": "codex",
                    "model": "old/x",
                    "effort": "high",
                    "lead": False,
                    "unavailable": "model_missing",
                }
            )
        },
    )
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(OWNER)
    permissions.grant(OWNER, conv.id, LEVEL_OWNER)
    await resolve_session_model(
        conv,
        conversations=env.app.state.conversation_store,
        permissions=permissions,
        agents=env.app.state.agent_store,
        agent_cache=env.app.state.agent_cache,
        preferences=env.prefs,
        catalogs=env.catalogs,
    )
    saved = env.app.state.conversation_store.get_conversation(conv.id)
    member = parse_member_entry(saved.labels[key])
    assert saved.model_override == "new/x"
    assert member == {
        "host": other_host,
        "harness": "codex",
        "model": "old/x",
        "effort": "high",
        "lead": False,
    }
    notes = notes_from_labels(saved.labels)
    assert {(note["host"], note["outcome"]) for note in notes} == {
        (HOST, "renamed"),
        (other_host, "missing"),
    }


@pytest.mark.parametrize("provider_rename", [True, False], ids=["explicit-alias", "no-alias"])
async def test_library_launch_retains_lead_and_member_resolution_notes(
    references_env, monkeypatch, provider_rename
):
    from omnigent.member_snapshot import member_label_key, parse_member_entry
    from omnigent.server.library_agent_launch import LibraryAgentLaunch, launch_library_agent
    from omnigent.server.model_resolution import notes_from_labels
    from omnigent.server.routes._sessions import helpers

    env = references_env
    if provider_rename:
        env.prefs.patch_namespace(OWNER, ALIASES_NAMESPACE, {"pairs": [["old", "new"]]})
    bundle = build_agent_bundle(
        name="joint",
        include_llm=False,
        executor={"type": "omnigent", "model": "old/x", "config": {"harness": "codex"}},
        sub_agents=[
            {
                "name": "worker",
                "executor": {"type": "omnigent", "model": "old/y", "config": {"harness": "codex"}},
            }
        ],
    )
    agent_id = "ca_" + uuid4().hex
    location = bundle_location(agent_id, bundle)
    env.app.state.artifact_store.put(location, bundle)
    spec = validate_agent_bundle(bundle)
    env.custom.create(
        OWNER,
        {
            "id": agent_id,
            "name": "joint",
            "harness": "codex",
            "model": "old/x",
            "members": project_members(spec),
            "bundle_location": location,
        },
    )

    async def catalog(host, harness):
        return [{"id": "new/x"}, {"id": "new/y"}]

    monkeypatch.setattr(helpers, "_host_model_options_via_registry", catalog)
    result = await launch_library_agent(
        custom_agents_store=env.custom,
        artifact_store=env.app.state.artifact_store,
        conversation_store=env.app.state.conversation_store,
        host_store=env.hosts,
        owner=OWNER,
        agent_id=agent_id,
        launch=LibraryAgentLaunch(
            title="Saved agent", host_id=HOST, workspace="/opt/work/example"
        ),
        preferences_store=env.prefs,
        catalog_store=env.catalogs,
    )
    saved = env.app.state.conversation_store.get_conversation(result.session_id)
    member = parse_member_entry(saved.labels[member_label_key("worker")])
    assert member is not None
    if provider_rename:
        assert saved.model_override == "new/x"
        assert member["model"] == "new/y"
    else:
        assert saved.model_override is None
        agent = env.app.state.agent_store.get(result.agent_id)
        saved_spec = validate_agent_bundle(env.app.state.artifact_store.get(agent.bundle_location))
        assert saved_spec.executor.model == "old/x"
        assert member["model"] == "old/y"
        assert "unavailable" not in member
        assert any("trying it as unknown" in warning for warning in result.warnings)
    notes = notes_from_labels(saved.labels)
    assert {note["setting"] for note in notes} >= {"member:worker", "library_agent"}
    assert {note["outcome"] for note in notes} == {"renamed" if provider_rename else "missing"}
    assert result.notes == notes
