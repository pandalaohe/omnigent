"""Saved models resolve at create, resume and library launch boundaries."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from omnigent.models.model_reference import ALIASES_NAMESPACE
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.custom_agent_bundles import project_members
from omnigent.server.custom_agents_store import CustomAgentsStore
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.server.helpers import build_agent_bundle
from tests.server.test_user_preferences import _preferences_app

OWNER = "owner@example.test"
HOST = "1" * 32


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
    assert any("passing it to the host unchanged" in warning for warning in warnings)


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
        assert any("passing it to the host unchanged" in warning for warning in result.warnings)
    notes = notes_from_labels(saved.labels)
    assert {note["setting"] for note in notes} >= {"member:worker", "library_agent"}
    assert {note["outcome"] for note in notes} == {"renamed" if provider_rename else "missing"}
    assert result.notes == notes
