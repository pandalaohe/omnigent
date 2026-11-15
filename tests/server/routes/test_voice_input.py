"""Voice cleanup sends only the segment and the caller's vocabulary."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import OmnigentError
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
