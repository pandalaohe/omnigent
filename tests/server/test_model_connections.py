"""Provider persistence, redaction and no-redirect discovery through the real API."""

import json
import stat
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.onboarding import secrets
from omnigent.server import model_connections as connections
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.model_connections import create_model_connections_router


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "resolve_data_dir", lambda: tmp_path)
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    app = FastAPI()
    app.include_router(create_model_connections_router(), prefix="/v1")
    with TestClient(app) as client:
        yield client


def draft(**kwargs):
    return {
        "base_url": "https://provider.example/v1",
        "wire_api": "chat",
        "models": {"default": "test-model"},
        "key": {"value": "sk-test-not-real"},
        **kwargs,
    }


def test_roundtrip_keeps_keys_out_of_catalogue(client, tmp_path):
    result = client.put("/v1/model-connections/example", json=draft())
    assert result.status_code == 200, result.text
    assert result.json()["key_set"] is True
    assert "sk-test-not-real" not in result.text
    assert "sk-test-not-real" not in client.get("/v1/model-connections").text
    path = tmp_path / "model_connections.json"
    assert "sk-test-not-real" not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert connections.resolve_connection("example").api_key == "sk-test-not-real"
    assert client.put("/v1/model-connections/example", json=draft(key=None)).status_code == 200
    assert client.delete("/v1/model-connections/example").status_code == 204
    assert client.delete("/v1/model-connections/example").status_code == 204
    assert secrets.load_secret("server-connection:example", home=str(tmp_path)) is None


@pytest.mark.parametrize(
    "change",
    [
        {"key": {"value": 12, "extra": "sk-test-not-real"}},
        {"extra": "sk-test-not-real"},
        {"base_url": "https://u:sk-test-not-real@provider.example/v1"},
        {"base_url": "https://provider.example/v1?key=sk-test-not-real"},
        {"base_url": "https://provider.example/v1#sk-test-not-real"},
        {"base_url": "$ENDPOINT"},
        {"key": None},
    ],
)
def test_invalid_inputs_never_echo_credentials(client, change, caplog):
    response = client.put("/v1/model-connections/example", json=draft(**change))
    assert response.status_code == 422
    assert "sk-test-not-real" not in response.text + caplog.text


def test_raw_body_and_readonly_name(client):
    assert client.put("/v1/model-connections/example", content="[").status_code == 400
    assert client.put("/v1/model-connections/server-llm", json=draft()).status_code == 422
    assert client.delete("/v1/model-connections/server-llm").status_code == 422


@pytest.mark.parametrize(
    "status,body,error",
    [
        (200, {"data": [{"id": "test-model"}]}, None),
        (401, {"error": "sk-test-not-real"}, "auth"),
        (302, {}, "redirect"),
        (500, {}, "http_500"),
        (200, {"data": 12}, "bad_response"),
    ],
)
def test_fetch_models_never_follows_redirect_or_echoes_body(
    client, monkeypatch, status, body, error
):
    original = httpx.Client
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json=body, headers={"Location": "https://other.example/"})

    monkeypatch.setattr(
        connections.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    result = client.post("/v1/model-connections/test", json={"name": "example", **draft()})
    assert result.status_code == 200
    assert result.json()["error_kind"] == error
    assert len(calls) == 1
    assert calls[0].url.path == "/v1/models"
    assert "sk-test-not-real" not in result.text


def test_multiuser_writes_fail_closed_without_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "resolve_data_dir", lambda: tmp_path)
    app = FastAPI()
    app.include_router(
        create_model_connections_router(
            UnifiedAuthProvider(source="header", local_single_user=False)
        ),
        prefix="/v1",
    )
    with TestClient(app) as client:
        assert (
            client.put(
                "/v1/model-connections/example",
                json=draft(),
                headers={"X-Forwarded-Email": "member@example.test"},
            ).status_code
            == 403
        )
    assert not (tmp_path / "model_connections.json").exists()


def test_secret_atomic_failure_and_concurrent_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    home = str(tmp_path)
    secrets.store_secret("first", "fake-first", home=home)
    replace = secrets.os.replace
    with monkeypatch.context() as patch:

        def fail(*args):
            raise OSError("simulated")

        patch.setattr(secrets.os, "replace", fail)
        with pytest.raises(OSError):
            secrets.store_secret("second", "fake-second", home=home)
    assert json.loads((tmp_path / "secrets.json").read_text()) == {"first": "fake-first"}
    assert not list(tmp_path.glob(".secrets-*"))
    assert secrets.os.replace is replace

    def write(name):
        for _ in range(50):
            secrets.store_secret(name, "fake-value", home=home)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, ["second", "third"]))
    assert set(json.loads((tmp_path / "secrets.json").read_text())) == {"first", "second", "third"}


def test_environment_key_and_member_redaction(client, monkeypatch):
    monkeypatch.setenv("EXAMPLE_PROVIDER_KEY", "fake-env-key")
    assert (
        client.put(
            "/v1/model-connections/example", json=draft(key={"env": "EXAMPLE_PROVIDER_KEY"})
        ).status_code
        == 200
    )
    assert connections.resolve_connection("example").api_key == "fake-env-key"
    assert "key_env" not in connections.load_connections(admin=False)[0]
    assert connections.load_connections(admin=True)[0]["key_env"] == "EXAMPLE_PROVIDER_KEY"


def test_member_cannot_write_or_fetch_models(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi.responses import JSONResponse

    from omnigent.errors import OmnigentError

    monkeypatch.setattr(connections, "resolve_data_dir", lambda: tmp_path)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def forbidden(request, exc):
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})

    app.include_router(
        create_model_connections_router(
            UnifiedAuthProvider(source="header", local_single_user=False),
            SimpleNamespace(is_admin=lambda owner: False),
        ),
        prefix="/v1",
    )
    with TestClient(app) as client:
        headers = {"X-Forwarded-Email": "member@example.test"}
        assert (
            client.put("/v1/model-connections/example", json=draft(), headers=headers).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/model-connections/test", json={"name": "example", **draft()}, headers=headers
            ).status_code
            == 403
        )
        assert client.get("/v1/model-connections", headers=headers).status_code == 200
    assert not (tmp_path / "model_connections.json").exists()
