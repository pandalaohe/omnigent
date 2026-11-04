"""The Host CLI-release read route and its store query.

``GET /v1/hosts/{host_id}/cli-releases`` answers what already happened on one
Host: newest first, whatever the intent's status, and only for the Host's
owner. The store query is proved against a real database so the ordering and
the owner scoping are the ones the route depends on.
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi import FastAPI

from omnigent.db.db_models import SqlCliReleaseIntent
from omnigent.db.utils import get_or_create_conversation_engine, make_named_managed_session_maker
from omnigent.server.cli_release_store import CliReleaseIntentStore
from omnigent.server.routes.hosts import create_hosts_router


class _RecordingStore:
    """Stands in for the intent store and records what the route asks for."""

    def __init__(self, rows: list[object] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, int]] = []

    def list_for_host(self, host_id: str, *, limit: int = 20):
        self.calls.append((host_id, limit))
        return self.rows


class _Intent:
    def __init__(self, *, created_at: int, reason: str, family: str | None, status: str) -> None:
        self.created_at = created_at
        self.reason = reason
        self.family = family
        self.status = status
        self.target_session_id = "0123456789abcdef0123456789abcdef"


def _router_app(*, hosts: dict[str, str], intent_store: object | None) -> FastAPI:
    """A router-only app; only the release route is exercised here."""

    class _HostStore:
        def get_host(self, host_id: str):
            owner = hosts.get(host_id)
            if owner is None:
                return None
            return type("Host", (), {"host_id": host_id, "user_id": owner})()

    app = FastAPI()
    app.include_router(
        create_hosts_router(
            host_registry=type("Reg", (), {"get": lambda self, host_id: None})(),
            host_store=_HostStore(),
            conversation_store=type("Convs", (), {})(),
        ),
        prefix="/v1",
    )
    app.state.cli_release_intent_store = intent_store
    return app


@pytest.mark.asyncio
async def test_lists_this_hosts_releases_newest_first() -> None:
    store = _RecordingStore(
        [
            _Intent(
                created_at=1_700_000_100,
                reason="archive",
                family="claude",
                status="completed",
            ),
            _Intent(
                created_at=1_700_000_000,
                reason="idle_pool_overflow",
                family=None,
                status="pending",
            ),
        ]
    )
    app = _router_app(hosts={"host-a": "alice"}, intent_store=store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/hosts/host-a/cli-releases")

    assert response.status_code == 200
    assert store.calls == [("host-a", 20)]
    assert response.json() == {
        "data": [
            {
                "created_at": 1_700_000_100,
                "reason": "archive",
                "family": "claude",
                "status": "completed",
                "target_session_id": "0123456789abcdef0123456789abcdef",
            },
            {
                "created_at": 1_700_000_000,
                "reason": "idle_pool_overflow",
                "family": None,
                "status": "pending",
                "target_session_id": "0123456789abcdef0123456789abcdef",
            },
        ]
    }


@pytest.mark.asyncio
async def test_answers_empty_when_no_store_is_wired() -> None:
    app = _router_app(hosts={"host-a": "alice"}, intent_store=None)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/hosts/host-a/cli-releases")

    assert response.status_code == 200
    assert response.json() == {"data": []}


@pytest.mark.asyncio
async def test_answers_404_for_an_unknown_host() -> None:
    app = _router_app(hosts={"host-a": "alice"}, intent_store=_RecordingStore())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unknown = await client.get("/v1/hosts/host-missing/cli-releases")

    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_passes_the_limit_through() -> None:
    store = _RecordingStore()
    app = _router_app(hosts={"host-a": "alice"}, intent_store=store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/hosts/host-a/cli-releases?limit=5")
        too_big = await client.get("/v1/hosts/host-a/cli-releases?limit=500")

    assert response.status_code == 200
    assert store.calls == [("host-a", 5)]
    assert too_big.status_code == 422


@pytest.fixture()
def intent_store(db_uri: str) -> CliReleaseIntentStore:
    return CliReleaseIntentStore(db_uri)


def test_store_query_is_scoped_to_one_host_and_newest_first(db_uri: str) -> None:
    import secrets

    from omnigent.db.db_models import current_workspace_id

    store = CliReleaseIntentStore(db_uri)
    session_maker = make_named_managed_session_maker(
        get_or_create_conversation_engine(db_uri), query_name_prefix="test.cli_release_intents"
    )
    now = int(time.time())
    with session_maker("insert_release_rows") as session:
        for host_id, created_at in (
            ("host-a", now - 300),
            ("host-a", now - 100),
            ("host-b", now - 50),
        ):
            session.add(
                SqlCliReleaseIntent(
                    workspace_id=current_workspace_id(),
                    id=secrets.token_hex(16),
                    dedupe_key=f"{host_id}:{created_at}",
                    reason="archive",
                    root_session_id="0123456789abcdef0123456789abcdef",
                    target_session_id="fedcba9876543210fedcba9876543210",
                    host_id=host_id,
                    status="completed",
                    next_attempt_at=0,
                    created_at=created_at,
                    updated_at=created_at,
                )
            )

    rows = store.list_for_host("host-a")

    assert [row.created_at for row in rows] == [now - 100, now - 300]
    assert {row.host_id for row in rows} == {"host-a"}
    assert store.list_for_host("host-a", limit=1)[0].created_at == now - 100
    assert store.list_for_host("host-c") == []
