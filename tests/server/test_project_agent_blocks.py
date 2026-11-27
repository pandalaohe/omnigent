"""Unit and route tests for project blocked_agents, D5/D6/D1 invariants, fork & first-bind."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.calling_defaults import resolve_calling
from omnigent.entities import Conversation, Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.project_agent_blocks import (
    assert_agent_allowed,
    blocked_agents,
    check_default_agents_not_blocked,
    validate_blocked_agents,
)
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store import FORK_SOURCE_LABEL_KEY
from omnigent.stores.host_store import HostStore
from tests.server.routes.test_calling_defaults_routes import AGENT_ID, _bundle
from tests.server.routes.test_project_collaboration_routes import (
    _HOST_A,
    _HOST_B,
    _build_app,
    _make_project,
)

AGENT_B = "287b7cb7ac30abf4debfaa578d052ec8"
AGENT_CLAUDE = "187b7cb7ac30abf4debfaa578d052ec7"


# ── Pure unit tests: shape, union, D5, D6 harness gate ───────────────────────


def test_validate_blocked_agents_normalizes_and_rejects_bad_shapes() -> None:
    """Scenario 11: shape validation, empty-list drop, duplicate/length/count rejection."""
    assert validate_blocked_agents({}) == {}
    assert validate_blocked_agents({"*": ["ag_a"], "h1": []}) == {"*": ["ag_a"]}

    # UUID spelling normalization: bare, dashed, and ag_/host_ prefixed forms compare equal.
    dashed_agent = "187b7cb7-ac30-abf4-debf-aa578d052ec7"
    assert validate_blocked_agents({"*": [dashed_agent]}) == {"*": [AGENT_CLAUDE]}
    with pytest.raises(OmnigentError) as dup_exc:
        validate_blocked_agents({"*": [AGENT_CLAUDE, f"ag_{AGENT_CLAUDE}"]})
    assert dup_exc.value.code == ErrorCode.INVALID_INPUT

    for bad in (
        "not-a-dict",
        {"h1": "ag_a"},
        {"": ["ag_a"]},
        {"h1": [""]},
        {"h1": ["   "]},
        {"h1": ["a" * 201]},
        {"h1": ["ag_a", "ag_a"]},
        {"h1": [f"ag_{i}" for i in range(65)]},
        {f"h_{i}": ["ag_a"] for i in range(65)},
    ):
        with pytest.raises(OmnigentError) as exc:
            validate_blocked_agents(bad)
        assert exc.value.code == ErrorCode.INVALID_INPUT


def test_blocked_agents_union_and_none_host_and_joint_agent_skip() -> None:
    """Scenarios 2, 3, 4 & D3: '*' union host list, host=None checks '*', ca_ ignored."""
    cfg = {"blocked_agents": {"*": ["ag_pi"], "host-a": ["ag_ag"]}}
    assert blocked_agents(cfg, "host-a") == frozenset({"ag_pi", "ag_ag"})
    assert blocked_agents(cfg, "host-b") == frozenset({"ag_pi"})
    assert blocked_agents(cfg, None) == frozenset({"ag_pi"})

    project = Project(id="p1", name="Alpha", user_id=None, created_at=1, config=cfg)
    assert_agent_allowed(project=project, host_id="host-b", agent_id="ag_ag")
    assert_agent_allowed(project=project, host_id=None, agent_id="ag_ag")
    assert_agent_allowed(project=project, host_id="host-a", agent_id="ca_joint_1")
    with pytest.raises(OmnigentError) as exc:
        assert_agent_allowed(
            project=project,
            host_id="host-a",
            agent_id="ag_ag",
            agent_label="Antigravity",
            host_label="HostA",
        )
    assert exc.value.code == ErrorCode.AGENT_BLOCKED_FOR_PROJECT
    assert "Alpha" in exc.value.message
    assert "Antigravity" in exc.value.message
    assert "HostA" in exc.value.message


def test_check_default_agents_not_blocked_invariant() -> None:
    """Scenario 10: D5 rejects host default or legacy agent in blocked_agents."""
    # Per-host default blocked on same host
    with pytest.raises(OmnigentError) as exc1:
        check_default_agents_not_blocked(
            {
                "calling_defaults": {"host-a": {"agent_id": "ag_ag"}},
                "blocked_agents": {"host-a": ["ag_ag"]},
            }
        )
    assert exc1.value.code == ErrorCode.INVALID_INPUT
    assert "host-a" in exc1.value.message and "ag_ag" in exc1.value.message

    # Legacy agent blocked on '*'
    with pytest.raises(OmnigentError) as exc2:
        check_default_agents_not_blocked({"agent_id": "ag_ag", "blocked_agents": {"*": ["ag_ag"]}})
    assert exc2.value.code == ErrorCode.INVALID_INPUT
    assert "ag_ag" in exc2.value.message

    # Legacy agent inherited by host-a when host-a blocks it
    with pytest.raises(OmnigentError) as exc3:
        check_default_agents_not_blocked(
            {"agent_id": "ag_ag", "blocked_agents": {"host-a": ["ag_ag"]}}
        )
    assert exc3.value.code == ErrorCode.INVALID_INPUT

    # Different UUID spellings for the same agent or host still trigger D5, even with a
    # non-blocked legacy agent_id present on the project.
    dashed_host = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    dashed_agent = "187b7cb7-ac30-abf4-debf-aa578d052ec7"
    with pytest.raises(OmnigentError) as exc4:
        check_default_agents_not_blocked(
            {
                "agent_id": AGENT_B,
                "calling_defaults": {f"host_{'a' * 32}": {"agent_id": AGENT_CLAUDE}},
                "blocked_agents": {dashed_host: [dashed_agent]},
            }
        )
    assert exc4.value.code == ErrorCode.INVALID_INPUT


def test_resolve_calling_d6_harness_gate() -> None:
    """Scenario 12: only the default agent's own harness row applies; others read master."""
    harnesses = {"ag_claude": "claude-native", "ag_codex": "codex-native"}
    project_config = {
        "calling_defaults": {
            "host-a": {
                "agent_id": "ag_claude",
                "harnesses": {
                    "claude-native": {"model": "opus"},
                    "codex-native": {"model": "stale-codex-model"},
                },
            }
        }
    }
    master = {"host-a": {"codex-native": {"model": "master-codex"}}}

    # Default agent gets its project row
    res_default = resolve_calling(
        explicit={"agent_id": "ag_claude"},
        explicit_fields={"agent_id"},
        project_config=project_config,
        master=master,
        host_id="host-a",
        agent_harness=harnesses.get,
    )
    assert res_default.model == "opus"
    assert res_default.sources["model"] == "project_host"

    # Non-default agent ignores stale project row and gets master
    res_other = resolve_calling(
        explicit={"agent_id": "ag_codex"},
        explicit_fields={"agent_id"},
        project_config=project_config,
        master=master,
        host_id="host-a",
        agent_harness=harnesses.get,
    )
    assert res_other.model == "master-codex"
    assert res_other.sources["model"] == "master"

    # Default agent with harness_override != default harness ignores stale row
    res_override = resolve_calling(
        explicit={"agent_id": "ag_claude", "harness_override": "codex-native"},
        explicit_fields={"agent_id", "harness_override"},
        project_config=project_config,
        master=master,
        host_id="host-a",
        agent_harness=harnesses.get,
    )
    assert res_override.model == "master-codex"
    assert res_override.sources["model"] == "master"


# ── Route & service integration tests ────────────────────────────────────────


@pytest.fixture()
def rework_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    app = _build_app(db_uri, tmp_path, preferences=True, catalogs=True)
    hosts = HostStore(db_uri)
    for host_id in (_HOST_A, _HOST_B):
        hosts.upsert_on_connect(host_id, f"Host-{host_id[-2:]}", "local")
        hosts.update_harness_readiness(host_id, {"claude-native": True, "codex-native": True})
        hosts.set_offline(host_id)
        app.state.host_model_catalog_cache_store.upsert(
            host_id, "claude-native", [{"id": "opus"}], 100
        )
        app.state.host_model_catalog_cache_store.upsert(
            host_id, "codex-native", [{"id": "gpt-6-sol"}], 100
        )
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    artifacts.put("agent/claude", _bundle("claude-native"))
    artifacts.put("agent/codex", _bundle("codex-native"))
    app.state.agent_store.create(AGENT_CLAUDE, "Claude Code", "agent/claude")
    app.state.agent_store.create(AGENT_ID, "Codex", "agent/codex")

    @app.middleware("http")
    async def _tag_runner_purpose(request: Any, call_next: Any) -> Any:
        if request.headers.get("x-test-runner-purpose") == "1":
            request.state.auth_purpose = "runner"
        return await call_next(request)

    return app


@pytest.mark.asyncio
async def test_patch_project_validation_d5_d1_and_agent_tool_guard(rework_app: FastAPI) -> None:
    """Scenarios 10, 11, 14, 31: PATCH validates blocked_agents, D5, orchestration, agent guard."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rework_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)

        # Scenario 10: D5 conflict → 400
        r10 = await client.patch(
            f"/v1/projects/{project_id}",
            json={
                "config": {
                    "calling_defaults": {_HOST_A: {"agent_id": AGENT_ID}},
                    "blocked_agents": {_HOST_A: [AGENT_ID]},
                }
            },
        )
        assert r10.status_code == 400
        assert _HOST_A in r10.text and AGENT_ID in r10.text

        # Scenario 11: bad blocked_agents shape → 400
        r11 = await client.patch(
            f"/v1/projects/{project_id}",
            json={"config": {"blocked_agents": {_HOST_A: AGENT_ID}}},
        )
        assert r11.status_code == 400

        # Scenario 14: bad child_cap, 6 exceptions → 400; child_cap=150 → 200 stored
        assert (
            await client.patch(
                f"/v1/projects/{project_id}",
                json={"config": {"orchestration": {"child_cap": "abc"}}},
            )
        ).status_code == 400
        assert (
            await client.patch(
                f"/v1/projects/{project_id}",
                json={
                    "config": {
                        "orchestration": {
                            "standing_approval": {"exceptions": [f"ex-{i}" for i in range(6)]}
                        }
                    }
                },
            )
        ).status_code == 400
        r14_ok = await client.patch(
            f"/v1/projects/{project_id}",
            json={
                "config": {
                    "orchestration": {"child_cap": 150},
                    "blocked_agents": {_HOST_A: [AGENT_ID]},
                }
            },
        )
        assert r14_ok.status_code == 200, r14_ok.text
        assert r14_ok.json()["config"]["orchestration"]["child_cap"] == 150

        # Scenario 31: orchestration-source header changing blocked_agents or orchestration → 403
        stored_cfg = r14_ok.json()["config"]
        r31_block = await client.patch(
            f"/v1/projects/{project_id}",
            headers={ORCHESTRATION_SOURCE_HEADER: "sess_caller"},
            json={"config": {**stored_cfg, "blocked_agents": {_HOST_A: [AGENT_CLAUDE]}}},
        )
        assert r31_block.status_code == 403

        r31_orch = await client.patch(
            f"/v1/projects/{project_id}",
            headers={ORCHESTRATION_SOURCE_HEADER: "sess_caller"},
            json={"config": {**stored_cfg, "orchestration": {"child_cap": 2}}},
        )
        assert r31_orch.status_code == 403

        # Scenario 31: icon-only round trip with orchestration-source header → 200
        r31_icon = await client.patch(
            f"/v1/projects/{project_id}",
            headers={ORCHESTRATION_SOURCE_HEADER: "sess_caller"},
            json={"config": {**stored_cfg, "icon": "rocket"}},
        )
        assert r31_icon.status_code == 200, r31_icon.text
        assert r31_icon.json()["config"]["icon"] == "rocket"

        # Scenario 31: runner callers without orchestration header → 403
        from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER

        r31_tunnel = await client.patch(
            f"/v1/projects/{project_id}",
            headers={RUNNER_TUNNEL_TOKEN_HEADER: "tok-runner"},
            json={"config": {**stored_cfg, "blocked_agents": {_HOST_A: [AGENT_CLAUDE]}}},
        )
        assert r31_tunnel.status_code == 403

        # auth_purpose="runner" (minted runner JWT) is also rejected on protected keys
        r31_jwt = await client.patch(
            f"/v1/projects/{project_id}",
            headers={"x-test-runner-purpose": "1"},
            json={"config": {**stored_cfg, "orchestration": {"child_cap": 3}}},
        )
        assert r31_jwt.status_code == 403

        # Verify UnifiedAuthProvider sets request.state.auth_purpose == "runner" for runner JWTs
        from omnigent.server.accounts_config import AccountsConfig
        from omnigent.server.auth import UnifiedAuthProvider

        accounts_cfg = AccountsConfig(
            cookie_secret=b"k" * 32,
            session_ttl_hours=1,
            base_url="http://localhost:8000",
            init_admin_password=None,
            invite_ttl_seconds=300,
            magic_ttl_seconds=300,
        )
        auth = UnifiedAuthProvider(source="accounts", accounts_config=accounts_cfg)
        runner_jwt = auth.mint_runner_token("alice@example.com", ttl_seconds=60)
        assert runner_jwt is not None
        fake_req = SimpleNamespace(
            cookies={},
            headers={"authorization": f"Bearer {runner_jwt}"},
            state=SimpleNamespace(),
            scope={"type": "http"},
        )
        assert auth.get_user_id(fake_req) == "alice@example.com"  # type: ignore[arg-type]
        assert fake_req.state.auth_purpose == "runner"


@pytest.mark.asyncio
async def test_settings_get_single_row_and_put_rejects_other_harness_and_d5(
    rework_app: FastAPI,
) -> None:
    """Scenarios 12 & 13: GET single row; PUT rejects other harness or host without default."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rework_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        url = f"/v1/projects/{project_id}/settings"

        # Scenario 13b: host with no default rejects any host.harnesses key
        rev0 = (await client.get(url)).json()["revision"]
        r13_none = await client.put(
            url,
            json={
                "expected_revision": rev0,
                "host": {
                    "host_id": _HOST_A,
                    "harnesses": {"codex-native": {"model": "gpt-6-sol"}},
                },
            },
        )
        assert r13_none.status_code == 400

        # Seed default Claude Code + stale codex-native row
        assert (
            await client.patch(
                f"/v1/projects/{project_id}",
                json={
                    "config": {
                        "calling_defaults": {
                            _HOST_A: {
                                "agent_id": AGENT_CLAUDE,
                                "harnesses": {
                                    "claude-native": {"model": "opus"},
                                    "codex-native": {"model": "gpt-6-sol"},
                                },
                            }
                        }
                    }
                },
            )
        ).status_code == 200

        # Scenario 12: GET /settings with harness=codex-native returns no row
        r12_get = await client.get(url, params={"host_id": _HOST_A, "harness": "codex-native"})
        assert r12_get.status_code == 200
        assert r12_get.json()["rows"] == []

        r12_claude = await client.get(url, params={"host_id": _HOST_A})
        assert [row["harness"] for row in r12_claude.json()["rows"]] == ["claude-native"]

        # Scenario 13a: PUT /settings with codex-native while default is Claude Code → 400
        rev1 = r12_claude.json()["revision"]
        r13_other = await client.put(
            url,
            json={
                "expected_revision": rev1,
                "host": {
                    "host_id": _HOST_A,
                    "harnesses": {"codex-native": {"model": "gpt-6-sol"}},
                },
            },
        )
        assert r13_other.status_code == 400

        # D5 on PUT /settings: setting host default agent to a blocked agent → 400
        assert (
            await client.patch(
                f"/v1/projects/{project_id}",
                json={
                    "config": {
                        "calling_defaults": {_HOST_A: {"agent_id": AGENT_CLAUDE}},
                        "blocked_agents": {_HOST_A: [AGENT_ID]},
                    }
                },
            )
        ).status_code == 200
        rev2 = (await client.get(url, params={"host_id": _HOST_A})).json()["revision"]
        r13_d5 = await client.put(
            url,
            json={
                "expected_revision": rev2,
                "host": {"host_id": _HOST_A, "agent_id": AGENT_ID},
            },
        )
        assert r13_d5.status_code == 400


@pytest.mark.asyncio
async def test_calling_defaults_patch_entries_put_d5_and_template_blocked(
    rework_app: FastAPI,
) -> None:
    """Scenarios 24, 26, 27: PATCH calling-defaults D5+409, entries PUT D5, template blocked."""
    rework_app.state.user_preferences_store.patch_namespace(
        "local", "project_host_agents", {_HOST_B: AGENT_ID}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rework_app), base_url="http://test"
    ) as client:
        project_id = await _make_project(client)
        saved = await client.patch(
            f"/v1/projects/{project_id}",
            json={"config": {"blocked_agents": {_HOST_A: [AGENT_ID], _HOST_B: [AGENT_ID]}}},
        )
        assert saved.status_code == 200
        rev = saved.json()["config_revision"]

        # Scenario 26: copy_agent=True with blocked agent → 400; stale revision → 409
        r26_d5 = await client.patch(
            f"/v1/projects/{project_id}/calling-defaults",
            json={
                "expected_revision": rev,
                "host_id": _HOST_A,
                "harness": "codex-native",
                "fields": {"model": "gpt-6-sol"},
                "copy_agent": True,
                "agent_id": AGENT_ID,
            },
        )
        assert r26_d5.status_code == 400
        assert _HOST_A in r26_d5.text and AGENT_ID in r26_d5.text

        r26_stale = await client.patch(
            f"/v1/projects/{project_id}/calling-defaults",
            json={
                "expected_revision": "0" * 64,
                "host_id": _HOST_A,
                "harness": "claude-native",
                "fields": {"model": "opus"},
                "copy_agent": True,
                "agent_id": AGENT_CLAUDE,
            },
        )
        assert r26_stale.status_code == 409

        # Scenario 27: entries PUT with explicit blocked agent_id → 400
        r27 = await client.put(
            f"/v1/projects/{project_id}/entries/{_HOST_A}",
            json={"workspace": "/opt/work/project", "agent_id": AGENT_ID},
        )
        assert r27.status_code == 400

        # Scenario 24: template blocked on _HOST_B → entry stored, no default written, notice shown
        r24 = await client.put(
            f"/v1/projects/{project_id}/entries/{_HOST_B}",
            json={"workspace": "/opt/work/project-b"},
        )
        assert r24.status_code == 200, r24.text
        body24 = r24.json()
        assert body24.get("host_agent_id") is None
        assert body24["initialization_notice"]
        refetched = (await client.get(f"/v1/projects/{project_id}")).json()
        assert _HOST_B not in refetched["config"].get("calling_defaults", {})


def test_fork_switch_blocked_and_same_agent_fork_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario 7: side-chat switch to blocked agent → 409; same-agent fork → 201."""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    import omnigent.runtime as runtime
    from omnigent.server.routes.sessions import create_sessions_router
    from tests.server.routes.test_sessions_fork import (
        _ConversationStore,
        _make_conversation,
        _StubAgentCache,
        _switch_agent_store,
    )

    target_id = "280d725b404d2915f9e9d6cccce91303"
    source = replace(
        _make_conversation(),
        project_id="proj_fork",
        host_id="host_src",
    )
    agents = _switch_agent_store()
    agents._agents[target_id] = replace(agents._agents[target_id], session_id=None)
    assert source.agent_id is not None
    cache = _StubAgentCache({source.agent_id: "claude-sdk", target_id: "claude-sdk"})
    monkeypatch.setattr(runtime, "get_agent_cache", lambda: cache)
    store = _ConversationStore({source.id: source})

    class _StubProjectStore:
        def get(self, pid: str, user_id: str | None = None) -> Project | None:
            if pid == "proj_fork":
                return Project(
                    id="proj_fork",
                    name="ForkProj",
                    user_id=None,
                    created_at=1,
                    config={"blocked_agents": {"host_src": [target_id, source.agent_id]}},
                )
            return None

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_err(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": str(exc.code), "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            conversation_store=store,  # type: ignore[arg-type]
            agent_store=agents,  # type: ignore[arg-type]
            project_store=_StubProjectStore(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    client = TestClient(app)

    # Side-chat switch to blocked target_id on source host → 409
    r_switch = client.post(
        f"/v1/sessions/{source.id}/fork",
        json={"agent_id": target_id, "side_chat": True},
    )
    assert r_switch.status_code == 409, r_switch.text
    assert r_switch.json()["error"]["code"] == "agent_blocked_for_project"

    # Same-agent fork keeping source.agent_id (even though source.agent_id is now blocked) → 201
    r_keep = client.post(f"/v1/sessions/{source.id}/fork", json={})
    assert r_keep.status_code == 201, r_keep.text


@pytest.mark.asyncio
async def test_first_bind_refuses_switched_fork_and_allows_same_agent_fork() -> None:
    """Scenarios 7 & 28: first bind blocks switched fork (409) and allows same-agent fork."""
    from omnigent.server.routes.hosts import create_hosts_router
    from omnigent.stores.host_store import now_epoch

    src_conv = Conversation(
        id="conv_src",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_src",
        agent_id="ag_blocked",
        project_id="proj_bind",
        host_id="host_old",
    )
    switched_fork = Conversation(
        id="conv_fork_switched",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_fork_switched",
        agent_id="ag_blocked",
        project_id="proj_bind",
        host_id=None,
        runner_id=None,
        labels={FORK_SOURCE_LABEL_KEY: "conv_src_other"},
    )
    src_other = Conversation(
        id="conv_src_other",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_src_other",
        agent_id="ag_original",
        project_id="proj_bind",
        host_id="host_old",
    )
    from omnigent.project_agent_blocks import FORK_SAME_AGENT_LABEL_KEY

    same_agent_fork = Conversation(
        id="conv_fork_same",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_fork_same",
        agent_id="ag_blocked",
        project_id="proj_bind",
        host_id=None,
        runner_id=None,
        labels={FORK_SOURCE_LABEL_KEY: "conv_src", FORK_SAME_AGENT_LABEL_KEY: "1"},
    )
    unmarked_fork = Conversation(
        id="conv_fork_unmarked",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_fork_unmarked",
        agent_id="ag_blocked",
        project_id="proj_bind",
        host_id=None,
        runner_id=None,
        labels={FORK_SOURCE_LABEL_KEY: "conv_src"},
    )

    class _ConvStore:
        def __init__(self) -> None:
            self.convs = {
                c.id: c
                for c in (src_conv, src_other, switched_fork, same_agent_fork, unmarked_fork)
            }

        def get_conversation(self, cid: str) -> Conversation | None:
            return self.convs.get(cid)

    class _HostStore:
        def get_host(self, hid: str) -> Any:
            return SimpleNamespace(
                host_id=hid,
                name="HostTMB",
                user_id=None,
                status="online",
                updated_at=now_epoch(),
            )

    class _HostRegistry:
        def get(self, hid: str) -> Any:
            return SimpleNamespace(host_id=hid, account_generation=0)

        def startup_launch(self, _sid: str) -> Any:
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _cm():
                yield

            return _cm()

    class _ProjStore:
        def get(self, pid: str, user_id: str | None = None) -> Project | None:
            if pid == "proj_bind":
                return Project(
                    id="proj_bind",
                    name="BindProj",
                    user_id=None,
                    created_at=1,
                    config={"blocked_agents": {"host_tmb": ["ag_blocked"]}},
                )
            return None

    class _Admission:
        def __init__(self) -> None:
            self.queued: list[str] = []

        def dispatching(self, _sid: str) -> bool:
            return False

        async def queue_preparation(
            self, _conn: Any, session_id: str, *_args: Any, **_kwargs: Any
        ) -> Any:
            self.queued.append(session_id)
            raise RuntimeError("reached_admission")

    from fastapi import Request
    from fastapi.responses import JSONResponse

    admission = _Admission()
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_err(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": str(exc.code), "message": exc.message}},
        )

    app.state.project_store = _ProjStore()
    app.state.startup_admission = admission
    app.state.agent_store = SimpleNamespace(
        get=lambda aid: SimpleNamespace(id=aid, name="Antigravity")
    )
    app.include_router(
        create_hosts_router(
            host_store=_HostStore(),  # type: ignore[arg-type]
            host_registry=_HostRegistry(),  # type: ignore[arg-type]
            conversation_store=_ConvStore(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        # Switched fork bound to host_tmb where ag_blocked is blocked → 409, never queued
        r_blocked = await client.post(
            "/v1/hosts/host_tmb/runners",
            json={"session_id": "conv_fork_switched", "workspace": "/opt/work/repo"},
        )
        assert r_blocked.status_code == 409, r_blocked.text
        assert r_blocked.json()["error"]["code"] == "agent_blocked_for_project"
        assert admission.queued == []

        # Unmarked FORK_SOURCE_LABEL_KEY without server FORK_SAME_AGENT_LABEL_KEY → 409
        r_unmarked = await client.post(
            "/v1/hosts/host_tmb/runners",
            json={"session_id": "conv_fork_unmarked", "workspace": "/opt/work/repo"},
        )
        assert r_unmarked.status_code == 409, r_unmarked.text
        assert admission.queued == []

        # Same-agent fork with server-stamped FORK_SAME_AGENT_LABEL_KEY → passes block check
        r_same = await client.post(
            "/v1/hosts/host_tmb/runners",
            json={"session_id": "conv_fork_same", "workspace": "/opt/work/repo"},
        )
        assert r_same.status_code != 409
        assert admission.queued == ["conv_fork_same"]

    # Server-reserved fork labels rejected on client writes, and FORK_SAME_AGENT_LABEL_KEY
    # preserves exemption even when the source conversation has been deleted.
    from omnigent.project_agent_blocks import FORK_SAME_AGENT_LABEL_KEY
    from omnigent.server.routes._sessions.helpers import _reject_server_reserved_label_seed

    for reserved_key in (FORK_SOURCE_LABEL_KEY, FORK_SAME_AGENT_LABEL_KEY):
        with pytest.raises(OmnigentError) as exc:
            _reject_server_reserved_label_seed({reserved_key: "1"}, at_creation=True)
        assert exc.value.code == ErrorCode.INVALID_INPUT
