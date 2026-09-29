"""Route tests for ``/v1/system/{status,history,settings}``."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.host.frames import CAP_RESOURCE_SNAPSHOT
from omnigent.server.routes.system_status import create_system_status_router
from omnigent.server.system_status import SystemStatusHub
from omnigent.stores.host_store import Host

_ALICE = "alice@example.com"
_BOB = "bob@example.com"


class _AuthProvider:
    """Reads the test user from a header; no real auth."""

    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("X-Test-User")


class _PermissionStore:
    def __init__(self, *admins: str) -> None:
        self._admins = set(admins)

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._admins


class _HostStore:
    def __init__(self, hosts: list[Host]) -> None:
        self._hosts = hosts

    def list_hosts(self, user_id: str) -> list[Host]:
        return [host for host in self._hosts if host.user_id == user_id]


def _host(host_id: str, name: str, user_id: str, status: str = "online") -> Host:
    return Host(
        host_id=host_id,
        name=name,
        user_id=user_id,
        status=status,
        created_at=0,
        updated_at=0,
    )


def _build(
    tmp_path: Path,
    *,
    admins: tuple[str, ...] = ("admin@example.com",),
    hosts: list[Host] | None = None,
) -> tuple[TestClient, SystemStatusHub]:
    hub = SystemStatusHub(tmp_path, None)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": str(exc)})

    app.state.system_status = hub
    app.include_router(
        create_system_status_router(
            _HostStore(hosts or []),
            auth_provider=_AuthProvider(),
            permission_store=_PermissionStore(*admins),
        ),
        prefix="/v1",
    )
    return TestClient(app), hub


def _seed(hub: SystemStatusHub) -> None:
    hub.host_changed(
        host_id="host_a",
        workspace_id=0,
        owner=_ALICE,
        name="alice-laptop",
        conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
        now=0.0,
    )
    hub.host_changed(
        host_id="host_b",
        workspace_id=0,
        owner=_BOB,
        name="bob-laptop",
        conn_capabilities=[CAP_RESOURCE_SNAPSHOT],
        now=0.0,
    )


def test_status_admin_sees_server_and_every_host(tmp_path: Path) -> None:
    """An admin gets the server card and all workspace hosts."""
    client, hub = _build(tmp_path, hosts=[_host("host_b", "bob-laptop", _BOB, "offline")])
    _seed(hub)

    resp = client.get("/v1/system/status", headers={"X-Test-User": "admin@example.com"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["server"] is not None
    assert {host["host_id"] for host in payload["hosts"]} == {"host_a", "host_b"}
    assert "monitor_overhead" in payload


def test_status_member_sees_only_own_hosts_and_no_server(tmp_path: Path) -> None:
    """A member gets their own hub hosts plus store-only offline hosts."""
    client, hub = _build(
        tmp_path,
        hosts=[
            _host("host_b", "bob-laptop", _BOB),
            _host("host_c", "old-bob-host", _BOB, "offline"),
        ],
    )
    _seed(hub)

    resp = client.get("/v1/system/status", headers={"X-Test-User": _BOB})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["server"] is None
    by_id = {host["host_id"]: host for host in payload["hosts"]}
    assert set(by_id) == {"host_b", "host_c"}
    assert by_id["host_c"]["state"] == "offline"
    assert by_id["host_c"]["last_snapshot"] is None


def test_status_live_records_a_viewer_lease(tmp_path: Path) -> None:
    """``live=1`` leases the returned hosts; a plain read does not."""
    client, hub = _build(tmp_path)
    _seed(hub)

    client.get("/v1/system/status", headers={"X-Test-User": _BOB})
    assert hub.fast_hosts(1_000_000.0) == []

    client.get("/v1/system/status?live=1", headers={"X-Test-User": _BOB})
    leased = hub.fast_hosts(1_000_000.0)
    assert [host_id for _ws, host_id in leased] == ["host_b"]


def test_status_summary_shape(tmp_path: Path) -> None:
    """``summary=1`` returns only revision / level / findings."""
    client, hub = _build(tmp_path)
    _seed(hub)

    resp = client.get("/v1/system/status?summary=1", headers={"X-Test-User": _BOB})
    assert resp.status_code == 200
    assert set(resp.json()) == {"revision", "level", "findings"}


def test_history_visibility_and_404(tmp_path: Path) -> None:
    """History is visible per caller and 404s otherwise."""
    client, hub = _build(tmp_path)
    _seed(hub)

    admin = client.get(
        "/v1/system/history?target=server",
        headers={"X-Test-User": "admin@example.com"},
    )
    assert admin.status_code == 200
    assert admin.json() == {"target": "server", "points": []}

    own = client.get("/v1/system/history?target=host_b", headers={"X-Test-User": _BOB})
    assert own.status_code == 200

    foreign = client.get("/v1/system/history?target=host_a", headers={"X-Test-User": _BOB})
    assert foreign.status_code == 404

    unknown = client.get("/v1/system/history?target=host_x", headers={"X-Test-User": _BOB})
    assert unknown.status_code == 404

    member_server = client.get("/v1/system/history?target=server", headers={"X-Test-User": _BOB})
    assert member_server.status_code == 404


def test_settings_admin_only_with_validation(tmp_path: Path) -> None:
    """Thresholds are admin-only; PUT validates and returns the stored set."""
    client, _hub = _build(tmp_path)

    forbidden = client.get("/v1/system/settings", headers={"X-Test-User": _BOB})
    assert forbidden.status_code == 403
    forbidden_put = client.put(
        "/v1/system/settings",
        headers={"X-Test-User": _BOB},
        json={"cpu_pct": 70},
    )
    assert forbidden_put.status_code == 403

    admin = client.get("/v1/system/settings", headers={"X-Test-User": "admin@example.com"})
    assert admin.status_code == 200
    assert admin.json()["cpu_pct"] == 85.0

    updated = client.put(
        "/v1/system/settings",
        headers={"X-Test-User": "admin@example.com"},
        json={"cpu_pct": 70, "cpu_sustain_min": 3},
    )
    assert updated.status_code == 200
    assert updated.json()["cpu_pct"] == 70.0
    assert updated.json()["cpu_sustain_min"] == 3
    assert updated.json()["mem_pct"] == 90.0

    for payload in (
        {"cpu_pct": 150},
        {"cpu_sustain_min": 0},
        {"cpu_sustain_min": 1441},
        {"cpu_sustain_min": 2.5},
    ):
        bad = client.put(
            "/v1/system/settings",
            headers={"X-Test-User": "admin@example.com"},
            json=payload,
        )
        assert bad.status_code == 400, payload


def test_single_user_mode_without_permission_store(tmp_path: Path) -> None:
    """No permission store means every caller is an admin (local server)."""
    hub = SystemStatusHub(tmp_path, None)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": str(exc)})

    app.state.system_status = hub
    app.include_router(
        create_system_status_router(_HostStore([])),
        prefix="/v1",
    )
    client = TestClient(app)

    assert client.get("/v1/system/status").status_code == 200
    assert client.get("/v1/system/settings").status_code == 200
