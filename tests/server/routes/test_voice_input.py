"""Voice cleanup sends only the segment and the caller's vocabulary."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
from fastapi import FastAPI

from omnigent.errors import OmnigentError
from omnigent.server import dictation_hosted, model_connections
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes import voice_input
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.server.voice_input import VoiceInputSettings


def save_settings(store, settings, *, user_id):
    store.patch_namespace(user_id or "local", "voice_input", settings)


def test_gateway_alias_uses_configured_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.runtime import get_caps
    from omnigent.spec.types import LLMConfig

    runtime_caps = get_caps()
    monkeypatch.setattr(
        runtime_caps,
        "llm",
        LLMConfig(
            model="openai/test-default", connection={"base_url": "https://provider.example/v1"}
        ),
    )
    model, connection = voice_input._configured_connection("gateway/test-fast")
    assert model == "openai/gateway/test-fast"
    assert connection == {"base_url": "https://provider.example/v1"}
    with pytest.raises(ValueError, match="configured LLM provider"):
        voice_input._configured_connection("anthropic/test-other")


async def test_default_six_second_timeout_is_forwarded(
    voice_store: SqlAlchemyUserPreferencesStore, llm: SimpleNamespace
) -> None:
    save_settings(
        voice_store, VoiceInputSettings(model="openai/test-cleanup").model_dump(), user_id=None
    )
    llm.responses.create.side_effect = TimeoutError()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/dictation/cleanup", json={"text": "keep this raw segment"}
        )
    assert response.status_code == 504
    assert llm.responses.create.call_args.kwargs["timeout"] == 6


@pytest.fixture()
def voice_store(db_uri: str) -> SqlAlchemyUserPreferencesStore:
    return SqlAlchemyUserPreferencesStore(db_uri)


@pytest.fixture()
def llm(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    response = SimpleNamespace(
        output=[
            SimpleNamespace(
                content=[
                    SimpleNamespace(
                        text=json.dumps({"complete": True, "text": "Do not deploy at 12:30."})
                    )
                ]
            )
        ],
        status="completed",
    )
    create = AsyncMock(return_value=response)
    monkeypatch.setattr(
        voice_input,
        "_configured_connection",
        lambda model: (model, {"base_url": "https://provider.example/v1"}),
    )
    return SimpleNamespace(responses=SimpleNamespace(create=create))


def app_for(
    store: SqlAlchemyUserPreferencesStore, llm: SimpleNamespace, *, auth: bool = False
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        voice_input.create_voice_input_router(
            store,
            UnifiedAuthProvider(source="header", local_single_user=False) if auth else None,
            llm_client=llm,
        ),
        prefix="/v1",
    )
    return app


@pytest.fixture()
def model_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(model_connections, "resolve_data_dir", lambda: tmp_path)
    monkeypatch.setenv("EXAMPLE_PROVIDER_KEY", "fake-key")
    model_connections.upsert_connection(
        "example",
        model_connections.ConnectionWrite(
            base_url="https://provider.example/v1",
            models={"row": "current-model"},
            key=model_connections.KeyEnvironment(env="EXAMPLE_PROVIDER_KEY"),
        ),
    )


@pytest.mark.parametrize(
    "status,payload,kind",
    [
        (200, {"text": "Testing voice input today."}, "ok"),
        (200, {"text": ""}, "rejected"),
        (200, {"other": "provider-private-data"}, "rejected"),
        (401, {"error": "provider-private-data"}, "key_rejected"),
        (403, {"error": "provider-private-data"}, "key_rejected"),
        (400, {"error": "provider-private-data"}, "wrong_type"),
        (404, {"error": "provider-private-data"}, "wrong_type"),
        (422, {"error": "provider-private-data"}, "wrong_type"),
        (429, {"error": "provider-private-data"}, "rejected"),
        (500, {"error": "provider-private-data"}, "rejected"),
    ],
)
async def test_speech_test_calls_unsaved_row_with_production_validation(
    voice_store,
    llm,
    model_provider,
    monkeypatch,
    status,
    payload,
    kind,
):
    calls = []
    original = openai.OpenAI

    def handle(request):
        calls.append(request)
        assert request.url.path == "/v1/audio/transcriptions"
        assert b"current-model" in request.content and b"RIFF" in request.content
        return httpx.Response(status, json=payload)

    def create(**options):
        options.pop("http_client").close()
        return original(**options, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    monkeypatch.setattr(dictation_hosted.openai, "OpenAI", create)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/dictation/test", json={"slot": "speech", "connection": "example", "row": "row"}
        )
    assert response.status_code == 200
    assert response.json()["kind"] == kind
    assert response.json()["ok"] == (kind == "ok")
    assert "provider-private-data" not in response.text and "fake-key" not in response.text
    assert len(calls) == 1
    assert voice_store.get("local") is None


@pytest.mark.parametrize(
    "failure,kind",
    [(httpx.ConnectError("private"), "unreachable"), (httpx.ReadTimeout("private"), "timeout")],
)
async def test_speech_test_connection_failures(
    voice_store, llm, model_provider, monkeypatch, failure, kind
):
    original = openai.OpenAI

    def fail(request):
        raise failure

    def create(**options):
        options.pop("http_client").close()
        return original(**options, http_client=httpx.Client(transport=httpx.MockTransport(fail)))

    monkeypatch.setattr(dictation_hosted.openai, "OpenAI", create)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/dictation/test", json={"slot": "speech", "connection": "example", "row": "row"}
        )
    assert response.json()["kind"] == kind
    assert "private" not in response.text


@pytest.mark.parametrize(
    "content,kind",
    [
        ('{"complete":true,"text":"Usable."}', "ok"),
        ('{"complete":false,"text":"Partial"}', "rejected"),
        ('{"complete":true,"text":" "}', "rejected"),
        ("private-response", "rejected"),
    ],
)
async def test_cleanup_test_uses_production_json_contract(
    voice_store, llm, model_provider, content, kind
):
    llm.responses.create.return_value.output[0].content[0].text = content
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/dictation/test", json={"slot": "cleanup", "connection": "example", "row": "row"}
        )
    assert response.json()["kind"] == kind
    assert llm.responses.create.call_args.kwargs["model"] == "openai-compatible/current-model"
    assert "private-response" not in response.text


async def test_cleanup_follows_row_and_missing_row_keeps_legacy(voice_store, llm, model_provider):
    save_settings(
        voice_store,
        VoiceInputSettings(
            cleanup_connection="example", cleanup_row="row", model="legacy"
        ).model_dump(),
        user_id=None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        assert (
            await client.post("/v1/dictation/cleanup", json={"text": "raw"})
        ).status_code == 200
        assert llm.responses.create.call_args.kwargs["model"] == "openai-compatible/current-model"
        save_settings(
            voice_store,
            VoiceInputSettings(
                cleanup_connection="example", cleanup_row="missing", model="legacy"
            ).model_dump(),
            user_id=None,
        )
        assert (
            await client.post("/v1/dictation/cleanup", json={"text": "raw"})
        ).status_code == 200
        assert llm.responses.create.call_args.kwargs["model"] == "openai-compatible/legacy"
        response = await client.post(
            "/v1/dictation/test",
            json={"slot": "cleanup", "connection": "example", "row": "missing"},
        )
        assert response.json()["kind"] == "gone"


async def test_only_segment_and_saved_list_are_sent(
    voice_store: SqlAlchemyUserPreferencesStore, llm: SimpleNamespace
) -> None:
    save_settings(
        voice_store,
        VoiceInputSettings(
            words="ExampleApp may be heard as example app",
            model="openai/test-cleanup",
            reasoning={"effort": "low"},
            thinking={"type": "disabled"},
            timeout=3,
            reasoning_effort="low",
        ).model_dump(),
        user_id=None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/dictation/cleanup", json={"text": "don't deploy at 12:30"}
        )
    assert response.status_code == 200
    assert response.json() == {"text": "Do not deploy at 12:30."}
    call = llm.responses.create.call_args.kwargs
    assert json.loads(call["input"][0]["content"]) == {
        "segment": "don't deploy at 12:30",
        "word_list": "ExampleApp may be heard as example app",
    }
    assert len(call["input"]) == 1
    assert call["timeout"] == 3
    assert call["reasoning"] == {"effort": "low"}
    assert "reasoning_effort" not in call
    assert call["thinking"] == {"type": "disabled"}
    assert call["connection_params"] == {"base_url": "https://provider.example/v1"}


def test_settings_ignore_keys_written_by_a_newer_build(
    voice_store: SqlAlchemyUserPreferencesStore,
) -> None:
    """A rollback must read settings a newer build stored, not reject the user."""
    stored = {
        "words": "ExampleApp",
        "model": "openai/test-cleanup",
        "recognition_source": "hosted",
    }
    settings = VoiceInputSettings.model_validate(stored)
    assert settings.words == "ExampleApp"
    assert "recognition_source" not in settings.model_dump()
    saved = voice_store.patch_namespace("local", "voice_input", stored)
    assert saved["settings"]["voice_input"] == stored


@pytest.mark.parametrize("settings", [{}, {"model": "openai/test-cleanup", "mode": "off"}])
async def test_unconfigured_or_off_refuses(
    voice_store: SqlAlchemyUserPreferencesStore, llm: SimpleNamespace, settings: dict
) -> None:
    save_settings(voice_store, VoiceInputSettings(**settings).model_dump(), user_id=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/dictation/cleanup", json={"text": "raw"})
    assert response.status_code == 409
    llm.responses.create.assert_not_called()


@pytest.mark.parametrize(
    "failure,status", [(TimeoutError(), 504), (RuntimeError("provider failed"), 503)]
)
async def test_failure_keeps_text_client_side(
    voice_store: SqlAlchemyUserPreferencesStore,
    llm: SimpleNamespace,
    failure: Exception,
    status: int,
) -> None:
    save_settings(
        voice_store, VoiceInputSettings(model="openai/test-cleanup").model_dump(), user_id=None
    )
    llm.responses.create.side_effect = failure
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/dictation/cleanup", json={"text": "keep original"})
    assert response.status_code == status
    assert "provider failed" not in response.text


async def test_wall_timeout_cancels_the_call(
    voice_store: SqlAlchemyUserPreferencesStore, llm: SimpleNamespace
) -> None:
    save_settings(
        voice_store,
        VoiceInputSettings(model="openai/test-cleanup", timeout=1).model_dump(),
        user_id=None,
    )
    cancelled = asyncio.Event()

    async def blocked(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    llm.responses.create.side_effect = blocked
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/dictation/cleanup", json={"text": "keep original"})
    assert response.status_code == 504
    assert cancelled.is_set()


async def test_settings_are_authenticated_and_user_scoped(
    voice_store: SqlAlchemyUserPreferencesStore, llm: SimpleNamespace
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm, auth=True)),
        base_url="http://test",
    ) as client:
        with pytest.raises(OmnigentError, match="Authentication required"):
            await client.post("/v1/dictation/cleanup", json={"text": "raw"})


async def test_rejects_surrounding_draft_and_bad_settings(
    voice_store: SqlAlchemyUserPreferencesStore, llm: SimpleNamespace
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        assert (
            await client.post("/v1/dictation/cleanup", json={"text": "raw", "draft": "typed"})
        ).status_code == 422
    llm.responses.create.assert_not_called()


@pytest.mark.parametrize(
    "completion",
    [
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        {},
        {"status": "completed", "incomplete_details": {"reason": "max_output_tokens"}},
    ],
)
async def test_rejects_nonempty_incomplete_actual_client(voice_store, monkeypatch, completion):
    import respx

    from omnigent.llms import Client

    save_settings(
        voice_store, VoiceInputSettings(model="openai/test-cleanup").model_dump(), user_id=None
    )
    monkeypatch.setattr(
        voice_input,
        "_configured_connection",
        lambda model: (model, {"base_url": "https://provider.example/v1"}),
    )
    raw = {
        "model": "test-cleanup",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"complete": True, "text": "partial words"}),
                    }
                ],
            }
        ],
        **completion,
    }
    with respx.mock:
        respx.post("https://provider.example/v1/responses").mock(
            return_value=httpx.Response(200, json=raw)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(voice_store, Client())),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/dictation/cleanup", json={"text": "full original words and negation"}
            )
    assert response.status_code == 503


@pytest.mark.parametrize("provider", ["openai", "groq"])
@pytest.mark.parametrize("complete", [True, False])
async def test_actual_client_payload_and_model_completion(
    voice_store, monkeypatch, provider, complete
):
    import respx

    from omnigent.llms import Client

    save_settings(
        voice_store,
        VoiceInputSettings(model=f"{provider}/test-cleanup", reasoning_effort="low").model_dump(),
        user_id=None,
    )
    monkeypatch.setattr(
        voice_input,
        "_configured_connection",
        lambda model: (model, {"base_url": "https://provider.example/v1"}),
    )
    text = json.dumps({"complete": complete, "text": "all original words kept"})
    raw = (
        {
            "model": "test-cleanup",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        }
        if provider == "openai"
        else {
            "model": "test-cleanup",
            "choices": [{"finish_reason": "stop", "message": {"content": text}}],
        }
    )
    path = "responses" if provider == "openai" else "chat/completions"
    with respx.mock:
        upstream = respx.post(f"https://provider.example/v1/{path}").mock(
            return_value=httpx.Response(200, json=raw)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(voice_store, Client())),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/dictation/cleanup", json={"text": "all original words kept"}
            )
    assert response.status_code == (200 if complete else 503)
    sent = json.loads(upstream.calls[0].request.content)
    if provider == "openai":
        assert sent["reasoning"] == {"effort": "low"}
        assert "reasoning_effort" not in sent
    else:
        assert sent["reasoning_effort"] == "low"


@pytest.mark.parametrize("finish", ["length", None, "content_filter"])
async def test_chat_client_rejects_partial_output(voice_store, monkeypatch, finish):
    import respx

    from omnigent.llms import Client

    save_settings(
        voice_store, VoiceInputSettings(model="groq/test-cleanup").model_dump(), user_id=None
    )
    monkeypatch.setattr(
        voice_input,
        "_configured_connection",
        lambda model: (model, {"base_url": "https://provider.example/v1"}),
    )
    raw = {
        "model": "test-cleanup",
        "choices": [
            {
                "finish_reason": finish,
                "message": {"content": json.dumps({"complete": True, "text": "partial words"})},
            }
        ],
    }
    with respx.mock:
        respx.post("https://provider.example/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=raw)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(voice_store, Client())),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/dictation/cleanup", json={"text": "full original words and negation"}
            )
    assert response.status_code == 503


@pytest.mark.parametrize("rules", ["", "   ", "Keep short sentences."])
async def test_cleanup_uses_saved_rules_and_fixed_format(voice_store, llm, rules):
    from omnigent.server.voice_input import CLEANUP_OUTPUT_FORMAT, CLEANUP_RULES

    save_settings(
        voice_store,
        VoiceInputSettings(model="openai/test-cleanup", instructions=rules).model_dump(),
        user_id=None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        response = await client.post("/v1/dictation/cleanup", json={"text": "raw words"})
    assert response.status_code == 200
    assert (
        llm.responses.create.call_args.kwargs["instructions"]
        == (rules.strip() or CLEANUP_RULES) + "\n" + CLEANUP_OUTPUT_FORMAT
    )


def test_instruction_limit_and_legacy_mode():
    from pydantic import ValidationError

    assert VoiceInputSettings(mode="manual").mode == "automatic"
    assert VoiceInputSettings(instructions="x" * 4000).instructions == "x" * 4000
    with pytest.raises(ValidationError):
        VoiceInputSettings(instructions="x" * 4001)


@pytest.mark.parametrize(
    "wire_api,prefix", [("chat", "openai-compatible"), ("responses", "openai")]
)
def test_external_cleanup_routes_by_api(monkeypatch, wire_api, prefix):
    from omnigent.server.model_connections import ResolvedConnection

    monkeypatch.setattr(
        voice_input,
        "resolve_connection",
        lambda name: ResolvedConnection("https://provider.example/v1", "fake-key", wire_api),
    )
    model, params = voice_input._cleanup_target(
        VoiceInputSettings(cleanup_connection="example", model="namespace/test-model")
    )
    assert model == f"{prefix}/namespace/test-model"
    assert params == {"base_url": "https://provider.example/v1", "api_key": "fake-key"}


async def test_deleted_cleanup_provider_returns_conflict(voice_store, llm, monkeypatch):
    from omnigent.server.model_connections import ConnectionUnavailable

    def missing(name):
        raise ConnectionUnavailable("Provider unavailable")

    monkeypatch.setattr(voice_input, "resolve_connection", missing)
    save_settings(
        voice_store, {"model": "test-model", "cleanup_connection": "removed"}, user_id=None
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(voice_store, llm)), base_url="http://test"
    ) as client:
        result = await client.post("/v1/dictation/cleanup", json={"text": "original"})
    assert result.status_code == 409
    llm.responses.create.assert_not_called()


async def test_external_adapter_redacts_provider_error_body(voice_store, monkeypatch, caplog):
    from omnigent.llms.adapters import clear_cache
    from omnigent.server.model_connections import ResolvedConnection

    monkeypatch.setattr(
        voice_input,
        "resolve_connection",
        lambda name: ResolvedConnection("https://provider.example/v1", "fake-credential"),
    )
    save_settings(
        voice_store, {"model": "test-model", "cleanup_connection": "example"}, user_id=None
    )
    original = httpx.AsyncClient

    def handle(request):
        return httpx.Response(401, text="fake-credential should never be logged")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    clear_cache()
    async with original(
        transport=httpx.ASGITransport(app=app_for(voice_store, voice_input.Client())),
        base_url="http://test",
    ) as client:
        response = await client.post("/v1/dictation/cleanup", json={"text": "original"})
    assert response.status_code == 503
    assert "fake-credential" not in caplog.text + response.text
