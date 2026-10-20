"""Proxy contracts exercised through the authenticated extension router."""

from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI

from omnigent.extensions import (
    ExtensionManifest,
    ExtensionPermission,
    ExtensionPluginState,
    ExtensionValidationError,
    ExternalPageContribution,
    ExternalPageProxy,
    validate_manifest,
)
from omnigent.server.routes.extensions import create_extensions_router

_PREFIX = "/v1/extensions/acme.dashboard/proxy/board/"
_PROXY = ExternalPageProxy(
    allowed_routes=(("GET", "/"), ("GET", "/api/summary.json"), ("POST", "/api/settings")),
    request_headers=(("x-dashboard", "1"),),
)
_PAGE = ExternalPageContribution(
    id="acme.dashboard.board",
    title="Board",
    route="board",
    url="http://board.test:8123/?view=mini#details",
    proxy=_PROXY,
)
_MANIFEST = ExtensionManifest(
    id="acme.dashboard",
    display_name="Dashboard",
    distribution="dashboard",
    version="1.0",
    requires_omnigent=">=0.11,<1",
    extension_api=1,
    permissions=frozenset({ExtensionPermission.PAGES_EXTERNAL, ExtensionPermission.PAGES_PROXY}),
    pages=(_PAGE,),
)


@pytest.fixture
def deployment(request: pytest.FixtureRequest) -> str:
    return getattr(request, "param", "")


@pytest.fixture
def proxy_client(monkeypatch: pytest.MonkeyPatch, deployment: str):
    requests: list[httpx.Request] = []
    reply: list[httpx.Response | Exception] = [httpx.Response(200, text="board")]

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        result = reply[0]
        if isinstance(result, Exception):
            raise result
        return result

    original = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(upstream))
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    app = FastAPI()
    if deployment in {"retained", "stripped"}:
        from omnigent.server.app import BasePathMiddleware

        app.state.base_path = "/proxy/6767"
        app.add_middleware(BasePathMiddleware, base_path=app.state.base_path)
    app.include_router(
        create_extensions_router(ExtensionPluginState(manifests=(_MANIFEST,))), prefix="/v1"
    )
    return (
        original(
            transport=httpx.ASGITransport(
                app=app, root_path="/proxy/6767" if deployment == "mounted" else ""
            ),
            base_url="https://app.test",
        ),
        requests,
        reply,
    )


@pytest.mark.parametrize("deployment", ["retained", "stripped", "mounted"], indirect=True)
async def test_proxy_forwards_public_deployment_prefix(proxy_client, deployment: str) -> None:
    client, requests, _ = proxy_client
    incoming_prefix = "/proxy/6767" if deployment == "retained" else ""
    async with client:
        response = await client.get(incoming_prefix + _PREFIX)
    assert response.status_code == 200
    assert str(requests[0].url) == "http://board.test:8123/"
    assert requests[0].headers["x-forwarded-prefix"] == "/proxy/6767" + _PREFIX.rstrip("/")


async def test_proxy_catalog_and_scrubbed_transport(proxy_client) -> None:
    client, requests, reply = proxy_client
    reply[0] = httpx.Response(
        200,
        content=b'{"ok":true}',
        headers={
            "content-type": "application/json",
            "set-cookie": "upstream=private",
            "location": "http://board.test/",
            "access-control-allow-origin": "*",
            "content-encoding": "identity",
        },
    )
    async with client:
        catalog = await client.get("/v1/extensions/acme.dashboard")
        assert catalog.json()["pages"][0]["kind"] == "proxied"
        assert catalog.json()["pages"][0]["url"] == _PREFIX + "?view=mini#details"
        response = await client.post(
            _PREFIX + "api/settings?view=mini&n=1&n=2",
            content=b'{"pin":true}',
            headers={
                "origin": "https://app.test",
                "sec-fetch-site": "same-origin",
                "content-type": "application/json",
                "accept": "application/json",
                "cookie": "session=private",
                "authorization": "Bearer private",
                "x-forwarded-email": "private@app.test",
                "x-dashboard": "attacker",
                "x-forwarded-host": "attacker.test",
            },
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["cache-control"] == "no-store"
    assert not {"set-cookie", "location", "access-control-allow-origin", "content-encoding"} & set(
        response.headers
    )
    sent = requests[0]
    assert str(sent.url) == "http://board.test:8123/api/settings?view=mini&n=1&n=2"
    assert sent.content == b'{"pin":true}'
    assert sent.headers["host"] == "board.test:8123"
    assert sent.headers["origin"] == "http://board.test:8123"
    assert sent.headers["x-forwarded-prefix"] == _PREFIX.rstrip("/")
    assert sent.headers["x-forwarded-host"] == "app.test"
    assert sent.headers["x-forwarded-proto"] == "https"
    assert sent.headers["x-dashboard"] == "1"
    assert sent.headers["content-type"] == sent.headers["accept"] == "application/json"
    assert not {"cookie", "authorization", "x-forwarded-email", "sec-fetch-site"} & set(
        sent.headers
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "api/push"),
        ("POST", "api/push"),
        ("GET", "api/settings"),
        ("DELETE", "api/settings"),
        ("GET", "api%2fsummary.json"),
        ("GET", "%2e%2e/api/summary.json"),
        ("GET", "api//summary.json"),
    ],
)
async def test_proxy_refuses_undeclared_routes(proxy_client, method: str, path: str) -> None:
    client, requests, _ = proxy_client
    async with client:
        response = await client.request(method, _PREFIX + path)
    assert response.status_code == 404
    assert requests == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "null"},
        {"origin": "https://other.test"},
        {"origin": "https://app.test", "sec-fetch-site": "cross-site"},
        {"origin": "https://app.test", "sec-fetch-site": "same-site"},
    ],
)
async def test_proxy_refuses_cross_site_writes(proxy_client, headers: dict[str, str]) -> None:
    client, requests, _ = proxy_client
    async with client:
        response = await client.post(_PREFIX + "api/settings", headers=headers)
    assert response.status_code == 403
    assert requests == []


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (httpx.Response(302, headers={"location": "http://other.test/"}), 502),
        (httpx.ConnectError("private upstream details"), 502),
        (httpx.ReadTimeout("private upstream details"), 504),
        (httpx.Response(503, text="temporarily unavailable"), 503),
    ],
)
async def test_proxy_error_mapping(proxy_client, reply, expected: int) -> None:
    client, requests, replies = proxy_client
    replies[0] = reply
    async with client:
        response = await client.get(_PREFIX + "?view=mini")
    assert response.status_code == expected
    assert "private upstream details" not in response.text
    assert len(requests) == 1
    assert requests[0].url.query == b"view=mini"


async def test_proxy_bounds_request_and_response(
    proxy_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes import extension_proxy

    monkeypatch.setattr(extension_proxy, "_MAX_REQUEST_BYTES", 4)
    monkeypatch.setattr(extension_proxy, "_MAX_RESPONSE_BYTES", 4)
    client, requests, replies = proxy_client
    async with client:
        response = await client.post(
            _PREFIX + "api/settings", headers={"origin": "https://app.test"}, content=b"12345"
        )
        assert response.status_code == 413
        assert requests == []
        replies[0] = httpx.Response(200, content=b"12345")
        assert (await client.get(_PREFIX)).status_code == 502


def test_proxy_requires_authentication() -> None:
    from fastapi.testclient import TestClient

    from omnigent.errors import OmnigentError
    from omnigent.server.auth import UnifiedAuthProvider

    app = FastAPI()
    app.include_router(
        create_extensions_router(
            ExtensionPluginState(manifests=(_MANIFEST,)),
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        ),
        prefix="/v1",
    )
    with TestClient(app) as client, pytest.raises(OmnigentError, match="Authentication required"):
        client.get(_PREFIX)


@pytest.mark.parametrize(
    ("page", "permissions"),
    [
        (_PAGE, frozenset({ExtensionPermission.PAGES_EXTERNAL})),
        (replace(_PAGE, url="http://board.test/subpath"), _MANIFEST.permissions),
        (replace(_PAGE, proxy=ExternalPageProxy((("GET", "/api/push"),))), _MANIFEST.permissions),
        (
            replace(_PAGE, proxy=ExternalPageProxy((("GET", "/"), ("GET", "/../api/push")))),
            _MANIFEST.permissions,
        ),
        (
            replace(_PAGE, proxy=replace(_PROXY, request_headers=(("cookie", "bad"),))),
            _MANIFEST.permissions,
        ),
        (
            replace(
                _PAGE, proxy=replace(_PROXY, request_headers=(("x-dashboard", "bad\r\nheader"),))
            ),
            _MANIFEST.permissions,
        ),
    ],
)
def test_invalid_proxy_manifests(page, permissions) -> None:
    with pytest.raises(ExtensionValidationError):
        validate_manifest(replace(_MANIFEST, pages=(page,), permissions=permissions))


def test_valid_proxy_manifest() -> None:
    validate_manifest(_MANIFEST)
