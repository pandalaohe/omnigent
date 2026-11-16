"""Provider persistence, redaction and no-redirect discovery through the real API."""

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

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


def test_row_identity_names_compatibility_and_conflicting_create(client):
    from omnigent.onboarding.provider_config import load_providers

    response = client.put(
        "/v1/model-connections/new?create=true",
        json=draft(model_display_names={"default": "Speech"}),
    )
    assert response.status_code == 200
    assert response.json()["models"] == [
        {
            "key": "default",
            "display_name": "Speech",
            "model_id": "test-model",
            "status": "unchecked",
            "used_by": {"users": 0, "slots": []},
        }
    ]
    assert load_providers({"providers": connections._load()}) is not None
    response = client.put(
        "/v1/model-connections/new?create=true", json=draft(models={"other": "replacement"})
    )
    assert response.status_code == 409
    assert connections.resolve_reference("new", "default", "older") == (
        "default",
        "test-model",
        False,
    )
    assert connections.resolve_reference("new", "missing", "older") == (None, "older", True)


def test_usage_delete_guards_and_legacy_follows_row_changes(client):
    assert client.put("/v1/model-connections/example", json=draft()).status_code == 200
    preferences = [
        (
            "member@example.test",
            {
                "speech_source": "third",
                "speech_connection": "example",
                "speech_model": "test-model",
            },
        )
    ]
    store = SimpleNamespace(namespace_values=lambda namespace: preferences)
    app = FastAPI()
    app.include_router(create_model_connections_router(user_preferences_store=store), prefix="/v1")
    with TestClient(app) as guarded:
        response = guarded.put("/v1/model-connections/example", json=draft(models={}))
        assert response.status_code == 409
        assert response.json()["detail"]["rows"]["default"]["users"] == 1
        assert guarded.delete("/v1/model-connections/example").status_code == 409
        assert (
            guarded.put(
                "/v1/model-connections/example", json=draft(models={"default": "new-model"})
            ).status_code
            == 200
        )
        assert connections.resolve_reference("example", "", "test-model") == (
            "default",
            "new-model",
            False,
        )
        listing = guarded.get("/v1/model-connections").json()["connections"][0]
        assert listing["models"][0]["used_by"]["slots"] == [
            {"feature": "Voice input", "slot": "Speech to text", "users": 1}
        ]
        preferences[0][1]["speech_model"] = "unmatched"
        assert guarded.delete("/v1/model-connections/example").status_code == 409
        assert (
            guarded.put(
                "/v1/model-connections/example", json=draft(models={"default": "new-model"})
            ).status_code
            == 200
        )
        assert connections._load()["example"]["model_previous_ids"]["default"] == []
        preferences.clear()
        assert guarded.delete("/v1/model-connections/example").status_code == 204


def test_catalogue_refresh_preserves_rows_and_statuses(client, monkeypatch):
    assert (
        client.put(
            "/v1/model-connections/example",
            json=draft(models={"known": "listed", "typo": "unknown"}),
        ).status_code
        == 200
    )
    original = httpx.Client
    ids = ["listed"]
    monkeypatch.setattr(
        connections.httpx,
        "Client",
        lambda **kwargs: original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"data": [{"id": model} for model in ids]}
                )
            ),
            **kwargs,
        ),
    )
    first = client.post("/v1/model-connections/example/refresh").json()
    assert [row["status"] for row in first["models"]] == ["listed", "not_listed"]
    assert client.get("/v1/model-connections/example/catalog").json()["ids"] == ["listed"]
    ids.clear()
    second = client.post("/v1/model-connections/example/refresh").json()
    assert [row["status"] for row in second["models"]] == ["gone", "not_listed"]
    assert [row["key"] for row in second["models"]] == ["known", "typo"]
    ids.extend([f"model-{i}" for i in range(2500)] + ["listed"])
    third = client.post("/v1/model-connections/example/refresh").json()
    assert third["catalog"]["complete"] is False
    assert len(third["catalog"]["ids"]) == 2000
    assert all(row["status"] == "unchecked" for row in third["models"])
    assert connections._load()["example"]["model_seen"]["known"] == "listed"
    member = connections.load_connections()[0]
    assert "used_by" not in member and "catalog" not in member
    assert all("used_by" not in row for row in member["models"])


def test_metadata_pruned_with_deleted_rows(client):
    assert client.put("/v1/model-connections/example", json=draft()).status_code == 200
    entries = connections._load()
    entries["example"].update(
        model_seen={"default": "test-model"}, model_previous_ids={"default": ["old"]}
    )
    connections._write(entries)
    assert client.put("/v1/model-connections/example", json=draft(models={})).status_code == 200
    entry = connections._load()["example"]
    assert entry["model_display_names"] == entry["model_seen"] == entry["model_previous_ids"] == {}


def test_one_step_does_not_lock_unused_separate_cleanup_provider(client):
    assert client.put("/v1/model-connections/example", json=draft()).status_code == 200
    store = SimpleNamespace(
        namespace_values=lambda namespace: [
            (
                "member@example.test",
                {
                    "speech_source": "third",
                    "speech_connection": "another",
                    "speech_method": "chat_audio",
                    "speech_row": "speech",
                    "speech_model": "audio",
                    "cleanup_connection": "example",
                    "model": "test-model",
                    "mode": "automatic",
                },
            )
        ]
    )
    assert connections.references(store)["providers"] == {"another": 1}
    connections.delete_connection("example", store=store)


def test_disabled_legacy_preference_retains_model_history(client):
    assert client.put("/v1/model-connections/example", json=draft()).status_code == 200
    store = SimpleNamespace(
        namespace_values=lambda namespace: [
            (
                "member@example.test",
                {
                    "cleanup_connection": "example",
                    "model": "test-model",
                    "mode": "off",
                },
            )
        ]
    )
    connections.upsert_connection(
        "example",
        connections.ConnectionWrite.model_validate(draft(models={"default": "renamed"})),
        store=store,
    )
    assert connections.resolve_reference("example", "", "test-model") == (
        "default",
        "renamed",
        False,
    )
    assert connections.references(store)["providers"] == {}
