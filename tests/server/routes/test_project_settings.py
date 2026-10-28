"""Guarded project-default writes share the UI's config and entry state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import validate

from omnigent.policies.types import EvaluationContext
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime.policies.builder import build_policy_engine
from omnigent.server.custom_agents_store import CustomAgentsStore
from omnigent.spec import AgentSpec
from omnigent.spec.types import Phase, PolicyAction
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.tools.manager import ToolManager
from tests.server.routes.test_calling_defaults_routes import AGENT_ID, _bundle
from tests.server.routes.test_project_collaboration_routes import (
    _HOST_A,
    _HOST_B,
    ALICE,
    BOB,
    _as_user,
    _build_app,
    _make_project,
)


@pytest.fixture()
def settings_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    app = _build_app(db_uri, tmp_path, preferences=True, catalogs=True)
    hosts = HostStore(db_uri)
    for host_id in (_HOST_A, _HOST_B):
        hosts.upsert_on_connect(host_id, f"Neutral host {host_id[-2:]}", "local")
        hosts.update_harness_readiness(host_id, {"codex-native": True})
        hosts.set_offline(host_id)
        app.state.host_model_catalog_cache_store.upsert(
            host_id,
            "codex-native",
            [{"id": "offered-model", "supportedReasoningEfforts": ["low", "high"]}],
            100,
        )
    return app


@pytest.fixture()
def shared_settings_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    app = _build_app(
        db_uri, tmp_path, auth=True, preferences=True, catalogs=True, permissions=True
    )
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(_HOST_A, "Owner host", ALICE)
    hosts.set_offline(_HOST_A)
    app.state.host_model_catalog_cache_store.upsert(
        _HOST_A, "codex-native", [{"id": "owner-model"}], 100
    )
    SqlAlchemyPermissionStore(db_uri).ensure_user("admin@example.com", is_admin=True)
    return app


@pytest.mark.asyncio
async def test_orchestration_tool_to_store_requires_approval(settings_app, db_uri):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        url = f"/v1/projects/{project_id}/settings"
        initial = (await client.get(url)).json()
        settings_app.state.user_preferences_store.patch_namespace(
            "local", "session_collab", {"standingPassDefault": ["install", "merge"]}
        )
        args = {
            "project_id": project_id,
            "expected_revision": initial["revision"],
            "orchestration": {
                "child_cap": 4,
                "standing_pass": {"remove": ["install"], "exceptions": ["production release"]},
            },
        }
        spec = AgentSpec(spec_version=1)
        schema = next(
            row["function"]["parameters"]
            for row in ToolManager(spec).get_tool_schemas()
            if row["function"]["name"] == "sys_project_settings_update"
        )
        validate(args, schema)
        conversations = SqlAlchemyConversationStore(db_uri)
        conversation = conversations.create_conversation(project_id=project_id)
        engine = build_policy_engine(
            spec=spec,
            conversation_id=conversation.id,
            conversation_store=conversations,
            policy_store=SqlAlchemyPolicyStore(db_uri),
        )
        decision = await engine.evaluate(
            EvaluationContext(
                phase=Phase.TOOL_CALL,
                tool_name="sys_project_settings_update",
                content={"name": "sys_project_settings_update", "arguments": args},
            )
        )
        assert decision.action == PolicyAction.ASK
        result = json.loads(
            await execute_tool(
                tool_name="sys_project_settings_update",
                arguments=json.dumps(args),
                server_client=client,
            )
        )
        assert "error" not in result, result
        saved = SqlAlchemyProjectStore(db_uri).get(project_id, user_id=None)
        assert saved.config["orchestration"] == args["orchestration"]
        record = settings_app.state.standing_pass_service.resolve(conversation)
        assert record["kinds"] == ["merge"]
        assert record["exceptions"] == ["production release"]
        assert set(result["changed_fields"]) == {
            "orchestration.child_cap",
            "orchestration.standing_pass.remove",
            "orchestration.standing_pass.exceptions",
        }
        stale = json.loads(
            await execute_tool(
                tool_name="sys_project_settings_update",
                arguments=json.dumps(args),
                server_client=client,
            )
        )
        assert stale["error"] == "server returned 409"


@pytest.mark.asyncio
async def test_orchestration_patches_only_named_leaves(settings_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        config = {
            "custom": "keep",
            "orchestration": {
                "child_cap": 4,
                "setup": "Saved",
                "standing_pass": {
                    "remove": ["install"],
                    "exceptions": ["production"],
                },
            },
        }
        await client.patch(f"/v1/projects/{project_id}", json={"config": config})
        url = f"/v1/projects/{project_id}/settings"
        for patch, expected in (
            (
                {"standing_pass": {"remove": None}},
                {
                    "child_cap": 4,
                    "standing_pass": {"exceptions": ["production"]},
                },
            ),
            (
                {"child_cap": "none"},
                {
                    "child_cap": "none",
                    "standing_pass": {"exceptions": ["production"]},
                },
            ),
            ({"child_cap": None, "standing_pass": {"exceptions": None}}, None),
        ):
            revision = (await client.get(url)).json()["revision"]
            response = await client.put(
                url,
                json={
                    "expected_revision": revision,
                    "orchestration": patch,
                },
            )
            assert response.status_code == 200, response.text
            actual = response.json()["config"]
            assert actual.get("orchestration") == expected
            assert actual["custom"] == "keep"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        None,
        {},
        {"child_cap": 0},
        {"child_cap": True},
        {"child_cap": 1.5},
        {"child_cap": "4"},
        {"setup": "Retired setup"},
        {"setup": None},
        {"unknown": 1},
        {"standing_pass": None},
        {"standing_pass": {}},
        {"standing_pass": {"remove": "install"}},
        {"standing_pass": {"remove": [True]}},
        {"standing_pass": {"remove": [""]}},
        {"standing_pass": {"remove": ["x" * 121]}},
        {"standing_pass": {"remove": ["x"] * 33}},
        {"standing_pass": {"exceptions": ["   "]}},
        {"standing_pass": {"exceptions": ["x"] * 6}},
        {"standing_pass": {"exceptions": ["x" * 121]}},
        {"standing_pass": {"extra": []}},
    ],
)
async def test_invalid_orchestration_is_atomic(settings_app, patch):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        url = f"/v1/projects/{project_id}/settings"
        before = (await client.get(url)).json()
        response = await client.put(
            url,
            json={
                "expected_revision": before["revision"],
                "orchestration": patch,
            },
        )
        assert response.status_code in (400, 422), response.text
        after = (await client.get(url)).json()
        assert after["revision"] == before["revision"]
        assert after["config"] == before["config"]


@pytest.mark.asyncio
async def test_named_leaves_entry_and_revision_guard(settings_app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        url = f"/v1/projects/{project_id}/settings"
        initial = (await client.get(url)).json()
        assert initial["default_host"] == {"host_id": None, "reason": "none"}
        assert any(row["harness"] == "codex-native" for row in initial["rows"])
        first_revision = initial["revision"]
        patch: dict[str, Any] = {
            "expected_revision": first_revision,
            "project": {"host_id": _HOST_B},
            "host": {
                "host_id": _HOST_B,
                "workspace": "/opt/work/project",
                "harnesses": {"codex-native": {"model": "offered-model", "effort": "high"}},
            },
        }
        updated = await client.put(url, json=patch)
        assert updated.status_code == 200, updated.text
        saved = updated.json()
        assert saved["revision"] != first_revision
        assert saved["entries"] == [{"host_id": _HOST_B, "workspace": "/opt/work/project"}]
        assert saved["entry"]["checked"] is False
        assert saved["config"]["workspace"] == "/opt/work/project"
        assert saved["default_host"] == {"host_id": _HOST_B, "reason": "config"}
        row = next(
            row
            for row in saved["rows"]
            if row["host_id"] == _HOST_B and row["harness"] == "codex-native"
        )
        assert row["effective"]["model"] == "offered-model"
        assert row["sources"]["model"] == "project_host"
        assert row["effective"]["effort"] == "high"
        unsupported_speed = await client.put(
            url,
            json={
                "expected_revision": saved["revision"],
                "host": {
                    "host_id": _HOST_B,
                    "harnesses": {"codex-native": {"speed": "priority"}},
                },
            },
        )
        assert unsupported_speed.status_code == 400
        assert "speed" in unsupported_speed.text
        settings_app.state.host_model_catalog_cache_store.upsert(
            _HOST_B,
            "codex-native",
            [
                {"id": "offered-model", "supportedReasoningEfforts": ["low", "high"]},
                {"id": "low-model", "supportedReasoningEfforts": ["low"]},
            ],
            100,
        )
        incompatible_model = await client.put(
            url,
            json={
                "expected_revision": saved["revision"],
                "host": {
                    "host_id": _HOST_B,
                    "harnesses": {"codex-native": {"model": "low-model"}},
                },
            },
        )
        assert incompatible_model.status_code == 400
        assert "effort" in incompatible_model.text

        stale = await client.put(url, json=patch)
        assert stale.status_code == 409
        assert (await client.get(url)).json()["revision"] == saved["revision"]

        cleared = await client.put(
            url,
            json={
                "expected_revision": saved["revision"],
                "host": {
                    "host_id": _HOST_B,
                    "workspace": None,
                    "harnesses": {"codex-native": {"model": None}},
                },
            },
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["entries"] == []
        assert "workspace" not in cleared.json()["config"]
        assert cleared.json()["config"]["calling_defaults"][_HOST_B]["harnesses"][
            "codex-native"
        ] == {"effort": "high"}


@pytest.mark.asyncio
async def test_sdk_inheritance_and_unnamed_config_survive_ui_writer(
    settings_app: FastAPI,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        url = f"/v1/projects/{project_id}/settings"
        initial_config = {
            "custom_setting": "keep",
            "calling_defaults": {
                _HOST_A: {"harnesses": {"codex-native": {"model": "offered-model"}}}
            },
        }
        assert (
            await client.patch(f"/v1/projects/{project_id}", json={"config": initial_config})
        ).status_code == 200
        read = await client.get(url, params={"host_id": _HOST_A, "harness": "codex"})
        assert read.status_code == 200, read.text
        assert len(read.json()["rows"]) == 1
        assert read.json()["rows"][0]["effective"]["model"] == "offered-model"
        assert read.json()["rows"][0]["sources"]["model"] == "project_host_native"
        revision = read.json()["revision"]
        saved = await client.put(
            url,
            json={
                "expected_revision": revision,
                "project": {"host_id": _HOST_A},
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["config"]["custom_setting"] == "keep"
        assert saved.json()["config"]["calling_defaults"] == initial_config["calling_defaults"]
        revision = saved.json()["revision"]
        changed = {**saved.json()["config"], "custom_setting": "UI changed"}
        assert (
            await client.patch(f"/v1/projects/{project_id}", json={"config": changed})
        ).status_code == 200
        stale = await client.put(
            url,
            json={"expected_revision": revision, "project": {"agent_id": None}},
        )
        assert stale.status_code == 409


@pytest.mark.asyncio
async def test_invalid_model_rejects_all_changes_and_ui_entry_edit_conflicts(
    settings_app: FastAPI,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        url = f"/v1/projects/{project_id}/settings"
        revision = (await client.get(url)).json()["revision"]
        invalid = await client.put(
            url,
            json={
                "expected_revision": revision,
                "host": {
                    "host_id": _HOST_A,
                    "workspace": "/opt/work/project",
                    "harnesses": {"codex-native": {"model": "not-offered"}},
                },
            },
        )
        assert invalid.status_code == 400, invalid.text
        assert "host.harnesses.codex-native.model" in invalid.text
        assert (await client.get(url)).json()["entries"] == []

        ui_write = await client.put(
            f"/v1/projects/{project_id}/entries/{_HOST_A}",
            json={"workspace": "/opt/work/from-ui"},
        )
        assert ui_write.status_code == 200, ui_write.text
        stale = await client.put(
            url,
            json={"expected_revision": revision, "project": {"host_id": _HOST_B}},
        )
        assert stale.status_code == 409
        assert (await client.get(url)).json()["entries"] == [
            {"host_id": _HOST_A, "workspace": "/opt/work/from-ui"}
        ]
        current_revision = (await client.get(url)).json()["revision"]
        assert (
            await client.put(url, json={"expected_revision": current_revision, "project": {}})
        ).status_code == 400
        assert (
            await client.put(
                url,
                json={
                    "expected_revision": current_revision,
                    "project": {"host_id": _HOST_A},
                    "user_id": "forged",
                },
            )
        ).status_code == 422


@pytest.mark.asyncio
async def test_owner_admin_and_foreign_user_are_scoped_to_project_owner(
    shared_settings_app: FastAPI,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=shared_settings_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client, headers=_as_user(ALICE))
        url = f"/v1/projects/{project_id}/settings"
        assert (await client.get(url, headers=_as_user(BOB))).status_code == 404
        owner = await client.get(url, headers=_as_user(ALICE))
        assert owner.status_code == 200, owner.text
        admin = await client.get(url, headers=_as_user("admin@example.com"))
        assert admin.status_code == 200, admin.text
        assert admin.json()["owner_user_id"] == ALICE
        assert [host["host_id"] for host in admin.json()["hosts"]] == [_HOST_A]
        response = await client.put(
            url,
            headers=_as_user("admin@example.com"),
            json={
                "expected_revision": admin.json()["revision"],
                "host": {"host_id": _HOST_A, "workspace": "/opt/work/owner-project"},
                "orchestration": {"child_cap": 4},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["owner_user_id"] == ALICE
        assert response.json()["config"]["orchestration"] == {"child_cap": 4}
        assert (
            await client.put(
                url,
                headers=_as_user(BOB),
                json={
                    "expected_revision": response.json()["revision"],
                    "project": {"host_id": _HOST_A},
                    "orchestration": {"child_cap": 8},
                },
            )
        ).status_code == 404


@pytest.mark.asyncio
async def test_entry_hook_warning_is_persisted_and_noop_retry_skips_hook(
    settings_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    template_calls: list[str] = []
    LocalArtifactStore(str(tmp_path / "artifacts")).put("template/bundle", _bundle("codex-native"))
    settings_app.state.agent_store.create(AGENT_ID, "Template Agent", "template/bundle")

    async def stored_workspace(**kwargs):
        return kwargs["workspace"], "Neutral host", True

    async def template(*args):
        template_calls.append(args[2])
        return AGENT_ID, None

    async def post_bind(**kwargs):
        calls.append(kwargs["workspace"])
        return {"status": "failed", "exit_code": 1, "output": "", "error": "setup failed"}

    monkeypatch.setattr(
        "omnigent.server.routes.project_settings._resolve_stored_workspace", stored_workspace
    )
    monkeypatch.setattr(
        "omnigent.server.routes.project_settings.resolve_project_host_template", template
    )
    monkeypatch.setattr("omnigent.server.routes.project_settings.run_post_bind_request", post_bind)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        url = f"/v1/projects/{project_id}/settings"
        revision = (await client.get(url)).json()["revision"]
        body = {
            "expected_revision": revision,
            "host": {"host_id": _HOST_A, "workspace": "/opt/work/project"},
        }
        saved = await client.put(url, json=body)
        assert saved.status_code == 200, saved.text
        result = saved.json()
        assert result["config"]["calling_defaults"][_HOST_A]["agent_id"] == AGENT_ID
        assert result["entry"]["post_bind"]["status"] == "failed"
        assert result["entry"]["changed"] is True
        assert calls == ["/opt/work/project"]
        body["expected_revision"] = result["revision"]
        retried = await client.put(url, json=body)
        assert retried.status_code == 200, retried.text
        assert retried.json()["changed"] is False
        assert "post_bind" not in retried.json()["entry"]
        assert calls == ["/opt/work/project"]
        removed = await client.put(
            url,
            json={
                "expected_revision": retried.json()["revision"],
                "host": {"host_id": _HOST_A, "workspace": None},
            },
        )
        assert removed.status_code == 200
        assert calls == ["/opt/work/project"]

        other_project = await _make_project(client)
        other_url = f"/v1/projects/{other_project}/settings"
        other_revision = (await client.get(other_url)).json()["revision"]
        explicit_clear = await client.put(
            other_url,
            json={
                "expected_revision": other_revision,
                "host": {
                    "host_id": _HOST_B,
                    "workspace": "/opt/work/other-project",
                    "agent_id": None,
                },
            },
        )
        assert explicit_clear.status_code == 200
        assert _HOST_B not in explicit_clear.json()["config"].get("calling_defaults", {})
        assert template_calls == [_HOST_A, _HOST_A]


@pytest.mark.asyncio
async def test_missing_library_agent_can_be_read_and_cleared(
    settings_app: FastAPI, db_uri: str
) -> None:
    settings_app.state.custom_agents_store = CustomAgentsStore(db_uri)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        project_url = f"/v1/projects/{project_id}"
        assert (
            await client.patch(project_url, json={"config": {"agent_id": "ca_deleted"}})
        ).status_code == 200
        url = f"{project_url}/settings"
        read = await client.get(url)
        assert read.status_code == 200, read.text
        assert read.json()["config"]["agent_id"] == "ca_deleted"
        unrelated = await client.put(
            url,
            json={
                "expected_revision": read.json()["revision"],
                "project": {"host_id": _HOST_A},
            },
        )
        assert unrelated.status_code == 200, unrelated.text
        invalid = await client.put(
            url,
            json={
                "expected_revision": unrelated.json()["revision"],
                "project": {"agent_id": "ca_deleted"},
            },
        )
        assert invalid.status_code == 400
        cleared = await client.put(
            url,
            json={
                "expected_revision": unrelated.json()["revision"],
                "project": {"agent_id": None},
            },
        )
        assert cleared.status_code == 200, cleared.text
        assert "agent_id" not in cleared.json()["config"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["host-clear", "project-model"])
@pytest.mark.parametrize("field,value", [("effort", "high"), ("speed", "fast")])
async def test_inherited_model_changes_validate_retained_defaults(
    settings_app: FastAPI, tmp_path: Path, source: str, field: str, value: str
) -> None:
    LocalArtifactStore(str(tmp_path / "artifacts")).put("agent/bundle", _bundle("codex-native"))
    settings_app.state.agent_store.create(AGENT_ID, "Default Agent", "agent/bundle")
    settings_app.state.host_model_catalog_cache_store.upsert(
        _HOST_A,
        "codex-native",
        [
            {
                "id": "offered-model",
                "supportedReasoningEfforts": ["low", "high"],
                "serviceTiers": [{"id": "priority", "name": "Fast"}],
            },
            {"id": "low-model", "supportedReasoningEfforts": ["low"]},
        ],
        100,
    )
    settings_app.state.user_preferences_store.patch_namespace(
        "local", "calling_defaults", {_HOST_A: {"codex-native": {"model": "low-model"}}}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        setting = {field: value}
        config: dict[str, Any] = {
            "host_id": _HOST_A,
            "agent_id": AGENT_ID,
            "calling_defaults": {_HOST_A: {"harnesses": {"codex-native": setting}}},
        }
        if source == "host-clear":
            setting["model"] = "offered-model"
        else:
            config["model"] = "offered-model"
        assert (
            await client.patch(f"/v1/projects/{project_id}", json={"config": config})
        ).status_code == 200
        url = f"/v1/projects/{project_id}/settings"
        revision = (await client.get(url)).json()["revision"]
        body: dict[str, Any] = {"expected_revision": revision}
        if source == "host-clear":
            body["host"] = {
                "host_id": _HOST_A,
                "harnesses": {"codex-native": {"model": None}},
            }
        else:
            body["project"] = {"model": "low-model"}
        rejected = await client.put(url, json=body)
        assert rejected.status_code == 400, rejected.text
        assert field in rejected.text
        assert (await client.get(url)).json()["revision"] == revision
        body.setdefault("host", {"host_id": _HOST_A}).setdefault("harnesses", {}).setdefault(
            "codex-native", {}
        )[field] = None
        cleared = await client.put(url, json=body)
        assert cleared.status_code == 200, cleared.text
        row = next(
            row
            for row in cleared.json()["rows"]
            if row["host_id"] == _HOST_A and row["harness"] == "codex-native"
        )
        assert row["effective"]["model"] == "low-model"
        assert row["effective"][field] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("readiness", [False, "binary-missing", "version-too-low", "needs-auth"])
async def test_unready_harness_rejects_new_defaults_but_allows_reset(
    settings_app: FastAPI, tmp_path: Path, readiness: Any
) -> None:
    LocalArtifactStore(str(tmp_path / "artifacts")).put("agent/bundle", _bundle("codex-native"))
    settings_app.state.agent_store.create(AGENT_ID, "Default Agent", "agent/bundle")
    settings_app.state.host_store.update_harness_readiness(_HOST_A, {"codex-native": readiness})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        config = {
            "calling_defaults": {
                _HOST_A: {
                    "harnesses": {"codex-native": {"model": "offered-model", "effort": "high"}}
                }
            }
        }
        assert (
            await client.patch(f"/v1/projects/{project_id}", json={"config": config})
        ).status_code == 200
        url = f"/v1/projects/{project_id}/settings"
        revision = (await client.get(url)).json()["revision"]
        for fields in (
            {"agent_id": AGENT_ID},
            {"harnesses": {"codex-native": {"model": "offered-model"}}},
        ):
            rejected = await client.put(
                url, json={"expected_revision": revision, "host": {"host_id": _HOST_A, **fields}}
            )
            assert rejected.status_code == 400, rejected.text
            assert "unavailable" in rejected.text
        cleared = await client.put(
            url,
            json={
                "expected_revision": revision,
                "host": {
                    "host_id": _HOST_A,
                    "harnesses": {
                        "codex-native": {
                            "model": None,
                            "effort": None,
                            "speed": None,
                            "permission": None,
                        }
                    },
                },
            },
        )
        assert cleared.status_code == 200, cleared.text
        assert _HOST_A not in cleared.json()["config"].get("calling_defaults", {})


@pytest.mark.asyncio
async def test_sdk_auth_is_advisory_and_invalid_model_hosts_are_field_errors(
    settings_app: FastAPI, tmp_path: Path
) -> None:
    LocalArtifactStore(str(tmp_path / "artifacts")).put("agent/bundle", _bundle("claude-sdk"))
    settings_app.state.agent_store.create(AGENT_ID, "SDK Agent", "agent/bundle")
    settings_app.state.host_store.update_harness_readiness(_HOST_A, {"claude-sdk": "needs-auth"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        url = f"/v1/projects/{project_id}/settings"
        revision = (await client.get(url)).json()["revision"]
        accepted = await client.put(
            url,
            json={
                "expected_revision": revision,
                "project": {"host_id": _HOST_A, "agent_id": AGENT_ID},
            },
        )
        assert accepted.status_code == 200, accepted.text
        revision = accepted.json()["revision"]
        for host_id in ("__sandbox__", "missing-host"):
            if host_id == "missing-host":
                assert (
                    await client.patch(
                        f"/v1/projects/{project_id}",
                        json={"config": {"host_id": host_id, "agent_id": AGENT_ID}},
                    )
                ).status_code == 200
                revision = (await client.get(url)).json()["revision"]
                project_patch = {"model": "offered-model"}
            else:
                project_patch = {
                    "host_id": host_id,
                    "agent_id": AGENT_ID,
                    "model": "offered-model",
                }
            rejected = await client.put(
                url, json={"expected_revision": revision, "project": project_patch}
            )
            assert rejected.status_code == 400, rejected.text
            assert "project.model" in rejected.text
