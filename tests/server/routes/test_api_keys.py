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


def test_managed_config_environment_and_valid_jwt_coexist(request, monkeypatch):
    import hashlib
    import secrets

    from fastapi import HTTPException, Request

    from omnigent.server.auth import AccountAuthenticationMiddleware
    from omnigent.server.server_config import load_api_keys

    store, accounts = request.getfixturevalue("keys")
    config_value, env_value = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    provider = _provider()
    provider.set_api_keys(
        load_api_keys(
            {
                "api_keys": [
                    {
                        "name": "configured",
                        "user": "owner",
                        "scopes": ["usage"],
                        "sha256": hashlib.sha256(config_value.encode()).hexdigest(),
                    }
                ]
            },
            {"OMNIGENT_USAGE_READ_TOKEN": f"owner:{env_value}"},
        )
    )
    provider.set_account_check(accounts.accepts_generation)
    provider.set_usage_read_account_lookup(lambda user: accounts.get_user(user).account_generation)
    lookups = []

    def resolve(digest, now):
        lookups.append(digest)
        return store.resolve(digest, now)

    provider.set_api_key_store(resolve, store.touch)
    _, managed_value = store.create("owner", name="managed", scopes=("*",), expires_at=None, now=1)
    app = FastAPI()
    app.add_middleware(AccountAuthenticationMiddleware, auth_provider=provider)
    app.include_router(create_api_keys_router(provider, store))

    @app.get("/v1/usage-events")
    def usage(request: Request):
        user = provider.get_user_id(request)
        if user is None:
            raise HTTPException(401)
        return {"user": user, "principal": request.state.usage_read_grant_id}

    @app.get("/v1/me/tasks")
    def tasks(request: Request):
        user = provider.get_user_id(request)
        if user is None:
            raise HTTPException(401)
        return {"user": user}

    monkeypatch.setenv("OMNIGENT_WS_ALLOWED_ORIGINS", "")
    with TestClient(app, base_url="https://omni.example.test") as client:
        for value in (config_value, env_value, managed_value):
            response = client.get("/v1/usage-events", headers={"Authorization": "Bearer " + value})
            assert response.status_code == 200
            assert response.json() == {"user": "owner", "principal": "config:usage-read"}
        assert len(lookups) == 1
        token = mint_session_cookie(
            "owner",
            _KEY,
            1,
            "accounts",
            account_generation=accounts.get_user("owner").account_generation,
        )
        assert (
            client.get("/v1/me/tasks", headers={"Authorization": "Bearer " + token}).status_code
            == 200
        )
        assert len(lookups) == 1
        browser = mint_session_cookie(
            "owner",
            _KEY,
            1,
            "accounts",
            browser_session=True,
            account_generation=accounts.get_user("owner").account_generation,
        )
        client.cookies.set(provider.session_cookie_name, browser)
        rows = client.get("/v1/me/api-keys").json()
        assert len(rows["keys"]) == 1
        assert {row["source"] for row in rows["read_only"]} == {"config", "environment"}
        assert all(row["last_used_at"] is not None for row in rows["read_only"])
        client.headers["Origin"] = "https://omni.example.test"
        assert (
            client.post(
                "/v1/me/api-keys", json={"name": "configured", "scopes": ["usage"]}
            ).status_code
            == 409
        )
