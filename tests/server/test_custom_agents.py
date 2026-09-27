"""Private library ownership and full-archive lifecycle regression coverage."""

from __future__ import annotations

import asyncio
import io
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import yaml
from starlette.requests import HTTPConnection

from omnigent.db.utils import generate_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, AuthProvider
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.custom_agent_bundles import patch_bundle
from omnigent.server.custom_agents_store import CustomAgentsStore
from omnigent.server.routes import custom_agents as custom_agents_routes
from omnigent.spec.parser import _ConfigYamlLoader
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


class HeaderAuth(AuthProvider):
    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


def bundle(
    config: str | None = None,
    subagent: str | None = None,
    extra: dict[str, bytes] | None = None,
) -> bytes:
    entries = {
        "config.yaml": (
            config
            or """spec_version: 1
name: custom-reviewer
description: Original description
executor:
  type: omnigent
  model: test-model
  config:
    harness: codex
instructions: prompts/custom.md
tools:
  remote:
    type: mcp
    url: https://example.invalid/mcp
    headers:
      Authorization: '${CATALOG_TEST_TOKEN}'
"""
        ).encode(),
        "prompts/custom.md": b"Original instructions",
        "tools/helper.py": b"# Preserve this bundled executable verbatim\n",
        "assets/data.bin": bytes(range(256)),
    }
    if subagent is not None:
        entries["agents/researcher/config.yaml"] = subagent.encode()
    entries.update(extra or {})
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".py") else 0o644
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def joint_bundle() -> bytes:
    return bundle(
        """spec_version: 1
name: custom-reviewer
description: Lead reviewer
executor: {type: omnigent, model: lead-model, reasoning_effort: high, config: {harness: codex}}
""",
        """spec_version: 1
name: researcher
description: Research support
executor: {type: omnigent, model: research-model, reasoning_effort: medium,
  config: {harness: claude-sdk}}
""",
    )


_FLAT_AGENT_YAML = """name: flat-reviewer
description: Flat description
prompt: Flat instructions
executor:
  harness: codex
  model: test-model
"""


def flat_bundle(config: str) -> bytes:
    """A single-file saved Agent: one root ``agent.yaml``, no ``config.yaml``."""
    out = io.BytesIO()
    data = config.encode()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        info = tarfile.TarInfo("agent.yaml")
        info.size = len(data)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def members(data: bytes) -> dict[str, tuple[bytes, int]]:
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return {m.name: (archive.extractfile(m).read(), m.mode) for m in archive if m.isfile()}


def roster_member(
    name: str,
    *,
    lead: bool = False,
    harness: str = "codex",
    model: str | None = None,
    reasoning_effort: str | None = None,
    description: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "description": description,
        "harness": harness,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "lead": lead,
    }


def make_app(db_uri: str, tmp_path: Path):
    artifacts = LocalArtifactStore(str(tmp_path / "custom-artifacts"))
    agents = SqlAlchemyAgentStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    app = create_app(
        agents,
        SqlAlchemyFileStore(db_uri),
        conversations,
        artifacts,
        AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "custom-cache"),
        auth_provider=HeaderAuth(),
        permission_store=permissions,
    )
    return app, artifacts, agents, conversations, permissions


@pytest.mark.asyncio
async def test_create_single_member_projection(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    data = bundle("""spec_version: 1
name: solo
executor: {type: omnigent, model: solo-model, reasoning_effort: high, config: {harness: codex}}
""")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/custom-agents",
            headers={"x-test-user": "alice"},
            files={"bundle": ("agent.tar.gz", data)},
        )

    assert response.status_code == 201, response.text
    assert response.json()["members"] == [
        {
            "name": "solo",
            "description": None,
            "harness": "codex",
            "model": "solo-model",
            "reasoning_effort": "high",
            "lead": True,
        }
    ]


@pytest.mark.asyncio
async def test_create_multi_member_projection(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/custom-agents",
            headers={"x-test-user": "alice"},
            files={"bundle": ("agent.tar.gz", joint_bundle())},
        )

    assert response.status_code == 201, response.text
    assert response.json()["members"] == [
        {
            "name": "custom-reviewer",
            "description": "Lead reviewer",
            "harness": "codex",
            "model": "lead-model",
            "reasoning_effort": "high",
            "lead": True,
        },
        {
            "name": "researcher",
            "description": "Research support",
            "harness": "claude-sdk",
            "model": "research-model",
            "reasoning_effort": "medium",
            "lead": False,
        },
    ]


@pytest.mark.asyncio
async def test_list_returns_stored_members_without_artifact_read(
    db_uri: str, tmp_path: Path, runtime_init: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents",
            headers={"x-test-user": "alice"},
            files={"bundle": ("agent.tar.gz", joint_bundle())},
        )
        assert created.status_code == 201, created.text
        CustomAgentsStore(db_uri).create(
            "alice",
            {
                "id": "ca_legacy_list",
                "name": "legacy",
                "harness": "codex",
                "bundle_location": "unused",
            },
        )

        def unexpected_get(_location: str) -> bytes:
            raise AssertionError("list read an artifact")

        monkeypatch.setattr(artifacts, "get", unexpected_get)
        listed = await client.get("/v1/custom-agents", headers={"x-test-user": "alice"})

    assert listed.status_code == 200, listed.text
    rows = {row["id"]: row for row in listed.json()["data"]}
    assert rows[created.json()["id"]]["members"] == created.json()["members"]
    assert rows["ca_legacy_list"]["members"] is None


@pytest.mark.asyncio
async def test_legacy_detail_backfills_members_without_version_change(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    data = joint_bundle()
    agent_id = "ca_legacy_detail"
    location = bundle_location(agent_id, data)
    artifacts.put(location, data)
    before = CustomAgentsStore(db_uri).create(
        "alice",
        {
            "id": agent_id,
            "name": "custom-reviewer",
            "description": "Lead reviewer",
            "harness": "codex",
            "model": "lead-model",
            "bundle_location": location,
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        detail = await client.get(
            f"/v1/custom-agents/{agent_id}", headers={"x-test-user": "alice"}
        )
        listed = await client.get("/v1/custom-agents", headers={"x-test-user": "alice"})

    assert detail.status_code == 200, detail.text
    assert [member["reasoning_effort"] for member in detail.json()["members"]] == [
        "high",
        "medium",
    ]
    assert detail.json()["members"][1]["model"] == "research-model"
    assert listed.json()["data"][0]["members"] == detail.json()["members"]
    assert detail.json()["version"] == before["version"]
    assert detail.json()["updated_at"] == before["updated_at"]


@pytest.mark.asyncio
async def test_patch_rederives_lead_model_from_bundle(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    data = bundle("""spec_version: 1
name: custom-reviewer
executor: {type: omnigent, model: newer-model, config: {harness: codex}}
""")
    agent_id = "ca_stale_model"
    location = bundle_location(agent_id, data)
    artifacts.put(location, data)
    CustomAgentsStore(db_uri).create(
        "alice",
        {
            "id": agent_id,
            "name": "custom-reviewer",
            "harness": "codex",
            "model": "older-model",
            "bundle_location": location,
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/v1/custom-agents/{agent_id}",
            headers={"x-test-user": "alice"},
            json={"name": "renamed"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["model"] == "newer-model"
    assert response.json()["members"][0]["model"] == "newer-model"


@pytest.mark.asyncio
async def test_patch_members_rewrites_bundle_row_and_projection(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle()
    roster = [
        roster_member(
            "custom-reviewer",
            lead=True,
            harness="claude-sdk",
            model="lead-2",
            reasoning_effort="high",
            description="Joint lead",
        ),
        roster_member(
            "researcher",
            harness="codex",
            model="research-model",
            reasoning_effort="medium",
            description="Research support",
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents",
            headers={"x-test-user": "alice"},
            files={"bundle": ("agent.tar.gz", original)},
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]
        response = await client.patch(
            f"/v1/custom-agents/{agent_id}",
            headers={"x-test-user": "alice"},
            json={"members": roster, "description": "Joint lead", "version": 1},
        )
        assert response.status_code == 200, response.text
        assert response.json()["members"] == roster
        assert response.json()["harness"] == "claude-sdk"
        assert response.json()["model"] == "lead-2"
        assert response.json()["description"] == "Joint lead"
        assert response.json()["version"] == 2
        downloaded = (
            await client.get(
                f"/v1/custom-agents/{agent_id}/contents", headers={"x-test-user": "alice"}
            )
        ).content

    before, after = members(original), members(downloaded)
    root = yaml.safe_load(after["config.yaml"][0])
    original_root = yaml.safe_load(before["config.yaml"][0])
    assert root["executor"]["config"]["harness"] == "claude-sdk"
    assert root["executor"]["model"] == "lead-2"
    assert root["executor"]["reasoning_effort"] == "high"
    assert root["tools"]["agents"] == ["researcher"]
    assert root["tools"]["remote"] == original_root["tools"]["remote"]
    assert root["spawn"] is True
    for name in ("prompts/custom.md", "tools/helper.py", "assets/data.bin"):
        assert after[name] == before[name]
    sub = yaml.safe_load(after["agents/researcher/config.yaml"][0])
    assert sub["name"] == "researcher"
    assert sub["description"] == "Research support"
    assert sub["executor"]["config"]["harness"] == "codex"
    assert sub["executor"]["model"] == "research-model"
    assert sub["executor"]["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_patch_members_stale_version_writes_nothing(
    db_uri: str, tmp_path: Path, runtime_init: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle()
    headers = {"x-test-user": "alice"}
    roster = [
        roster_member("custom-reviewer", lead=True, harness="codex", model="lead-2"),
        roster_member("researcher", harness="codex", model="research-model"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents", headers=headers, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]
        writes: list[str] = []
        original_put = artifacts.put

        def record_put(location: str, data: bytes) -> None:
            writes.append(location)
            original_put(location, data)

        monkeypatch.setattr(artifacts, "put", record_put)
        response = await client.patch(
            f"/v1/custom-agents/{agent_id}",
            headers=headers,
            json={"members": roster, "version": 2},
        )
        contents = await client.get(f"/v1/custom-agents/{agent_id}/contents", headers=headers)

    assert response.status_code == 409, response.text
    assert writes == []
    assert contents.content == original
    row = CustomAgentsStore(db_uri).get("alice", agent_id)
    assert row["version"] == 1
    assert (row["harness"], row["model"]) == ("codex", "test-model")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        pytest.param(
            {"members": [roster_member("researcher")], "version": 1},
            id="no-lead",
        ),
        pytest.param(
            {
                "members": [
                    roster_member("custom-reviewer", lead=True),
                    roster_member("researcher", lead=True),
                ],
                "version": 1,
            },
            id="two-leads",
        ),
        pytest.param(
            {
                "members": [
                    roster_member("custom-reviewer", lead=True),
                    roster_member("researcher"),
                    roster_member("researcher"),
                ],
                "version": 1,
            },
            id="duplicate-names",
        ),
        pytest.param(
            {
                "members": [
                    roster_member("custom-reviewer", lead=True),
                    roster_member("researcher"),
                ]
            },
            id="missing-version",
        ),
        pytest.param(
            {
                "members": [
                    roster_member("other-name", lead=True),
                    roster_member("researcher"),
                ],
                "version": 1,
            },
            id="lead-name-mismatch",
        ),
        pytest.param(
            {
                "members": [
                    roster_member("custom-reviewer", lead=True, description="Different"),
                    roster_member("researcher"),
                ],
                "version": 1,
            },
            id="lead-description-mismatch",
        ),
    ],
)
async def test_patch_members_rejects_invalid_roster(
    db_uri: str, tmp_path: Path, runtime_init: None, changes: dict[str, object]
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents",
            headers={"x-test-user": "alice"},
            files={"bundle": ("agent.tar.gz", bundle())},
        )
        assert created.status_code == 201, created.text
        response = await client.patch(
            f"/v1/custom-agents/{created.json()['id']}",
            headers={"x-test-user": "alice"},
            json=changes,
        )

    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_patch_members_rejects_flat_single_file_agent(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = flat_bundle(_FLAT_AGENT_YAML)
    headers = {"x-test-user": "alice"}
    roster = [
        roster_member(
            "flat-reviewer",
            lead=True,
            harness="claude-sdk",
            model="lead-2",
        )
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents", headers=headers, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]
        response = await client.patch(
            f"/v1/custom-agents/{agent_id}",
            headers=headers,
            json={"members": roster, "version": 1},
        )
        contents = await client.get(f"/v1/custom-agents/{agent_id}/contents", headers=headers)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["message"] == (
        "Members can only be edited on a directory Agent bundle (config.yaml); "
        "this Agent is a single YAML file"
    )
    assert contents.status_code == 200
    assert contents.content == original


@pytest.mark.asyncio
async def test_patch_scalars_on_flat_single_file_agent(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = flat_bundle(_FLAT_AGENT_YAML)
    headers = {"x-test-user": "alice"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents", headers=headers, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]
        response = await client.patch(
            f"/v1/custom-agents/{agent_id}",
            headers=headers,
            json={"name": "flat-renamed"},
        )
        contents = await client.get(f"/v1/custom-agents/{agent_id}/contents", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "flat-renamed"
    assert response.json()["members"][0]["name"] == "flat-renamed"
    edited = yaml.safe_load(members(contents.content)["agent.yaml"][0])
    assert edited["name"] == "flat-renamed"
    assert edited["prompt"] == "Flat instructions"
    assert edited["executor"]["model"] == "test-model"


@pytest.mark.asyncio
async def test_patch_members_preserves_imported_sub_agent_config_keys(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle(
        """spec_version: 1
name: custom-reviewer
description: Lead reviewer
executor: {type: omnigent, model: lead-model, reasoning_effort: high, config: {harness: codex}}
""",
        """spec_version: 1
name: researcher
description: Research support
executor: {type: omnigent, model: old-model, reasoning_effort: low, config: {harness: codex}}
params:
  keep: me
""",
        extra={"agents/researcher/notes.md": b"Keep this file\n"},
    )
    roster = [
        roster_member(
            "custom-reviewer",
            lead=True,
            harness="codex",
            model="lead-2",
            reasoning_effort="high",
            description="Lead reviewer",
        ),
        roster_member(
            "researcher",
            harness="codex",
            model="new-model",
            reasoning_effort="high",
            description="Research support",
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents",
            headers={"x-test-user": "alice"},
            files={"bundle": ("agent.tar.gz", original)},
        )
        assert created.status_code == 201, created.text
        response = await client.patch(
            f"/v1/custom-agents/{created.json()['id']}",
            headers={"x-test-user": "alice"},
            json={"members": roster, "version": 1},
        )
        assert response.status_code == 200, response.text
        downloaded = (
            await client.get(
                f"/v1/custom-agents/{created.json()['id']}/contents",
                headers={"x-test-user": "alice"},
            )
        ).content

    after = members(downloaded)
    sub = yaml.safe_load(after["agents/researcher/config.yaml"][0])
    assert sub["params"] == {"keep": "me"}
    assert sub["description"] == "Research support"
    assert sub["executor"]["model"] == "new-model"
    assert sub["executor"]["reasoning_effort"] == "high"
    assert sub["executor"]["config"]["harness"] == "codex"
    assert after["agents/researcher/notes.md"][0] == b"Keep this file\n"
    assert yaml.safe_load(after["config.yaml"][0])["executor"]["model"] == "lead-2"


@pytest.mark.asyncio
async def test_patch_members_drops_role_removed_from_roster(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    lead = roster_member(
        "custom-reviewer",
        lead=True,
        harness="codex",
        model="lead-model",
        reasoning_effort="high",
        description="Lead reviewer",
    )
    researcher = roster_member(
        "researcher",
        harness="claude-sdk",
        model="research-model",
        reasoning_effort="medium",
        description="Research support",
    )
    headers = {"x-test-user": "alice"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents",
            headers=headers,
            files={"bundle": ("agent.tar.gz", joint_bundle())},
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]
        added = await client.patch(
            f"/v1/custom-agents/{agent_id}",
            headers=headers,
            json={"members": [lead, researcher], "version": 1},
        )
        assert added.status_code == 200, added.text
        assert added.json()["version"] == 2
        trimmed = await client.patch(
            f"/v1/custom-agents/{agent_id}",
            headers=headers,
            json={"members": [lead], "version": 2},
        )
        assert trimmed.status_code == 200, trimmed.text
        assert trimmed.json()["members"] == [lead]
        assert trimmed.json()["version"] == 3
        downloaded = (
            await client.get(f"/v1/custom-agents/{agent_id}/contents", headers=headers)
        ).content

    with tarfile.open(fileobj=io.BytesIO(downloaded)) as archive:
        assert not any(member.name.startswith("agents/researcher") for member in archive)
    root = yaml.safe_load(members(downloaded)["config.yaml"][0])
    assert "agents" not in root["tools"]
    assert root["spawn"] is True


@pytest.mark.asyncio
async def test_patch_members_detaches_aliased_executor(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle("""spec_version: 1
name: custom-reviewer
description: Lead reviewer
executor: &exec {type: omnigent, model: old-model, config: {harness: codex}}
params:
  original: *exec
""")
    headers = {"x-test-user": "alice"}
    roster = [
        roster_member(
            "custom-reviewer",
            lead=True,
            harness="claude-sdk",
            model="new-model",
            description="Lead reviewer",
        )
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents", headers=headers, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        response = await client.patch(
            f"/v1/custom-agents/{created.json()['id']}",
            headers=headers,
            json={"members": roster, "version": 1},
        )
        assert response.status_code == 200, response.text
        downloaded = (
            await client.get(f"/v1/custom-agents/{created.json()['id']}/contents", headers=headers)
        ).content

    parsed = yaml.load(members(downloaded)["config.yaml"][0], Loader=_ConfigYamlLoader)
    assert parsed["executor"]["model"] == "new-model"
    assert parsed["params"]["original"]["model"] == "old-model"
    assert parsed["params"]["original"]["config"]["harness"] == "codex"


@pytest.mark.asyncio
async def test_patch_members_keeps_unrelated_block_style_tools_keys(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle(
        """spec_version: 1
name: custom-reviewer
description: Lead reviewer
executor:
  type: omnigent
  model: lead-model
  config:
    harness: codex
tools:
  agents:
    - researcher
  remote:
    type: mcp
    url: https://example.invalid/mcp
""",
        """spec_version: 1
name: researcher
description: Research support
executor:
  type: omnigent
  model: research-model
  config:
    harness: claude-sdk
""",
    )
    headers = {"x-test-user": "alice"}
    roster = [
        roster_member(
            "custom-reviewer",
            lead=True,
            harness="codex",
            model="lead-model",
            description="Lead reviewer",
        ),
        roster_member(
            "researcher",
            harness="claude-sdk",
            model="research-model",
            description="Research support",
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents", headers=headers, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        response = await client.patch(
            f"/v1/custom-agents/{created.json()['id']}",
            headers=headers,
            json={"members": roster, "version": 1},
        )
        assert response.status_code == 200, response.text
        downloaded = (
            await client.get(f"/v1/custom-agents/{created.json()['id']}/contents", headers=headers)
        ).content

    root = yaml.safe_load(members(downloaded)["config.yaml"][0])
    assert root["tools"]["agents"] == ["researcher"]
    assert root["tools"]["remote"] == {"type": "mcp", "url": "https://example.invalid/mcp"}
    assert validate_agent_bundle(downloaded).tools.agents == ["researcher"]


@pytest.mark.asyncio
async def test_patch_members_removes_legacy_sub_agent_llm_model(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle(
        """spec_version: 1
name: custom-reviewer
description: Lead reviewer
executor: {type: omnigent, model: lead-model, config: {harness: codex}}
""",
        """spec_version: 1
name: researcher
description: Research support
executor: {type: omnigent, config: {harness: codex}}
llm: {model: old-model, reasoning_effort: high}
""",
    )
    headers = {"x-test-user": "alice"}
    roster = [
        roster_member(
            "custom-reviewer",
            lead=True,
            harness="codex",
            model="lead-model",
            description="Lead reviewer",
        ),
        roster_member("researcher", harness="codex", description="Research support"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents", headers=headers, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        response = await client.patch(
            f"/v1/custom-agents/{created.json()['id']}",
            headers=headers,
            json={"members": roster, "version": 1},
        )
        assert response.status_code == 200, response.text
        assert response.json()["members"][1]["model"] is None
        assert response.json()["members"][1]["reasoning_effort"] is None
        downloaded = (
            await client.get(f"/v1/custom-agents/{created.json()['id']}/contents", headers=headers)
        ).content

    sub = yaml.safe_load(members(downloaded)["agents/researcher/config.yaml"][0])
    assert "llm" not in sub
    assert "model" not in sub["executor"]
    assert "reasoning_effort" not in sub["executor"]


@pytest.mark.asyncio
async def test_patch_members_keeps_legacy_root_llm_block_valid(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle("""spec_version: 1
name: custom-reviewer
description: Lead reviewer
executor: {type: omnigent, config: {harness: codex}}
llm: {model: old-model, temperature: 0.7}
""")
    headers = {"x-test-user": "alice"}
    roster = [
        roster_member(
            "custom-reviewer",
            lead=True,
            harness="codex",
            model="new-model",
            description="Lead reviewer",
        )
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents", headers=headers, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        response = await client.patch(
            f"/v1/custom-agents/{created.json()['id']}",
            headers=headers,
            json={"members": roster, "version": 1},
        )
        assert response.status_code == 200, response.text
        assert [member["model"] for member in response.json()["members"]] == ["new-model"]
        downloaded = (
            await client.get(f"/v1/custom-agents/{created.json()['id']}/contents", headers=headers)
        ).content

    root = yaml.load(members(downloaded)["config.yaml"][0], Loader=_ConfigYamlLoader)
    assert root["llm"] == {"model": "new-model", "temperature": 0.7}
    assert validate_agent_bundle(downloaded).executor.model == "new-model"


@pytest.mark.asyncio
async def test_patch_members_rejects_clearing_legacy_root_llm_model(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle("""spec_version: 1
name: custom-reviewer
description: Lead reviewer
executor: {type: omnigent, config: {harness: codex}}
llm: {model: old-model, temperature: 0.7}
""")
    headers = {"x-test-user": "alice"}
    roster = [
        roster_member("custom-reviewer", lead=True, harness="codex", description="Lead reviewer")
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents", headers=headers, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]
        response = await client.patch(
            f"/v1/custom-agents/{agent_id}",
            headers=headers,
            json={"members": roster, "version": 1},
        )
        contents = await client.get(f"/v1/custom-agents/{agent_id}/contents", headers=headers)

    assert response.status_code == 400, response.text
    message = response.json()["error"]["message"]
    assert "custom-reviewer" in message
    assert "legacy llm block requires a model" in message
    assert contents.content == original
    assert CustomAgentsStore(db_uri).get("alice", agent_id)["version"] == 1


@pytest.mark.asyncio
async def test_patch_members_matches_sub_agent_by_yaml_name(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle(
        """spec_version: 1
name: custom-reviewer
description: Lead reviewer
executor: {type: omnigent, model: lead-model, config: {harness: codex}}
""",
        extra={
            "agents/research-dir/config.yaml": b"""spec_version: 1
name: researcher
description: Research support
executor: {type: omnigent, model: old-model, config: {harness: codex}}
""",
            "agents/research-dir/notes.md": b"Keep this file\n",
        },
    )
    headers = {"x-test-user": "alice"}
    roster = [
        roster_member(
            "custom-reviewer",
            lead=True,
            harness="codex",
            model="lead-model",
            description="Lead reviewer",
        ),
        roster_member(
            "researcher",
            harness="claude-sdk",
            model="new-model",
            description="Research support",
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents", headers=headers, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        response = await client.patch(
            f"/v1/custom-agents/{created.json()['id']}",
            headers=headers,
            json={"members": roster, "version": 1},
        )
        assert response.status_code == 200, response.text
        downloaded = (
            await client.get(f"/v1/custom-agents/{created.json()['id']}/contents", headers=headers)
        ).content

    after = members(downloaded)
    assert "agents/researcher/config.yaml" not in after
    assert after["agents/research-dir/notes.md"][0] == b"Keep this file\n"
    sub = yaml.safe_load(after["agents/research-dir/config.yaml"][0])
    assert sub["name"] == "researcher"
    assert sub["description"] == "Research support"
    assert sub["executor"]["model"] == "new-model"
    assert sub["executor"]["config"]["harness"] == "claude-sdk"


@pytest.mark.asyncio
async def test_patch_members_moves_new_role_off_retained_directory(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle(
        """spec_version: 1
name: custom-reviewer
description: Lead reviewer
executor: {type: omnigent, model: lead-model, config: {harness: codex}}
""",
        extra={
            "agents/research-dir/config.yaml": b"""spec_version: 1
name: researcher
description: Research support
executor: {type: omnigent, model: old-model, config: {harness: codex}}
params:
  keep: me
""",
            "agents/research-dir/notes.md": b"Keep this file\n",
        },
    )
    headers = {"x-test-user": "alice"}
    roster = [
        roster_member(
            "custom-reviewer",
            lead=True,
            harness="codex",
            model="lead-model",
            description="Lead reviewer",
        ),
        roster_member(
            "researcher",
            harness="claude-sdk",
            model="new-model",
            description="Research support",
        ),
        roster_member(
            "research-dir",
            harness="codex",
            model="dir-model",
            description="Directory support",
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents", headers=headers, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        response = await client.patch(
            f"/v1/custom-agents/{created.json()['id']}",
            headers=headers,
            json={"members": roster, "version": 1},
        )
        assert response.status_code == 200, response.text
        assert response.json()["members"] == roster
        downloaded = (
            await client.get(f"/v1/custom-agents/{created.json()['id']}/contents", headers=headers)
        ).content

    after = members(downloaded)
    assert after["agents/research-dir/notes.md"][0] == b"Keep this file\n"
    retained = yaml.safe_load(after["agents/research-dir/config.yaml"][0])
    assert retained["name"] == "researcher"
    assert retained["params"] == {"keep": "me"}
    assert retained["executor"]["config"]["harness"] == "claude-sdk"
    assert retained["executor"]["model"] == "new-model"
    added = yaml.safe_load(after["agents/research-dir-2/config.yaml"][0])
    assert added["name"] == "research-dir"
    assert added["executor"]["config"]["harness"] == "codex"
    assert added["executor"]["model"] == "dir-model"
    assert validate_agent_bundle(downloaded).tools.agents == ["researcher", "research-dir"]


@pytest.mark.asyncio
async def test_patch_members_reuses_dropped_role_directory(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle(
        """spec_version: 1
name: custom-reviewer
description: Lead reviewer
executor: {type: omnigent, model: lead-model, config: {harness: codex}}
""",
        extra={
            "agents/old-dir/config.yaml": b"""spec_version: 1
name: old
description: Old support
executor: {type: omnigent, model: old-model, config: {harness: codex}}
""",
            "agents/old-dir/extra.txt": b"Drop this file\n",
        },
    )
    headers = {"x-test-user": "alice"}
    roster = [
        roster_member(
            "custom-reviewer",
            lead=True,
            harness="codex",
            model="lead-model",
            description="Lead reviewer",
        ),
        roster_member(
            "old-dir",
            harness="codex",
            model="dir-model",
            description="Directory support",
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents", headers=headers, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        response = await client.patch(
            f"/v1/custom-agents/{created.json()['id']}",
            headers=headers,
            json={"members": roster, "version": 1},
        )
        assert response.status_code == 200, response.text
        downloaded = (
            await client.get(f"/v1/custom-agents/{created.json()['id']}/contents", headers=headers)
        ).content

    after = members(downloaded)
    assert {name for name in after if name.startswith("agents/old-dir/")} == {
        "agents/old-dir/config.yaml"
    }
    added = yaml.safe_load(after["agents/old-dir/config.yaml"][0])
    assert added["name"] == "old-dir"
    assert added["executor"]["model"] == "dir-model"
    assert validate_agent_bundle(downloaded).tools.agents == ["old-dir"]


@pytest.mark.asyncio
async def test_patch_members_preserves_request_order(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle()
    headers = {"x-test-user": "alice"}
    roster = [
        roster_member(
            "custom-reviewer",
            lead=True,
            harness="codex",
            model="lead-model",
            description="Original description",
        ),
        roster_member("zulu", harness="codex", model="zulu-model", description="Zulu support"),
        roster_member("alpha", harness="codex", model="alpha-model", description="Alpha support"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents", headers=headers, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        response = await client.patch(
            f"/v1/custom-agents/{created.json()['id']}",
            headers=headers,
            json={"members": roster, "version": 1},
        )

    assert response.status_code == 200, response.text
    assert response.json()["members"] == roster


@pytest.mark.asyncio
async def test_private_crud_archive_and_existing_session_survive(
    db_uri: str,
    tmp_path: Path,
    runtime_init: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATALOG_TEST_TOKEN", "must-not-expand")
    app, artifacts, agents, conversations, permissions = make_app(db_uri, tmp_path)
    alice, bob = {"x-test-user": "alice"}, {"x-test-user": "bob"}
    original = bundle()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/custom-agents")).status_code == 401
        created = await client.post(
            "/v1/custom-agents", headers=alice, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        row = created.json()
        assert row["instructions"] == "Original instructions"
        path = f"/v1/custom-agents/{row['id']}"
        assert (await client.get("/v1/agents", headers=alice)).json()["data"] == []
        assert (await client.get("/v1/custom-agents", headers=bob)).json()["data"] == []
        for suffix in ("", "/contents"):
            assert (await client.get(path + suffix, headers=bob)).status_code == 404
        assert (await client.patch(path, headers=bob, json={"name": "stolen"})).status_code == 404
        assert (await client.delete(path, headers=bob)).status_code == 404
        assert (await client.get(path + "/contents", headers=alice)).content == original

        # A real runtime snapshot uses its own agent id and retained bundle key.
        runtime_id = generate_agent_id()
        location = bundle_location(runtime_id, original)
        artifacts.put(location, original)
        snapshot = conversations.create_session_with_agent(
            agent_id=runtime_id,
            agent_name="custom-reviewer",
            agent_bundle_location=location,
            agent_description=None,
            labels={"omnigent:agent-template-id": row["id"]},
        )
        permissions.grant("alice", snapshot.conversation.id, LEVEL_OWNER)
        edited = await client.patch(
            path,
            headers=alice,
            json={
                "name": "Renamed",
                "description": None,
                "instructions": "assets/data.bin",
                "version": 1,
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["instructions"] == "assets/data.bin"
        assert edited.json()["version"] == 2
        downloaded = (await client.get(path + "/contents", headers=alice)).content
        before, after = members(original), members(downloaded)
        for name in before.keys() - {"config.yaml"}:
            assert after[name] == before[name]
        assert b"${CATALOG_TEST_TOKEN}" in after["config.yaml"][0]
        assert b"must-not-expand" not in downloaded
        assert (
            await client.patch(path, headers=alice, json={"name": "stale", "version": 1})
        ).status_code == 409
        assert (await client.delete(path, headers=alice)).status_code == 204
        assert (await client.get(path, headers=alice)).status_code == 404
        assert (await client.get("/v1/custom-agents", headers=alice)).json()["data"] == []
        runtime = agents.get(runtime_id)
        assert runtime is not None and artifacts.get(runtime.bundle_location) == original
        existing = await client.get(
            f"/v1/sessions/{snapshot.conversation.id}/agent/contents", headers=alice
        )
        assert existing.status_code == 200 and existing.content == original
        assert existing.headers["x-agent-session-scoped"] == "true"


@pytest.mark.asyncio
async def test_import_requires_owner_and_retains_archive(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, artifacts, agents, conversations, permissions = make_app(db_uri, tmp_path)
    original = joint_bundle()
    runtime_id = generate_agent_id()
    location = bundle_location(runtime_id, original)
    artifacts.put(location, original)
    snapshot = conversations.create_session_with_agent(
        agent_id=runtime_id,
        agent_name="custom-reviewer",
        agent_bundle_location=location,
        agent_description=None,
    )
    session_id = snapshot.conversation.id
    permissions.grant("alice", session_id, LEVEL_OWNER)
    permissions.grant("bob", session_id, LEVEL_READ)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        payload = {"source_session_id": session_id}
        assert (
            await client.post("/v1/custom-agents", headers={"x-test-user": "bob"}, json=payload)
        ).status_code == 403
        created = await client.post(
            "/v1/custom-agents", headers={"x-test-user": "alice"}, json=payload
        )
        assert created.status_code == 201, created.text
        assert created.json()["members"][1]["reasoning_effort"] == "medium"
        contents = await client.get(
            f"/v1/custom-agents/{created.json()['id']}/contents", headers={"x-test-user": "alice"}
        )
        assert contents.content == original
        assert agents.get(runtime_id) is not None
        template_id = created.json()["id"]
        assert (
            conversations.get_conversation(session_id).labels["omnigent:agent-template-id"]
            == template_id
        )
        deleted = await client.delete(
            f"/v1/custom-agents/{template_id}", headers={"x-test-user": "alice"}
        )
        assert deleted.status_code == 204
        assert (
            conversations.get_conversation(session_id).labels["omnigent:agent-template-id"]
            == template_id
        )
        assert artifacts.get(location) == original
        bad_type = await client.post(
            "/v1/custom-agents",
            headers={"x-test-user": "alice", "content-type": "text/plain"},
            content="{}",
        )
        assert bad_type.status_code == 415


def test_patch_block_scalar_keeps_following_yaml_and_empty_instructions() -> None:
    original = bundle("""spec_version: 1
name: test
description: |
  Long description
executor:
  type: omnigent
  config:
    harness: codex
instructions: prompts/custom.md
""")
    updated = patch_bundle(original, {"description": "Short", "instructions": None})
    spec = validate_agent_bundle(updated)
    assert spec.description == "Short" and spec.instructions == ""
    assert spec.executor.harness_kind == "codex"


@pytest.mark.parametrize("trailing_comma", [False, True])
def test_patch_flow_root_adds_fields_inside_mapping(trailing_comma: bool) -> None:
    config = (
        "{spec_version: 1, name: custom-reviewer, executor: "
        "{type: omnigent, config: {harness: codex}}"
        + (", # Keep this comment\n" if trailing_comma else "")
        + "}\n"
    )
    original = bundle(config)
    updated = patch_bundle(
        original, {"description": "New description", "instructions": "New text"}
    )
    spec = validate_agent_bundle(updated)
    assert spec.description == "New description" and spec.instructions == "New text"
    before, after = members(original), members(updated)
    for name in before.keys() - {"config.yaml"}:
        assert after[name] == before[name]


@pytest.mark.parametrize("flow", [False, True])
def test_patch_anchored_scalar_preserves_unrelated_alias_values(flow: bool) -> None:
    config = (
        "{spec_version: 1, name: &title custom-reviewer, description: *title, "
        "executor: {type: omnigent, model: *title, config: {harness: codex}}, "
        "instructions: 'Keep ${UNEXPANDED_VALUE}'}\n"
        if flow
        else "spec_version: 1\nname: &title custom-reviewer\ndescription: *title\n"
        "executor:\n  type: omnigent\n  model: *title\n  config:\n    harness: codex\n"
        "instructions: 'Keep ${UNEXPANDED_VALUE}'\n"
    )
    updated = patch_bundle(bundle(config), {"name": "new-name"})
    spec = validate_agent_bundle(updated)
    assert spec.name == "new-name"
    assert spec.description == "custom-reviewer"
    assert spec.executor.model == "custom-reviewer"
    assert spec.instructions == "Keep ${UNEXPANDED_VALUE}"


def test_patch_alias_value_and_document_end_preserve_other_fields() -> None:
    original = bundle("""spec_version: 1
name: &title custom-reviewer
description: *title
executor:
  type: omnigent
  config:
    harness: codex
...
""")
    updated = patch_bundle(original, {"description": "Changed", "instructions": "Added"})
    spec = validate_agent_bundle(updated)
    assert spec.name == "custom-reviewer" and spec.description == "Changed"
    assert spec.instructions == "Added"
    parsed = yaml.safe_load(members(updated)["config.yaml"][0])
    assert parsed["executor"] == {"type": "omnigent", "config": {"harness": "codex"}}


@pytest.mark.asyncio
async def test_template_identity_survives_clones_updates_and_same_name_uploads(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, agents, conversations, permissions = make_app(db_uri, tmp_path)
    template_id = generate_agent_id()
    old_location = f"{template_id}/original-hash"
    template = agents.create(template_id, "codex-sdk", old_location)
    clone_id = generate_agent_id()
    clone = conversations.create_session_with_agent(
        agent_id=clone_id,
        agent_name=template.name,
        agent_bundle_location=old_location,
        agent_description=None,
    )
    private_id = generate_agent_id()
    private = conversations.create_session_with_agent(
        agent_id=private_id,
        agent_name=template.name,
        agent_bundle_location=f"{private_id}/private-hash",
        agent_description=None,
    )
    invalid_id = generate_agent_id()
    conversations.create_session_with_agent(
        agent_id=invalid_id,
        agent_name="legacy",
        agent_bundle_location="legacy-non-uuid/hash",
        agent_description=None,
    )
    agents.update(template_id, f"{template_id}/new-version-hash")
    assert agents.get_template_ids([template_id, clone_id, private_id, invalid_id]) == {
        template_id: template_id,
        clone_id: template_id,
    }
    permissions.ensure_user("alice")
    for session in [clone, private]:
        permissions.grant("alice", session.conversation.id, LEVEL_OWNER)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/sessions", headers={"x-test-user": "alice"})
        assert response.status_code == 200, response.text
        rows = {row["id"]: row for row in response.json()["data"]}
        assert rows[clone.conversation.id]["agent_template_id"] == template_id
        assert rows[private.conversation.id].get("agent_template_id") is None


@pytest.mark.asyncio
async def test_detail_includes_template_identity_for_pinned_backfill(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _artifacts, agents, conversations, permissions = make_app(db_uri, tmp_path)
    template_id = generate_agent_id()
    agents.create(template_id, "codex-sdk", f"{template_id}/old-hash")
    cloned = conversations.create_session_with_agent(
        agent_id=generate_agent_id(),
        agent_name="codex-sdk",
        agent_bundle_location=f"{template_id}/old-hash",
        agent_description=None,
    )
    permissions.ensure_user("alice")
    permissions.grant("alice", cloned.conversation.id, LEVEL_OWNER)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/v1/sessions/{cloned.conversation.id}", headers={"x-test-user": "alice"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["agent_template_id"] == template_id


def test_patch_replaces_pax_sized_member_without_truncation() -> None:
    raw = b"""spec_version: 1
name: old
executor:
  type: omnigent
  config:
    harness: codex
"""
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo("config.yaml")
        info.size = len(raw)
        info.pax_headers = {"size": str(len(raw))}
        archive.addfile(info, io.BytesIO(raw))

    new_name = "a-name-longer-than-the-original-pax-sized-config-member"
    updated = patch_bundle(archive_bytes.getvalue(), {"name": new_name})

    assert validate_agent_bundle(updated).name == new_name
    config = members(updated)["config.yaml"][0]
    assert yaml.safe_load(config)["name"] == new_name


def test_repeated_instruction_edits_reuse_generated_member() -> None:
    once = patch_bundle(bundle(), {"instructions": "First revision"})
    twice = patch_bundle(once, {"instructions": "Second revision"})

    generated = [name for name in members(twice) if name.startswith("catalog-instructions-")]
    assert len(generated) == 1
    assert members(twice)[generated[0]][0] == b"Second revision"
    assert validate_agent_bundle(twice).instructions == "Second revision"


@pytest.mark.asyncio
async def test_chunked_multipart_stops_reading_after_request_limit(
    db_uri: str,
    tmp_path: Path,
    runtime_init: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    monkeypatch.setattr(custom_agents_routes, "MAX_MULTIPART_REQUEST_BYTES", 1024)
    boundary = "catalog-boundary"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="bundle"; filename="agent.tar.gz"\r\n'
        "Content-Type: application/gzip\r\n\r\n"
    ).encode()
    chunks = [prefix, *([b"x" * 256] * 20), f"\r\n--{boundary}--\r\n".encode()]
    yielded = 0

    async def stream() -> AsyncIterator[bytes]:
        nonlocal yielded
        for chunk in chunks:
            yielded += 1
            yield chunk

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/custom-agents",
            headers={
                "x-test-user": "alice",
                "content-type": f"multipart/form-data; boundary={boundary}",
            },
            content=stream(),
        )

    assert response.status_code == 413
    assert yielded < len(chunks)


@pytest.mark.asyncio
async def test_detail_validates_bundle_only_once_per_artifact(
    db_uri: str,
    tmp_path: Path,
    runtime_init: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle()
    agent_id = "ca_cache_probe"
    location = bundle_location(agent_id, original)
    artifacts.put(location, original)
    CustomAgentsStore(db_uri).create(
        "alice",
        {
            "id": agent_id,
            "name": "custom-reviewer",
            "description": "Original description",
            "harness": "codex",
            "model": "test-model",
            "bundle_location": location,
        },
    )
    original_get = artifacts.get
    reads = 0

    def counted_get(key: str) -> bytes:
        nonlocal reads
        reads += 1
        return original_get(key)

    monkeypatch.setattr(artifacts, "get", counted_get)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/v1/custom-agents/{agent_id}"
        first = await client.get(path, headers={"x-test-user": "alice"})
        second = await client.get(path, headers={"x-test-user": "alice"})

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["instructions"] == "Original instructions"
    assert reads == 1


@pytest.mark.asyncio
async def test_same_content_patch_cas_keeps_winner_bundle(
    db_uri: str,
    tmp_path: Path,
    runtime_init: None,
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    headers = {"x-test-user": "alice"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents",
            headers=headers,
            files={"bundle": ("agent.tar.gz", bundle())},
        )
        assert created.status_code == 201
        path = f"/v1/custom-agents/{created.json()['id']}"
        payload = {"name": "Concurrent_winner", "version": 1}
        results = await asyncio.gather(
            client.patch(path, headers=headers, json=payload),
            client.patch(path, headers=headers, json=payload),
        )

        assert sorted(response.status_code for response in results) == [200, 409], [
            (response.status_code, response.text) for response in results
        ]
        contents = await client.get(path + "/contents", headers=headers)
        assert contents.status_code == 200
        assert validate_agent_bundle(contents.content).name == "Concurrent_winner"


@pytest.mark.asyncio
async def test_custom_upload_rejects_untrusted_browser_origin(
    db_uri: str, tmp_path: Path, runtime_init: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for origin, expected in [
            ("https://untrusted.invalid", 403),
            ("http://localhost:5187", 201),
        ]:
            response = await client.post(
                "/v1/custom-agents",
                headers={"x-test-user": "alice", "origin": origin},
                files={"bundle": ("agent.tar.gz", bundle())},
            )
            assert response.status_code == expected, response.text
