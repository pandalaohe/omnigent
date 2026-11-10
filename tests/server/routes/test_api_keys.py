"""The management boundary accepts browser cookies only and preserves one-time reveal."""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.oidc import mint_session_cookie
from omnigent.server.routes.api_keys import create_api_keys_router
from tests.server.routes.test_usage_read_auth import _KEY, _provider
from tests.server.test_api_key_store import keys  # noqa: F401


@pytest.fixture
def client(request, monkeypatch):
    store, accounts = request.getfixturevalue("keys")
    provider = _provider()
    provider.set_account_check(accounts.accepts_generation)
    monkeypatch.setenv("OMNIGENT_WS_ALLOWED_ORIGINS", "")
    app = FastAPI()
    app.include_router(create_api_keys_router(provider, store))
    with TestClient(app, base_url="https://omni.example.test") as client:
        token = mint_session_cookie(
            "owner",
            _KEY,
            1,
            "accounts",
            browser_session=True,
            account_generation=accounts.get_user("owner").account_generation,
        )
        client.cookies.set(provider.session_cookie_name, token)
        client.headers["origin"] = "https://omni.example.test"
        yield client, provider, store


def test_crud_and_one_time_value(client):
    client, _provider, _store = client
    path = "/v1/me/api-keys"
    response = client.post(path, json={"name": "feed", "scopes": ["*"], "expires_at": None})
    assert response.status_code == 201
    body = response.json()
    key_id = body["key"]["id"]
    assert response.headers["cache-control"] == "no-store"
    assert "value" not in client.get(path).text
    assert client.post(path, json={"name": "feed", "scopes": ["usage"]}).status_code == 409
    edited = client.patch(
        f"{path}/{key_id}", json={"scopes": ["usage"], "expires_at": int(time.time()) + 600}
    )
    assert edited.status_code == 200 and edited.json()["key"]["scopes"] == ["usage"]
    regenerated = client.post(f"{path}/{key_id}/regenerate", json={"expires_at": None})
    assert regenerated.status_code == 200 and regenerated.json()["value"] != body["value"]
    for method, suffix in [("PATCH", ""), ("DELETE", ""), ("POST", "/regenerate")]:
        kwargs = {"json": {}} if method != "DELETE" else {}
        assert client.request(method, f"{path}/missing{suffix}", **kwargs).status_code == 404
    assert client.delete(f"{path}/{key_id}").status_code == 204
    assert client.get(path).json()["keys"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"name": "-x", "scopes": ["usage"]},
        {"name": "x", "scopes": []},
        {"name": "x", "scopes": ["usage", "usage"]},
        {"name": "x", "scopes": ["sessions"]},
        {"name": "x", "scopes": ["*", "usage"]},
        {"name": "x", "scopes": ["usage"], "expires_at": 1},
        {"name": "x", "scopes": ["usage"], "expires_at": True},
    ],
)
def test_validation(client, body):
    client, _, _ = client
    assert client.post("/v1/me/api-keys", json=body).status_code == 400


def test_browser_and_origin_required(client):
    client, provider, _ = client
    path = "/v1/me/api-keys"
    body = {"name": "feed", "scopes": ["*"]}
    client.headers.pop("origin")
    assert client.get(path).status_code == 200
    assert client.post(path, json=body).status_code == 403
    client.headers["origin"] = "https://foreign.example.test"
    assert client.post(path, json=body).status_code == 403
    client.headers["origin"] = "https://omni.example.test"
    client.headers["authorization"] = "Bearer " + client.cookies.get(provider.session_cookie_name)
    assert client.get(path).status_code == 401
    assert client.post(path, json=body).status_code == 401
    del client.headers["authorization"]
    client.cookies.clear()
    assert client.get(path).status_code == 401
